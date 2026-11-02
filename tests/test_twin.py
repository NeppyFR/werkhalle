"""The twin: closed loop on a fork, scenario hooks, variants A, B and C, Monte Carlo, keep against the candidates, and the flagship."""
from __future__ import annotations

import copy
import json
import math
import multiprocessing
import pickle
from concurrent.futures import ProcessPoolExecutor
from dataclasses import replace

import pytest
from test_execution_support import ClosedLoop, engine_at_start, hand_plan, lot, midrun, open_lots_of, order, plan_from
from test_planning import ASSIGNMENT_KEYS, CANDIDATE_KEYS, METRIC_KEYS
from test_scheduler_support import assert_valid, realistic_lots

from werkhalle.domain import catalog
from werkhalle.execution import Tracker, rebase
from werkhalle.mes import planning
from werkhalle.mes.planning import plan_from_result, solve_candidate
from werkhalle.mes.seed import seed_orders
from werkhalle.mes.twin import (
    MAX_FRAMES,
    TwinRun,
    default_horizon_min,
    evaluate_replan,
    evaluate_scenario,
    monte_carlo_percentiles,
    monte_carlo_rows,
    run_assignments_json,
    run_closed_loop,
    scenario_items,
    warm_start_schedule,
)
from werkhalle.scheduling import OrderInfo, build_problem, compute_metrics, decode_edd, validate_schedule
from werkhalle.simcore import SimEngine, planning_state_from_snapshot

LIMIT = 1.0
KPI_KEYS = {"makespan", "total_tardiness", "late_orders", "setups", "energy_kwh", "unfinished_orders", "stalled"}
PR2_DOWN = {"type": "machine_down", "machine": "PR2", "at_offset_min": 0, "duration_min": 240, "code": "P-204"}
RUSH = {"type": "rush_order", "product_id": "KK-80", "qty_bars": 200, "due_in_min": 600}


def book(seed: int = 42):
    seeded = seed_orders(seed)
    lots = [item for entry in seeded for item in entry.lots]
    orders = [OrderInfo(entry.order.id, int(entry.order.due_min), entry.order.weight) for entry in seeded]
    return lots, orders


@pytest.fixture(scope="module")
def plant():
    """The start-up order book in mid-run: the plant the flagship story happens in."""
    lots, orders = book()
    engine, plan = midrun(lots, orders, 400.0, seed=42)
    snapshot = engine.export_snapshot()
    return snapshot, plan, open_lots_of(lots, snapshot), orders


@pytest.fixture(scope="module")
def pr2_down(plant):
    snapshot, plan, open_lots, orders = plant
    return evaluate_scenario(
        snapshot, plan, open_lots, orders, [PR2_DOWN], revision=2, melt_counter=60, solver_limit_s=LIMIT, seed=1, monte_carlo=6, workers=4
    )


@pytest.fixture(scope="module")
def rush_and_down(plant):
    snapshot, plan, open_lots, orders = plant
    return evaluate_scenario(
        snapshot, plan, open_lots, orders, [PR2_DOWN, RUSH], revision=2, melt_counter=60, solver_limit_s=LIMIT, seed=1, workers=4
    )


# ---------------------------------------------------------------------- run_closed_loop


def test_a_run_has_every_field_of_the_contract(plant):
    snapshot, plan, _, _ = plant
    run = run_closed_loop(snapshot, plan)
    assert isinstance(run, TwinRun)
    assert isinstance(run.makespan, int) and run.makespan > 0
    assert isinstance(run.tardiness, int)
    assert isinstance(run.late_orders, int)
    assert isinstance(run.setups, int)
    assert run.energy_kwh > 0
    assert run.unfinished_orders == []
    assert run.stalled is False
    assert run.horizon_hit is False
    assert run.refusals == 0
    assert set(run.order_completion) == {o.id for o in plan.problem.orders}
    assert run.wall_seconds > 0
    assert run.sim_minutes == pytest.approx(run.ended_at - run.started_at)
    assert set(run.kpis()) == KPI_KEYS | {"objective"}
    assert set(run.twin_json()) == KPI_KEYS | {"objective", "speedup"}
    json.dumps(run.kpis())
    assert run.lots_packed == len(plan.lots)


def test_energy_is_what_the_fork_used_not_what_the_plant_used_before(plant):
    snapshot, plan, _, _ = plant
    assert sum(m["counters"]["energy_kwh"] for m in snapshot["machines"].values()) > 1000
    run = run_closed_loop(snapshot, plan)
    fork = SimEngine.from_snapshot(snapshot)
    before = fork.total_energy_kwh()
    assert not ClosedLoop(fork, plan).run().stalled
    assert run.energy_kwh == pytest.approx(fork.total_energy_kwh() - before, abs=0.2)


def test_speedup_is_measured_from_the_run(plant):
    snapshot, plan, _, _ = plant
    run = run_closed_loop(snapshot, plan)
    assert run.speedup == pytest.approx(run.sim_minutes * 60.0 / run.wall_seconds)
    assert run.speedup > 2000, "a twin that is not far faster than real time cannot be used for planning at all"


def test_a_fork_leaves_the_snapshot_untouched(plant):
    snapshot, plan, _, _ = plant
    before = copy.deepcopy(snapshot)
    run_closed_loop(snapshot, plan, scenario=[PR2_DOWN], stochastic=True)
    assert snapshot == before


def test_a_stochastic_fork_has_random_failures_unless_told_otherwise(plant):
    snapshot, plan, _, _ = plant

    def faults(**kwargs) -> int:
        count = 0
        for seed in range(1, 9):
            run = run_closed_loop(snapshot, plan, seed=seed, stochastic=True, **kwargs)
            count += sum(row["type"] == "status" and row["status"] == "FAULT" for row in run.timeline)
        return count

    assert faults() > 0
    assert faults(random_failures=False) == 0


def test_a_deterministic_run_repeats_and_a_stochastic_one_varies_by_seed(plant):
    snapshot, plan, _, _ = plant
    first = run_closed_loop(snapshot, plan, seed=1)
    assert run_closed_loop(snapshot, plan, seed=99).order_completion == first.order_completion, "no randomness in a deterministic fork"
    noisy = [run_closed_loop(snapshot, plan, seed=seed, stochastic=True).order_completion for seed in (1, 2, 3)]
    assert noisy[0] == run_closed_loop(snapshot, plan, seed=1, stochastic=True).order_completion
    assert noisy[0] != noisy[1] or noisy[1] != noisy[2]


def test_twin_kpis_equal_the_plans_own_metrics_on_a_plan_built_at_the_fork():
    """Deterministic, so what the plan promises and what the twin delivers must agree, to the minute."""
    lots, orders = realistic_lots(target=20, seed=91)
    engine, _ = midrun(lots, orders, 321.5, seed=91, stochastic=True, random_failures=False)
    snapshot = engine.export_snapshot()
    state = planning_state_from_snapshot(snapshot)
    open_lots = open_lots_of(lots, snapshot)
    problem = build_problem(state, open_lots, orders).problem
    schedule = decode_edd(problem)
    assert_valid(problem, schedule)
    plan = plan_from(SimEngine.from_snapshot(snapshot), open_lots, orders)
    metrics = compute_metrics(plan.problem, plan.schedule)
    run = run_closed_loop(snapshot, plan)
    assert run.makespan <= metrics.makespan
    assert run.tardiness <= metrics.total_tardiness
    assert run.objective <= metrics.objective
    for order_id, planned in metrics.order_completion.items():
        assert run.order_completion[order_id] <= planned, order_id


