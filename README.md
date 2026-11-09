# WQAutoResearch

An LLM research agent that searches for alphas on [WorldQuant BRAIN](https://platform.worldquantbrain.com/) in a loop: generate ideas, backtest them, learn from the results, and submit the best ones.

> [!WARNING]
> **This is an unofficial, personal project. It is not affiliated with, endorsed by, or supported by WorldQuant.**
> Use it **at your own risk**. Read the [Disclaimer](#disclaimer) before running it.

## What it does

```
  ┌─▶ 1. Check the notebook     What did we try? What worked? What failed?
  │   2. Update the lessons     Which beliefs still hold?
  │   3. Generate / polish      Half new ideas, half fixes of "almost good" ones
  │   4. Test                   Backtest each idea on BRAIN
  │   5. Submit the best        Once a day, only alphas that pass every check
  └──────┘
```

The project has three layers:

| Layer | Path | Role |
|---|---|---|
| **Tools** | `WQBrainSim/` | Command-line client for BRAIN: simulate, check, look up the catalog, submit. Every job is saved in SQLite and can be resumed. |
| **Rulebook** | `research.py` | Enforces the hard rules in code: rate limits, batch diversity, no reruns, exhausted families, grade gate, daily submission cap. |
| **Skills** | `workflow/` | Claude Code skills for the judgment: `wq`, `wq-setup`, `wq-generate`, `wq-improve`, `wq-submit`, plus a shared `PROTOCOL.md`. |

Seed alphas live in `initial_alphas/`, and tests in `tests/`.

## Requirements

- Python 3.11+ and [uv](https://docs.astral.sh/uv/)
- [Claude Code](https://docs.claude.com/en/docs/claude-code/overview)
- A WorldQuant BRAIN account

## Quick start

```bash
# 1. BRAIN login (read only by WQBrainSim, never by the agent)
printf 'BRAIN_USERNAME=you@example.com\nBRAIN_PASSWORD=your_password\n' > WQBrainSim/.env
chmod 600 WQBrainSim/.env
(cd WQBrainSim && uv sync)

# 2. Guard rails for the agent
mkdir -p .claude && cp claude-settings.proposed.json .claude/settings.json

# 3. Start Claude Code
claude
```

Inside Claude Code:

```
/plugin marketplace add ./workflow
/plugin install wq@wq
/wq
```

The first `/wq` runs setup and stops for your approval. After that, it loops on its own. Type `/wq` again at any time to resume.

For the unattended mode, see [`HOWTO.md`](HOWTO.md). For the full story, see [`https://nvtran.com/blog/2026/can-an-ai-do-research/`](https://nvtran.com/blog/2026/can-an-ai-do-research/).

## Safety defaults

- One simulation at a time, 4 to 7 minutes apart, at most 15 per hour.
- Only alphas graded GOOD or better are submitted, and at most 3 per day.
- The agent cannot read `.env`, edit the code or result records, or call `run`, `resume` or `submit` directly. Everything goes through `research.py`.

These defaults reduce risk. They do not remove it.

## Disclaimer

- **Not official.** This project is an independent, personal tool. It is not a product of WorldQuant, and WorldQuant does not review, endorse or support it. "WorldQuant" and "BRAIN" are names of their respective owners and are used here only to describe compatibility.
- **Your account, your responsibility.** You are responsible for following the BRAIN Terms of Use and any consultant agreement you have signed, including any rules on automated access, rate limits and submissions. Check these rules before running the tool. The author accepts no liability for account restrictions, lost points, or any other consequence.
- **Submissions are irreversible.** The agent can submit alphas to your account. Once submitted, they cannot be undone.
- **No guarantee.** The software is provided "as is", without warranty of any kind (see [`LICENSE`](LICENSE)). It may contain bugs, and the BRAIN platform may change without notice.
- **Not financial advice.** Backtest results do not predict future performance. Nothing in this repository is investment advice.
- **Protect your credentials.** Keep `WQBrainSim/.env` private, and never commit it.

## Author

Nam Tran (vannam.tran@ntu.edu.sg)

## License

MIT. See [`LICENSE`](LICENSE).
