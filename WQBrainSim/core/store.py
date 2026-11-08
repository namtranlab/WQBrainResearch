from contextlib import contextmanager
import csv
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import uuid

from .models import Job, METRICS, utcnow


class JobStore:
    def __init__(self, path: str = "data/jobs.db"):
        self.path = Path(path).resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path)
        self.db.execute("CREATE TABLE IF NOT EXISTS jobs (id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, data TEXT NOT NULL)")
        self.db.execute("CREATE INDEX IF NOT EXISTS jobs_fingerprint ON jobs(fingerprint)")
        self.db.commit()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.db.close()

    @contextmanager
    def lock(self):
        # OS locks are released after crashes; the empty lock file may remain.
        with open(str(self.path) + ".lock", "a+b") as handle:
            try:
                if os.name == "nt":
                    import msvcrt
                    handle.write(b"\0")
                    handle.flush()
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                raise ValueError("Another runner is using this database.") from None
            try:
                yield
            finally:
                if os.name == "nt":
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(handle, fcntl.LOCK_UN)

    def save(self, job: Job) -> Job:
        job.updated_at = utcnow()
        with self.db:
            self.db.execute("INSERT INTO jobs VALUES (?, ?, ?) ON CONFLICT(id) DO UPDATE SET data=excluded.data",
                            (job.id, job.fingerprint, json.dumps(job.to_dict(), allow_nan=False)))
        return job

    def get(self, job_id: str) -> Job:
        row = self.db.execute("SELECT data FROM jobs WHERE id=?", (job_id,)).fetchone()
        if row is None:
            raise ValueError("Job not found.")
        return Job(**json.loads(row[0]))

    def all(self) -> list[Job]:
        return [Job(**json.loads(row[0])) for row in self.db.execute("SELECT data FROM jobs ORDER BY rowid")]

    def last_attempt(self) -> float | None:
        (value,) = self.db.execute("SELECT MAX(json_extract(data, '$.attempted_at')) FROM jobs").fetchone()
        return datetime.fromisoformat(value).timestamp() if value else None

    def prepare(self, expression: str, settings: dict, origin: str, account: str, rerun: bool) -> Job:
        canonical = json.dumps([expression, settings, origin, account, 1], sort_keys=True, allow_nan=False)
        fingerprint = hashlib.sha256(canonical.encode()).hexdigest()
        row = self.db.execute("SELECT data FROM jobs WHERE fingerprint=? ORDER BY rowid DESC LIMIT 1",
                              (fingerprint,)).fetchone()
        if row and not rerun:
            return Job(**json.loads(row[0]))
        return self.save(Job(str(uuid.uuid4()), fingerprint, expression, dict(settings), origin, account))

    def export(self, output: str, format: str) -> None:
        if format not in ("json", "csv"):
            raise ValueError("Export format must be json or csv.")
        if Path(output).resolve() in (self.path, Path(str(self.path) + ".lock")):
            raise ValueError("Export cannot overwrite the database or its lock.")
        jobs = self.all()
        with open(output, "x", encoding="utf-8", newline="") as handle:
            if format == "json":
                json.dump([j.to_dict() for j in jobs], handle, indent=2, allow_nan=False)
            else:
                fields = ["id", "expression", "description", "settings", "state", "alpha_id", "grade", "created_at",
                          "submitted_at", "completed_at", "checks", *METRICS]
                writer = csv.DictWriter(handle, fieldnames=fields)
                writer.writeheader()
                for job in jobs:
                    data = job.to_dict() | job.metrics
                    row = {}
                    for key in fields:
                        value = data[key]
                        if isinstance(value, (dict, list)):
                            value = json.dumps(value, allow_nan=False)
                        if isinstance(value, str) and value.lstrip().startswith(("=", "+", "-", "@")):
                            value = "'" + value
                        row[key] = value
                    writer.writerow(row)
