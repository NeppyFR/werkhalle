"""CP-SAT on small instances with hand-derived optima. Every expected number below was worked out by hand first."""
from __future__ import annotations

from dataclasses import replace

import pytest
from test_scheduler_support import NO_RUSH, assert_valid, by_op, lot, order, realistic_lots, state_at

from werkhalle.domain.catalog import AGE, PACK, SAW
from werkhalle.domain.state import MachineState
from werkhalle.scheduling import (
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
    solve_anneal,
    solve_cpsat,
    validate_schedule,
)
from werkhalle.scheduling.cpsat import _add_complete_hint, _build_model, _extract
from werkhalle.scheduling.decoder import decode_best_rule

LIMIT = 10  # generous for instances this small: they all finish far earlier, and each test demands a proof of optimality
WORKERS = 4


def solve(problem: Problem) -> Schedule:
    schedule = solve_cpsat(problem, time_limit_s=LIMIT, workers=WORKERS)
    assert schedule.status == "optimal"
    assert_valid(problem, schedule)
    # What CP-SAT optimised and what the rest of the system measures are the same number.
    assert schedule.solver_objective == compute_metrics(problem, schedule).objective
    assert schedule.best_bound == schedule.solver_objective
    return schedule


def furnace(machine_id="OF1", capacity=6, initial="T66", downtimes=()) -> MachineInfo:
    return MachineInfo(machine_id, AGE, capacity, True, initial_setup_class=initial, downtimes=downtimes)


def age_op(lot_id: str, recipe: str = "T66", minutes: int | None = None, order_id="A", size=2, machines=("OF1",), **kw) -> Op:
    minutes = minutes if minutes is not None else {"T5": 180, "T66": 300, "T6": 360}[recipe]
    return Op(
        f"{lot_id}:age", AGE, lot_id, order_id, {m: minutes for m in machines}, recipe, size=size, batch_family=recipe, **kw
    )


# ---- Exact cases the plan was analysed on ---------------------------------------------------------------------------


def test_setup_must_lie_after_the_downtime():
    """OF1 idle with T66 mounted, down for [0,75), one T5 lot ready at 0: the 30 minute setup comes after the downtime."""
    problem = Problem(0, (age_op("L1", "T5"),), (furnace(downtimes=(Downtime(0, 75),)),), (OrderInfo("A", NO_RUSH),))
    schedule = solve(problem)
    a = by_op(schedule)["L1:age"]
    assert (a.start, a.end, a.setup_before, a.machine) == (105, 285, 30, "OF1")
    metrics = compute_metrics(problem, schedule)
    assert metrics.objective == 285 + 2 * 30


def test_no_setup_no_wait_beyond_the_downtime():
    problem = Problem(0, (age_op("L1", "T66"),), (furnace(downtimes=(Downtime(0, 75),)),), (OrderInfo("A", NO_RUSH),))
    a = by_op(solve(problem))["L1:age"]
    assert (a.start, a.setup_before) == (75, 0)


def test_two_t5_and_four_t66_lots_on_of1():
    """T66 is mounted, so its two loads go first: [0,300) of 3 lots and [300,600) of 1. Then 30 setup, T5 [630,810)."""
    ops = (*(age_op(f"T{i}", "T5") for i in range(2)), *(age_op(f"S{i}", "T66") for i in range(4)))
    problem = Problem(0, ops, (furnace(),), (OrderInfo("A", NO_RUSH),))
    schedule = solve(problem)
    metrics = compute_metrics(problem, schedule)
    assert metrics.makespan == 810
    assert metrics.setups == 1
    assert metrics.setup_minutes == 30
    # The other two orderings end at 840 with two setups, 960. 810 plus twice 30 is the optimum.
    assert metrics.objective == 870
    assert schedule.solver_objective == 870
    items = by_op(schedule)
    t5_starts = {items[f"T{i}:age"].start for i in range(2)}
    assert t5_starts == {630}
    assert {items[f"T{i}:age"].batch_id for i in range(2)} == {items["T0:age"].batch_id}


