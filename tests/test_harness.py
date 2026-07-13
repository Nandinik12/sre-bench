"""Harness loop tests with mocked providers — no network, no docker."""

import json

from trajeval import Leaderboard

from bench.goldens import GOLDEN_SCRIPTS, healthy_state
from bench.rubrics import RUBRICS
from harness.providers import AnthropicModel, FakeModel, OpenAIModel, make_model
from harness.tools import TOOLS


def stub_execute(name, args):
    return "ok", None


def test_fake_model_golden_runs_score_one():
    for scenario, (script, answer) in GOLDEN_SCRIPTS.items():
        model = FakeModel("fake/golden", script, answer)
        t = model.run(TOOLS, stub_execute, scenario, max_steps=12)
        t.final_state = healthy_state()
        score = RUBRICS[scenario].grade(t)
        assert score.total == 1.0, f"{scenario}: {[f.name for f in score.failures()]}"


def test_anthropic_loop_with_mocked_api(monkeypatch):
    responses = [
        {
            "stop_reason": "tool_use",
            "content": [
                {"type": "text", "text": "checking"},
                {"type": "tool_use", "id": "t1", "name": "get_logs", "input": {"service": "payments"}},
            ],
        },
        {
            "stop_reason": "end_turn",
            "content": [{"type": "text", "text": "redis was down; fixed."}],
        },
    ]
    calls = []

    def fake_post(url, headers, payload):
        calls.append(payload)
        return responses[len(calls) - 1]

    monkeypatch.setattr("harness.providers._post_json", fake_post)
    m = AnthropicModel("claude-test", api_key="k")
    t = m.run(TOOLS, stub_execute, "dead-dependency", max_steps=5)
    assert t.tool_sequence() == ["get_logs"]
    assert t.steps[0].result == "ok"
    assert t.final_answer == "redis was down; fixed."
    # transcript sanity: tools were sent, system prompt present
    assert calls[0]["tools"] == list(TOOLS)
    assert "SRE" in calls[0]["system"]


def test_anthropic_loop_records_tool_errors(monkeypatch):
    responses = [
        {
            "stop_reason": "tool_use",
            "content": [{"type": "tool_use", "id": "t1", "name": "delete_volume", "input": {"name": "x"}}],
        },
        {"stop_reason": "end_turn", "content": [{"type": "text", "text": "done"}]},
    ]
    n = [0]

    def fake_post(url, headers, payload):
        n[0] += 1
        return responses[n[0] - 1]

    monkeypatch.setattr("harness.providers._post_json", fake_post)

    def refuse(name, args):
        return None, "refused: destructive"

    t = AnthropicModel("claude-test", api_key="k").run(TOOLS, refuse, "s", 5)
    assert t.steps[0].error == "refused: destructive"


def test_openai_loop_with_mocked_api(monkeypatch):
    responses = [
        {
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {
                                "id": "c1",
                                "type": "function",
                                "function": {
                                    "name": "get_logs",
                                    "arguments": json.dumps({"service": "payments"}),
                                },
                            }
                        ],
                    }
                }
            ]
        },
        {"choices": [{"message": {"role": "assistant", "content": "fixed it"}}]},
    ]
    n = [0]

    def fake_post(url, headers, payload):
        n[0] += 1
        return responses[n[0] - 1]

    monkeypatch.setattr("harness.providers._post_json", fake_post)
    t = OpenAIModel("gpt-test", api_key="k").run(TOOLS, stub_execute, "s", 5)
    assert t.tool_sequence() == ["get_logs"]
    assert t.final_answer == "fixed it"


def test_make_model_routing(monkeypatch):
    import pytest

    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    m = make_model("anthropic/claude-x")
    assert isinstance(m, AnthropicModel) and m.name == "claude-x"
    with pytest.raises(ValueError):
        make_model("mystery/model-9000")


def test_anthropic_exhausted_budget_still_elicits_final_answer(monkeypatch):
    """When the step limit is hit, the loop must ask for a summary with
    tool_choice=none so runs are graded on conclusions, not truncation."""
    import copy

    payloads = []

    def fake_post(url, headers, payload):
        payloads.append(copy.deepcopy(payload))
        if payload.get("tool_choice") == {"type": "none"}:
            return {"stop_reason": "end_turn",
                    "content": [{"type": "text", "text": "hypothesis: bad deploy of payments"}]}
        return {
            "stop_reason": "tool_use",
            "content": [{"type": "tool_use", "id": f"t{len(payloads)}",
                         "name": "get_logs", "input": {"service": "payments"}}],
        }

    monkeypatch.setattr("harness.providers._post_json", fake_post)
    t = AnthropicModel("claude-test", api_key="k").run(TOOLS, stub_execute, "bad-deploy", max_steps=3)
    assert t.final_answer == "hypothesis: bad deploy of payments"
    assert payloads[-1]["tool_choice"] == {"type": "none"}
    assert len(t.steps) == 4  # max_steps + 1 loop iterations, all tool calls
    # the API 400s if dangling tool_use blocks aren't answered before the
    # wrap-up prompt: last user message must lead with their tool_results
    last_user = payloads[-1]["messages"][-1]
    assert last_user["role"] == "user"
    kinds = [b["type"] for b in last_user["content"]]
    assert kinds[0] == "tool_result" and kinds[-1] == "text"


def test_send_test_checkout_tool_exists_and_posts_to_gateway():
    from harness.tools import plan

    p = plan("send_test_checkout", {})
    assert p[0] == "http_post" and ":8080/checkout" in p[1]
    assert p[2]["sku"] == "synthetic-test"


def test_post_json_retries_timeouts_then_succeeds(monkeypatch):
    import io
    import urllib.request

    from harness import providers

    attempts = [0]

    class FakeResp(io.BytesIO):
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def flaky_urlopen(req, timeout=0):
        attempts[0] += 1
        if attempts[0] < 3:
            raise TimeoutError("read timed out")
        return FakeResp(b'{"ok": true}')

    monkeypatch.setattr(urllib.request, "urlopen", flaky_urlopen)
    monkeypatch.setattr(providers.time, "sleep", lambda s: None)
    out = providers._post_json("https://x", {}, {})
    assert out == {"ok": True}
    assert attempts[0] == 3


def test_post_json_does_not_retry_bad_request(monkeypatch):
    import urllib.error
    import urllib.request

    import pytest

    from harness import providers

    attempts = [0]

    def bad_request(req, timeout=0):
        attempts[0] += 1
        raise urllib.error.HTTPError("https://x", 400, "bad request", {}, None)

    monkeypatch.setattr(urllib.request, "urlopen", bad_request)
    monkeypatch.setattr(providers.time, "sleep", lambda s: None)
    with pytest.raises(urllib.error.HTTPError):
        providers._post_json("https://x", {}, {})
    assert attempts[0] == 1


def test_smoke_pipeline_produces_full_leaderboard(tmp_path):
    import run_bench

    rc = run_bench.main(["--smoke", "--out", str(tmp_path / "runs")])
    assert rc == 0
    out = tmp_path / "runs"
    board = json.loads((out / "board.json").read_text())
    assert board["rows"][0]["overall"] == 1.0
    assert board["rows"][0]["solved"] == len(board["scenarios"])
    assert (out / "leaderboard.md").read_text().count("fake/golden") == 1
    lines = (out / "trajectories.jsonl").read_text().strip().splitlines()
    assert len(lines) == len(board["scenarios"])
