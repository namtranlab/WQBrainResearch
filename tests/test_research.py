import json
from datetime import datetime, timedelta, timezone
import time
from pathlib import Path

import pytest
import requests

import research
from core import BrainClient, JobStore
from core.client import APIError, Deadline

ROOT = Path(__file__).resolve().parent.parent
SETTINGS_FILE = ROOT / "WQBrainSim/example/settings.json"
PASSING = [{"name": "LOW_SHARPE", "result": "PASS", "limit": 1.25, "value": 1.6},
           {"name": "SELF_CORRELATION", "result": "PENDING"}]


class Response:
    def __init__(self, status, body=None, headers=None):
        self.status_code = status
        self.headers = headers or {}
        self.content = json.dumps(body).encode() if body is not None else b""
        self._body = body

    def json(self):
        return self._body


class Brain:
    def __init__(self):
        self.calls = []
        self.check_result = "PASS"
        self.grade = "GOOD"

    def request(self, method, url, **kwargs):
        route = url.removeprefix(BrainClient.origin)
        self.calls.append((method, route))
        if route == "/authentication":
            return Response(201)
        if route == "/simulations":
            return Response(201, headers={"Location": "/simulations/s1"})
        if route == "/simulations/s1":
            return Response(200, {"status": "COMPLETE", "alpha": "A1"})
        if route == "/users/self/alphas":
            return Response(200, {"count": 0, "results": []})
        if route.endswith("/check"):
            return Response(200, {"is": {"checks": [{"name": "SELF_CORRELATION", "result": self.check_result, "value": 0.3}]}})
        if route.endswith("/submit"):
            return Response(201) if method == "POST" else Response(200, {"is": {"checks": []}})
        return Response(200, {"is": {"sharpe": 1.6, "fitness": 1.2, "checks": PASSING}, "grade": self.grade})

    def close(self):
        pass


class Clock:
    def __init__(self):
        self.now = time.time()

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


@pytest.fixture
def brain(tmp_path, monkeypatch):
    fake = Brain()
    clock = Clock()
    monkeypatch.setattr(research, "RESULTS", tmp_path / "results")
    monkeypatch.setattr(research, "now_et", lambda: datetime(2026, 10, 30, 12, tzinfo=research.EASTERN))
    monkeypatch.setattr(research, "make_client", lambda state: BrainClient(
        session=fake, credentials=lambda: ("u", "p"), account=state["account"],
        clock=clock, wall_clock=clock, sleep=clock.sleep))
    assert research.main(["init", "--account", "test", "--settings", str(SETTINGS_FILE)]) == 0
    return fake


def candidate(name="c1", **fields):
    data = {"expression": "rank(close)", "description": {"hypothesis": "h", "implementation": "i"},
            "parent": None, "change": "baseline"} | fields
    research.write(research.path("candidates", f"{name}.json"), data)


def state():
    return research.read(research.path("experiment_state.json"))


def update_state(**fields):
    research.write(research.path("experiment_state.json"), state() | fields)


def pool(alpha_id, grade="GOOD", fitness=1.0, pooled_at="2026-10-01T00:00:00+00:00"):
    entry = {"alpha_id": alpha_id, "candidate": f"c_{alpha_id}", "status": "pooled", "expression": f"rank({alpha_id})",
             "grade": grade, "metrics": {"fitness": fitness, "sharpe": 1.5}, "settings": {}, "pooled_at": pooled_at}
    research.write(research.path("submission_pool", f"{alpha_id}.json"), entry)
    successful = research.read(research.path("successful_alphas.json"))
    research.write(research.path("successful_alphas.json"), successful | {alpha_id: entry})


def test_passing_simulation_is_recorded(brain):
    candidate()
    assert research.main(["sim", "c1"]) == 0
    assert research.read(research.path("successful_alphas.json"))["A1"]["status"] == "new"
    assert len(list(research.path("runs").glob("*.json"))) == 1
    assert '"event": "simulated"' in research.path("experiments.jsonl").read_text()


def test_candidate_without_change_is_rejected(brain):
    candidate(change=" ")
    assert research.main(["sim", "c1"]) == 2
    assert brain.calls == []


def test_research_continues_after_the_daily_submission(brain):
    candidate()
    pool("P1")
    update_state(hold_until="2026-10-30", pooled_counts={"2026-10-30": research.POOL_TRIGGER})
    assert research.main(["sim", "c1"]) == 0
    assert research.path("submission_pool", "P1.json").exists()


def test_due_daily_submission_blocks_simulation(brain):
    candidate()
    pool("P1")
    update_state(pooled_counts={"2026-10-30": research.POOL_TRIGGER})
    assert research.main(["sim", "c1"]) == 3
    assert brain.calls == []


def test_paused_job_blocks_other_candidates(brain):
    candidate("c1")
    candidate("c2", expression="rank(open)")
    brain.request = lambda method, url, **kwargs: Response(401)
    assert research.main(["sim", "c1"]) == 3
    brain.request = Brain().request
    assert research.main(["sim", "c2"]) == 3


def test_pipeline_pools_a_passing_alpha(brain):
    candidate()
    research.main(["sim", "c1"])
    assert research.main(["pipeline", "A1"]) == 0
    assert research.path("submission_pool", "A1.json").exists()
    assert state()["pooled_counts"]["2026-10-30"] == 1


def test_pipeline_keeps_correlated_alpha_out_of_pool(brain):
    candidate()
    research.main(["sim", "c1"])
    brain.check_result = "FAIL"
    assert research.main(["pipeline", "A1"]) == 1
    assert not research.path("submission_pool", "A1.json").exists()
    assert research.read(research.path("successful_alphas.json"))["A1"]["status"] == "correlated"


