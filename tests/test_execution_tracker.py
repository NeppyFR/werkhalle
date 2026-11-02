"""Tracker: from a state, from events, de-duplication, gaps, and agreement with what the plant itself reports."""
from __future__ import annotations

import pytest
from test_execution_support import ClosedLoop, RUN_ID, engine_at_start, lot, order, plan_from, sync
from test_scheduler_support import realistic_lots

from werkhalle.domain import catalog
from werkhalle.domain.catalog import AGE, PACK, PRESS, SAW
from werkhalle.domain.orders import Lot
from werkhalle.domain.state import LotInTransit, MachineState, MeltInTransit, PlanningState
from werkhalle.execution import Tracker
from werkhalle.scheduling import decode_edd
from werkhalle.simcore import SimEngine


def press_command(lot_id="L-1", product="FR-60", n=8, machine="PR1") -> dict:
    return {
        "machine": machine, "run_id": RUN_ID, "op_ids": [f"{lot_id}:press"], "kind": "press", "lot_id": lot_id,
        "order_id": "A-1", "product_id": product, "n_billets": n,
    }


def cast_command(plan_id="M-6060-00001", alloy="6060") -> dict:
    return {"machine": "GUSS", "run_id": RUN_ID, "op_ids": [f"{plan_id}:cast"], "kind": "cast", "melt_plan_id": plan_id, "alloy": alloy}


def test_from_state_of_a_fresh_plant():
    engine = engine_at_start()
    tracker = Tracker.from_state(engine.planning_state())
    assert tracker.last_seq == engine.seq == 6
    assert tracker.op_state("L-1:press") == "pending"
    assert tracker.melt_arrived("BESTAND")
    assert not tracker.melt_arrived("M-6060-00001")
    assert not tracker.transports_pending()
    assert not tracker.running()
    for alloy, count in catalog.INITIAL_BILLET_STOCK.items():
        assert tracker.free_billets(alloy) == count
    assert tracker.setup_class("PR1") == catalog.INITIAL_SETUP_CLASS["PR1"]


def test_initial_stock_events_are_history_for_a_tracker_from_state():
    engine = engine_at_start()
    tracker = Tracker.from_state(engine.planning_state())
    for event in engine.events_after(0)["events"]:
        tracker.apply(event)
    assert not tracker.resync_needed
    assert tracker.last_seq == 6
    assert tracker.free_billets("6060") == catalog.INITIAL_BILLET_STOCK["6060"]


def test_op_state_follows_start_and_finish():
    engine = engine_at_start()
    tracker = Tracker.from_state(engine.planning_state())
    assert engine.start_operation(press_command(n=2))["ok"]
    assert tracker.op_state("L-1:press") == "pending", "only events move a tracker"
    sync(engine, tracker)
    assert tracker.op_state("L-1:press") == "running"
    running = tracker.running_on("PR1")
    assert running.op_ids == ("L-1:press",)
    assert running.lot_ids == ("L-1",)
    assert running.started_min == 0.0
    engine.run_until(1000.0)
    sync(engine, tracker)
    assert tracker.op_state("L-1:press") == "done"
    assert tracker.running_on("PR1") is None


def test_a_repeated_event_changes_nothing():
    engine = engine_at_start()
    tracker = Tracker.from_state(engine.planning_state())
    assert engine.start_operation(press_command(n=1))["ok"]
    assert engine.start_operation(cast_command())["ok"]
    engine.run_until(300.0)
    events = engine.events_after(tracker.last_seq)["events"]
    assert any(e["type"] == "melt_arrived" for e in events)
    for event in events:
        tracker.apply(event)
    billets = tracker.free_billets("6060")
    seq = tracker.last_seq
    state = (tracker.op_state("L-1:press"), tracker.lot_at_stage("L-1", "age"), tracker.melt_arrived("M-6060-00001"))
    for event in events:
        tracker.apply(event)
    assert tracker.free_billets("6060") == billets == catalog.INITIAL_BILLET_STOCK["6060"] - 1 + catalog.BILLETS_PER_MELT
    assert tracker.last_seq == seq
    assert not tracker.resync_needed
    assert state == (tracker.op_state("L-1:press"), tracker.lot_at_stage("L-1", "age"), tracker.melt_arrived("M-6060-00001"))