def test_an_empty_plan_finishes_at_once():
    engine = engine_at_start()
    snapshot = engine.export_snapshot()
    plan = plan_from(engine, [], [order()])
    run = run_closed_loop(snapshot, plan)
    assert run.makespan == 0 and not run.stalled and not run.unfinished_orders


# ---------------------------------------------------------------------- frames, timeline


def test_frames_every_five_minutes_with_the_api_shape(plant):
    snapshot, plan, _, _ = plant
    run = run_closed_loop(snapshot, plan, record_frames=True, horizon_min=200.0)
    assert len(run.frames) >= 39
    for earlier, later in zip(run.frames, run.frames[1:]):
        assert later["t"] - earlier["t"] == pytest.approx(5.0, abs=0.02)
    assert run.frames[0]["t"] == pytest.approx(snapshot["t"])
    frame = run.frames[10]
    assert set(frame) == {"t", "machines", "material"}
    assert set(frame["machines"]) == set(catalog.MACHINE_ORDER)
    assert set(frame["machines"]["PR2"]) == {"status", "progress", "lot_ids"}
    assert set(frame["material"]) == {"t", "transports", "buffers", "in_machine"}
    assert frame["material"]["t"] == pytest.approx(frame["t"], abs=0.01)
    assert any(f["machines"][m]["lot_ids"] for f in run.frames for m in catalog.MACHINE_ORDER)
    json.dumps(run.frames)


def test_frames_are_capped_at_600(plant):
    snapshot, plan, _, _ = plant
    run = run_closed_loop(snapshot, plan, record_frames=True, frame_step_min=1)
    assert run.sim_minutes > MAX_FRAMES
    assert MAX_FRAMES == 600
    assert len(run.frames) <= 600
    # Thinned, not cut off: the whole run stays in the replay.
    assert run.frames[-1]["t"] >= run.frames[0]["t"] + run.sim_minutes - 2 * (run.frames[1]["t"] - run.frames[0]["t"])


def test_no_frames_unless_asked_and_recording_changes_nothing(plant):
    snapshot, plan, _, _ = plant
    plain = run_closed_loop(snapshot, plan)
    recorded = run_closed_loop(snapshot, plan, record_frames=True)
    assert plain.frames == []
    assert recorded.frames
    assert recorded.order_completion == plain.order_completion
    assert recorded.makespan == plain.makespan
    assert recorded.energy_kwh == pytest.approx(plain.energy_kwh, abs=0.2)
    noisy_plain = run_closed_loop(snapshot, plan, seed=5, stochastic=True)
    noisy_recorded = run_closed_loop(snapshot, plan, seed=5, stochastic=True, record_frames=True)
    assert noisy_plain.finished_at == noisy_recorded.finished_at, "looking at a run must not change it"


def test_the_timeline_gives_every_op_as_it_really_ran(plant):
    snapshot, _, open_lots, orders = plant
    plan = plan_from(SimEngine.from_snapshot(snapshot), open_lots, orders)
    run = run_closed_loop(snapshot, plan)
    rows = run_assignments_json(plan, run)
    assert {row["op_id"] for row in rows} == {op.id for op in plan.problem.ops}
    planned = {a.op_id: a for a in plan.schedule.assignments}
    for row in rows:
        assert set(row) == ASSIGNMENT_KEYS
        assert row["state"] == "done"
        assert row["end"] == pytest.approx(run.finished_at[row["op_id"]], abs=0.01)
        assert row["end"] <= planned[row["op_id"]].end + 0.01
        assert row["machine"] == planned[row["op_id"]].machine
        assert row["frozen"] == (plan.problem.op(row["op_id"]).frozen is not None)
    assert any(row["frozen"] for row in rows)
    assert any(row["setup_before"] > 0 for row in rows)
    assert any(row["batch_id"] for row in rows if row["kind"] == "age")
    loads: dict[str, list[dict]] = {}
    for row in rows:
        if row["kind"] == "age":
            loads.setdefault(row["batch_id"], []).append(row)
    shared = [members for members in loads.values() if len(members) > 1]
    assert shared, "this book batches"
    for members in shared:
        assert sum(1 for row in members if row["setup_before"] > 0) <= 1, "a load is set up once, not once per lot"
    json.dumps(rows)
    statuses = [row for row in run.timeline if row["type"] == "status"]
    assert statuses and all(set(row) >= {"t", "machine", "status"} for row in statuses)


def test_rows_of_a_run_that_did_not_finish_keep_their_planned_times(plant):
    snapshot, plan, _, _ = plant
    run = run_closed_loop(snapshot, plan, horizon_min=100.0)
    rows = {row["op_id"]: row for row in run_assignments_json(plan, run)}
    assert set(rows) == {op.id for op in plan.problem.ops}
    open_rows = [row for row in rows.values() if row["state"] != "done"]
    assert {row["state"] for row in open_rows} == {"pending", "running"}
    for row in open_rows:
        assert row["end"] == next(a.end for a in plan.schedule.assignments if a.op_id == row["op_id"])
    # This plan is older than the fork: what was finished by then is history, and says so.
    before_the_fork = [op_id for op_id in snapshot["finished_op_ids"] if op_id in rows]
    assert before_the_fork
    assert all(rows[op_id]["state"] == "done" for op_id in before_the_fork)


# ---------------------------------------------------------------------- scenario hooks


def _status_rows(run: TwinRun, machine: str) -> list[dict]:
    return [row for row in run.timeline if row["type"] == "status" and row["machine"] == machine]


def test_machine_down_with_an_offset_happens_then_and_for_that_long(plant):
    snapshot, plan, _, _ = plant
    t0 = snapshot["t"]
    run = run_closed_loop(snapshot, plan, scenario=[{"type": "machine_down", "machine": "PR1", "at_offset_min": 30, "duration_min": 100}])
    rows = _status_rows(run, "PR1")
    fault = next(row for row in rows if row["status"] == "FAULT")
    assert fault["t"] == pytest.approx(t0 + 30)
    after = next(row for row in rows if row["t"] > fault["t"])
    assert after["t"] == pytest.approx(t0 + 130)
    assert not run.stalled and not run.unfinished_orders


def test_machine_down_at_offset_zero_on_a_running_machine_takes_effect_at_once(plant):
    snapshot, plan, _, _ = plant
    assert snapshot["machines"]["PR2"]["status"] == "RUN", "this test needs PR2 at work"
    t0 = snapshot["t"]
    run = run_closed_loop(snapshot, plan, scenario=[PR2_DOWN])
    rows = _status_rows(run, "PR2")
    assert rows[1]["status"] == "FAULT" and rows[1]["t"] == pytest.approx(t0), rows[:3]
    assert rows[2]["t"] == pytest.approx(t0 + 240)
    assert rows[2]["status"] == "RUN", "the op on the press carries on after the repair"
    assert not run.stalled and not run.unfinished_orders


def test_machine_down_on_an_idle_machine_keeps_it_from_starting(plant):
    snapshot, plan, _, _ = plant
    assert snapshot["machines"]["VP"]["status"] == "IDLE"
    t0 = snapshot["t"]
    run = run_closed_loop(snapshot, plan, scenario=[{"type": "machine_down", "machine": "VP", "duration_min": 600}])
    packs = [row for row in run.timeline if row["type"] == "op" and row["machine"] == "VP"]
    assert packs and all(row["start"] >= t0 + 600 for row in packs)