@pytest.mark.parametrize("grade", ["INFERIOR", "AVERAGE"])
def test_pipeline_keeps_an_alpha_below_good_out_of_pool_without_asking_brain(brain, capsys, grade):
    brain.grade = grade
    candidate()
    research.main(["sim", "c1"])
    brain.calls.clear()
    assert research.main(["pipeline", "A1"]) == 0
    assert brain.calls == []
    assert not research.path("submission_pool", "A1.json").exists()
    assert research.read(research.path("successful_alphas.json"))["A1"]["status"] == "below_grade"
    capsys.readouterr()
    research.main(["history"])
    assert f"c1  below_grade  {grade}" in capsys.readouterr().out


def test_history_lists_a_correlated_alpha_to_improve(brain, capsys):
    candidate()
    research.main(["sim", "c1"])
    brain.check_result = "FAIL"
    research.main(["pipeline", "A1"])
    capsys.readouterr()
    research.main(["history"])
    assert "c1  correlated  GOOD  0.3  -  rank(close)" in capsys.readouterr().out


def test_daily_submit_refuses_before_it_is_due(brain):
    pool("P1")
    assert research.main(["daily_submit"]) == 3
    assert brain.calls == []


def test_daily_submit_respects_cap_and_ranking(brain):
    pool("P1", grade="AVERAGE")
    pool("P2", grade="EXCELLENT")
    pool("P3", grade="GOOD")
    pool("P4", grade="EXCELLENT")
    update_state(pooled_counts={"2026-10-30": research.POOL_TRIGGER})
    assert research.main(["daily_submit"]) == 0
    submitted = [route for method, route in brain.calls if method == "POST" and route.endswith("/submit")]
    assert sorted(submitted[:2]) == ["/alphas/P2/submit", "/alphas/P4/submit"]
    assert submitted[2:] == ["/alphas/P3/submit"]
    assert state()["daily_submissions"]["2026-10-30"] == research.DAILY_CAP
    assert state()["hold_until"] == "2026-10-30"
    assert research.path("submission_pool", "P1.json").exists()


def test_check_status():
    assert research.check_status([]) == "check_pending"
    assert research.check_status([{"name": "X", "result": "PASS"}]) == "passed"
    assert research.check_status([{"name": "X", "result": "PASS"}, {"name": "UNITS", "result": "WARNING"}]) == "passed"
    assert research.check_status([{"name": "X", "result": "ERROR"}]) == "check_error"
    assert research.check_status([{"name": "SELF_CORRELATION", "result": "FAIL"},
                                  {"name": "X", "result": "FAIL"}]) == "check_failed"


def test_status_and_dry_run_need_no_network(brain, capsys):
    candidate()
    assert research.main(["sim", "c1", "--dry-run"]) == 0
    assert research.main(["status"]) == 0
    assert "research may continue" in capsys.readouterr().out
    assert brain.calls == []


def test_daily_simulation_limit_holds_research(brain):
    candidate()
    brain.request = lambda method, url, **kwargs: (
        Response(429, {"detail": "DAILY_SIMULATION_LIMIT_EXCEEDED"}) if url.endswith("/simulations") else Response(201))
    assert research.main(["sim", "c1"]) == 3
    assert state()["sim_limit_until"] == "2026-10-30"
    assert "DAILY_SIMULATION_LIMIT" in research.research_blocked(state(), 0, research.now_et())


def test_refused_submission_keeps_alpha_in_pool(brain):
    pool("P1")
    update_state(pooled_counts={"2026-10-30": research.POOL_TRIGGER})
    real = brain.request
    brain.request = lambda method, url, **kwargs: (
        Response(429, {"detail": "THROTTLED"}) if method == "POST" and url.endswith("/submit") else real(method, url, **kwargs))
    assert research.main(["daily_submit"]) == 0
    assert research.path("submission_pool", "P1.json").exists()
    assert "P1" not in state()["blocked"]


def test_queue_runs_untried_candidates_and_pools_passes(brain, capsys):
    candidate("c1")
    candidate("c2", expression="rank(open)")
    candidate("bad", change="")
    assert research.main(["queue"]) == 0
    out = capsys.readouterr().out
    assert "c1  complete" in out and "pipeline=pooled" in out
    assert "bad  set aside" in out and "Queue finished: 2 simulated" in out
    assert research.path("submission_pool", "A1.json").exists()
    assert not research.path("candidates", "bad.json").exists()
    assert len(list(research.path("batches").glob("*.txt"))) == 1
    assert research.main(["queue"]) == 0
    assert "Queue finished: 0 simulated" in capsys.readouterr().out
    assert len(list(research.path("batches").glob("*.txt"))) == 1
    assert sum(1 for method, route in brain.calls if route == "/simulations") == 2


def test_queue_stops_when_research_is_blocked(brain, capsys):
    candidate("c1")
    update_state(sim_limit_until="2026-10-30")
    assert research.main(["queue"]) == 3
    assert "DAILY_SIMULATION_LIMIT" in capsys.readouterr().out
    assert brain.calls == []


def test_slow_check_is_retried_in_the_next_queue_run(brain, monkeypatch, capsys):
    candidate()
    real = research.check_submission
    monkeypatch.setattr(research, "check_submission", lambda *a, **k: (_ for _ in ()).throw(Deadline("slow")))
    assert research.main(["queue"]) == 0
    entry = research.read(research.path("successful_alphas.json"))["A1"]
    assert (entry["status"], entry["check_attempts"]) == ("check_pending", 1)
    monkeypatch.setattr(research, "check_submission", real)
    assert research.main(["queue"]) == 0
    assert "A1  pipeline retry  pooled" in capsys.readouterr().out


