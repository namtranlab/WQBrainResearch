# WQBrainSim

A small command-line tool for WorldQuant BRAIN. It sends alpha expressions for simulation, waits for the results, and saves every job in a local SQLite database so that interrupted work can resume. It can also look up operators and data fields, run the read-only submission check, and submit an alpha for production.

**Status**: simulation, `check` and `submitted` were tested against the live API in October 2026. `submit` has only been tested offline. Interactive login challenges are detected but not supported.

## Setup

The tool uses [uv](https://docs.astral.sh/uv/), a Python package manager. Run every command from this folder.

```sh
uv sync
uv run pytest
```

`uv sync` creates `.venv` from `uv.lock`. `uv run` runs a command inside it, so you never activate the environment.

## Credentials

Create `.env` in this folder:

```dotenv
BRAIN_USERNAME=your_account_email
BRAIN_PASSWORD=your_password
```

Then run `chmod 600 .env`. The tool reads `.env` as plain text: characters such as `$` and `#` are kept as they are. Environment variables with the same names take priority. With neither, the tool asks for them in an interactive terminal.

## Commands

All of these run with `uv run wqbrainsim <command>`. Add `--help` to any command for its options.

| Command | Contacts BRAIN | What it does |
|---|---|---|
| `run --alpha FILE --settings FILE` | Yes | Simulates one alpha and prints a short summary |
| `run --expression EXPR --settings FILE` | Yes | Same, for an expression with no description |
| `run --batch FILE --settings FILE` | Yes | Simulates a JSON list of expressions, one at a time (at most `--limit`, default 20) |
| `run ... --dry-run` | No | Prints the exact request and stops |
| `resume JOB_ID` | Yes | Continues a paused job without submitting it again |
| `list`, `show JOB_ID`, `export` | No | Reads the local database |
| `operators`, `datasets`, `datafields` | Read only | Saves the catalog to a new JSON file |
| `check ALPHA_ID` | Read only | Runs the submission checks, including `SELF_CORRELATION` |
| `submitted` | Read only | Lists alphas already submitted from the account |
| `submit ALPHA_ID --confirm` | Yes, irreversible | Submits the alpha for production |

Useful `run` and `resume` options:

- `--db PATH`: the job database (default `data/jobs.db`).
- `--account LABEL`: a non-secret label for the account. Use the same label to resume.
- `--deadline SECONDS`: the longest local wait per job (default 600).
- `--min-gap SECONDS`: the shortest time between two simulation submissions recorded in the database. The tool waits as needed.
- `--rerun`: start a new attempt even when the same expression and settings already have a job.

An alpha file holds one object:

```json
{
  "expression": "-ts_rank(fn_liab_fair_val_l1_a,252)",
  "description": {"hypothesis": "Why it should work.", "implementation": "How the expression does it."}
}
```

A settings file must contain exactly these keys: `region`, `universe`, `instrumentType`, `delay`, `decay`, `neutralization`, `truncation`, `pasteurization`, `unitHandling`, `nanHandling`, `language`, `visualization`. See `example/settings.json`.

## How jobs behave

| State | Meaning | What to do |
|---|---|---|
| `complete` | Results collected. Checks may still have failed. | Read the checks |
| `failed` | BRAIN rejected the expression or settings | Fix the input |
| `paused` | Deadline, login problem, or BRAIN at its simulation limit. Nothing was lost. | `resume JOB_ID` |
| `uncertain` | The submission may have been accepted, but the tool cannot tell | Check BRAIN before any rerun |

- The same expression, settings and account label reuse the existing job unless you pass `--rerun`.
- Simulation submissions are retried only when BRAIN answers 429 (simulation limit), because that answer means nothing was accepted.
- A 429 with `DAILY_SIMULATION_LIMIT_EXCEEDED` or `THROTTLED` pauses the job at once instead of retrying.
- `submit` treats HTTP 303 and 503 as accepted or queued, and polls for the result.
- An expired login is renewed once automatically.
- Progress is checked at most every 30 seconds, or later if BRAIN asks.
- One runner per database at a time.

Exit codes: `0` success, `1` simulation or check failed, `2` local error, `3` paused, uncertain or pending, `130` interrupted.

## Python use

```python
from core import BrainClient, JobStore, credentials, run

with BrainClient(credentials=credentials, account="main") as client, JobStore("data/jobs.db") as store:
    job = run("rank(close)", settings, client=client, store=store, min_gap=300)
```

Also exported: `resume`, `run_batch`, `list_operators`, `list_datasets`, `list_data_fields`, `save_catalog`, `check_submission`, `submit_alpha`, `list_submitted_alphas`.

## Files

- `core/`: the package.
- `tests/`: offline tests with a fake BRAIN server.
- `example/`: one sample alpha and one settings file.
- `test_run/live_test.py`: simulates the example alpha against the live API. It writes into `test_run/`.

## Author

Nam Tran (vannam.tran@ntu.edu.sg)

## License

MIT. See `LICENSE`.
