---
name: wq
description: "Iterative WorldQuant BRAIN alpha research through research.py, end to end: set up the research root, generate new alphas, improve promising ones through settings and expressions, keep a persistent experiment history and lessons, and submit only alphas that clear the enforced submission pipeline. Use when the user wants to run or resume alpha research, write the next batch, says \"run the research loop\" or \"auto quant research\", or wants the whole loop rather than one step. Coordinates wq-setup, wq-generate, wq-improve and wq-submit."
argument-hint: "[setup | resume]"
---

# wq — Auto Quant Research

```
0.  Setup        → wq-setup      → CHECKPOINT: seeds, settings, policy approved once
1.  Read state   → this context  → status, lessons, patterns, history
2.  Lessons      → this context  → judge, retire, add
3.  Write batch  → wq-generate + wq-improve → results/batch.json
4.  Queue        → this context  → R queue, read the summary
5.  Submit       → wq-submit     → only on DAILY_SUBMIT_DUE
6.  Repeat from 1
```

Generate new alphas and improve those that show potential until they pass or are clearly exhausted. Favor simple, economically interpretable ideas. Settings are a first-class lever: the same expression can behave very differently under another universe, neutralization, decay, delay or truncation, and a settings change is often the cheaper fix.

This skill authorizes simulation research after setup is approved, and production submission through `research.py daily_submit`.

**Read `workflow/PROTOCOL.md` first.** It holds the rules the code enforces, the commands, the file formats and the grade lines. They are not repeated here. `R` stands for `uv run --project WQBrainSim python research.py`; always type it out in full.

## Step skills

| Skill | File | Use it for |
|---|---|---|
| `wq-setup` | `workflow/skills/wq-setup/SKILL.md` | First run, before `results/experiment_state.json` exists |
| `wq-generate` | `workflow/skills/wq-generate/SKILL.md` | New ideas (`parent` null) for a batch |
| `wq-improve` | `workflow/skills/wq-improve/SKILL.md` | Variants of alphas with potential: settings, expression, correlation fixes |
| `wq-submit` | `workflow/skills/wq-submit/SKILL.md` | `DAILY_SUBMIT_DUE`, and the report for each submitted alpha |

Load a step skill with the Skill tool, or read its file when skills are unavailable (as under `autorun.sh`). Steps run in this context: none forks.

## Step 0: Setup

If `results/experiment_state.json` does not exist, run `wq-setup` and stop at its checkpoint. Otherwise go to the research loop.

## Research loop

1. Run `R status`, `R lessons`, `R patterns` and `R history`. Build on the best near misses, and do not repeat ideas or errors already in the history. Use `R show` to look closer at a simulation before acting on it.
2. Lessons are claims and can be wrong. Retire or judge each lesson the results contradict. Add a lesson only for a pattern seen in at least 2 simulations, written as specific, testable advice. Make 2 or 3 candidates of each batch tests of untested or doubtful lessons.
3. Choose between:
   - **Generate** (`wq-generate`): a new alpha, mainly from catalog datasets and operators, also from seeds, new combinations or the literature.
   - **Improve** (`wq-improve`): an alpha with potential (passes or nearly passes Sharpe or Fitness, a plausible mechanism, or fails only one or two checks), with settings and expression changes, until it passes, then toward the next grade line, or until ideas run out.
   Start at least one candidate of each batch on a dataset not used before; otherwise `queue` holds back the batch's variants. At least half of each batch must be new ideas, unless the variants improve a PROMISING family, and variants spread over several families; `queue` enforces both and holds extra variants for later batches. Do not spend simulations on ideas with no sign of an edge.
4. Write `results/batch.json` with 10 to 15 candidates ranked by expected edge, best first, each with a one-line `reason`. Rank the candidates still waiting from earlier batches among them, or leave them out to drop them. Then run `R queue` once. Only about 15 simulations run per batch, so spend them on the best ideas and skip cosmetic variants of tested candidates.
5. Read only the queue's printed summary: one line per candidate, with its pipeline status when it passed. Compare each result with its parent, and say whether the settings match.
6. If the queue stopped with `DAILY_SUBMIT_DUE`, run `wq-submit`, then continue. For any other stop reason, follow the printed instruction.
7. A `correlated` or `below_grade` alpha passed every simulation check, so it has an edge: improve it as a parent with `wq-improve`. Also improve the pooled alphas `R history` lists near the next grade line, so a better version is ready before the GOOD one is submitted. Then write the next batch.

Robustness: one backtest is not proof. Prefer results that hold across neighbouring settings. Report how many variants were tried. Repeated tuning overfits.

## Continuation

Once approved, keep going without asking until the user stops you or a command reports a blocking condition you cannot clear. Every command saves its state, so a stop at any moment loses nothing: the next session starts with `R status`, and `R sim` on a paused job's candidate resumes it. You cannot see the account's usage window. Never claim an inactive session will wake itself. A user stop overrides everything.
