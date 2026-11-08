# Can an AI Do Research While I Sleep?

*A story about teaching an LLM agent to hunt for trading signals, and what it taught me about automating research.*

---

## 1. It started with a question

Lately I have been curious about one thing: **can a large language model do research on its own?**

I don't mean answering a question or writing a function when asked. I mean the whole loop a researcher goes through every day:

> have an idea → test it → look at the result → learn something → have a better idea → repeat

People call this *autoresearch*. It sounds simple, but it asks a lot from the AI. It has to remember what it already tried, not fool itself with lucky results, know when to give up on a dead end, and keep going for hours without someone holding its hand.

So I wanted a playground where I could try this for real, with real feedback, real limits, and real consequences.

## 2. Why trading signals?

I picked a fun one: **finding alphas on WorldQuant BRAIN.**

An *alpha* is a small formula that tries to predict which stocks will do better than others tomorrow. Something like:

```
rank(close - open) / (high - low)
```

Read it as: "stocks that closed near the top of their daily range might keep going up."

BRAIN lets you write such a formula, runs a backtest over years of market data, and gives you back a score card: Sharpe ratio, returns, turnover, a *fitness* score and a list of pass/fail checks.

Why is this a good playground for an LLM?

- **LLMs are very good at spotting patterns.** Alpha research is basically pattern hunting: "this kind of signal works in that kind of universe with these settings."
- **The feedback is clear and numeric.** Every idea gets a score. No arguing about whether it "worked."
- **The search space is huge.** Thousands of data fields, hundreds of operators, many settings. Too much for a human to explore by hand, which is exactly where an agent could help.
- **Mistakes are cheap, but not free.** Simulations are rate-limited, and submissions cannot be undone. So the agent must be careful, not just fast.

It felt like the perfect small world to test a big idea.

## 3. The idea: a research loop

Here is the workflow I wanted the AI to follow, again and again:

```
        ┌──────────────────────────────────────────────┐
        │                                              │
        ▼                                              │
  1. Read the state      what has been tried? what worked? what failed?
        │
  2. Update lessons      "decay helps reversal signals" — still true?
        │
  3. Write a batch       half new ideas, half improvements of near misses
        │
  4. Simulate            send each alpha to BRAIN, wait, collect results
        │
  5. Submit (once a day) only the best alphas that pass every check
        │                                              │
        └──────────────────────────────────────────────┘
```

Two kinds of thinking happen in step 3:

- **Explore**: try a dataset nobody has looked at yet, starting from an economic story ("companies with rising short interest should underperform, because...").
- **Exploit**: take an alpha that *almost* passed and improve it: change the universe, add decay, neutralize by industry, swap a field, combine it with another weak signal.

The magic (hopefully) is that each round makes the next round smarter.

## 4. Building the lab bench: WQBrainSim

Before the AI can do anything, it needs hands. So I first built **WQBrainSim**, a small command-line tool that talks to BRAIN.

Full confession: I vibe-coded it. I described what I wanted, Claude wrote the code, and I tested it, poked at it, and asked for changes until it behaved. Who writes code by hand these days? :)

It lets me (and the agent) do things like:

```bash
uv run wqbrainsim run --alpha example/alpha.json --settings example/settings.json
uv run wqbrainsim datafields --region USA --delay 1 --universe TOP3000
uv run wqbrainsim check <ALPHA_ID>
```

What it does for us:

- **Simulates** an alpha and waits for the result.
- **Remembers every job** in a small SQLite database. If my laptop sleeps or the network drops, nothing is lost: `resume` picks it up again.
- **Looks up the catalog**: which datasets, fields and operators exist. This is the agent's "library."
- **Runs submission checks**, including self-correlation (is this alpha just a copy of one I already have?).
- **Submits** an alpha, but only with an explicit `--confirm`, because it is irreversible.

A lot of the boring work went into handling the messy real world: rate limits, expired logins, a job whose submission may or may not have gone through. That boring work turned out to be essential. An agent running for hours *will* hit every edge case.

## 5. Writing the rulebook: skills for the agent

Next, I needed to teach the AI *how* to be a researcher. I did this with two layers.

### Layer 1: the hard rules live in code (`research.py`)

This was my most important design decision: **anything that must never go wrong is enforced by code, not by asking nicely in a prompt.**

`research.py` wraps WQBrainSim and refuses to break the rules, no matter what the agent writes. (And yes, of course I vibe-coded this one too :) There is something funny about an AI writing the rulebook that keeps another AI in line.) For example:

| Rule | Why |
|---|---|
| One simulation at a time, 4 to 7 minutes apart, at most 15 per hour | Be polite to the platform and never hit the limit |
| At least half of each batch must be new ideas | Stop the agent from endlessly tweaking one alpha |
| At most 3 variants of one family per batch | Spread the bets |
| A family with 5 tries in a row and no real gain is "exhausted" | Know when to give up |
| No rerun of the exact same input | No wasting simulations on hope |
| Only GOOD-grade alphas are pooled; at most 3 submissions a day | Quality over quantity |
| Lessons must cite evidence, and get retired if contradicted | Don't let wrong beliefs stick around |

