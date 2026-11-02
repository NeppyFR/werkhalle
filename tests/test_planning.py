"""make_candidates and its pieces: built from a real state, every result judged the same way, a failed solver costs only itself."""
from __future__ import annotations

import json
import multiprocessing
import pickle
import subprocess
import sys
from concurrent.futures import ProcessPoolExecutor
from dataclasses import replace
from pathlib import Path

import pytest
from test_execution_support import ClosedLoop, engine_at_start, lot, midrun, open_lots_of, order, plan_from
from test_scheduler_support import assert_valid, realistic_lots

from werkhalle.mes import planning
from werkhalle.mes.planning import (
    Candidate,
    CandidateSet,
    assignments_json,
    candidate_json,
    downtimes_json,
    finalize_candidates,
    make_candidates,
    pending_melts_of,
    plan_data_json,
    plan_from_result,
    prepare_problem,
    problem_from_json,
    problem_to_json,
    solve_candidate,
)
from werkhalle.scheduling import (
    Assignment,
    Downtime,
    OrderInfo,
    Schedule,
    build_problem,
    compute_metrics,
    decode_edd,
    retime,
    validate_schedule,
)

LIMIT = 1.0
ASSIGNMENT_KEYS = {"op_id", "kind", "machine", "start", "end", "setup_before", "batch_id", "lot_id", "order_id", "product_id", "state", "frozen"}
CANDIDATE_KEYS = {
    "solver", "label", "status", "valid", "violations", "solve_seconds", "best_bound", "iterations", "note", "metrics", "assignments",
    "downtimes", "twin",
}
METRIC_KEYS = {"makespan", "total_tardiness", "weighted_tardiness", "late_orders", "max_tardiness", "setups", "setup_minutes", "objective", "order_completion"}


@pytest.fixture(scope="module")
def fresh():
    lots, orders = realistic_lots(target=14, seed=81)
    engine = engine_at_start(81)
    state = engine.planning_state()
    candidate_set = make_candidates(state, lots, orders, None, revision=3, melt_counter=7, solver_limit_s=LIMIT, seed=1, workers=4)
    return lots, orders, state, candidate_set


@pytest.fixture(scope="module")
def running():
    """A plant in mid-run on an active plan, with melts still to cast."""
    lots, orders = realistic_lots(target=20, seed=82)
    engine, plan = midrun(lots, orders, 200.0, seed=82, stochastic=True, random_failures=False)
    state = engine.planning_state()
    pending = pending_melts_of(plan, state)
    assert pending, "the point of this fixture is melts that are not cast yet"
    snapshot = engine.export_snapshot()
    open_lots = open_lots_of(lots, snapshot)
    candidate_set = make_candidates(state, open_lots, orders, plan, revision=2, melt_counter=50, solver_limit_s=LIMIT, seed=1, workers=4)
    return engine, plan, state, open_lots, orders, pending, candidate_set


def test_from_a_fresh_state_there_are_cpsat_and_anneal_and_nothing_to_keep(fresh):
    _, _, _, candidate_set = fresh
    assert set(candidate_set.candidates) == {"cpsat", "anneal"}
    for name, candidate in candidate_set.candidates.items():
        assert candidate.valid, (name, candidate.status, candidate.violations[:2], candidate.note)
        assert candidate.solver == name


def test_every_candidate_is_retimed_validated_and_scored(fresh, running):
    for candidate_set in (fresh[3], running[6]):
        problem = candidate_set.problem
        for candidate in candidate_set.candidates.values():
            schedule = candidate.plan.schedule
            assert candidate.plan.problem is problem
            assert retime(problem, schedule).assignments == schedule.assignments, "retiming a retimed plan changes nothing"
            assert candidate.violations == tuple(validate_schedule(problem, schedule)) == ()
            assert candidate.metrics == compute_metrics(problem, schedule)
            assert candidate.status == schedule.status
            assert candidate.solve_seconds == schedule.solve_seconds


def test_plans_carry_what_they_were_made_for(fresh):
    lots, _, state, candidate_set = fresh
    for candidate in candidate_set.candidates.values():
        plan = candidate.plan
        assert plan.revision == 3
        assert plan.source == "manual"
        assert plan.created_min == state.now
        assert plan.lots == tuple(lots)
        assert candidate_set.problem.batch_prefix == "B-r3"
        for a in plan.schedule.assignments:
            if a.batch_id:
                assert a.batch_id.startswith("B-r3-")


