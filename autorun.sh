#!/usr/bin/env bash
set -u
cd "$(dirname "$0")"

# Stop the Mac from idle-sleeping while this runs; sleep mid-request can leave a job uncertain.
if [ -z "${AUTORUN_AWAKE:-}" ] && command -v caffeinate >/dev/null; then
  AUTORUN_AWAKE=1 exec caffeinate -i "$0" "$@"
fi

R() { uv run --project WQBrainSim python research.py "$@"; }
W() { uv run --project WQBrainSim wqbrainsim "$@"; }
claude_run() {
  CLAUDE_CODE_DISABLE_AUTO_MEMORY=1 claude -p "$1" --model "${MODEL:-sonnet}" --strict-mcp-config \
    --setting-sources project --disable-slash-commands \
    --settings "{\"claudeMdExcludes\": [\"$HOME/.claude/CLAUDE.md\"], \"permissions\": {\"deny\": [
      \"Read(**/.env)\", \"Edit(./research.py)\", \"Edit(./autorun.sh)\", \"Edit(./workflow/**)\", \"Edit(./HOWTO.md)\",
      \"Edit(./WQBrainSim/**)\", \"Edit(./tests/**)\", \"Edit(./.claude/**)\", \"Edit(./results/experiment_state.json)\",
      \"Edit(./results/successful_alphas.json)\", \"Edit(./results/experiments.jsonl)\", \"Edit(./results/jobs.db)\",
      \"Edit(./results/lessons.json)\", \"Edit(./results/runs/**)\", \"Edit(./results/submission_pool/**)\"]}}" "${@:2}"
}
ask_claude() {
  claude_run "$1" --permission-mode acceptEdits \
    --allowedTools "Bash(uv run --project WQBrainSim wqbrainsim datafields:*)" \
    "Bash(uv run --project WQBrainSim python research.py show:*)" \
    "Bash(uv run --project WQBrainSim python research.py lesson:*)"
}
notify() {
  osascript -e 'on run argv' -e 'display notification (item 1 of argv) with title "WorldQuant autorun"' \
    -e 'end run' "$1" >/dev/null 2>&1 || true
}
stop() { echo "$1"; notify "$1"; exit 1; }
report() {
  claude_run "Read workflow/skills/wq-submit/SKILL.md, then write results/submission_reports/$1.md as described in its \"Report for submission\" section. This runs unattended and nobody can answer questions, so never ask; write only that file.
Alpha $1 was just submitted to WorldQuant BRAIN. Submission line:
$2
Its record:
$(python3 -c "import json,sys;print(json.dumps(json.load(open('results/successful_alphas.json'))[sys.argv[1]], indent=1))" "$1")" \
    --permission-mode acceptEdits --tools "Read,Write" </dev/null || { echo "Could not write the report for $1; continuing."; return; }
  notify "Submitted $1. Report: results/submission_reports/$1.md"
}

if [ ! -f WQBrainSim/.env ]; then
  stop "Create WQBrainSim/.env with BRAIN_USERNAME=... and BRAIN_PASSWORD=... first."
fi
[ -d WQBrainSim/.venv ] || (cd WQBrainSim && uv sync -q) || stop "Could not install WQBrainSim."
[ -f results/experiment_state.json ] || R init --account main --settings WQBrainSim/example/settings.json \
  || stop "Could not initialise results/."

read -r REGION DELAY UNIVERSE < <(python3 -c "import json;s=json.load(open('results/settings.json'));print(s['region'],s['delay'],s['universe'])")
SCOPE="--region $REGION --delay $DELAY --universe $UNIVERSE"
DATASETS="results/catalog/datasets_${REGION}_${DELAY}_${UNIVERSE}.json"
# BRAIN adds datasets and operators over time, so catalogs older than a week are fetched again.
refresh_catalog() {
  local file=$1; shift
  [ -n "$(find "$file" -mtime -7 2>/dev/null)" ] && return
  rm -f "$file.new"
  W "$@" --output "$file.new" && mv "$file.new" "$file" || echo "Could not refresh $file; continuing."
}
refresh_catalog results/catalog/operators.json operators
refresh_catalog results/catalog/settings_options.json settings
UNIVERSES=$(python3 -c "import json,sys;d=json.load(open('results/catalog/settings_options.json'))['results']['universe']['choices']['instrumentType']['EQUITY']['region'][sys.argv[1]];print(' '.join(c['value'] for c in d))" "$REGION" 2>/dev/null) || UNIVERSES=$UNIVERSE
for universe in $UNIVERSES; do
  refresh_catalog "results/catalog/datasets_${REGION}_${DELAY}_${universe}.json" datasets --region "$REGION" --delay "$DELAY" --universe "$universe"
