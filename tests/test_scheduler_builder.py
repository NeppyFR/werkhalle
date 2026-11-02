"""The builder: melt allocation rules 1 to 4 of PLAN section 5, frozen ops, downtimes, slowdowns, from fresh and mid-run states."""
from __future__ import annotations

import pytest
from test_scheduler_support import assert_valid, lot, order, state_at

from werkhalle.domain.catalog import BILLETS_PER_MELT, TRANSPORT_MIN
from werkhalle.domain.state import LotInTransit, MachineState, MeltInTransit
from werkhalle.scheduling import BilletSupply, Downtime, Frozen, Pred, build_problem, decode_edd

NO_STOCK = {"6060": 0, "6063": 0, "6082": 0}


def casts_of(problem):
    return [op.id[: -len(":cast")] for op in problem.ops if op.kind == "cast" and op.frozen is None]


def fr60_lots(n: int, n_billets: int = 8):
    return [lot(f"L-{i:05d}", "FR-60", n_billets) for i in range(1, n + 1)]


# ---- Durations and routes -------------------------------------------------------------------------------------------


def test_fresh_lot_has_four_ops_with_planning_rate_durations():
    problem = build_problem(state_at(), [lot("L-00001", "FR-60", 8)], [order()]).problem
    ops = {op.id: op for op in problem.ops}
    assert set(ops) == {"L-00001:press", "L-00001:age", "L-00001:saw", "L-00001:pack"}
    press = ops["L-00001:press"]
    # 7 min per billet on PR1 and 8 on PR2, times 8 billets, times allowance 1.08, rounded up.
    assert press.durations == {"PR1": 61, "PR2": 70}
    assert press.setup_class == "6060|M-1001"
    assert press.billets_in == 8 and press.alloy == "6060"
    age = ops["L-00001:age"]
    assert age.durations == {"OF1": 300, "OF2": 300}
    assert (age.size, age.batch_family, age.setup_class) == (2, "T66", "T66")
    # 64 bars at 0.25 min, times 1.06; 4 packages at 6 min, times 1.05.
    assert ops["L-00001:saw"].durations == {"SAE": 17}
    assert ops["L-00001:pack"].durations == {"VP": 26}
    assert age.preds == (Pred("L-00001:press", TRANSPORT_MIN),)
    assert ops["L-00001:saw"].preds == (Pred("L-00001:age", TRANSPORT_MIN),)
    assert ops["L-00001:pack"].preds == (Pred("L-00001:saw", TRANSPORT_MIN),)


def test_st200_is_eligible_on_pr2_only():
    problem = build_problem(state_at(), [lot("L-00001", "ST-200", 8)], [order()]).problem
    assert problem.op("L-00001:press").durations == {"PR2": 96}


def test_machines_are_complete_and_furnaces_are_batch_machines():
    problem = build_problem(state_at(), [], []).problem
    machines = {m.id: m for m in problem.machines}
    assert set(machines) == {"GUSS", "PR1", "PR2", "OF1", "OF2", "SAE", "VP"}
    assert machines["OF1"].is_batch and machines["OF1"].capacity == 6
    assert machines["OF2"].is_batch and machines["OF2"].capacity == 10
    assert not machines["PR1"].is_batch
    assert machines["OF1"].initial_setup_class == "T66"


def test_unknown_order_is_refused():
    with pytest.raises(ValueError):
        build_problem(state_at(), [lot("L-1", order="nope")], [order()])


# ---- Melt allocation ------------------------------------------------------------------------------------------------


def test_store_covers_demand_no_melts():
    problem = build_problem(state_at(store={"6060": 16, "6063": 0, "6082": 0}), fr60_lots(2), [order()]).problem
    assert casts_of(problem) == []
    assert problem.op("L-00001:press").preds == ()
    assert problem.billet_supply == (BilletSupply("6060", 16, 0),)