def test_machine_down_on_a_machine_already_down_keeps_the_longer_of_both():
    lots, orders = realistic_lots(target=8, seed=92)
    engine, plan = midrun(lots, orders, 100.0, seed=92)
    assert engine.inject_fault("PR2", "P-204", 500.0)["ok"]
    engine.run_until(150.0)
    snapshot = engine.export_snapshot()
    short = run_closed_loop(snapshot, plan, scenario=[{"type": "machine_down", "machine": "PR2", "duration_min": 100}])
    long = run_closed_loop(snapshot, plan, scenario=[{"type": "machine_down", "machine": "PR2", "duration_min": 800}])
    baseline = run_closed_loop(snapshot, plan)

    def back_up(run: TwinRun) -> float:
        rows = _status_rows(run, "PR2")
        return next(row["t"] for row in rows if row["t"] > 150.0 and row["status"] != "FAULT")

    assert back_up(short) == pytest.approx(back_up(baseline)) == pytest.approx(600.0)
    assert back_up(long) == pytest.approx(950.0)


def test_slowdown_stretches_what_runs_in_its_window(plant):
    snapshot, plan, _, _ = plant
    base = run_closed_loop(snapshot, plan)
    slow = run_closed_loop(
        snapshot, plan, scenario=[{"type": "slowdown", "machine": "PR1", "factor": 3.0, "at_offset_min": 0, "duration_min": 2000}]
    )
    assert not slow.stalled and not slow.unfinished_orders
    pr1 = lambda run: max(run.finished_at[op_id] for op_id in run.finished_at if op_id.endswith(":press") and op_id in [a.op_id for a in plan.schedule.assignments if a.machine == "PR1"])
    assert pr1(slow) > pr1(base)


@pytest.mark.parametrize(
    "bad",
    [
        {"type": "machine_down", "machine": "XX", "duration_min": 10},
        {"type": "machine_down", "machine": "PR1", "duration_min": 0},
        {"type": "machine_down", "machine": "PR1", "duration_min": 10, "code": "V-501"},
        {"type": "slowdown", "machine": "XX", "factor": 2, "duration_min": 10},
        {"type": "slowdown", "machine": "PR1", "factor": 0, "duration_min": 10},
        {"type": "rush_order", "product_id": "XX", "qty_bars": 10, "due_in_min": 10},
        {"type": "rush_order", "product_id": "KK-80", "qty_bars": 0, "due_in_min": 10},
        {"type": "rush_order", "product_id": "KK-80", "qty_bars": 10},
        {"type": "meteorite"},
    ],
)
def test_a_scenario_that_cannot_be_honoured_is_refused(bad):
    with pytest.raises(ValueError):
        scenario_items([bad])


# ---------------------------------------------------------------------- unfinished orders


def test_an_unfinished_order_counts_with_the_horizon_as_its_completion(plant):
    snapshot, plan, _, _ = plant
    t0 = snapshot["t"]
    horizon = 300.0
    run = run_closed_loop(snapshot, plan, horizon_min=horizon)
    assert run.horizon_hit
    assert not run.stalled
    assert run.unfinished_orders
    end = int(t0 + horizon)
    for order_id in run.unfinished_orders:
        assert run.order_completion[order_id] == end
    assert run.makespan == end - int(snapshot["t"]) or run.makespan == end - int(snapshot["t"]) - 1
    assert run.sim_minutes == pytest.approx(horizon)
    full = run_closed_loop(snapshot, plan)
    assert not full.unfinished_orders


def test_a_variant_cannot_look_better_by_not_delivering(plant):
    """Stopping early hides nothing: what is late at the horizon is reported as at least that late, and the rank says so."""
    from werkhalle.mes.twin import run_rank

    snapshot, plan, _, _ = plant
    full = run_closed_loop(snapshot, plan)
    cut = run_closed_loop(snapshot, plan, horizon_min=full.sim_minutes * 0.6)
    assert cut.unfinished_orders
    assert run_rank(full) < run_rank(cut), "an unfinished run ranks behind any finished one, however good its figures"


def test_the_default_horizon_leaves_room_for_scenario_and_noise(plant):
    snapshot, plan, _, _ = plant
    base = default_horizon_min(plan)
    assert base >= 2000
    assert default_horizon_min(plan, [PR2_DOWN]) >= base + 480 - 1
    for seed in range(1, 11):
        run = run_closed_loop(snapshot, plan, seed=seed, stochastic=True)
        assert not run.horizon_hit and not run.unfinished_orders


def test_the_horizon_counts_repairs_a_stale_plan_does_not_know_about():
    """The plan is from before the fault. It must still finish in the fork, however long the repair takes."""
    lots, orders = realistic_lots(target=12, seed=97)
    engine, plan = midrun(lots, orders, 100.0, seed=97)
    assert engine.inject_fault("PR2", "P-204", 50000.0)["ok"]
    snapshot = engine.export_snapshot()
    assert default_horizon_min(plan, (), snapshot) >= 2 * 50000.0
    run = run_closed_loop(snapshot, plan)
    assert not run.horizon_hit and not run.stalled and not run.unfinished_orders
    assert run.sim_minutes >= 50000.0


def test_the_horizon_counts_the_work_a_stale_plan_still_holds():
    lots, orders = realistic_lots(target=12, seed=97)
    engine = engine_at_start(97)
    plan = plan_from(engine, lots, orders)
    serial = sum(min(op.durations.values()) for op in plan.problem.ops)
    stale = replace(plan, schedule=replace(plan.schedule, assignments=()))
    assert default_horizon_min(stale) >= serial


def test_a_repair_time_of_100000_minutes_is_no_problem():
    """What the end-to-end test injects. Event driven, so the twin jumps over it, and the horizon grows with it."""
    lots, orders = realistic_lots(target=12, seed=93)
    engine, plan = midrun(lots, orders, 100.0, seed=93)
    assert engine.inject_fault("PR2", "P-204", 100000.0)["ok"]
    result = evaluate_replan(engine.export_snapshot(), plan, lots, orders, revision=2, melt_counter=60, solver_limit_s=0.5, workers=2)
    assert result["trigger"]["machine"] == "PR2"
    assert result["trigger"]["est_repair_min"] == 100000.0
    for candidate in result["candidates"].values():
        assert candidate["valid"]
        [down] = candidate["downtimes"]
        assert down["machine"] == "PR2" and down["end"] - down["start"] >= 100000
        assert not candidate["twin"]["unfinished_orders"]
        assert not candidate["twin"]["stalled"]


# ---------------------------------------------------------------------- evaluate_scenario


