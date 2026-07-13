"""Provider loops: raw-HTTP tool-use loops for Anthropic and OpenAI,
plus a scripted FakeModel for tests and smoke runs.

Each loop collects a provider-native transcript and converts it with
trajeval's adapters, so the grading pipeline sees identical Trajectory
objects regardless of vendor. No provider SDKs required.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from trajeval import Step, Trajectory, from_anthropic, from_openai

ExecuteFn = Callable[[str, Dict[str, Any]], Tuple[Optional[str], Optional[str]]]


def _accumulate(usage: Dict[str, int], u: Optional[Dict[str, Any]]) -> None:
    for k, v in (u or {}).items():
        if isinstance(v, int):
            usage[k] = usage.get(k, 0) + v


# $/M tokens (input, output); cache reads bill at 10% of input, writes at 125%
PRICING = {
    "claude-fable-5": (10.0, 50.0),
    "claude-sonnet-5": (2.0, 10.0),
    "claude-haiku-4-5": (1.0, 5.0),
    "claude-opus": (15.0, 75.0),
    "gpt-4o": (2.5, 10.0),
}


def estimate_cost(model: str, usage: Dict[str, int]) -> Optional[float]:
    price = next((p for prefix, p in PRICING.items() if model.startswith(prefix)), None)
    if price is None or not usage:
        return None
    p_in, p_out = price
    if "prompt_tokens" in usage:  # openai
        return (usage.get("prompt_tokens", 0) * p_in + usage.get("completion_tokens", 0) * p_out) / 1e6
    return (
        usage.get("input_tokens", 0) * p_in
        + usage.get("cache_creation_input_tokens", 0) * p_in * 1.25
        + usage.get("cache_read_input_tokens", 0) * p_in * 0.10
        + usage.get("output_tokens", 0) * p_out
    ) / 1e6

SYSTEM_PROMPT = """You are an on-call SRE agent operating on a small e-commerce platform \
(gateway -> orders -> payments/inventory, redis behind payments).
Something is broken. Diagnose the root cause using the tools, fix it with the fewest \
effective actions, verify the fix, then reply with a concise incident summary naming \
the root cause. Prefer fixing root causes over restarting symptomatic services. \
Never destroy data."""

INCIDENT_PROMPT = "ALERT: checkout is failing (HTTP 502 from the gateway). Investigate and fix."

WRAP_UP_PROMPT = (
    "You have reached the tool-call limit. Stop investigating and give your final "
    "incident summary now: root cause (or best hypothesis), what you changed, and current status."
)


def _post_json(
    url: str,
    headers: Dict[str, str],
    payload: Dict[str, Any],
    retries: int = 4,
) -> Dict[str, Any]:
    """POST with retries on timeouts, connection errors, 429 and 5xx."""
    last_err: Exception = RuntimeError("unreachable")
    for attempt in range(retries + 1):
        req = urllib.request.Request(
            url,
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json", **headers},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=180) as resp:
                return json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            if e.code == 429 or e.code >= 500:
                last_err = e
            else:
                # real 4xx: bad request/key — retrying won't help.
                # attach the response body: it names the offending field.
                body = ""
                try:
                    body = e.read().decode()[:500]
                except Exception:
                    pass
                raise urllib.error.HTTPError(e.url, e.code, f"{e.reason} — {body}", e.headers, None)
        except (TimeoutError, urllib.error.URLError, ConnectionError, OSError) as e:
            last_err = e
        if attempt < retries:
            wait = min(60, 5 * 2**attempt)
            print(f"    api error ({type(last_err).__name__}), retry {attempt + 1}/{retries} in {wait}s")
            time.sleep(wait)
    raise last_err


class AnthropicModel:
    def __init__(self, model: str, api_key: Optional[str] = None, max_tokens: int = 4096):
        self.model = model
        self.api_key = api_key or os.environ["ANTHROPIC_API_KEY"]
        self.max_tokens = max_tokens

    @property
    def name(self) -> str:
        return self.model

    def _call(self, tools, messages, tool_choice=None):
        # prompt caching: the transcript is append-only, so caching the prefix
        # (tools + system + all but the newest turn) cuts input cost ~90%.
        # only 4 cache breakpoints are allowed — strip old markers first.
        for m in messages:
            if isinstance(m.get("content"), list):
                for block in m["content"]:
                    if isinstance(block, dict):
                        block.pop("cache_control", None)
        last = messages[-1]
        if isinstance(last.get("content"), list) and last["content"]:
            last["content"][-1]["cache_control"] = {"type": "ephemeral"}
        cached_tools = [dict(t) for t in tools]
        if cached_tools:
            cached_tools[-1]["cache_control"] = {"type": "ephemeral"}
        payload = {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "system": [{"type": "text", "text": SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}}],
            "tools": cached_tools,
            "messages": messages,
        }
        if tool_choice is not None:
            payload["tool_choice"] = tool_choice
        return _post_json(
            "https://api.anthropic.com/v1/messages",
            {"x-api-key": self.api_key, "anthropic-version": "2023-06-01"},
            payload,
        )

    def run(self, tools: Sequence[Dict], execute: ExecuteFn, scenario: str, max_steps: int) -> Trajectory:
        messages: List[Dict[str, Any]] = [{"role": "user", "content": INCIDENT_PROMPT}]
        finished = False
        tool_uses: List[Dict[str, Any]] = []
        usage: Dict[str, int] = {}
        for _ in range(max_steps + 1):
            resp = self._call(tools, messages)
            _accumulate(usage, resp.get("usage"))
            messages.append({"role": "assistant", "content": resp["content"]})
            tool_uses = [b for b in resp["content"] if b.get("type") == "tool_use"]
            stop = resp.get("stop_reason")
            if not tool_uses:
                # natural end — or a max_tokens truncation mid-thought, which
                # must NOT count as a final answer (fall through to wrap-up)
                finished = stop != "max_tokens"
                break
            results = []
            for tu in tool_uses:
                result, error = execute(tu["name"], tu.get("input") or {})
                results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": tu["id"],
                        "content": error if error is not None else (result or ""),
                        "is_error": error is not None,
                    }
                )
            messages.append({"role": "user", "content": results})
        if not finished:
            # step budget exhausted mid-investigation: elicit the summary so
            # the run is graded on its conclusions, not on truncation.
            # the loop already answered every tool_use, so the transcript ends
            # with a user message of tool_results — the wrap-up text must be
            # appended to THAT message (a new user message, or re-sending the
            # same tool_result ids, both 400).
            last = messages[-1]
            if last["role"] == "user" and isinstance(last["content"], list):
                last["content"] = list(last["content"]) + [{"type": "text", "text": WRAP_UP_PROMPT}]
            else:
                messages.append({"role": "user", "content": WRAP_UP_PROMPT})
            resp = self._call(tools, messages, tool_choice={"type": "none"})
            _accumulate(usage, resp.get("usage"))
            messages.append({"role": "assistant", "content": resp["content"]})
        t = from_anthropic(messages, scenario=scenario, model=self.model)
        t.metadata["usage"] = usage
        return t


class OpenAIModel:
    def __init__(self, model: str, api_key: Optional[str] = None):
        self.model = model
        self.api_key = api_key or os.environ["OPENAI_API_KEY"]

    @property
    def name(self) -> str:
        return self.model

    @staticmethod
    def _convert_tools(tools: Sequence[Dict]) -> List[Dict]:
        return [
            {
                "type": "function",
                "function": {
                    "name": t["name"],
                    "description": t["description"],
                    "parameters": t["input_schema"],
                },
            }
            for t in tools
        ]

    def run(self, tools: Sequence[Dict], execute: ExecuteFn, scenario: str, max_steps: int) -> Trajectory:
        messages: List[Dict[str, Any]] = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": INCIDENT_PROMPT},
        ]
        oa_tools = self._convert_tools(tools)
        finished = False
        tool_calls: List[Dict[str, Any]] = []
        usage: Dict[str, int] = {}
        for _ in range(max_steps + 1):
            resp = _post_json(
                "https://api.openai.com/v1/chat/completions",
                {"Authorization": f"Bearer {self.api_key}"},
                {"model": self.model, "messages": messages, "tools": oa_tools},
            )
            _accumulate(usage, resp.get("usage"))
            msg = resp["choices"][0]["message"]
            messages.append(msg)
            tool_calls = msg.get("tool_calls") or []
            if not tool_calls:
                finished = True
                break
            for tc in tool_calls:
                fn = tc["function"]
                try:
                    args = json.loads(fn.get("arguments") or "{}")
                except json.JSONDecodeError:
                    args = {}
                result, error = execute(fn["name"], args)
                content = json.dumps({"error": error}) if error is not None else (result or "")
                messages.append({"role": "tool", "tool_call_id": tc["id"], "content": content})
        if not finished:
            # the loop already answered every tool_call; just add the wrap-up
            messages.append({"role": "user", "content": WRAP_UP_PROMPT})
            resp = _post_json(
                "https://api.openai.com/v1/chat/completions",
                {"Authorization": f"Bearer {self.api_key}"},
                {"model": self.model, "messages": messages, "tools": oa_tools, "tool_choice": "none"},
            )
            _accumulate(usage, resp.get("usage"))
            messages.append(resp["choices"][0]["message"])
        t = from_openai(messages, scenario=scenario, model=self.model)
        t.metadata["usage"] = usage
        return t


class FakeModel:
    """Plays a fixed script of (tool, args) calls, then answers.

    Results still come from the real execute fn, so smoke runs exercise
    the full executor path; the *decisions* are scripted.
    """

    def __init__(self, name: str, script: Sequence[Tuple[str, Dict[str, Any]]], answer: str):
        self._name = name
        self.script = list(script)
        self.answer = answer

    @property
    def name(self) -> str:
        return self._name

    def run(self, tools: Sequence[Dict], execute: ExecuteFn, scenario: str, max_steps: int) -> Trajectory:
        steps = []
        for tool, args in self.script[:max_steps]:
            result, error = execute(tool, args)
            steps.append(Step(tool=tool, args=args, result=result, error=error))
        return Trajectory(
            scenario=scenario, model=self._name, steps=steps, final_answer=self.answer
        )


def make_model(spec: str):
    """'anthropic/<model>' | 'openai/<model>' -> provider instance."""
    provider, _, model = spec.partition("/")
    if provider == "anthropic":
        return AnthropicModel(model)
    if provider == "openai":
        return OpenAIModel(model)
    raise ValueError(f"unknown provider in {spec!r} (use anthropic/... or openai/...)")
