"""rebase at accept time: lots created after the snapshot, a load half dispatched elsewhere, a billet shortfall, and more."""
from __future__ import annotations

import math
import sys
from dataclasses import replace

import pytest
from test_execution_support import ClosedLoop, RUN_ID, engine_at_start, lot, open_lots_of, order, plan_from, sync, views_of
from test_scheduler_support import assert_valid, realistic_lots

from werkhalle.domain import catalog
from werkhalle.execution import Tracker, next_commands, rebase
from werkhalle.scheduling import Assignment, OrderInfo, Plan, Schedule, Violation, build_problem, decode_edd
from werkhalle.simcore import SimEngine


def start_press(engine: SimEngine, lot_id: str, product: str = "FR-60", n: int = 8, machine: str = "PR1", order_id: str = "A-1") -> None:
    command = {
        "machine": machine, "run_id": RUN_ID, "op_ids": [f"{lot_id}:press"], "kind": "press", "lot_id": lot_id,
        "order_id": order_id, "product_id": product, "n_billets": n,
    }
    assert engine.start_operation(command)["ok"], command


def run_to_the_end(engine: SimEngine, plan: Plan) -> ClosedLoop:
    loop = ClosedLoop(engine, plan)
    result = loop.run()
    assert not result.stalled and not result.refusals
    return loop


# ---------------------------------------------------------------------- nothing changed


def test_rebase_with_nothing_changed_keeps_the_sequences():
    lots, orders = realistic_lots(target=12, seed=71)
    engine = engine_at_start(71)
    plan = plan_from(engine, lots, orders)
    tracker = Tracker.from_state(engine.planning_state())
    result = rebase(plan, tracker, [item.id for item in lots])
    assert result.plan is not None, result.reason
    assert result.reason == "" and result.dropped == ()
    assert result.plan.sequences == plan.sequences
    assert_valid(result.plan.problem, result.plan.schedule)
    assert result.plan.revision == plan.revision
    assert result.plan.schedule.solver == plan.schedule.solver


# ---------------------------------------------------------------------- lots created after the snapshot


def test_a_lot_created_after_the_snapshot_is_appended_at_machine_tails():
    lots = [lot("L-1"), lot("L-2", "KK-80", 8, "A-2"), lot("L-3", "ST-200", 8, "A-3")]
    orders = [order("A-1"), order("A-2"), order("A-3")]
    engine = engine_at_start()
    plan = plan_from(engine, lots, orders)
    late = lot("L-4", "FR-60", 4, "A-4")  # 12 of 24 6060 billets used: there is enough for this one
    tracker = Tracker.from_state(engine.planning_state())
    result = rebase(plan, tracker, [*(item.id for item in lots), late.id], new_lots=[late], new_orders=[order("A-4")])
    assert result.plan is not None, result.reason
    rebased = result.plan
    assert_valid(rebased.problem, rebased.schedule)
    for kind, machine_kind in (("press", "PR"), ("saw", "SAE"), ("pack", "VP")):
        op_id = f"L-4:{kind}"
        machine = next(a.machine for a in rebased.schedule.assignments if a.op_id == op_id)
        assert machine.startswith(machine_kind)
        tail = rebased.sequences[machine][-1]
        assert op_id in tail.op_ids, f"{op_id} should be at the tail of {machine}"
    assert {x.id for x in rebased.lots} == {"L-1", "L-2", "L-3", "L-4"}
    assert rebased.lots_by_id["L-4"] == late
    run_to_the_end(engine, rebased)


def test_a_lot_passed_as_a_lot_object_needs_no_extra_argument():
    engine = engine_at_start()
    plan = plan_from(engine, [lot("L-1")], [order()])
    late = lot("L-2", "FR-60", 4, "A-2")
    result = rebase(plan, Tracker.from_state(engine.planning_state()), [lot("L-1"), late], new_orders=[order("A-2")])
    assert result.plan is not None, result.reason
    assert result.plan.problem.has_op("L-2:press")


def test_a_new_lot_without_its_data_stays_missing_and_there_is_no_plan():
    engine = engine_at_start()
    plan = plan_from(engine, [lot("L-1")], [order()])
    result = rebase(plan, Tracker.from_state(engine.planning_state()), ["L-1", "L-99"])
    assert result.plan is None
    assert result.reason == "Lose fehlen im Plan: L-99."


