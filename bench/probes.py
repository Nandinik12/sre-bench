"""Post-run environment probes.

probe_environment() captures the ground-truth final_state that trajeval's
end-state checks grade against. The agent is graded on the world as probed
here — never on its own claims.
"""

from __future__ import annotations

import json
import pathlib
import subprocess
import urllib.error
import urllib.request
from typing import Any, Dict

import yaml

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
INVENTORY_CONFIG = REPO_ROOT / "services" / "inventory" / "config" / "inventory.yaml"

SERVICES = {
    "gateway": 8080,
    "orders": 8081,
    "payments": 8082,
    "inventory": 8083,
}


def _http_health(port: int, host: str = "localhost", timeout: float = 3.0) -> Dict[str, Any]:
    url = f"http://{host}:{port}/health"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            body = json.loads(resp.read().decode() or "{}")
            return {"healthy": resp.status == 200, "detail": body}
    except urllib.error.HTTPError as e:
        return {"healthy": False, "detail": e.read().decode()[:200]}
    except Exception as e:
        return {"healthy": False, "detail": f"unreachable: {e}"}


def _checkout_works(host: str = "localhost", timeout: float = 8.0) -> bool:
    """End-to-end probe: can a real order actually go through?"""
    req = urllib.request.Request(
        f"http://{host}:{SERVICES['gateway']}/checkout",
        data=json.dumps({"sku": "probe-sku", "qty": 1, "amount_cents": 100}).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status == 200
    except Exception:
        return False


def _redis_running() -> Dict[str, Any]:
    try:
        out = subprocess.run(
            ["docker", "inspect", "-f", "{{.State.Running}}", "sre-redis"],
            capture_output=True, text=True, timeout=10,
        )
        return {"running": out.stdout.strip() == "true"}
    except Exception as e:
        return {"running": False, "detail": f"probe failed: {e}"}


def _payments_version(host: str = "localhost") -> str:
    try:
        with urllib.request.urlopen(f"http://{host}:8082/version", timeout=3) as resp:
            return json.loads(resp.read().decode()).get("version", "unknown")
    except Exception:
        return "unknown"


def _inventory_disk_pct() -> int:
    """Percent used of inventory's /data, via df inside the container. -1 if unknown."""
    try:
        out = subprocess.run(
            ["docker", "compose", "exec", "-T", "inventory",
             "sh", "-c", "df /data | tail -1 | awk '{print $5}'"],
            capture_output=True, text=True, timeout=15, cwd=REPO_ROOT,
        )
        return int(out.stdout.strip().rstrip("%"))
    except Exception:
        return -1


def _inventory_config_valid() -> bool:
    try:
        cfg = yaml.safe_load(INVENTORY_CONFIG.read_text())
        return (
            isinstance(cfg, dict)
            and isinstance(cfg.get("max_reserve"), int)
            and cfg["max_reserve"] >= 1
            and isinstance(cfg.get("reserve_timeout_ms"), int)
            and cfg["reserve_timeout_ms"] >= 100
        )
    except Exception:
        return False


def probe_environment(host: str = "localhost") -> Dict[str, Any]:
    services = {name: _http_health(port, host) for name, port in SERVICES.items()}
    return {
        "services": services,
        "checkout_works": _checkout_works(host),
        "redis": _redis_running(),
        "payments_version": _payments_version(host),
        "disk": {"inventory_data_pct": _inventory_disk_pct()},
        "config": {"inventory_valid": _inventory_config_valid()},
    }
