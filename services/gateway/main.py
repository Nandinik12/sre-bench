"""gateway — public entry point; proxies checkout to orders."""

import logging
import os

import httpx
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

logging.basicConfig(level=logging.INFO, format="%(asctime)s gateway %(levelname)s %(message)s")
log = logging.getLogger("gateway")

app = FastAPI(title="gateway")
ORDERS_URL = os.environ.get("ORDERS_URL", "http://orders:8000")


class Checkout(BaseModel):
    sku: str
    qty: int = 1
    amount_cents: int = 999


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/checkout")
def checkout(c: Checkout):
    with httpx.Client(timeout=8) as client:
        try:
            r = client.post(f"{ORDERS_URL}/orders", json=c.model_dump())
        except Exception as e:
            log.error("orders unreachable: %s", e)
            raise HTTPException(502, detail="orders unreachable")
    if r.status_code != 200:
        log.error("checkout failed (%s): %s", r.status_code, r.text)
        raise HTTPException(r.status_code, detail=r.json().get("detail", r.text))
    return r.json()