def test_new_melts_come_from_the_counter_and_it_moves_on(fresh):
    _, _, _, candidate_set = fresh
    casts = [op.id for op in candidate_set.problem.ops if op.kind == "cast"]
    assert casts
    numbers = sorted(int(op_id.split(":")[0].rsplit("-", 1)[1]) for op_id in casts)
    assert numbers[0] == 7
    assert candidate_set.melt_counter == numbers[-1] + 1


def test_pending_melts_of_the_active_plan_are_reused(running):
    _, _, _, _, _, pending, candidate_set = running
    casts = [op.id[: -len(":cast")] for op in candidate_set.problem.ops if op.kind == "cast" and op.frozen is None]
    assert pending
    assert set(pending) & set(casts) == set(pending), "a melt the plan needs keeps its id"
    assert not any(int(melt.rsplit("-", 1)[1]) >= 50 for melt in casts), "and no new one is needed"
    assert candidate_set.melt_counter >= 50, "the counter never goes back"


def test_pending_melts_of_stops_at_what_has_started():
    lots = [lot(f"L-{i}", "KK-80", 8) for i in range(1, 9)]
    engine = engine_at_start()
    plan = plan_from(engine, lots, [order()])
    state = engine.planning_state()
    all_melts = pending_melts_of(plan, state)
    assert len(all_melts) >= 2
    loop = ClosedLoop(engine, plan)
    loop.dispatch()
    assert pending_melts_of(plan, engine.planning_state()) == all_melts[1:]
    assert pending_melts_of(None, state) == []


def test_keep_carries_the_active_plan_over(running):
    engine, plan, state, _, _, _, candidate_set = running
    keep = candidate_set.candidates["keep"]
    assert keep.valid, keep.violations[:2]
    assert keep.solver == "keep"
    assert "angehängt" in keep.note
    assert not any(a.op_id in state.finished_op_ids for a in keep.plan.schedule.assignments)
    # On every machine the pending ops run in the order the active plan had them.
    for machine, units in plan.sequences.items():
        before = [op_id for unit in units for op_id in unit.op_ids if op_id not in state.started_op_ids]
        carried = [op_id for unit in keep.plan.sequences.get(machine, ()) for op_id in unit.op_ids if op_id not in state.started_op_ids]
        assert [op_id for op_id in carried if op_id in before] == before, machine


def test_cpsat_starting_from_keep_is_at_least_as_good_as_keep(running):
    candidate_set = running[6]
    assert candidate_set.candidates["cpsat"].metrics.objective <= candidate_set.candidates["keep"].metrics.objective


def _spy_on_cpsat(monkeypatch) -> dict:
    seen: dict = {}
    real = planning.solve_cpsat

    def spy(problem, **kwargs):
        seen.update(kwargs)
        return real(problem, **{**kwargs, "time_limit_s": 0.1})

    monkeypatch.setattr(planning, "solve_cpsat", spy)
    return seen


def test_cpsat_is_warm_started_from_the_active_plan(monkeypatch, running):
    _, plan, state, open_lots, orders, _, _ = running
    seen = _spy_on_cpsat(monkeypatch)
    make_candidates(state, open_lots, orders, plan, revision=2, melt_counter=50, solver_limit_s=0.1, workers=3, seed=5)
    assert seen["hint"] is plan.schedule
    assert seen["workers"] == 3 and seen["seed"] == 5


def test_an_explicit_hint_wins_over_the_active_plan(monkeypatch, running):
    from werkhalle.mes.planning import solve_all

    _, plan, _, _, _, _, candidate_set = running
    seen = _spy_on_cpsat(monkeypatch)
    other = candidate_set.candidates["anneal"].plan.schedule
    schedules = solve_all(candidate_set.problem, plan.schedule, limit=0.1, workers=2, hint=other)
    assert seen["hint"] is other
    assert schedules["keep"].solver == "keep", "the plan to keep stays the active one"


