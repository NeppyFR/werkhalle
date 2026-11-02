"""next_commands and the Dispatcher: the rules of PLAN section 7, one by one, and a plant that answers late."""
from __future__ import annotations

from collections import Counter, deque

import pytest
from test_execution_support import (
    ClosedLoop,
    RUN_ID,
    engine_at_start,
    hand_plan,
    lot,
    order,
    plan_from,
    sync,
    views_of,
)
from test_scheduler_support import realistic_lots

from werkhalle.domain import catalog
from werkhalle.domain.catalog import MACHINE_ORDER
from werkhalle.execution import Dispatcher, Tracker, billet_shortfall, machine_heads, next_commands
from werkhalle.scheduling import Assignment, Frozen, Plan, Pred, Schedule, build_problem, decode_edd
from werkhalle.simcore import SimEngine


def idle_views(**status: str) -> dict[str, dict]:
    return {machine: {"Status": status.get(machine, "IDLE")} for machine in MACHINE_ORDER}


def run_until_arrived(engine: SimEngine, tracker: Tracker, lot_id: str, stage: str) -> None:
    while not tracker.lot_at_stage(lot_id, stage):
        engine.run_until(engine.next_event_time())
        sync(engine, tracker)


# ---------------------------------------------------------------------- command JSON


def test_cast_command_is_section_4_plus_run_id_and_machine():
    engine = engine_at_start()
    lots = [lot(f"L-{i}", "KK-80", 8, "A-1") for i in range(1, 5)]
    plan = plan_from(engine, lots, [order("A-1")])
    tracker = Tracker.from_state(engine.planning_state())
    commands = next_commands(plan, tracker, views_of(engine), frozenset(), RUN_ID)
    cast = next(c for c in commands if c["kind"] == "cast")
    assert cast == {
        "run_id": RUN_ID,
        "machine": "GUSS",
        "op_ids": ["M-6063-00001:cast"],
        "kind": "cast",
        "melt_plan_id": "M-6063-00001",
        "alloy": "6063",
    }
    assert engine.start_operation(cast) == {"ok": True, "reason": ""}


def test_press_command_is_section_4_plus_run_id_and_machine():
    engine = engine_at_start()
    plan = plan_from(engine, [lot("L-00012", "FR-60", 8, "A-1001")], [order("A-1001")])
    tracker = Tracker.from_state(engine.planning_state())
    [command] = next_commands(plan, tracker, views_of(engine), frozenset(), RUN_ID)
    assert command == {
        "run_id": RUN_ID,
        "machine": "PR1",
        "op_ids": ["L-00012:press"],
        "kind": "press",
        "lot_id": "L-00012",
        "order_id": "A-1001",
        "product_id": "FR-60",
        "n_billets": 8,
    }
    assert engine.start_operation(command) == {"ok": True, "reason": ""}


def test_age_saw_and_pack_commands_are_section_4_plus_run_id_and_machine():
    engine = engine_at_start()
    lots = [lot("L-1", "FR-60", 2), lot("L-2", "FR-60", 2)]
    plan = hand_plan(
        engine, lots, [order("A-1")],
        {"PR1": [["L-1:press"], ["L-2:press"]], "OF1": [["L-1:age", "L-2:age"]], "SAE": [["L-1:saw"], ["L-2:saw"]], "VP": [["L-1:pack"], ["L-2:pack"]]},
    )
    tracker = Tracker.from_state(engine.planning_state())
    assert engine.start_operation(next_commands(plan, tracker, views_of(engine), frozenset(), RUN_ID)[0])["ok"]
    sync(engine, tracker)
    while tracker.op_state("L-1:press") != "done":
        engine.run_until(engine.next_event_time())
        sync(engine, tracker)
    assert engine.start_operation(next_commands(plan, tracker, views_of(engine), frozenset(), RUN_ID)[0])["ok"]
    sync(engine, tracker)
    run_until_arrived(engine, tracker, "L-2", "age")

    [age] = next_commands(plan, tracker, views_of(engine), frozenset(), RUN_ID)
    assert age == {
        "run_id": RUN_ID,
        "machine": "OF1",
        "op_ids": ["L-1:age", "L-2:age"],
        "kind": "age",
        "batch_id": "B-t-1",
        "recipe": "T66",
        "lot_ids": ["L-1", "L-2"],
    }
    assert engine.start_operation(age)["ok"]
    sync(engine, tracker)
    run_until_arrived(engine, tracker, "L-2", "saw")

    [saw] = next_commands(plan, tracker, views_of(engine), frozenset(), RUN_ID)
    assert saw == {"run_id": RUN_ID, "machine": "SAE", "op_ids": ["L-1:saw"], "kind": "saw", "lot_id": "L-1"}
    assert engine.start_operation(saw)["ok"]
    sync(engine, tracker)
    run_until_arrived(engine, tracker, "L-1", "pack")

    commands = next_commands(plan, tracker, views_of(engine), frozenset(), RUN_ID)
    pack = next(c for c in commands if c["kind"] == "pack")
    assert pack == {"run_id": RUN_ID, "machine": "VP", "op_ids": ["L-1:pack"], "kind": "pack", "lot_id": "L-1"}
    assert engine.start_operation(pack)["ok"]


