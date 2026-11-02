"""retime and keep_schedule: same sequences and loads, left-justified; old plans carried over to a changed problem."""
from __future__ import annotations

from dataclasses import replace

from test_scheduler_support import NO_RUSH, assert_valid, by_op, lot, order, realistic_lots, state_at

from werkhalle.domain.catalog import AGE, CAST, SAW
from werkhalle.domain.state import MachineState
from werkhalle.scheduling import (
    Assignment,
    Downtime,
    Frozen,
    MachineInfo,
    Op,
    OrderInfo,
    Pred,
    Problem,
    Schedule,
    build_problem,
    compute_metrics,
    decode_edd,
    keep_schedule,
    retime,
    solve_cpsat,
)


def saw(op_id, cls="L6000", minutes=30, **kw):
    return Op(f"{op_id}:saw", SAW, op_id, "A", {"SAE": minutes}, cls, **kw)


def age(lot_id, recipe="T66", minutes=300, **kw):
    return Op(f"{lot_id}:age", AGE, lot_id, "A", {"OF1": minutes}, recipe, size=2, batch_family=recipe, **kw)


SAE = MachineInfo("SAE", SAW, initial_setup_class="L6000")
OF1 = MachineInfo("OF1", AGE, 6, True, initial_setup_class="T66")
ORDERS = (OrderInfo("A", NO_RUSH),)


def schedule_of(*assignments) -> Schedule:
    return Schedule("edd", "feasible", tuple(assignments))


def test_slack_is_removed_and_the_sequence_kept():
    problem = Problem(0, (saw("a"), saw("b")), (SAE,), ORDERS)
    retimed = retime(problem, schedule_of(Assignment("b:saw", "SAE", 100, 130), Assignment("a:saw", "SAE", 200, 230)))
    items = by_op(retimed)
    # b stays first, even though a sorts earlier by id.
    assert (items["b:saw"].start, items["a:saw"].start) == (0, 30)
    assert_valid(problem, retimed)


def test_setups_are_recomputed_from_the_sequence():
    problem = Problem(0, (saw("a", "L3000"), saw("b", "L6000")), (SAE,), ORDERS)
    retimed = retime(problem, schedule_of(Assignment("a:saw", "SAE", 0, 30), Assignment("b:saw", "SAE", 40, 70)))
    items = by_op(retimed)
    assert (items["a:saw"].start, items["a:saw"].setup_before) == (15, 15)
    assert (items["b:saw"].start, items["b:saw"].setup_before) == (60, 15)
    assert_valid(problem, retimed)


def test_precedence_with_lag_is_kept():
    problem = Problem(
        0,
        (saw("a"), Op("a:pack", "pack", "a", "A", {"VP": 10}, "B-16", preds=(Pred("a:saw", 8),))),
        (SAE, MachineInfo("VP", "pack", initial_setup_class="B-16")),
        ORDERS,
    )
    retimed = retime(problem, schedule_of(Assignment("a:saw", "SAE", 5, 35), Assignment("a:pack", "VP", 100, 110)))
    assert by_op(retimed)["a:pack"].start == 38


def test_release_and_now_are_kept():
    problem = Problem(50, (saw("a", release=70), saw("b")), (SAE,), ORDERS)
    retimed = retime(problem, schedule_of(Assignment("b:saw", "SAE", 500, 530), Assignment("a:saw", "SAE", 900, 930)))
    items = by_op(retimed)
    assert items["b:saw"].start == 50
    assert items["a:saw"].start == 80


def test_furnace_load_moves_as_one_and_only_first_member_carries_the_setup():
    problem = Problem(0, (age("x", "T5", 180), age("y", "T5", 180, release=100)), (OF1,), ORDERS)
    retimed = retime(
        problem,
        schedule_of(Assignment("y:age", "OF1", 500, 680, 0, "B-1"), Assignment("x:age", "OF1", 500, 680, 30, "B-1")),
    )
    items = by_op(retimed)
    # y arrives at 100, and only then does the 30 minute setup begin, for the whole load.
    assert (items["x:age"].start, items["y:age"].start) == (130, 130)
    assert (items["x:age"].end, items["y:age"].end) == (310, 310)
    assert (items["x:age"].setup_before, items["y:age"].setup_before) == (30, 0)
    assert_valid(problem, retimed)


