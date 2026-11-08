import argparse
from collections import Counter
from statistics import median
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import random
import re
import sqlite3
import sys
import tempfile
import time
from zoneinfo import ZoneInfo

from core import BrainClient, JobStore, check_submission, credentials, list_submitted_alphas, submit_alpha
from core.__main__ import summary
from core.client import DAILY_LIMIT, APIError, Deadline, Throttled
from core.models import payload, utcnow, validate_description, validate_settings
from core.runner import advance

RESULTS = Path(__file__).resolve().parent / "results"
GAP = (240, 420)
MAX_RUNNING = 1
MAX_PER_HOUR = 15
BATCH_SIZE = 15
STEP_DEADLINE = 60
BUDGET_MARGIN = 5
MIN_LEARNED_LIMIT = 20
UNCERTAIN_WAIT = 1800
POOL_TRIGGER = 10
SUBMIT_HOUR = 23
DAILY_CAP = 3
EASTERN = ZoneInfo("America/New_York")
OK_RESULTS = ("PASS", "WARNING")
GRADES = {"SPECTACULAR": 4, "EXCELLENT": 3, "GOOD": 2, "AVERAGE": 1, "INFERIOR": 0}
MIN_GRADE = "GOOD"
SUBMIT_NOW_GRADE = "EXCELLENT"
MAX_POOL_DAYS = 2
GRADE_LINES = (1.5, 2.0, 2.5)
NEAR_GRADE = 0.2
DIRS = ("candidates", "runs", "successful", "catalog", "submission_checks", "submitted",
        "submission_pool/removed")
CANDIDATE_KEYS = {"expression", "description", "parent", "change"}
BATCH_KEYS = {"id", "reason"}
FINAL = {"submitted", "already_submitted", "uncertain", "pooled", "below_grade"}
CHECK_DEADLINE = 1800
RETRY_PIPELINE = {"new", "check_pending", "check_error"}
MAX_CHECK_ATTEMPTS = 5
CANDIDATE_ID = r"[A-Za-z0-9_.-]+"
MAX_PER_FAMILY = 3
MAX_DEFERRALS = 2
FAMILY_PATIENCE = 5
MIN_GAIN = 0.05
REVERSED_SHARPE = -0.5
NEAR_FITNESS = 0.8
NEAR_SHARPE = 1.0
BLOCK_EDGE = 0.6
STUCK_AFTER = 1800
STUCK_RETRY_AFTER = 6 * 3600
MAX_STUCK_ATTEMPTS = 2
CANCELLED = "Cancelled on BRAIN"
RESUMABLE = ("paused", "running", "collecting")
TERMINAL = ("complete", "failed", "uncertain")
MAX_LESSONS = 20
DISPROVED_AT = 2
EXPECTS = ("better", "not_better")
VERDICTS = ("supports", "contradicts")
NOT_FIELDS = {"true", "false", "nan", "inf", "market", "sector", "industry", "subindustry", "exchange", "country"}


def path(*parts: str) -> Path:
    return RESULTS.joinpath(*parts)


def now_et() -> datetime:
    return datetime.now(EASTERN)


def stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def make_client(state: dict) -> BrainClient:
    return BrainClient(credentials=credentials, account=state["account"])


def read(file: Path, default=None):
    return json.loads(file.read_text(encoding="utf-8")) if file.exists() else default


def write(file: Path, data) -> None:
    file.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=file.parent, suffix=".tmp", delete=False) as handle:
        json.dump(data, handle, indent=2, allow_nan=False)
        handle.write("\n")
    os.replace(handle.name, file)


def log_event(event: str, **fields) -> None:
    with open(path("experiments.jsonl"), "a", encoding="utf-8") as handle:
        handle.write(json.dumps({"at": utcnow(), "event": event, **fields}, allow_nan=False) + "\n")


def announce(text: str) -> None:
    print(text)
    with open(path("research.log"), "a", encoding="utf-8") as handle:
        handle.write(f"{utcnow()}  {text}\n")


@contextmanager
def locked():
    """Read-modify-write of the state and index files; saved only if the block succeeds."""
    with open(path(".state.lock"), "a") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        state, successful = read(path("experiment_state.json")), read(path("successful_alphas.json"))
        yield state, successful
        write(path("experiment_state.json"), state)
        write(path("successful_alphas.json"), successful)


def simulation_passed(job) -> bool:
    return job.state == "complete" and bool(job.checks) and all(
        check.get("result") in OK_RESULTS
        or (check.get("name") == "SELF_CORRELATION" and check.get("result") == "PENDING")
        for check in job.checks)


def check_status(checks: list[dict]) -> str:
    failed = {check.get("name") for check in checks if check.get("result") == "FAIL"}
    if "ALREADY_SUBMITTED" in failed:
        return "already_submitted"
    if failed == {"SELF_CORRELATION"}:
        return "correlated"
    if failed:
        return "check_failed"
    if any(check.get("result") == "ERROR" for check in checks):
        return "check_error"
    if not checks or any(check.get("result") not in OK_RESULTS for check in checks):
        return "check_pending"
    return "passed"


def compact(expression: str) -> str:
    return "".join(expression.split())


def duplicate_of(alpha_id: str, expression: str, submitted: list[dict]) -> str | None:
    for item in submitted:
        if item["alpha_id"] == alpha_id or (item["expression"] and compact(item["expression"]) == compact(expression)):
            return item["alpha_id"]
    return None


def overlaps(expression: str, submitted: list[dict]) -> list[str]:
    return [item["alpha_id"] for item in submitted
            if item["expression"] and compact(item["expression"]) in compact(expression)]


def number(value) -> float:
    return value if type(value) in (int, float) else float("-inf")


def rank_key(entry: dict) -> tuple:
    metrics = entry.get("metrics") or {}
    turnover = metrics.get("turnover")
    return (GRADES.get(entry.get("grade"), -1), number(metrics.get("fitness")), number(metrics.get("sharpe")),
            -turnover if type(turnover) in (int, float) else float("-inf"))


def pool_files() -> list[Path]:
    return sorted(path("submission_pool").glob("*.json"))


def submit_due(state: dict, pool_size: int, now: datetime) -> bool:
    today = now.date().isoformat()
    if state["hold_until"] and state["hold_until"] >= today:
        return False
    return pool_size > 0 and (state["pooled_counts"].get(today, 0) >= POOL_TRIGGER or now.hour >= SUBMIT_HOUR)


def research_blocked(state: dict, pool_size: int, now: datetime) -> str | None:
    today = now.date().isoformat()
    if submit_due(state, pool_size, now):
        return "DAILY_SUBMIT_DUE: run `research.py daily_submit` before starting new simulations."
    if state.get("sim_limit_until") and state["sim_limit_until"] >= today:
        return f"DAILY_SIMULATION_LIMIT: BRAIN refused simulations for {state['sim_limit_until']} (US Eastern); wait for the next day."
    return None


def unresolved_jobs(store: JobStore, state: dict) -> list:
    return [job for job in store.all()
            if job.state in RESUMABLE or (job.state == "uncertain" and job.id not in state["resolved"])]


def load_candidate(candidate_id: str) -> tuple[dict, dict]:
    if not re.fullmatch(CANDIDATE_ID, candidate_id):
        raise ValueError("Candidate id may contain only letters, digits, '_', '.' and '-'.")
    data = read(path("candidates", f"{candidate_id}.json"))
    if not isinstance(data, dict):
        raise ValueError(f"results/candidates/{candidate_id}.json is missing or not an object.")
    missing, unknown = CANDIDATE_KEYS - data.keys(), data.keys() - CANDIDATE_KEYS - {"settings", "tests"}
    if missing or unknown:
        raise ValueError(f"Candidate needs {sorted(CANDIDATE_KEYS)} and optional settings overrides and tests; "
                         f"missing {sorted(missing)}, unknown {sorted(unknown)}.")
    if not isinstance(data["change"], str) or not data["change"].strip():
        raise ValueError("change must state what changed from the parent and why.")
    if data["parent"] is not None and not isinstance(data["parent"], str):
        raise ValueError("parent must be a candidate id or null.")
    if not isinstance(data.get("settings", {}), dict):
        raise ValueError("settings must be an object of overrides.")
    validate_description(data["description"])
    if "tests" in data:
        validate_test(candidate_id, data)
    settings = read(path("settings.json")) | data.get("settings", {})
    payload(data["expression"], settings)
    return data, settings


def family(candidate_id: str) -> str:
    seen, current = set(), candidate_id
    while current not in seen:
        seen.add(current)
        data = read(path("candidates", f"{current}.json"))
        parent = data.get("parent") if isinstance(data, dict) else None
        if not isinstance(parent, str) or not re.fullmatch(CANDIDATE_ID, parent):
            return current
        current = parent
    return current