def test_the_run_id_is_on_every_command_and_a_wrong_one_is_refused():
    engine = engine_at_start()
    lots, orders = realistic_lots(target=8, seed=3)
    plan = plan_from(engine, lots, orders)
    tracker = Tracker.from_state(engine.planning_state())
    commands = next_commands(plan, tracker, views_of(engine), frozenset(), "r-other")
    assert commands
    assert all(c["run_id"] == "r-other" for c in commands)
    assert engine.start_operation(commands[0])["reason"] == "STALE_RUN"


# ---------------------------------------------------------------------- who is free


@pytest.mark.parametrize("status", ["SETUP", "RUN", "FAULT"])
def test_a_machine_not_idle_gets_nothing(status):
    engine = engine_at_start()
    plan = plan_from(engine, [lot("L-1")], [order()])
    tracker = Tracker.from_state(engine.planning_state())
    assert next_commands(plan, tracker, idle_views(PR1=status)) == []
    assert len(next_commands(plan, tracker, idle_views())) == 1


def test_a_machine_nobody_reports_gets_nothing():
    engine = engine_at_start()
    plan = plan_from(engine, [lot("L-1")], [order()])
    tracker = Tracker.from_state(engine.planning_state())
    views = idle_views()
    del views["PR1"]
    assert next_commands(plan, tracker, views) == []


def test_the_api_spelling_of_status_works_too():
    engine = engine_at_start()
    plan = plan_from(engine, [lot("L-1")], [order()])
    tracker = Tracker.from_state(engine.planning_state())
    assert len(next_commands(plan, tracker, {m: {"status": "IDLE"} for m in MACHINE_ORDER})) == 1
    assert next_commands(plan, tracker, {m: {"status": "RUN"} for m in MACHINE_ORDER}) == []


def test_a_machine_with_a_command_in_flight_gets_no_second_one():
    engine = engine_at_start()
    lots = [lot("L-1"), lot("L-2")]
    plan = hand_plan(engine, lots, [order()], {"PR1": [["L-1:press"], ["L-2:press"]], "PR2": []})
    tracker = Tracker.from_state(engine.planning_state())
    [first] = next_commands(plan, tracker, idle_views())
    assert first["op_ids"] == ["L-1:press"]
    assert next_commands(plan, tracker, idle_views(), frozenset(first["op_ids"])) == [], "the head is commanded, the machine stays taken"


def test_commanded_ops_are_skipped_on_other_machines_only_as_their_own_machine_allows():
    engine = engine_at_start()
    lots = [lot("L-1"), lot("L-2")]
    plan = hand_plan(engine, lots, [order()], {"PR1": [["L-1:press"]], "PR2": [["L-2:press"]]})
    tracker = Tracker.from_state(engine.planning_state())
    commands = next_commands(plan, tracker, idle_views(), {"L-1:press"})
    assert [c["op_ids"] for c in commands] == [["L-2:press"]]


def test_a_machine_the_tracker_saw_starting_stays_taken_while_its_status_lags():
    engine = engine_at_start()
    lots = [lot("L-1"), lot("L-2")]
    plan = hand_plan(engine, lots, [order()], {"PR1": [["L-1:press"], ["L-2:press"]], "PR2": []})
    tracker = Tracker.from_state(engine.planning_state())
    assert engine.start_operation(next_commands(plan, tracker, views_of(engine), frozenset(), RUN_ID)[0])["ok"]
    sync(engine, tracker)
    assert next_commands(plan, tracker, idle_views(), frozenset(), RUN_ID) == [], "Status still says IDLE, the events know better"


