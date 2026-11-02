"""Closed loop without OPC UA: real plans on the real engine, to the end, with noise, with faults, and from many forks."""
from __future__ import annotations

import pytest
from test_execution_support import (
    ClosedLoop,
    engine_at_start,
    hand_plan,
    lot,
    midrun,
    open_lots_of,
    order,
    plan_from,
    sync,
    views_of,
)
from test_scheduler_support import realistic_lots
from test_simcore_support import assert_conserved

from werkhalle.domain import catalog
from werkhalle.execution import Tracker, billet_shortfall, plan_finished, stalled
from werkhalle.mes.twin import run_closed_loop
from werkhalle.scheduling import compute_metrics, decode_edd, solve_anneal, solve_cpsat
from werkhalle.simcore import SimEngine


def assert_all_through(loop: ClosedLoop, lots, *, complete: bool = True) -> None:
    packed = [op_id for op_id in loop.finished_at if op_id.endswith(":pack")]
    assert sorted(packed) == sorted(item.op_id("pack") for item in lots)
    started = [op_id for command in loop.commands for op_id in command["op_ids"]]
    assert len(started) == len(set(started)), "an op was started twice"


# ---------------------------------------------------------------------- whole runs


@pytest.mark.parametrize("seed", [21, 22, 23])
def test_twenty_lots_run_to_the_end_deterministically(seed):
    lots, orders = realistic_lots(target=20, seed=seed)
    engine = engine_at_start(seed)
    plan = plan_from(engine, lots, orders)
    loop = ClosedLoop(engine, plan)
    result = loop.run()
    assert not result.stalled and not result.refusals
    assert_all_through(loop, lots)
    assert set(loop.finished_at) == {op.id for op in plan.problem.ops}
    while (when := engine.next_event_time()) is not None:
        engine.run_until(when)
    sync(engine, loop.tracker)
    assert_conserved(engine.events_after(0, 100_000)["events"], lots, engine, complete=True)


def test_twenty_lots_run_to_the_end_with_noise_and_random_faults():
    """Many seeds, because a fault is not certain on any one of them: about one per 2500 operating minutes per machine."""
    faults = 0
    for seed in range(31, 61):
        lots, orders = realistic_lots(target=20, seed=seed)
        engine = engine_at_start(seed, True)
        plan = plan_from(engine, lots, orders)
        loop = ClosedLoop(engine, plan)
        result = loop.run()
        assert not result.stalled and not result.refusals, seed
        assert_all_through(loop, lots)
        faults += sum(engine.machine_view(m)["FaultCount"] for m in catalog.MACHINES)
        while (when := engine.next_event_time()) is not None:
            engine.run_until(when)
        assert_conserved(engine.events_after(0, 100_000)["events"], lots, engine, complete=True)
    assert faults >= 20, "these runs have to meet faults, or this test tests less than it says"


@pytest.mark.parametrize("machine, code, repair", [("PR2", "P-204", 400.0), ("OF2", "O-301", 500.0), ("GUSS", "G-102", 300.0), ("SAE", "S-401", 200.0), ("VP", "V-501", 100.0)])
@pytest.mark.parametrize("stochastic", [False, True])
def test_a_machine_going_down_for_long_does_not_stall_the_plan(machine, code, repair, stochastic):
    lots, orders = realistic_lots(target=20, seed=41)
    engine = engine_at_start(41, stochastic, random_failures=False)
    plan = plan_from(engine, lots, orders)
    loop = ClosedLoop(engine, plan)
    assert not loop.run(until=150.0).stalled
    assert engine.inject_fault(machine, code, repair)["ok"]
    result = loop.run()
    assert not result.stalled and not result.refusals
    assert_all_through(loop, lots)


def test_machines_down_at_the_start_and_in_the_middle():
    lots, orders = realistic_lots(target=20, seed=42)
    engine = engine_at_start(42)
    assert engine.inject_fault("PR1", "P-201", 300.0)["ok"]
    plan = plan_from(engine, lots, orders)
    assert plan.problem.machine("PR1").downtimes, "the plan has to know"
    loop = ClosedLoop(engine, plan)
    assert not loop.run(until=500.0).stalled
    assert engine.inject_fault("OF1", "O-301", 240.0)["ok"]
    assert engine.inject_fault("PR2", "P-202", 240.0)["ok"]
    result = loop.run()
    assert not result.stalled and not result.refusals
    assert_all_through(loop, lots)