def test_demand_beyond_the_store_creates_chained_melts_with_cumulative_preds():
    result = build_problem(state_at(store=NO_STOCK), fr60_lots(4), [order()], melt_counter=5)
    problem = result.problem
    # 32 billets need two melts of 24. The alloy is in the id.
    assert casts_of(problem) == ["M-6060-00005", "M-6060-00006"]
    assert result.melt_counter == 7
    first, second = "M-6060-00005:cast", "M-6060-00006:cast"
    assert problem.op(first).preds == ()
    assert problem.op(second).preds == (Pred(first, 0),)
    assert problem.op(first).billets_out == BILLETS_PER_MELT
    # Cumulative demand 8, 16, 24 fits into the first melt, 32 needs the second as well.
    lag = TRANSPORT_MIN
    assert problem.op("L-00001:press").preds == (Pred(first, lag),)
    assert problem.op("L-00003:press").preds == (Pred(first, lag),)
    assert problem.op("L-00004:press").preds == (Pred(first, lag), Pred(second, lag))


def test_lots_are_ranked_by_due_then_id_within_an_alloy():
    lots = [lot("L-00001", order="late"), lot("L-00002", order="soon"), lot("L-00003", order="soon")]
    orders = [order("late", 5000), order("soon", 1000)]
    store = {"6060": 16, "6063": 0, "6082": 0}
    problem = build_problem(state_at(store=store), lots, orders).problem
    # The two soon lots eat the store. The late lot, although first by id, has to wait for the melt.
    assert problem.op("L-00002:press").preds == ()
    assert problem.op("L-00003:press").preds == ()
    assert problem.op("L-00001:press").preds == (Pred("M-6060-00001:cast", TRANSPORT_MIN),)


def test_alloys_never_share_supply():
    lots = [lot("L-00001", "FR-60", 8), lot("L-00002", "KK-80", 8), lot("L-00003", "ST-200", 8)]
    store = {"6060": 0, "6063": 8, "6082": 0}
    problem = build_problem(state_at(store=store), lots, [order()], melt_counter=1).problem
    assert casts_of(problem) == ["M-6060-00001", "M-6082-00002"]
    assert problem.op("L-00002:press").preds == ()
    assert problem.op("L-00001:press").preds == (Pred("M-6060-00001:cast", 8),)
    assert problem.op("L-00003:press").preds == (Pred("M-6082-00002:cast", 8),)


def test_all_supply_tiers_in_order():
    """Store 8, then a melt in transit, then the running cast, then pending, then a new melt."""
    cast_op = "M-6060-00010:cast"
    state = state_at(
        now=100.0,
        store={"6060": 8, "6063": 0, "6082": 0},
        started=(cast_op,),
        melts=(MeltInTransit("S-00009", "M-6060-00009", "6060", 24, 104.5),),
        GUSS=MachineState(
            "GUSS", "RUN", "6060", op_ids=(cast_op,), kind="cast", started_min=80.0, remaining_work_min=50.0
        ),
    )
    # Demand: one lot of 8 from the store, then 3 from transit, 3 from the running cast, 3 pending, 1 new.
    lots = fr60_lots(11)
    result = build_problem(state, lots, [order()], pending_melts=["M-6060-00011"], melt_counter=12)
    problem = result.problem
    assert casts_of(problem) == ["M-6060-00011", "M-6060-00012"]
    assert result.melt_counter == 13
    assert problem.billet_supply == (BilletSupply("6060", 8, 100), BilletSupply("6060", 24, 105))

    running = problem.op(cast_op)
    assert running.frozen == Frozen("GUSS", 80, 150, None)
    assert running.billets_out == BILLETS_PER_MELT
    # The pending melt is chained behind the running cast, the new melt behind the pending one.
    assert problem.op("M-6060-00011:cast").preds == (Pred(cast_op, 0),)
    assert problem.op("M-6060-00012:cast").preds == (Pred("M-6060-00011:cast", 0),)

    press = lambda number: problem.op(f"L-{number:05d}:press")
    assert (press(1).release, press(1).preds) == (100, ())
    assert (press(2).release, press(2).preds) == (105, ())
    assert (press(4).release, press(4).preds) == (105, ())
    # From here on a lot depends on every tier up to the one covering it.
    assert (press(5).release, press(5).preds) == (105, (Pred(cast_op, 8),))
    assert press(7).preds == (Pred(cast_op, 8),)
    assert press(8).preds == (Pred(cast_op, 8), Pred("M-6060-00011:cast", 8))
    assert press(10).preds == (Pred(cast_op, 8), Pred("M-6060-00011:cast", 8))
    assert press(11).preds == (Pred(cast_op, 8), Pred("M-6060-00011:cast", 8), Pred("M-6060-00012:cast", 8))
    assert_valid(problem, decode_edd(problem))