def test_the_scenario_result_has_the_shape_of_the_api(pr2_down, plant):
    snapshot, _, _, _ = plant
    result = pr2_down
    for key in ("status", "created_min", "forked_seq", "scenario", "note", "variants", "candidates", "recommended", "delta_b_vs_a", "delta_c_vs_b", "monte_carlo", "applicable"):
        assert key in result, key
    assert result["status"] == "ready"
    assert result["created_min"] == snapshot["t"]
    assert result["forked_seq"] == snapshot["seq"]
    assert result["scenario"] == [PR2_DOWN]
    assert result["applicable"] is True
    assert set(result["variants"]) == {"A", "B", "C"}
    assert result["variants"]["A"]["label"] == "Aktueller Plan, ungestört"
    assert result["variants"]["B"]["label"] == "Aktueller Plan mit Szenario"
    assert result["variants"]["C"]["label"] == "Neuer Plan mit Szenario"
    assert result["variants"]["C"]["solver"] == result["recommended"]
    for variant in result["variants"].values():
        assert set(variant) >= {"label", "kpis", "order_completion", "speedup", "wall_seconds", "sim_minutes", "assignments", "frames"}
        assert set(variant["kpis"]) >= KPI_KEYS
        assert variant["frames"]
        for row in variant["assignments"]:
            assert set(row) == ASSIGNMENT_KEYS
    assert set(result["candidates"]) == {"cpsat", "anneal"}
    for candidate in result["candidates"].values():
        assert set(candidate) == CANDIDATE_KEYS
        assert set(candidate["metrics"]) == METRIC_KEYS
        assert set(candidate["twin"]) >= KPI_KEYS | {"speedup"}
    text = json.dumps(result)
    assert len(text) < 5_000_000
    assert json.loads(text)["recommended"] == result["recommended"]


def test_recommended_is_the_best_by_twin_result_and_deltas_add_up(pr2_down):
    result = pr2_down
    twins = {name: c["twin"] for name, c in result["candidates"].items()}
    best = min(twins.values(), key=lambda t: t["objective"])["objective"]
    assert twins[result["recommended"]]["objective"] == best
    a, b, c = (result["variants"][key]["kpis"] for key in "ABC")
    assert result["delta_b_vs_a"]["makespan"] == b["makespan"] - a["makespan"]
    assert result["delta_b_vs_a"]["total_tardiness"] == b["total_tardiness"] - a["total_tardiness"]
    assert result["delta_c_vs_b"]["makespan"] == c["makespan"] - b["makespan"]
    assert result["delta_c_vs_b"]["total_tardiness"] == c["total_tardiness"] - b["total_tardiness"]
    assert c["objective"] == twins[result["recommended"]]["objective"], "variant C is the recommended candidate run again"


def test_variant_a_is_the_current_plan_undisturbed(pr2_down, plant):
    snapshot, plan, _, _ = plant
    direct = run_closed_loop(snapshot, plan)
    a = pr2_down["variants"]["A"]
    assert a["kpis"]["makespan"] == direct.makespan
    assert a["kpis"]["total_tardiness"] == direct.tardiness
    assert a["order_completion"] == direct.order_completion
    assert not any(f["machines"]["PR2"]["status"] == "FAULT" for f in a["frames"])


def test_variant_b_keeps_the_sequence_and_takes_the_fault(pr2_down):
    b = pr2_down["variants"]["B"]
    assert any(f["machines"]["PR2"]["status"] == "FAULT" for f in b["frames"])
    a = pr2_down["variants"]["A"]
    for machine in catalog.MACHINE_ORDER:
        before = [row["op_id"] for row in sorted(a["assignments"], key=lambda r: r["end"]) if row["machine"] == machine]
        after = [row["op_id"] for row in sorted(b["assignments"], key=lambda r: r["end"]) if row["machine"] == machine]
        assert sorted(before) == sorted(after), machine
        if catalog.MACHINES[machine].kind != catalog.AGE:
            assert before == after, machine


def test_variant_c_is_planned_around_the_downtime(pr2_down):
    for candidate in pr2_down["candidates"].values():
        assert candidate["valid"]
        downs = [d for d in candidate["downtimes"] if d["machine"] == "PR2"]
        assert downs and downs[0]["reason"] == "P-204"
        assert downs[0]["end"] - downs[0]["start"] >= 240
        for row in candidate["assignments"]:
            if row["machine"] == "PR2" and not row["frozen"]:
                assert row["start"] - row["setup_before"] >= downs[0]["end"]


def test_variant_c_is_warm_started_from_variant_b(monkeypatch, plant):
    """So a re-plan knowing the scenario can only be better than sitting it out, however little time the solvers get."""
    snapshot, plan, open_lots, orders = plant
    seen: dict = {}
    real = planning.solve_cpsat

    def spy(problem, **kwargs):
        seen.update(kwargs)
        return real(problem, **{**kwargs, "time_limit_s": 0.1})

    monkeypatch.setattr(planning, "solve_cpsat", spy)
    evaluate_scenario(snapshot, plan, open_lots, orders, [RUSH], revision=2, melt_counter=60, solver_limit_s=0.1, workers=2, record_frames=False)
    assert seen["hint"].solver == "keep"
    assert any(a.op_id.startswith("Z-L-") for a in seen["hint"].assignments), "variant B's plan, with the rush lots at the tails"


def test_no_scenario_means_b_is_a(plant):
    snapshot, plan, open_lots, orders = plant
    result = evaluate_scenario(snapshot, plan, open_lots, orders, [], revision=2, melt_counter=60, solver_limit_s=0.3, workers=2, record_frames=False)
    assert result["variants"]["A"]["kpis"] == result["variants"]["B"]["kpis"]
    assert result["delta_b_vs_a"] == {"makespan": 0, "total_tardiness": 0, "objective": 0}
    assert result["variants"]["A"]["frames"] == []


def test_a_slowdown_scenario_says_it_is_approximate(plant):
    snapshot, plan, open_lots, orders = plant
    scenario = [{"type": "slowdown", "machine": "PR1", "factor": 1.5, "at_offset_min": 0, "duration_min": 300}]
    result = evaluate_scenario(snapshot, plan, open_lots, orders, scenario, revision=2, melt_counter=60, solver_limit_s=0.3, workers=2, record_frames=False)
    assert "näherungsweise" in result["note"]
    assert result["applicable"]
    assert all(c["valid"] for c in result["candidates"].values())
    # Variant C plans with the longer durations; B has to live with the plan it has.
    pressed = [row for row in result["candidates"]["anneal"]["assignments"] if row["machine"] == "PR1" and row["kind"] == "press" and not row["frozen"]]
    assert pressed
    by_id = {item.id: item for item in open_lots}
    for row in pressed:
        item = by_id[row["lot_id"]]
        plain = catalog.planned_minutes("press", catalog.ideal_press_min(item.product, "PR1", item.n_billets))
        assert row["end"] - row["start"] == math.ceil(plain * 1.5 - 1e-9), row["op_id"]


def test_a_scenario_with_no_active_plan_still_works():
    lots, orders = realistic_lots(target=8, seed=94)
    engine = engine_at_start(94)
    result = evaluate_scenario(engine.export_snapshot(), None, lots, orders, [PR2_DOWN], revision=1, melt_counter=1, solver_limit_s=0.3, workers=2, record_frames=False)
    assert result["applicable"]
    assert not result["variants"]["B"]["kpis"]["stalled"]


def test_a_rush_order_on_an_empty_order_book_works():
    engine = engine_at_start()
    result = evaluate_scenario(engine.export_snapshot(), None, [], [], [RUSH], revision=1, melt_counter=1, solver_limit_s=0.3, workers=2, record_frames=False)
    assert result["applicable"]
    assert result["variants"]["A"]["kpis"]["makespan"] == 0
    assert result["variants"]["B"]["kpis"]["makespan"] > 0
    assert not result["variants"]["C"]["kpis"]["unfinished_orders"]