def test_an_older_event_is_ignored():
    engine = engine_at_start()
    tracker = Tracker.from_state(engine.planning_state())
    assert engine.start_operation(press_command(n=1))["ok"]
    engine.run_until(100.0)
    events = engine.events_after(tracker.last_seq)["events"]
    finished = next(e for e in events if e["type"] == "op_finished")
    for event in events:
        tracker.apply(event)
    moved = tracker.t
    tracker.apply({"seq": finished["seq"], "t": 0.0, "type": "op_started", "machine": "PR1", "op_ids": ["L-9:press"], "kind": "press",
                   "lot_ids": ["L-9"], "batch_id": None, "setup_min": 0, "setup_class": "6060|M-1001"})
    assert tracker.op_state("L-9:press") == "pending"
    assert tracker.t == moved


def test_a_gap_sets_resync_needed():
    engine = engine_at_start()
    tracker = Tracker.from_state(engine.planning_state())
    assert engine.start_operation(press_command(n=2))["ok"]
    engine.run_until(100.0)
    events = engine.events_after(tracker.last_seq)["events"]
    assert len(events) > 2
    tracker.apply(events[0])
    assert not tracker.resync_needed
    tracker.apply(events[2])
    assert tracker.resync_needed
    assert tracker.last_seq == events[2]["seq"]
    # Sticky: only a new tracker from a fresh state clears it.
    tracker.apply(events[3])
    assert tracker.resync_needed


def test_contiguous_events_never_set_resync():
    lots, orders = realistic_lots(target=6, seed=2)
    engine = engine_at_start(2, True)
    loop = ClosedLoop(engine, plan_from(engine, lots, orders))
    assert not loop.run().stalled
    assert not loop.tracker.resync_needed
    assert loop.tracker.last_seq == engine.seq


def test_lot_at_stage_through_the_whole_route():
    engine = engine_at_start()
    tracker = Tracker.from_state(engine.planning_state())
    lot_id = "L-1"

    def where() -> list[str]:
        return [stage for stage in ("age", "saw", "pack", "ship") if tracker.lot_at_stage(lot_id, stage)]

    assert engine.start_operation(press_command(lot_id, n=1))["ok"]
    sync(engine, tracker)
    assert where() == []
    engine.run_until(engine.next_event_time())
    while tracker.op_state(f"{lot_id}:press") != "done":
        sync(engine, tracker)
        if tracker.op_state(f"{lot_id}:press") != "done":
            engine.run_until(engine.next_event_time())
    assert where() == [], "on the transport, not at the stage yet"
    stage, arrival = tracker.lot_arrival(lot_id)
    assert stage == "age" and arrival == pytest.approx(engine.t + catalog.TRANSPORT_MIN)
    assert tracker.transports_pending()
    engine.run_until(engine.t + catalog.TRANSPORT_MIN)
    sync(engine, tracker)
    assert where() == ["age"]
    assert tracker.lot_arrival(lot_id) is None
    assert engine.start_operation(
        {"machine": "OF1", "run_id": RUN_ID, "op_ids": [f"{lot_id}:age"], "kind": "age", "batch_id": "B-1", "recipe": "T66", "lot_ids": [lot_id]}
    )["ok"]
    sync(engine, tracker)
    assert where() == [], "in the furnace, not in its buffer"
    engine.run_until(engine.t + 1000)
    sync(engine, tracker)
    assert where() == ["saw"]
    assert engine.start_operation({"machine": "SAE", "run_id": RUN_ID, "op_ids": [f"{lot_id}:saw"], "kind": "saw", "lot_id": lot_id})["ok"]
    engine.run_until(engine.t + 100)
    sync(engine, tracker)
    assert where() == ["pack"]
    assert engine.start_operation({"machine": "VP", "run_id": RUN_ID, "op_ids": [f"{lot_id}:pack"], "kind": "pack", "lot_id": lot_id})["ok"]
    engine.run_until(engine.t + 100)
    sync(engine, tracker)
    assert where() == ["ship"]


