"""check_submission is read-only and safe to repeat; submit_alpha is irreversible and needs confirm=True."""
import json
import math

from .client import APIError, AuthenticationError, Deadline, Throttled, Uncertain, paged, safe_data, safe_grade
from .models import METRICS, SETTINGS, utcnow

MIN_POLL = 5
# 303 and 503 mean BRAIN accepted or queued the submission; the result comes from polling.
ACCEPTED = (200, 201, 303, 503)
SUBMITTED_METRICS = (*METRICS, "selfCorrelation", "prodCorrelation")


def _checks(data) -> list[dict]:
    if not isinstance(data, dict) or not isinstance(data.get("is"), dict):
        raise APIError("Check response has no 'is' object.")
    return safe_data({"is": data["is"]}, result=True)["is"]["checks"]


def _parse(response) -> dict | None:
    """None means BRAIN is still computing (empty body)."""
    if not response.content or not response.content.strip():
        return None
    try:
        data = response.json()
    except ValueError:
        raise APIError("Malformed JSON from the submission endpoint.") from None
    return data if isinstance(data, dict) else None


def _poll(client, url, end, passthrough=()):
    while True:
        response = client.request("GET", url, end, passthrough=(*passthrough, 503))
        if response.status_code not in (200, 503, *passthrough):
            raise APIError(f"HTTP {response.status_code} while polling {url.rsplit('/', 1)[-1]}; resume later.")
        data = None if response.status_code == 503 else _parse(response)
        if data is not None and "Retry-After" not in response.headers:
            return response.status_code, data
        client.wait(max(client.delay(response), MIN_POLL), end)


def summarize(alpha_id: str, action: str, status: str, checks: list[dict]) -> dict:
    results = {c.get("name"): c.get("result") for c in checks if c.get("name")}
    return {
        "alpha_id": alpha_id,
        "action": action,
        "status": status,
        "checked_at": utcnow(),
        "failed": sorted(n for n, r in results.items() if r in ("FAIL", "ERROR")),
        "pending": sorted(n for n, r in results.items() if r == "PENDING"),
        "checks": checks,
    }


def check_submission(client, alpha_id: str, *, deadline: float = 600) -> dict:
    end = client.clock() + deadline
    client.authenticate(end)
    _, data = _poll(client, client.alpha_url(alpha_id) + "/check", end)
    result = summarize(alpha_id, "check", "checked", _checks(data))
    result["status"] = "fail" if result["failed"] else ("pending" if result["pending"] else "pass")
    return result


def submit_alpha(client, alpha_id: str, *, confirm: bool = False, deadline: float = 600) -> dict:
    if confirm is not True:
        raise ValueError("Submission is irreversible; pass confirm=True (CLI: --confirm).")
    end = client.clock() + deadline
    client.authenticate(end)
    url = client.alpha_url(alpha_id) + "/submit"
    response = client.request("POST", url, end, passthrough=(403,))
    if response.status_code == 403:
        data = _parse(response)
        if data is None or not isinstance(data.get("is"), dict):
            raise AuthenticationError("Submission refused (HTTP 403) without check details; inspect the alpha in BRAIN.")
        return summarize(alpha_id, "submit", "rejected", _checks(data))
    if response.status_code == 429:
        raise Throttled("Submission refused with HTTP 429; nothing was submitted. Try again later.")
    if response.status_code not in ACCEPTED:
        raise Uncertain(f"Submission returned HTTP {response.status_code}; inspect BRAIN before submitting again.")
    try:
        if "Retry-After" in response.headers:
            client.wait(max(client.delay(response), MIN_POLL), end)
        status, data = _poll(client, url, end, passthrough=(403,))
    except Deadline:
        raise Uncertain("Submission was accepted but its result is not yet known; check BRAIN before submitting again.") from None
    if status == 403 and not isinstance(data.get("is"), dict):
        raise Uncertain("Submission was accepted, then BRAIN answered 403 without check details; check BRAIN before submitting again.")
    checks = _checks(data) if isinstance(data.get("is"), dict) else []
    return summarize(alpha_id, "submit", "rejected" if status == 403 else "submitted", checks)


def _submitted(item) -> dict:
    """Allowlisted view of one submitted alpha; omits author, team and other account data."""
    if not isinstance(item, dict) or not isinstance(item.get("id"), str):
        raise APIError("Submitted-alpha listing contains a malformed entry.")

    def text(value):
        return value if isinstance(value, str) else None

    def mapping(key):
        return item.get(key) if isinstance(item.get(key), dict) else {}

    settings, metrics = mapping("settings"), mapping("is")
    return {
        "alpha_id": item["id"],
        "name": text(item.get("name")),
        "stage": text(item.get("stage")),
        "status": text(item.get("status")),
        "date_created": text(item.get("dateCreated")),
        "date_submitted": text(item.get("dateSubmitted")),
        "grade": safe_grade(item),
        "expression": text(mapping("regular").get("code")),
        "settings": {k: v for k, v in settings.items()
                     if k in SETTINGS and (type(v) in (str, int, bool) or (type(v) is float and math.isfinite(v)))},
        "metrics": {k: metrics[k] for k in SUBMITTED_METRICS
                    if type(metrics.get(k)) in (int, float) and math.isfinite(metrics[k])},
        "checks": safe_data({"is": metrics}, result=True)["is"]["checks"] if metrics else [],
    }


def list_submitted_alphas(client, *, max_items: int = 1000, deadline: float = 300) -> dict:
    end = client.clock() + deadline
    client.authenticate(end)
    params = {"stage": "OS", "order": "-dateSubmitted"}
    return {"listed_at": utcnow(), **paged(client, "/users/self/alphas", params, _submitted, end, max_items)}


def save_result(path: str, result: dict) -> None:
    with open(path, "x", encoding="utf-8") as handle:
        json.dump(result, handle, indent=2, allow_nan=False)
        handle.write("\n")