def test_a_new_lot_that_breaks_the_billet_supply_gives_no_plan():
    lots = [lot("L-1"), lot("L-2"), lot("L-3")]  # exactly the 24 billets in the store: no melt planned
    engine = engine_at_start()
    plan = plan_from(engine, lots, [order()])
    assert not any(op.kind == "cast" for op in plan.problem.ops)
    late = lot("L-4", "FR-60", 2, "A-2")
    result = rebase(plan, Tracker.from_state(engine.planning_state()), ["L-1", "L-2", "L-3", "L-4"], new_lots=[late], new_orders=[order("A-2")])
    assert result.plan is None
    assert result.reason == "Zu wenig Bolzen für die offenen Lose (6060: 2)."


def test_a_new_lot_uses_a_surplus_from_a_planned_melt():
    lots = [lot("L-1"), lot("L-2"), lot("L-3"), lot("L-4")]  # 32 billets: one melt, 16 of its 24 are spare
    engine = engine_at_start()
    plan = plan_from(engine, lots, [order()])
    cast_ops = [op for op in plan.problem.ops if op.kind == "cast"]
    assert len(cast_ops) == 1
    late = lot("L-5", "FR-60", 8, "A-2")
    result = rebase(plan, Tracker.from_state(engine.planning_state()), [*(x.id for x in lots), "L-5"], new_lots=[late], new_orders=[order("A-2")])
    assert result.plan is not None, result.reason
    # Whatever order the lots start in, the one depending on the melt really waits for it.
    press = result.plan.problem.op("L-5:press")
    assert any(pred.op_id == cast_ops[0].id for pred in press.preds)
    run_to_the_end(engine, result.plan)


def test_a_started_lot_the_plan_does_not_know_keeps_running_and_its_rest_is_appended():
    engine = engine_at_start()
    plan = plan_from(engine, [lot("L-1")], [order()])
    late = lot("L-2", "KK-80", 2, "A-2")
    start_press(engine, "L-2", "KK-80", 2, "PR2", "A-2")
    tracker = Tracker.from_state(engine.planning_state())
    result = rebase(plan, tracker, ["L-1", "L-2"], new_lots=[late], new_orders=[order("A-2")])
    assert result.plan is not None, result.reason
    assert result.plan.problem.op("L-2:press").frozen is not None
    assert result.plan.problem.op("L-2:press").frozen.machine == "PR2"
    assert result.plan.problem.op("L-2:press").billets_in == 0, "its billets left the store when it started"
    assert not result.plan.problem.op("L-2:age").frozen
    run_to_the_end(engine, result.plan)


# ---------------------------------------------------------------------- load half dispatched


def _half_dispatched():
    """The candidate holds L-1 and L-2 as one load on OF2. Before it is accepted, the old plan put L-1 into OF1."""
    lots = [lot("L-1", n_billets=4), lot("L-2", n_billets=4)]
    engine = engine_at_start()
    start_press(engine, "L-1", n=4, machine="PR1")
    start_press(engine, "L-2", n=4, machine="PR2")
    engine.run_until(100.0)
    assert len(engine.export_snapshot()["buffers"]["AGE_IN"]) == 2
    state = engine.planning_state()
    assert {"L-1:press", "L-2:press"} <= state.finished_op_ids
    problem = build_problem(state, lots, [order()], batch_prefix="B-r2").problem
    start = problem.now + 30
    soak = problem.op("L-1:age").durations["OF2"]
    assignments = [
        Assignment("L-1:age", "OF2", start, start + soak, 30, "B-r2-1"),
        Assignment("L-2:age", "OF2", start, start + soak, 0, "B-r2-1"),
    ]
    end = start + soak
    for lot_id in ("L-1", "L-2"):
        saw = problem.op(f"{lot_id}:saw").durations["SAE"]
        pack = problem.op(f"{lot_id}:pack").durations["VP"]
        assignments.append(Assignment(f"{lot_id}:saw", "SAE", end + 8, end + 8 + saw))
        assignments.append(Assignment(f"{lot_id}:pack", "VP", end + 8 + saw + 8, end + 8 + saw + 8 + pack))
        end += saw
    candidate = Plan(2, problem, Schedule("cpsat", "feasible", tuple(assignments)), tuple(lots), created_min=state.now, source="proposal")
    assert engine.start_operation(
        {"machine": "OF1", "run_id": RUN_ID, "op_ids": ["L-1:age"], "kind": "age", "batch_id": "B-r1-4", "recipe": "T66", "lot_ids": ["L-1"]}
    )["ok"]
    return engine, candidate


