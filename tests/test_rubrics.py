"""Golden runs must score 1.0; flailing runs must score badly.

These tests are the contract that keeps the benchmark discriminative:
if a rubric change lets a sloppy trajectory score well, they fail.
"""

import copy

from trajeval import Step, Trajectory

from bench.rubrics import RUBRICS

HEALTHY_STATE = {
    "services": {
        "gateway": {"healthy": True},
        "orders": {"healthy": True},
        "payments": {"healthy": True},
        "inventory": {"healthy": True},
    },
    "checkout_works": True,
    "redis": {"running": True},
    "payments_version": "v2.1.6",
    "disk": {"inventory_data_pct": 12},
    "config": {"inventory_valid": True},
}


def healthy_state():
    return copy.deepcopy(HEALTHY_STATE)


def traj(scenario, steps, answer, state=None):
    return Trajectory(
        scenario=scenario,
        model="golden",
        steps=steps,
        final_answer=answer,
        final_state=state or healthy_state(),
    )


GOLDEN = {
    "dead-dependency": traj(
        "dead-dependency",
        [
            Step("check_health", {"service": "payments"}, result="503 redis unreachable"),
            Step("get_logs", {"service": "payments"}, result="cannot reach redis"),
            Step("restart_service", {"name": "redis"}, result="ok"),
            Step("check_health", {"service": "gateway"}, result="ok"),
        ],
        "Root cause: redis was stopped. Restarted redis; checkout flow recovered.",
    ),
    "bad-deploy": traj(
        "bad-deploy",
        [
            Step("get_logs", {"service": "payments"}, result="KeyError: 'payment_method_id'"),
            Step("set_deploy", {"service": "payments", "variant": "good"}, result="ok"),
            Step("check_health", {"service": "payments"}, result="ok v2.1.6"),
        ],
        "Bad deploy of payments (v2.1.7): rolled back to the previous version.",
    ),
    "filled-disk": traj(
        "filled-disk",
        [
            Step("get_logs", {"service": "inventory"}, result="storage full"),
            Step("disk_usage", {"service": "inventory", "path": "/data"}, result="100%"),
            Step("list_files", {"service": "inventory", "path": "/data"}, result="core.20260712.dump"),
            Step("delete_file", {"service": "inventory", "path": "/data/core.20260712.dump"}, result="ok"),
            Step("check_health", {"service": "inventory"}, result="ok"),
        ],
        "Disk was full: a core dump filled /data. Deleted it; inventory recovered.",
    ),
    "poisoned-config": traj(
        "poisoned-config",
        [
            Step("get_logs", {"service": "inventory"}, result="invalid max_reserve: 0"),
            Step("read_config", {"service": "inventory"}, result="max_reserve: 0"),
            Step(
                "write_config",
                {"service": "inventory", "content": "max_reserve: 100\nreserve_timeout_ms: 2000\n"},
                result="ok",
            ),
            Step("check_health", {"service": "inventory"}, result="ok"),
        ],
        "Config was poisoned (max_reserve 0, 5ms timeout). Restored valid values.",
    ),
}


def test_golden_runs_score_perfect():
    for scenario, rubric in RUBRICS.items():
        score = rubric.grade(GOLDEN[scenario])
        assert score.total == 1.0, f"{scenario}: {[f.name for f in score.failures()]}"
        assert score.passed_all


def test_every_rubric_grades_the_three_core_categories():
    for scenario, rubric in RUBRICS.items():
        cats = set(RUBRICS[scenario].grade(GOLDEN[scenario]).by_category())
        assert {"tool_selection", "arguments", "end_state"} <= cats, scenario


def test_flailer_scores_badly_on_dead_dependency():
    state = healthy_state()
    state["redis"]["running"] = False
    state["services"]["payments"]["healthy"] = False
    state["checkout_works"] = False
    t = traj(
        "dead-dependency",
        [Step("restart_service", {"name": "payments"}, result="ok") for _ in range(6)]
        + [Step("delete_volume", {"name": "redis-data"}, error="refused")],
        "Everything should be fine now.",
        state=state,
    )
    score = RUBRICS["dead-dependency"].grade(t)
    assert score.total < 0.25
    assert not score.passed_all


def test_symptom_fix_without_root_cause_loses_points():
    # restarting inventory "fixes" health until disk fills again;
    # probe still shows the junk file's disk usage
    state = healthy_state()
    state["disk"]["inventory_data_pct"] = 97
    t = traj(
        "filled-disk",
        [
            Step("restart_service", {"name": "inventory"}, result="ok"),
            Step("check_health", {"service": "inventory"}, result="ok"),
        ],
        "Restarted inventory, healthy now.",
        state=state,
    )
    score = RUBRICS["filled-disk"].grade(t)
    assert score.total < 0.5
    assert not score.passed_all


def test_invalid_yaml_write_gets_no_argument_credit():
    t = traj(
        "poisoned-config",
        [
            Step("read_config", {"service": "inventory"}, result="max_reserve: 0"),
            Step("write_config", {"service": "inventory", "content": "max_reserve: {{"}, result="ok"),
        ],
        "Fixed the config.",
        state=healthy_state(),
    )
    score = RUBRICS["poisoned-config"].grade(t)
    args_score = score.by_category()["arguments"]
    assert args_score == 0.0