def data_fields(expression: str) -> frozenset[str]:
    names = re.findall(r"(?<![\w.])([A-Za-z_]\w*)\s*(\(|=(?!=))?", expression)
    defined = {name for name, follower in names if follower == "="}
    return frozenset(name for name, follower in names
                     if not follower and name not in defined and name.lower() not in NOT_FIELDS)


def simulated_runs(state: dict) -> list[tuple[str, dict]]:
    runs = [(candidate_id, read(path("runs", f"{job_id}.json"))) for job_id, candidate_id in state["jobs"].items()]
    return [(candidate_id, run) for candidate_id, run in runs if run]


def assign_families(items: list[tuple[str, str, str]]) -> dict[str, str]:
    roots, by_fields = {}, {}
    for candidate_id, expression, universe in items:
        root = family(candidate_id)
        root = roots.get(root, root)
        fields = (data_fields(expression), universe)
        if root == candidate_id and fields in by_fields:
            root = by_fields[fields]
        if fields[0]:
            by_fields.setdefault(fields, root)
        roots[candidate_id] = root
    return roots


def edge(metrics: dict) -> float:
    values = [abs(metrics[key]) / near for key, near in (("fitness", NEAR_FITNESS), ("sharpe", NEAR_SHARPE))
              if type(metrics.get(key)) in (int, float)]
    return max(values, default=0.0)


def submitted_alphas() -> set[str]:
    return {alpha_id for alpha_id, entry in read(path("successful_alphas.json")).items()
            if entry["status"] in ("submitted", "already_submitted")}


def family_stats(runs: list[tuple[str, dict]], roots: dict[str, str], submitted: set[str]) -> dict[str, dict]:
    stats = {}
    for candidate_id, run in runs:
        entry = stats.setdefault(roots[candidate_id], {"runs": 0, "since_best": 0, "fitness": float("-inf"),
                                                       "best": None, "edge": 0.0, "edge_run": None,
                                                       "submitted": False})
        entry["runs"] += 1
        entry["submitted"] = entry["submitted"] or run["alpha_id"] in submitted
        if run["state"] == "complete" and edge(run["metrics"]) > entry["edge"]:
            entry["edge"], entry["edge_run"] = edge(run["metrics"]), (candidate_id, run)
        fitness = number(run["metrics"].get("fitness"))
        if run["state"] != "complete":
            entry["since_best"] += 1
            continue
        entry["since_best"] = 0 if fitness >= entry["fitness"] + MIN_GAIN else entry["since_best"] + 1
        if fitness > entry["fitness"]:
            entry["best"], entry["fitness"] = (candidate_id, run), fitness
    return stats


def current_families(state: dict) -> tuple[dict[str, str], dict[str, dict]]:
    runs = simulated_runs(state)
    roots = assign_families([(candidate_id, run["expression"], run["settings"]["universe"]) for candidate_id, run in runs])
    return roots, family_stats(runs, roots, submitted_alphas())


def grade_gap(fitness) -> float:
    value = number(fitness)
    return next((round(line - value, 4) for line in GRADE_LINES if line > value), float("inf"))


def exhausted(entry: dict | None) -> bool:
    return bool(entry) and entry["since_best"] >= FAMILY_PATIENCE


def promising(entry: dict | None) -> bool:
    return bool(entry) and entry["edge"] >= 1 and not entry["submitted"]


def diversify(candidates: list[str], roots: dict[str, str], stats: dict,
              new_data: bool) -> tuple[list[str], list[tuple[str, str, bool]]]:
    variants_allowed = sum(1 for candidate_id in candidates if roots[candidate_id] == candidate_id)
    chosen, skipped, per_family = [], [], {}
    for candidate_id in candidates:
        root = roots[candidate_id]
        if root == candidate_id:
            chosen.append(candidate_id)
        elif exhausted(stats.get(root)):
            skipped.append((candidate_id, f"family {root} is exhausted: {stats[root]['since_best']} simulations "
                                          f"without raising its best fitness by {MIN_GAIN}; explore a different idea",
                            False))
        elif not new_data:
            skipped.append((candidate_id, "no candidate in this batch uses a data field never simulated before; "
                                          "add at least one", True))
        elif per_family.get(root, 0) >= MAX_PER_FAMILY:
            skipped.append((candidate_id, f"at most {MAX_PER_FAMILY} variants of family {root} per batch", True))
        elif variants_allowed == 0 and not promising(stats.get(root)):
            skipped.append((candidate_id, "at least half of each batch must be new ideas: parent null and "
                                          "a set of data fields not used before", True))
        else:
            chosen.append(candidate_id)
            per_family[root] = per_family.get(root, 0) + 1
            if not promising(stats.get(root)):
                variants_allowed -= 1
    return chosen, skipped


def candidate_hash(candidate_id: str) -> str:
    return hashlib.sha256(path("candidates", f"{candidate_id}.json").read_bytes()).hexdigest()


def set_aside(candidate_id: str, reason: str) -> str:
    target = path("candidates", "invalid", f"{candidate_id}_{stamp()}.json")
    target.parent.mkdir(parents=True, exist_ok=True)
    path("candidates", f"{candidate_id}.json").rename(target)
    log_event("candidate_set_aside", candidate=candidate_id, reason=reason, file=str(target.relative_to(RESULTS)))
    return f"{candidate_id}  set aside  {reason}"


def save_json_result(folder: str, name: str, result: dict) -> Path:
    file = path(folder, f"{name}_{stamp()}.json")
    write(file, result)
    return file


def refresh_submitted(client: BrainClient) -> list[dict]:
    listing = list_submitted_alphas(client, max_items=100000)
    if listing["truncated"]:
        raise APIError("Submitted-alpha list is incomplete; the duplicate check cannot run.")
    save_json_result("submitted", "submitted_alphas", listing)
    return listing["results"]


def schedule_retry(retries: dict, candidate_id: str, job) -> None:
    if job.state in ("paused", "uncertain"):
        return
    attempts = retries.pop(candidate_id, {}).get("attempts", 0) + 1
    if was_cancelled(job) and attempts < MAX_STUCK_ATTEMPTS:
        after = datetime.now(timezone.utc) + timedelta(seconds=STUCK_RETRY_AFTER)
        retries[candidate_id] = {"attempts": attempts, "after": after.isoformat()}


def due_retries(state: dict, busy: set[str]) -> list[str]:
    return [candidate_id for candidate_id, entry in state.get("stuck_retries", {}).items()
            if candidate_id not in busy and seconds_since(entry["after"]) >= 0
            and path("candidates", f"{candidate_id}.json").exists()]


def record_simulation(job, candidate_id: str, candidate: dict, started: str, rerun_reason: str | None) -> None:
    write(path("runs", f"{job.id}.json"), job.to_dict())
    log_event("simulated", candidate=candidate_id, parent=candidate["parent"], change=candidate["change"],
              job=job.id, reused=job.created_at < started, rerun_reason=rerun_reason, state=job.state,
              alpha=job.alpha_id, grade=job.grade, settings=job.settings, metrics=job.metrics, error=job.error)
    with locked() as (state, successful):
        state["jobs"][job.id] = candidate_id
        state.setdefault("candidate_hashes", {})[candidate_id] = candidate_hash(candidate_id)
        if DAILY_LIMIT in (job.error or ""):
            state["sim_limit_until"] = now_et().date().isoformat()
        schedule_retry(state.setdefault("stuck_retries", {}), candidate_id, job)
        if not simulation_passed(job):
            return
        snapshot = path("successful", job.alpha_id, f"{job.id}.json")
        write(snapshot, job.to_dict())
        entry = successful.setdefault(job.alpha_id, {"alpha_id": job.alpha_id, "status": "new",
                                                     "job_ids": [], "files": []})
        entry.update(candidate=candidate_id, expression=job.expression, description=job.description,
                     settings=job.settings, metrics=job.metrics, checks=job.checks, grade=job.grade)
        if job.id not in entry["job_ids"]:
            entry["job_ids"].append(job.id)
            entry["files"].append(str(snapshot.relative_to(RESULTS)))
    announce(f"PASSED SIMULATION {job.alpha_id} ({candidate_id}) grade={job.grade} "
             f"sharpe={job.metrics['sharpe']} fitness={job.metrics['fitness']} {job.expression} "
             f"settings={json.dumps(job.settings)}")