def test_melt_arrived_only_when_the_billets_are_there():
    engine = engine_at_start()
    tracker = Tracker.from_state(engine.planning_state())
    assert engine.start_operation(cast_command())["ok"]
    sync(engine, tracker)
    assert tracker.op_state("M-6060-00001:cast") == "running"
    assert not tracker.melt_arrived("M-6060-00001")
    while tracker.op_state("M-6060-00001:cast") != "done":
        engine.run_until(engine.next_event_time())
        sync(engine, tracker)
    assert not tracker.melt_arrived("M-6060-00001"), "cast, but still on its way"
    moving = {m.plan_id: m for m in tracker.melts_in_transit()}
    assert moving["M-6060-00001"].alloy == "6060"
    assert moving["M-6060-00001"].n_billets == catalog.BILLETS_PER_MELT
    assert moving["M-6060-00001"].arrives_min == pytest.approx(engine.t + catalog.TRANSPORT_MIN)
    before = tracker.free_billets("6060")
    engine.run_until(engine.t + catalog.TRANSPORT_MIN)
    sync(engine, tracker)
    assert tracker.melt_arrived("M-6060-00001")
    assert tracker.melts_in_transit() == ()
    assert tracker.free_billets("6060") == before + catalog.BILLETS_PER_MELT


def _same_picture(event_fed: Tracker, fresh: Tracker, engine: SimEngine, lots: list[Lot]) -> None:
    """A tracker fed by events has to say what one built from a state at that instant says, and what the plant says."""
    lots_by_id = {item.id: item for item in lots}
    snapshot = engine.export_snapshot()
    assert event_fed.last_seq == fresh.last_seq == snapshot["seq"]
    for item in lots:
        for kind in (PRESS, AGE, SAW, PACK):
            op_id = item.op_id(kind)
            assert event_fed.op_state(op_id) == fresh.op_state(op_id), op_id
        for stage in ("age", "saw", "pack", "ship"):
            assert event_fed.lot_at_stage(item.id, stage) == fresh.lot_at_stage(item.id, stage), (item.id, stage)
        assert (event_fed.lot_arrival(item.id) is None) == (fresh.lot_arrival(item.id) is None), item.id
    for alloy in catalog.ALLOYS:
        store = len(snapshot["billet_store"][alloy])
        assert event_fed.free_billets(alloy, lots_by_id) == store, alloy
        assert fresh.free_billets(alloy, lots_by_id) == store, alloy
    for machine in catalog.MACHINE_ORDER:
        fed = event_fed.running_on(machine)
        state = fresh.running_on(machine)
        assert (fed.op_ids if fed else ()) == (state.op_ids if state else ()), machine
        assert (fed.lot_ids if fed else ()) == (state.lot_ids if state else ()), machine
        assert event_fed.setup_class(machine) == fresh.setup_class(machine), machine
    assert {m.plan_id: m.alloy for m in event_fed.melts_in_transit()} == {m.plan_id: m.alloy for m in fresh.melts_in_transit()}
    assert event_fed.transports_pending() == fresh.transports_pending() == bool(snapshot["transports"])


@pytest.mark.parametrize("seed, stochastic", [(3, False), (4, True), (5, True)])
def test_events_and_state_agree_at_every_step_of_a_run(seed, stochastic):
    lots, orders = realistic_lots(target=12, seed=seed)
    engine = engine_at_start(seed, stochastic)
    loop = ClosedLoop(engine, plan_from(engine, lots, orders))
    checked = 0

    def check(current: ClosedLoop) -> None:
        nonlocal checked
        current.collect()
        _same_picture(current.tracker, Tracker.from_state(engine.planning_state()), engine, lots)
        checked += 1

    assert not loop.run(on_step=check).stalled
    assert checked > 150


