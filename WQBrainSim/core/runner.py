import math

from .client import DONE, FAILED, PENDING, APIError, AuthenticationError, Rejected, Throttled, safe_data
from .models import Job, METRICS, payload, utcnow, validate_batch, validate_description

POLL_INTERVAL = 30


def _deadline(client, seconds: float) -> float:
    if type(seconds) not in (int, float) or not math.isfinite(seconds) or seconds <= 0:
        raise ValueError("Deadline must be a positive finite number of seconds.")
    return client.clock() + seconds


def _wait_for_gap(client, store, min_gap: float, end: float) -> None:
    last = store.last_attempt()
    if last is not None:
        client.wait(max(0, last + min_gap - client.wall_clock()), end)


def _step(job: Job, client, store, end: float) -> None:
    if job.stage == "queued":
        job.state = job.stage = "submitting"
        job.attempted_at = utcnow()
        store.save(job)
        url, response = client.submit(payload(job.expression, job.settings), end)
        job.progress_url = url
        job.submitted_at = utcnow()
        job.state = job.stage = "running"
        store.save(job)
        job.next_poll_at = client.wall_clock() + max(client.delay(response), POLL_INTERVAL)
        return
    if not job.alpha_id:
        if not job.progress_url:
            raise APIError("Missing progress URL; inspect the saved job before retrying.")
        data, delay = client.get_json(job.progress_url, end)
        job.raw_progress = safe_data(data)
        status = data.get("status")
        if status is not None and not isinstance(status, str):
            raise APIError("Unrecognized simulation response; resume after verifying API behavior.")
        if status in FAILED:
            reason = job.raw_progress.get("message")
            raise Rejected(f"BRAIN rejected the simulation: {reason}" if reason else
                           "Remote simulation failed or was cancelled; inspect expression/settings in BRAIN using the saved progress URL.")
        if status in DONE:
            job.alpha_id = data.get("alpha")
            client.alpha_url(job.alpha_id)
            job.state = job.stage = "collecting"
            job.next_poll_at = client.wall_clock() + delay
            return
        if status not in PENDING and not (status is None and delay > 0):
            raise APIError("Unrecognized simulation response; resume after verifying API behavior.")
        job.state = job.stage = "running"
        job.next_poll_at = client.wall_clock() + max(delay, POLL_INTERVAL)
        return
    data, _ = client.get_json(client.alpha_url(job.alpha_id), end)
    if not isinstance(data.get("is"), dict):
        raise APIError("Alpha response is missing its 'is' metrics object; resume collection later.")
    job.raw_result = safe_data(data, result=True)
    metrics = job.raw_result["is"]
    job.metrics = {key: metrics.get(key) for key in METRICS}
    job.checks = metrics["checks"]
    job.grade = job.raw_result["grade"]
    job.state = job.stage = "complete"
    job.completed_at = utcnow()


def _attempt(job: Job, client, store, end: float, action) -> Job:
    if job.origin != client.origin or job.account != client.account:
        raise ValueError("Job origin/account differs from this client; use the original account label.")
    if job.state in ("complete", "failed", "uncertain"):
        return job
    if job.stage == "submitting":
        job.state = "uncertain"
        job.error = "Interrupted submission may have been accepted; inspect BRAIN before rerunning."
        return store.save(job)
    try:
        client.authenticate(end)
        job.error = None
        action()
    except Rejected as error:
        job.state = "failed"
        job.error = str(error)
        job.completed_at = utcnow()
    except (AuthenticationError, Throttled) as error:
        # Both mean BRAIN did not accept a simulation, so the job can be submitted again.
        if job.stage == "submitting":
            job.stage = "queued"
        job.state, job.error = "paused", str(error)
    except APIError as error:
        job.state = "uncertain" if job.stage == "submitting" else "paused"
        job.error = str(error)
        job.next_poll_at = max(job.next_poll_at, client.retry_not_before)
    except KeyboardInterrupt:
        job.state = "uncertain" if job.stage == "submitting" else "paused"
        job.error = "Interrupted locally; remote work may continue."
        job.next_poll_at = max(job.next_poll_at, client.retry_not_before)
        store.save(job)
        raise
    return store.save(job)


def advance(job: Job, client, store, end: float) -> Job:
    return _attempt(job, client, store, end, lambda: _step(job, client, store, end))


def _drive(job: Job, client, store, end: float, min_gap: float) -> Job:
    def wait_then_step():
        if job.stage == "queued":
            _wait_for_gap(client, store, min_gap, end)
        else:
            client.wait(max(0, job.next_poll_at - client.wall_clock()), end)
        _step(job, client, store, end)

    while True:
        job = _attempt(job, client, store, end, wait_then_step)
        if job.state in ("complete", "failed", "uncertain", "paused"):
            return job


def run(expression: str, settings: dict, *, client, store, deadline: float = 600,
        rerun: bool = False, description: dict | None = None, min_gap: float = 0) -> Job:
    payload(expression, settings)
    if description is not None:
        validate_description(description)
    end = _deadline(client, deadline)
    with store.lock():
        job = store.prepare(expression, settings, client.origin, client.account, rerun)
        if description is not None:
            job.description = dict(description)
            store.save(job)
        return _drive(job, client, store, end, min_gap)


def resume(job_id: str, *, client, store, deadline: float = 600, min_gap: float = 0) -> Job:
    end = _deadline(client, deadline)
    with store.lock():
        return _drive(store.get(job_id), client, store, end, min_gap)


def run_batch(expressions: list[str], settings: dict, *, client, store, limit: int = 20,
              deadline: float = 600, rerun: bool = False, min_gap: float = 0) -> list[Job]:
    validate_batch(expressions, settings, limit)
    _deadline(client, deadline)
    results = []
    with store.lock():
        for expression in expressions:
            job = store.prepare(expression, settings, client.origin, client.account, rerun)
            results.append(_drive(job, client, store, _deadline(client, deadline), min_gap))
            # Do not add remote work while the previous outcome remains unresolved.
            if results[-1].state in ("paused", "uncertain"):
                break
    return results