def test_one_full_lot_end_to_end():
    """FR-60, 8 billets, nothing mounted wrong: [0,61) on PR1, [69,369) on OF1, [377,394) on SAE, [402,428) on VP."""
    problem = build_problem(state_at(), [lot("L-1", "FR-60", 8)], [order()]).problem
    schedule = solve(problem)
    items = by_op(schedule)
    assert (items["L-1:press"].machine, items["L-1:press"].start, items["L-1:press"].end) == ("PR1", 0, 61)
    # OF2 would cost a 30 minute setup without saving anything, so OF1 it is.
    assert (items["L-1:age"].machine, items["L-1:age"].start, items["L-1:age"].end) == ("OF1", 69, 369)
    assert (items["L-1:saw"].start, items["L-1:saw"].end) == (377, 394)
    assert (items["L-1:pack"].start, items["L-1:pack"].end) == (402, 428)
    metrics = compute_metrics(problem, schedule)
    assert metrics.makespan == 428
    assert metrics.setups == 0
    assert metrics.objective == 428
    assert metrics.order_completion == {"A-1": 428}


# ---- Eligibility, capacity, batching ---------------------------------------------------------------------------------


def test_st200_runs_on_pr2_only_even_if_that_means_waiting():
    state = state_at(store={"6060": 0, "6063": 0, "6082": 8})
    problem = build_problem(state, [lot("L-1", "ST-200", 8)], [order()], downtimes={"PR2": [Downtime(0, 1000)]}).problem
    schedule = solve(problem)
    press = by_op(schedule)["L-1:press"]
    # PR1 is idle the whole time, and not eligible. PR2 has the right die mounted, so no setup.
    assert (press.machine, press.start, press.end) == ("PR2", 1000, 1096)


def test_batching_a_full_load_starts_together():
    ops = tuple(age_op(f"L{i}") for i in range(3))
    problem = Problem(0, ops, (furnace(),), (OrderInfo("A", NO_RUSH),))
    schedule = solve(problem)
    assert {a.start for a in schedule.assignments} == {0}
    assert {a.end for a in schedule.assignments} == {300}
    assert len({a.batch_id for a in schedule.assignments}) == 1
    assert compute_metrics(problem, schedule).objective == 300


def test_furnace_capacity_forces_a_second_load():
    """Four lots of 2 baskets into a furnace taking 6: one load of 3 and one of 1, however the optimum splits them."""
    ops = tuple(age_op(f"L{i}") for i in range(4))
    problem = Problem(0, ops, (furnace(),), (OrderInfo("A", NO_RUSH),))
    schedule = solve(problem)
    baskets: dict[str, int] = {}
    for a in schedule.assignments:
        baskets[a.batch_id] = baskets.get(a.batch_id, 0) + problem.op(a.op_id).size
    assert len(baskets) == 2
    assert max(baskets.values()) <= 6
    assert compute_metrics(problem, schedule).makespan == 600


def test_oversized_op_cannot_use_the_small_furnace():
    ops = (age_op("L1", size=8, machines=("OF1", "OF2")),)
    machines = (furnace("OF1", capacity=6, downtimes=()), furnace("OF2", capacity=10, initial="T5"))
    problem = Problem(0, ops, machines, (OrderInfo("A", NO_RUSH),))
    schedule = solve(problem)
    # OF2 needs a setup, OF1 would not, but only OF2 can hold 8 baskets.
    a = by_op(schedule)["L1:age"]
    assert (a.machine, a.start, a.setup_before) == ("OF2", 30, 30)


def test_recipes_never_share_a_load_though_they_would_fit_and_want_to():
    ops = (age_op("L1", "T66", minutes=100), age_op("L2", "T5", minutes=100))
    problem = Problem(0, ops, (furnace(),), (OrderInfo("A", NO_RUSH),))
    schedule = solve(problem)
    items = by_op(schedule)
    assert items["L1:age"].batch_id != items["L2:age"].batch_id
    # T66 first with no setup [0,100), then 30 setup, T5 [130,230).
    assert (items["L1:age"].start, items["L2:age"].start) == (0, 130)
    assert compute_metrics(problem, schedule).objective == 230 + 2 * 30


def test_two_furnaces_are_used_when_that_is_better():
    ops = tuple(age_op(f"L{i}", machines=("OF1", "OF2")) for i in range(4))
    machines = (furnace("OF1"), furnace("OF2", capacity=10, initial="T66"))
    problem = Problem(0, ops, machines, (OrderInfo("A", NO_RUSH),))
    schedule = solve(problem)
    # All four fit into OF2 at once: 300 minutes, no setup at all.
    assert compute_metrics(problem, schedule).objective == 300


