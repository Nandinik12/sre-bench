"""orders — orchestrates inventory reserve + payments charge.

Also runs an async retry worker for failed charges (a "retry lane" fed by
batch jobs). The worker's retry policy comes from /config/orders.yaml:
retry_limit > 0 bounds attempts; retry_limit: 0 means retry forever.

Failure hooks:
- runaway-retry: retry_limit poisoned to 0 + doomed jobs seeded into the
  redis-backed queue -> worker retries forever, floods logs, saturates the
  queue -> /orders returns 503 backpressure. Restarting doesn't help: the
  queue lives in redis. The fix is bounding the retries in config.
"""

import json
import logging
import os
import threading
import time
import uuid

import httpx
import redis as redislib
import yaml
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

logging.basicConfig(level=logging.INFO, format="%(asctime)s orders %(levelname)s %(message)s")
log = logging.getLogger("orders")

app = FastAPI(title="orders")
PAYMENTS_URL = os.environ.get("PAYMENTS_URL", "http://payments:8000")
INVENTORY_URL = os.environ.get("INVENTORY_URL", "http://inventory:8000")
CONFIG_PATH = os.environ.get("CONFIG_PATH", "/config/orders.yaml")
RETRY_QUEUE = "orders:retry_queue"
BACKPRESSURE_THRESHOLD = 5
WORKER_INTERVAL = 0.3


def get_redis():
    return redislib.Redis.from_url(
        os.environ.get("REDIS_URL", "redis://redis:6379/0"),
        socket_connect_timeout=2,
        socket_timeout=2,
    )


def load_config():
    with open(CONFIG_PATH) as f:
        cfg = yaml.safe_load(f)
    if not isinstance(cfg, dict) or not isinstance(cfg.get("retry_limit"), int) or cfg["retry_limit"] < 0:
        raise ValueError(f"invalid orders config: {cfg!r}")
    return cfg


def queue_length() -> int:
    try:
        return int(get_redis().llen(RETRY_QUEUE))
    except Exception:
        return -1  # redis unreachable; payments health will surface that


def retry_worker():
    while True:
        time.sleep(WORKER_INTERVAL)
        try:
            try:
                cfg = load_config()
            except Exception as e:
                log.error("retry worker: %s", e)
                continue
            r = get_redis()
            raw = r.rpop(RETRY_QUEUE)
            if raw is None:
                continue
            item = json.loads(raw)
            item["attempts"] = int(item.get("attempts", 0)) + 1
            ok = False
            try:
                resp = httpx.post(
                    f"{PAYMENTS_URL}/charge",
                    json={"order_id": item["order_id"], "amount_cents": item["amount_cents"]},
                    timeout=3,
                )
                ok = resp.status_code == 200
            except Exception:
                ok = False
            if ok:
                log.info("retry succeeded for order %s", item["order_id"])
                continue
            limit = cfg["retry_limit"]
            if limit > 0 and item["attempts"] >= limit:
                log.warning(
                    "giving up on order %s after %d attempts (retry_limit=%d)",
                    item["order_id"], item["attempts"], limit,
                )
                continue
            log.error(
                "charge failed for order %s, retrying (attempt %d%s)",
                item["order_id"], item["attempts"],
                ", retry_limit=0: UNBOUNDED" if limit == 0 else f"/{limit}",
            )
            r.lpush(RETRY_QUEUE, json.dumps(item))
        except Exception as e:
            log.error("retry worker error: %s", e)


@app.on_event("startup")
def start_worker():
    threading.Thread(target=retry_worker, daemon=True).start()


class Order(BaseModel):
    sku: str
    qty: int = 1
    amount_cents: int = 999


@app.get("/health")
def health():
    problems = []
    try:
        load_config()
    except Exception as e:
        log.error("health: %s", e)
        problems.append(str(e))
    qlen = queue_length()
    if qlen >= BACKPRESSURE_THRESHOLD:
        problems.append(f"retry queue saturated ({qlen} jobs, threshold {BACKPRESSURE_THRESHOLD})")
    if problems:
        raise HTTPException(503, detail="; ".join(problems))
    return {"status": "ok", "retry_queue": qlen}


@app.get("/health/deep")
def health_deep():
    deps = {}
    with httpx.Client(timeout=3) as client:
        for name, url in (("payments", PAYMENTS_URL), ("inventory", INVENTORY_URL)):
            try:
                r = client.get(f"{url}/health")
                deps[name] = "ok" if r.status_code == 200 else f"unhealthy ({r.status_code})"
            except Exception as e:
                deps[name] = f"unreachable: {e}"
    if any(v != "ok" for v in deps.values()):
        raise HTTPException(503, detail=deps)
    return {"status": "ok", "dependencies": deps}


@app.post("/orders")
def create_order(o: Order):
    qlen = queue_length()
    if qlen >= BACKPRESSURE_THRESHOLD:
        log.error("rejecting order: retry queue saturated (%d jobs)", qlen)
        raise HTTPException(503, detail=f"backpressure: retry queue saturated ({qlen} jobs)")
    order_id = str(uuid.uuid4())[:8]
    with httpx.Client(timeout=5) as client:
        try:
            inv = client.post(f"{INVENTORY_URL}/reserve", json={"sku": o.sku, "qty": o.qty})
        except Exception as e:
            log.error("inventory unreachable: %s", e)
            raise HTTPException(502, detail="inventory unreachable")
        if inv.status_code != 200:
            log.error("reserve failed (%s): %s", inv.status_code, inv.text)
            raise HTTPException(502, detail=f"inventory error: {inv.text}")
        try:
            pay = client.post(
                f"{PAYMENTS_URL}/charge",
                json={"order_id": order_id, "amount_cents": o.amount_cents},
            )
        except Exception as e:
            log.error("payments unreachable: %s", e)
            raise HTTPException(502, detail="payments unreachable")
        if pay.status_code != 200:
            log.error("charge failed (%s): %s", pay.status_code, pay.text)
            raise HTTPException(502, detail=f"payments error: {pay.text}")
    log.info("order %s placed: %s x%s", order_id, o.sku, o.qty)
    return {"order_id": order_id, "status": "placed"}