def test_status_idle_ahead_of_the_events_still_waits_for_them():
    """The machine has finished and the DataChange is in, but op_finished is not: the tracker still has it running."""
    engine = engine_at_start()
    lots = [lot("L-1", n_billets=1), lot("L-2", n_billets=1)]
    plan = hand_plan(engine, lots, [order()], {"PR1": [["L-1:press"], ["L-2:press"]], "PR2": []})
    tracker = Tracker.from_state(engine.planning_state())
    assert engine.start_operation(next_commands(plan, tracker, views_of(engine), frozenset(), RUN_ID)[0])["ok"]
    sync(engine, tracker)
    engine.run_until(100.0)
    assert engine.machine_view("PR1")["Status"] == "IDLE"
    assert next_commands(plan, tracker, views_of(engine), frozenset(), RUN_ID) == []
    sync(engine, tracker)
    assert [c["op_ids"] for c in next_commands(plan, tracker, views_of(engine), frozenset(), RUN_ID)] == [["L-2:press"]]


# ---------------------------------------------------------------------- head of line


def test_head_of_line_is_binding():
    """The head waits for its predecessor. The op behind it could run at once and still has to wait."""
    engine = engine_at_start()
    lots = [lot("L-1", n_billets=1), lot("L-2", n_billets=1)]
    plan = hand_plan(
        engine, lots, [order()],
        {"PR1": [["L-1:press"]], "PR2": [["L-2:press"]], "OF1": [["L-2:age"], ["L-1:age"]], "OF2": []},
    )
    tracker = Tracker.from_state(engine.planning_state())
    for command in next_commands(plan, tracker, views_of(engine), frozenset(), RUN_ID):
        assert engine.start_operation(command)["ok"]
    sync(engine, tracker)
    # PR1 is faster, so L-1 reaches the furnace first. OF1's head is L-2.
    run_until_arrived(engine, tracker, "L-1", "age")
    assert not tracker.lot_at_stage("L-2", "age")
    commands = next_commands(plan, tracker, views_of(engine), frozenset(), RUN_ID)
    assert all(c["machine"] != "OF1" for c in commands)
    run_until_arrived(engine, tracker, "L-2", "age")
    commands = next_commands(plan, tracker, views_of(engine), frozenset(), RUN_ID)
    assert [c["op_ids"] for c in commands if c["machine"] == "OF1"] == [["L-2:age"]]


def test_started_and_done_ops_are_skipped():
    engine = engine_at_start()
    lots = [lot("L-1", n_billets=1), lot("L-2", n_billets=1), lot("L-3", n_billets=1)]
    plan = hand_plan(engine, lots, [order()], {"PR1": [["L-1:press"], ["L-2:press"], ["L-3:press"]], "PR2": []})
    tracker = Tracker.from_state(engine.planning_state())
    assert engine.start_operation(next_commands(plan, tracker, views_of(engine), frozenset(), RUN_ID)[0])["ok"]
    engine.run_until(50.0)
    sync(engine, tracker)
    assert [c["op_ids"] for c in next_commands(plan, tracker, views_of(engine), frozenset(), RUN_ID)] == [["L-2:press"]]


def test_a_partly_started_load_is_reduced_to_its_pending_members():
    engine = engine_at_start()
    lots = [lot("L-1", n_billets=1), lot("L-2", n_billets=1)]
    plan = hand_plan(
        engine, lots, [order()],
        {"PR1": [["L-1:press"]], "PR2": [["L-2:press"]], "OF2": [["L-1:age", "L-2:age"]], "OF1": []},
    )
    tracker = Tracker.from_state(engine.planning_state())
    for command in next_commands(plan, tracker, views_of(engine), frozenset(), RUN_ID):
        assert engine.start_operation(command)["ok"]
    sync(engine, tracker)
    run_until_arrived(engine, tracker, "L-1", "age")
    run_until_arrived(engine, tracker, "L-2", "age")
    # An older plan already put half of this load into the other furnace.
    assert engine.start_operation(
        {"machine": "OF1", "run_id": RUN_ID, "op_ids": ["L-1:age"], "kind": "age", "batch_id": "B-old", "recipe": "T66", "lot_ids": ["L-1"]}
    )["ok"]
    sync(engine, tracker)
    [command] = next_commands(plan, tracker, views_of(engine), frozenset(), RUN_ID)
    assert command["machine"] == "OF2"
    assert command["op_ids"] == ["L-2:age"]
    assert command["lot_ids"] == ["L-2"]
    assert command["batch_id"] == "B-t-1"
    assert engine.start_operation(command)["ok"]