# ---- Sequence-dependent setups ---------------------------------------------------------------------------------------


def pack_op(op_id: str, cls: str, minutes=10, due_order="A", **kw) -> Op:
    return Op(f"{op_id}:pack", PACK, op_id, due_order, {"VP": minutes}, cls, **kw)


def saw_op(op_id: str, cls: str = "L6000", minutes=30, due_order="A", **kw) -> Op:
    return Op(f"{op_id}:saw", SAW, op_id, due_order, {"SAE": minutes}, cls, **kw)


def test_sequence_groups_classes_starting_with_the_initial_one():
    """B-16 is mounted. y (B-16) [0,10), 10 setup, then the two B-10 ops: 40, one setup, objective 40 + 2 * 10."""
    ops = (pack_op("x", "B-10"), pack_op("y", "B-16"), pack_op("z", "B-10"))
    machine = MachineInfo("VP", PACK, initial_setup_class="B-16")
    problem = Problem(0, ops, (machine,), (OrderInfo("A", NO_RUSH),))
    schedule = solve(problem)
    items = by_op(schedule)
    assert (items["y:pack"].start, items["y:pack"].setup_before) == (0, 0)
    assert sorted((items["x:pack"].start, items["z:pack"].start)) == [20, 30]
    assert compute_metrics(problem, schedule).objective == 60


def test_initial_class_decides_the_first_setup():
    """Same ops, but B-10 is mounted now, so the two B-10 ops go first and the setup moves behind them."""
    ops = (pack_op("x", "B-10"), pack_op("y", "B-16"), pack_op("z", "B-10"))
    machine = MachineInfo("VP", PACK, initial_setup_class="B-10")
    problem = Problem(0, ops, (machine,), (OrderInfo("A", NO_RUSH),))
    schedule = solve(problem)
    items = by_op(schedule)
    assert sorted((items["x:pack"].start, items["z:pack"].start)) == [0, 10]
    assert (items["y:pack"].start, items["y:pack"].setup_before) == (30, 10)
    assert compute_metrics(problem, schedule).objective == 60


def test_a_setup_waits_for_its_material():
    """The lot arrives at 40. An idle saw could set up ahead of that, but the plant only starts it once the lot is there."""
    ops = (saw_op("p", "L6000", release=40),)
    machine = MachineInfo("SAE", SAW, initial_setup_class="L3000")
    problem = Problem(0, ops, (machine,), (OrderInfo("A", NO_RUSH),))
    a = by_op(solve(problem))["p:saw"]
    assert (a.start, a.end, a.setup_before) == (55, 85, 15)


def test_a_load_setup_waits_for_every_member():
    """Two T5 lots, one there at 0, the other at 100. The load's 30 minute setup begins at 100, though it is carried by the other."""
    ops = (age_op("a", "T5"), age_op("b", "T5", release=100))
    problem = Problem(0, ops, (furnace(),), (OrderInfo("A", NO_RUSH),))
    schedule = solve(problem)
    items = by_op(schedule)
    assert items["a:age"].start == items["b:age"].start == 130
    assert items["a:age"].batch_id == items["b:age"].batch_id
    assert compute_metrics(problem, schedule).objective == 310 + 2 * 30


def test_setup_after_a_predecessor_counts_the_transport_too():
    """Saw ends at 30, lag 8, so the pack's 10 minute setup runs from 38 to 48."""
    saw = saw_op("p")
    pack = pack_op("p", "B-10", preds=(Pred("p:saw", 8),))
    machines = (MachineInfo("SAE", SAW, initial_setup_class="L6000"), MachineInfo("VP", PACK, initial_setup_class="B-16"))
    problem = Problem(0, (saw, pack), machines, (OrderInfo("A", NO_RUSH),))
    a = by_op(solve(problem))["p:pack"]
    assert (a.start, a.setup_before) == (48, 10)


def test_no_initial_class_means_no_first_setup():
    ops = (pack_op("x", "B-10"),)
    problem = Problem(0, ops, (MachineInfo("VP", PACK, initial_setup_class=None),), (OrderInfo("A", NO_RUSH),))
    a = by_op(solve(problem))["x:pack"]
    assert (a.start, a.setup_before) == (0, 0)


