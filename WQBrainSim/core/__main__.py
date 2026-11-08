import argparse
import json
import os
import sqlite3
import sys

from . import BrainClient, JobStore, credentials, resume, run, run_batch
from .catalog import list_data_fields, list_datasets, list_operators, list_settings, save_catalog
from .client import APIError
from .models import payload, validate_alpha, validate_batch
from .submission import check_submission, list_submitted_alphas, save_result, submit_alpha

CATALOG = ("operators", "settings", "datasets", "datafields")
SUBMISSION = ("check", "submit")


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description="Simulate user-provided alphas and save BRAIN results.")
    commands = root.add_subparsers(dest="command", required=True)
    for name in ("run", "resume", "list", "show", "export"):
        sub = commands.add_parser(name)
        sub.add_argument("--db", default="data/jobs.db", help="Local SQLite database (default: data/jobs.db)")
        if name in ("run", "resume"):
            sub.add_argument("--account", default="default", help="Nonsecret account label used for deduplication")
            sub.add_argument("--deadline", type=float, default=600, help="Maximum local wait seconds per job")
            sub.add_argument("--min-gap", type=float, default=0,
                             help="Minimum seconds between simulation submissions recorded in this database")
        if name == "run":
            source = sub.add_mutually_exclusive_group(required=True)
            source.add_argument("--expression")
            source.add_argument("--alpha", help="JSON object with one expression and its description")
            source.add_argument("--batch", help="JSON array of expressions")
            sub.add_argument("--settings", required=True, help="Explicit simulation settings JSON")
            sub.add_argument("--limit", type=int, default=20)
            sub.add_argument("--dry-run", action="store_true", help="Print the request and stop; no network")
            sub.add_argument("--rerun", action="store_true")
        if name in ("resume", "show"):
            sub.add_argument("job_id")
        if name == "export":
            sub.add_argument("--format", choices=("json", "csv"), required=True)
            sub.add_argument("--output", required=True, help="New output file (must not exist)")
    for name in CATALOG:
        sub = commands.add_parser(name, help="Read-only catalog lookup")
        sub.add_argument("--output", required=True, help="New JSON file (must not exist)")
        sub.add_argument("--deadline", type=float, default=600, help="Maximum local wait seconds")
        if name not in ("operators", "settings"):
            sub.add_argument("--region", required=True)
            sub.add_argument("--delay", type=int, required=True)
            sub.add_argument("--universe", required=True)
            sub.add_argument("--instrument-type", default="EQUITY")
            sub.add_argument("--max-items", type=int, default=500 if name == "datasets" else 1000)
        if name == "datafields":
            sub.add_argument("--dataset", help="Only fields from this dataset id")
            sub.add_argument("--search", help="Only fields matching this search term")
    for name in SUBMISSION:
        sub = commands.add_parser(name, help="Read-only submission check" if name == "check"
                                  else "Irreversible production submission")
        sub.add_argument("alpha_id")
        sub.add_argument("--output", help="New JSON file for the result (must not exist)")
        sub.add_argument("--deadline", type=float, default=600, help="Maximum local wait seconds")
        if name == "submit":
            sub.add_argument("--confirm", action="store_true", help="Required: production submission cannot be undone")
    sub = commands.add_parser("submitted", help="List alphas already submitted from this account (read-only)")
    sub.add_argument("--output", help="New JSON file for the listing (must not exist)")
    sub.add_argument("--max-items", type=int, default=1000)
    sub.add_argument("--deadline", type=float, default=300, help="Maximum local wait seconds")
    return root


def submitted(args) -> int:
    with BrainClient(credentials=credentials) as client:
        result = list_submitted_alphas(client, max_items=args.max_items, deadline=args.deadline)
    for alpha in result["results"]:
        m = alpha["metrics"]
        print(f"{alpha['alpha_id']}  grade={alpha['grade'] or '-'}  {alpha['settings'].get('universe', '-')}  "
              f"sharpe={m.get('sharpe', '-')} fitness={m.get('fitness', '-')} selfCorr={m.get('selfCorrelation', '-')}"
              f"  {alpha['expression']}")
    note = " (truncated; raise --max-items)" if result["truncated"] else ""
    print(f"{result['fetched']} submitted alphas{note}")
    if args.output:
        save_result(args.output, result)
    return 0


def submission(args) -> int:
    if args.command == "submit" and not args.confirm:
        raise ValueError("Submission is irreversible; add --confirm to proceed.")
    with BrainClient(credentials=credentials) as client:
        if args.command == "check":
            result = check_submission(client, args.alpha_id, deadline=args.deadline)
        else:
            result = submit_alpha(client, args.alpha_id, confirm=True, deadline=args.deadline)
    print_checks(result["checks"])
    print(f"{result['alpha_id']}  {result['action']}: {result['status']}")
    if args.output:
        save_result(args.output, result)
    return 0 if result["status"] in ("pass", "submitted") else (3 if result["status"] == "pending" else 1)