def test_started_lots_are_no_demand_and_carry_no_melt_preds():
    press_id = "L-00001:press"
    state = state_at(
        now=50.0,
        store=NO_STOCK,
        started=(press_id,),
        PR1=MachineState(
            "PR1", "RUN", "6060|M-1001", op_ids=(press_id,), kind="press", lot_ids=("L-00001",), started_min=40.0,
            remaining_work_min=51.0,
        ),
    )
    problem = build_problem(state, fr60_lots(1), [order()]).problem
    assert casts_of(problem) == []
    op = problem.op(press_id)
    assert op.preds == ()
    assert op.frozen == Frozen("PR1", 40, 101, None)
    # Its billets left the store when it started.
    assert op.billets_in == 0


# ---- Melt ids across rebuilds ----------------------------------------------------------------------------------------


def test_melt_ids_stable_across_rebuilds():
    state = state_at(store=NO_STOCK)
    lots = fr60_lots(7)  # 56 billets, three melts
    first = build_problem(state, lots, [order()], melt_counter=3)
    assert casts_of(first.problem) == ["M-6060-00003", "M-6060-00004", "M-6060-00005"]
    second = build_problem(state, lots, [order()], pending_melts=casts_of(first.problem), melt_counter=first.melt_counter)
    assert casts_of(second.problem) == casts_of(first.problem)
    assert second.melt_counter == first.melt_counter
    assert [op.preds for op in first.problem.ops] == [op.preds for op in second.problem.ops]


def test_surplus_is_dropped_from_the_tail_only():
    state = state_at(store=NO_STOCK)
    pending = ["M-6060-00001", "M-6060-00002", "M-6060-00003", "M-6060-00004"]
    # 30 billets need two melts.
    result = build_problem(state, [lot("L-1", n_billets=8), lot("L-2", n_billets=8), lot("L-3", n_billets=8), lot("L-4", n_billets=6)], [order()], pending_melts=pending, melt_counter=5)
    assert casts_of(result.problem) == ["M-6060-00001", "M-6060-00002"]
    assert any("überzählige" in note for note in result.notes)
    # The counter stays: a dropped id is never reused for something else.
    assert result.melt_counter == 5


def test_new_demand_extends_the_chain_with_never_used_numbers():
    state = state_at(store=NO_STOCK)
    pending = ["M-6060-00001"]
    result = build_problem(state, fr60_lots(4), [order()], pending_melts=pending, melt_counter=9)
    # Number 1 is reused because it is that melt. The next one is 9, not 2.
    assert casts_of(result.problem) == ["M-6060-00001", "M-6060-00009"]
    assert result.melt_counter == 10


def test_melts_of_another_alloy_are_not_relabelled_to_cover_demand():
    """A pending 6063 melt may never serve a 6060 lot, whatever its number; that is why the alloy is in the id."""
    state = state_at(store=NO_STOCK)
    result = build_problem(state, fr60_lots(1), [order()], pending_melts=["M-6063-00001"], melt_counter=2)
    assert casts_of(result.problem) == ["M-6060-00002"]
    assert "M-6063-00001:cast" not in {op.id for op in result.problem.ops}


def test_a_stale_counter_never_collides_with_an_existing_id():
    cast_op = "M-6060-00020:cast"
    state = state_at(
        store=NO_STOCK,
        started=(cast_op,),
        GUSS=MachineState("GUSS", "RUN", "6060", op_ids=(cast_op,), kind="cast", started_min=0.0, remaining_work_min=10.0),
    )
    result = build_problem(state, fr60_lots(4), [order()], pending_melts=["M-6060-00025"], melt_counter=1)
    # 32 billets: the running cast and the pending melt are enough, and the counter moved past all of them.
    assert casts_of(result.problem) == ["M-6060-00025"]
    assert result.melt_counter == 26


def test_a_melt_already_started_is_not_planned_again():
    state = state_at(store=NO_STOCK, finished=("M-6060-00001:cast",))
    result = build_problem(state, fr60_lots(1), [order()], pending_melts=["M-6060-00001"], melt_counter=2)
    assert casts_of(result.problem) == ["M-6060-00002"]


# ---- Mid-run: what has started, finished, is travelling ------------------------------------------------------------


