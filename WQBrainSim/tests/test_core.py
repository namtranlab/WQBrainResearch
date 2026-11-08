import json
import time
from pathlib import Path

import pytest

from core import BrainClient, JobStore, run, submit_alpha
from core.catalog import list_settings
from core.client import APIError, AuthenticationError, Uncertain
from core.__main__ import main
from core.credentials import read_env_file

ROOT = Path(__file__).resolve().parent.parent
SETTINGS = json.loads((ROOT / "example/settings.json").read_text())
ALPHA = {"is": {"sharpe": 1.5, "fitness": 1.1, "checks": [
    {"name": "LOW_SHARPE", "result": "PASS", "limit": 1.25, "value": 1.5},
    {"name": "SELF_CORRELATION", "result": "PENDING"}]}, "grade": "GOOD"}


class Response:
    def __init__(self, status, body=None, headers=None):
        self.status_code = status
        self.headers = headers or {}
        self.content = json.dumps(body).encode() if body is not None else b""
        self._body = body

    def json(self):
        if self._body is None:
            raise ValueError("empty")
        return self._body


class Session:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url.removeprefix(BrainClient.origin)))
        return self.responses.pop(0)

    def close(self):
        pass


class Clock:
    def __init__(self):
        self.now = 0.0
        self.sleeps = []

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


AUTH = Response(201)
CREATED = Response(201, headers={"Location": "/simulations/s1"})
DONE = Response(200, {"status": "COMPLETE", "alpha": "abc"})
RESULT = Response(200, ALPHA)


def client(responses, clock):
    return BrainClient(session=Session(responses), credentials=lambda: ("u", "p"),
                       clock=clock, sleep=clock.sleep)


def test_simulation_completes(tmp_path):
    clock = Clock()
    with client([AUTH, CREATED, DONE, RESULT], clock) as c, JobStore(tmp_path / "jobs.db") as store:
        job = run("rank(close)", SETTINGS, client=c, store=store)
    assert job.state == "complete"
    assert job.metrics["sharpe"] == 1.5
    assert job.grade == "GOOD"


def test_failed_simulation_keeps_brains_reason(tmp_path):
    clock = Clock()
    failed = Response(200, {"status": "ERROR", "message": "Attempted to use unknown variable \"clos\"" + "x" * 500})
    with client([AUTH, CREATED, failed], clock) as c, JobStore(tmp_path / "jobs.db") as store:
        job = run("rank(clos)", SETTINGS, client=c, store=store)
    assert job.state == "failed"
    assert job.error.startswith('BRAIN rejected the simulation: Attempted to use unknown variable "clos"')
    assert len(job.raw_progress["message"]) == 300


def test_settings_lists_the_allowed_choices():
    choices = {"region": {"choices": {"instrumentType": {"EQUITY": [{"value": "USA"}, {"value": "EUR"}]}}}}
    options = Response(200, {"actions": {"POST": {"settings": {"children": choices}}}})
    with client([AUTH, options], Clock()) as c:
        assert list_settings(c) == {"results": choices}
    assert c.session.calls[-1] == ("OPTIONS", "/simulations")


def test_throttled_post_is_retried(tmp_path):
    clock = Clock()
    responses = [AUTH, Response(429), CREATED, DONE, RESULT]
    with client(responses, clock) as c, JobStore(tmp_path / "jobs.db") as store:
        job = run("rank(close)", SETTINGS, client=c, store=store)
    assert job.state == "complete"
    assert [call for call in c.session.calls if call[1] == "/simulations"] == [("POST", "/simulations")] * 2


def test_throttled_until_deadline_pauses_without_uncertainty(tmp_path):
    clock = Clock()
    with client([AUTH] + [Response(429)] * 10, clock) as c, JobStore(tmp_path / "jobs.db") as store:
        job = run("rank(close)", SETTINGS, client=c, store=store, deadline=25)
    assert (job.state, job.stage) == ("paused", "queued")


def test_expired_session_reauthenticates(tmp_path):
    clock = Clock()
    responses = [AUTH, CREATED, Response(401), AUTH, DONE, RESULT]
    with client(responses, clock) as c, JobStore(tmp_path / "jobs.db") as store:
        job = run("rank(close)", SETTINGS, client=c, store=store)
    assert job.state == "complete"


def test_min_gap_waits_after_previous_attempt(tmp_path):
    clock = Clock()
    with JobStore(tmp_path / "jobs.db") as store:
        with client([AUTH, CREATED, DONE, RESULT], clock) as c:
            run("rank(close)", SETTINGS, client=c, store=store)
        clock.sleeps.clear()
        with client([AUTH, CREATED, DONE, RESULT], clock) as c:
            run("rank(open)", SETTINGS, client=c, store=store, min_gap=300)
    assert 290 < clock.sleeps[0] <= 300


def test_env_file_is_read_as_plain_text(tmp_path):
    env = tmp_path / ".env"
    env.write_text("# comment\nexport BRAIN_USERNAME='me@example.com'\nBRAIN_PASSWORD=pa$$#word\nOTHER=x\n")
    assert read_env_file(env) == {"BRAIN_USERNAME": "me@example.com", "BRAIN_PASSWORD": "pa$$#word"}


def test_dry_run_needs_no_network(capsys):
    code = main(["run", "--expression", "rank(close)", "--settings", str(ROOT / "example/settings.json"), "--dry-run"])
    assert code == 0
    assert json.loads(capsys.readouterr().out)["regular"] == "rank(close)"


def test_daily_simulation_limit_stops_without_retrying(tmp_path):
    clock = Clock()
    responses = [AUTH, Response(429, {"detail": "DAILY_SIMULATION_LIMIT_EXCEEDED"})]
    with client(responses, clock) as c, JobStore(tmp_path / "jobs.db") as store:
        job = run("rank(close)", SETTINGS, client=c, store=store)
    assert (job.state, job.stage) == ("paused", "queued")
    assert "DAILY_SIMULATION_LIMIT_EXCEEDED" in job.error
    assert clock.sleeps == []


def test_403_after_an_accepted_submission_is_uncertain():
    responses = [AUTH, Response(201), Response(403, {"message": "forbidden"})]
    with client(responses, Clock()) as c, pytest.raises(Uncertain):
        submit_alpha(c, "abc", confirm=True)


def test_busy_login_is_temporary_not_an_access_problem():
    with client([Response(503)], Clock()) as c, pytest.raises(APIError) as raised:
        c.authenticate(c.clock() + 60)
    assert not isinstance(raised.value, AuthenticationError)
    assert "uthentication" not in str(raised.value)


@pytest.mark.parametrize("accepted", [303, 503])
def test_queued_submission_is_polled_to_a_result(accepted):
    clock = Clock()
    responses = [AUTH, Response(accepted), Response(503, headers={"Retry-After": "1"}), Response(200, {"is": {"checks": []}})]
    with client(responses, clock) as c:
        result = submit_alpha(c, "abc", confirm=True)
    assert result["status"] == "submitted"
