# Protocol

Contracts every `wq` skill shares. The skills point here instead of repeating them.

All paths are relative to the research root, the folder holding `research.py`. Run every command from there.

## 1. What the code enforces

`research.py` checks every hard rule itself. Do all simulation and submission work through it; never call WQBrainSim's `run`, `resume` or `submit` directly, or import `core` in your own scripts.

| Rule | Enforced by |
|---|---|
| One simulation runs at a time, with a random 4 to 7 minutes between submissions and at most 15 submissions in any hour | `queue`, `sim` |
| `queue` simulates at most 15 candidates per run, best-ranked first; the rest wait for the next batch, which must rank them again or they are set aside | `queue` |
| After BRAIN reports its daily simulation limit at a count above 20 and above every earlier day, research stops 5 simulations short of that count on later days | `queue`, `sim` |
| Every candidate has a parent, a description and a stated change | `sim` rejects the candidate file |
| No new simulation while another job is paused or uncertain | `sim` |
| A simulation with no result 30 minutes after submission is cancelled on BRAIN and retried once, 6 hours later | `sim`, `queue` |
| A simulation whose submission is unconfirmed is resolved automatically after 30 minutes, and its candidate is retried once; no retry starts while the candidate has an unresolved job | `sim`, `queue` |
| No retry of an unchanged input; a rerun needs a recorded reason. A new candidate with the same expression and settings as one that has a result is set aside, naming that one | `sim --rerun REASON`, `queue` |
| At least half of each batch is new ideas (`parent` null), except that variants of a PROMISING family need none beside them; at most 3 variants of one family per batch. A PROMISING family reached fitness 0.8 or Sharpe 1.0 in either direction and has no submitted alpha. Extra variants wait for up to 2 later batches, then are set aside | `queue` |
| A failed simulation records BRAIN's reason, so the history shows why it failed | `sim`, `queue` |
| A candidate with exactly the same data fields and universe as an earlier one is in that one's family, whatever its `parent` says; the same fields in a new universe start a new family | `queue` |
| A family with 5 simulations in a row that do not raise its best fitness by at least 0.05 is exhausted and takes no more variants | `queue` |
| A batch with no candidate that uses a data field never simulated before runs only its new ideas; its variants, even of a PROMISING family, wait for a later batch | `queue` |
| A lesson cites simulated candidates; at most 20 are active; one contradicted at least twice and more often than supported is retired | `lesson`, `lessons` |
| A candidate that tests a lesson has a parent, and its result is judged against the parent's fitness | `sim`, `lessons` |
| A candidate is successful only when complete and every check passes except a pending `SELF_CORRELATION` | `sim` |
| Only alphas graded GOOD or better are pooled or submitted; a lower or ungraded one is marked `below_grade` before any BRAIN call | `pipeline`, `daily_submit` |
| Duplicate check against freshly listed submitted alphas, then the read-only check, before pooling | `pipeline` |
| Daily submission only when 10 alphas were pooled today or after 23:00 US Eastern | `daily_submit` |
| Ranking by grade, fitness, Sharpe, then lower turnover; re-check before each submission; at most 3 per day | `daily_submit` |
| An EXCELLENT or better alpha is submitted at the next daily submission. A GOOD one waits in the pool until its family is exhausted or it has been 2 days in the pool | `daily_submit` |
| An uncertain submission stops the day and that alpha is never submitted again; a 403 after BRAIN accepted the submission counts as uncertain | `daily_submit` |
| One daily submission per day: `daily_submit` refuses a second run on the same day, and a run started meanwhile waits for the first | `daily_submit` |
| Research continues after the daily submission; alphas pooled later wait for the next day's submission | `sim` |
| No simulations for the rest of the day after BRAIN reports its daily simulation limit | `sim` |
| Unattended Claude cannot read `.env` files or edit the code, the docs, or the state files in `results/` | `autorun.sh` |
| A refused submission (429) stops the day but keeps the alpha in the pool | `daily_submit` |
| Runs, results, checks, submissions, the log and the checkpoint are written atomically | all commands |

## 2. Commands