LLMs are smart, but they can also be over-confident, forgetful, or just a bit too eager. Code doesn't get tired at 3 a.m.

### Layer 2: the judgment lives in skills (`workflow/`)

The creative part, *what to try next*, is guided by a set of Claude Code skills:

| Skill | Role |
|---|---|
| `wq` | The orchestrator: runs the loop |
| `wq-setup` | First-time setup: review seed alphas, pick default settings, agree on the policy with me |
| `wq-generate` | New ideas from unexplored datasets, always starting from an economic hypothesis |
| `wq-improve` | Improve near misses: settings research, a playbook for fitness, Sharpe, turnover, correlation |
| `wq-submit` | End-of-day submission plus a written report for each submitted alpha |

Plus a shared `PROTOCOL.md`: the commands, file formats and grade lines all skills agree on.

The skills read like advice from a senior colleague:

> *"Change one setting at a time when diagnosing, and tie each change to a mechanism."*
>
> *"No blind grid searches. Prefer values that work across neighbours."*
>
> *"One backtest is not proof."*

### Memory: the lab notebook

A researcher without a notebook repeats the same mistakes. So the agent keeps one:

- `R history`: best alpha of each family, building blocks, near misses.
- `R patterns`: which kinds of changes actually raised fitness.
- `R lessons`: short, testable claims like "SUBINDUSTRY neutralization helps sparse fundamental signals", each with its own record of supporting and contradicting evidence.

Every batch, the agent reads the notebook before writing new ideas, and 2 or 3 candidates in each batch are deliberate *tests* of a lesson. That way, lessons are not just remembered, they are checked.

## 6. Letting it run (together with the agent)

I like to run the research in **interactive mode**: Claude Code is open in the project folder, and I can watch every step, ask questions, and steer when I want to. It feels less like launching a robot and more like working next to a very patient research assistant.

### Step 1: Give the tool your BRAIN login

```bash
cd WQAutoResearch
printf 'BRAIN_USERNAME=you@example.com\nBRAIN_PASSWORD=your_password\n' > WQBrainSim/.env
chmod 600 WQBrainSim/.env
(cd WQBrainSim && uv sync)
```

The agent never needs to see this file. WQBrainSim reads it by itself.

### Step 2: Put the guard rails in place

Copy the proposed permissions into the project's Claude settings:

```bash
mkdir -p .claude
cp claude-settings.proposed.json .claude/settings.json
```

This lets the agent run `research.py` and catalog lookups without asking each time, but **blocks** it from reading `.env`, editing the code or the result records, and calling WQBrainSim's `run`, `resume` or `submit` directly. Every simulation and submission has to go through the referee.

### Step 3: Install the skills and start Claude Code

```bash
claude
```

Then, inside Claude Code:

```
/plugin marketplace add ./workflow
/plugin install wq@wq
```

### Step 4: Say hello: `/wq`

```
/wq
```

The first time, the agent runs **setup** (`wq-setup`). It reads the 159 seed alphas in `initial_alphas/`, proposes default settings and an account label, explains the submission policy, and then stops at a checkpoint:

```
Setup complete.
Account: main   Default settings: WQBrainSim/example/settings.json
Seeds: 159 records, best fitness ...
Ready to start the research loop? Type "yes" or tell me what to change.
```

This is the one moment the agent waits for you. Change anything you like ("stay in TOP3000", "no more than 2 submissions a day"), then type **yes**.

### Step 5: Watch the loop, and chat with it

From here the agent keeps going on its own:

1. It reads `status`, `lessons`, `patterns` and `history`.
2. It writes `results/batch.json` with 10 to 15 ranked ideas, each with a one-line reason.
3. It runs `queue` in the background. Each simulation waits 4 to 7 minutes after the previous one, so a batch takes about an hour.
4. It reads the summary, compares each result with its parent, updates the lessons, and writes the next batch.
5. When the queue says `DAILY_SUBMIT_DUE`, it runs `wq-submit` and writes a report for each submitted alpha in `results/submission_reports/`.

Because you are in interactive mode, you can jump in at any time:

- *"Why did you pick this dataset?"*
- *"Show me the best alpha of each family."*
- *"Try a few ideas on analyst revisions next."*
- *"Stop after this batch."*

Useful commands to check things yourself (`R` is short for `uv run --project WQBrainSim python research.py`):

```bash
uv run --project WQBrainSim python research.py status
uv run --project WQBrainSim python research.py history
uv run --project WQBrainSim python research.py lessons
uv run --project WQBrainSim python research.py show <candidate_id>
```

### Stopping and resuming

Every command saves its state, so you can close Claude Code at any time. Next time, open it in the same folder and type `/wq` again. It starts from `status` and carries on where it left off. A paused simulation resumes by itself.