def test_finished_ops_are_excluded_and_the_remaining_ops_keep_their_chain():
    the_lot = lot("L-00001")
    state = state_at(now=500.0, finished=(the_lot.op_id("press"), the_lot.op_id("age")))
    problem = build_problem(state, [the_lot], [order()]).problem
    assert {op.id for op in problem.ops} == {the_lot.op_id("saw"), the_lot.op_id("pack")}
    assert problem.op(the_lot.op_id("saw")).preds == ()
    assert problem.op(the_lot.op_id("pack")).preds == (Pred(the_lot.op_id("saw"), TRANSPORT_MIN),)


def test_a_lot_with_nothing_left_contributes_no_ops():
    the_lot = lot("L-00001")
    state = state_at(now=900.0, finished=tuple(the_lot.op_id(k) for k in ("press", "age", "saw", "pack")))
    assert build_problem(state, [the_lot], [order()]).problem.ops == ()


def test_lot_in_transit_gets_its_arrival_as_release():
    the_lot = lot("L-00001")
    state = state_at(
        now=100.0,
        finished=(the_lot.op_id("press"),),
        lots=(LotInTransit(the_lot.id, "age", 106.2),),
    )
    problem = build_problem(state, [the_lot], [order()]).problem
    op = problem.op(the_lot.op_id("age"))
    assert op.release == 107
    assert op.preds == ()
    assert decode_edd(problem).assignments[0].start == 107


def test_a_buffered_lot_is_ready_now():
    the_lot = lot("L-00001")
    state = state_at(now=100.0, finished=(the_lot.op_id("press"),))
    problem = build_problem(state, [the_lot], [order()]).problem
    assert problem.op(the_lot.op_id("age")).release == 0
    assert_valid(problem, decode_edd(problem))


def test_now_rounds_up_so_nothing_is_ever_planned_in_the_past():
    assert build_problem(state_at(now=100.2), [], []).problem.now == 101
    assert build_problem(state_at(now=100.0), [], []).problem.now == 100


def test_running_op_keeps_downstream_ops_behind_it():
    the_lot = lot("L-00001")
    press = the_lot.op_id("press")
    state = state_at(
        now=10.0,
        started=(press,),
        PR1=MachineState(
            "PR1", "RUN", "6060|M-1001", op_ids=(press,), kind="press", lot_ids=(the_lot.id,), started_min=10.0,
            remaining_work_min=61.0,
        ),
    )
    problem = build_problem(state, [the_lot], [order()]).problem
    assert problem.op(the_lot.op_id("age")).preds == (Pred(press, TRANSPORT_MIN),)
    schedule = decode_edd(problem)
    assert_valid(problem, schedule)
    age = {a.op_id: a for a in schedule.assignments}[the_lot.op_id("age")]
    assert age.start == 71 + TRANSPORT_MIN


def test_running_furnace_load_is_frozen_with_its_batch_id_and_cannot_be_joined():
    a, b, c = lot("L-00001"), lot("L-00002"), lot("L-00003")
    ops = (a.op_id("age"), b.op_id("age"))
    state = state_at(
        now=100.0,
        finished=(a.op_id("press"), b.op_id("press"), c.op_id("press")),
        started=ops,
        OF1=MachineState(
            "OF1", "RUN", "T66", op_ids=ops, kind="age", lot_ids=(a.id, b.id), batch_id="B-r2-7", started_min=70.0,
            remaining_work_min=270.0,
        ),
    )
    problem = build_problem(state, [a, b, c], [order()], batch_prefix="B-r3").problem
    frozen = Frozen("OF1", 70, 370, "B-r2-7")
    assert problem.op(ops[0]).frozen == frozen
    assert problem.op(ops[1]).frozen == frozen
    assert problem.batch_prefix == "B-r3"
    schedule = decode_edd(problem)
    assert_valid(problem, schedule)
    items = {x.op_id: x for x in schedule.assignments}
    # OF1 has room for the third lot and is the right recipe, but the load has already started.
    assert items[c.op_id("age")].batch_id != "B-r2-7"
    assert items[c.op_id("age")].batch_id.startswith("B-r3-")


# ---- Frozen.end and downtimes ---------------------------------------------------------------------------------------


