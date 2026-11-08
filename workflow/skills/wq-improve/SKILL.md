---
name: wq-improve
description: "Improve WorldQuant BRAIN alphas that show potential — near misses, PROMISING families, correlated or below_grade alphas, pooled alphas near the next grade line — through settings research, the improvement playbook (fitness, Sharpe, returns, turnover, correlation, reversed edges, combining legs, units, sparse data) and correlation fixes. Use when writing the variant half of a research batch, when the user asks to improve, tune or fix an alpha, or when pipeline reports an alpha as correlated. Part of Step 3 (Write batch) of the wq workflow."
argument-hint: "[candidate_id]"
---

# wq — Improve an alpha

Read `workflow/PROTOCOL.md` first: §3 for the candidate format, §5 for the fitness formula and grade lines. Each improvement is a new candidate with the improved one as `parent`, written into `results/batch.json`; the orchestrator (`wq`) ranks and queues the batch. Use `R show <candidate_id>` to see a parent's metrics and checks before changing it.

**What to improve**: an alpha that passes or nearly passes Sharpe or Fitness, has a plausible mechanism, or fails only one or two checks; a `correlated` or `below_grade` alpha (it passed every simulation check, so it has an edge); and pooled alphas `R history` lists near the next grade line. Improve until it passes, then toward the next grade line, or until ideas run out.

## Settings research

- Change one setting at a time when diagnosing, and tie each change to a mechanism: a churning reversal signal should benefit from decay; a sector effect may need SUBINDUSTRY; a liquidity effect may be stronger in a smaller universe.
- No blind grid searches. Prefer values that work across neighbours (decay 3, 5 and 8 all improve, not only 5).

| Setting | Effect |
|---|---|
| `decay` | Averages over N days. Higher lowers turnover; lower raises turnover and often returns. |
| `universe` | Choose it per candidate from `results/catalog/settings_options.json`. Smaller and more liquid (TOP200, TOP500, TOPSP500) raises turnover and returns; larger (TOP1000, TOP2000, TOP3000) gives breadth, which combinations of slow signals usually need. Moving a tuned alpha to another universe unchanged tests transfer, not the universe. |
| `neutralization` | NONE, MARKET, SECTOR, INDUSTRY, SUBINDUSTRY. Finer removes more common risk. |
| `truncation` | Maximum weight per stock. Lower limits concentration. |
| `delay` | 0 uses same-day data; often stronger but harder. Delay-1 robustness matters. |

## Improvement playbook

- **Fitness**: fitness = Sharpe × √(|returns| ÷ max(turnover, 0.125)), and it sets the grade. Raise Sharpe or returns. Lowering turnover helps only down to 12.5%.
- **Sharpe**: raise returns, or cut volatility with neutralization and `group_neutralize` or `group_rank`. Do not just tune parameters; changes must make economic sense.
- **Returns**: higher turnover, lower decay, smaller liquid universes, higher volatility at a similar returns-to-drawdown ratio. News and analyst data often help.
- **Turnover**: keep below 40% where possible and always below 70%. Lower it with `decay`, `rank()`, `trade_when`, or `hump`. Raise it with lower decay, smaller universes, shorter lookbacks, or frequently updated data.
- **Correlation**: swap to equivalent fields (`high`, `low`, `open` for `close`), similar operators (median for mean, `zscore` for `rank`), or different groupings. Never invent a group only to cut correlation.
- **Reversed edge**: a result marked `REVERSED` works the other way. Build variants on the negated expression. Simulate a plain negation only if its flipped numbers would clear the limits; otherwise its result is already known.
- **Combining**: strong alphas usually add 3 to 7 weak, unrelated legs from different datasets, taken from the building blocks in `R history` (negate REVERSED ones). Write each leg as `group_rank(<signal>, bucket(rank(ts_sum(returns, N)), range="0,1,0.1"))`, which compares stocks only with others of similar recent returns, and multiply the sum by `(1 + rank(adv20))` to lean toward liquid stocks. Never reuse a leg of a submitted alpha.
- **Units**: put each field on a common scale with `rank` or `zscore` before combining fields. `WARNING:UNITS` means units were mixed.
- **Sparse data** (options, social media, some fundamentals): fill gaps with `ts_backfill`, or rank within groups with `group_rank`, before tightening truncation. This is the usual fix for `CONCENTRATED_WEIGHT`.

## Fixing a correlated alpha

`pipeline` prints the self-correlation value and any submitted alphas it overlaps with. Try the small correlation edits in the playbook first. If they are not enough: replace the shared leg and keep the new part, change the mechanism (dataset, horizon, holding period) rather than settings, or make the signal orthogonal to the submitted alpha. Run each fix as a new candidate with this one as `parent`. Stop when ideas run out, and return to generating. Settings variants and blends that share a leg with a submitted alpha usually fail `SELF_CORRELATION` (limit 0.7), so prefer new data and new mechanisms.
