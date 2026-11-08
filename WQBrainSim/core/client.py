from email.utils import parsedate_to_datetime
import math
import re
import time
from urllib.parse import urljoin, urlsplit

import requests

from .models import METRICS

FAILED = {"FAILED", "ERROR", "CANCELLED", "CANCELED"}
DONE = {"COMPLETE", "WARNING"}
PENDING = {"PENDING", "CREATED", "SUBMITTED", "QUEUED", "RUNNING", "IN_PROGRESS", "PROCESSING"}
RETRYABLE = (429, 500, 502, 503, 504)
THROTTLE_WAIT = 10
DAILY_LIMIT = "DAILY_SIMULATION_LIMIT_EXCEEDED"
# 429 details that will not clear by retrying within one run.
STOP_DETAILS = (DAILY_LIMIT, "THROTTLED")
PAGE_SIZE = 50
MESSAGE_LIMIT = 300
EXTRA_METRICS = ("pnl", "volatility", "maxDrawdown", "winRate", "avgReturn")
CHECK_KEYS = ("name", "result", "status", "type", "severity", "limit", "value")


class APIError(Exception):
    pass


class AuthenticationError(APIError):
    pass


class Rejected(APIError):
    pass


class Uncertain(APIError):
    pass


class Deadline(APIError):
    pass


class Throttled(APIError):
    pass


def _finite(value) -> bool:
    return type(value) in (int, float) and math.isfinite(value)


def _detail(response) -> str | None:
    try:
        body = response.json()
    except ValueError:
        return None
    detail = body.get("detail") if isinstance(body, dict) else None
    return detail if isinstance(detail, str) else None


def safe_grade(data: dict) -> str | None:
    grade = data.get("grade") if isinstance(data, dict) else None
    return grade if isinstance(grade, str) and re.fullmatch(r"[A-Z][A-Z_]{0,31}", grade) else None


def _safe_check(check) -> dict:
    if not isinstance(check, dict):
        raise APIError("Alpha response contains a malformed check.")
    return {key: value for key in CHECK_KEYS if (value := check.get(key)) is not None
            and (_finite(value) or (isinstance(value, str) and re.fullmatch(r"[A-Z][A-Z0-9_]{0,79}", value)))}


def safe_data(data: dict, *, result: bool = False) -> dict:
    """Archive only expected fields and BRAIN's short failure message; never other response text or headers."""
    if not result:
        out = {}
        if isinstance(data.get("status"), str) and data["status"] in FAILED | DONE | PENDING:
            out["status"] = data["status"]
        if out.get("status") in FAILED and isinstance(data.get("message"), str):
            out["message"] = data["message"][:MESSAGE_LIMIT]
        if _finite(data.get("progress")):
            out["progress"] = data["progress"]
        return out
    metrics = data.get("is", {})
    if not isinstance(metrics, dict):
        raise APIError("Alpha response has an invalid 'is' object.")
    checks = metrics.get("checks", [])
    if not isinstance(checks, list):
        raise APIError("Alpha response has invalid checks.")
    clean = {k: v for k, v in metrics.items() if k in (*METRICS, *EXTRA_METRICS) and (v is None or _finite(v))}
    clean["checks"] = [_safe_check(check) for check in checks]
    return {"is": clean, "grade": safe_grade(data)}