def test_reused_candidate_id_is_set_aside(brain, capsys):
    candidate("c1")
    research.main(["queue"])
    candidate("c1", expression="rank(volume)")
    assert research.main(["queue"]) == 0
    assert "c1  set aside  this id was already simulated" in capsys.readouterr().out
    assert sum(1 for method, route in brain.calls if route == "/simulations") == 1


def test_daily_submit_keeps_going_after_a_check_error(brain, monkeypatch):
    pool("P1", grade="EXCELLENT")
    pool("P2", grade="GOOD")
    update_state(pooled_counts={"2026-10-30": research.POOL_TRIGGER})
    real = research.check_submission

    def check(client, alpha_id, **kwargs):
        if alpha_id == "P1":
            raise APIError("HTTP 502")
        return real(client, alpha_id, **kwargs)

    monkeypatch.setattr(research, "check_submission", check)
    assert research.main(["daily_submit"]) == 0
    assert research.path("submission_pool", "P1.json").exists()
    assert state()["daily_submissions"]["2026-10-30"] == 1
    assert research.read(research.path("successful_alphas.json"))["P1"]["status"] == "pooled"
    assert "P1" not in research.pipeline_backlog()


def test_history_shows_one_line_per_family(brain, capsys):
    candidate("c1")
    candidate("c2", expression="rank(open)", settings={"decay": 9})
    research.main(["queue"])
    candidate("c3", parent="c1", expression="rank(high)")
    candidate("n1", expression="rank(low)")
    research.main(["queue"])
    research.main(["log", "dataset_explored", "--data", '{"dataset": "news12", "outcome": "no edge"}'])
    capsys.readouterr()
    assert research.main(["history", "--top", "1"]) == 0
    out = capsys.readouterr().out
    assert "simulated=4  failed=0" in out and "families=3" in out
    assert "Best 1 families" in out and out.count("pooled") == 1
    assert "news12  no edge" in out


def test_family_follows_parents_to_the_root(brain):
    candidate("c1")
    candidate("c2", parent="c1")
    candidate("c3", parent="c2")
    candidate("s1", parent="seed_alpha")
    assert research.family("c3") == "c1"
    assert research.family("s1") == "seed_alpha"
    assert research.family("c1") == "c1"


def batch(*names):
    items = [(name, research.read(research.path("candidates", f"{name}.json"))["expression"], "TOPSP500") for name in names]
    return research.diversify(list(names), research.assign_families(items), {}, True)


def test_diversify_limits_variants_per_family(brain):
    for index in range(4):
        candidate(f"n{index}", expression=f"rank(new_field_{index})")
        candidate(f"v{index}", parent="c1", expression=f"rank(variant_field_{index})")
    chosen, skipped = batch("n0", "n1", "n2", "n3", "v0", "v1", "v2", "v3")
    assert chosen == ["n0", "n1", "n2", "n3", "v0", "v1", "v2"]
    assert skipped[0][0] == "v3" and "at most 3 variants" in skipped[0][1]


def test_diversify_keeps_half_of_the_batch_on_new_ideas(brain):
    candidate("n1", expression="rank(open)")
    candidate("v1", parent="c1")
    candidate("w1", parent="c2")
    chosen, skipped = batch("n1", "v1", "w1")
    assert chosen == ["n1", "v1"]
    assert skipped[0][0] == "w1" and "at least half" in skipped[0][1]


def test_variants_of_a_promising_family_need_no_new_ideas_beside_them(brain):
    for index in range(4):
        candidate(f"v{index}", parent="c1", expression=f"rank(variant_field_{index})")
    names = ["v0", "v1", "v2", "v3"]
    items = [(name, research.read(research.path("candidates", f"{name}.json"))["expression"], "TOPSP500") for name in names]
    family = {"runs": 5, "since_best": 0, "fitness": 0.85, "sharpe": 0.9, "best": None, "edge": 1.06, "submitted": False}
    chosen, skipped = research.diversify(names, research.assign_families(items), {"c1": family}, True)
    assert chosen == ["v0", "v1", "v2"]
    assert "at most 3 variants" in skipped[0][1]
    chosen, skipped = research.diversify(names, research.assign_families(items), {"c1": family | {"edge": 0.6}}, True)
    assert chosen == [] and "at least half" in skipped[0][1]


def test_blocked_queue_neither_defers_nor_sets_aside(brain, monkeypatch, capsys):
    monkeypatch.setattr(research, "NEAR_SHARPE", 9)
    monkeypatch.setattr(research, "NEAR_FITNESS", 9)
    candidate("c1")
    research.main(["queue"])
    candidate("v1", parent="c1", expression="rank(open)")
    research.main(["queue"])
    update_state(sim_limit_until="2026-10-30")
    capsys.readouterr()
    for _ in range(3):
        assert research.main(["queue"]) == 3
        assert "DAILY_SIMULATION_LIMIT" in capsys.readouterr().out
    assert research.path("candidates", "v1.json").exists() and state()["deferred"] == {"v1": 1}


def test_queue_waiting_on_an_uncertain_job_neither_defers_nor_sets_aside(brain, monkeypatch, capsys):
    monkeypatch.setattr(research, "NEAR_SHARPE", 9)
    monkeypatch.setattr(research, "NEAR_FITNESS", 9)
    candidate("c1")
    research.main(["queue"])
    candidate("v1", parent="c1", expression="rank(open)")
    research.main(["queue"])
    with JobStore(research.path("jobs.db")) as store:
        job = store.prepare("rank(volume)", {}, BrainClient.origin, "test", True)
        job.state, job.attempted_at = "uncertain", datetime.now(timezone.utc).isoformat()
        store.save(job)
    update_state(jobs=state()["jobs"] | {job.id: "u1"})
    capsys.readouterr()
    for _ in range(3):
        assert research.main(["queue"]) == 3
        assert "Unresolved jobs above" in capsys.readouterr().out
    assert research.path("candidates", "v1.json").exists() and state()["deferred"] == {"v1": 1}