@pytest.mark.parametrize(
    "solve",
    [
        decode_edd,
        lambda p: solve_anneal(p, time_limit_s=10, seed=1, max_iterations=400),
        lambda p: solve_cpsat(p, time_limit_s=1.5, seed=1, workers=4),
    ],
    ids=["edd", "anneal", "cpsat"],
)
def test_deterministic_twin_finishes_each_op_when_the_plan_says(solve):
    """stochastic=False means exactly planned_minutes, so a plan's completion has to hold within rounding."""
    lots, orders = realistic_lots(target=20, seed=43)
    engine = engine_at_start(43)
    plan = plan_from(engine, lots, orders, solve)
    loop = ClosedLoop(engine, plan)
    assert not loop.run().stalled
    planned = {a.op_id: a for a in plan.schedule.assignments}
    late = {op_id: (planned[op_id].end, at) for op_id, at in loop.finished_at.items() if at > planned[op_id].end + 1e-6}
    assert not late, list(late.items())[:5]
    metrics = compute_metrics(plan.problem, plan.schedule)
    for order_id, planned_completion in metrics.order_completion.items():
        ends = [t for op_id, t in loop.finished_at.items() if op_id.endswith(":pack") and plan.problem.op(op_id).order_id == order_id]
        assert max(ends) <= planned_completion + 1e-6, order_id


@pytest.mark.parametrize("fork_at", [80.0, 233.3, 500.5, 900.25])
def test_deterministic_twin_from_a_midrun_fork_holds_a_plan_built_at_the_fork(fork_at):
    lots, orders = realistic_lots(target=20, seed=44)
    engine, _ = midrun(lots, orders, fork_at, seed=44, stochastic=True, random_failures=False)
    snapshot = engine.export_snapshot()
    fork = SimEngine.from_snapshot(snapshot)
    plan = plan_from(fork, open_lots_of(lots, snapshot), orders, revision=2)
    assert plan.problem.now == pytest.approx(fork_at, abs=1)
    loop = ClosedLoop(fork, plan)
    assert not loop.run().stalled
    planned = {a.op_id: a for a in plan.schedule.assignments}
    late = {op_id: (planned[op_id].end, at) for op_id, at in loop.finished_at.items() if op_id in planned and at > planned[op_id].end + 1e-6}
    assert not late, list(late.items())[:5]


def test_replanning_along_the_way_from_each_plans_own_state_reaches_the_end():
    """Each revision is built from a snapshot at that moment, reusing the previous plan's pending melts and sequences."""
    lots, orders = realistic_lots(target=20, seed=45)
    engine = engine_at_start(45, True, random_failures=False)
    plan = plan_from(engine, lots, orders)
    loop = ClosedLoop(engine, plan)
    for number, at in enumerate((120.0, 310.0, 505.0, 770.0, 1100.0), start=2):
        assert not loop.run(until=at).stalled
        if loop.done():
            break
        snapshot = engine.export_snapshot()
        loop.plan = plan_from(engine, open_lots_of(lots, snapshot), orders, revision=number, previous=loop.plan)
    result = loop.run()
    assert not result.stalled and not result.refusals
    assert_all_through(loop, lots)


# ---------------------------------------------------------------------- forks


@pytest.mark.parametrize("seed", [51, 52])
def test_a_fork_at_many_points_of_a_run_completes_with_no_horizon_hit_and_conserves_lots(seed):
    lots, orders = realistic_lots(target=20, seed=seed)
    engine = engine_at_start(seed, True, random_failures=True)
    plan = plan_from(engine, lots, orders)
    loop = ClosedLoop(engine, plan)
    forks = 0

    def fork_and_finish(current: ClosedLoop) -> None:
        nonlocal forks
        if current.steps % 3:
            return
        current.collect()
        snapshot = engine.export_snapshot()
        assert all(_where(snapshot, item.id) <= 1 for item in lots), "a lot is in two places at once"
        run = run_closed_loop(snapshot, plan, seed=forks + 1, record_timeline=False)
        assert not run.stalled and not run.horizon_hit and not run.unfinished_orders, snapshot["t"]
        assert run.refusals == 0
        if current.steps % 15 == 0:
            noisy = run_closed_loop(snapshot, plan, seed=forks + 1, stochastic=True, record_timeline=False)
            assert not noisy.stalled and not noisy.horizon_hit and not noisy.unfinished_orders, snapshot["t"]
            assert noisy.refusals == 0
            fork = SimEngine.from_snapshot(snapshot, seed=forks + 1)
            finish = ClosedLoop(fork, plan)
            assert not finish.run().stalled
            while (when := fork.next_event_time()) is not None:
                fork.run_until(when)
            assert fork.export_snapshot()["buffers"]["SHIP"]["lots_total"] == len(lots), "a lot went missing in the fork"
        forks += 1

    assert not loop.run(on_step=fork_and_finish).stalled
    assert forks > 60


def _where(snapshot: dict, lot_id: str) -> int:
    """In how many places a lot is at this instant, to check that none is ever in two."""
    count = 0
    for machine in snapshot["machines"].values():
        if machine["op"] and lot_id in machine["op"]["lot_ids"]:
            count += 1
    for name in ("AGE_IN", "SAW_IN", "PACK_IN"):
        count += sum(1 for item in snapshot["buffers"][name] if item["lot_id"] == lot_id)
    count += sum(1 for item in snapshot["transports"] if item["lot_id"] == lot_id)
    return count