def cmd_init(args) -> int:
    if path("experiment_state.json").exists():
        raise ValueError("results/ is already initialized.")
    settings = json.loads(Path(args.settings).read_text(encoding="utf-8"))
    validate_settings(settings)
    for folder in DIRS:
        path(folder).mkdir(parents=True, exist_ok=True)
    write(path("settings.json"), settings)
    write(path("successful_alphas.json"), {})
    write(path("experiment_state.json"), {
        "account": args.account, "hold_until": None, "pooled_counts": {}, "daily_submissions": {},
        "blocked": [], "resolved": {}, "jobs": {}, "explored_datasets": [], "sim_limit_until": None})
    log_event("init", account=args.account, settings=settings)
    print(f"Initialized {RESULTS}")
    return 0


class Blocked(Exception):
    pass


def seconds_since(timestamp: str | None) -> float:
    if timestamp is None:
        return 0
    return (datetime.now(timezone.utc) - datetime.fromisoformat(timestamp)).total_seconds()


def daily_counts(store: JobStore) -> Counter:
    return Counter(datetime.fromisoformat(job.submitted_at).astimezone(EASTERN).date()
                   for job in store.all() if job.submitted_at)


def submitted_today(store: JobStore) -> int:
    return daily_counts(store)[now_et().date()]


def hour_opens_at(store: JobStore, now: float) -> float:
    recent = sorted(timestamp for timestamp in (datetime.fromisoformat(job.attempted_at).timestamp()
                                                for job in store.all() if job.attempted_at)
                    if timestamp > now - 3600)
    return recent[-MAX_PER_HOUR] + 3600 if len(recent) >= MAX_PER_HOUR else now


def budget_used_up(state: dict, store: JobStore) -> str | None:
    seen = state.get("sim_limit_seen")
    if not seen or submitted_today(store) < seen - BUDGET_MARGIN:
        return None
    return (f"HOLD: {submitted_today(store)} simulations today; BRAIN refused more after {seen} on an earlier day. "
            "Research resumes tomorrow (US Eastern).")


def cancel_stuck(job, client: BrainClient, store: JobStore):
    try:
        client.cancel(job.progress_url, client.clock() + STEP_DEADLINE)
    except APIError as error:
        job.state, job.error = "paused", f"Cancelling the stuck simulation failed: {error}"
        return store.save(job)
    job.state = "failed"
    job.error = f"{CANCELLED}: no result {STUCK_AFTER // 60} minutes after submission."
    job.completed_at = utcnow()
    return store.save(job)


def note_submission(job, candidate_id: str) -> None:
    with locked() as (state, _):
        state["jobs"][job.id] = candidate_id


def note_daily_limit(store: JobStore) -> None:
    counts = daily_counts(store)
    sent = counts.pop(now_et().date(), 0)
    # A refusal at or below an earlier day's total was caused by other use, such as the web page.
    if sent <= max([MIN_LEARNED_LIMIT, *counts.values()]):
        return
    with locked() as (state, _):
        state["sim_limit_seen"] = max(state.get("sim_limit_seen") or 0, sent)


def resolve_old_uncertain(store: JobStore) -> None:
    state = read(path("experiment_state.json"))
    for job in store.all():
        if job.state != "uncertain" or job.id in state["resolved"] or seconds_since(job.attempted_at) < UNCERTAIN_WAIT:
            continue
        with locked() as (saved, _):
            candidate_id = saved["jobs"].get(job.id)
            retries = saved.setdefault("stuck_retries", {})
            # A candidate still listed here was already retried, and this job was that retry.
            retried = retries.pop(candidate_id, None) is not None
            if candidate_id and not retried:
                retries[candidate_id] = {"attempts": MAX_STUCK_ATTEMPTS - 1, "after": utcnow()}
            note = (f"Resolved automatically {UNCERTAIN_WAIT // 60} minutes after an unconfirmed submission; "
                    + ("it was already the retry, so it is not retried again." if retried
                       else "if BRAIN ran it, the retry only costs one duplicate simulation."))
            saved["resolved"][job.id] = note
        log_event("resolved", job=job.id, note=note)


def unresolved_reason(jobs: list, state: dict) -> str:
    lines = [f"{job.id}  {job.state}  candidate={state['jobs'].get(job.id, '?')}  {job.expression}" for job in jobs]
    return ("\n".join(lines) + "\nUnresolved jobs above. Paused: rerun `research.py sim` on that candidate to resume "
            "it. Uncertain: check BRAIN, then `research.py resolve JOB_ID --note ...`.")


def simulate(order: list[tuple[str, str | None]], on_done) -> str | None:
    with JobStore(path("jobs.db")) as store:
        resolve_old_uncertain(store)
    state = read(path("experiment_state.json"))
    reason = research_blocked(state, len(pool_files()), now_et())
    if reason:
        raise Blocked(reason)
    candidates = {candidate_id: load_candidate(candidate_id) for candidate_id, _ in order}
    keys = [(candidate["expression"], settings) for candidate, settings in candidates.values()]
    started = utcnow()
    waiting = [(candidate_id, rerun_reason, None) for candidate_id, rerun_reason in order]
    running, stop = {}, None
    with JobStore(path("jobs.db")) as store, store.lock(), make_client(state) as client:
        others = [job for job in unresolved_jobs(store, state) if (job.expression, job.settings) not in keys]
        if others:
            raise Blocked(unresolved_reason(others, state))
        last = store.last_attempt()
        next_submit = last + random.uniform(*GAP) if last else 0

        def finish(job, candidate_id: str, rerun_reason: str | None) -> None:
            nonlocal stop
            record_simulation(job, candidate_id, candidates[candidate_id][0], started, rerun_reason)
            on_done(candidate_id, job)
            if job.state in ("paused", "uncertain") and not stop:
                stop = f"{job.state.upper()}: {candidate_id} ({job.error})"

        while running or (waiting and not stop):
            for job_id, (job, candidate_id, rerun_reason) in list(running.items()):
                if job.next_poll_at > client.wall_clock():
                    continue
                job = advance(job, client, store, client.clock() + STEP_DEADLINE)
                if job.state in ("running", "paused") and job.stage == "running" \
                        and seconds_since(job.submitted_at) >= STUCK_AFTER:
                    job = cancel_stuck(job, client, store)
                if job.state in ("running", "collecting"):
                    running[job_id] = (job, candidate_id, rerun_reason)
                else:
                    del running[job_id]
                    finish(job, candidate_id, rerun_reason)
            while waiting and not stop:
                candidate_id, rerun_reason, job = waiting[0]
                if job is None:
                    candidate, settings = candidates[candidate_id]
                    job = store.prepare(candidate["expression"], settings, client.origin, client.account,
                                        bool(rerun_reason))
                    if not job.description:
                        job.description = dict(candidate["description"])
                        store.save(job)
                    waiting[0] = (candidate_id, rerun_reason, job)
                if job.stage == "queued" and job.state not in TERMINAL:
                    break
                waiting.pop(0)
                if job.state in TERMINAL:
                    finish(job, candidate_id, rerun_reason)
                else:
                    running[job.id] = (job, candidate_id, rerun_reason)
            if waiting and not stop and len(running) < MAX_RUNNING and client.wall_clock() >= next_submit:
                state = read(path("experiment_state.json"))
                stop = research_blocked(state, len(pool_files()), now_et()) or budget_used_up(state, store)
                if stop:
                    continue
                next_submit = hour_opens_at(store, client.wall_clock())
                if next_submit > client.wall_clock():
                    continue
                candidate_id, rerun_reason, job = waiting.pop(0)
                job = advance(job, client, store, client.clock() + STEP_DEADLINE)
                next_submit = client.wall_clock() + random.uniform(*GAP)
                if job.state == "running":
                    note_submission(job, candidate_id)
                    running[job.id] = (job, candidate_id, rerun_reason)
                else:
                    if DAILY_LIMIT in (job.error or ""):
                        note_daily_limit(store)
                    finish(job, candidate_id, rerun_reason)
            wake = [job.next_poll_at for job, _, _ in running.values()]
            if waiting and not stop and len(running) < MAX_RUNNING:
                wake.append(next_submit)
            if wake:
                client.sleep(max(0, min(wake) - client.wall_clock()))
    return stop


def was_cancelled(job) -> bool:
    return job.state == "failed" and (job.error or "").startswith(CANCELLED)


def job_exit_code(job) -> int:
    if job.state in ("paused", "uncertain"):
        return 3
    return 1 if job.state == "failed" else 0