def test_submission_due_comes_before_the_simulation_limit(brain):
    update_state(sim_limit_until="2026-10-30", pooled_counts={"2026-10-30": research.POOL_TRIGGER})
    reason = research.research_blocked(state(), 1, research.now_et())
    assert reason.startswith("DAILY_SUBMIT_DUE")


def test_candidate_identical_to_a_simulated_one_is_set_aside(brain, capsys):
    candidate("c1")
    research.main(["queue"])
    candidate("dup")
    capsys.readouterr()
    research.main(["queue"])
    assert "dup  set aside  same expression and settings as c1" in capsys.readouterr().out
    assert list(state()["jobs"].values()) == ["c1"]


def test_retry_waits_while_the_candidate_has_an_unresolved_job(brain):
    candidate("c1")
    answer = brain.request

    def lost_post(method, url, **kwargs):
        if method == "POST" and url.endswith("/simulations"):
            raise requests.ConnectionError()
        return answer(method, url, **kwargs)

    brain.request = lost_post
    assert research.main(["sim", "c1"]) == 3
    brain.request = answer
    update_state(stuck_retries={"c1": {"attempts": 1, "after": "2026-01-01T00:00:00+00:00"}})
    research.main(["queue"])
    assert ("POST", "/simulations") not in brain.calls


def test_set_aside_refuses_a_candidate_with_an_unfinished_simulation(brain):
    candidate("c1")
    brain.request = lambda method, url, **kwargs: Response(401)
    research.main(["sim", "c1"])
    assert research.main(["set_aside", "c1", "--reason", "x"]) == 2
    assert research.path("candidates", "c1.json").exists()


def test_lesson_test_needs_a_lesson_id_string(brain):
    research.write(research.path("lessons.json"), {})
    candidate("t1", parent="p", tests={"lesson": ["L1"], "expect": "better"})
    with pytest.raises(ValueError, match="tests must be"):
        research.load_candidate("t1")


def test_daily_submit_refuses_after_todays_submission_ran(brain):
    pool("P1")
    update_state(hold_until="2026-10-30", pooled_counts={"2026-10-30": research.POOL_TRIGGER})
    assert research.main(["daily_submit"]) == 3
    assert not [route for method, route in brain.calls if route.endswith("/submit")]


def test_reversed_edges_are_promising_but_submitted_families_are_not():
    reversed_run = {"state": "complete", "alpha_id": "R1", "metrics": {"fitness": -0.3, "sharpe": -1.2}}
    assert research.promising(research.family_stats([("r", reversed_run)], {"r": "r"}, set())["r"])
    assert not research.promising(research.family_stats([("r", reversed_run)], {"r": "r"}, {"R1"})["r"])
    weak_run = reversed_run | {"metrics": {"fitness": 0.3, "sharpe": 0.6}}
    assert not research.promising(research.family_stats([("r", weak_run)], {"r": "r"}, set())["r"])


def test_extra_variants_wait_for_later_batches_then_are_set_aside(brain, monkeypatch, capsys):
    monkeypatch.setattr(research, "NEAR_SHARPE", 9)
    monkeypatch.setattr(research, "NEAR_FITNESS", 9)
    candidate("c1")
    research.main(["queue"])
    candidate("v1", parent="c1", expression="rank(open)")
    capsys.readouterr()
    for attempt in (1, 2):
        research.main(["queue"])
        assert f"v1  waits for a later batch ({attempt}/{research.MAX_DEFERRALS})" in capsys.readouterr().out
        assert research.path("candidates", "v1.json").exists()
    research.main(["queue"])
    assert "v1  set aside  at least half" in capsys.readouterr().out
    assert not research.path("candidates", "v1.json").exists()


def test_pool_ties_go_to_lower_turnover():
    low = {"grade": "GOOD", "metrics": {"fitness": 1.1, "sharpe": 1.5, "turnover": 0.1}}
    high = {"grade": "GOOD", "metrics": {"fitness": 1.1, "sharpe": 1.5, "turnover": 0.3}}
    assert research.rank_key(low) > research.rank_key(high)
    assert research.rank_key(high) > research.rank_key({"grade": "GOOD", "metrics": {"fitness": 1.1, "sharpe": 1.5}})


def test_same_data_fields_count_as_the_same_family_whatever_the_parent(brain):
    candidate("c1", expression="ts_corr(close, volume, 20)")
    candidate("copy", expression="-ts_corr(volume, close, 10)")
    candidate("v1", parent="copy", expression="rank(close)")
    candidate("other", expression="rank(eps)")
    items = [(name, research.read(research.path("candidates", f"{name}.json"))["expression"], "TOPSP500")
             for name in ("c1", "copy", "v1", "other")]
    assert research.assign_families(items) == {"c1": "c1", "copy": "c1", "v1": "c1", "other": "other"}


def test_same_data_fields_in_another_universe_start_a_new_family(brain):
    candidate("c1", expression="ts_corr(close, volume, 20)")
    candidate("wide", expression="ts_corr(close, volume, 60)")
    items = [("c1", "ts_corr(close, volume, 20)", "TOPSP500"), ("wide", "ts_corr(close, volume, 60)", "TOP3000")]
    assert research.assign_families(items) == {"c1": "c1", "wide": "wide"}


def test_queue_sets_aside_a_disguised_copy_of_an_exhausted_family(brain, monkeypatch, capsys):
    monkeypatch.setattr(research, "FAMILY_PATIENCE", 1)
    candidate("c1")
    research.main(["queue"])
    candidate("v1", parent="c1", expression="rank(close + 1)")
    candidate("n1", expression="rank(open)")
    research.main(["queue"])
    candidate("copy", expression="ts_rank(close, 5)")
    candidate("fresh", expression="rank(vwap)")
    capsys.readouterr()
    research.main(["queue"])
    out = capsys.readouterr().out
    assert "copy  set aside  family c1 is exhausted" in out
    assert "fresh  complete" in out