def test_a_load_waits_until_every_member_has_arrived():
    engine = engine_at_start()
    lots = [lot("L-1", n_billets=1), lot("L-2", n_billets=4)]
    plan = hand_plan(
        engine, lots, [order()],
        {"PR1": [["L-1:press"]], "PR2": [["L-2:press"]], "OF2": [["L-1:age", "L-2:age"]], "OF1": []},
    )
    tracker = Tracker.from_state(engine.planning_state())
    for command in next_commands(plan, tracker, views_of(engine), frozenset(), RUN_ID):
        assert engine.start_operation(command)["ok"]
    sync(engine, tracker)
    run_until_arrived(engine, tracker, "L-1", "age")
    assert not tracker.lot_at_stage("L-2", "age")
    assert all(c["machine"] != "OF2" for c in next_commands(plan, tracker, views_of(engine), frozenset(), RUN_ID))
    run_until_arrived(engine, tracker, "L-2", "age")
    commands = next_commands(plan, tracker, views_of(engine), frozenset(), RUN_ID)
    assert [c["op_ids"] for c in commands if c["machine"] == "OF2"] == [["L-1:age", "L-2:age"]]


def test_a_lot_on_a_transport_does_not_start_even_though_its_predecessor_is_done():
    engine = engine_at_start()
    plan = plan_from(engine, [lot("L-1", n_billets=1)], [order()])
    tracker = Tracker.from_state(engine.planning_state())
    assert engine.start_operation(next_commands(plan, tracker, views_of(engine), frozenset(), RUN_ID)[0])["ok"]
    sync(engine, tracker)
    while tracker.op_state("L-1:press") != "done":
        engine.run_until(engine.next_event_time())
        sync(engine, tracker)
    assert not tracker.lot_at_stage("L-1", "age")
    assert next_commands(plan, tracker, views_of(engine), frozenset(), RUN_ID) == []
    run_until_arrived(engine, tracker, "L-1", "age")
    assert [c["kind"] for c in next_commands(plan, tracker, views_of(engine), frozenset(), RUN_ID)] == ["age"]


def test_a_press_waits_until_its_melt_has_arrived_not_just_until_it_is_cast():
    engine = engine_at_start()
    lots = [lot(f"L-{i}", "KK-80", 8) for i in range(1, 5)]  # 32 billets against 24 in stock: L-4 needs the melt
    plan = hand_plan(
        engine, lots, [order()],
        {"PR1": [["L-4:press"], ["L-1:press"], ["L-2:press"], ["L-3:press"]], "GUSS": [["M-6063-00001:cast"]], "PR2": []},
    )
    assert any(p.op_id == "M-6063-00001:cast" for p in plan.problem.op("L-4:press").preds)
    tracker = Tracker.from_state(engine.planning_state())
    commands = next_commands(plan, tracker, views_of(engine), frozenset(), RUN_ID)
    assert [c["kind"] for c in commands] == ["cast"], "L-4's melt is not cast yet, however many billets the store still holds"
    assert engine.start_operation(commands[0])["ok"]
    sync(engine, tracker)
    while tracker.op_state("M-6063-00001:cast") != "done":
        engine.run_until(engine.next_event_time())
        sync(engine, tracker)
    assert not tracker.melt_arrived("M-6063-00001")
    assert next_commands(plan, tracker, views_of(engine), frozenset(), RUN_ID) == []
    while not tracker.melt_arrived("M-6063-00001"):
        engine.run_until(engine.next_event_time())
        sync(engine, tracker)
    assert [c["op_ids"] for c in next_commands(plan, tracker, views_of(engine), frozenset(), RUN_ID)] == [["L-4:press"]]


def test_a_press_waits_for_a_melt_that_was_on_its_way_when_the_plan_was_built():
    engine = engine_at_start()
    lots = [lot(f"L-{i}", "KK-80", 8) for i in range(1, 5)]
    assert engine.start_operation(
        {"machine": "GUSS", "run_id": RUN_ID, "op_ids": ["M-6063-00001:cast"], "kind": "cast", "melt_plan_id": "M-6063-00001", "alloy": "6063"}
    )["ok"]
    tracker = Tracker.from_state(engine.planning_state())
    while tracker.op_state("M-6063-00001:cast") != "done":
        engine.run_until(engine.next_event_time())
        sync(engine, tracker)
    state = engine.planning_state()
    assert state.melts_in_transit
    result = build_problem(state, lots, [order()])
    problem = result.problem
    assert not any(op.kind == "cast" for op in problem.ops), "no new melt needed"
    last = problem.op("L-4:press")
    assert last.release > problem.now
    schedule = decode_edd(problem)
    plan = Plan(1, problem, schedule, tuple(lots))
    tracker = Tracker.from_state(state)
    # Hand only the late lot to a press: it has no predecessor, and the store could even serve it.
    forced = Plan(
        1, problem,
        Schedule("edd", "feasible", (Assignment("L-4:press", "PR1", last.release, last.release + 1),)),
        tuple(lots),
    )
    assert tracker.free_billets("6063", forced.lots_by_id) >= 8
    assert next_commands(forced, tracker, views_of(engine), frozenset(), RUN_ID) == []
    engine.run_until(state.melts_in_transit[0].arrives_min)
    sync(engine, tracker)
    assert [c["op_ids"] for c in next_commands(forced, tracker, views_of(engine), frozenset(), RUN_ID)] == [["L-4:press"]]
    assert plan.sequences