def cmd_sim(args) -> int:
    if args.dry_run:
        candidate, settings = load_candidate(args.candidate_id)
        print(json.dumps(payload(candidate["expression"], settings), indent=2))
        return 0
    done = []
    try:
        stop = simulate([(args.candidate_id, args.rerun)], lambda candidate_id, job: done.append(job))
    except Blocked as reason:
        print(reason)
        return 3
    if not done:
        print(stop)
        return 3
    job = done[0]
    summary(job)
    if simulation_passed(job):
        print(f"Next: research.py pipeline {job.alpha_id}")
    return job_exit_code(job)


def cmd_set_aside(args) -> int:
    if not re.fullmatch(CANDIDATE_ID, args.candidate_id) or not path("candidates", f"{args.candidate_id}.json").exists():
        raise ValueError("No waiting candidate with that id.")
    state = read(path("experiment_state.json"))
    with JobStore(path("jobs.db")) as store:
        if any(state["jobs"].get(job.id) == args.candidate_id for job in unresolved_jobs(store, state)):
            raise ValueError(f"{args.candidate_id} has an unfinished simulation, which needs its file to resume; "
                             "resolve that job instead.")
    print(set_aside(args.candidate_id, args.reason))
    return 0


def cmd_resolve(args) -> int:
    if not args.note.strip():
        raise ValueError("--note must say how the uncertain job was reconciled.")
    with JobStore(path("jobs.db")) as store:
        if store.get(args.job_id).state != "uncertain":
            raise ValueError("Only uncertain jobs can be resolved.")
    with locked() as (state, _):
        state["resolved"][args.job_id] = args.note
    log_event("resolved", job=args.job_id, note=args.note)
    print(f"Resolved {args.job_id}")
    return 0


def run_check(client: BrainClient, alpha_id: str, expression: str) -> tuple[str, dict | None]:
    submitted = refresh_submitted(client)
    duplicate = duplicate_of(alpha_id, expression, submitted)
    if duplicate:
        return "already_submitted", {"duplicate_of": duplicate}
    try:
        result = check_submission(client, alpha_id, deadline=CHECK_DEADLINE)
    except Deadline:
        return "check_pending", {"note": "BRAIN did not finish the check in time; it is retried later"}
    save_json_result("submission_checks", f"{alpha_id}_check", result)
    return check_status(result["checks"]), {"overlaps": overlaps(expression, submitted),
                                            "self_correlation": self_correlation_of(result),
                                            "failed": result["failed"]}


def self_correlation_of(result: dict):
    return next((c.get("value") for c in result["checks"] if c.get("name") == "SELF_CORRELATION"), None)


def set_status(alpha_id: str, status: str, **fields) -> None:
    with locked() as (_, successful):
        successful[alpha_id].update(status=status, **fields)
    log_event("pipeline", alpha=alpha_id, status=status, **fields)


def run_pipeline(alpha_id: str) -> tuple[str, dict]:
    state, successful = read(path("experiment_state.json")), read(path("successful_alphas.json"))
    entry = successful.get(alpha_id)
    if entry is None:
        raise ValueError("Only alphas in successful_alphas.json can enter the pipeline.")
    if entry["status"] in FINAL or alpha_id in state["blocked"]:
        return entry["status"], {"note": "already processed"}
    if GRADES.get(entry.get("grade"), -1) < GRADES[MIN_GRADE]:
        set_status(alpha_id, "below_grade")
        return "below_grade", {"note": f"grade {entry.get('grade')} is below {MIN_GRADE}; improve it as a parent"}
    with make_client(state) as client:
        status, details = run_check(client, alpha_id, entry["expression"])
    details["check_attempts"] = entry.get("check_attempts", 0) + 1
    if status == "passed":
        write(path("submission_pool", f"{alpha_id}.json"),
              {key: entry[key] for key in ("alpha_id", "grade", "metrics", "settings", "expression")}
              | {"self_correlation": details["self_correlation"], "pooled_at": utcnow()})
        with locked() as (state, _):
            today = now_et().date().isoformat()
            state["pooled_counts"][today] = state["pooled_counts"].get(today, 0) + 1
        status = "pooled"
    set_status(alpha_id, status, **details)
    return status, details


def cmd_pipeline(args) -> int:
    status, details = run_pipeline(args.alpha_id)
    print(f"{args.alpha_id}  {status}  {json.dumps(details)}")
    return 0 if status in ("pooled", *FINAL) else (3 if status in ("check_pending", "check_error") else 1)


def by_rank(state: dict, candidate_ids: list[str]) -> list[str]:
    positions = {candidate_id: position for position, candidate_id in enumerate(state.get("ranking", {}))}
    return sorted(candidate_ids, key=lambda candidate_id: positions.get(candidate_id, len(positions)))


def batch_entry_problem(entry, ranking: dict, tried: set[str]) -> str | None:
    if not isinstance(entry, dict):
        return "it must be an object"
    if not isinstance(entry.get("id"), str) or not re.fullmatch(CANDIDATE_ID, entry["id"]):
        return "id may contain only letters, digits, '_', '.' and '-'"
    if not isinstance(entry.get("reason"), str) or not entry["reason"].strip() or "\n" in entry["reason"]:
        return f"{entry['id']}: reason must give the expected edge in one line"
    if entry["id"] in ranking:
        return f"{entry['id']} is listed twice"
    if entry["id"] in tried:
        return f"{entry['id']} was already simulated; write changes under a new id"
    if entry.keys() == BATCH_KEYS and not path("candidates", f"{entry['id']}.json").exists():
        return f"{entry['id']} is not a waiting candidate; give its full candidate fields"
    return None


def split_batch(state: dict) -> list[str]:
    file = path("batch.json")
    if not file.exists():
        return []
    archived = path("batches", f"{stamp()}_batch.json")
    archived.parent.mkdir(parents=True, exist_ok=True)
    file.rename(archived)
    try:
        entries = read(archived)
    except json.JSONDecodeError as error:
        return [f"{archived.relative_to(RESULTS)} ignored: invalid JSON ({error})"]
    if not isinstance(entries, list):
        return [f"{archived.relative_to(RESULTS)} ignored: it must be a JSON list of candidates, best first"]
    tried = set(state["jobs"].values())
    lines, ranking = [], {}
    for number, entry in enumerate(entries, 1):
        problem = batch_entry_problem(entry, ranking, tried)
        if problem:
            lines.append(f"batch entry {number} skipped: {problem}")
            continue
        if entry.keys() != BATCH_KEYS:
            write(path("candidates", f"{entry['id']}.json"),
                  {key: value for key, value in entry.items() if key not in BATCH_KEYS})
        ranking[entry["id"]] = entry["reason"]
    for file in path("candidates").glob("*.json"):
        if file.stem not in tried and file.stem not in ranking:
            lines.append(set_aside(file.stem, "left out of the latest ranked batch"))
    with locked() as (saved, _):
        saved["ranking"] = ranking
    log_event("batch", file=str(archived.relative_to(RESULTS)), ranking=ranking)
    return lines


def queued_candidates(state: dict, store: JobStore) -> tuple[list[str], list[str], list[str]]:
    paused = list(dict.fromkeys(state["jobs"][job.id] for job in store.all()
                                if job.state in RESUMABLE and job.id in state["jobs"]))
    tried = set(state["jobs"].values())
    hashes = state.get("candidate_hashes", {})
    files = sorted(path("candidates").glob("*.json"), key=lambda file: file.stat().st_mtime)
    fresh = by_rank(state, [file.stem for file in files if file.stem not in tried])
    reused = [file.stem for file in files
              if file.stem in hashes and file.stem not in paused and candidate_hash(file.stem) != hashes[file.stem]]
    return paused, fresh, reused


def pipeline_backlog() -> list[str]:
    successful = read(path("successful_alphas.json"))
    return [alpha_id for alpha_id, entry in successful.items()
            if entry["status"] in RETRY_PIPELINE and entry.get("check_attempts", 0) < MAX_CHECK_ATTEMPTS]


def reversed_note(state: str, metrics: dict) -> str:
    if state != "complete" or type(metrics.get("sharpe")) not in (int, float) or metrics["sharpe"] > REVERSED_SHARPE:
        return ""
    fitness = metrics.get("fitness")
    return (f"REVERSED: the negated expression scores about sharpe={-metrics['sharpe']} "
            f"fitness={-fitness if type(fitness) in (int, float) else '-'}")


def queue_line(candidate_id: str, job) -> str:
    metrics = job.metrics
    notable = " ".join(f"{check.get('result')}:{check.get('name')}" for check in job.checks
                       if check.get("result") != "PASS")
    reversed_edge = reversed_note(job.state, metrics)
    return (f"{candidate_id}  {job.state}  alpha={job.alpha_id or '-'}  grade={job.grade or '-'}  "
            f"sharpe={metrics['sharpe']} fitness={metrics['fitness']} turnover={metrics['turnover']}"
            + (f"  {notable}" if notable else "") + (f"  {reversed_edge}" if reversed_edge else "")
            + (f"  error={job.error}" if job.error else ""))


