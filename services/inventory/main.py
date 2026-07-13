"""inventory — reserves stock; reads /config/inventory.yaml, writes /data.

Failure hooks:
- poisoned-config: bad yaml or invalid values -> /health 503, /reserve 500
- filled-disk: /data full -> writes fail -> /reserve 507, /health 503
"""

import logging
import os

import yaml
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

logging.basicConfig(level=logging.INFO, format="%(asctime)s inventory %(levelname)s %(message)s")
log = logging.getLogger("inventory")

app = FastAPI(title="inventory")
CONFIG_PATH = os.environ.get("CONFIG_PATH", "/config/inventory.yaml")
DATA_DIR = os.environ.get("DATA_DIR", "/data")


def load_config():
    """Read config fresh each call so a config fix takes effect without restart."""
    try:
        with open(CONFIG_PATH) as f:
            cfg = yaml.safe_load(f)
    except Exception as e:
        raise ValueError(f"config unreadable: {e}")
    if not isinstance(cfg, dict):
        raise ValueError("config is not a mapping")
    max_reserve = cfg.get("max_reserve")
    timeout_ms = cfg.get("reserve_timeout_ms")
    if not isinstance(max_reserve, int) or max_reserve < 1:
        raise ValueError(f"invalid max_reserve: {max_reserve!r} (must be int >= 1)")
    if not isinstance(timeout_ms, int) or timeout_ms < 100:
        raise ValueError(f"invalid reserve_timeout_ms: {timeout_ms!r} (must be int >= 100)")
    return cfg


def check_disk_writable():
    probe = os.path.join(DATA_DIR, ".probe")
    try:
        with open(probe, "w") as f:
            f.write("x" * 4096)
        os.remove(probe)
    except OSError as e:
        raise OSError(f"data dir not writable: {e}")


class Reserve(BaseModel):
    sku: str
    qty: int


@app.get("/health")
def health():
    problems = []
    try:
        load_config()
    except ValueError as e:
        log.error("health: %s", e)
        problems.append(str(e))
    try:
        check_disk_writable()
    except OSError as e:
        log.error("health: %s", e)
        problems.append(str(e))
    if problems:
        raise HTTPException(503, detail="; ".join(problems))
    return {"status": "ok"}


@app.post("/reserve")
def reserve(r: Reserve):
    try:
        cfg = load_config()
    except ValueError as e:
        log.error("reserve failed: %s", e)
        raise HTTPException(500, detail=f"config error: {e}")
    if r.qty > cfg["max_reserve"]:
        raise HTTPException(422, detail=f"qty {r.qty} exceeds max_reserve {cfg['max_reserve']}")
    try:
        with open(os.path.join(DATA_DIR, "reservations.log"), "a") as f:
            f.write(f"{r.sku} {r.qty}\n")
    except OSError as e:
        log.error("cannot write reservation: %s", e)
        raise HTTPException(507, detail=f"storage full or unwritable: {e}")
    log.info("reserved %s x%s", r.sku, r.qty)
    return {"reserved": True, "sku": r.sku, "qty": r.qty}