`R` below stands for `uv run --project WQBrainSim python research.py`; always type it out in full.

| Task | Command |
|---|---|
| First-time setup | `R init --account <label> --settings <default settings file>` |
| Where things stand | `R status` |
| Best alpha of each family, building blocks to combine, submitted alphas, `correlated` and `below_grade` alphas to improve, pooled alphas near the next grade line, latest errors, explored datasets | `R history` |
| Effect of each kind of change on fitness, results by dataset and by universe, failing checks and what cleared them | `R patterns` |
| Lessons with their test record, then retired ones | `R lessons` |
| Everything recorded about one candidate: attempts, all metrics, checks with values and limits, parent, children, lessons | `R show <candidate_id>` |
| Record a lesson, citing the simulations it rests on | `R lesson add "<advice>" --evidence <id>,<id>` |
| Retire a lesson the results contradict | `R lesson retire <lesson_id> --reason "<why>"` |
| Record one simulation as evidence for or against a lesson | `R lesson judge <lesson_id> <candidate_id> --verdict supports\|contradicts --reason "<why>"` |
| Simulate every waiting candidate, then run `pipeline` on each pass | `R queue` |
| Simulate one candidate (also resumes its paused job) | `R sim <candidate_id>` |
| Check a candidate without network | `R sim <candidate_id> --dry-run` |
| Start a new attempt on purpose | `R sim <candidate_id> --rerun "<reason>"` |
| Move a waiting candidate out of the queue (not one with an unfinished simulation) | `R set_aside <candidate_id> --reason "<why>"` |
| After reconciling an uncertain job in BRAIN | `R resolve <job_id> --note "<what you found>"` |
| Send a successful alpha through the checks | `R pipeline <alpha_id>` |
| End-of-day submission | `R daily_submit` |
| Record a research event | `R log <event> --data '<json object>'` (use `dataset_explored` with a `dataset` key for each explored dataset) |

Catalog lookups use WQBrainSim directly (read-only): `uv run --project WQBrainSim wqbrainsim operators|datasets|datafields ... --output results/catalog/<name>.json`. Reuse the saved files; `autorun.sh` fetches operators and datasets again when they are more than a week old. List datasets before fields, and narrow fields by dataset or search term.

## 3. Files

- A batch is `results/batch.json`: a JSON list ordered by expected edge, best first. Each entry is a candidate plus `id` and `reason` (one line on why it should have an edge). To keep a waiting candidate, list only its `id` and a new `reason`. `queue` splits the batch into candidate files, sets aside every waiting candidate the batch leaves out, and archives the file in `results/batches/`.
- A candidate is `results/candidates/<candidate_id>.json`: `expression`, `description` (`hypothesis`, `implementation`), `parent` (candidate id or `null`), `change` (what changed and why), optional `settings` with only the keys that differ from `results/settings.json`, and optional `tests` (`{"lesson": "<lesson id>", "expect": "better" or "not_better"}`) when it tests a lesson against its parent. One candidate id is simulated once; for any change, write a new candidate with a new id.
- Submission reports are `results/submission_reports/<alpha_id>.md`, shaped from `workflow/templates/submission-report.md`.

## 4. Running commands

- Run `queue` and `sim` as background commands and wait for the completion notice. Do not poll or `sleep`. Each simulation waits 4 to 7 minutes after the previous submission, so a full run takes over an hour.
- Every command prints a few lines. That is enough to decide; open saved files only when you need more.
- Exit codes: `0` done, `1` failed, `2` input or local error, `3` blocked, paused, uncertain or pending. Read the printed reason.
- Never read or print `WQBrainSim/.env`. Never edit `WQBrainSim/`, `research.py`, or the files the commands write.
- Pause and tell the user when login needs an interactive challenge, access is denied, or the same error repeats.

## 5. Grades and metrics

- BRAIN grades a delay-1 alpha by fitness: GOOD above 1.5, EXCELLENT above 2.0, SPECTACULAR above 2.5.
- Fitness = Sharpe × √(|returns| ÷ max(turnover, 0.125)).
- `SELF_CORRELATION` limit is 0.7.
