"""Failure mode definitions.

Each failure mode is a declarative plan of steps. Steps are tuples:

    ("run", [argv...])            -- subprocess from repo root
    ("write_file", relpath, txt)  -- overwrite a file relative to repo root

Keeping plans as data (rather than imperative code) makes them unit-testable
without docker and makes the injector trivially auditable.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Tuple

Step = Tuple  # ("run", argv) | ("write_file", relpath, content)

GOOD_INVENTORY_CONFIG = "max_reserve: 100\nreserve_timeout_ms: 2000\n"

# poisoned: valid YAML, invalid values — forces the agent to read the config
# and understand the constraints, not just re-run a formatter.
POISONED_INVENTORY_CONFIG = "max_reserve: 0\nreserve_timeout_ms: 5\n"

INVENTORY_CONFIG_PATH = "services/inventory/config/inventory.yaml"


@dataclass
class FailureMode:
    name: str
    description: str
    blast_radius: str
    break_steps: List[Step] = field(default_factory=list)
    restore_steps: List[Step] = field(default_factory=list)


FAILURE_MODES: Dict[str, FailureMode] = {
    "dead-dependency": FailureMode(
        name="dead-dependency",
        description="redis is stopped; payments loses its backing store",
        blast_radius="payments 503 -> orders/gateway checkout 502",
        break_steps=[("run", ["docker", "compose", "stop", "redis"])],
        restore_steps=[("run", ["docker", "compose", "start", "redis"])],
    ),
    "bad-deploy": FailureMode(
        name="bad-deploy",
        description="payments redeployed with a broken build (KeyError on charge)",
        blast_radius="payments /charge 500 -> checkout 502; /health still ok",
        break_steps=[
            ("write_file", ".env", "PAYMENTS_BROKEN=1\n"),
            ("run", ["docker", "compose", "up", "-d", "payments"]),
        ],
        restore_steps=[
            ("write_file", ".env", "PAYMENTS_BROKEN=0\n"),
            ("run", ["docker", "compose", "up", "-d", "payments"]),
        ],
    ),
    "filled-disk": FailureMode(
        name="filled-disk",
        description="inventory's /data volume is filled with junk",
        blast_radius="inventory /reserve 507, /health 503 -> checkout 502",
        break_steps=[
            (
                "run",
                [
                    "docker", "compose", "exec", "-T", "inventory",
                    "sh", "-c",
                    "dd if=/dev/zero of=/data/core.20260712.dump bs=1M count=31 2>/dev/null || true",
                ],
            )
        ],
        restore_steps=[
            (
                "run",
                [
                    "docker", "compose", "exec", "-T", "inventory",
                    "sh", "-c", "rm -f /data/core.20260712.dump",
                ],
            )
        ],
    ),
    "poisoned-config": FailureMode(
        name="poisoned-config",
        description="inventory config has invalid values (max_reserve: 0, timeout 5ms)",
        blast_radius="inventory /reserve 500, /health 503 -> checkout 502",
        break_steps=[("write_file", INVENTORY_CONFIG_PATH, POISONED_INVENTORY_CONFIG)],
        restore_steps=[("write_file", INVENTORY_CONFIG_PATH, GOOD_INVENTORY_CONFIG)],
    ),
}