def test_a_fork_with_several_machines_down_finishes():
    lots, orders = realistic_lots(target=20, seed=53)
    engine, plan = midrun(lots, orders, 300.0, seed=53)
    assert engine.inject_fault("PR2", "P-204", 500.0)["ok"]
    assert engine.inject_fault("OF2", "O-301", 200.0)["ok"]
    assert engine.inject_fault("GUSS", "G-101", 200.0)["ok"]
    run = run_closed_loop(engine.export_snapshot(), plan)
    assert not run.stalled and not run.unfinished_orders


# ---------------------------------------------------------------------- stalled


def _under_supplied():
    """Four lots of 8 against 24 billets: without the melt the plan would need, the last press can never start."""
    engine = engine_at_start()
    lots = [lot(f"L-{i}", "FR-60", 8) for i in range(1, 5)]
    orders = [order()]
    full = plan_from(engine, lots, orders)
    sequences = {
        machine: [list(unit.op_ids) for unit in units]
        for machine, units in full.sequences.items()
        if machine != "GUSS"
    }
    plan = hand_plan(engine, lots, orders, sequences, drop_ops=("M-6060-00001:cast",))
    return engine, lots, plan


def test_the_under_supplied_plan_is_seen_as_short_of_billets():
    engine, _, plan = _under_supplied()
    assert billet_shortfall(plan, Tracker.from_state(engine.planning_state())) == {"6060": 8}


def test_stalled_is_true_only_when_nothing_is_left_to_happen():
    engine, lots, plan = _under_supplied()
    loop = ClosedLoop(engine, plan)
    seen_false = 0

    def check(current: ClosedLoop) -> None:
        nonlocal seen_false
        current.collect()
        if not current.is_stalled():
            seen_false += 1

    result = loop.run(on_step=check)
    assert result.stalled
    assert not result.refusals, "next_commands knows the billets are not there and does not even ask"
    assert seen_false > 20, "the plant ran for a long while before it ran out of things to do"
    assert loop.is_stalled()
    assert not plan_finished(plan, loop.tracker)
    assert loop.tracker.op_state("L-4:press") == "pending"
    assert sum(1 for op_id in loop.finished_at if op_id.endswith(":pack")) == 3


def test_a_healthy_plan_is_never_stalled_at_any_step():
    lots, orders = realistic_lots(target=12, seed=61)
    engine = engine_at_start(61, True)
    loop = ClosedLoop(engine, plan_from(engine, lots, orders))

    def check(current: ClosedLoop) -> None:
        current.collect()
        assert not current.is_stalled(), current.engine.t

    assert not loop.run(on_step=check).stalled


def test_stalled_is_false_at_the_very_start_though_everything_is_idle():
    engine = engine_at_start()
    plan = plan_from(engine, [lot("L-1")], [order()])
    assert not stalled(plan, Tracker.from_state(engine.planning_state()), views_of(engine), False)


def test_stalled_is_false_with_a_transport_on_its_way():
    engine, _, plan = _under_supplied()
    loop = ClosedLoop(engine, plan)
    assert loop.run().stalled
    views = views_of(engine)
    assert stalled(plan, loop.tracker, views, False)
    assert not stalled(plan, loop.tracker, views, True)


def test_stalled_is_false_while_a_machine_is_in_fault():
    engine, _, plan = _under_supplied()
    loop = ClosedLoop(engine, plan)
    assert loop.run().stalled
    assert engine.inject_fault("VP", "V-501", 30.0)["ok"]
    assert not stalled(plan, loop.tracker, views_of(engine), False)
    engine.run_until(engine.t + 31.0)
    assert stalled(plan, loop.tracker, views_of(engine), False)


def test_stalled_is_false_when_the_plan_is_done():
    engine = engine_at_start()
    plan = plan_from(engine, [lot("L-1", n_billets=1)], [order()])
    loop = ClosedLoop(engine, plan)
    assert not loop.run().stalled
    engine.run_until(engine.t + catalog.TRANSPORT_MIN)
    loop.collect()
    assert plan_finished(plan, loop.tracker)
    assert not stalled(plan, loop.tracker, views_of(engine), False)


def test_stalled_is_true_with_an_op_dropped_from_the_plan():
    """The press is not in the plan at all, so the age op's predecessor never comes."""
    engine = engine_at_start()
    lots = [lot("L-1", n_billets=1)]
    plan = hand_plan(engine, lots, [order()], {"OF1": [["L-1:age"]]}, drop_ops=("L-1:press",))
    assert stalled(plan, Tracker.from_state(engine.planning_state()), views_of(engine), False)


def test_the_twin_ends_stalled_with_the_order_flagged_and_completion_at_the_horizon():
    engine, lots, plan = _under_supplied()
    snapshot = engine.export_snapshot()
    run = run_closed_loop(snapshot, plan, horizon_min=5000.0)
    assert run.stalled
    assert not run.horizon_hit
    assert run.unfinished_orders == ["A-1"]
    assert run.order_completion["A-1"] == 5000
    assert run.makespan == 5000
    assert run.lots_packed == 3
    assert run.tardiness == 0, "due date far out: flagged, but not late"
    assert run.sim_minutes < 5000, "it stops at the stall and does not idle until the horizon"