# ---- Due dates -------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("urgent", ["p", "q"])
def test_tighter_order_goes_first_when_it_matters(urgent):
    """Two 30 minute saw jobs, one due at 35. Either id can be the urgent one, so no tie-break can pass by luck."""
    relaxed = "q" if urgent == "p" else "p"
    ops = (saw_op("p", due_order="P"), saw_op("q", due_order="Q"))
    machine = MachineInfo("SAE", SAW, initial_setup_class="L6000")
    orders = (OrderInfo(urgent.upper(), 35), OrderInfo(relaxed.upper(), 500))
    problem = Problem(0, ops, (machine,), orders)
    schedule = solve(problem)
    items = by_op(schedule)
    assert items[f"{urgent}:saw"].start == 0
    assert items[f"{relaxed}:saw"].start == 30
    metrics = compute_metrics(problem, schedule)
    assert metrics.total_tardiness == 0
    assert metrics.objective == 60


def test_weight_decides_who_is_late_when_someone_must_be():
    """Both due at 30, one op must end at 60. The order of weight 3 must not be the late one."""
    ops = (saw_op("p", due_order="P"), saw_op("q", due_order="Q"))
    machine = MachineInfo("SAE", SAW, initial_setup_class="L6000")
    problem = Problem(0, ops, (machine,), (OrderInfo("P", 30, weight=1), OrderInfo("Q", 30, weight=3)))
    schedule = solve(problem)
    items = by_op(schedule)
    assert items["q:saw"].start == 0
    metrics = compute_metrics(problem, schedule)
    assert metrics.total_tardiness == 30
    assert metrics.weighted_tardiness == 30
    assert metrics.late_orders == 1
    assert metrics.max_tardiness == 30
    assert metrics.objective == 10 * 30 + 60


def test_completed_lots_of_an_order_count_towards_its_completion():
    """The order finished a lot at 900, later than the pending lot ends, so its tardiness is due to the 900."""
    ops = (saw_op("p", due_order="P"),)
    machine = MachineInfo("SAE", SAW, initial_setup_class="L6000")
    problem = Problem(0, ops, (machine,), (OrderInfo("P", 800, completed_lots_end=900),))
    schedule = solve(problem)
    metrics = compute_metrics(problem, schedule)
    assert metrics.order_completion == {"P": 900}
    assert metrics.total_tardiness == 100
    assert metrics.objective == 10 * 100 + 30


def test_an_order_with_only_completed_lots_is_a_constant_in_the_objective():
    ops = (saw_op("p", due_order="P"),)
    machine = MachineInfo("SAE", SAW, initial_setup_class="L6000")
    orders = (OrderInfo("P", 500), OrderInfo("OLD", 100, weight=2, completed_lots_end=150))
    problem = Problem(0, ops, (machine,), orders)
    schedule = solve(problem)
    metrics = compute_metrics(problem, schedule)
    assert metrics.order_completion == {"P": 30, "OLD": 150}
    assert metrics.objective == 10 * 2 * 50 + 30


# ---- Downtime --------------------------------------------------------------------------------------------------------


def test_op_fits_before_a_downtime_setup_included():
    """15 minutes of setup and 50 of work end exactly when the downtime starts at 65, so it does not wait."""
    machine = MachineInfo("SAE", SAW, initial_setup_class="L3000", downtimes=(Downtime(65, 500),))
    problem = Problem(0, (saw_op("p", "L6000", minutes=50),), (machine,), (OrderInfo("A", NO_RUSH),))
    a = by_op(solve(problem))["p:saw"]
    assert (a.start, a.end, a.setup_before) == (15, 65, 15)


def test_op_that_does_not_fit_goes_behind_the_downtime():
    """One minute less room than above: setup and work both run after the downtime."""
    machine = MachineInfo("SAE", SAW, initial_setup_class="L3000", downtimes=(Downtime(64, 500),))
    problem = Problem(0, (saw_op("p", "L6000", minutes=50),), (machine,), (OrderInfo("A", NO_RUSH),))
    a = by_op(solve(problem))["p:saw"]
    assert (a.start, a.end, a.setup_before) == (515, 565, 15)