done

need_ideas=no
empty_batches=0
failed_batches=0
failures=0
claude_failures=0
triages=0
decision=

# Temporary problems: wait 5, 10, 20, 40, 60 and 60 minutes, then ask Claude what to do.
wait_and_retry() {
  failures=$((failures + 1))
  if [ $failures -gt 6 ]; then
    failures=0
    triage "$1"
    return
  fi
  seconds=$((300 << (failures - 1)))
  [ $seconds -le 3600 ] || seconds=3600
  echo "Retrying in $((seconds / 60)) minutes."
  sleep $seconds
}

triage() {
  triages=$((triages + 1))
  [ $triages -le 3 ] || stop "Claude was asked what to do 3 times in a row without progress. Stopped: the loop needs you."
  reply=$(claude_run "The unattended WorldQuant BRAIN research loop in this folder stopped. Choose what it does next. You cannot run commands or edit files.

Last output:
$(printf '%s\n' "$1" | tail -40)

Status:
$(R status 2>&1)

Actions:
- WAIT: a temporary problem, such as a network error, BRAIN being busy or down, or another run still active. The loop waits 30 minutes and tries again.
- RETRY: the problem is already gone. The loop tries again now.
- SET_ASIDE <candidate_id>: one candidate file causes the problem. It is moved out of the queue and the loop goes on.
- STOP: a person must act: a login or access problem, a bug in the code, a submission whose outcome is unknown, or anything you are unsure about.

Reply with one line giving the reason, then a last line exactly in the form: ACTION: <action>" --tools "") || reply=
  decision=$(printf '%s\n' "$reply" | grep -E '^ACTION: (WAIT|RETRY|STOP|SET_ASIDE [A-Za-z0-9_.-]+)$' | tail -1)
  decision=${decision#ACTION: }
  [ -n "$decision" ] || decision=STOP
  R log autorun_triage --data "$(python3 -c 'import json,sys;print(json.dumps({"decision":sys.argv[1],"output":sys.argv[2][-2000:],"reply":sys.argv[3][-1000:]}))' "$decision" "$1" "$reply")" >/dev/null
  echo "Claude decided: $decision"
  case "$decision" in
    WAIT) sleep 1800 ;;
    RETRY) ;;
    SET_ASIDE\ *) R set_aside "${decision#SET_ASIDE }" --reason "autorun: Claude found it stopped the loop" \
                    || stop "Could not set aside ${decision#SET_ASIDE }. Stopped." ;;
    *) stop "Claude decided a person must act. See the lines above. Run ./autorun.sh again when it is fixed." ;;
  esac
}

