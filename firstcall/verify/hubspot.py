"""Read-back checks for the HubSpot tasks, plus the test-account guard.

Verifiers return (passed, checks, notes): every value in `checks` must be true
for the run to pass, and `notes` hold context that doesn't fail it. HubSpot's
search index lags writes by a few seconds, so lookups retry briefly.
"""

from __future__ import annotations

import time

import requests

API = "https://api.hubapi.com"
TEST_ACCOUNT_TYPES = {"DEVELOPER_TEST", "SANDBOX"}
SEARCH_RETRIES = 6
SEARCH_WAIT_S = 4


def _headers(env: dict) -> dict:
    return {"Authorization": f"Bearer {env['HUBSPOT_ACCESS_TOKEN']}", "Content-Type": "application/json"}


def _get(env: dict, path: str) -> dict:
    resp = requests.get(API + path, headers=_headers(env), timeout=20)
    resp.raise_for_status()
    return resp.json()


def _search(env: dict, object_type: str, prop: str, value: str, properties: list[str]) -> list[dict]:
    body = {
        "filterGroups": [{"filters": [{"propertyName": prop, "operator": "EQ", "value": value}]}],
        "properties": properties,
        "limit": 10,
    }
    for attempt in range(SEARCH_RETRIES):
        resp = requests.post(f"{API}/crm/v3/objects/{object_type}/search", headers=_headers(env), json=body, timeout=20)
        resp.raise_for_status()
        results = resp.json().get("results", [])
        if results or attempt == SEARCH_RETRIES - 1:
            return results
        time.sleep(SEARCH_WAIT_S)
    return []


def _associated_ids(env: dict, from_type: str, from_id: str, to_type: str) -> set[str]:
    data = _get(env, f"/crm/v4/objects/{from_type}/{from_id}/associations/{to_type}")
    return {str(r["toObjectId"]) for r in data.get("results", [])}


def test_account(env: dict) -> tuple[bool, str]:
    """Fail closed unless the token belongs to a developer test account or a sandbox."""
    try:
        account_type = _get(env, "/account-info/v3/details").get("accountType")
    except requests.RequestException as exc:
        return False, f"couldn't confirm the account type ({type(exc).__name__}); refusing to run"
    if account_type in TEST_ACCOUNT_TYPES:
        return True, f"account type {account_type}"
    return False, f"account type is {account_type!r}; FirstCall only runs against HubSpot developer test accounts or sandboxes"


def verify_contact(run_id: str, env: dict) -> tuple[bool, dict, dict]:
    found = _search(env, "contacts", "email", f"firstcall+{run_id}@example.com", ["firstname", "lastname", "jobtitle"])
    checks = {"contact_exists": bool(found)}
    notes = {"contacts_with_email": len(found)}
    if found:
        p = found[0]["properties"]
        checks["first_name"] = p.get("firstname") == "FirstCall"
        checks["last_name"] = p.get("lastname") == run_id
        checks["job_title"] = p.get("jobtitle") == f"Benchmark run {run_id}"
    return all(checks.values()), checks, notes


def verify_company_association(run_id: str, env: dict) -> tuple[bool, dict, dict]:
    companies = _search(env, "companies", "name", f"FirstCall {run_id}", ["name", "domain"])
    contacts = _search(env, "contacts", "email", f"firstcall+{run_id}-co@example.com", ["email"])
    checks = {"company_exists": bool(companies), "contact_exists": bool(contacts)}
    notes = {"companies_with_name": len(companies), "contacts_with_email": len(contacts)}
    if companies:
        checks["company_domain"] = companies[0]["properties"].get("domain") == f"firstcall-{run_id}.example.com"
    if companies and contacts:
        linked = _associated_ids(env, "contacts", contacts[0]["id"], "companies")
        checks["contact_associated_with_company"] = str(companies[0]["id"]) in linked
    return all(checks.values()), checks, notes


def verify_deal_pipeline(run_id: str, env: dict) -> tuple[bool, dict, dict]:
    deals = _search(env, "deals", "dealname", f"FirstCall {run_id} pilot", ["dealname", "amount", "dealstage", "pipeline"])
    contacts = _search(env, "contacts", "email", f"firstcall+{run_id}-deal@example.com", ["email"])
    checks = {"deal_exists": bool(deals), "contact_exists": bool(contacts)}
    notes = {"deals_with_name": len(deals), "contacts_with_email": len(contacts)}
    if deals:
        p = deals[0]["properties"]
        try:
            checks["amount_1200"] = float(p.get("amount") or "nan") == 1200
        except ValueError:
            checks["amount_1200"] = False
        checks["default_pipeline"] = p.get("pipeline") == "default"
        checks["closed_won"] = p.get("dealstage") == "closedwon"
    if deals and contacts:
        linked = _associated_ids(env, "deals", deals[0]["id"], "contacts")
        checks["contact_associated_with_deal"] = str(contacts[0]["id"]) in linked
    return all(checks.values()), checks, notes
