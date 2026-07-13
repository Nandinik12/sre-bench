"""payments — charges orders; depends on redis for idempotency storage.

Failure hooks:
- dead-dependency: redis down -> /health 503, /charge 503
- bad-deploy: BROKEN=1 -> /charge 500 with a KeyError in the logs
"""

import logging
import os

import redis as redislib
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

logging.basicConfig(level=logging.INFO, format="%(asctime)s payments %(levelname)s %(message)s")
log = logging.getLogger("payments")

app = FastAPI(title="payments")
BROKEN = os.environ.get("BROKEN", "0") == "1"
VERSION = "v2.1.7-broken" if BROKEN else "v2.1.6"


def get_redis():
    return redislib.Redis.from_url(
        os.environ.get("REDIS_URL", "redis://redis:6379/0"),
        socket_connect_timeout=2,
        socket_timeout=2,
    )


class Charge(BaseModel):
    order_id: str
    amount_cents: int
    currency: str = "USD"


@app.get("/health")
def health():
    try:
        get_redis().ping()
    except Exception as e:
        log.error("health check failed: cannot reach redis: %s", e)
        raise HTTPException(503, detail=f"redis unreachable: {e}")
    return {"status": "ok", "version": VERSION}


@app.get("/version")
def version():
    return {"version": VERSION}


@app.post("/charge")
def charge(c: Charge):
    if c.amount_cents <= 0:
        log.warning("rejected charge for order %s: invalid amount %s", c.order_id, c.amount_cents)
        raise HTTPException(400, detail=f"invalid amount_cents: {c.amount_cents}")
    if BROKEN:
        # simulate a bad deploy: new code path assumes a field that isn't set
        log.error("charge failed for order %s", c.order_id)
        log.error("Traceback (most recent call last): KeyError: 'payment_method_id'")
        raise HTTPException(500, detail="internal error")
    try:
        r = get_redis()
        if r.set(f"charge:{c.order_id}", c.amount_cents, nx=True) is None:
            log.info("duplicate charge suppressed for order %s", c.order_id)
            return {"charged": False, "duplicate": True}
    except HTTPException:
        raise
    except Exception as e:
        log.error("cannot reach redis at %s: %s", os.environ.get("REDIS_URL"), e)
        raise HTTPException(503, detail="dependency unavailable: redis")
    log.info("charged order %s %s %s", c.order_id, c.amount_cents, c.currency)
    return {"charged": True, "order_id": c.order_id}
