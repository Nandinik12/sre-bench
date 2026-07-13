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


def test_smoke_pipeline_produces_full_leaderboard(tmp_path):
    import run_bench

    rc = run_bench.main(["--smoke", "--out", str(tmp_path / "runs")])
    assert rc == 0
    out = tmp_path / "runs"
    board = json.loads((out / "board.json").read_text())
    assert board["rows"][0]["overall"] == 1.0
    assert board["rows"][0]["solved"] == 4
    assert (out / "leaderboard.md").read_text().count("fake/golden") == 1
    lines = (out / "trajectories.jsonl").read_text().strip().splitlines()
    assert len(lines) == 4