class BrainClient:
    origin = "https://api.worldquantbrain.com"

    def __init__(self, *, session=None, credentials=None, account: str = "default",
                 clock=time.monotonic, wall_clock=time.time, sleep=time.sleep):
        self.session = session if session is not None else requests.Session()
        self.credentials = credentials
        self.account = account
        self.clock, self.wall_clock, self.sleep = clock, wall_clock, sleep
        self.authenticated = False
        self.retry_not_before = 0.0

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.session.close()

    def trusted_url(self, url: str) -> str:
        if not isinstance(url, str) or not url or any(c.isspace() for c in url) or "\\" in url:
            raise APIError("Invalid progress URL.")
        resolved = urljoin(self.origin + "/", url)
        try:
            parts = urlsplit(resolved)
            valid = (parts.scheme == "https" and parts.hostname == "api.worldquantbrain.com"
                     and parts.port in (None, 443) and parts.username is None
                     and parts.password is None and not parts.query and not parts.fragment)
        except ValueError:
            valid = False
        if not valid:
            raise APIError("Refusing an untrusted or credential-bearing API URL.")
        return resolved

    def delay(self, response) -> float:
        value = response.headers.get("Retry-After")
        if value is None:
            return 0
        try:
            seconds = float(value)
        except (TypeError, ValueError):
            try:
                seconds = parsedate_to_datetime(value).timestamp() - self.wall_clock()
            except (TypeError, ValueError, OverflowError):
                raise APIError("Invalid Retry-After header; retry later.") from None
        if not math.isfinite(seconds):
            raise APIError("Invalid Retry-After header; retry later.")
        return max(0, seconds)

    def wait(self, delay: float, end: float) -> None:
        if self.clock() + delay >= end:
            raise Deadline("Local deadline reached; resume this job later.")
        if delay > 0:
            self.sleep(delay)

    def request(self, method: str, url: str, end: float, *, passthrough=(), **kwargs):
        """passthrough: status codes returned to the caller instead of handled here."""
        url = self.trusted_url(url)
        retries = 3 if method == "GET" else 0
        attempt = 0
        reauthenticated = False
        while True:
            remaining = end - self.clock()
            if remaining <= 0:
                raise Deadline("Local deadline reached; resume this job later.")
            try:
                response = self.session.request(method, url, timeout=(min(10, remaining / 2),
                                                min(30, remaining / 2)), allow_redirects=False, **kwargs)
            except requests.RequestException:
                if method != "GET":
                    raise Uncertain("Request outcome unknown; do not automatically resubmit.") from None
                if attempt == retries:
                    raise APIError("Network retries exhausted; resume later.") from None
                self.wait(2 ** attempt, end)
                attempt += 1
                continue
            if response.status_code in passthrough:
                return response
            if response.status_code in (401, 403):
                self.authenticated = False
                if response.headers.get("WWW-Authenticate", "").lower() == "persona":
                    raise AuthenticationError("Interactive BRAIN authentication is required; this flow is not yet verified or supported.")
                # A 401 means the request was not processed, so retrying after a fresh login is safe.
                if response.status_code == 401 and not reauthenticated and not url.endswith("/authentication"):
                    reauthenticated = True
                    self.authenticate(end)
                    continue
                raise AuthenticationError("BRAIN authentication failed or access expired; check access and resume.")
            if method == "GET" and response.status_code in RETRYABLE:
                delay = max(self.delay(response), 2 ** attempt)
                self.retry_not_before = self.wall_clock() + delay
                if attempt == retries:
                    raise APIError(f"HTTP {response.status_code}: retries exhausted; resume later.")
                self.wait(delay, end)
                attempt += 1
                continue
            return response

    def authenticate(self, end: float) -> None:
        if self.authenticated:
            return
        if self.credentials is None:
            raise AuthenticationError("Supply credentials or a credential callback.")
        response = self.request("POST", "/authentication", end, auth=self.credentials())
        if response.status_code in RETRYABLE:
            raise APIError(f"BRAIN login is unavailable (HTTP {response.status_code}); resume later.")
        if response.status_code != 201:
            raise AuthenticationError(f"Authentication returned HTTP {response.status_code}; check account access.")
        self.authenticated = True

    def submit(self, payload: dict, end: float) -> tuple[str, requests.Response]:
        while True:
            response = self.request("POST", "/simulations", end, json=payload)
            if response.status_code != 429:
                break
            detail = _detail(response)
            if detail in STOP_DETAILS:
                raise Throttled(f"BRAIN refused the simulation ({detail}); nothing was submitted. Resume later.")
            # 429 means BRAIN refused the simulation (concurrency limit), so it is safe to retry.
            try:
                self.wait(max(self.delay(response), THROTTLE_WAIT), end)
            except APIError:
                raise Throttled("BRAIN is at its simulation limit; nothing was submitted. Resume later.") from None
        if response.status_code in (400, 422):
            raise Rejected(f"Simulation rejected (HTTP {response.status_code}); check expression and settings in BRAIN.")
        if response.status_code != 201:
            raise Uncertain(f"Submission returned HTTP {response.status_code}; acceptance is unverified. Inspect BRAIN before rerunning.")
        try:
            url = self.trusted_url(response.headers.get("Location"))
        except APIError:
            raise Uncertain("Submission accepted without a usable trusted progress URL; inspect BRAIN before rerunning.") from None
        # Return the URL before interpreting Retry-After so it can be persisted.
        return url, response

    def cancel(self, url: str, end: float) -> None:
        response = self.request("DELETE", url, end, passthrough=(404,))
        if response.status_code not in (200, 202, 204, 404):
            raise APIError(f"Cancel returned HTTP {response.status_code}; inspect the simulation in BRAIN.")

    def fetch(self, url: str, end: float, params: dict | None = None) -> tuple[object, float]:
        response = self.request("GET", url, end, params=params)
        if response.status_code != 200:
            raise APIError(f"HTTP {response.status_code} from {urlsplit(url).path}; inspect access and resume.")
        try:
            data = response.json()
        except ValueError:
            raise APIError("Malformed JSON response; resume after inspecting the API behavior.") from None
        return data, self.delay(response)

    def get_json(self, url: str, end: float, params: dict | None = None) -> tuple[dict, float]:
        data, delay = self.fetch(url, end, params)
        if not isinstance(data, dict):
            raise APIError("Expected a JSON object from BRAIN.")
        return data, delay

    def alpha_url(self, alpha_id: str) -> str:
        if not isinstance(alpha_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", alpha_id):
            raise APIError("Invalid alpha identifier in response.")
        return self.origin + "/alphas/" + alpha_id


def paged(client: BrainClient, path: str, params: dict, parse, end: float, max_items: int,
          pause: float = 1.0) -> dict:
    if type(max_items) is not int or max_items < 1:
        raise ValueError("max_items must be a positive integer.")
    items, offset, total = [], 0, None
    while len(items) < max_items:
        data, _ = client.get_json(path, end, {**params, "limit": PAGE_SIZE, "offset": offset})
        rows = data.get("results")
        if not isinstance(rows, list):
            raise APIError(f"Unexpected response shape from {path}.")
        if type(data.get("count")) is int:
            total = data["count"]
        items.extend(parse(row) for row in rows[: max_items - len(items)])
        offset += len(rows)
        if not rows or (total is not None and offset >= total):
            break
        client.wait(pause, end)
    return {"count": total, "fetched": len(items), "truncated": total is not None and len(items) < total,
            "results": items}