def test_with_no_plan_cpsat_gets_no_hint_and_starts_from_its_own_edd(monkeypatch, fresh):
    lots, orders, state, _ = fresh
    seen = _spy_on_cpsat(monkeypatch)
    make_candidates(state, lots, orders, None, revision=1, melt_counter=1, solver_limit_s=0.1, workers=2)
    assert seen["hint"] is None


def test_cpsat_with_no_plan_to_keep_is_at_least_as_good_as_edd(fresh):
    _, _, _, candidate_set = fresh
    edd = compute_metrics(candidate_set.problem, decode_edd(candidate_set.problem))
    assert candidate_set.candidates["cpsat"].metrics.objective <= edd.objective
    assert candidate_set.candidates["anneal"].metrics.objective <= edd.objective


def test_a_fault_in_the_state_becomes_a_downtime_in_every_candidate():
    lots, orders = realistic_lots(target=8, seed=83)
    engine = engine_at_start(83)
    assert engine.inject_fault("PR2", "P-204", 240.0)["ok"]
    candidate_set = make_candidates(engine.planning_state(), lots, orders, None, revision=1, melt_counter=1, solver_limit_s=0.5, workers=4)
    [down] = candidate_set.problem.machine("PR2").downtimes
    assert (down.start, down.end, down.reason) == (0, 240, "P-204")
    for candidate in candidate_set.candidates.values():
        assert candidate.valid
        for a in candidate.plan.schedule.assignments:
            if a.machine == "PR2":
                assert a.start - a.setup_before >= 240


def test_downtimes_and_slowdowns_reach_the_problem():
    lots = [lot("L-1", "ST-200", 4)]
    engine = engine_at_start()
    state = engine.planning_state()
    plain = make_candidates(state, lots, [order()], None, revision=1, melt_counter=1, solver_limit_s=0.2, workers=2)
    down = make_candidates(
        state, lots, [order()], None, revision=1, melt_counter=1, solver_limit_s=0.2, workers=2,
        downtimes={"PR2": [Downtime(0, 100, "Test")]},
    )
    slow = make_candidates(
        state, lots, [order()], None, revision=1, melt_counter=1, solver_limit_s=0.2, workers=2,
        slowdowns=[{"type": "slowdown", "machine": "PR2", "factor": 2.0, "at_offset_min": 0, "duration_min": 1000}],
    )
    assert downtimes_json(down.problem) == [{"machine": "PR2", "start": 0, "end": 100, "reason": "Test"}]
    assert slow.problem.op("L-1:press").durations["PR2"] == 2 * plain.problem.op("L-1:press").durations["PR2"]
    press = next(a for a in down.candidates["anneal"].plan.schedule.assignments if a.op_id == "L-1:press")
    assert press.start >= 100


# ---------------------------------------------------------------------- failures


def _boom(*_args, **_kwargs):
    raise RuntimeError("Absturz im Solver")


def test_a_failed_cpsat_leaves_the_others_usable(monkeypatch, running):
    _, plan, state, open_lots, orders, _, _ = running
    monkeypatch.setattr(planning, "solve_cpsat", _boom)
    candidate_set = make_candidates(state, open_lots, orders, plan, revision=2, melt_counter=50, solver_limit_s=0.3, workers=2)
    failed = candidate_set.candidates["cpsat"]
    assert not failed.valid
    assert failed.status == "no_solution"
    assert "Absturz im Solver" in failed.note and "CP-SAT" in failed.note
    assert failed.metrics is None
    assert failed.plan.schedule.assignments == ()
    assert candidate_set.candidates["anneal"].valid
    assert candidate_set.candidates["keep"].valid
    data = candidate_json(failed)
    assert data["valid"] is False and data["metrics"] is None and data["assignments"] == []


def test_a_failed_anneal_leaves_the_others_usable(monkeypatch):
    lots, orders = realistic_lots(target=6, seed=84)
    engine = engine_at_start(84)
    monkeypatch.setattr(planning, "solve_anneal", _boom)
    candidate_set = make_candidates(engine.planning_state(), lots, orders, None, revision=1, melt_counter=1, solver_limit_s=0.5, workers=2)
    assert candidate_set.candidates["anneal"].status == "no_solution"
    assert "Simulated Annealing" in candidate_set.candidates["anneal"].note
    assert candidate_set.candidates["cpsat"].valid


