"""Zerodha Kite Connect execution/sync helpers for ApexWealth.

This module intentionally uses plain requests instead of browser-side calls so
API secrets stay on the backend. Apex holdings/trades are updated only after an
order is confirmed COMPLETE by Kite's order history endpoint.
"""
from __future__ import annotations

import hashlib
import time
from urllib.parse import urlencode
import requests

KITE_BASE = "https://api.kite.trade"
KITE_LOGIN = "https://kite.zerodha.com/connect/login"

DEFAULT_TIMEOUT = 12

class ZerodhaError(RuntimeError):
    pass


def _headers(api_key: str, access_token: str | None = None) -> dict:
    headers = {
        "X-Kite-Version": "3",
        "User-Agent": "ApexWealth-Zerodha/1.0",
    }
    if access_token:
        headers["Authorization"] = f"token {api_key}:{access_token}"
    return headers


def kite_login_url(api_key: str, state: str | None = None) -> str:
    params = {"api_key": api_key, "v": "3"}
    if state:
        params["state"] = state
    return f"{KITE_LOGIN}?{urlencode(params)}"


def generate_session(api_key: str, api_secret: str, request_token: str) -> dict:
    checksum = hashlib.sha256(f"{api_key}{request_token}{api_secret}".encode()).hexdigest()
    resp = requests.post(
        f"{KITE_BASE}/session/token",
        data={"api_key": api_key, "request_token": request_token, "checksum": checksum},
        headers=_headers(api_key),
        timeout=DEFAULT_TIMEOUT,
    )
    return _json_or_raise(resp)


def get_profile(api_key: str, access_token: str) -> dict:
    resp = requests.get(f"{KITE_BASE}/user/profile", headers=_headers(api_key, access_token), timeout=DEFAULT_TIMEOUT)
    return _json_or_raise(resp)


def get_holdings(api_key: str, access_token: str) -> list[dict]:
    resp = requests.get(f"{KITE_BASE}/portfolio/holdings", headers=_headers(api_key, access_token), timeout=DEFAULT_TIMEOUT)
    data = _json_or_raise(resp)
    return data.get("data") or []


def place_order(api_key: str, access_token: str, *, variety: str = "regular", **payload) -> dict:
    resp = requests.post(
        f"{KITE_BASE}/orders/{variety}",
        data=payload,
        headers=_headers(api_key, access_token),
        timeout=DEFAULT_TIMEOUT,
    )
    return _json_or_raise(resp)


def order_history(api_key: str, access_token: str, order_id: str) -> list[dict]:
    resp = requests.get(f"{KITE_BASE}/orders/{order_id}", headers=_headers(api_key, access_token), timeout=DEFAULT_TIMEOUT)
    data = _json_or_raise(resp)
    return data.get("data") or []


def wait_for_complete(api_key: str, access_token: str, order_id: str, *, timeout_sec: int = 12, poll_sec: float = 1.0) -> dict:
    deadline = time.time() + max(1, int(timeout_sec))
    last = None
    while time.time() <= deadline:
        hist = order_history(api_key, access_token, order_id)
        if hist:
            last = hist[-1]
            status = str(last.get("status") or "").upper()
            if status in {"COMPLETE", "REJECTED", "CANCELLED"}:
                return last
        time.sleep(max(0.25, float(poll_sec)))
    return last or {"order_id": order_id, "status": "UNKNOWN", "status_message": "Order confirmation timed out"}


def normalise_exchange_symbol(symbol: str) -> tuple[str, str]:
    raw = str(symbol or "").strip().upper()
    if raw.startswith("NSE:"):
        raw = raw.split(":", 1)[1]
        return "NSE", raw.replace(".NS", "").replace("-EQ", "")
    if raw.startswith("BSE:"):
        raw = raw.split(":", 1)[1]
        return "BSE", raw.replace(".BO", "")
    if raw.endswith(".BO"):
        return "BSE", raw[:-3]
    return "NSE", raw.replace(".NS", "").replace("-EQ", "")


def extract_executed(order: dict, fallback_qty: float = 0, fallback_price: float = 0) -> tuple[float, float]:
    qty = order.get("filled_quantity") or order.get("quantity") or fallback_qty or 0
    avg = order.get("average_price") or order.get("price") or fallback_price or 0
    try:
        qty = float(qty)
    except Exception:
        qty = 0.0
    try:
        avg = float(avg)
    except Exception:
        avg = 0.0
    return qty, avg


def _json_or_raise(resp: requests.Response) -> dict:
    try:
        data = resp.json()
    except Exception:
        raise ZerodhaError(f"Kite returned HTTP {resp.status_code}: {resp.text[:180]}")
    if resp.status_code >= 400 or data.get("status") == "error":
        msg = data.get("message") or data.get("error") or f"HTTP {resp.status_code}"
        raise ZerodhaError(str(msg))
    return data