def test_downtime_avoided_by_taking_the_other_machine():
    """OF1 down for 200 minutes, OF2 up with the same class: the lot goes to OF2 and starts at once."""
    ops = (age_op("L1", machines=("OF1", "OF2")),)
    machines = (furnace("OF1", downtimes=(Downtime(0, 200),)), furnace("OF2", capacity=10, initial="T66"))
    problem = Problem(0, ops, machines, (OrderInfo("A", NO_RUSH),))
    a = by_op(solve(problem))["L1:age"]
    assert (a.machine, a.start) == ("OF2", 0)


# ---- Frozen ops -------------------------------------------------------------------------------------------------------


def test_frozen_ops_are_copied_and_occupy_the_machine():
    frozen = Frozen("OF1", 40, 150, "B-r1-1")
    ops = (
        age_op("F1", frozen=frozen),
        age_op("F2", frozen=frozen),
        age_op("P1"),
    )
    problem = Problem(100, ops, (furnace(),), (OrderInfo("A", NO_RUSH),))
    schedule = solve(problem)
    items = by_op(schedule)
    assert (items["F1:age"].machine, items["F1:age"].start, items["F1:age"].end) == ("OF1", 40, 150)
    assert items["F1:age"].batch_id == "B-r1-1"
    assert items["F2:age"].batch_id == "B-r1-1"
    # The running load is the right recipe with 2 of 6 baskets free, and still cannot be joined.
    assert items["P1:age"].start == 150
    assert items["P1:age"].batch_id != "B-r1-1"
    assert compute_metrics(problem, schedule).makespan == 450 - 100


def test_frozen_op_may_overlap_a_downtime_while_pending_ops_may_not():
    frozen = Frozen("OF1", 40, 300, "B-r1-1")
    ops = (age_op("F1", frozen=frozen), age_op("P1"))
    problem = Problem(100, ops, (furnace(downtimes=(Downtime(100, 280),)),), (OrderInfo("A", NO_RUSH),))
    items = by_op(solve(problem))
    assert items["F1:age"].end == 300
    assert items["P1:age"].start == 300


def test_setup_after_a_frozen_op_starts_from_its_class():
    frozen = Frozen("OF1", 40, 150, "B-r1-1")
    # The machine's initial class is out of date; what counts is what is running on it.
    ops = (age_op("F1", "T5", minutes=110, frozen=frozen), age_op("P1", "T66"))
    problem = Problem(100, ops, (furnace(initial="T66"),), (OrderInfo("A", NO_RUSH),))
    a = by_op(solve(problem))["P1:age"]
    assert (a.start, a.setup_before) == (180, 30)


def test_frozen_ops_count_for_makespan_and_order_completion():
    frozen = Frozen("OF1", 40, 900, "B-r1-1")
    ops = (age_op("F1", frozen=frozen, order_id="Z"), saw_op("p", due_order="A"))
    machines = (furnace(), MachineInfo("SAE", SAW, initial_setup_class="L6000"))
    problem = Problem(100, ops, machines, (OrderInfo("Z", 800), OrderInfo("A", NO_RUSH)))
    schedule = solve(problem)
    metrics = compute_metrics(problem, schedule)
    assert metrics.makespan == 800
    assert metrics.total_tardiness == 100
    assert metrics.order_completion["Z"] == 900


def test_nothing_but_frozen_ops():
    frozen = Frozen("OF1", 40, 150, "B-r1-1")
    problem = Problem(100, (age_op("F1", frozen=frozen),), (furnace(),), (OrderInfo("A", NO_RUSH),))
    schedule = solve(problem)
    assert by_op(schedule)["F1:age"].end == 150


# ---- Two machines faulted at once ------------------------------------------------------------------------------------