def test_two_presses_do_not_get_the_same_billets():
    engine = engine_at_start()
    lots = [lot("L-1", "ST-200", 8), lot("L-2", "FR-60", 8), lot("L-3", "FR-60", 8), lot("L-4", "FR-60", 8)]
    plan = hand_plan(engine, lots[:], [order()], {"PR1": [["L-2:press"]], "PR2": [["L-3:press"]]})
    engine._store["6060"].clear()
    engine._store["6060"].extend({"id": f"B-{i}", "melt_id": "S-00001"} for i in range(8))
    tracker = Tracker.from_state(engine.planning_state())
    commands = next_commands(plan, tracker, views_of(engine), frozenset(), RUN_ID)
    assert len(commands) == 1, "only 8 billets: one press starts"
    assert engine.start_operation(commands[0])["ok"]


def test_billets_of_a_commanded_press_are_not_promised_again():
    engine = engine_at_start()
    lots = [lot("L-2", "FR-60", 8), lot("L-3", "FR-60", 8)]
    plan = hand_plan(engine, lots, [order()], {"PR1": [["L-2:press"]], "PR2": [["L-3:press"]]})
    engine._store["6060"].clear()
    engine._store["6060"].extend({"id": f"B-{i}", "melt_id": "S-00001"} for i in range(8))
    tracker = Tracker.from_state(engine.planning_state())
    assert next_commands(plan, tracker, views_of(engine), {"L-2:press"}, RUN_ID) == []


def test_a_frozen_op_is_never_commanded_even_by_a_tracker_behind_the_plan():
    engine = engine_at_start()
    lots = [lot("L-1", n_billets=1)]
    state = engine.planning_state()
    problem = build_problem(state, lots, [order()]).problem
    from dataclasses import replace

    frozen = Frozen("PR1", 0, 50)
    op = problem.op("L-1:press")
    problem = replace(problem, ops=tuple(replace(o, frozen=frozen) if o.id == op.id else o for o in problem.ops))
    schedule = Schedule("edd", "feasible", (Assignment("L-1:press", "PR1", 0, 50),))
    plan = Plan(1, problem, schedule, tuple(lots))
    assert next_commands(plan, Tracker.from_state(state), idle_views(), frozenset(), RUN_ID) == []


def test_next_commands_is_pure():
    engine = engine_at_start()
    lots, orders = realistic_lots(target=10, seed=4)
    plan = plan_from(engine, lots, orders)
    tracker = Tracker.from_state(engine.planning_state())
    views = views_of(engine)
    before = dict(views)
    first = next_commands(plan, tracker, views, frozenset(), RUN_ID)
    assert next_commands(plan, tracker, views, frozenset(), RUN_ID) == first
    assert views == before
    assert tracker.last_seq == engine.seq
    assert not engine.export_snapshot()["started_op_ids"]


# ---------------------------------------------------------------------- head report for the UI


def test_machine_heads_say_what_each_machine_waits_for():
    engine = engine_at_start()
    lots = [lot("L-1", n_billets=1), lot("L-2", n_billets=1)]
    plan = hand_plan(engine, lots, [order()], {"PR1": [["L-1:press"], ["L-2:press"]], "OF1": [["L-1:age", "L-2:age"]], "SAE": [["L-1:saw"]]})
    tracker = Tracker.from_state(engine.planning_state())
    heads = machine_heads(plan, tracker, idle_views(VP="FAULT"))
    assert heads["PR1"].next_op == "L-1:press" and heads["PR1"].waiting_on is None
    assert heads["OF1"].next_op == "L-1:age" and heads["OF1"].waiting_on == "Vorgänger"
    assert heads["VP"].waiting_on == "Störung"
    assert heads["PR2"].next_op is None and heads["PR2"].waiting_on == "Kein Auftrag"
    assert engine.start_operation(next_commands(plan, tracker, views_of(engine), frozenset(), RUN_ID)[0])["ok"]
    sync(engine, tracker)
    while tracker.op_state("L-1:press") != "done":
        engine.run_until(engine.next_event_time())
        sync(engine, tracker)
    heads = machine_heads(plan, tracker, views_of(engine))
    assert heads["SAE"].waiting_on == "Vorgänger"
    # L-2 still has no predecessor, so the press is free to go; its age op has one.
    assert heads["PR1"].next_op == "L-2:press"
    assert machine_heads(None, tracker, idle_views())["PR1"].waiting_on == "Kein Auftrag"