def test_an_unusable_scenario_raises_before_any_work(plant):
    snapshot, plan, open_lots, orders = plant
    with pytest.raises(ValueError):
        evaluate_scenario(snapshot, plan, open_lots, orders, [{"type": "meteorite"}], revision=2, melt_counter=60, solver_limit_s=0.1)


def test_no_valid_new_plan_is_reported_not_raised(monkeypatch, plant):
    def boom(*_args, **_kwargs):
        raise RuntimeError("kaputt")

    snapshot, plan, open_lots, orders = plant
    monkeypatch.setattr(planning, "solve_cpsat", boom)
    monkeypatch.setattr(planning, "solve_anneal", boom)
    result = evaluate_scenario(snapshot, plan, open_lots, orders, [PR2_DOWN], revision=2, melt_counter=60, solver_limit_s=0.1, record_frames=False)
    assert result["applicable"] is False
    assert result["recommended"] is None
    assert result["variants"]["C"] is None
    assert result["delta_c_vs_b"] == {}
    assert result["note"]
    assert result["variants"]["B"]["kpis"]["makespan"] > 0
    json.dumps(result)


def test_monte_carlo_gives_ordered_percentiles(pr2_down):
    monte = pr2_down["monte_carlo"]
    assert monte["n"] == 6
    for variant in ("B", "C"):
        for key in ("makespan", "total_tardiness"):
            row = monte[variant][key]
            assert set(row) == {"p10", "p50", "p90"}
            assert 0 < row["p10"] <= row["p50"] <= row["p90"]
    assert monte["B"]["makespan"]["p10"] < monte["B"]["makespan"]["p90"], "every run has its own seed"


def test_monte_carlo_is_off_by_default_and_percentiles_are_interpolated(rush_and_down):
    assert rush_and_down["monte_carlo"]["n"] == 0
    assert rush_and_down["monte_carlo"]["B"]["makespan"] == {"p10": 0, "p50": 0, "p90": 0}
    rows = [{"makespan": v, "total_tardiness": 0} for v in (10, 20, 30, 40, 50)]
    assert monte_carlo_percentiles(rows)["makespan"] == {"p10": 14.0, "p50": 30.0, "p90": 46.0}
    assert monte_carlo_percentiles([])["makespan"] == {"p10": 0, "p50": 0, "p90": 0}


def test_monte_carlo_rows_vary_with_the_seed_and_repeat_with_it(plant):
    snapshot, plan, _, _ = plant
    rows = monte_carlo_rows(snapshot, plan, [PR2_DOWN], [1, 2, 3, 4])
    assert [row["seed"] for row in rows] == [1, 2, 3, 4]
    assert len({row["makespan"] for row in rows}) > 1
    assert rows == monte_carlo_rows(snapshot, plan, [PR2_DOWN], [1, 2, 3, 4])
    assert all(row["unfinished"] == 0 for row in rows)


# ---------------------------------------------------------------------- rush orders, Z- namespace


def test_machine_down_and_rush_order_together_work(rush_and_down):
    result = rush_and_down
    assert result["applicable"]
    for name, candidate in result["candidates"].items():
        assert candidate["valid"], (name, candidate["violations"][:2], candidate["note"])
        assert not candidate["twin"]["stalled"]
        assert not candidate["twin"]["unfinished_orders"]
    for key in "ABC":
        assert not result["variants"][key]["kpis"]["stalled"]
        assert not result["variants"][key]["kpis"]["unfinished_orders"]
    rush_id = result["rush_orders"][0]["order_id"]
    assert rush_id in result["variants"]["B"]["order_completion"]
    assert rush_id in result["variants"]["C"]["order_completion"]
    assert rush_id not in result["variants"]["A"]["order_completion"]


def test_fork_only_ids_are_in_the_z_namespace(rush_and_down, plant):
    _, plan, open_lots, _ = plant
    result = rush_and_down
    [rush] = result["rush_orders"]
    assert rush["order_id"].startswith("Z-A-")
    assert rush["lot_ids"] and all(lot_id.startswith("Z-L-") for lot_id in rush["lot_ids"])
    real_lots = {item.id for item in open_lots}
    real_ops = {op.id for op in plan.problem.ops}
    rush_lots = set(rush["lot_ids"])
    for name in ("B", "C"):
        rows = result["variants"][name]["assignments"]
        assert {row["lot_id"] for row in rows if row["lot_id"]} >= rush_lots
        for row in rows:
            if row["op_id"] in real_ops:
                continue
            assert row["op_id"].startswith("Z-"), row
        for row in rows:
            if row["batch_id"] and not row["frozen"]:
                assert row["batch_id"].startswith("Z-B-") or row["batch_id"].startswith("B-r1-"), row
        assert not any(row["lot_id"] in rush_lots and row["order_id"] != rush["order_id"] for row in rows)
    a_lots = {row["lot_id"] for row in result["variants"]["A"]["assignments"] if row["lot_id"]}
    assert a_lots <= real_lots


def test_new_melts_for_a_rush_order_are_fork_only():
    """More demand than the plan has melts for: whatever the fork casts in addition has a Z- id, in B and in C."""
    lots = [lot("L-1", "KK-80", 8), lot("L-2", "KK-80", 8)]
    orders = [order()]
    engine = engine_at_start()
    plan = plan_from(engine, lots, orders)
    assert not any(op.kind == "cast" for op in plan.problem.ops)
    result = evaluate_scenario(
        engine.export_snapshot(), plan, lots, orders, [RUSH], revision=2, melt_counter=9, solver_limit_s=0.3, workers=2, record_frames=False
    )
    for name in ("B", "C"):
        casts = [row["op_id"] for row in result["variants"][name]["assignments"] if row["kind"] == "cast"]
        assert casts, name
        assert all(op_id.startswith("Z-M-6063-") for op_id in casts), casts
    candidate_casts = [row["op_id"] for row in result["candidates"]["anneal"]["assignments"] if row["kind"] == "cast"]
    assert all(op_id.startswith("Z-M-") for op_id in candidate_casts)


def test_variant_b_appends_rush_lots_at_machine_and_chain_tails(plant):
    from werkhalle.mes.twin import _current_plan, _fork_melts, _rush_of

    snapshot, plan, open_lots, orders = plant
    state = planning_state_from_snapshot(snapshot)
    scenario = scenario_items([RUSH])
    rush_lots, rush_orders, _ = _rush_of(scenario, state.now)
    all_lots = [*open_lots, *rush_lots]
    b = _current_plan(
        state, all_lots, [*orders, *rush_orders], plan, revision=2, melt_counter=60, melts=_fork_melts(all_lots, 60), prefix="Z-B-B", rush_orders=rush_orders
    )
    assert_valid(b.problem, b.schedule)
    rush_ids = {item.id for item in rush_lots}
    for machine, units in b.sequences.items():
        kinds = [any(b.problem.op(op_id).lot_id in rush_ids for op_id in unit.op_ids) for unit in units]
        if any(kinds):
            first = kinds.index(True)
            assert all(kinds[first:]), f"{machine}: nothing of the plan may come after a rush lot"
    # Lots of the plan keep the melts they had: a rush lot ranks last for supply.
    for item in open_lots:
        press = item.op_id("press")
        if b.problem.has_op(press) and b.problem.op(press).frozen is None:
            before = {p.op_id for p in plan.problem.op(press).preds}
            after = {p.op_id for p in b.problem.op(press).preds}
            assert not {op_id for op_id in after - before if op_id.startswith("Z-")}, item.id
    gus = [op_id for unit in b.sequences.get("GUSS", ()) for op_id in unit.op_ids]
    z_casts = [i for i, op_id in enumerate(gus) if op_id.startswith("Z-")]
    assert z_casts, "this rush order needs more melts"
    assert z_casts == list(range(len(gus) - len(z_casts), len(gus))), "fork-only melts at the tail of the chain"
    assert all(o.due != 1 << 60 for o in b.problem.orders), "the ranking trick must not leak into the metrics"