def test_a_setup_does_not_run_ahead_of_its_material():
    """The saw is free and 40 minutes of idle would fit the setup. The plant still starts it when the lot is there."""
    machine = MachineInfo("SAE", SAW, initial_setup_class="L3000")
    problem = Problem(0, (saw("a", "L6000", release=40),), (machine,), ORDERS)
    a = by_op(retime(problem, schedule_of(Assignment("a:saw", "SAE", 40, 70, 15))))["a:saw"]
    assert (a.start, a.end, a.setup_before) == (55, 85, 15)


def test_downtime_pushes_the_setup_behind_it_not_just_the_op():
    machine = replace(OF1, downtimes=(Downtime(0, 75),))
    problem = Problem(0, (age("x", "T5", 180),), (machine,), ORDERS)
    retimed = retime(problem, schedule_of(Assignment("x:age", "OF1", 200, 380, 30, "B-1")))
    assert by_op(retimed)["x:age"].start == 105
    assert_valid(problem, retimed)


def test_op_longer_than_the_gap_between_two_downtimes_skips_both():
    machine = replace(SAE, downtimes=(Downtime(10, 20), Downtime(40, 50)))
    problem = Problem(0, (saw("a", minutes=25),), (machine,), ORDERS)
    assert by_op(retime(problem, schedule_of(Assignment("a:saw", "SAE", 0, 25))))["a:saw"].start == 50


def test_frozen_ops_are_copied_and_their_end_occupies_the_machine():
    frozen = Frozen("SAE", 10, 120)
    problem = Problem(100, (saw("f", frozen=frozen), saw("a")), (SAE,), ORDERS)
    retimed = retime(problem, schedule_of(Assignment("a:saw", "SAE", 300, 330)))
    items = by_op(retimed)
    assert (items["f:saw"].start, items["f:saw"].end) == (10, 120)
    assert items["a:saw"].start == 120


def test_cycle_is_reported_not_raised():
    first = Op("M-1:cast", CAST, None, None, {"GUSS": 93}, "6060")
    second = Op("M-2:cast", CAST, None, None, {"GUSS": 93}, "6060", preds=(Pred("M-1:cast"),))
    problem = Problem(0, (first, second), (MachineInfo("GUSS", CAST, initial_setup_class="6060"),), ())
    # The machine sequence says second, then first; the chain says the opposite.
    retimed = retime(problem, schedule_of(Assignment("M-2:cast", "GUSS", 0, 93), Assignment("M-1:cast", "GUSS", 93, 186)))
    assert retimed.status == "no_solution"
    assert retimed.note


def test_unknown_and_ineligible_assignments_are_left_for_the_validator_to_report():
    problem = Problem(0, (saw("a"),), (SAE,), ORDERS)
    ghost = Assignment("ghost:saw", "SAE", 5, 6)
    wrong = Assignment("a:saw", "VP", 5, 35)
    assert retime(problem, schedule_of(ghost, wrong)).assignments == (ghost, wrong)


def test_retime_is_idempotent_and_never_later_on_a_real_plan():
    lots, orders = realistic_lots(target=10, seed=2)
    state = state_at(now=30.0, PR2=MachineState("PR2", "FAULT", "6082|M-3001", fault_code="P-201", fault_remaining_min=120.0))
    problem = build_problem(state, lots, orders).problem
    edd = decode_edd(problem)
    # Stretch every pending op well to the right, in sequence order, so there is slack to remove.
    stretched = replace(
        edd,
        assignments=tuple(
            a if problem.op(a.op_id).frozen else replace(a, start=a.start * 3, end=a.start * 3 + (a.end - a.start))
            for a in edd.assignments
        ),
    )
    once = retime(problem, stretched)
    assert_valid(problem, once)
    assert by_op(once) == by_op(edd)
    assert retime(problem, once).assignments == once.assignments


