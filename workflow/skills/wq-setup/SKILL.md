---
name: wq-setup
description: "First-time setup of the WorldQuant BRAIN research root: review the seed alphas in initial_alphas/, choose default settings and an account label, agree the objective and submission policy with the user, then run research.py init. Use when results/ does not exist yet, or when the user asks to set up or re-scope alpha research. This is Step 0 (Setup) of the wq workflow. Runs in the current context because it ends in an approval gate."
argument-hint: "[account-label] [settings-file]"
---

# wq Step 0 — Setup

Read `workflow/PROTOCOL.md` first. `R` stands for `uv run --project WQBrainSim python research.py`; always type it out in full.

1. Read `initial_alphas/`. It is large, so never print it whole. Get an overview with:
   `python3 -c "import json;[print(a['alpha_id'],a['metrics']['sharpe'],a['metrics']['fitness'],a['metrics']['turnover'],a['settings']['universe'],a['expression']) for a in json.load(open('initial_alphas/alphas.json'))]"`
   Each record already has a result. Use it as the baseline instead of re-simulating.
2. Choose default settings and a stable, non-secret account label. Agree the objective: Sharpe and Fitness until both clear their limits, then fitness up to the next grade line (PROTOCOL.md §5). Also watch turnover, drawdown, margin and position counts. Record any limits the user sets.
3. Present the seeds, default settings, which settings may vary, and the submission policy. Get confirmation once, unless the user already approved setup and live research. Then run `R init --account <label> --settings <default settings file>` and do not ask again within that scope.

## Checkpoint

```
Setup complete.
Account: [label]   Default settings: [file]
Seeds: [N] records, best fitness [x]
Ready to start the research loop? Type "yes" or tell me what to change.
```

After approval, return to `wq` and start the research loop.