def test_a_rush_lot_takes_no_billets_from_lots_ahead_of_it_in_variant_b():
    """Three lots use up the 6063 store exactly. By due date the rush lot would rank first and leave L-3 waiting for a melt."""
    from werkhalle.mes.twin import _current_plan, _fork_melts, _rush_of

    lots = [lot("L-1", "KK-80", 8), lot("L-2", "KK-80", 8), lot("L-3", "KK-80", 8)]
    orders = [order()]
    engine = engine_at_start()
    plan = plan_from(engine, lots, orders)
    state = engine.planning_state()
    rush_lots, rush_orders, _ = _rush_of(scenario_items([RUSH]), state.now)
    all_lots = [*lots, *rush_lots]
    b = _current_plan(state, all_lots, [*orders, *rush_orders], plan, revision=2, melt_counter=9, melts=_fork_melts(all_lots, 9), prefix="Z-B-B", rush_orders=rush_orders)
    for item in lots:
        assert b.problem.op(item.op_id("press")).preds == (), item.id
    assert all(any(p.op_id.startswith("Z-M-") for p in b.problem.op(item.op_id("press")).preds) for item in rush_lots[1:])
    assert {o.id: o.due for o in b.problem.orders}[rush_orders[0].id] == rush_orders[0].due


def test_rush_order_is_tracked_as_weighted_in_the_objective():
    lots = [lot("L-1", "KK-80", 4)]
    orders = [order()]
    engine = engine_at_start()
    plan = plan_from(engine, lots, orders)
    tight = {"type": "rush_order", "product_id": "KK-80", "qty_bars": 40, "due_in_min": 1}
    result = evaluate_scenario(engine.export_snapshot(), plan, lots, orders, [tight], revision=2, melt_counter=9, solver_limit_s=0.2, workers=2, record_frames=False)
    b = result["variants"]["B"]["kpis"]
    assert b["late_orders"] == 1
    assert b["objective"] >= 10 * 3 * b["total_tardiness"], "weight 3, tardiness weight 10"


def test_warm_start_maps_fork_lots_and_drops_what_only_existed_in_the_fork(plant):
    snapshot, plan, open_lots, orders = plant
    result = evaluate_scenario(snapshot, plan, open_lots, orders, [RUSH], revision=2, melt_counter=60, solver_limit_s=0.3, workers=2, record_frames=False)
    [rush] = result["rush_orders"]
    mapping = {fork_id: f"L-9{i:04d}" for i, fork_id in enumerate(rush["lot_ids"])}
    solver = result["recommended"]
    schedule = warm_start_schedule(result, solver, mapping)
    ids = [a.op_id for a in schedule.assignments]
    assert not any(op_id.startswith("Z-") for op_id in ids)
    assert not any((a.batch_id or "").startswith("Z-") for a in schedule.assignments)
    assert all(f"{real}:press" in ids for real in mapping.values())
    # Step two of the apply path: the same order for real, live state, no hypothetical downtime.
    state = planning_state_from_snapshot(snapshot)
    real_lots = [
        replace(item, id=mapping[item.id], order_id="A-RUSH")
        for item in [catalog_lot for catalog_lot in _rush_lots(rush)]
    ]
    all_orders = [*orders, OrderInfo("A-RUSH", rush["due_min"], 3)]
    problem = build_problem(state, [*open_lots, *real_lots], all_orders, melt_counter=200, batch_prefix="B-r3").problem
    carried = solve_candidate(problem, "keep", 0.0, 1, schedule)
    assert carried.status == "feasible", carried.note
    assert_valid(problem, carried)
    without_map = warm_start_schedule(result, solver)
    assert not any(a.op_id.startswith("Z-") for a in without_map.assignments)
    assert not any(a.op_id.split(":")[0] in mapping for a in without_map.assignments)


def _rush_lots(rush: dict):
    from werkhalle.mes.twin import _rush_of

    lots, _, _ = _rush_of(scenario_items([{"type": "rush_order", "product_id": rush["product_id"], "qty_bars": rush["qty_bars"], "due_in_min": 0}]), 0.0)
    return lots


# ---------------------------------------------------------------------- evaluate_replan


@pytest.fixture(scope="module")
def faulted():
    lots, orders = book()
    engine, plan = midrun(lots, orders, 400.0, seed=42)
    assert engine.inject_fault("PR2", "P-204", 240.0)["ok"]
    engine.run_until(401.0)
    snapshot = engine.export_snapshot()
    open_lots = open_lots_of(lots, snapshot)
    result = evaluate_replan(snapshot, plan, open_lots, orders, revision=2, melt_counter=60, solver_limit_s=LIMIT, seed=1, workers=4)
    return snapshot, plan, open_lots, orders, result


def test_the_replan_result_has_the_fields_of_a_proposal(faulted):
    snapshot, plan, _, _, result = faulted
    for key in ("trigger", "based_on_revision", "keep", "candidates", "recommended", "beats_keep", "summary", "delta", "snapshot_seq", "melt_counter", "plan_data"):
        assert key in result, key
    assert result["based_on_revision"] == plan.revision
    assert result["snapshot_seq"] == snapshot["seq"]
    assert set(result["candidates"]) == {"cpsat", "anneal"}
    assert result["keep"]["solver"] == "keep"
    assert set(result["keep"]) == CANDIDATE_KEYS
    assert result["keep"]["twin"] is not None
    assert result["recommended"] in ("cpsat", "anneal")
    json.dumps(result)


def test_the_trigger_comes_from_the_snapshot(faulted):
    snapshot, _, _, _, result = faulted
    assert result["trigger"] == {
        "kind": "fault",
        "machine": "PR2",
        "code": "P-204",
        "text": "Hauptzylinder undicht",
        "est_repair_min": 240.0,
        "t": pytest.approx(400.0),
    }


def test_a_given_trigger_is_passed_through(faulted):
    snapshot, plan, open_lots, orders, _ = faulted
    trigger = {"kind": "stall", "machine": None, "code": "", "text": "", "est_repair_min": 0, "t": snapshot["t"]}
    result = evaluate_replan(snapshot, plan, open_lots, orders, revision=2, melt_counter=60, solver_limit_s=0.2, workers=2, trigger=trigger)
    assert result["trigger"] == trigger


def test_every_faulted_machine_is_a_downtime_in_the_plans(faulted):
    snapshot, plan, open_lots, orders, _ = faulted
    engine = SimEngine.from_snapshot(snapshot)
    assert engine.inject_fault("OF2", "O-301", 150.0)["ok"]
    engine.run_until(engine.t + 2.0)
    result = evaluate_replan(engine.export_snapshot(), plan, open_lots, orders, revision=2, melt_counter=60, solver_limit_s=0.3, workers=2)
    for candidate in result["candidates"].values():
        machines = {d["machine"] for d in candidate["downtimes"]}
        assert {"PR2", "OF2"} <= machines
    assert result["trigger"]["machine"] == "OF2", "the newest fault is what set this off"