def test_all_solvers_failing_is_still_an_answer(monkeypatch):
    lots, orders = realistic_lots(target=4, seed=85)
    engine = engine_at_start(85)
    monkeypatch.setattr(planning, "solve_anneal", _boom)
    monkeypatch.setattr(planning, "solve_cpsat", _boom)
    candidate_set = make_candidates(engine.planning_state(), lots, orders, None, revision=1, melt_counter=1, solver_limit_s=0.2)
    assert not any(c.valid for c in candidate_set.candidates.values())
    assert candidate_set.melt_counter >= 1


def test_solve_candidate_never_raises():
    problem = build_problem(engine_at_start().planning_state(), [lot("L-1")], [order()]).problem
    assert solve_candidate(problem, "gibt-es-nicht").status == "no_solution"
    assert "gibt-es-nicht" in solve_candidate(problem, "gibt-es-nicht").note
    without = solve_candidate(problem, "keep")
    assert without.status == "no_solution" and without.solver == "keep"
    assert solve_candidate(problem, "edd").status == "feasible"
    assert solve_candidate(problem, "keep", 0, 1, decode_edd(problem)).status == "feasible"


def test_each_solver_is_reachable_through_solve_candidate(fresh):
    problem = fresh[3].problem
    edd = solve_candidate(problem, "edd")
    assert edd.solver == "edd"
    assert_valid(problem, edd)
    anneal = solve_candidate(problem, "anneal", 0.2, 1)
    assert anneal.solver == "anneal" and anneal.iterations
    assert_valid(problem, anneal)
    cpsat = solve_candidate(problem, "cpsat", 0.5, 1, edd, workers=2)
    assert cpsat.solver == "cpsat"
    assert_valid(problem, cpsat)
    assert compute_metrics(problem, cpsat).objective <= compute_metrics(problem, edd).objective


def test_a_solver_returning_rubbish_gives_a_candidate_marked_invalid(fresh):
    lots, _, state, candidate_set = fresh
    build = _build_of(candidate_set)
    assignments = decode_edd(candidate_set.problem).assignments
    broken = Schedule("cpsat", "feasible", assignments[:-3])
    result = finalize_candidates(build, {"cpsat": broken}, revision=1, lots=lots)
    candidate = result.candidates["cpsat"]
    assert not candidate.valid
    assert any(v.rule == "unassigned" for v in candidate.violations)
    assert candidate.metrics is not None


def test_finalize_retimes_what_a_solver_left_slack_in(fresh):
    lots, _, _, candidate_set = fresh
    build = _build_of(candidate_set)
    schedule = decode_edd(candidate_set.problem)
    slack = tuple(
        replace(a, start=a.start + 500, end=a.end + 500) if candidate_set.problem.op(a.op_id).frozen is None else a
        for a in schedule.assignments
    )
    result = finalize_candidates(build, {"anneal": Schedule("anneal", "feasible", slack)}, revision=1, lots=lots)
    assert result.candidates["anneal"].plan.schedule.assignments == schedule.assignments


def test_finalize_survives_an_exception_while_retiming(fresh):
    lots, _, _, candidate_set = fresh
    build = _build_of(candidate_set)
    unknown_machine_on_known_op = Schedule(
        "anneal", "feasible", (Assignment(candidate_set.problem.ops[0].id, "NIEMAND", 0, 1), Assignment("gibt-es-nicht", "PR1", 0, 1))
    )
    result = finalize_candidates(build, {"anneal": unknown_machine_on_known_op}, revision=1, lots=lots)
    assert not result.candidates["anneal"].valid


def test_finalize_survives_retime_itself_failing(monkeypatch, fresh):
    lots, _, _, candidate_set = fresh
    monkeypatch.setattr(planning, "retime", _boom)
    result = finalize_candidates(_build_of(candidate_set), {"anneal": decode_edd(candidate_set.problem)}, revision=1, lots=lots)
    candidate = result.candidates["anneal"]
    assert candidate.status == "no_solution"
    assert "Absturz im Solver" in candidate.note