def test_a_load_half_dispatched_on_another_furnace_is_reduced():
    engine, candidate = _half_dispatched()
    tracker = Tracker.from_state(engine.planning_state())
    result = rebase(candidate, tracker, ["L-1", "L-2"])
    assert result.plan is not None, result.reason
    rebased = result.plan
    assert result.dropped == ("L-1:age",)
    assert_valid(rebased.problem, rebased.schedule)
    frozen = rebased.problem.op("L-1:age").frozen
    assert (frozen.machine, frozen.batch_id) == ("OF1", "B-r1-4")
    of2 = rebased.sequences["OF2"]
    assert [unit.op_ids for unit in of2] == [("L-2:age",)]
    assert of2[0].batch_id == "B-r2-1"
    [command] = next_commands(rebased, tracker, views_of(engine), frozenset(), RUN_ID)
    assert command["machine"] == "OF2"
    assert command["op_ids"] == ["L-2:age"]
    assert command["lot_ids"] == ["L-2"]


def test_a_reduced_load_runs_to_the_end():
    engine, candidate = _half_dispatched()
    tracker = Tracker.from_state(engine.planning_state())
    rebased = rebase(candidate, tracker, ["L-1", "L-2"]).plan
    loop = run_to_the_end(engine, rebased)
    assert {"L-1:pack", "L-2:pack"} <= set(loop.finished_at)


def test_a_pending_member_never_joins_a_running_load_of_the_same_id():
    """The candidate's load id is the very one the running half got, so the pending half has to get its own."""
    engine, candidate = _half_dispatched()
    assignments = tuple(
        replace(a, batch_id="B-r1-4") if a.batch_id == "B-r2-1" else a for a in candidate.schedule.assignments
    )
    candidate = replace(candidate, schedule=replace(candidate.schedule, assignments=assignments))
    rebased = rebase(candidate, Tracker.from_state(engine.planning_state()), ["L-1", "L-2"]).plan
    assert rebased is not None
    assert_valid(rebased.problem, rebased.schedule)
    assert rebased.sequences["OF2"][0].batch_id != "B-r1-4"


def test_a_load_with_no_member_left_leaves_no_empty_unit():
    engine, candidate = _half_dispatched()
    assert engine.start_operation(
        {"machine": "OF2", "run_id": RUN_ID, "op_ids": ["L-2:age"], "kind": "age", "batch_id": "B-r1-5", "recipe": "T66", "lot_ids": ["L-2"]}
    )["ok"]
    result = rebase(candidate, Tracker.from_state(engine.planning_state()), ["L-1", "L-2"])
    assert result.plan is not None, result.reason
    assert set(result.dropped) == {"L-1:age", "L-2:age"}
    for machine in ("OF1", "OF2"):
        for unit in result.plan.sequences.get(machine, ()):
            assert all(result.plan.problem.op(op_id).frozen is not None for op_id in unit.op_ids)


# ---------------------------------------------------------------------- billet shortfall


def test_a_billet_shortfall_gives_no_plan_and_says_why():
    """Between snapshot and accept a press of a lot unknown to the candidate took billets the candidate counts on."""
    lots = [lot("L-1"), lot("L-2"), lot("L-3")]
    engine = engine_at_start()
    state = engine.planning_state()
    problem = build_problem(state, lots, [order()]).problem
    assert not any(op.kind == "cast" for op in problem.ops), "the store covers it exactly"
    candidate = Plan(2, problem, decode_edd(problem), tuple(lots), created_min=0.0, source="proposal")
    start_press(engine, "L-9", n=2, machine="PR2")
    tracker = Tracker.from_state(engine.planning_state())
    result = rebase(candidate, tracker, ["L-1", "L-2", "L-3"])
    assert result.plan is None
    assert result.reason == "Zu wenig Bolzen für die offenen Lose (6060: 2)."