def test_queue_sets_aside_variants_of_an_exhausted_family(brain, monkeypatch, capsys):
    monkeypatch.setattr(research, "FAMILY_PATIENCE", 2)
    candidate("c1")
    research.main(["queue"])
    for index in range(2):
        candidate(f"v{index}", parent="c1", expression=f"rank(close + {index})")
        candidate(f"n{index}", expression=f"rank(new_field_{index})")
    research.main(["queue"])
    candidate("late", parent="c1", expression="rank(volume)")
    candidate("fresh", expression="rank(vwap)")
    capsys.readouterr()
    research.main(["queue"])
    out = capsys.readouterr().out
    assert "late  set aside  family c1 is exhausted" in out
    assert "fresh  complete" in out


def test_warning_check_does_not_block_a_passing_simulation():
    job = type("Job", (), {"state": "complete", "checks": PASSING + [{"name": "UNITS", "result": "WARNING"}]})()
    assert research.simulation_passed(job)


def test_family_progress_needs_a_fitness_gain_of_at_least_min_gain():
    def run(fitness, sharpe=1.0):
        return {"state": "complete", "alpha_id": None, "metrics": {"fitness": fitness, "sharpe": sharpe}}
    runs = [("a", run(1.0)), ("b", run(1.03)), ("c", run(0.9, sharpe=3.0))]
    roots = {"a": "a", "b": "a", "c": "a", "d": "a"}
    entry = research.family_stats(runs, roots, set())["a"]
    assert entry["since_best"] == 2 and entry["best"][0] == "b"
    entry = research.family_stats(runs + [("d", run(1.03 + research.MIN_GAIN))], roots, set())["a"]
    assert entry["since_best"] == 0 and entry["best"][0] == "d"


def test_negative_sharpe_is_marked_reversed():
    assert research.reversed_note("complete", {"sharpe": -0.71, "fitness": -0.32}) == (
        "REVERSED: the negated expression scores about sharpe=0.71 fitness=0.32")
    assert research.reversed_note("complete", {"sharpe": -0.2, "fitness": -0.1}) == ""
    assert research.reversed_note("failed", {"sharpe": None, "fitness": None}) == ""


def deletes(brain):
    return sum(1 for method, route in brain.calls if method == "DELETE")


def slow(brain, polls):
    real, sims = brain.request, {"count": 0, "left": {}, "peak": 0}

    def request(method, url, **kwargs):
        route = url.removeprefix(BrainClient.origin)
        in_flight = sum(1 for left in sims["left"].values() if left > 0)
        if method == "POST" and route == "/simulations":
            brain.calls.append((method, route))
            sims["count"] += 1
            sims["left"][f"/simulations/p{sims['count']}"] = polls
            sims["peak"] = max(sims["peak"], in_flight + 1)
            return Response(201, headers={"Location": f"/simulations/p{sims['count']}"})
        if route in sims["left"]:
            brain.calls.append((method, route))
            sims["left"][route] -= 1
            if method == "GET" and sims["left"][route] > 0:
                return Response(200, {"status": "RUNNING"})
            return Response(200, {"status": "COMPLETE", "alpha": "A1"})
        return real(method, url, **kwargs)

    brain.request = request
    return sims


def test_queue_runs_one_simulation_at_a_time_with_a_random_gap(brain, monkeypatch, capsys):
    gaps = []
    monkeypatch.setattr(research.random, "uniform", lambda low, high: gaps.append((low, high)) or low)
    sims = slow(brain, polls=10)
    for name in ("c1", "c2", "c3"):
        candidate(name, expression=f"rank({name}x)")
    assert research.main(["queue"]) == 0
    assert "Queue finished: 3 simulated" in capsys.readouterr().out
    assert sims["peak"] == 1
    assert gaps and set(gaps) == {(240, 420)}


def test_queue_waits_when_the_hour_is_full(brain, monkeypatch):
    monkeypatch.setattr(research, "MAX_PER_HOUR", 2)
    for name in ("c1", "c2", "c3"):
        candidate(name, expression=f"rank({name}x)")
    with JobStore(research.path("jobs.db")) as store:
        start = time.time()
        assert research.hour_opens_at(store, start) == start
        assert research.main(["queue"]) == 0
        attempts = sorted(datetime.fromisoformat(job.attempted_at).timestamp() for job in store.all())
        assert research.hour_opens_at(store, attempts[-1]) == attempts[-2] + 3600


def batch_file(*entries):
    research.write(research.path("batch.json"), list(entries))


def ranked(name, reason="edge", **fields):
    return {"id": name, "reason": reason, "expression": f"rank({name}x)",
            "description": {"hypothesis": "h", "implementation": "i"}, "parent": None, "change": "baseline"} | fields


def test_queue_splits_the_batch_and_simulates_best_ranked_first(brain, capsys):
    batch_file(ranked("b"), ranked("a"), ranked("c"))
    assert research.main(["queue", "--max", "2"]) == 0
    out = capsys.readouterr().out
    assert out.index("b  complete") < out.index("a  complete")
    assert "c  waits for the next batch; rank it again or it is set aside  edge" in out
    assert not research.path("batch.json").exists()
    assert research.read(research.path("candidates", "b.json"))["expression"] == "rank(bx)"
    assert "id" not in research.read(research.path("candidates", "b.json"))