def pipeline_status(alpha_id: str) -> str:
    try:
        return run_pipeline(alpha_id)[0]
    except APIError as error:
        return f"error ({error}); retried in the next queue run"


def cmd_queue(args) -> int:
    lines, stop, simulated = [], None, 0

    def emit(line: str) -> None:
        lines.append(line)
        print(line, flush=True)

    with JobStore(path("jobs.db")) as store:
        resolve_old_uncertain(store)
        for line in split_batch(read(path("experiment_state.json"))):
            emit(line)
        state = read(path("experiment_state.json"))
        paused, fresh, reused = queued_candidates(state, store)
        unresolved = unresolved_jobs(store, state)
        busy = {state["jobs"][job.id] for job in unresolved if job.id in state["jobs"]}
        others = [job for job in unresolved if state["jobs"].get(job.id) not in paused]
        blocked = (research_blocked(state, len(pool_files()), now_et()) or budget_used_up(state, store)
                   or (unresolved_reason(others, state) if others else None))

    for candidate_id in reused:
        emit(set_aside(candidate_id, "this id was already simulated; write changes under a new id"))
    for alpha_id in pipeline_backlog():
        emit(f"{alpha_id}  pipeline retry  {pipeline_status(alpha_id)}")
    if blocked:
        return finish_queue(lines, blocked, True)
    runs = simulated_runs(state)
    known = {(run["expression"], json.dumps(run["settings"], sort_keys=True)): candidate_id for candidate_id, run in runs}
    valid = {}
    for candidate_id in paused + fresh:
        try:
            data, settings = load_candidate(candidate_id)
        except ValueError as error:
            emit(set_aside(candidate_id, str(error)))
            continue
        key = (data["expression"], json.dumps(settings, sort_keys=True))
        if candidate_id not in paused and known.setdefault(key, candidate_id) != candidate_id:
            emit(set_aside(candidate_id, f"same expression and settings as {known[key]}, which already has a result; "
                                         f"see `research.py show {known[key]}`"))
            continue
        valid[candidate_id] = (data["expression"], settings["universe"])
    accepted = [candidate_id for candidate_id in valid
                if candidate_id in state.get("accepted", []) and candidate_id not in paused]
    batch = [candidate_id for candidate_id in valid if candidate_id not in paused and candidate_id not in accepted]
    roots = assign_families([(candidate_id, run["expression"], run["settings"]["universe"]) for candidate_id, run in runs]
                            + [(candidate_id, *valid[candidate_id]) for candidate_id in batch])
    used = set().union(*(data_fields(run["expression"]) for _, run in runs))
    new_data = any(data_fields(valid[candidate_id][0]) - used for candidate_id in batch)
    chosen, skipped = diversify(batch, roots, family_stats(runs, roots, submitted_alphas()), new_data)
    deferred = {}
    for candidate_id, reason, later in skipped:
        waited = state.get("deferred", {}).get(candidate_id, 0)
        if later and waited < MAX_DEFERRALS:
            deferred[candidate_id] = waited + 1
            emit(f"{candidate_id}  waits for a later batch ({waited + 1}/{MAX_DEFERRALS})  {reason}")
        else:
            emit(set_aside(candidate_id, reason))
    with locked() as (saved, _):
        saved["accepted"] = accepted + chosen
        saved["deferred"] = deferred
    order = ([(candidate_id, None) for candidate_id in valid if candidate_id in paused]
             + [(candidate_id, "BRAIN got stuck on the last attempt, so it was cancelled")
                for candidate_id in due_retries(state, busy)]
             + [(candidate_id, None) for candidate_id in by_rank(state, accepted + chosen)])
    passed = []

    def done(candidate_id: str, job) -> None:
        nonlocal simulated
        simulated += 1
        emit(queue_line(candidate_id, job))
        if simulation_passed(job):
            passed.append((candidate_id, job.alpha_id))

    try:
        stop = simulate(order[: args.max], done) if order else None
    except Blocked as reason:
        stop = str(reason)
    for candidate_id, alpha_id in passed:
        emit(f"{candidate_id}  alpha={alpha_id}  pipeline={pipeline_status(alpha_id)}")
    ranking = state.get("ranking", {})
    for candidate_id, _ in order[args.max:]:
        if candidate_id in ranking:
            emit(f"{candidate_id}  waits for the next batch; rank it again or it is set aside  {ranking[candidate_id]}")
    return finish_queue(lines, stop or f"Queue finished: {simulated} simulated.", bool(stop))


def finish_queue(lines: list[str], footer: str, stopped: bool) -> int:
    print(footer)
    if lines:
        report = path("batches", f"{stamp()}.txt")
        report.parent.mkdir(parents=True, exist_ok=True)
        report.write_text("\n".join(lines + [footer]) + "\n", encoding="utf-8")
        print(f"Summary: {report.relative_to(RESULTS.parent)}")
    return 3 if stopped else 0


def retire(file: Path, reason: str) -> None:
    entry = read(file) | {"removed": reason, "removed_at": utcnow()}
    write(path("submission_pool", "removed", file.name), entry)
    file.unlink()


def cmd_daily_submit(args) -> int:
    # A second run waits here, then finds the hold the first one set.
    with open(path(".submit.lock"), "a") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        return daily_submit()


def daily_submit() -> int:
    state = read(path("experiment_state.json"))
    now = now_et()
    today = now.date().isoformat()
    if state["hold_until"] and state["hold_until"] >= today:
        print(f"HOLD: the daily submission for {state['hold_until']} (US Eastern) already ran.")
        return 3
    files = pool_files()
    if not submit_due(state, len(files), now):
        print(f"Not due: needs {POOL_TRIGGER} alphas pooled today or {SUBMIT_HOUR}:00 US Eastern with a nonempty pool.")
        return 3
    pool = sorted(((read(file), file) for file in files), key=lambda item: rank_key(item[0]), reverse=True)
    roots, stats = current_families(state)
    successful = read(path("successful_alphas.json"))
    with make_client(state) as client:
        for entry, file in pool:
            alpha_id = entry["alpha_id"]
            if read(path("experiment_state.json"))["daily_submissions"].get(today, 0) >= DAILY_CAP:
                break
            if alpha_id in state["blocked"]:
                retire(file, "blocked")
                continue
            if GRADES.get(entry.get("grade"), -1) < GRADES[MIN_GRADE]:
                retire(file, "below_grade")
                set_status(alpha_id, "below_grade")
                continue
            root = roots.get(successful[alpha_id]["candidate"])
            waited = now - datetime.fromisoformat(entry["pooled_at"])
            if (GRADES.get(entry.get("grade"), -1) < GRADES[SUBMIT_NOW_GRADE] and not exhausted(stats.get(root))
                    and waited < timedelta(days=MAX_POOL_DAYS)):
                print(f"{alpha_id}  waits: family {root} may still reach a higher grade; it is submitted once the "
                      f"family is exhausted or after {MAX_POOL_DAYS} days in the pool")
                continue
            try:
                status, details = run_check(client, alpha_id, entry["expression"])
            except APIError as error:
                status, details = "check_error", {"note": str(error)}
            if status in ("check_pending", "check_error"):
                print(f"{alpha_id}  {status}: {details['note']}; it stays in the pool")
                continue
            if status != "passed":
                retire(file, status)
                set_status(alpha_id, status, **details)
                continue
            try:
                result = submit_alpha(client, alpha_id, confirm=True)
            except Throttled as error:
                announce(f"SUBMISSION REFUSED {alpha_id}: {error} Stopped today's submissions; the alpha stays in the pool.")
                break
            except APIError as error:
                with locked() as (state, _):
                    state["blocked"].append(alpha_id)
                retire(file, "uncertain")
                set_status(alpha_id, "uncertain", error=str(error))
                announce(f"UNCERTAIN SUBMISSION {alpha_id}: {error} Stopped today's submissions; confirm in BRAIN.")
                break
            save_json_result("submission_checks", f"{alpha_id}_submit", result)
            retire(file, result["status"])
            if result["status"] == "submitted":
                with locked() as (state, _):
                    state["daily_submissions"][today] = state["daily_submissions"].get(today, 0) + 1
                set_status(alpha_id, "submitted", submitted_at=utcnow())
                announce(f"SUBMITTED {alpha_id} grade={entry['grade']} {entry['expression']} "
                         f"settings={json.dumps(entry['settings'])} metrics={json.dumps(entry['metrics'])}")
            else:
                status = "correlated" if "SELF_CORRELATION" in result["failed"] else "rejected"
                set_status(alpha_id, status, failed=result["failed"], self_correlation=self_correlation_of(result))
    with locked() as (state, _):
        state["hold_until"] = today
    print(f"Submitted today: {read(path('experiment_state.json'))['daily_submissions'].get(today, 0)}. "
          "Research continues; new passes wait in the pool for the next day's submission.")
    return 0


