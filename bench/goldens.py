"""Golden solutions per scenario: the scripted 'perfect run'.

Used by the rubric tests (golden must score 1.0) and by run_bench --smoke
(pipeline check without docker or API keys).
"""

from __future__ import annotations

import copy
from typing import Any, Dict, List, Tuple

GOOD_CONFIG = "max_reserve: 100\nreserve_timeout_ms: 2000\n"

HEALTHY_STATE: Dict[str, Any] = {
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
    "config": {"inventory_valid": True, "orders_retry_bounded": True},
    "orders_retry_queue": 0,
}


def healthy_state() -> Dict[str, Any]:
    return copy.deepcopy(HEALTHY_STATE)


# (tool, args) scripts + final answers
GOLDEN_SCRIPTS: Dict[str, Tuple[List[Tuple[str, Dict[str, Any]]], str]] = {
    "dead-dependency": (
        [
            ("check_health", {"service": "payments"}),
            ("get_logs", {"service": "payments"}),
            ("restart_service", {"name": "redis"}),
            ("check_health", {"service": "gateway"}),
        ],
        "Root cause: redis was stopped. Restarted redis; checkout flow recovered.",
    ),
    "bad-deploy": (
        [
            ("get_logs", {"service": "payments"}),
            ("set_deploy", {"service": "payments", "variant": "good"}),
            ("check_health", {"service": "payments"}),
        ],
        "Bad deploy of payments (KeyError in charge path): rolled back to the good version.",
    ),
    "filled-disk": (
        [
            ("get_logs", {"service": "inventory"}),
            ("disk_usage", {"service": "inventory", "path": "/data"}),
            ("list_files", {"service": "inventory", "path": "/data"}),
            ("delete_file", {"service": "inventory", "path": "/data/core.20260712.dump"}),
            ("check_health", {"service": "inventory"}),
        ],
        "Disk was full: a core dump filled /data. Deleted it; inventory recovered.",
    ),
    "poisoned-config": (
        [
            ("get_logs", {"service": "inventory"}),
            ("read_config", {"service": "inventory"}),
            ("write_config", {"service": "inventory", "content": GOOD_CONFIG}),
            ("check_health", {"service": "inventory"}),
        ],
        "Config was poisoned (max_reserve 0, 5ms timeout). Restored valid config values.",
    ),
    "runaway-retry": (
        [
            ("check_health", {"service": "orders"}),
            ("get_logs", {"service": "orders"}),
            ("read_config", {"service": "orders"}),
            ("write_config", {"service": "orders", "content": "retry_limit: 3\n"}),
            ("check_health", {"service": "gateway"}),
        ],
        "Root cause: retry_limit was 0, so the retry worker looped unbounded on doomed "
        "jobs and saturated the queue. Bounded retries to 3; backlog drained.",
    ),
    "compound-outage": (
        [
            ("check_health", {"service": "payments"}),
            ("get_logs", {"service": "payments"}),
            ("restart_service", {"name": "redis"}),
            ("check_health", {"service": "inventory"}),
            ("get_logs", {"service": "inventory"}),
            ("read_config", {"service": "inventory"}),
            ("write_config", {"service": "inventory", "content": GOOD_CONFIG}),
            ("check_health", {"service": "gateway"}),
        ],
        "Two simultaneous faults: redis was stopped (payments down) and inventory's "
        "config was poisoned. Restarted redis and restored valid config; checkout recovered.",
    ),
}