def test_solver_results_are_already_retimed():
    lots, orders = realistic_lots(target=8, seed=4)
    problem = build_problem(state_at(), lots, orders).problem
    schedule = solve_cpsat(problem, time_limit_s=1.0, workers=4)
    assert retime(problem, schedule).assignments == schedule.assignments


# ---- keep_schedule ----------------------------------------------------------------------------------------------------


def _plan(lots, state=None, **kw):
    problem = build_problem(state or state_at(), lots, kw.pop("orders", [order()]), **kw).problem
    return problem, decode_edd(problem)


def test_keep_on_the_same_problem_keeps_the_plan():
    lots = [lot(f"L-{i}", "FR-60", 8) for i in range(1, 6)]
    problem, plan = _plan(lots)
    kept = keep_schedule(problem, plan)
    assert kept.solver == "keep"
    assert_valid(problem, kept)
    assert by_op(kept) == by_op(plan)


def test_keep_drops_ops_that_are_gone():
    lots = [lot(f"L-{i}", "FR-60", 8) for i in range(1, 4)]
    _, plan = _plan(lots)
    problem = build_problem(state_at(), lots[:2], [order()]).problem
    kept = keep_schedule(problem, plan)
    assert_valid(problem, kept)
    assert "L-3:press" not in by_op(kept)
    assert "4 entfallen" in kept.note


def test_keep_appends_new_ops_at_the_tail_of_the_first_eligible_machine_in_stage_order():
    old = [lot("L-1", "FR-60", 8)]
    _, plan = _plan(old)
    new = lot("L-2", "ST-200", 8)
    store = {"6060": 24, "6063": 24, "6082": 24}
    problem = build_problem(state_at(store=store), [*old, new], [order()]).problem
    kept = keep_schedule(problem, plan)
    assert_valid(problem, kept)
    items = by_op(kept)
    assert by_op(kept)["L-1:press"] == by_op(plan)["L-1:press"]
    assert items["L-2:press"].machine == "PR2"  # the first and only eligible press
    assert items["L-2:age"].machine == "OF1"  # the first furnace
    assert items["L-2:age"].batch_id != items["L-1:age"].batch_id
    assert items["L-2:age"].start >= items["L-1:age"].end
    assert items["L-2:saw"].start > items["L-1:saw"].start
    assert items["L-2:pack"].start > items["L-1:pack"].start
    assert "4 Vorgänge angehängt" in kept.note


def test_keep_appends_new_melts_at_the_tail_of_their_chain():
    no_stock = {"6060": 0, "6063": 0, "6082": 0}
    old = [lot(f"L-{i}", "FR-60", 8) for i in range(1, 4)]
    first = build_problem(state_at(store=no_stock), old, [order()], melt_counter=1)
    plan = decode_edd(first.problem)
    new = [*old, lot("L-4", "FR-60", 8)]
    pending = [op.id[: -len(":cast")] for op in first.problem.ops if op.kind == "cast"]
    problem = build_problem(state_at(store=no_stock), new, [order()], pending_melts=pending, melt_counter=first.melt_counter).problem
    kept = keep_schedule(problem, plan)
    assert_valid(problem, kept)
    items = by_op(kept)
    assert items["M-6060-00002:cast"].start >= items["M-6060-00001:cast"].end
    assert items["L-4:press"].start >= items["M-6060-00002:cast"].end + 8


def test_keep_reorders_an_old_sequence_that_disagrees_with_the_chain():
    first = Op("M-6060-00001:cast", CAST, None, None, {"GUSS": 93}, "6060", alloy="6060", billets_out=24)
    second = Op("M-6060-00002:cast", CAST, None, None, {"GUSS": 93}, "6060", preds=(Pred(first.id),), alloy="6060", billets_out=24)
    problem = Problem(0, (first, second), (MachineInfo("GUSS", CAST, initial_setup_class="6060"),), ())
    previous = schedule_of(Assignment(second.id, "GUSS", 0, 93), Assignment(first.id, "GUSS", 93, 186))
    kept = keep_schedule(problem, previous)
    assert_valid(problem, kept)
    assert by_op(kept)[first.id].start == 0