def test_two_machines_faulted_at_once():
    """PR2 down 200 with a press paused at 50 left, OF2 down 300. By hand, from now = 100:

    The press ends at 100 + 200 + 50 = 350 and the age op is ready at 358.
    On OF1 it sets up from 358 for 30 minutes (T66 to T6): age 388 to 748, saw 771 to 780, pack 798 to 824.
    On OF2 it must wait for 400 but needs no setup: age to 760, saw 783 to 792, pack 810 to 836.
    Saw and pack need 15 + 10 of setup either way. OF1 gives 724 + 2 * 55 = 834, OF2 gives 736 + 2 * 25 = 786.
    """
    the_lot = lot("L-1", "ST-200", 8)
    press = the_lot.op_id("press")
    state = state_at(
        now=100.0,
        store={"6060": 0, "6063": 0, "6082": 0},
        started=(press,),
        PR2=MachineState(
            "PR2", "FAULT", "6082|M-3001", op_ids=(press,), kind="press", lot_ids=(the_lot.id,), started_min=20.0,
            remaining_work_min=50.0, fault_code="P-204", fault_remaining_min=200.0,
        ),
        OF2=MachineState("OF2", "FAULT", "T6", fault_code="O-301", fault_remaining_min=300.0),
    )
    problem = build_problem(state, [the_lot], [order()]).problem
    assert problem.machine("PR2").downtimes == (Downtime(100, 300, "P-204"),)
    assert problem.machine("OF2").downtimes == (Downtime(100, 400, "O-301"),)
    assert problem.op(press).frozen == Frozen("PR2", 20, 350, None)

    schedule = solve(problem)
    items = by_op(schedule)
    assert (items[the_lot.op_id("age")].machine, items[the_lot.op_id("age")].start) == ("OF2", 400)
    assert items[the_lot.op_id("pack")].end == 836
    assert compute_metrics(problem, schedule).objective == 786
    assert_valid(problem, solve_anneal(problem, time_limit_s=0.3))


# ---- Objective identity, hint, parameters, timeout -------------------------------------------------------------------


def _mid_size_problem() -> Problem:
    lots, orders = realistic_lots(target=12, seed=3)
    state = state_at(
        now=50.0,
        OF1=MachineState("OF1", "FAULT", "T66", fault_code="O-301", fault_remaining_min=90.0),
        PR1=MachineState("PR1", "FAULT", "6060|M-1001", fault_code="P-201", fault_remaining_min=45.0),
    )
    return build_problem(state, lots, orders).problem


def test_hint_covers_every_variable_and_is_a_feasible_solution():
    from ortools.sat.python import cp_model

    problem = _mid_size_problem()
    edd = decode_edd(problem)
    built = _build_model(problem, edd)
    _add_complete_hint(built, problem, edd)
    proto = built.model.Proto()
    assert len(proto.solution_hint.vars) == len(proto.variables)
    assert len(set(proto.solution_hint.vars)) == len(proto.variables)

    solver = cp_model.CpSolver()
    solver.parameters.fix_variables_to_their_hinted_value = True
    solver.parameters.num_workers = 1
    solver.parameters.max_time_in_seconds = 20
    status = solver.solve(built.model)
    assert status == cp_model.OPTIMAL, solver.status_name(status)
    # With every variable fixed there is nothing left to choose: the model's value of the hint is compute_metrics'.
    assert round(solver.objective_value) == compute_metrics(problem, edd).objective


def test_objective_reported_by_cpsat_equals_compute_metrics_on_its_raw_solution():
    """Holds at any point of the search, not just at the optimum, since every objective term is an exact equality."""
    from ortools.sat.python import cp_model

    problem = _mid_size_problem()
    edd = decode_edd(problem)
    built = _build_model(problem, edd)
    _add_complete_hint(built, problem, edd)
    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = 1.5
    solver.parameters.num_workers = WORKERS
    solver.parameters.cp_model_probing_level = 0
    status = solver.solve(built.model)
    assert status in (cp_model.OPTIMAL, cp_model.FEASIBLE)
    raw = Schedule("cpsat", "feasible", tuple(_extract(built, problem, solver)))
    assert_valid(problem, raw)
    assert compute_metrics(problem, raw).objective == round(solver.objective_value)


def test_arguments_reach_the_solver(monkeypatch):
    from ortools.sat.python import cp_model

    seen = {}
    original = cp_model.CpSolver.solve

    def spy(self, model, *args, **kwargs):
        seen["workers"] = self.parameters.num_workers
        seen["seed"] = self.parameters.random_seed
        seen["limit"] = self.parameters.max_time_in_seconds
        return original(self, model, *args, **kwargs)

    monkeypatch.setattr(cp_model.CpSolver, "solve", spy)
    problem = Problem(0, (saw_op("p"),), (MachineInfo("SAE", SAW, initial_setup_class="L6000"),), (OrderInfo("A", NO_RUSH),))
    solve_cpsat(problem, time_limit_s=2.5, seed=77, workers=3)
    assert seen["workers"] == 3
    assert seen["seed"] == 77
    assert 0 < seen["limit"] <= 2.5