while true; do
  if [ "$need_ideas" = yes ]; then
    recent=$(ls -t results/batches/*.txt 2>/dev/null | head -3)
    if [ -n "$recent" ]; then
      history="Lessons learned so far:
$(R lessons 2>&1)

Patterns over all simulations, computed from the results:
$(R patterns 2>&1)

All simulations so far:
$(R history 2>&1)

Results of the last 3 batches, newest first:
$(for file in $recent; do cat "$file"; echo; done)

Learn from all of it before writing candidates:
1. Lessons are claims and can be wrong. Check each active lesson against the patterns and the recent batches. If the results contradict it, retire it or record the evidence with: uv run --project WQBrainSim python research.py lesson judge <lesson_id> <candidate_id> --verdict supports|contradicts --reason \"...\"
   Retire with: uv run --project WQBrainSim python research.py lesson retire <lesson_id> --reason \"...\"
   A lesson contradicted at least twice and more often than supported is retired automatically.
2. Add a lesson only for a pattern seen in at least 2 simulations, and cite them: uv run --project WQBrainSim python research.py lesson add \"<advice>\" --evidence <candidate_id>,<candidate_id>
   Write each lesson as specific, testable advice. Keep at most 20 active: merge overlapping lessons by adding the merged one and retiring the old ones. If there are no lessons yet, build the first set from the patterns and the full history.
3. Make 2 or 3 of the candidates tests of untested or doubtful lessons: apply the lesson to a parent, and add \"tests\": {\"lesson\": \"<lesson_id>\", \"expect\": \"better\" or \"not_better\"} to the candidate file. The result is judged automatically against the parent's fitness.
4. To look closer at one simulation before deciding (all metrics, every check with its value and limit, the comparison with its parent, its children, lessons it bears on), run: uv run --project WQBrainSim python research.py show <candidate_id>
Then improve near misses, do not repeat ideas that failed or errors that happened, and move away from explored datasets.
Explore widely, but go deep on PROMISING families. Without simulating, the queue holds back for up to 2 later batches: variants beyond the number of new ideas (parent null) in the batch, except variants of a PROMISING family, and more than 3 variants of one family per batch. Held-back candidates stay only if you rank them again in the next batch, so write fewer variants while some wait. It sets aside any variant of an EXHAUSTED family. A family is a root idea and every candidate descended from it. A candidate that uses exactly the same data fields as an earlier one joins that one's family, whatever its parent says. Spread variants over several families, not only the top one.
A REVERSED result means the idea works the other way: build variants on the negated expression. Simulate a plain negation only if its flipped numbers would clear the limits, because the result is otherwise known.
An alpha that passed every simulation check but is correlated or below_grade cannot be submitted, yet it has a real edge: improve it as a parent. For a correlated one, follow \"Fixing a correlated alpha\" in workflow/skills/wq-improve/SKILL.md.
Aim higher than passing. BRAIN grades by fitness: GOOD above 1.5, EXCELLENT above 2.0, SPECTACULAR above 2.5. Only GOOD or better is submitted, and a GOOD alpha waits up to 2 days for a better version from its family. Improve the pooled alphas the history lists near the next grade line. Fitness = Sharpe * sqrt(abs(returns) / max(turnover, 0.125)), so raise Sharpe or returns; turnover below 12.5% adds nothing.
Start at least one candidate in each batch on a dataset not used before, so new alphas do not correlate with the submitted ones. The queue holds back every variant in a batch where no candidate uses a data field never simulated before.
Strong alphas usually add 3 to 7 weak, unrelated legs. Take them from the building blocks in the history, from different datasets, negate REVERSED ones, and never reuse a leg of a submitted alpha. Write each leg as group_rank(<signal>, bucket(rank(ts_sum(returns, N)), range=\"0,1,0.1\")) with N near the signal's horizon, which compares stocks only with others of similar recent returns, and multiply the sum by (1 + rank(adv20)) to lean toward liquid stocks.
Choose each candidate's universe on purpose and put it in its settings: TOP3000, TOP2000, TOP1000, TOP500, TOP200 or TOPSP500. Combinations of slow signals (analyst, fundamentals, sentiment, options) usually need breadth, so try TOP3000 or TOP1000 first; short-horizon price signals may suit smaller universes. The patterns above show results by universe; ones with few runs are unexplored. An earlier test moved TOPSP500-tuned alphas to TOP3000 unchanged and they got worse; that tested transfer, not the universe. To try an idea in another universe, write it with parent null: the same data fields in a new universe start a new family.
Before combining fields, put each on a common scale with rank or zscore; WARNING:UNITS means units were mixed.
On sparse data (options, social media, some fundamentals), fill gaps with ts_backfill or rank within groups with group_rank before tightening truncation, to avoid CONCENTRATED_WEIGHT."
    else
      history="No batches yet. Best seed alphas (id sharpe fitness turnover expression):
$(python3 -c "import json;d=json.load(open('initial_alphas/alphas.json'));d=[a for a in d if a.get('metrics')];d.sort(key=lambda a:-(a['metrics']['fitness'] or -9));[print(a['candidate_id'],a['metrics']['sharpe'],a['metrics']['fitness'],a['metrics']['turnover'],a['expression']) for a in d[:30]]")"
    fi
    if ! ask_claude "Read workflow/PROTOCOL.md, workflow/skills/wq/SKILL.md, workflow/skills/wq-generate/SKILL.md and workflow/skills/wq-improve/SKILL.md. Setup is approved; do one batch of the research loop only. This runs unattended and nobody can answer questions, so never ask; write the files.
Current status:
$(R status 2>&1)

$history

Catalogs: results/catalog/operators.json, results/catalog/settings_options.json (the settings this account may use) and results/catalog/datasets_${REGION}_${DELAY}_<universe>.json for each universe, whose coverage differs by universe. Read only the parts you need. Use only operators, fields and settings that exist there.
To list a dataset's fields, run: uv run --project WQBrainSim wqbrainsim datafields $SCOPE --dataset <id> --output results/catalog/fields_<id>.json
Reuse a fields file that already exists instead of fetching it again. Besides that, run only the research.py lesson and show commands above.
Run each command exactly as written, one per call, from the current folder: never add cd, &&, pipes or other commands, or it is refused and nobody can approve it. Write the batch file directly; do not write helper scripts or scratch folders.
Write results/batch.json as described in workflow/PROTOCOL.md: about 15 new candidates, each with an id that was never used before, ranked by expected edge, best first, each with a one-line reason. Candidates the last batch report says wait for the next batch are set aside unless you rank them again by id. End with one line per candidate in rank order: its id and reason."; then
      claude_failures=$((claude_failures + 1))
      # Ten tries 30 minutes apart outlast a Claude usage window.
      [ $claude_failures -lt 10 ] || stop "Claude failed 10 times in a row, for example a login problem. Stopped."
      echo "Claude failed, for example a usage limit. Trying again in 30 minutes."
      sleep 1800
      continue
    fi
    claude_failures=0
  fi
  out=$(R queue 2>&1 | tee /dev/stderr)
  case "$out" in
    *"Queue finished: 0 simulated"*)
      if [ "$need_ideas" = yes ]; then
        empty_batches=$((empty_batches + 1))
        [ $empty_batches -lt 2 ] || stop "Claude wrote no usable candidates twice in a row. Stopped."
      fi
      need_ideas=yes; failures=0 ;;
    *"Queue finished"*)
      need_ideas=yes; failures=0; triages=0; empty_batches=0
      if printf '%s\n' "$out" | grep -q "  complete  alpha="; then
        failed_batches=0
      else
        failed_batches=$((failed_batches + 1))
        [ $failed_batches -lt 3 ] || stop "No simulation completed in 3 batches in a row, for example because BRAIN rejects them all. Stopped."
      fi ;;
    *[Aa]uthentication*) stop "BRAIN login needs you. See the lines above." ;;
    *DAILY_SUBMIT_DUE*)
      if submitted=$(R daily_submit 2>&1); then
        echo "$submitted"; need_ideas=no; failures=0
        case "$submitted" in
          *"UNCERTAIN SUBMISSION"*|*"SUBMISSION REFUSED"*) notify "A submission needs your attention; see results/research.log." ;;
        esac
        while read -r alpha; do
          report "$alpha" "$(printf '%s\n' "$submitted" | grep "^SUBMITTED $alpha ")"
        done < <(printf '%s\n' "$submitted" | sed -n 's/^SUBMITTED \([A-Za-z0-9]*\) .*/\1/p')
      else
        echo "$submitted"; wait_and_retry "$submitted"
      fi ;;
    *HOLD*|*DAILY_SIMULATION_LIMIT*) need_ideas=no; failures=0; sleep 3600 ;;
    *"PAUSED: "*|*"UNCERTAIN: "*|*"Unresolved jobs above"*|*"Another runner"*) need_ideas=no; wait_and_retry "$out" ;;
    *) need_ideas=no; triage "$out" ;;
  esac
done