def test_keep_takes_frozen_ops_from_the_problem_not_the_old_plan():
    the_lot = lot("L-1", "FR-60", 8)
    _, plan = _plan([the_lot])
    press = the_lot.op_id("press")
    state = state_at(
        now=10.0,
        started=(press,),
        PR2=MachineState("PR2", "RUN", "6060|M-1001", op_ids=(press,), kind="press", lot_ids=(the_lot.id,), started_min=5.0, remaining_work_min=40.0),
    )
    problem = build_problem(state, [the_lot], [order()]).problem
    kept = keep_schedule(problem, plan)
    assert_valid(problem, kept)
    a = by_op(kept)[press]
    assert (a.machine, a.start, a.end) == ("PR2", 5, 50)
    assert by_op(kept)[the_lot.op_id("age")].start == 58


def test_keep_retires_a_load_id_that_is_running_for_its_pending_member():
    a, b = lot("L-1"), lot("L-2")
    plan = schedule_of(
        Assignment(a.op_id("age"), "OF1", 200, 500, 0, "B-r8-1"),
        Assignment(b.op_id("age"), "OF1", 200, 500, 0, "B-r8-1"),
    )
    state = state_at(
        now=100.0,
        finished=(a.op_id("press"), b.op_id("press")),
        started=(a.op_id("age"),),
        OF1=MachineState(
            "OF1", "RUN", "T66", op_ids=(a.op_id("age"),), kind="age", lot_ids=(a.id,),
            batch_id="B-r8-1", started_min=90.0, remaining_work_min=290.0,
        ),
    )
    problem = build_problem(state, [a, b], [order()], batch_prefix="B-r9").problem
    kept = keep_schedule(problem, plan)
    assert_valid(problem, kept)
    items = by_op(kept)
    assert items[a.op_id("age")].batch_id == "B-r8-1"
    assert items[b.op_id("age")].batch_id == "B-r9-1"
    assert items[b.op_id("age")].start >= items[a.op_id("age")].end


def test_keep_moves_an_op_whose_machine_is_gone_to_the_tail():
    the_lot = lot("L-1", "FR-60", 8)
    problem, plan = _plan([the_lot])
    corrupted = replace(
        plan,
        assignments=tuple(
            replace(a, machine="OF9") if a.op_id == the_lot.op_id("age") else a for a in plan.assignments
        ),
    )
    kept = keep_schedule(problem, corrupted)
    assert_valid(problem, kept)
    assert by_op(kept)[the_lot.op_id("age")].machine == "OF1"


def test_keep_splits_a_load_that_no_longer_fits():
    the_ops = tuple(Op(f"L{i}:age", AGE, f"L{i}", "A", {"OF1": 300}, "T66", size=4, batch_family="T66") for i in range(2))
    problem = Problem(0, the_ops, (OF1,), ORDERS)
    previous = schedule_of(*(Assignment(op.id, "OF1", 0, 300, 0, "B-1") for op in the_ops))
    kept = keep_schedule(problem, previous)
    assert_valid(problem, kept)
    assert len({a.batch_id for a in kept.assignments}) == 2


def test_keep_survives_a_real_fault_rebuild_with_new_lots_and_lost_lots():
    lots, orders = realistic_lots(target=14, seed=11)
    first = build_problem(state_at(), lots[:10], orders)
    plan = solve_cpsat(first.problem, time_limit_s=1.0, workers=4)
    pending = [op.id[: -len(":cast")] for op in first.problem.ops if op.kind == "cast"]
    state = state_at(
        now=0.0,
        PR1=MachineState("PR1", "FAULT", "6060|M-1001", fault_code="P-204", fault_remaining_min=240.0),
        OF2=MachineState("OF2", "FAULT", "T6", fault_code="O-301", fault_remaining_min=75.0),
    )
    problem = build_problem(state, lots[2:], orders, pending_melts=pending, melt_counter=first.melt_counter).problem
    kept = keep_schedule(problem, plan)
    assert_valid(problem, kept)
    assert compute_metrics(problem, kept).makespan > 0