def test_solve_cpsat_really_passes_the_complete_hint(monkeypatch):
    from ortools.sat.python import cp_model

    seen = {}
    original = cp_model.CpSolver.solve

    def spy(self, model, *args, **kwargs):
        proto = model.Proto()
        seen["hinted"] = len(proto.solution_hint.vars)
        seen["variables"] = len(proto.variables)
        return original(self, model, *args, **kwargs)

    monkeypatch.setattr(cp_model.CpSolver, "solve", spy)
    solve_cpsat(_mid_size_problem(), time_limit_s=0.5, workers=WORKERS)
    assert seen["variables"] > 100
    assert seen["hinted"] == seen["variables"]


def test_a_repair_far_beyond_the_horizon_of_everything_else():
    """The E2E scenario: PR2 down for 100000 minutes with ST-200 lots pending. They wait, the others go on."""
    lots = [lot("L-1", "ST-200", 8), lot("L-2", "FR-60", 8)]
    state = state_at(
        now=120.0,
        store={"6060": 24, "6063": 0, "6082": 24},
        PR2=MachineState("PR2", "FAULT", "6082|M-3001", fault_code="P-204", fault_remaining_min=100000.0),
    )
    problem = build_problem(state, lots, [order()]).problem
    assert problem.machine("PR2").downtimes == (Downtime(120, 100120, "P-204"),)
    schedule = solve(problem)
    items = by_op(schedule)
    assert items["L-1:press"].start == 100120
    assert items["L-2:press"].start == 120
    assert_valid(problem, solve_anneal(problem, time_limit_s=0.3))
    assert_valid(problem, keep_schedule(problem, decode_edd(problem)))


def test_timeout_never_raises_and_still_returns_a_valid_plan():
    lots, orders = realistic_lots(target=25)
    problem = build_problem(state_at(), lots, orders).problem
    schedule = solve_cpsat(problem, time_limit_s=0.01, workers=2)
    assert schedule.status in ("optimal", "feasible", "no_solution")
    if schedule.status != "no_solution":
        assert_valid(problem, schedule)
        assert compute_metrics(problem, schedule).objective <= compute_metrics(problem, decode_edd(problem)).objective


def test_a_result_worse_than_the_start_plan_is_not_returned(monkeypatch):
    """Can happen if CP-SAT runs out of time before loading the hint. The start plan is valid, so it wins, labelled so."""
    from werkhalle.scheduling import cpsat

    problem = _mid_size_problem()
    real_extract = cpsat._extract

    def worse(built, the_problem, solver):
        return [
            a if the_problem.op(a.op_id).frozen else replace(a, start=a.start + 5000, end=a.end + 5000)
            for a in real_extract(built, the_problem, solver)
        ]

    monkeypatch.setattr(cpsat, "_extract", worse)
    monkeypatch.setattr(cpsat, "retime", lambda the_problem, schedule: schedule)
    schedule = solve_cpsat(problem, time_limit_s=0.5, workers=WORKERS)
    assert schedule.status == "feasible"
    assert schedule.solver == "cpsat"
    assert "Startlösung" in schedule.note
    assert_valid(problem, schedule)
    assert compute_metrics(problem, schedule).objective == compute_metrics(problem, cpsat.decode_best_rule(problem)).objective


def test_an_op_no_machine_can_take_is_no_solution_not_an_exception():
    problem = Problem(0, (saw_op("p"),), (MachineInfo("VP", PACK),), (OrderInfo("A", NO_RUSH),))
    schedule = solve_cpsat(problem, time_limit_s=1, workers=1)
    assert schedule.status == "no_solution"
    assert schedule.assignments == ()


def test_a_better_caller_hint_is_kept_and_a_broken_one_is_ignored():
    """Warm start: what the caller hints is carried over by keep_schedule and must never make the result worse."""
    lots, orders = realistic_lots(target=8, seed=5)
    problem = build_problem(state_at(), lots, orders).problem
    good = solve_cpsat(problem, time_limit_s=1.5, workers=WORKERS)
    again = solve_cpsat(problem, time_limit_s=0.5, workers=WORKERS, hint=good)
    assert compute_metrics(problem, again).objective <= compute_metrics(problem, good).objective

    broken = replace(good, assignments=tuple(replace(a, machine="VP") for a in good.assignments))
    result = solve_cpsat(problem, time_limit_s=0.5, workers=WORKERS, hint=broken)
    assert_valid(problem, result)