def test_frozen_end_is_never_in_the_past():
    press = "L-00001:press"
    state = state_at(
        now=100.0,
        started=(press,),
        PR1=MachineState("PR1", "RUN", "6060|M-1001", op_ids=(press,), kind="press", started_min=30.0, remaining_work_min=0.0),
    )
    op = build_problem(state, [lot("L-00001")], [order()]).problem.op(press)
    assert op.frozen == Frozen("PR1", 30, 101, None)


def test_frozen_end_is_pushed_through_the_repair_of_its_own_fault():
    press = "L-00001:press"
    state = state_at(
        now=100.0,
        started=(press,),
        PR1=MachineState(
            "PR1", "FAULT", "6060|M-1001", op_ids=(press,), kind="press", started_min=30.0, remaining_work_min=20.0,
            fault_code="P-204", fault_estimate_min=240.0, fault_elapsed_min=40.0, fault_remaining_min=200.0,
        ),
    )
    problem = build_problem(state, [lot("L-00001")], [order()]).problem
    assert problem.machine("PR1").downtimes == (Downtime(100, 300, "P-204"),)
    # 200 minutes of repair and then the remaining 20 minutes of work.
    assert problem.op(press).frozen == Frozen("PR1", 30, 320, None)


def test_frozen_end_is_pushed_through_a_later_downtime_only_if_it_reaches_it():
    press = "L-00001:press"
    running = MachineState(
        "PR1", "RUN", "6060|M-1001", op_ids=(press,), kind="press", started_min=90.0, remaining_work_min=50.0
    )
    state = state_at(now=100.0, started=(press,), PR1=running)
    reaches = build_problem(state, [lot("L-00001")], [order()], downtimes={"PR1": [Downtime(120, 180)]}).problem
    assert reaches.op(press).frozen.end == 210
    misses = build_problem(state, [lot("L-00001")], [order()], downtimes={"PR1": [Downtime(160, 180)]}).problem
    assert misses.op(press).frozen.end == 150


def test_every_faulted_machine_gets_a_downtime():
    state = state_at(
        now=100.0,
        PR2=MachineState("PR2", "FAULT", "6082|M-3001", fault_code="P-201", fault_remaining_min=90.5),
        OF2=MachineState("OF2", "FAULT", "T6", fault_code="O-301", fault_remaining_min=15.0),
        VP=MachineState("VP", "FAULT", "B-16", fault_code="V-501", fault_remaining_min=33.0),
    )
    problem = build_problem(state, [], []).problem
    assert problem.machine("PR2").downtimes == (Downtime(100, 191, "P-201"),)
    assert problem.machine("OF2").downtimes == (Downtime(100, 115, "O-301"),)
    assert problem.machine("VP").downtimes == (Downtime(100, 133, "V-501"),)
    assert problem.machine("PR1").downtimes == ()


def test_extra_downtimes_are_added_merged_and_clipped():
    state = state_at(
        now=100.0, PR2=MachineState("PR2", "FAULT", "6082|M-3001", fault_code="P-201", fault_remaining_min=50.0)
    )
    extra = {
        "PR2": [Downtime(140, 200, "P-201"), Downtime(500, 600, "Wartung"), Downtime(0, 50, "vorbei")],
        "OF1": [Downtime(50, 150, "Wartung")],
    }
    problem = build_problem(state, [], [], downtimes=extra).problem
    assert problem.machine("PR2").downtimes == (Downtime(100, 200, "P-201"), Downtime(500, 600, "Wartung"))
    assert problem.machine("OF1").downtimes == (Downtime(100, 150, "Wartung"),)


def test_setup_class_comes_from_the_state():
    state = state_at(
        now=10.0,
        OF2=MachineState("OF2", "IDLE", "T5"),
        SAE=MachineState("SAE", "IDLE", None),
    )
    problem = build_problem(state, [], []).problem
    assert problem.machine("OF2").initial_setup_class == "T5"
    assert problem.machine("SAE").initial_setup_class is None


def test_op_of_a_lot_no_longer_open_still_occupies_the_machine():
    press = "L-00099:press"
    state = state_at(
        now=100.0,
        started=(press,),
        PR1=MachineState("PR1", "RUN", "6060|M-1001", op_ids=(press,), kind="press", started_min=90.0, remaining_work_min=30.0),
    )
    result = build_problem(state, [], [])
    assert result.problem.machine("PR1").available_from == 130
    assert any("L-00099" in note for note in result.notes)