def test_machine_heads_look_past_what_is_already_commanded():
    engine = engine_at_start()
    lots = [lot("L-1", n_billets=1), lot("L-2", n_billets=1)]
    plan = hand_plan(engine, lots, [order()], {"PR1": [["L-1:press"], ["L-2:press"]]})
    tracker = Tracker.from_state(engine.planning_state())
    assert machine_heads(plan, tracker, idle_views())["PR1"].next_op == "L-1:press"
    assert machine_heads(plan, tracker, idle_views(), {"L-1:press"})["PR1"].next_op == "L-2:press"


def test_machine_heads_report_missing_material():
    engine = engine_at_start()
    plan = plan_from(engine, [lot("L-1", n_billets=1)], [order()])
    tracker = Tracker.from_state(engine.planning_state())
    assert engine.start_operation(next_commands(plan, tracker, views_of(engine), frozenset(), RUN_ID)[0])["ok"]
    sync(engine, tracker)
    while tracker.op_state("L-1:press") != "done":
        engine.run_until(engine.next_event_time())
        sync(engine, tracker)
    furnace = plan.schedule.assignments[1].machine
    assert machine_heads(plan, tracker, views_of(engine))[furnace].waiting_on == "Material"


# ---------------------------------------------------------------------- Dispatcher


def test_dispatcher_records_commanded_before_anything_is_sent():
    engine = engine_at_start()
    plan = plan_from(engine, [lot("L-1")], [order()])
    tracker = Tracker.from_state(engine.planning_state())
    dispatcher = Dispatcher(RUN_ID)
    [command] = dispatcher.poll(plan, tracker, views_of(engine), 0.0)
    assert dispatcher.commanded == {"L-1:press"}
    assert dispatcher.poll(plan, tracker, views_of(engine), 0.1) == [], "no answer yet: still in flight"


def test_dispatcher_clears_only_on_a_definite_refusal():
    engine = engine_at_start()
    plan = plan_from(engine, [lot("L-1")], [order()])
    tracker = Tracker.from_state(engine.planning_state())
    dispatcher = Dispatcher(RUN_ID, backoff=2.0)
    [command] = dispatcher.poll(plan, tracker, views_of(engine), 0.0)

    dispatcher.on_result(plan, tracker, command, {"ok": True, "reason": ""}, 0.1)
    assert dispatcher.commanded == {"L-1:press"}
    dispatcher.on_result(plan, tracker, command, {"ok": False, "reason": "ALREADY_STARTED"}, 0.1)
    assert dispatcher.commanded == {"L-1:press"}, "it started: the events will say so"
    dispatcher.on_result(plan, tracker, command, {"ok": False, "reason": "SOMETHING_NEW"}, 0.1)
    assert dispatcher.commanded == {"L-1:press"}, "not known to have done nothing"

    dispatcher.on_result(plan, tracker, command, {"ok": False, "reason": "MACHINE_BUSY"}, 1.0)
    assert dispatcher.commanded == set()
    assert dispatcher.last_refusal["reason"] == "MACHINE_BUSY"


def test_dispatcher_backs_off_on_a_refusal_for_that_machine_only():
    engine = engine_at_start()
    lots = [lot("L-1"), lot("L-2")]
    plan = hand_plan(engine, lots, [order()], {"PR1": [["L-1:press"]], "PR2": [["L-2:press"]]})
    tracker = Tracker.from_state(engine.planning_state())
    dispatcher = Dispatcher(RUN_ID, backoff=2.0)
    commands = dispatcher.poll(plan, tracker, views_of(engine), 0.0)
    assert len(commands) == 2
    dispatcher.on_result(plan, tracker, commands[0], {"ok": False, "reason": "MACHINE_FAULT"}, 0.0)
    assert dispatcher.poll(plan, tracker, views_of(engine), 1.9) == []
    assert [c["op_ids"] for c in dispatcher.poll(plan, tracker, views_of(engine), 2.0)] == [commands[0]["op_ids"]]


def test_dispatcher_forgets_a_commanded_op_once_it_started():
    engine = engine_at_start()
    plan = plan_from(engine, [lot("L-1", n_billets=1)], [order()])
    tracker = Tracker.from_state(engine.planning_state())
    dispatcher = Dispatcher(RUN_ID)
    [command] = dispatcher.poll(plan, tracker, views_of(engine), 0.0)
    assert engine.start_operation(command)["ok"]
    sync(engine, tracker)
    dispatcher.poll(plan, tracker, views_of(engine), 0.1)
    assert dispatcher.commanded == set()


