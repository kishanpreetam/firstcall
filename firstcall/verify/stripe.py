"""Read-back checks for the Stripe tasks.

Each verifier returns (passed, checks, notes). `checks` must all be true for
the run to pass; `notes` record things worth knowing that don't fail the run
(for example, duplicate objects left behind by retries).
"""

from __future__ import annotations

import requests

API = "https://api.stripe.com/v1"


def _get(key: str, path: str, params: dict | None = None) -> dict:
    resp = requests.get(API + path, auth=(key, ""), params=params, timeout=20)
    resp.raise_for_status()
    return resp.json()


def _customers(key: str, email: str) -> list[dict]:
    return _get(key, "/customers", {"email": email, "limit": 10})["data"]


def verify_customer(run_id: str, env: dict) -> tuple[bool, dict, dict]:
    key = env["STRIPE_SECRET_KEY"]
    found = _customers(key, f"firstcall+{run_id}@example.com")
    checks = {"customer_exists": bool(found)}
    notes = {"customers_with_email": len(found)}
    if found:
        c = found[0]
        checks["name"] = c.get("name") == f"FirstCall {run_id}"
        checks["metadata"] = (c.get("metadata") or {}).get("firstcall_run") == run_id
    return all(checks.values()), checks, notes


def verify_subscription(run_id: str, env: dict) -> tuple[bool, dict, dict]:
    key = env["STRIPE_SECRET_KEY"]
    found = _customers(key, f"firstcall+{run_id}-sub@example.com")
    checks = {"customer_exists": bool(found)}
    notes = {"customers_with_email": len(found)}
    if not found:
        return False, checks, notes
    customer = found[0]
    checks["customer_metadata"] = (customer.get("metadata") or {}).get("firstcall_run") == run_id

    subs = _get(key, "/subscriptions", {"customer": customer["id"], "status": "all", "limit": 10})["data"]
    checks["subscription_exists"] = bool(subs)
    notes["subscriptions"] = len(subs)
    if not subs:
        return False, checks, notes
    sub = next((s for s in subs if s["status"] == "active"), subs[0])
    checks["active"] = sub["status"] == "active"

    price = sub["items"]["data"][0]["price"]
    checks["price_1200_usd"] = price.get("unit_amount") == 1200 and price.get("currency") == "usd"
    checks["monthly"] = (price.get("recurring") or {}).get("interval") == "month"
    product = _get(key, f"/products/{price['product']}")
    checks["product_name"] = product.get("name") == f"FirstCall Pro {run_id}"

    invoice_id = sub.get("latest_invoice")
    invoice = _get(key, f"/invoices/{invoice_id}") if invoice_id else {}
    checks["first_invoice_paid"] = invoice.get("status") == "paid"
    return all(checks.values()), checks, notes


def verify_partial_refund(run_id: str, env: dict) -> tuple[bool, dict, dict]:
    key = env["STRIPE_SECRET_KEY"]
    found = _customers(key, f"firstcall+{run_id}-pay@example.com")
    checks = {"customer_exists": bool(found)}
    notes = {"customers_with_email": len(found)}
    if not found:
        return False, checks, notes
    customer = found[0]
    checks["customer_metadata"] = (customer.get("metadata") or {}).get("firstcall_run") == run_id

    intents = _get(key, "/payment_intents", {"customer": customer["id"], "limit": 20})["data"]
    tagged = [pi for pi in intents if (pi.get("metadata") or {}).get("firstcall_run") == run_id]
    notes["payment_intents"] = len(intents)
    checks["tagged_payment_intent"] = bool(tagged)
    if not tagged:
        return False, checks, notes
    pi = next((p for p in tagged if p["status"] == "succeeded"), tagged[0])
    checks["amount_2500_usd"] = pi.get("amount") == 2500 and pi.get("currency") == "usd"
    checks["succeeded"] = pi.get("status") == "succeeded"

    charge_id = pi.get("latest_charge")
    charge = _get(key, f"/charges/{charge_id}") if charge_id else {}
    checks["refunded_1000"] = charge.get("amount_refunded") == 1000
    checks["not_fully_refunded"] = charge.get("refunded") is False
    return all(checks.values()), checks, notes