def cmd_log(args) -> int:
    data = json.loads(args.data)
    if not isinstance(data, dict):
        raise ValueError("--data must be a JSON object.")
    if args.event == "dataset_explored":
        if not isinstance(data.get("dataset"), str):
            raise ValueError("dataset_explored needs a dataset id.")
        with locked() as (state, _):
            if data["dataset"] not in state["explored_datasets"]:
                state["explored_datasets"].append(data["dataset"])
    log_event(args.event, **data)
    return 0


def cmd_status(args) -> int:
    state, successful = read(path("experiment_state.json")), read(path("successful_alphas.json"))
    now = now_et()
    today = now.date().isoformat()
    pool_size = len(pool_files())
    print(f"US Eastern {now:%Y-%m-%d %H:%M}  account={state['account']}")
    print(f"blocked: {research_blocked(state, pool_size, now) or 'no, research may continue'}")
    print(f"pool={pool_size}  pooled_today={state['pooled_counts'].get(today, 0)}/{POOL_TRIGGER}  "
          f"submitted_today={state['daily_submissions'].get(today, 0)}/{DAILY_CAP}")
    counts = {}
    for entry in successful.values():
        counts[entry["status"]] = counts.get(entry["status"], 0) + 1
    print(f"successful alphas by status: {json.dumps(counts)}")
    print(f"explored datasets: {len(state['explored_datasets'])}")
    with JobStore(path("jobs.db")) as store:
        jobs = store.all()
        for job in unresolved_jobs(store, state):
            print(f"UNRESOLVED {job.id}  {job.state}  candidate={state['jobs'].get(job.id, '?')}  {job.error}")
        last = store.last_attempt()
        today = submitted_today(store)
        earliest = max(last + GAP[0], hour_opens_at(store, time.time())) if last else None
    next_allowed = f"{datetime.fromtimestamp(earliest, EASTERN):%H:%M:%S} ET at the earliest" if earliest else "now"
    seen = state.get("sim_limit_seen")
    print(f"simulations: {len(jobs)}  today: {today}"
          + (f" (stops at {seen - BUDGET_MARGIN}; BRAIN refused more after {seen})" if seen else "")
          + f"  next allowed submission: {next_allowed}")
    return 0


def run_outcome(run: dict, successful: dict) -> str:
    if run["alpha_id"] in successful:
        return successful[run["alpha_id"]]["status"]
    failing = [check.get("name") for check in run["checks"] if check.get("result") == "FAIL"]
    return "fails " + ",".join(failing) if failing else "no checks"


def cmd_history(args) -> int:
    state, successful = read(path("experiment_state.json")), read(path("successful_alphas.json"))
    defaults = read(path("settings.json"))
    runs = simulated_runs(state)
    failed = [(candidate_id, run) for candidate_id, run in runs if run["state"] == "failed"]
    roots, stats = current_families(state)
    ranked = sorted((item for item in stats.items() if item[1]["best"]), key=lambda item: item[1]["fitness"], reverse=True)
    total = sum(entry["runs"] for entry in stats.values())
    in_top3 = sum(entry["runs"] for _, entry in ranked[:3])
    print(f"simulated={total}  failed={len(failed)}  passed_simulation={len(successful)}  families={len(stats)}  "
          f"exhausted={sum(map(exhausted, stats.values()))}  runs_in_top3_families={in_top3}/{total}")
    print(f"default settings: {json.dumps(defaults)}")
    print(f"Best {min(args.top, len(ranked))} families, one line each (family runs runs_since_progress best_candidate "
          "outcome sharpe fitness turnover settings expression). EXHAUSTED families take no more variants; "
          f"variants of PROMISING families (fitness at least {NEAR_FITNESS} or Sharpe at least {NEAR_SHARPE} "
          "in either direction, and nothing submitted from them) do not need new ideas beside them:")
    for root, entry in ranked[: args.top]:
        candidate_id, run = entry["best"]
        metrics = run["metrics"]
        overrides = {key: value for key, value in run["settings"].items() if defaults.get(key) != value}
        label = " EXHAUSTED" if exhausted(entry) else " PROMISING" if promising(entry) else ""
        print(f"{root}{label}  {entry['runs']}  {entry['since_best']}  {candidate_id}  "
              f"{run_outcome(run, successful)}  {metrics.get('sharpe')} {metrics.get('fitness')} {metrics.get('turnover')}  "
              f"{json.dumps(overrides) if overrides else 'default'}  {run['expression']}")
    blocks = sorted((entry for entry in stats.values() if entry["edge"] >= BLOCK_EDGE and not entry["submitted"]),
                    key=lambda entry: entry["edge"], reverse=True)
    if blocks:
        print(f"Building blocks: the strongest simulation of each idea with an edge, even if too weak alone. Combine "
              "legs from different datasets; negate a REVERSED one (candidate direction sharpe fitness turnover "
              "universe expression):")
        for entry in blocks[: args.blocks]:
            candidate_id, run = entry["edge_run"]
            metrics = run["metrics"]
            direction = "REVERSED" if number(metrics.get("sharpe")) < 0 else "as_is"
            print(f"{candidate_id}  {direction}  {metrics.get('sharpe')} {metrics.get('fitness')} {metrics.get('turnover')}  "
                  f"{run['settings']['universe']}  {run['expression']}")
    reversed_runs = sorted((item for item in runs if reversed_note(item[1]["state"], item[1]["metrics"])),
                           key=lambda item: number(item[1]["metrics"].get("sharpe")))
    if reversed_runs:
        print("Reversed edges: build variants on the negated expression (candidate note expression):")
        for candidate_id, run in reversed_runs[: args.top]:
            print(f"{candidate_id}  {reversed_note(run['state'], run['metrics'])}  {run['expression']}")
    submitted = [entry for entry in successful.values() if entry["status"] == "submitted"]
    if submitted:
        print("Submitted, so new alphas must not correlate with these (alpha expression):")
        for entry in submitted:
            print(f"{entry['alpha_id']}  {entry['expression']}")
    repairable = [entry for entry in successful.values() if entry["status"] in ("correlated", "below_grade")]
    if repairable:
        print("Passed every simulation check but cannot be submitted; improve these as parents "
              "(candidate status grade self_correlation overlaps expression):")
        for entry in repairable:
            print(f"{entry['candidate']}  {entry['status']}  {entry.get('grade')}  {entry.get('self_correlation', '-')}  "
                  f"{','.join(entry.get('overlaps') or []) or '-'}  {entry['expression']}")
    gaps = {alpha_id: grade_gap(entry["metrics"].get("fitness")) for alpha_id, entry in successful.items()
            if entry["status"] == "pooled" and not exhausted(stats.get(roots.get(entry["candidate"])))}
    near = sorted((alpha_id for alpha_id, gap in gaps.items() if gap <= NEAR_GRADE), key=gaps.get)
    if near:
        print(f"Pooled, within {NEAR_GRADE} fitness of the next grade line ({', '.join(map(str, GRADE_LINES))}); "
              "improve these as parents to reach a higher grade "
              "(candidate grade fitness gap expression):")
        for alpha_id in near[: args.top]:
            entry = successful[alpha_id]
            print(f"{entry['candidate']}  {entry.get('grade')}  {entry['metrics'].get('fitness')}  {gaps[alpha_id]}  "
                  f"{entry['expression']}")
    if failed:
        print("Latest errors (candidate error expression):")
        for candidate_id, run in failed[-args.errors:]:
            print(f"{candidate_id}  {(run['error'] or '')[:160]}  {run['expression']}")
    events = map(json.loads, path("experiments.jsonl").read_text(encoding="utf-8").splitlines())
    explored = [event for event in events if event["event"] == "dataset_explored"]
    if explored:
        print("Explored datasets (dataset outcome):")
        for event in explored:
            print(f"{event['dataset']}  {event.get('outcome', '-')}")
    return 0


def latest_runs(state: dict) -> dict[str, dict]:
    return dict(simulated_runs(state))


def improved(run: dict | None, parent: dict | None) -> bool | None:
    if not run or not parent or run["state"] != "complete" or parent["state"] != "complete":
        return None
    return number(run["metrics"].get("fitness")) > number(parent["metrics"].get("fitness"))