def test_from_state_reads_a_handmade_state():
    state = PlanningState(
        run_id="x",
        seq=40,
        now=100.0,
        machines=(
            MachineState("PR1", "FAULT", "6060|M-1001", ("L-1:press",), "press", ("L-1",), None, 50.0, 30.0, "P-201", 90.0, 5.0, 85.0),
            MachineState("OF1", "RUN", "T66", ("L-2:age", "L-3:age"), "age", ("L-2", "L-3"), "B-1", 70.0, 200.0),
            MachineState("VP", "IDLE", "B-16"),
        ),
        billet_store={"6060": 5},
        melts_in_transit=(MeltInTransit("S-00009", "M-6063-00002", "6063", 24, 104.0),),
        lots_in_transit=(LotInTransit("L-4", "saw", 103.0), LotInTransit("L-5", "ship", 107.0)),
        lots_buffered={"L-6": "pack"},
        started_op_ids=frozenset({"L-1:press", "L-2:age", "L-3:age", "M-6063-00002:cast", "M-6060-00001:cast", "L-4:age", "L-5:pack", "L-7:pack"}),
        finished_op_ids=frozenset({"M-6063-00002:cast", "M-6060-00001:cast", "L-4:age", "L-5:pack", "L-7:pack"}),
    )
    tracker = Tracker.from_state(state)
    assert tracker.last_seq == 40
    assert tracker.t == 100.0
    assert tracker.op_state("L-1:press") == "running", "a fault pauses an op, it stays on the machine"
    assert tracker.op_state("L-4:age") == "done"
    assert tracker.running_on("OF1").op_ids == ("L-2:age", "L-3:age")
    assert tracker.running_on("OF1").batch_id == "B-1"
    assert tracker.running_on("OF1").est_end_min == 300.0
    assert tracker.melt_arrived("M-6060-00001")
    assert not tracker.melt_arrived("M-6063-00002")
    assert tracker.lot_arrival("L-4") == ("saw", 103.0)
    assert not tracker.lot_at_stage("L-4", "saw")
    assert tracker.lot_at_stage("L-6", "pack")
    assert tracker.lot_at_stage("L-7", "ship")
    assert not tracker.lot_at_stage("L-5", "ship"), "packed, but still on the way to shipping"
    assert tracker.free_billets("6060") == 5
    assert tracker.free_billets("6063") == 0


def test_free_billets_of_a_running_press_it_has_no_data_for():
    engine = engine_at_start()
    tracker = Tracker.from_state(engine.planning_state())
    assert engine.start_operation(press_command(n=3))["ok"]
    sync(engine, tracker)
    store = catalog.INITIAL_BILLET_STOCK["6060"]
    assert tracker.free_billets("6060", {"L-1": lot("L-1", n_billets=3)}) == store - 3
    assert tracker.free_billets("6060") == store - catalog.MAX_BILLETS_PER_LOT, "unknown lot counts as a full one"
    engine.run_until(500.0)
    sync(engine, tracker)
    assert tracker.free_billets("6060") == store - 3, "when it is done the events say what it used"


def test_a_press_running_when_the_state_was_taken_is_not_taken_off_twice():
    engine = engine_at_start()
    assert engine.start_operation(press_command(n=3))["ok"]
    engine.run_until(2.0)
    tracker = Tracker.from_state(engine.planning_state())
    store = catalog.INITIAL_BILLET_STOCK["6060"] - 3
    assert tracker.free_billets("6060") == store
    engine.run_until(500.0)
    sync(engine, tracker)
    assert tracker.op_state("L-1:press") == "done"
    assert tracker.free_billets("6060") == store


def test_plan_helper_runs_cleanly_with_real_events():
    """Guards the support code itself: a small plan goes through and every op finishes exactly once."""
    lots = [lot("L-1"), lot("L-2", "KK-80", 5, "A-2")]
    orders = [order("A-1"), order("A-2")]
    engine = engine_at_start()
    plan = plan_from(engine, lots, orders, decode_edd)
    loop = ClosedLoop(engine, plan)
    result = loop.run()
    assert not result.stalled and not result.refusals
    assert set(loop.finished_at) == {op.id for op in plan.problem.ops}