def test_no_material_with_no_planned_supply_asks_for_a_replan():
    engine = engine_at_start()
    lots = [lot(f"L-{i}", "FR-60", 8) for i in range(1, 5)]
    plan = hand_plan(engine, lots, [order()], {"PR1": [[f"L-{i}:press"] for i in range(1, 5)]}, drop_ops=("M-6060-00001:cast",))
    tracker = Tracker.from_state(engine.planning_state())
    assert billet_shortfall(plan, tracker) == {"6060": 8}
    dispatcher = Dispatcher(RUN_ID)
    command = {"machine": "PR1", "op_ids": ["L-4:press"]}
    dispatcher.on_result(plan, tracker, command, {"ok": False, "reason": "NO_MATERIAL"}, 0.0)
    assert dispatcher.replan_needed


def test_a_melt_in_transit_counts_as_supply():
    engine = engine_at_start()
    lots = [lot(f"L-{i}", "FR-60", 8) for i in range(1, 5)]
    plan = hand_plan(engine, lots, [order()], {"PR1": [[f"L-{i}:press"] for i in range(1, 5)]}, drop_ops=("M-6060-00001:cast",))
    assert engine.start_operation(
        {"machine": "GUSS", "run_id": RUN_ID, "op_ids": ["M-6060-00009:cast"], "kind": "cast", "melt_plan_id": "M-6060-00009", "alloy": "6060"}
    )["ok"]
    tracker = Tracker.from_state(engine.planning_state())
    assert billet_shortfall(plan, tracker) == {}, "a running cast this plan does not list still delivers"
    while tracker.op_state("M-6060-00009:cast") != "done":
        engine.run_until(engine.next_event_time())
        sync(engine, tracker)
    assert tracker.melts_in_transit()
    assert billet_shortfall(plan, tracker) == {}, "and so does its melt on the way"


def test_no_material_with_supply_on_its_way_does_not():
    engine = engine_at_start()
    lots = [lot(f"L-{i}", "FR-60", 8) for i in range(1, 5)]
    plan = plan_from(engine, lots, [order()])
    tracker = Tracker.from_state(engine.planning_state())
    assert billet_shortfall(plan, tracker) == {}
    dispatcher = Dispatcher(RUN_ID)
    dispatcher.on_result(plan, tracker, {"machine": "PR1", "op_ids": ["L-4:press"]}, {"ok": False, "reason": "NO_MATERIAL"}, 0.0)
    assert not dispatcher.replan_needed


def test_forget_unconfirmed_lets_a_command_go_out_again_and_the_plant_refuses_a_repeat():
    engine = engine_at_start()
    plan = plan_from(engine, [lot("L-1")], [order()])
    tracker = Tracker.from_state(engine.planning_state())
    dispatcher = Dispatcher(RUN_ID)
    [command] = dispatcher.poll(plan, tracker, views_of(engine), 0.0)
    assert engine.start_operation(command)["ok"]  # the answer gets lost
    dispatcher.forget_unconfirmed()
    assert dispatcher.commanded == set()
    [again] = dispatcher.poll(plan, tracker, idle_views(), 1.0)
    assert again == command
    assert engine.start_operation(again)["reason"] == "ALREADY_STARTED"


# ---------------------------------------------------------------------- a plant that answers late


class LatePlant:
    """The MES's view of a plant behind OPC UA: commands act at once, status and events reach it `delay` seconds later.

    Virtual time, so the delay is exact and the test independent of the machine's load. Both streams lag on their own
    schedule, as they do for real: DataChange and GetEvents are not synchronised.
    """

    def __init__(self, engine: SimEngine, factor: float, delay_s: float, event_delay_s: float | None = None) -> None:
        self.engine = engine
        self.factor = factor
        self.delay_s = delay_s
        self.event_delay_s = delay_s if event_delay_s is None else event_delay_s
        self.wall = 0.0
        self.t0 = engine.t
        self._views: deque[tuple[float, dict]] = deque()
        self._events: deque[tuple[float, dict]] = deque()
        self._seen_seq = engine.seq
        self.view = views_of(engine)

    def advance(self, step_s: float) -> None:
        self.wall += step_s
        self.engine.run_until(self.t0 + self.wall * self.factor / 60.0)
        self._views.append((self.wall + self.delay_s, {m: {"Status": self.engine.machine_view(m)["Status"]} for m in MACHINE_ORDER}))
        for event in self.engine.events_after(self._seen_seq, 5000)["events"]:
            self._seen_seq = event["seq"]
            self._events.append((self.wall + self.event_delay_s, event))
        while self._views and self._views[0][0] <= self.wall:
            self.view = self._views.popleft()[1]

    def arrived_events(self) -> list[dict]:
        arrived = []
        while self._events and self._events[0][0] <= self.wall:
            arrived.append(self._events.popleft()[1])
        return arrived