def test_hint_is_carried_over_when_the_problem_has_changed():
    lots, orders = realistic_lots(target=8, seed=5)
    old_problem = build_problem(state_at(), lots[:-2], orders).problem
    old = keep_schedule(old_problem, decode_edd(old_problem))
    problem = build_problem(state_at(), lots, orders).problem
    assert_valid(problem, solve_cpsat(problem, time_limit_s=0.5, workers=WORKERS, hint=old))


def test_batch_ids_use_the_prefix_and_do_not_collide_with_a_frozen_load():
    frozen = Frozen("OF1", 40, 150, "B-1")
    ops = (age_op("F1", frozen=frozen), age_op("P1"))
    problem = Problem(100, ops, (furnace(),), (OrderInfo("A", NO_RUSH),), batch_prefix="B")
    assert by_op(solve(problem))["P1:age"].batch_id == "B-2"
    renamed = replace(problem, ops=(replace(ops[0], frozen=Frozen("OF1", 40, 150, "B-r1-1")), ops[1]), batch_prefix="B-r2")
    assert by_op(solve(renamed))["P1:age"].batch_id == "B-r2-1"
    assert validate_schedule(renamed, solve(renamed)) == []


@pytest.mark.parametrize("seed", [3, 8, 21])
def test_the_horizon_taken_from_the_start_plan_does_not_cut_off_the_optimum(seed):
    """The tighter horizon is what keeps "optimal" honest: same proven optimum with and without it."""
    from ortools.sat.python import cp_model

    lots, orders = realistic_lots(target=3, seed=seed)
    tight = [replace(o, due=300 + 120 * k) for k, o in enumerate(orders)]
    problem = build_problem(state_at(), lots, tight).problem
    start = decode_best_rule(problem)
    results = []
    for built in (_build_model(problem, start), _build_model(problem, None)):
        solver = cp_model.CpSolver()
        solver.parameters.max_time_in_seconds = 30
        solver.parameters.num_workers = WORKERS
        assert solver.solve(built.model) == cp_model.OPTIMAL
        results.append(round(solver.objective_value))
    assert results[0] == results[1] <= compute_metrics(problem, start).objective


def test_the_horizon_counts_a_spare_melt_at_the_end_as_makespan_only():
    """An op without an order costs makespan alone past the last due date, so the bound must not price it as tardiness."""
    from ortools.sat.python import cp_model

    spare = Op("spare:pack", PACK, None, None, {"VP": 200}, "B-16")
    ops = (pack_op("x", "B-10", due_order="A"), spare)
    machine = MachineInfo("VP", PACK, initial_setup_class="B-16")
    problem = Problem(0, ops, (machine,), (OrderInfo("A", 10, 3),))
    results = []
    for built in (_build_model(problem, decode_best_rule(problem)), _build_model(problem, None)):
        solver = cp_model.CpSolver()
        solver.parameters.num_workers = WORKERS
        assert solver.solve(built.model) == cp_model.OPTIMAL
        results.append(round(solver.objective_value))
    # x first: setup 10, x to 20, setup 10, spare to 230. Tardiness 10 at weight 3 and factor 10, makespan 230, setups 20 x 2.
    assert results == [570, 570]


def test_cpsat_starts_from_the_best_rule_when_that_beats_edd():
    from werkhalle.scheduling import cpsat

    lots, orders = realistic_lots(target=12, seed=6)
    problem = build_problem(state_at(), lots, orders).problem
    start, note = cpsat._usable_hint(problem, None)
    assert note == ""
    assert_valid(problem, start)
    objective = compute_metrics(problem, start).objective
    assert objective < compute_metrics(problem, decode_edd(problem)).objective
    assert objective == compute_metrics(problem, decode_best_rule(problem)).objective
    # A plan carried over from the caller is only used when it is at least as good as the rule.
    assert compute_metrics(problem, cpsat._usable_hint(problem, decode_edd(problem))[0]).objective == objective