def test_supply_is_read_from_the_tracker_not_from_the_plan():
    """The same theft, but a melt finishes in between: now it is enough again, and the plan is good."""
    lots = [lot("L-1"), lot("L-2"), lot("L-3")]
    engine = engine_at_start()
    problem = build_problem(engine.planning_state(), lots, [order()]).problem
    candidate = Plan(2, problem, decode_edd(problem), tuple(lots), created_min=0.0, source="proposal")
    start_press(engine, "L-9", n=2, machine="PR2")
    assert engine.start_operation(
        {"machine": "GUSS", "run_id": RUN_ID, "op_ids": ["M-6060-00042:cast"], "kind": "cast", "melt_plan_id": "M-6060-00042", "alloy": "6060"}
    )["ok"]
    tracker = Tracker.from_state(engine.planning_state())
    sync(engine, tracker)
    result = rebase(candidate, tracker, ["L-1", "L-2", "L-3"])
    assert result.plan is not None, result.reason
    assert result.plan.problem.op("M-6060-00042:cast").frozen is not None, "a cast this plan did not list still occupies the line"
    assert_valid(result.plan.problem, result.plan.schedule)
    # L-9 is no open lot here, so its pressing is the plant's business; the plan must only not stall on it.
    loop = ClosedLoop(engine, result.plan)
    assert not loop.run().stalled


def test_a_melt_in_transit_counts_as_supply_with_its_arrival():
    lots = [lot(f"L-{i}", "KK-80", 8) for i in range(1, 5)]
    engine = engine_at_start()
    problem = build_problem(engine.planning_state(), lots, [order()]).problem
    assert any(op.kind == "cast" for op in problem.ops)
    candidate = Plan(2, problem, decode_edd(problem), tuple(lots), source="proposal")
    assert engine.start_operation(
        {"machine": "GUSS", "run_id": RUN_ID, "op_ids": ["M-6063-00001:cast"], "kind": "cast", "melt_plan_id": "M-6063-00001", "alloy": "6063"}
    )["ok"]
    tracker = Tracker.from_state(engine.planning_state())
    while tracker.op_state("M-6063-00001:cast") != "done":
        engine.run_until(engine.next_event_time())
        sync(engine, tracker)
    moving = tracker.melts_in_transit()[0]
    result = rebase(candidate, tracker, [x.id for x in lots])
    assert result.plan is not None, result.reason
    plan = result.plan
    assert_valid(plan.problem, plan.schedule)
    assert not plan.problem.has_op("M-6063-00001:cast"), "done: its melt counts as a transport now"
    assert any(s.available_at >= moving.arrives_min for s in plan.problem.billet_supply if s.alloy == "6063")
    assert plan.problem.op("L-4:press").release >= int(moving.arrives_min)
    run_to_the_end(engine, plan)


# ---------------------------------------------------------------------- started and done


def test_started_and_done_ops_are_dropped_and_what_runs_is_frozen():
    lots, orders = realistic_lots(target=12, seed=72)
    engine = engine_at_start(72)
    plan = plan_from(engine, lots, orders)
    state = engine.planning_state()
    loop = ClosedLoop(engine, plan)
    assert not loop.run(until=700.0).stalled
    snapshot = engine.export_snapshot()
    result = rebase(plan, loop.tracker, [item.id for item in open_lots_of(lots, snapshot)], now=snapshot["t"])
    assert result.plan is not None, result.reason
    rebased = result.plan
    assert_valid(rebased.problem, rebased.schedule)
    assert state.now == 0.0
    done = set(snapshot["finished_op_ids"])
    running = set(snapshot["started_op_ids"]) - done
    assert running, "a test of what runs needs something running"
    assert set(result.dropped) == done | running
    for op_id in done:
        assert not rebased.problem.has_op(op_id)
    for op_id in running:
        assert rebased.problem.op(op_id).frozen is not None
        assert rebased.problem.op(op_id).billets_in == 0, "a running press took its billets when it started"
    assert rebased.problem.now >= 700
    for a in rebased.schedule.assignments:
        if rebased.problem.op(a.op_id).frozen is None:
            assert a.start - a.setup_before >= rebased.problem.now
    run_to_the_end(engine, rebased)


def test_an_op_frozen_in_the_plan_stays_frozen_with_a_tracker_behind_the_plan():
    lots, orders = realistic_lots(target=6, seed=73)
    engine = engine_at_start(73)
    tracker = Tracker.from_state(engine.planning_state())
    plan = plan_from(engine, lots, orders)
    loop = ClosedLoop(engine, plan)
    assert not loop.run(until=50.0).stalled
    newer = plan_from(engine, lots, orders, revision=2, previous=plan)
    assert any(op.frozen is not None for op in newer.problem.ops)
    # This tracker has seen nothing since t=0, the plan knows better.
    result = rebase(newer, tracker, [x.id for x in lots])
    assert result.plan is not None, result.reason
    assert_valid(result.plan.problem, result.plan.schedule)
    for op in newer.problem.ops:
        if op.frozen is not None:
            assert result.plan.problem.op(op.id).frozen is not None