@pytest.mark.parametrize(
    "factor, delay_s, event_delay_s",
    [(120.0, 0.15, 0.15), (480.0, 0.15, 0.15), (240.0, 0.15, 0.6), (240.0, 0.6, 0.15), (480.0, 0.05, 0.3)],
)
def test_a_late_view_of_the_plant_never_produces_a_double_start(factor, delay_s, event_delay_s):
    lots, orders = realistic_lots(target=8, seed=11)
    engine = engine_at_start(11)
    plan = plan_from(engine, lots, orders)
    plant = LatePlant(engine, factor, delay_s, event_delay_s)
    tracker = Tracker.from_state(engine.planning_state())
    dispatcher = Dispatcher(RUN_ID)
    sent: Counter[str] = Counter()
    refused: list[dict] = []
    for _ in range(400_000):
        plant.advance(0.05)
        for event in plant.arrived_events():
            tracker.apply(event)
        if all(tracker.op_state(op.id) == "done" for op in plan.problem.ops):
            break
        for command in dispatcher.poll(plan, tracker, plant.view, plant.wall):
            sent.update(command["op_ids"])
            result = engine.start_operation(command)
            if not result["ok"]:
                refused.append({**result, "command": command, "t": engine.t})
            dispatcher.on_result(plan, tracker, command, result, plant.wall)
    else:
        raise AssertionError("did not finish")
    assert not refused, refused[:3]
    assert all(count == 1 for count in sent.values()), [op for op, count in sent.items() if count > 1]
    assert set(sent) == {op.id for op in plan.problem.ops}
    assert not tracker.resync_needed


def test_the_late_plant_really_bites_a_dispatcher_with_no_memory():
    """The negative control: same plant and plan, no commanded set. If this passed, the test above would prove nothing."""
    lots, orders = realistic_lots(target=8, seed=11)
    engine = engine_at_start(11)
    plan = plan_from(engine, lots, orders)
    plant = LatePlant(engine, 120.0, 0.15)
    tracker = Tracker.from_state(engine.planning_state())
    double = 0
    for _ in range(2000):
        plant.advance(0.05)
        for event in plant.arrived_events():
            tracker.apply(event)
        for command in next_commands(plan, tracker, plant.view, frozenset(), RUN_ID):
            if engine.start_operation(command)["reason"] in ("ALREADY_STARTED", "MACHINE_BUSY"):
                double += 1
    assert double > 0


def test_a_late_loop_finishes_as_a_prompt_one_on_a_deterministic_plant():
    """The same plan with and without lag ends within what the lag can add per op, so nothing was skipped or held up."""
    lots, orders = realistic_lots(target=6, seed=12)
    prompt_engine = engine_at_start(12)
    prompt_plan = plan_from(prompt_engine, lots, orders)
    prompt = ClosedLoop(prompt_engine, prompt_plan)
    assert not prompt.run().stalled
    last_prompt = max(prompt.finished_at.values())

    engine = engine_at_start(12)
    plan = plan_from(engine, lots, orders)
    plant = LatePlant(engine, 120.0, 0.15)
    tracker = Tracker.from_state(engine.planning_state())
    dispatcher = Dispatcher(RUN_ID)
    finished: dict[str, float] = {}
    for _ in range(200_000):
        plant.advance(0.05)
        for event in plant.arrived_events():
            tracker.apply(event)
            if event["type"] == "op_finished":
                for op_id in event["op_ids"]:
                    finished[op_id] = event["t"]
        if len(finished) == len(plan.problem.ops):
            break
        for command in dispatcher.poll(plan, tracker, plant.view, plant.wall):
            dispatcher.on_result(plan, tracker, command, engine.start_operation(command), plant.wall)
    assert len(finished) == len(plan.problem.ops)
    assert last_prompt <= max(finished.values()) <= last_prompt + 60


def test_pred_on_an_op_that_is_not_in_the_plan_still_blocks():
    """Dropped ops are the other way to a stall, so it has to show as waiting, not as a start ahead of its predecessor."""
    engine = engine_at_start()
    lots = [lot("L-1", n_billets=1)]
    plan = hand_plan(engine, lots, [order()], {"OF1": [["L-1:age"]]}, drop_ops=("L-1:press",))
    assert plan.problem.op("L-1:age").preds == (Pred("L-1:press", catalog.TRANSPORT_MIN),)
    tracker = Tracker.from_state(engine.planning_state())
    assert next_commands(plan, tracker, views_of(engine), frozenset(), RUN_ID) == []
