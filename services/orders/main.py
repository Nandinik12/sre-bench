"""orders — orchestrates inventory reserve + payments charge."""

import logging
import os
import uuid

import httpx
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

logging.basicConfig(level=logging.INFO, format="%(asctime)s orders %(levelname)s %(message)s")
log = logging.getLogger("orders")

app = FastAPI(title="orders")
PAYMENTS_URL = os.environ.get("PAYMENTS_URL", "http://payments:8000")
INVENTORY_URL = os.environ.get("INVENTORY_URL", "http://inventory:8000")


class Order(BaseModel):
    sku: str
    qty: int = 1
    amount_cents: int = 999


@app.get("/health")
def health():
    return {"status": "ok"}


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