def test_a_frozen_op_past_its_estimated_end_is_pushed_beyond_now():
    lots, orders = realistic_lots(target=6, seed=73)
    engine = engine_at_start(73)
    tracker = Tracker.from_state(engine.planning_state())
    plan = plan_from(engine, lots, orders)
    assert not ClosedLoop(engine, plan).run(until=50.0).stalled
    newer = plan_from(engine, lots, orders, revision=2, previous=plan)
    frozen_ids = [op.id for op in newer.problem.ops if op.frozen is not None]
    late = max(newer.problem.op(op_id).frozen.end for op_id in frozen_ids) + 50
    result = rebase(newer, tracker, [x.id for x in lots], now=float(late))
    assert result.plan is not None, result.reason
    assert_valid(result.plan.problem, result.plan.schedule)
    for op_id in frozen_ids:
        assert result.plan.problem.op(op_id).frozen.end == late + 1


def test_a_lot_on_a_transport_gets_its_arrival_as_release():
    """Its predecessor is done and says nothing any more, so the release has to keep the plan's times honest."""
    lots = [lot("L-1", n_billets=1)]
    engine = engine_at_start()
    plan = plan_from(engine, lots, [order()])
    loop = ClosedLoop(engine, plan)
    while loop.tracker.op_state("L-1:press") != "done":
        loop.dispatch()
        engine.run_until(engine.next_event_time())
        loop.collect()
    stage, arrival = loop.tracker.lot_arrival("L-1")
    assert stage == "age"
    result = rebase(plan, loop.tracker, ["L-1"], now=engine.t)
    assert result.plan is not None, result.reason
    assert result.plan.problem.op("L-1:age").release == math.ceil(arrival)
    age = next(a for a in result.plan.schedule.assignments if a.op_id == "L-1:age")
    assert age.start - age.setup_before >= arrival
    assert result.plan.problem.op("L-1:saw").release == 0, "only the op it is on its way to"


@pytest.mark.parametrize("from_state", [True, False])
def test_a_running_op_nobody_planned_blocks_its_machine(from_state):
    lots = [lot("L-1", "ST-200", 1)]
    engine = engine_at_start()
    tracker = Tracker.from_state(engine.planning_state())
    plan = plan_from(engine, lots, [order()])
    start_press(engine, "L-9", "CB-90", 8, "PR2")
    sync(engine, tracker)
    if from_state:
        tracker = Tracker.from_state(engine.planning_state())
    rebased = rebase(plan, tracker, ["L-1"]).plan
    assert rebased is not None
    free_at = rebased.problem.machine("PR2").available_from
    assert free_at > rebased.problem.now
    press = next(a for a in rebased.schedule.assignments if a.op_id == "L-1:press")
    assert press.start - press.setup_before >= free_at
    if from_state:
        assert free_at == math.ceil(engine.planning_state().machine("PR2").remaining_work_min)


def test_a_carried_over_plan_that_fails_validation_is_no_plan(monkeypatch):
    module = sys.modules["werkhalle.execution.rebase"]
    monkeypatch.setattr(module, "validate_schedule", lambda *_args: [Violation("overlap", ("x",), "Testverletzung")])
    engine = engine_at_start()
    plan = plan_from(engine, [lot("L-1")], [order()])
    result = rebase(plan, Tracker.from_state(engine.planning_state()), ["L-1"])
    assert result.plan is None
    assert "Testverletzung" in result.reason


def test_a_lot_gone_from_the_open_list_loses_its_pending_ops():
    lots = [lot("L-1"), lot("L-2", "KK-80", 4, "A-2")]
    engine = engine_at_start()
    plan = plan_from(engine, lots, [order("A-1"), order("A-2")])
    result = rebase(plan, Tracker.from_state(engine.planning_state()), ["L-1"])
    assert result.plan is not None, result.reason
    assert not result.plan.problem.has_op("L-2:press")
    assert set(result.dropped) == {"L-2:press", "L-2:age", "L-2:saw", "L-2:pack"}
    assert [x.id for x in result.plan.lots] == ["L-1"]