def test_a_candidate_beating_keep_is_said_plainly_and_with_the_right_delta(faulted):
    _, _, _, _, result = faulted
    assert result["beats_keep"] is True
    best = result["candidates"][result["recommended"]]["twin"]
    keep = result["keep"]["twin"]
    assert best["objective"] < keep["objective"]
    assert result["delta"] == {
        "makespan": best["makespan"] - keep["makespan"],
        "total_tardiness": best["total_tardiness"] - keep["total_tardiness"],
        "late_orders": best["late_orders"] - keep["late_orders"],
        "setups": best["setups"] - keep["setups"],
    }
    assert result["summary"].startswith("Neuplanung"), result["summary"]
    assert "gegenüber" in result["summary"] or "Gesamtdauer" in result["summary"] or "Rüstzeit" in result["summary"]


def test_accepting_the_recommended_candidate_goes_through_rebase_and_runs(faulted):
    snapshot, _, open_lots, orders, result = faulted
    wire = json.loads(json.dumps(result))
    plan = plan_from_result(wire, wire["recommended"], revision=2, source_proposal_id="pr-1")
    assert plan.source == "proposal"
    assert plan.source_proposal_id == "pr-1"
    assert_valid(plan.problem, plan.schedule)
    fork = SimEngine.from_snapshot(snapshot)
    tracker = Tracker.from_state(planning_state_from_snapshot(snapshot))
    rebased = rebase(plan, tracker, [item.id for item in open_lots], now=snapshot["t"])
    assert rebased.plan is not None, rebased.reason
    assert_valid(rebased.plan.problem, rebased.plan.schedule)
    loop = ClosedLoop(fork, rebased.plan)
    run = loop.run()
    assert not run.stalled and not run.refusals
    assert sum(1 for op_id in loop.finished_at if op_id.endswith(":pack")) == len(open_lots)


def test_nothing_beating_keep_is_said_plainly_and_can_still_be_accepted():
    """One lot on a free plant: nothing beats the plan already there."""
    lots = [lot("L-1", "FR-60", 4)]
    orders = [order("A-1", 100000)]
    engine = engine_at_start()
    plan = plan_from(engine, lots, orders)
    snapshot = engine.export_snapshot()
    result = evaluate_replan(snapshot, plan, lots, orders, revision=2, melt_counter=1, solver_limit_s=0.2, workers=2)
    assert result["beats_keep"] is False
    assert result["summary"] == "Kein Kandidat ist besser als der aktuelle Plan. Der beste geprüfte Kandidat lässt sich trotzdem übernehmen."
    assert result["recommended"] is not None
    assert result["delta"]["total_tardiness"] == 0
    accepted = plan_from_result(result, result["recommended"])
    assert_valid(accepted.problem, accepted.schedule)
    assert not validate_schedule(accepted.problem, accepted.schedule)
    assert not ClosedLoop(engine, accepted).run().stalled


def test_with_no_plan_to_keep_any_valid_candidate_wins():
    lots, orders = realistic_lots(target=6, seed=95)
    engine = engine_at_start(95)
    result = evaluate_replan(engine.export_snapshot(), None, lots, orders, revision=1, melt_counter=1, solver_limit_s=0.2, workers=2)
    assert result["keep"] is None
    assert result["based_on_revision"] is None
    assert result["beats_keep"] is True
    assert result["delta"] is None
    assert "keinen aktuellen Plan" in result["summary"]
    assert result["recommended"]


def test_with_no_valid_candidate_there_is_nothing_to_recommend(monkeypatch):
    def boom(*_args, **_kwargs):
        raise RuntimeError("kaputt")

    lots, orders = realistic_lots(target=6, seed=96)
    engine = engine_at_start(96)
    plan = plan_from(engine, lots, orders)
    monkeypatch.setattr(planning, "solve_cpsat", boom)
    monkeypatch.setattr(planning, "solve_anneal", boom)
    result = evaluate_replan(engine.export_snapshot(), plan, lots, orders, revision=2, melt_counter=9, solver_limit_s=0.2)
    assert result["recommended"] is None
    assert result["beats_keep"] is False
    assert result["delta"] is None
    assert "Kein gültiger Kandidat" in result["summary"]
    assert result["keep"]["valid"]
    assert result["keep"]["twin"] is not None, "what the plant would run anyway is still worth a look"
    assert all(candidate["twin"] is None for candidate in result["candidates"].values())


def test_a_keep_plan_that_stalls_loses_to_one_that_finishes(plant):
    """keep with a dropped melt can only stall. Delivering outranks any objective."""
    from werkhalle.mes.twin import _replan_summary

    snapshot, plan, _, _ = plant
    finished = run_closed_loop(snapshot, plan)
    cut = run_closed_loop(snapshot, plan, horizon_min=10.0)
    summary, delta = _replan_summary(finished, cut, True, "CP-SAT")
    assert summary == "Der aktuelle Plan wird nicht fertig, die Neuplanung liefert alle Aufträge."
    assert delta["makespan"] == finished.makespan - cut.makespan


def _figures(**overrides) -> TwinRun:
    base = dict(
        makespan=1000, tardiness=100, late_orders=2, setups=10, energy_kwh=1.0, order_completion={}, unfinished_orders=[], stalled=False,
        wall_seconds=0.1, sim_minutes=1000.0, speedup=1.0, weighted_tardiness=100, setup_minutes=100, objective=2200,
    )
    return TwinRun(**{**base, **overrides})


@pytest.mark.parametrize(
    "better, expected",
    [
        (dict(tardiness=60, weighted_tardiness=60), "Neuplanung spart 40 min Verspätung gegenüber dem aktuellen Plan."),
        (dict(tardiness=110, weighted_tardiness=70), "Neuplanung senkt die nach Priorität gewichtete Verspätung um 30 min."),
        (dict(makespan=950), "Neuplanung verkürzt die Gesamtdauer um 50 min, die Verspätung bleibt gleich."),
        (dict(makespan=900, tardiness=105, weighted_tardiness=100), "Neuplanung verkürzt die Gesamtdauer um 100 min, bei 5 min mehr Verspätung."),
        (dict(setup_minutes=70), "Neuplanung spart 30 min Rüstzeit gegenüber dem aktuellen Plan."),
        (dict(objective=2190), "Neuplanung verbessert die Zielfunktion um 10 Punkte."),
    ],
)
def test_the_summary_names_what_really_got_better(better, expected):
    from werkhalle.mes.twin import _replan_summary

    summary, delta = _replan_summary(_figures(**better), _figures(), True, "CP-SAT")
    assert summary == expected
    assert set(delta) == {"makespan", "total_tardiness", "late_orders", "setups"}


# ---------------------------------------------------------------------- the flagship