def _build_of(candidate_set: CandidateSet):
    from werkhalle.scheduling import BuildResult

    return BuildResult(candidate_set.problem, candidate_set.melt_counter, candidate_set.notes)


# ---------------------------------------------------------------------- JSON


def test_candidate_json_matches_the_api(fresh):
    _, _, _, candidate_set = fresh
    for name, candidate in candidate_set.candidates.items():
        data = candidate_json(candidate, {"makespan": 1, "total_tardiness": 0, "late_orders": 0, "setups": 0, "energy_kwh": 1.0, "unfinished_orders": [], "stalled": False, "speedup": 5})
        assert set(data) == CANDIDATE_KEYS
        assert data["solver"] == name
        assert data["label"] == {"cpsat": "CP-SAT", "anneal": "Simulated Annealing"}[name]
        assert data["status"] in ("optimal", "feasible")
        assert data["valid"] is True
        assert data["violations"] == []
        assert set(data["metrics"]) == METRIC_KEYS
        assert data["twin"]["speedup"] == 5
        json.dumps(data)
        assert len(data["assignments"]) == len(candidate_set.problem.ops)
        for row in data["assignments"]:
            assert set(row) == ASSIGNMENT_KEYS
    assert candidate_json(candidate_set.candidates["cpsat"])["twin"] is None
    assert candidate_json(candidate_set.candidates["anneal"])["iterations"] > 0
    assert candidate_json(candidate_set.candidates["anneal"])["best_bound"] is None


def test_assignment_rows_say_what_the_op_is(running):
    _, _, state, _, _, _, candidate_set = running
    plan = candidate_set.candidates["anneal"].plan
    rows = {row["op_id"]: row for row in assignments_json(plan)}
    casts = [row for row in rows.values() if row["kind"] == "cast"]
    assert casts
    assert all(row["lot_id"] is None and row["order_id"] is None and row["product_id"] is None for row in casts)
    press = next(row for row in rows.values() if row["kind"] == "press" and not row["frozen"])
    source = plan.lots_by_id[press["lot_id"]]
    assert press["order_id"] == source.order_id and press["product_id"] == source.product_id
    frozen = [row for row in rows.values() if row["frozen"]]
    assert frozen
    assert all(row["state"] == "running" for row in frozen)
    assert all(row["state"] == "pending" for row in rows.values() if not row["frozen"])
    furnace = [row for row in rows.values() if row["kind"] == "age" and not row["frozen"]]
    assert all(row["batch_id"] for row in furnace)
    assert {row["state"] for row in assignments_json(plan, lambda _op: "done")} == {"done"}


def test_a_problem_round_trips_through_json(running, fresh):
    for candidate_set in (running[6], fresh[3]):
        problem = candidate_set.problem
        again = problem_from_json(json.loads(json.dumps(problem_to_json(problem))))
        assert again == problem


def test_plan_from_result_rebuilds_a_plan_that_works(running):
    _, _, _, _, _, _, candidate_set = running
    result = {
        "plan_data": plan_data_json(candidate_set, 2, 200.0),
        "keep": candidate_json(candidate_set.candidates["keep"]),
        "candidates": {name: candidate_json(c) for name, c in candidate_set.candidates.items() if name != "keep"},
    }
    result = json.loads(json.dumps(result))
    original = candidate_set.candidates["anneal"].plan
    plan = plan_from_result(result, "anneal", revision=7, source_proposal_id="pr-3")
    assert (plan.revision, plan.source, plan.source_proposal_id) == (7, "proposal", "pr-3")
    assert plan.problem == original.problem
    assert plan.lots == original.lots
    assert plan.schedule.assignments == original.schedule.assignments
    assert plan.sequences == original.sequences
    assert_valid(plan.problem, plan.schedule)
    assert plan_from_result(result, "keep").schedule.solver == "keep"


# ---------------------------------------------------------------------- processes


def test_everything_survives_pickling(running):
    _, _, _, _, _, _, candidate_set = running
    again = pickle.loads(pickle.dumps(candidate_set))
    assert isinstance(again, CandidateSet)
    assert isinstance(again.candidates["keep"], Candidate)
    assert again.candidates["anneal"].plan.sequences == candidate_set.candidates["anneal"].plan.sequences
    assert again.problem == candidate_set.problem


