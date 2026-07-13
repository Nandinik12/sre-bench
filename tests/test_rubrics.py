"""Golden runs must score 1.0; flailing runs must score badly.

These tests are the contract that keeps the benchmark discriminative:
if a rubric change lets a sloppy trajectory score well, they fail.
"""

from trajeval import Step, Trajectory

from bench.goldens import GOLDEN_SCRIPTS, healthy_state
from bench.rubrics import RUBRICS


def traj(scenario, steps, answer, state=None):
    return Trajectory(
        scenario=scenario,
        model="golden",
        steps=steps,
        final_answer=answer,
        final_state=state or healthy_state(),
    )


# built from the single source of truth in bench/goldens.py
GOLDEN = {
    scenario: traj(scenario, [Step(tool, args, result="ok") for tool, args in script], answer)
    for scenario, (script, answer) in GOLDEN_SCRIPTS.items()
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


def test_slow_but_correct_run_still_counts_as_solved():
    """Efficiency shapes the score but doesn't gate 'solved' — a slow fix
    is still a fix. (Surfaced by the first live claude-sonnet-5 run.)"""
    golden = GOLDEN["dead-dependency"]
    t = traj(
        "dead-dependency",
        list(golden.steps) + [Step("check_health", {"service": "orders"})] * 8,
        golden.final_answer,
    )
    score = RUBRICS["dead-dependency"].grade(t)
    assert score.total < 1.0
    assert score.passed_all


def test_runaway_retry_restart_does_not_count_as_fix():
    """The classic wrong move: restart orders. Queue persists in redis,
    config still unbounded — end state stays broken."""
    state = healthy_state()
    state["config"]["orders_retry_bounded"] = False
    state["orders_retry_queue"] = 6
    state["services"]["orders"]["healthy"] = False
    state["checkout_works"] = False
    t = traj(
        "runaway-retry",
        [
            Step("get_logs", {"service": "orders"}, result="retrying doomed-1 (UNBOUNDED)"),
            Step("restart_service", {"name": "orders"}, result="ok"),
            Step("restart_service", {"name": "orders"}, result="ok"),
        ],
        "Restarted orders, should recover.",
        state=state,
    )
    score = RUBRICS["runaway-retry"].grade(t)
    assert score.total < 0.3
    assert not score.passed_all


def test_compound_outage_fixing_only_one_fault_is_not_solved():
    state = healthy_state()
    state["config"]["inventory_valid"] = False  # config fault never fixed
    state["services"]["inventory"]["healthy"] = False
    state["checkout_works"] = False
    t = traj(
        "compound-outage",
        [
            Step("get_logs", {"service": "payments"}, result="redis down"),
            Step("restart_service", {"name": "redis"}, result="ok"),
            Step("check_health", {"service": "payments"}, result="ok"),
        ],
        "redis was down, restarted it.",
        state=state,
    )
    score = RUBRICS["compound-outage"].grade(t)
    assert not score.passed_all
    assert score.total < 0.6


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
