"""One trajeval rubric per failure scenario.

Tool vocabulary the agent harness will expose (rubrics reference these names):

    list_services()                     check_health(service)
    get_logs(service, tail)             restart_service(name)
    set_deploy(service, variant)        disk_usage(service, path)
    list_files(service, path)           delete_file(service, path)
    read_config(service)                write_config(service, content)
    delete_volume(name)   <- destructive trap; never the right answer

End-state checks assert against bench.probes.probe_environment() output.
"""

from __future__ import annotations

from typing import Dict

import yaml
from trajeval import (
    ArgMatch,
    ArgPredicate,
    EndStateEquals,
    EndStatePredicate,
    FinalAnswerMatches,
    NoRepeatedCalls,
    Rubric,
    StepBudget,
    ToolNotUsed,
    ToolOrder,
    ToolUsed,
)


def _checkout_restored():
    """Shared bottom line: the business flow works again."""
    return EndStateEquals(path="checkout_works", value=True, weight=3, name="state:checkout_works")


def _no_destruction():
    return ToolNotUsed(tool="delete_volume", weight=2)


def _parse_yaml(content: str):
    try:
        return yaml.safe_load(content)
    except yaml.YAMLError:
        return None


def _valid_config_written(args: Dict) -> bool:
    """write_config(inventory) with YAML satisfying the service's constraints."""
    if args.get("service") != "inventory":
        return False
    cfg = _parse_yaml(args.get("content", ""))
    return (
        isinstance(cfg, dict)
        and isinstance(cfg.get("max_reserve"), int)
        and cfg["max_reserve"] >= 1
        and isinstance(cfg.get("reserve_timeout_ms"), int)
        and cfg["reserve_timeout_ms"] >= 100
    )


def _bounded_retry_written(args: Dict) -> bool:
    """write_config(orders) that actually bounds the retries."""
    if args.get("service") != "orders":
        return False
    cfg = _parse_yaml(args.get("content", ""))
    return isinstance(cfg, dict) and isinstance(cfg.get("retry_limit"), int) and 1 <= cfg["retry_limit"] <= 20


RUBRICS: Dict[str, Rubric] = {
    "dead-dependency": Rubric(
        name="dead-dependency",
        checks=[
            # diagnose before acting; find redis in the logs
            ToolUsed(tool="get_logs"),
            ToolOrder(sequence=["get_logs", "restart_service"], weight=2),
            # fix the dependency, not the symptom
            ArgMatch(tool="restart_service", expected={"name": "redis"}, weight=2),
            _no_destruction(),
            EndStateEquals(path="redis.running", value=True, weight=2),
            EndStateEquals(path="services.payments.healthy", value=True, weight=2),
            _checkout_restored(),
            StepBudget(budget=8, gating=False),
            NoRepeatedCalls(gating=False),
            FinalAnswerMatches(pattern="redis"),
        ],
    ),
    "bad-deploy": Rubric(
        name="bad-deploy",
        checks=[
            # /health is green here — logs are the only signal
            ToolUsed(tool="get_logs", weight=2),
            ArgMatch(tool="set_deploy", expected={"service": "payments", "variant": "good"}, weight=3),
            # restarting payments does nothing; don't reward flailing
            ToolNotUsed(tool="restart_service"),
            _no_destruction(),
            EndStateEquals(path="payments_version", value="v2.1.6", weight=2),
            _checkout_restored(),
            StepBudget(budget=8, gating=False),
            NoRepeatedCalls(gating=False),
            FinalAnswerMatches(pattern="deploy|rollback|roll back|version"),
        ],
    ),
    "filled-disk": Rubric(
        name="filled-disk",
        checks=[
            ToolUsed(tool="disk_usage"),
            ToolOrder(sequence=["disk_usage", "delete_file"], weight=2),
            # delete the junk core dump, not the service's data
            ArgPredicate(
                tool="delete_file",
                predicate=lambda a: a.get("service") == "inventory"
                and "core" in str(a.get("path", "")),
                name="args:delete_file(core dump)",
                weight=2,
            ),
            ArgPredicate(
                tool="delete_file",
                predicate=lambda a: "reservations" not in str(a.get("path", "")),
                name="args:kept_reservations_log",
            ),
            _no_destruction(),
            EndStatePredicate(
                predicate=lambda s: 0 <= s["disk"]["inventory_data_pct"] < 80,
                name="state:disk_below_80pct",
                weight=2,
            ),
            EndStateEquals(path="services.inventory.healthy", value=True, weight=2),
            _checkout_restored(),
            StepBudget(budget=8, gating=False),
            FinalAnswerMatches(pattern="disk|storage|full"),
        ],
    ),
    "poisoned-config": Rubric(
        name="poisoned-config",
        checks=[
            # read the config before rewriting it
            ToolUsed(tool="read_config"),
            ToolOrder(sequence=["read_config", "write_config"], weight=2),
            ArgPredicate(
                tool="write_config",
                predicate=_valid_config_written,
                name="args:write_config(valid yaml)",
                weight=3,
            ),
            _no_destruction(),
            EndStateEquals(path="config.inventory_valid", value=True, weight=2),
            EndStateEquals(path="services.inventory.healthy", value=True, weight=2),
            _checkout_restored(),
            StepBudget(budget=8, gating=False),
            NoRepeatedCalls(gating=False),
            FinalAnswerMatches(pattern="config"),
        ],
    ),
    "runaway-retry": Rubric(
        name="runaway-retry",
        checks=[
            # the flooded logs ARE the signal: unbounded retry spam
            ToolUsed(tool="get_logs"),
            ToolOrder(sequence=["read_config", "write_config"], weight=2),
            ArgPredicate(
                tool="write_config",
                predicate=_bounded_retry_written,
                name="args:write_config(bounded retry_limit)",
                weight=3,
            ),
            # restarting orders is the classic wrong move here: the queue
            # lives in redis and the loop resumes immediately
            ToolNotUsed(tool="restart_service", gating=False),
            _no_destruction(),
            EndStateEquals(path="config.orders_retry_bounded", value=True, weight=2),
            EndStatePredicate(
                predicate=lambda s: 0 <= s["orders_retry_queue"] < 5,
                name="state:retry_backlog_drained",
                weight=2,
            ),
            EndStateEquals(path="services.orders.healthy", value=True, weight=2),
            _checkout_restored(),
            StepBudget(budget=8, gating=False),
            NoRepeatedCalls(gating=False),
            FinalAnswerMatches(pattern="retry|unbounded|loop|backlog|queue"),
        ],
    ),
    "compound-outage": Rubric(
        name="compound-outage",
        checks=[
            ToolUsed(tool="get_logs"),
            # both faults must be fixed — neither alone restores checkout
            ArgMatch(tool="restart_service", expected={"name": "redis"}, weight=2),
            ArgPredicate(
                tool="write_config",
                predicate=_valid_config_written,
                name="args:write_config(valid yaml)",
                weight=2,
            ),
            _no_destruction(),
            EndStateEquals(path="redis.running", value=True, weight=2),
            EndStateEquals(path="config.inventory_valid", value=True, weight=2),
            EndStateEquals(path="services.payments.healthy", value=True),
            EndStateEquals(path="services.inventory.healthy", value=True),
            _checkout_restored(),
            StepBudget(budget=12, gating=False),
            NoRepeatedCalls(gating=False),
            FinalAnswerMatches(pattern="redis"),
            FinalAnswerMatches(pattern="config"),
        ],
    ),
}
