---
name: wq-generate
description: "Generate new WorldQuant BRAIN alpha candidates (parent null) from unexplored catalog datasets and operators, seeds, new combinations or the literature, each built on an economic hypothesis. Use when writing the new-idea half of a research batch, when the user asks for new alphas or to explore a dataset, or when families are exhausted. Part of Step 3 (Write batch) of the wq workflow."
argument-hint: "[dataset-id]"
---

# wq — Generate new alphas

Read `workflow/PROTOCOL.md` first: §2 for catalog lookups, §3 for the candidate format. Write new ideas with `parent` null into `results/batch.json`; the orchestrator (`wq`) ranks and queues the batch.

Price/volume and basic fundamentals are heavily explored. Untried datasets give less correlated alphas.

1. Pick an unexplored dataset (`R status` shows how many are done; the log lists them). Read what each field measures and how often it updates.
2. Form an economic hypothesis first: why it should predict returns, and why the market prices it slowly.
3. Prefer fields with high coverage (below about 0.5 means a narrow book). `VECTOR` fields need `vec_avg`, `vec_sum` or similar. Low `userCount` and `alphaCount` suggest less crowding. Simulate in the same region, delay and universe as the lookup.
4. Start simple: one field with `rank`, `zscore`, `ts_zscore`, `ts_delta`, `group_neutralize` or `group_rank`.
5. Use only operators in the saved catalog. Look there for unused ones that fit (for example `ts_regression`, `ts_quantile`, `hump`, `bucket`, `winsorize`).
6. After two or three distinct hypotheses on a dataset show no edge, move on. Record it with `R log dataset_explored --data '{"dataset": "<id>", "fields": [...], "outcome": "..."}'`.

Choose each candidate's universe on purpose (see "Settings research" in `wq-improve`). The same data fields in a new universe start a new family, so an idea tried in another universe is written with `parent` null.