@pytest.mark.parametrize("seed, fork_at", [(42, 400.0), (42, 150.0), (7, 400.0), (3, 300.0), (11, 500.0)])
def test_flagship_pr2_down_makes_b_worse_than_a_and_c_at_least_as_good_as_b(seed, fork_at):
    """The demonstration the whole twin exists for, on the start-up order book at a state the plant really reaches."""
    lots, orders = book(seed)
    engine, plan = midrun(lots, orders, fork_at, seed=seed)
    snapshot = engine.export_snapshot()
    open_lots = open_lots_of(lots, snapshot)
    result = evaluate_scenario(
        snapshot, plan, open_lots, orders, [PR2_DOWN], revision=2, melt_counter=60, solver_limit_s=LIMIT, seed=1, workers=4, record_frames=False
    )
    a, b, c = (result["variants"][key]["kpis"] for key in "ABC")
    assert b["objective"] > a["objective"], (seed, fork_at, a, b)
    assert c["objective"] <= b["objective"], (seed, fork_at, b, c)
    assert b["makespan"] >= a["makespan"]
    assert result["delta_b_vs_a"]["objective"] > 0
    assert result["delta_c_vs_b"]["objective"] <= 0
    for kpis in (a, b, c):
        assert not kpis["stalled"] and not kpis["unfinished_orders"]


def test_flagship_impact_is_real_in_the_numbers(pr2_down):
    """Not just "not worse": four hours without PR2 costs this plan real lateness and hours at the end."""
    a, b, c = (pr2_down["variants"][key]["kpis"] for key in "ABC")
    assert b["total_tardiness"] > a["total_tardiness"] + 100
    assert b["late_orders"] >= a["late_orders"]
    assert b["makespan"] > a["makespan"] + 60
    assert c["objective"] <= b["objective"]


def test_flagship_also_shows_up_with_noise(pr2_down):
    """With noise and random faults the re-plan does not fall apart. The margin is for the few seeds and a solver on a busy machine."""
    monte = pr2_down["monte_carlo"]
    assert monte["C"]["total_tardiness"]["p50"] <= 1.1 * monte["B"]["total_tardiness"]["p50"]


# ---------------------------------------------------------------------- process pools


def test_results_survive_pickling(plant):
    snapshot, plan, _, _ = plant
    run = run_closed_loop(snapshot, plan, record_frames=True, horizon_min=50.0)
    again = pickle.loads(pickle.dumps(run))
    assert again.frames == run.frames
    assert pickle.loads(pickle.dumps(plan)).sequences == plan.sequences


def test_the_twin_jobs_run_in_a_real_spawn_pool(plant, faulted):
    snapshot, plan, open_lots, orders = plant
    with ProcessPoolExecutor(max_workers=3, mp_context=multiprocessing.get_context("spawn")) as pool:
        run = pool.submit(run_closed_loop, snapshot, plan, scenario=[PR2_DOWN])
        scenario = pool.submit(
            evaluate_scenario, snapshot, plan, open_lots, orders, [PR2_DOWN], revision=2, melt_counter=60, solver_limit_s=0.5, workers=2, monte_carlo=2, record_frames=False,
        )
        replan = pool.submit(
            evaluate_replan, faulted[0], plan, open_lots, orders, revision=2, melt_counter=60, solver_limit_s=0.5, workers=2,
        )
        rows = pool.submit(monte_carlo_rows, snapshot, plan, [PR2_DOWN], [1, 2])
        assert not run.result(timeout=180).stalled
        assert scenario.result(timeout=180)["applicable"]
        assert replan.result(timeout=180)["recommended"]
        assert len(rows.result(timeout=180)) == 2


# ---------------------------------------------------------------------- how a run ends


def test_a_stall_ends_the_run_even_with_a_scenario_event_still_ahead():
    """Nothing can start, nothing runs: a slowdown due much later changes nothing, so the run stops at the stall, not then."""
    engine = engine_at_start()
    plan = hand_plan(engine, [lot("L-1", "FR-60", 8)], [order()], {"OF1": [["L-1:age"]]}, drop_ops=("L-1:press",))
    late = {"type": "slowdown", "machine": "SAE", "factor": 2.0, "at_offset_min": 4000, "duration_min": 10}
    run = run_closed_loop(engine.export_snapshot(), plan, scenario=[late], horizon_min=9000.0)
    assert run.stalled
    assert run.sim_minutes < 1.0
    assert run.unfinished_orders == ["A-1"]


def test_a_command_the_plant_keeps_refusing_ends_as_a_stall_not_as_a_hang():
    """The plan puts ST-200 on PR1, which the plant does not allow. The tracker cannot know that, the engine refuses."""
    engine = engine_at_start()
    plan = hand_plan(engine, [lot("L-1", "ST-200", 2)], [order()], {"PR1": [["L-1:press"]]})
    run = run_closed_loop(engine.export_snapshot(), plan, horizon_min=1000.0)
    assert run.stalled
    assert run.refusals == 1
    assert run.unfinished_orders == ["A-1"]


def test_a_refused_command_is_tried_again_after_the_next_event(monkeypatch):
    """The fork's store is empty, the tracker's is not. So the press is commanded and refused, and goes through once a melt is in."""
    engine = engine_at_start()
    assert engine.start_operation(
        {"machine": "GUSS", "run_id": engine.run_id, "op_ids": ["M-6060-00001:cast"], "kind": "cast", "melt_plan_id": "M-6060-00001", "alloy": "6060"}
    )["ok"]
    engine.run_until(5.0)
    lots = [lot("L-1", "FR-60", 8)]
    plan = plan_from(engine, lots, [order()])
    snapshot = engine.export_snapshot()
    real = copy.deepcopy(snapshot)
    real["billet_store"]["6060"] = []
    original = SimEngine.from_snapshot
    monkeypatch.setattr(SimEngine, "from_snapshot", staticmethod(lambda _snapshot, **kwargs: original(real, **kwargs)))
    run = run_closed_loop(snapshot, plan)
    assert run.refusals >= 1
    assert not run.stalled
    assert run.lots_packed == 1


@pytest.mark.parametrize("item", [
    {"type": "machine_down", "machine": "PR2", "duration_min": float("nan")},
    {"type": "machine_down", "machine": "PR2", "duration_min": 1e12},
    {"type": "machine_down", "machine": "PR2", "duration_min": 10, "at_offset_min": -500},
    {"type": "machine_down", "machine": ["PR2"], "duration_min": 10},
    {"type": "slowdown", "machine": "PR2", "factor": 1.0, "duration_min": 10},
    {"type": "slowdown", "machine": "PR2", "factor": float("inf"), "duration_min": 10},
    {"type": "rush_order", "product_id": ["KK-80"], "qty_bars": 20, "due_in_min": 4000},
    {"type": "rush_order", "product_id": "KK-80", "qty_bars": 20.5, "due_in_min": 4000},
    {"type": "rush_order", "product_id": "KK-80", "qty_bars": 20, "due_in_min": "soon"},
    {"type": "rush_order", "product_id": "KK-80", "qty_bars": 20, "due_in_min": 1},
    {"type": "rush_order", "product_id": "KK-80", "qty_bars": 100000, "due_in_min": 4000},
    "kaputt",
])
def test_api_scenario_items_refuse_what_post_orders_refuses(item):
    from werkhalle.mes.twin import api_scenario_items

    with pytest.raises(ValueError):
        api_scenario_items([item])


def test_scenario_items_keep_only_known_keys():
    from werkhalle.mes.twin import scenario_items

    [item] = scenario_items([{"type": "machine_down", "machine": "PR2", "duration_min": 10, "deep": [[[]]]}])
    assert "deep" not in item
