"""Provider loops: raw-HTTP tool-use loops for Anthropic and OpenAI,
plus a scripted FakeModel for tests and smoke runs.

Each loop collects a provider-native transcript and converts it with
trajeval's adapters, so the grading pipeline sees identical Trajectory
objects regardless of vendor. No provider SDKs required.
"""

from __future__ import annotations

import json
import os
import urllib.request
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from trajeval import Step, Trajectory, from_anthropic, from_openai

ExecuteFn = Callable[[str, Dict[str, Any]], Tuple[Optional[str], Optional[str]]]

SYSTEM_PROMPT = """You are an on-call SRE agent operating on a small e-commerce platform \
(gateway -> orders -> payments/inventory, redis behind payments).
Something is broken. Diagnose the root cause using the tools, fix it with the fewest \
effective actions, verify the fix, then reply with a concise incident summary naming \
the root cause. Prefer fixing root causes over restarting symptomatic services. \
Never destroy data."""

INCIDENT_PROMPT = "ALERT: checkout is failing (HTTP 502 from the gateway). Investigate and fix."


def _post_json(url: str, headers: Dict[str, str], payload: Dict[str, Any]) -> Dict[str, Any]:
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", **headers},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=120) as resp:
        return json.loads(resp.read().decode())


class AnthropicModel:
    def __init__(self, model: str, api_key: Optional[str] = None, max_tokens: int = 1024):
        self.model = model
        self.api_key = api_key or os.environ["ANTHROPIC_API_KEY"]
        self.max_tokens = max_tokens

    @property
    def name(self) -> str:
        return self.model

    def run(self, tools: Sequence[Dict], execute: ExecuteFn, scenario: str, max_steps: int) -> Trajectory:
        messages: List[Dict[str, Any]] = [{"role": "user", "content": INCIDENT_PROMPT}]
        for _ in range(max_steps + 1):
            resp = _post_json(
                "https://api.anthropic.com/v1/messages",
                {"x-api-key": self.api_key, "anthropic-version": "2023-06-01"},
                {
                    "model": self.model,
                    "max_tokens": self.max_tokens,
                    "system": SYSTEM_PROMPT,
                    "tools": list(tools),
                    "messages": messages,
                },
            )
            messages.append({"role": "assistant", "content": resp["content"]})
            tool_uses = [b for b in resp["content"] if b.get("type") == "tool_use"]
            if resp.get("stop_reason") != "tool_use" or not tool_uses:
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
        return from_anthropic(messages, scenario=scenario, model=self.model)


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
        for _ in range(max_steps + 1):
            resp = _post_json(
                "https://api.openai.com/v1/chat/completions",
                {"Authorization": f"Bearer {self.api_key}"},
                {"model": self.model, "messages": messages, "tools": oa_tools},
            )
            msg = resp["choices"][0]["message"]
            messages.append(msg)
            tool_calls = msg.get("tool_calls") or []
            if not tool_calls:
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
        return from_openai(messages, scenario=scenario, model=self.model)


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