def test_the_two_worker_path_gives_what_make_candidates_gives(running):
    """Pieces in a real spawn pool, as the replan pool runs them: one worker per solver, assembled in the caller."""
    engine, plan, state, open_lots, orders, pending, _ = running
    build = prepare_problem(state, open_lots, orders, plan, revision=2, melt_counter=50)
    assert build.problem.batch_prefix == "B-r2"
    with ProcessPoolExecutor(max_workers=2, mp_context=multiprocessing.get_context("spawn")) as pool:
        cpsat = pool.submit(solve_candidate, build.problem, "cpsat", 1.0, 1, plan.schedule, workers=2)
        anneal = pool.submit(solve_candidate, build.problem, "anneal", 1.0, 1)
        schedules = {"cpsat": cpsat.result(timeout=120), "anneal": anneal.result(timeout=120)}
    schedules["keep"] = solve_candidate(build.problem, "keep", 0.0, 1, plan.schedule)
    result = finalize_candidates(build, schedules, revision=2, lots=open_lots, created_min=state.now, source="proposal")
    assert set(result.candidates) == {"cpsat", "anneal", "keep"}
    for candidate in result.candidates.values():
        assert candidate.valid, candidate.note
        assert candidate.plan.source == "proposal"
    assert result.candidates["cpsat"].metrics.objective <= result.candidates["keep"].metrics.objective


def test_importing_planning_and_twin_loads_no_server_stack():
    """Workers import these first. ortools is imported only when CP-SAT really runs, so the event loop never pays for it."""
    code = (
        "import sys\n"
        "import werkhalle.mes.planning, werkhalle.mes.twin, werkhalle.execution\n"
        "bad = [m for m in ('asyncua', 'fastapi', 'uvicorn', 'werkhalle.plant', 'ortools') if m in sys.modules]\n"
        "assert not bad, bad\n"
    )
    src = Path(__file__).resolve().parent.parent / "src"
    subprocess.run([sys.executable, "-c", code], check=True, env={"PYTHONPATH": str(src), "SYSTEMROOT": _system_root(), "SYSTEMDRIVE": _system_drive()}, timeout=120)


def test_ortools_is_loaded_before_the_annealer_starts_next_to_it():
    """Importing it while the annealer holds the GIL cost 4 s on top of the solver limit, on the first solve of every worker."""
    code = """
import sys
from werkhalle.domain.orders import Lot
from werkhalle.mes import planning
from werkhalle.scheduling import OrderInfo, build_problem
from werkhalle.simcore import SimEngine

real = planning.solve_anneal
seen = []

def spy(problem, **kwargs):
    seen.append("ortools.sat.python.cp_model" in sys.modules)
    return real(problem, **kwargs)

planning.solve_anneal = spy
state = SimEngine(seed=1, stochastic=False, random_failures=False, run_id="x").planning_state()
problem = build_problem(state, [Lot("L-1", "A-1", "FR-60", 2)], [OrderInfo("A-1", 100000)]).problem
assert "ortools" not in sys.modules
planning.solve_all(problem, None, limit=0.1, workers=1)
assert seen == [True], seen
"""
    src = Path(__file__).resolve().parent.parent / "src"
    subprocess.run([sys.executable, "-c", code], check=True, env={"PYTHONPATH": str(src), "SYSTEMROOT": _system_root(), "SYSTEMDRIVE": _system_drive()}, timeout=120)


def _system_root() -> str:
    import os

    return os.environ.get("SYSTEMROOT", r"C:\Windows")


def _system_drive() -> str:
    """Without it, Windows creates a literal %SystemDrive% folder in the child's working directory."""
    import os

    return os.environ.get("SYSTEMDRIVE", "C:")


def test_orders_are_not_mutated_and_order_info_passes_through():
    orders = [OrderInfo("A-1", 500, 3, 400)]
    engine = engine_at_start()
    candidate_set = make_candidates(engine.planning_state(), [lot("L-1", n_billets=2)], orders, None, revision=1, melt_counter=1, solver_limit_s=0.2, workers=2)
    assert candidate_set.problem.orders == tuple(orders)
    assert candidate_set.candidates["anneal"].metrics.order_completion["A-1"] >= 400