def test_started_op_on_no_machine_is_skipped_with_a_note():
    state = state_at(now=100.0, started=("L-00001:press",))
    result = build_problem(state, [lot("L-00001")], [order()])
    assert "L-00001:press" not in {op.id for op in result.problem.ops}
    assert result.problem.op("L-00001:age").preds == ()
    assert any("L-00001:press" in note for note in result.notes)


def test_completed_lots_end_is_passed_through():
    orders = [order("A-1", due=500, completed=800)]
    problem = build_problem(state_at(now=900.0), [], orders).problem
    assert problem.orders[0].completed_lots_end == 800


# ---- Slowdowns ------------------------------------------------------------------------------------------------------


def _slow(machine="PR1", factor=2.0, at=0, minutes=200):
    return {"type": "slowdown", "machine": machine, "factor": factor, "at_offset_min": at, "duration_min": minutes}


def test_slowdown_scales_an_op_that_could_fall_into_the_window():
    the_lot = lot("L-00001")
    problem = build_problem(state_at(), [the_lot], [order()], slowdowns=[_slow()]).problem
    # 61 on PR1 doubled. PR2 is untouched.
    assert problem.op(the_lot.op_id("press")).durations == {"PR1": 122, "PR2": 70}


def test_a_factor_below_one_is_no_slowdown():
    """The factor is a duration multiplier, as in SimEngine.schedule_slowdown. Planning does not count on a speed-up."""
    the_lot = lot("L-00001")
    problem = build_problem(state_at(), [the_lot], [order()], slowdowns=[_slow(factor=0.5)]).problem
    assert problem.op(the_lot.op_id("press")).durations["PR1"] == 61
    press = the_lot.op_id("press")
    state = state_at(
        now=100.0,
        started=(press,),
        PR1=MachineState("PR1", "RUN", "6060|M-1001", op_ids=(press,), kind="press", started_min=90.0, remaining_work_min=50.0),
    )
    running = build_problem(state, [the_lot], [order()], slowdowns=[_slow(factor=0.5)]).problem
    assert running.op(press).frozen.end == 150


def test_slowdown_leaves_ops_that_cannot_fall_into_the_window():
    the_lot = lot("L-00001")
    press = the_lot.op_id("press")
    before = build_problem(state_at(), [the_lot], [order()], slowdowns=[_slow(machine="SAE", at=0, minutes=100)]).problem
    # The saw can start at 61 + 8 + 300 + 8 at the earliest, long after a window ending at 100.
    assert before.op(the_lot.op_id("saw")).durations == {"SAE": 17}
    assert before.op(press).durations == {"PR1": 61, "PR2": 70}
    # A window far behind everything on that machine cannot be reached either.
    far = build_problem(state_at(), [the_lot], [order()], slowdowns=[_slow(at=5000)]).problem
    assert far.op(press).durations["PR1"] == 61


def test_slowdown_scales_a_furnace_family_together_so_it_can_still_share_a_load():
    lots = [lot("L-00001"), lot("L-00002")]
    problem = build_problem(state_at(), lots, [order()], slowdowns=[_slow(machine="OF1", factor=1.5, at=0, minutes=400)]).problem
    assert problem.op("L-00001:age").durations == problem.op("L-00002:age").durations == {"OF1": 450, "OF2": 300}
    assert_valid(problem, decode_edd(problem))


def test_slowdown_extends_a_running_op():
    press = "L-00001:press"
    state = state_at(
        now=100.0,
        started=(press,),
        PR1=MachineState("PR1", "RUN", "6060|M-1001", op_ids=(press,), kind="press", started_min=90.0, remaining_work_min=50.0),
    )
    problem = build_problem(state, [lot("L-00001")], [order()], slowdowns=[_slow(at=0, minutes=20)]).problem
    # 20 of the 50 remaining minutes run at half speed: 30 + 2 * 20 = 70.
    assert problem.op(press).frozen.end == 170


def test_slowdown_without_any_effect_leaves_the_problem_as_it_was():
    the_lot = lot("L-00001")
    plain = build_problem(state_at(), [the_lot], [order()]).problem
    slowed = build_problem(state_at(), [the_lot], [order()], slowdowns=[_slow(factor=1.0)]).problem
    assert plain.ops == slowed.ops