def test_next_batch_reranks_or_sets_aside_waiting_candidates(brain, capsys):
    candidate("old1", expression="rank(o1)")
    candidate("old2", expression="rank(o2)")
    batch_file(ranked("new"), {"id": "old2", "reason": "still promising"})
    assert research.main(["queue"]) == 0
    out = capsys.readouterr().out
    assert "old1  set aside  left out of the latest ranked batch" in out
    assert out.index("new  complete") < out.index("old2  complete")
    assert state()["ranking"] == {"new": "edge", "old2": "still promising"}


def test_bad_batch_entries_are_skipped(brain, capsys):
    research.main(["queue"])
    candidate("done")
    research.main(["sim", "done"])
    capsys.readouterr()
    batch_file(ranked("ok"), ranked("ok"), ranked("x", reason=""), {"id": "ghost", "reason": "r"}, ranked("done"))
    assert research.main(["queue"]) == 0
    out = capsys.readouterr().out
    assert "batch entry 2 skipped: ok is listed twice" in out
    assert "batch entry 3 skipped: x: reason must give the expected edge in one line" in out
    assert "batch entry 4 skipped: ghost is not a waiting candidate" in out
    assert "batch entry 5 skipped: done was already simulated" in out
    assert "Queue finished: 1 simulated" in out


def test_learned_daily_limit_stops_before_it(brain, monkeypatch, capsys):
    monkeypatch.setattr(research, "now_et", lambda: datetime.now(research.EASTERN))
    update_state(sim_limit_seen=research.BUDGET_MARGIN + 2)
    for name in ("c1", "c2", "c3"):
        candidate(name, expression=f"rank({name}x)")
    assert research.main(["queue"]) == 3
    out = capsys.readouterr().out
    assert "HOLD" in out and "c3  complete" not in out
    assert sum(1 for method, route in brain.calls if route == "/simulations") == 2


def test_stuck_simulation_is_cancelled_and_retried_once_later(brain, monkeypatch, capsys):
    monkeypatch.setattr(research, "STUCK_AFTER", 0)
    slow(brain, polls=10**6)
    candidate("c1")
    assert research.main(["queue"]) == 0
    assert "c1  failed" in capsys.readouterr().out
    assert deletes(brain) == 1 and state()["stuck_retries"]["c1"]["attempts"] == 1
    assert research.main(["queue"]) == 0
    assert "Queue finished: 0 simulated" in capsys.readouterr().out
    update_state(stuck_retries={"c1": {"attempts": 1, "after": "2026-01-01T00:00:00+00:00"}})
    assert research.main(["queue"]) == 0
    assert "c1  failed" in capsys.readouterr().out
    assert deletes(brain) == 2 and state()["stuck_retries"] == {}


def test_paused_job_past_the_limit_is_cancelled_when_resumed(brain, monkeypatch, capsys):
    real = brain.request
    brain.request = lambda method, url, **kwargs: (
        Response(503) if url.endswith("/simulations/s1") else real(method, url, **kwargs))
    candidate("c1")
    assert research.main(["queue"]) == 3
    assert "PAUSED: c1" in capsys.readouterr().out and deletes(brain) == 0
    monkeypatch.setattr(research, "STUCK_AFTER", 0)
    brain.request = lambda method, url, **kwargs: (
        Response(404, {"detail": "Not found."}) if url.endswith("/simulations/s1") else real(method, url, **kwargs))
    assert research.main(["queue"]) == 0
    assert "c1  failed" in capsys.readouterr().out
    assert "c1" in state()["stuck_retries"]


def test_queue_keeps_candidates_it_already_accepted(brain, capsys):
    candidate("base")
    assert research.main(["sim", "base"]) == 0
    candidate("n1", expression="rank(volume)")
    candidate("v1", expression="ts_rank(close, 5)", parent="base")
    assert research.main(["queue", "--max", "1"]) == 0
    assert research.main(["queue"]) == 0
    out = capsys.readouterr().out
    assert "set aside" not in out and "v1  complete" in out