def catalog(args) -> int:
    with BrainClient(credentials=credentials) as client:
        if args.command == "operators":
            result = list_operators(client, deadline=args.deadline)
        elif args.command == "settings":
            result = list_settings(client, deadline=args.deadline)
        else:
            scope = dict(region=args.region, delay=args.delay, universe=args.universe,
                         instrument_type=args.instrument_type, max_items=args.max_items,
                         deadline=args.deadline)
            result = (list_datasets(client, **scope) if args.command == "datasets"
                      else list_data_fields(client, dataset=args.dataset, search=args.search, **scope))
    save_catalog(args.output, result)
    note = " (truncated; raise --max-items or narrow the query)" if result.get("truncated") else ""
    print(f"Saved {len(result['results'])} {args.command} to {args.output}{note}")
    return 0


def remote(args) -> int:
    if args.output and os.path.exists(args.output):
        print("Output file already exists; choose a new path.", file=sys.stderr)
        return 2
    try:
        if args.command == "submitted":
            return submitted(args)
        return catalog(args) if args.command in CATALOG else submission(args)
    except APIError as error:
        print(str(error), file=sys.stderr)
        return 3


def read_json(path: str):
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def print_checks(checks: list[dict]) -> None:
    notable = [f"{c.get('result', '?')}:{c.get('name', '?')}({c.get('value', '-')}/{c.get('limit', '-')})"
               for c in checks if c.get("result") != "PASS"]
    print(f"  checks: {len(checks) - len(notable)} PASS" + ("  " + " ".join(notable) if notable else ""))


def summary(job) -> None:
    print(f"{job.id}  {job.state}  alpha={job.alpha_id or '-'}  grade={job.grade or '-'}")
    print("  " + "  ".join(f"{k}={'-' if v is None else v}" for k, v in job.metrics.items()))
    if job.checks:
        print_checks(job.checks)
    if job.error:
        print("  " + job.error)


def load_inputs(args) -> tuple[dict, dict | None, list]:
    settings = read_json(args.settings)
    alpha = read_json(args.alpha) if args.alpha else None
    if alpha is not None:
        validate_alpha(alpha, settings)
        expressions = [alpha["expression"]]
    else:
        expressions = read_json(args.batch) if args.batch else [args.expression]
    validate_batch(expressions, settings, args.limit)
    return settings, alpha, expressions


def simulate(args, store) -> list:
    settings, alpha, expressions = load_inputs(args)
    options = dict(deadline=args.deadline, rerun=args.rerun, min_gap=args.min_gap)
    with BrainClient(credentials=credentials, account=args.account) as client:
        if alpha is not None:
            return [run(alpha["expression"], settings, description=alpha["description"],
                        client=client, store=store, **options)]
        jobs = run_batch(expressions, settings, client=client, store=store, limit=args.limit, **options)
    if len(jobs) < len(expressions):
        print(f"Stopped after {len(jobs)} of {len(expressions)} inputs; rerun the same batch to continue.")
    return jobs


def dry_run(args) -> int:
    settings, _, expressions = load_inputs(args)
    for expression in expressions:
        print(json.dumps(payload(expression, settings), indent=2, allow_nan=False))
    return 0


def local(args) -> int:
    if args.command == "run" and args.dry_run:
        return dry_run(args)
    with JobStore(args.db) as store:
        if args.command == "list":
            for job in store.all():
                summary(job)
            return 0
        if args.command == "show":
            print(json.dumps(store.get(args.job_id).to_dict(), indent=2))
            return 0
        if args.command == "export":
            store.export(args.output, args.format)
            print(f"Exported to {args.output}")
            return 0
        if args.command == "run":
            jobs = simulate(args, store)
        else:
            with BrainClient(credentials=credentials, account=args.account) as client:
                jobs = [resume(args.job_id, client=client, store=store, deadline=args.deadline,
                               min_gap=args.min_gap)]
    for job in jobs:
        summary(job)
    if any(job.state in ("paused", "uncertain") for job in jobs):
        return 3
    return 1 if any(job.state == "failed" for job in jobs) else 0


def main(argv=None) -> int:
    args = parser().parse_args(argv)
    try:
        if args.command in (*CATALOG, *SUBMISSION, "submitted"):
            return remote(args)
        return local(args)
    except KeyboardInterrupt:
        print("Stopped locally. Use list and resume to continue saved jobs.", file=sys.stderr)
        return 130
    except (ValueError, OSError, sqlite3.Error) as error:
        # Never echo arbitrary file contents, SQL values, or credential callbacks.
        message = str(error) if type(error) is ValueError else "Could not read/write local files or database. Check paths, JSON syntax, and permissions."
        print(message, file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