def read_lessons() -> dict[str, dict]:
    return read(path("lessons.json"), {})


def lesson_tests(runs: dict[str, dict]) -> dict[str, list[tuple[str, str]]]:
    tests = {}
    for candidate_id, run in runs.items():
        candidate = read(path("candidates", f"{candidate_id}.json"))
        test = candidate.get("tests") if isinstance(candidate, dict) else None
        if not isinstance(test, dict):
            continue
        better = improved(run, runs.get(candidate["parent"]))
        if better is not None:
            verdict = VERDICTS[0] if better == (test.get("expect") == "better") else VERDICTS[1]
            tests.setdefault(test.get("lesson"), []).append((candidate_id, verdict))
    return tests


def assess(lesson: dict, tests: list[tuple[str, str]]) -> tuple[str, int, int]:
    verdicts = [verdict for _, verdict in tests] + [judgment["verdict"] for judgment in lesson["judgments"]]
    supports, contradicts = verdicts.count(VERDICTS[0]), verdicts.count(VERDICTS[1])
    if lesson["retired"]:
        return "retired", supports, contradicts
    if contradicts >= DISPROVED_AT and contradicts > supports:
        return "disproved", supports, contradicts
    if contradicts and contradicts >= supports:
        return "doubtful", supports, contradicts
    return ("holding" if supports else "untested"), supports, contradicts


def assessed_lessons(state: dict) -> dict[str, tuple[dict, str, int, int, list]]:
    tests = lesson_tests(latest_runs(state))
    return {lesson_id: (lesson, *assess(lesson, tests.get(lesson_id, [])), tests.get(lesson_id, []))
            for lesson_id, lesson in read_lessons().items()}


def open_lesson(lesson_id: str, state: dict, closed_ok: bool = False) -> tuple[dict, list]:
    found = assessed_lessons(state).get(lesson_id)
    if not found:
        raise ValueError(f"No lesson {lesson_id}; run `research.py lessons` for the list.")
    lesson, status, _, _, tests = found
    if status in ("retired", "disproved") and not closed_ok:
        raise ValueError(f"Lesson {lesson_id} is {status}; write a new lesson instead.")
    return lesson, tests


def validate_test(candidate_id: str, candidate: dict) -> None:
    test = candidate["tests"]
    if (not isinstance(test, dict) or test.keys() != {"lesson", "expect"} or not isinstance(test["lesson"], str)
            or test["expect"] not in EXPECTS):
        raise ValueError('tests must be {"lesson": "<lesson id>", "expect": "better" or "not_better"}.')
    state = read(path("experiment_state.json"))
    parent = latest_runs(state).get(candidate["parent"])
    if not parent or parent["state"] != "complete":
        raise ValueError("A candidate that tests a lesson needs a parent with a complete simulation to compare with.")
    # Once submitted, a candidate stays valid even if its lesson closes, so its job can still be resumed.
    open_lesson(test["lesson"], state, closed_ok=candidate_id in state["jobs"].values())


def cmd_lessons(args) -> int:
    lessons = assessed_lessons(read(path("experiment_state.json")))
    active = {key: value for key, value in lessons.items() if value[1] not in ("retired", "disproved")}
    print(f"Active lessons {len(active)}/{MAX_LESSONS} (id status supports-contradicts lesson; tests). "
          "Lessons are claims, not facts: test untested and doubtful ones.")
    order = ("doubtful", "untested", "holding")
    for lesson_id, (lesson, status, supports, contradicts, tests) in sorted(
            active.items(), key=lambda item: order.index(item[1][1])):
        shown = " ".join(f"{candidate_id}{'+' if verdict == VERDICTS[0] else '-'}" for candidate_id, verdict in tests[-5:])
        print(f"{lesson_id}  {status} {supports}-{contradicts}  {lesson['text']}" + (f"  tests: {shown}" if shown else ""))
    gone = [(lesson_id, value) for lesson_id, value in lessons.items() if lesson_id not in active]
    if gone:
        print("Retired or disproved; do not learn them again without new evidence (id status reason):")
        for lesson_id, (lesson, status, supports, contradicts, _) in gone[-args.retired:]:
            reason = lesson["retired"]["reason"] if lesson["retired"] else f"contradicted {contradicts} times, supported {supports}"
            print(f"{lesson_id}  {status}  {lesson['text']}  reason: {reason}")
    return 0


def cmd_lesson(args) -> int:
    state, lessons = read(path("experiment_state.json")), read_lessons()
    runs = latest_runs(state)
    if args.action == "add":
        if not args.text.strip():
            raise ValueError("A lesson needs text.")
        active = [value for value in assessed_lessons(state).values() if value[1] not in ("retired", "disproved")]
        if len(active) >= MAX_LESSONS:
            raise ValueError(f"{MAX_LESSONS} lessons are active; retire or merge some first.")
        evidence = [candidate_id.strip() for candidate_id in args.evidence.split(",") if candidate_id.strip()]
        unknown = [candidate_id for candidate_id in evidence if candidate_id not in runs]
        if not evidence or unknown:
            raise ValueError(f"Cite at least one simulated candidate as evidence; not simulated: {unknown}.")
        lesson_id = f"L{1 + max((int(key[1:]) for key in lessons), default=0)}"
        lessons[lesson_id] = {"text": args.text.strip(), "evidence": evidence, "added": utcnow(),
                              "judgments": [], "retired": None}
        args.lesson_id = lesson_id
        print(f"{lesson_id} added. Test it with candidates that have \"tests\": "
              f"{{\"lesson\": \"{lesson_id}\", \"expect\": \"better\" or \"not_better\"}}.")
    elif args.action == "retire":
        lesson, _ = open_lesson(args.lesson_id, state)
        lesson["retired"] = {"reason": args.reason, "at": utcnow()}
        print(f"{args.lesson_id} retired.")
    else:
        lesson, tests = open_lesson(args.lesson_id, state)
        if args.candidate_id not in runs:
            raise ValueError(f"{args.candidate_id} has no simulation to judge by.")
        if args.candidate_id in {candidate_id for candidate_id, _ in tests} | {
                judgment["candidate"] for judgment in lesson["judgments"]}:
            raise ValueError(f"{args.candidate_id} already counts for {args.lesson_id}.")
        lesson["judgments"].append({"candidate": args.candidate_id, "verdict": args.verdict,
                                    "reason": args.reason, "at": utcnow()})
        print(f"{args.lesson_id}: {args.candidate_id} {args.verdict} it.")
    write(path("lessons.json"), lessons | ({} if args.action == "add" else {args.lesson_id: lesson}))
    log_event(f"lesson_{args.action}", **{key: value for key, value in vars(args).items() if key not in ("command", "action")})
    return 0


def operators(expression: str) -> set[str]:
    return set(re.findall(r"([A-Za-z_]\w*)\s*\(", expression))


def change_labels(parent: dict, run: dict) -> list[str]:
    labels = []
    for key in sorted(parent["settings"].keys() | run["settings"].keys()):
        before, after = parent["settings"].get(key), run["settings"].get(key)
        if before == after:
            continue
        numeric = all(type(value) in (int, float) for value in (before, after))
        labels.append(f"{key} {'up' if numeric and after > before else 'down'}" if numeric else f"{key}={after}")
    before, after = operators(parent["expression"]), operators(run["expression"])
    labels += [f"+{name}" for name in sorted(after - before)] + [f"-{name}" for name in sorted(before - after)]
    if data_fields(parent["expression"]) != data_fields(run["expression"]):
        labels.append("fields changed")
    return labels or ["parameters only"]


def field_datasets() -> dict[str, str]:
    owners = {}
    for file in path("catalog").glob("fields_*.json"):
        for field in (read(file) or {}).get("results", []):
            owners[field["id"]] = field["dataset"]["id"]
    return owners


def failing(run: dict) -> set[str]:
    return {check.get("name") for check in run["checks"] if check.get("result") == "FAIL"}