def test_daily_limit_is_learned_only_above_earlier_days(brain, monkeypatch):
    monkeypatch.setattr(research, "now_et", lambda: datetime.now(research.EASTERN))
    monkeypatch.setattr(research, "MIN_LEARNED_LIMIT", 2)
    with JobStore(research.path("jobs.db")) as store:
        def send(count, days_ago=0):
            for _ in range(count):
                job = store.prepare("rank(close)", {}, BrainClient.origin, "test", True)
                job.submitted_at = (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()
                store.save(job)

        send(2)
        research.note_daily_limit(store)
        assert "sim_limit_seen" not in state()
        send(3, days_ago=1)
        send(1)
        research.note_daily_limit(store)
        assert "sim_limit_seen" not in state()
        send(1)
        research.note_daily_limit(store)
        assert state()["sim_limit_seen"] == 4


def test_old_uncertain_simulation_is_resolved_and_retried(brain, monkeypatch, capsys):
    real = brain.request

    def lost_post(method, url, **kwargs):
        if method == "POST" and url.endswith("/simulations"):
            raise requests.ConnectionError()
        return real(method, url, **kwargs)

    brain.request = lost_post
    candidate("c1")
    assert research.main(["queue"]) == 3
    assert "UNCERTAIN: c1" in capsys.readouterr().out
    brain.request = real
    candidate("c2", expression="rank(open)")
    assert research.main(["queue"]) == 3
    assert "Unresolved jobs above" in capsys.readouterr().out
    monkeypatch.setattr(research, "UNCERTAIN_WAIT", 0)
    assert research.main(["queue"]) == 0
    assert "c1  complete" in capsys.readouterr().out
    assert len(state()["resolved"]) == 1 and state()["stuck_retries"] == {}


def test_unconfirmed_simulation_is_retried_only_once(brain, monkeypatch, capsys):
    posts = []

    def lost_post(method, url, **kwargs):
        posts.append(url)
        raise requests.ConnectionError()

    real = brain.request
    brain.request = lambda method, url, **kwargs: (
        lost_post(method, url) if method == "POST" and url.endswith("/simulations") else real(method, url, **kwargs))
    monkeypatch.setattr(research, "UNCERTAIN_WAIT", 0)
    candidate("c1")
    assert research.main(["queue"]) == 3
    assert research.main(["queue"]) == 3
    for _ in range(3):
        assert research.main(["queue"]) == 0
    assert len(posts) == 2 and state()["stuck_retries"] == {}
    assert "already the retry" in json.dumps(state()["resolved"])


def test_set_aside_moves_a_waiting_candidate(brain):
    candidate("c1")
    assert research.main(["set_aside", "c1", "--reason", "breaks the loop"]) == 0
    assert not research.path("candidates", "c1.json").exists()
    assert research.main(["set_aside", "c1", "--reason", "again"]) == 2


def ran(name, fitness, checks=(), settings=None, **fields):
    candidate(name, **fields)
    data = research.read(research.path("candidates", f"{name}.json"))
    run = {"id": f"job_{name}", "state": "complete", "alpha_id": f"A_{name}", "grade": "INFERIOR",
           "expression": data["expression"], "settings": {"universe": "TOPSP500", "decay": 4, "truncation": 0.08} | (settings or {}),
           "metrics": {"sharpe": fitness, "fitness": fitness, "turnover": 0.1}, "error": None,
           "checks": [{"name": check, "result": "FAIL"} for check in checks],
           "submitted_at": "t0", "completed_at": "t1", "raw_result": None}
    research.write(research.path("runs", f"job_{name}.json"), run)
    update_state(jobs=state()["jobs"] | {f"job_{name}": name})


def lessons_out(capsys):
    capsys.readouterr()
    research.main(["lessons"])
    return capsys.readouterr().out


def test_lesson_tests_are_judged_against_the_parent_and_disprove_it(brain, capsys):
    ran("p", 0.2)
    assert research.main(["lesson", "add", "Raise decay on fundamentals", "--evidence", "p"]) == 0
    test = {"lesson": "L1", "expect": "better"}
    ran("t1", 0.3, parent="p", change="decay up", tests=test)
    assert "L1  holding 1-0" in lessons_out(capsys)
    ran("t2", 0.1, parent="p", change="decay up", tests=test)
    assert "L1  doubtful 1-1" in lessons_out(capsys)
    ran("t3", 0.0, parent="p", change="decay up", tests=test)
    out = lessons_out(capsys)
    assert "Active lessons 0/" in out and "L1  disproved" in out and "contradicted 2 times, supported 1" in out
    candidate("t4", parent="p", tests=test)
    with pytest.raises(ValueError, match="disproved"):
        research.load_candidate("t4")
    assert research.load_candidate("t3")[0]["tests"] == test


def test_lesson_test_needs_a_parent_and_a_known_lesson(brain):
    ran("p", 0.2)
    candidate("t1", tests={"lesson": "L1", "expect": "better"})
    with pytest.raises(ValueError, match="needs a parent"):
        research.load_candidate("t1")
    candidate("t3", parent="seed_alpha", tests={"lesson": "L1", "expect": "better"})
    with pytest.raises(ValueError, match="needs a parent"):
        research.load_candidate("t3")
    candidate("t2", parent="p", tests={"lesson": "L9", "expect": "better"})
    with pytest.raises(ValueError, match="No lesson L9"):
        research.load_candidate("t2")


def test_lesson_needs_simulated_evidence_and_a_free_slot(brain, monkeypatch):
    ran("p", 0.2)
    assert research.main(["lesson", "add", "x", "--evidence", "nope"]) == 2
    monkeypatch.setattr(research, "MAX_LESSONS", 1)
    assert research.main(["lesson", "add", "x", "--evidence", "p"]) == 0
    assert research.main(["lesson", "add", "y", "--evidence", "p"]) == 2
    assert research.main(["lesson", "retire", "L1", "--reason", "wrong"]) == 0
    assert research.main(["lesson", "add", "y", "--evidence", "p"]) == 0
    assert set(research.read_lessons()) == {"L1", "L2"}


def test_judging_counts_a_candidate_once(brain, capsys):
    ran("p", 0.2)
    ran("c", 0.1, parent="p")
    research.main(["lesson", "add", "x", "--evidence", "p"])
    judge = ["lesson", "judge", "L1", "c", "--verdict", "contradicts", "--reason", "worse"]
    assert research.main(judge) == 0
    assert research.main(judge) == 2
    assert "L1  doubtful 0-1" in lessons_out(capsys)


def test_patterns_show_change_effects_datasets_and_fixes(brain, capsys):
    research.write(research.path("catalog", "fields_option8.json"),
                   {"results": [{"id": "pcr", "dataset": {"id": "option8"}}]})
    ran("p", 0.2, checks=["CONCENTRATED_WEIGHT"], expression="rank(pcr)")
    ran("c1", 0.4, parent="p", expression="rank(ts_backfill(pcr, 20))")
    ran("c2", 0.1, checks=["CONCENTRATED_WEIGHT"], parent="p", settings={"decay": 8})
    capsys.readouterr()
    assert research.main(["patterns"]) == 0
    out = capsys.readouterr().out
    assert "+ts_backfill  1  1  +0.20" in out and "decay up  1  0  -0.10" in out
    assert "option8  2  0  0.40" in out and "other  1" in out
    assert "By universe" in out and "TOPSP500  3  0  0.40" in out
    assert "CONCENTRATED_WEIGHT  2  +ts_backfill 1/1, decay up 0/1" in out


def test_history_lists_building_blocks_strongest_first(brain, capsys):
    ran("weak", 0.3, expression="rank(a)")
    ran("strong", 0.9, expression="rank(b)")
    ran("flip", -0.7, expression="rank(c)")
    capsys.readouterr()
    research.main(["history"])
    out = capsys.readouterr().out
    blocks = out.split("Building blocks")[1].splitlines()[1:3]
    assert blocks[0].startswith("strong  as_is  0.9") and blocks[1].startswith("flip  REVERSED  -0.7")
    assert "weak  as_is" not in out


def test_show_prints_details_parent_and_lessons(brain, capsys):
    ran("p", 0.2)
    ran("c", 0.5, parent="p", settings={"decay": 8})
    research.main(["lesson", "add", "x", "--evidence", "c"])
    capsys.readouterr()
    assert research.main(["show", "c"]) == 0
    out = capsys.readouterr().out
    assert "attempt job_c  complete" in out and "changes decay up" in out and "fitness 0.2 -> 0.5" in out
    assert "lesson L1 (untested): evidence" in out
    research.main(["show", "p"])
    assert "children: c fitness=0.5" in capsys.readouterr().out
    assert research.main(["show", "missing"]) == 2


def test_daily_submit_holds_a_good_alpha_for_up_to_two_days(brain, capsys):
    pool("P1", grade="EXCELLENT", pooled_at="2026-10-30T15:00:00+00:00")
    pool("P2", grade="GOOD", fitness=1.9, pooled_at="2026-10-30T15:00:00+00:00")
    pool("P3", grade="GOOD", fitness=1.6, pooled_at="2026-10-28T15:00:00+00:00")
    update_state(pooled_counts={"2026-10-30": research.POOL_TRIGGER})
    assert research.main(["daily_submit"]) == 0
    submitted = [route for method, route in brain.calls if method == "POST" and route.endswith("/submit")]
    assert submitted == ["/alphas/P1/submit", "/alphas/P3/submit"]
    assert research.path("submission_pool", "P2.json").exists()
    assert "P2  waits: family" in capsys.readouterr().out


def test_daily_submit_sends_a_good_alpha_once_its_family_is_exhausted(brain, monkeypatch):
    candidate("c1")
    research.main(["sim", "c1"])
    research.main(["pipeline", "A1"])
    update_state(pooled_counts={"2026-10-30": research.POOL_TRIGGER})
    research.main(["daily_submit"])
    assert research.path("submission_pool", "A1.json").exists()
    monkeypatch.setattr(research, "FAMILY_PATIENCE", 0)
    update_state(hold_until=None)
    research.main(["daily_submit"])
    assert not research.path("submission_pool", "A1.json").exists()
    assert research.read(research.path("successful_alphas.json"))["A1"]["status"] == "submitted"


def test_daily_submit_retires_an_average_alpha_without_asking_brain(brain):
    pool("P1", grade="AVERAGE")
    update_state(pooled_counts={"2026-10-30": research.POOL_TRIGGER})
    assert research.main(["daily_submit"]) == 0
    assert not any(route.startswith("/alphas/P1") for _, route in brain.calls)
    assert research.read(research.path("submission_pool", "removed", "P1.json"))["removed"] == "below_grade"
    assert research.read(research.path("successful_alphas.json"))["P1"]["status"] == "below_grade"


def test_history_lists_pooled_alphas_near_the_next_grade_line(brain, capsys):
    pool("P1", fitness=1.9)
    pool("P2", fitness=1.6)
    research.main(["history"])
    out = capsys.readouterr().out
    assert "c_P1  GOOD  1.9  0.1  rank(P1)" in out
    assert "c_P2" not in out


def test_queue_holds_variants_even_of_a_promising_family_when_no_candidate_uses_new_data(brain, capsys):
    candidate("c1", expression="rank(close)")
    candidate("c2", expression="rank(open)")
    research.main(["queue"])
    candidate("v1", parent="c1", expression="rank(close + 1)")
    candidate("mix", expression="rank(close) + rank(open)")
    capsys.readouterr()
    research.main(["queue"])
    out = capsys.readouterr().out
    assert "v1  waits for a later batch (1/2)  no candidate in this batch uses a data field never simulated" in out
    assert "mix  complete" in out
    candidate("fresh", expression="rank(vwap)")
    research.main(["queue"])
    assert "v1  complete" in capsys.readouterr().out


def test_rejected_submission_records_brains_self_correlation(brain, monkeypatch):
    pool("P1", grade="EXCELLENT")
    update_state(pooled_counts={"2026-10-30": research.POOL_TRIGGER})
    rejected = {"status": "rejected", "failed": ["SELF_CORRELATION"],
                "checks": [{"name": "SELF_CORRELATION", "result": "FAIL", "limit": 0.7, "value": 0.88}]}
    monkeypatch.setattr(research, "submit_alpha", lambda client, alpha_id, **kwargs: rejected)
    assert research.main(["daily_submit"]) == 0
    entry = research.read(research.path("successful_alphas.json"))["P1"]
    assert entry["status"] == "correlated" and entry["self_correlation"] == 0.88


def test_queue_stops_for_the_daily_submission_that_falls_due_while_it_runs(brain, monkeypatch, capsys):
    pool("P1")
    monkeypatch.setattr(research, "now_et", lambda: datetime(
        2026, 10, 30, 23 if ("POST", "/simulations") in brain.calls else 22, 30, tzinfo=research.EASTERN))
    candidate("c1")
    candidate("c2", expression="rank(open)")
    assert research.main(["queue"]) == 3
    assert brain.calls.count(("POST", "/simulations")) == 1
    assert "DAILY_SUBMIT_DUE" in capsys.readouterr().out


def test_complete_run_without_sharpe_is_not_reversed():
    assert research.reversed_note("complete", {"sharpe": None, "fitness": None}) == ""