def test_downtimes_stay_and_are_clipped():
    lots = [lot("L-1", n_billets=2)]
    engine = engine_at_start()
    assert engine.inject_fault("PR1", "P-201", 120.0)["ok"]
    plan = plan_from(engine, lots, [order()])
    assert plan.problem.machine("PR1").downtimes
    engine.run_until(50.0)
    tracker = Tracker.from_state(engine.planning_state())
    rebased = rebase(plan, tracker, ["L-1"], now=50.0).plan
    assert rebased is not None
    [down] = rebased.problem.machine("PR1").downtimes
    assert down.start == 50 and down.end == 120
    assert_valid(rebased.problem, rebased.schedule)


def test_a_setup_class_mounted_since_the_snapshot_counts():
    lots = [lot("L-1", "ST-200", 1)]
    engine = engine_at_start()
    plan = plan_from(engine, lots, [order()])
    assert plan.schedule.assignments[0].setup_before == 0, "PR2 starts with the ST-200 die"
    start_press(engine, "L-9", "FR-60", 1, "PR2")
    tracker = Tracker.from_state(engine.planning_state())
    rebased = rebase(plan, tracker, ["L-1"]).plan
    assert rebased is not None
    press = next(a for a in rebased.schedule.assignments if a.op_id == "L-1:press")
    assert press.machine == "PR2"
    assert press.setup_before == catalog.setup_minutes(catalog.PRESS, "6060|M-1001", "6082|M-3001")


@pytest.mark.parametrize("seed", [74, 75, 76])
def test_rebase_of_an_old_plan_at_many_points_of_a_noisy_run_never_stalls(seed):
    """Every time the old plan is carried over, the result must validate, and running it must reach the end."""
    lots, orders = realistic_lots(target=16, seed=seed)
    engine = engine_at_start(seed, True, random_failures=True)
    plan = plan_from(engine, lots, orders)
    loop = ClosedLoop(engine, plan)
    rebases = 0

    def check(current: ClosedLoop) -> None:
        nonlocal rebases
        if current.steps % 6:
            return
        current.collect()
        snapshot = engine.export_snapshot()
        result = rebase(plan, current.tracker, [item.id for item in open_lots_of(lots, snapshot)], now=snapshot["t"])
        assert result.plan is not None, (snapshot["t"], result.reason)
        assert_valid(result.plan.problem, result.plan.schedule)
        rebases += 1
        fork = SimEngine.from_snapshot(snapshot, seed=seed)
        finish = ClosedLoop(fork, result.plan)
        ran = finish.run()
        assert not ran.stalled and not ran.refusals, snapshot["t"]

    assert not loop.run(on_step=check).stalled
    assert rebases > 30


def test_a_rebased_plan_can_be_rebased_again():
    lots, orders = realistic_lots(target=12, seed=77)
    engine = engine_at_start(77)
    plan = plan_from(engine, lots, orders)
    loop = ClosedLoop(engine, plan)
    for at in (150.0, 400.0, 800.0):
        assert not loop.run(until=at).stalled
        snapshot = engine.export_snapshot()
        result = rebase(loop.plan, loop.tracker, [x.id for x in open_lots_of(lots, snapshot)], now=at)
        assert result.plan is not None, result.reason
        assert_valid(result.plan.problem, result.plan.schedule)
        loop.plan = result.plan
    assert not loop.run().stalled
    assert sum(1 for op_id in loop.finished_at if op_id.endswith(":pack")) == len(lots)


def test_new_orders_give_a_new_lot_its_rank_and_a_place_in_the_metrics():
    from werkhalle.scheduling import compute_metrics

    lots = [lot("L-1"), lot("L-2")]
    engine = engine_at_start()
    plan = plan_from(engine, lots, [order("A-1")])
    late = lot("L-3", "FR-60", 4, "A-2")
    result = rebase(plan, Tracker.from_state(engine.planning_state()), ["L-1", "L-2", "L-3"], new_lots=[late], new_orders=[OrderInfo("A-2", 50, 3)])
    assert result.plan is not None, result.reason
    metrics = compute_metrics(result.plan.problem, result.plan.schedule)
    assert "A-2" in metrics.order_completion
    assert metrics.late_orders >= 1