> 💡 **Prefer to sleep while it works?** There is also an unattended mode, `./autorun.sh`, which runs the same loop with stricter permissions and sends a Mac notification when it needs you. See `HOWTO.md`. I started in interactive mode so I could see how the agent thinks before handing it the keys.

## 7. What I found

> ✏️ *Fill in with your own results. Some prompts to guide this section:*

- **How many simulations ran, and over how many days?** How many alphas reached GOOD / EXCELLENT / SPECTACULAR?
- **Was the agent good at pattern detection?** Did its lessons hold up when tested, or did many get retired?
- **Explore vs. exploit**: where did the best alphas come from: brand new datasets, or patient improvement of a near miss?
- **Surprises**: ideas the agent came up with that you would not have tried.
- **Failure modes**: where it got stuck, repeated itself, or over-tuned. Which of the code-enforced rules were added *because* of something the agent did?
- **Human vs. agent**: how much of your time did it save? What did you still have to do yourself?

A few lessons that came from *building* the system, regardless of the numbers:

1. **Put the hard rules in code, the judgment in prompts.** Prompts guide; code guarantees.
2. **Make every step resumable.** Long-running agents get interrupted. Saving state after every command means a crash costs nothing.
3. **Give the agent a notebook it must cite.** Lessons with evidence and a test record are far more useful than free-form "memory."
4. **Force exploration.** Left alone, an agent loves to polish what already works. A simple rule ("half the batch must be new ideas") keeps it curious.
5. **Know when to quit.** "Exhausted family" rules saved many wasted simulations.

## 8. Beyond trading: autoresearch for computational science

Here is the part that excites me most. **Nothing about this loop is really about finance.**

Look at the shape of it again:

| Alpha research | Computational research (e.g. DFT, MD, CFD) |
|---|---|
| Write an alpha expression | Write an input file (structure, functional, k-points, force field...) |
| Choose settings (universe, decay, neutralization) | Choose parameters (cutoff, smearing, timestep, mesh) |
| Simulate on BRAIN | Run a job on an HPC cluster |
| Read Sharpe, fitness, checks | Parse energies, forces, convergence, errors |
| Lessons: "decay helps reversal signals" | Lessons: "this smearing converges metals faster" |
| Families & near misses | Candidate materials / molecules worth refining |
| Daily submission of the best | Shortlist for experiment or a higher-level calculation |

Computational research has the same ingredients that made this project work:

- a **clear numeric signal** for each try,
- a **huge search space**,
- **expensive, rate-limited compute** that should not be wasted,
- and a lot of **tacit know-how** ("if SCF doesn't converge, try...") that lives in people's heads and old papers.

### My initial idea for a research agent toolkit

I imagine the same two-layer design, rebuilt for a scientific field:

**1. Tools (the hands)** — the equivalent of WQBrainSim:

- **Input builder**: create input files from a structured description, and validate them before anything is submitted.
- **HPC connector**: submit jobs to the scheduler (SLURM, PBS), track them in a local database, resume after interruptions, respect the allocation budget.
- **Result analyzer**: parse outputs, detect common failures (no convergence, wall-time exceeded, imaginary frequencies), and summarize the key numbers.

**2. A rulebook in code** — the equivalent of `research.py`:

- Limits on core-hours per day and jobs in the queue.
- No rerun of an identical input without a stated reason.
- Required convergence tests before a result counts.
- A lesson system with evidence and retirement.

**3. Skills (the judgment)** — setup, generate, improve, report — written in the language of the field.

**4. A literature RAG (the library)** — this is the new piece. In the alpha project, the agent's "library" is the dataset catalog. For science, it should be the **literature**:

- Collect papers, method guides and benchmark studies in the field.
- Index them so the agent can retrieve relevant passages: "what parameters did others use for this class of materials?", "what is a known failure mode for this method?"
- Use them as an **initial guidance**: seed hypotheses, sensible default settings, and a sanity check on surprising results.

The agent would then start where a good PhD student starts: by reading, not by guessing.

## 9. Closing thoughts

I started this project to answer a simple question: *can an LLM do research on its own?*

My honest answer so far: **yes, if you build it a good lab.** Give it reliable tools, strict rules it cannot bend, a notebook it must keep honest, and room to be curious. Then it can run the boring, patient part of research (the hundreds of small experiments) while the human focuses on asking better questions.

Trading signals were just the playground. The real prize is a research assistant that never gets tired of running one more experiment.

---

### Try it yourself

In short:

```bash
printf 'BRAIN_USERNAME=you@example.com\nBRAIN_PASSWORD=your_password\n' > WQBrainSim/.env
chmod 600 WQBrainSim/.env
mkdir -p .claude && cp claude-settings.proposed.json .claude/settings.json
claude
```

Then inside Claude Code:

```
/plugin marketplace add ./workflow
/plugin install wq@wq
/wq
```

See `HOWTO.md` (including the unattended `./autorun.sh` mode), `WQBrainSim/README.md` and `workflow/PROTOCOL.md` for the details.

*— Nam Tran*