def cmd_patterns(args) -> int:
    state, successful = read(path("experiment_state.json")), read(path("successful_alphas.json"))
    runs = latest_runs(state)
    complete = {candidate_id: run for candidate_id, run in runs.items() if run["state"] == "complete"}
    effects, fixes, failures = {}, {}, Counter()
    for candidate_id, run in complete.items():
        failures.update(failing(run))
        candidate = read(path("candidates", f"{candidate_id}.json"))
        parent = complete.get(candidate.get("parent")) if isinstance(candidate, dict) else None
        if not parent:
            continue
        delta = number(run["metrics"].get("fitness")) - number(parent["metrics"].get("fitness"))
        for label in change_labels(parent, run):
            effects.setdefault(label, []).append(delta)
            for check in failing(parent):
                fixes.setdefault(check, {}).setdefault(label, []).append(check not in failing(run))
    print(f"Patterns over all {len(complete)} complete simulations. Facts computed from results; "
          "fewer than 3 tries is weak evidence.")
    print("Effect of a change on fitness versus the parent (change tries better median_change):")
    for label, deltas in sorted(effects.items(), key=lambda item: -len(item[1]))[: args.top]:
        print(f"{label}  {len(deltas)}  {sum(delta > 0 for delta in deltas)}  {median(deltas):+.2f}")
    owners, datasets, universes = field_datasets(), {}, {}
    for candidate_id, run in complete.items():
        for dataset in {owners.get(field, "other") for field in data_fields(run["expression"])} or {"other"}:
            datasets.setdefault(dataset, []).append(run)
        universes.setdefault(run["settings"]["universe"], []).append(run)
    for name, groups in (("dataset", datasets), ("universe", universes)):
        print(f"By {name} ({name} runs passed best_fitness median_fitness best_sharpe):")
        for key, items in sorted(groups.items(), key=lambda item: -len(item[1])):
            fitness = [number(run["metrics"].get("fitness")) for run in items]
            print(f"{key}  {len(items)}  {sum(run['alpha_id'] in successful for run in items)}  {max(fitness):.2f}  "
                  f"{median(fitness):.2f}  {max(number(run['metrics'].get('sharpe')) for run in items):.2f}")
    print("Failing checks (check runs; changes that cleared it in a child: change cleared/tried):")
    for check, count in failures.most_common(args.top):
        cleared = sorted(fixes.get(check, {}).items(), key=lambda item: -sum(item[1]))[:4]
        print(f"{check}  {count}" + ("  " + ", ".join(f"{label} {sum(results)}/{len(results)}"
                                                      for label, results in cleared) if cleared else ""))
    return 0


def cmd_show(args) -> int:
    state = read(path("experiment_state.json"))
    attempts = [run for candidate_id, run in simulated_runs(state) if candidate_id == args.candidate_id]
    candidate = read(path("candidates", f"{args.candidate_id}.json"))
    if not attempts and not isinstance(candidate, dict):
        raise ValueError(f"No candidate or simulation named {args.candidate_id}.")
    if isinstance(candidate, dict):
        print(f"candidate: {json.dumps(candidate)}")
    runs = latest_runs(state)
    for run in attempts:
        print(f"attempt {run['id']}  {run['state']}  alpha={run['alpha_id'] or '-'}  grade={run['grade'] or '-'}  "
              f"submitted={run['submitted_at']}  completed={run['completed_at']}" + (f"  error={run['error']}" if run["error"] else ""))
    if not attempts:
        return 0
    run = attempts[-1]
    print(f"settings: {json.dumps(run['settings'])}")
    result = (run.get("raw_result") or {}).get("is", {})
    print(f"metrics: {json.dumps({key: value for key, value in result.items() if key != 'checks'} or run['metrics'])}")
    print("checks: " + "  ".join(f"{check.get('result')}:{check.get('name')}"
                                 + (f" {check['value']}/{check['limit']}" if "value" in check and "limit" in check else "")
                                 for check in run["checks"]))
    parent_id = candidate.get("parent") if isinstance(candidate, dict) else None
    parent = runs.get(parent_id)
    if parent:
        print(f"versus parent {parent_id}: changes {', '.join(change_labels(parent, run))}; " + "  ".join(
            f"{key} {parent['metrics'].get(key)} -> {run['metrics'].get(key)}" for key in ("sharpe", "fitness", "turnover")))
    children = []
    for child_id, child in runs.items():
        data = read(path("candidates", f"{child_id}.json"))
        if isinstance(data, dict) and data.get("parent") == args.candidate_id:
            children.append(f"{child_id} fitness={child['metrics'].get('fitness')}")
    if children:
        print(f"children: {', '.join(children)}")
    for lesson_id, (lesson, status, _, _, tests) in assessed_lessons(state).items():
        roles = (["evidence"] * (args.candidate_id in lesson["evidence"])
                 + [verdict for candidate_id, verdict in tests if candidate_id == args.candidate_id]
                 + [judgment["verdict"] for judgment in lesson["judgments"] if judgment["candidate"] == args.candidate_id])
        if roles:
            print(f"lesson {lesson_id} ({status}): {', '.join(roles)}  {lesson['text']}")
    return 0


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description="Research workflow over WQBrainSim with every hard rule enforced.")
    commands = root.add_subparsers(dest="command", required=True)
    sub = commands.add_parser("init", help="Create results/ once")
    sub.add_argument("--account", required=True, help="Nonsecret account label")
    sub.add_argument("--settings", required=True, help="Default settings JSON")
    sub = commands.add_parser("sim", help="Simulate results/candidates/<id>.json (also resumes its paused job)")
    sub.add_argument("candidate_id")
    sub.add_argument("--dry-run", action="store_true")
    sub.add_argument("--rerun", metavar="REASON", help="Start a new attempt; the reason is recorded")
    sub = commands.add_parser("queue", help="Split results/batch.json, then simulate candidates best-ranked first "
                                            "(paused ones before them) without supervision")
    sub.add_argument("--max", type=int, default=BATCH_SIZE,
                     help="Most candidates to process in this run; the rest wait for the next batch")
    sub = commands.add_parser("resolve", help="Mark an uncertain job as reconciled")
    sub.add_argument("job_id")
    sub.add_argument("--note", required=True)
    sub = commands.add_parser("set_aside", help="Move a waiting candidate out of the queue")
    sub.add_argument("candidate_id")
    sub.add_argument("--reason", required=True)
    sub = commands.add_parser("pipeline", help="Duplicate check, submission check, then pool")
    sub.add_argument("alpha_id")
    commands.add_parser("daily_submit", help="End-of-day submission from the pool")
    sub = commands.add_parser("log", help="Append a research event")
    sub.add_argument("event")
    sub.add_argument("--data", default="{}", help="JSON object")
    commands.add_parser("status", help="Short checkpoint summary")
    sub = commands.add_parser("history", help="Compact record of all simulations so far, for planning the next batch")
    sub.add_argument("--top", type=int, default=20, help="How many of the best complete simulations to list")
    sub.add_argument("--errors", type=int, default=5, help="How many of the latest failed simulations to list")
    sub.add_argument("--blocks", type=int, default=25, help="How many building blocks to list")
    sub = commands.add_parser("patterns", help="Statistics over all simulations: effects of changes, datasets, checks")
    sub.add_argument("--top", type=int, default=25, help="How many rows to list per table")
    sub = commands.add_parser("show", help="Everything recorded about one candidate: attempts, metrics, checks, parent")
    sub.add_argument("candidate_id")
    sub = commands.add_parser("lessons", help="Active lessons with their test record, then retired ones")
    sub.add_argument("--retired", type=int, default=10, help="How many retired lessons to list")
    lesson = commands.add_parser("lesson", help="Add, retire or judge a lesson").add_subparsers(dest="action", required=True)
    sub = lesson.add_parser("add", help="Record a new lesson with the candidates it rests on")
    sub.add_argument("text")
    sub.add_argument("--evidence", required=True, help="Comma-separated simulated candidate ids")
    sub = lesson.add_parser("retire", help="Retire a lesson the results contradict")
    sub.add_argument("lesson_id")
    sub.add_argument("--reason", required=True)
    sub = lesson.add_parser("judge", help="Record one simulation as evidence for or against a lesson")
    sub.add_argument("lesson_id")
    sub.add_argument("candidate_id")
    sub.add_argument("--verdict", required=True, choices=VERDICTS)
    sub.add_argument("--reason", required=True)
    return root


COMMANDS = {"init": cmd_init, "sim": cmd_sim, "queue": cmd_queue, "resolve": cmd_resolve, "set_aside": cmd_set_aside,
            "pipeline": cmd_pipeline, "daily_submit": cmd_daily_submit, "log": cmd_log, "status": cmd_status,
            "history": cmd_history, "patterns": cmd_patterns, "lessons": cmd_lessons, "lesson": cmd_lesson,
            "show": cmd_show}


def main(argv=None) -> int:
    args = parser().parse_args(argv)
    if args.command != "init" and not path("experiment_state.json").exists():
        print("results/ is not initialized; run `research.py init` first.", file=sys.stderr)
        return 2
    try:
        return COMMANDS[args.command](args)
    except APIError as error:
        print(str(error), file=sys.stderr)
        return 3
    except (ValueError, OSError, sqlite3.Error) as error:
        print(str(error), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
