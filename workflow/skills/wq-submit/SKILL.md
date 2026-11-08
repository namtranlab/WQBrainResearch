---
name: wq-submit
description: "Run the end-of-day WorldQuant BRAIN submission through research.py daily_submit, then write a self-contained report for every submitted alpha to results/submission_reports/<alpha_id>.md. Use when the queue stops with DAILY_SUBMIT_DUE, when the user asks to submit the pool, or to write or redo the report for a submitted alpha. This is Step 5 (Submit) of the wq workflow. Submissions cannot be undone."
argument-hint: "[report <alpha_id>]"
---

# wq Step 5 — Submit

Read `workflow/PROTOCOL.md` first: §1 lists the submission rules `daily_submit` enforces. `R` stands for `uv run --project WQBrainSim python research.py`; always type it out in full.

1. Run `R daily_submit`. It decides what to submit and when; never submit any other way.
2. For each `SUBMITTED` line it prints, write the report below.
3. On `UNCERTAIN SUBMISSION` or `SUBMISSION REFUSED`, tell the user and follow the printed instruction.
4. Return to `wq` and continue the research loop.

With `report <alpha_id>` as the argument, skip to the report for that alpha.

## Report for submission

Write `results/submission_reports/<alpha_id>.md` for every submitted alpha, shaped from `workflow/templates/submission-report.md`, so a reader understands it without other context. Take every value from the submission line and the alpha's record in `successful_alphas.json`; never retype or guess one.

1. **Header**: alpha ID and grade as the title. Then the submission date (US Eastern), Sharpe, fitness, returns, turnover, drawdown, self-correlation, and the settings (universe, delay, neutralization, decay, truncation, nanHandling).
2. **Alpha**: the exact submitted expression in a code block.
3. **Breakdown**: the same expression split into legs in a code block, one leg per line, labelled `L1`, `L2` and so on, each with a short comment on what it measures. Group legs that share a hypothesis under a comment line that names it, its direction (FAVOR or FADE) and its share of the legs. Show a wrapper that applies to every leg once, at the end. Never change a leg.
4. **Explanation**: one short plain-language paragraph per group: what it measures, which stocks it favors, and why that should predict returns. Define each term at first use, for example implied volatility.
