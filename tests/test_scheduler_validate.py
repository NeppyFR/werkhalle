"""One negative test per validator rule. Each corrupts a schedule that is valid at first and expects exactly that rule."""
from __future__ import annotations

from dataclasses import replace

import pytest
from test_scheduler_support import by_op

from werkhalle.domain.catalog import AGE, CAST, PRESS, SAW
from werkhalle.scheduling import (
    Assignment,
    BilletSupply,
    Downtime,
    Frozen,
    MachineInfo,
    Op,
    OrderInfo,
    Pred,
    Problem,
    Schedule,
    validate_schedule,
)
from werkhalle.scheduling.model import Unit
from werkhalle.scheduling.retime import forward_pass

CAST_ID = "M-6060-00001:cast"


def furnace_op(lot_id: str, *, minutes=300, setup="T66", family="T66", preds=(), frozen=None) -> Op:
    return Op(
        f"{lot_id}:age",
        AGE,
        lot_id,
        "A",
        {"OF1": minutes},
        setup,
        preds=preds,
        size=2,
        batch_family=family,
        frozen=frozen,
    )


def fixture() -> tuple[Problem, Schedule]:
    """A small plant mid-run, with one of everything a rule can talk about, and its hand-checked valid schedule."""
    machines = (
        MachineInfo("GUSS", CAST, initial_setup_class="6060"),
        MachineInfo("PR1", PRESS, initial_setup_class="6060|M-1001"),
        # The first downtime overlaps the frozen load, which is exempt from it.
        MachineInfo("OF1", AGE, 6, True, initial_setup_class="T66", downtimes=(Downtime(100, 120), Downtime(3000, 3100))),
        MachineInfo("SAE", SAW, initial_setup_class="L6000"),
    )
    press = dict(kind=PRESS, order_id="A", durations={"PR1": 60}, setup_class="6060|M-1001", alloy="6060", billets_in=8)
    # Started before now and of odd length: frozen ops are exempt from start_before_now and duration.
    frozen_load = Frozen("OF1", 40, 150, "F-1")
    ops = (
        Op(CAST_ID, CAST, None, None, {"GUSS": 93}, "6060", alloy="6060", billets_out=24),
        Op("L1:press", lot_id="L1", **press),
        Op("L2:press", lot_id="L2", preds=(Pred(CAST_ID, 8),), **press),
        Op("L8:press", PRESS, "L8", "A", {"PR1": 60}, "6060|M-1001", release=300),
        furnace_op("L0", minutes=5, frozen=frozen_load),
        furnace_op("L9", minutes=5, frozen=frozen_load),
        furnace_op("L3"),
        furnace_op("L4"),
        furnace_op("L5"),
        furnace_op("L6"),
        furnace_op("L1", preds=(Pred("L1:press", 8),)),
        furnace_op("L2", preds=(Pred("L2:press", 8),)),
        furnace_op("L10", family="X"),
        furnace_op("L7", minutes=180, setup="T5", family="T5"),
        furnace_op("L11", minutes=180, setup="T5", family="T5"),
        Op("L3:saw", SAW, "L3", "A", {"SAE": 20}, "L3000", preds=(Pred("L3:age", 8),)),
        Op("L1:saw", SAW, "L1", "A", {"SAE": 20}, "L6000", preds=(Pred("L1:age", 8),)),
        Op("L2:saw", SAW, "L2", "A", {"SAE": 20}, "L6000", preds=(Pred("L2:age", 8),)),
    )
    problem = Problem(
        now=100,
        ops=ops,
        machines=machines,
        orders=(OrderInfo("A", 100000),),
        billet_supply=(BilletSupply("6060", 8, 100),),
    )
    sequences = {
        "GUSS": [Unit((CAST_ID,))],
        "PR1": [Unit(("L1:press",)), Unit(("L2:press",)), Unit(("L8:press",))],
        "OF1": [
            Unit(("L3:age", "L4:age", "L5:age"), "B-1"),
            Unit(("L1:age", "L2:age"), "B-2"),
            Unit(("L6:age",), "B-3"),
            Unit(("L10:age",), "B-4"),
            Unit(("L11:age", "L7:age"), "B-5"),
        ],
        "SAE": [Unit(("L3:saw",)), Unit(("L1:saw",)), Unit(("L2:saw",))],
    }
    assignments, complete = forward_pass(problem, sequences)
    assert complete
    return problem, Schedule("edd", "feasible", tuple(assignments))


def with_changes(schedule: Schedule, op_id: str, **changes) -> Schedule:
    return replace(
        schedule, assignments=tuple(replace(a, **changes) if a.op_id == op_id else a for a in schedule.assignments)
    )


def shift(schedule: Schedule, op_id: str, by: int) -> Schedule:
    a = by_op(schedule)[op_id]
    return with_changes(schedule, op_id, start=a.start + by, end=a.end + by)


def rules(problem: Problem, schedule: Schedule) -> set[str]:
    return {v.rule for v in validate_schedule(problem, schedule)}


def test_fixture_is_valid_and_has_the_hand_derived_times():
    problem, schedule = fixture()
    assert validate_schedule(problem, schedule) == []
    items = by_op(schedule)
    assert (items[CAST_ID].start, items[CAST_ID].end) == (100, 193)
    assert (items["L1:press"].start, items["L2:press"].start, items["L8:press"].start) == (100, 201, 300)
    # After the frozen load: 150. L1 is ready at 168, L2 at 269, so B-2 starts when B-1 ends.
    assert (items["L3:age"].start, items["L3:age"].end) == (150, 450)
    assert (items["L1:age"].start, items["L2:age"].start) == (450, 450)
    assert (items["L6:age"].start, items["L10:age"].start) == (750, 1050)
    # Change to T5 takes 30 minutes, and L11 sorts before L7 so it is the one carrying the setup.
    assert (items["L11:age"].start, items["L11:age"].setup_before, items["L7:age"].setup_before) == (1380, 30, 0)
    # L3:saw waits for its lot, ready at 458, and only then sets up. L1:saw sets up the moment its lot is there too.
    assert (items["L3:saw"].setup_before, items["L3:saw"].start) == (15, 473)
    assert (items["L1:saw"].setup_before, items["L1:saw"].start) == (15, 773)
    assert items["L0:age"].end == 150


def test_unassigned():
    problem, schedule = fixture()
    # The last unit on its machine, so nothing else's setup depends on it.
    corrupted = replace(schedule, assignments=tuple(a for a in schedule.assignments if a.op_id != "L2:saw"))
    assert rules(problem, corrupted) == {"unassigned"}


def test_unknown_op():
    problem, schedule = fixture()
    corrupted = replace(schedule, assignments=(*schedule.assignments, Assignment("X:press", "PR1", 5000, 5060)))
    assert rules(problem, corrupted) == {"unknown_op"}


def test_unknown_op_also_catches_a_double_assignment():
    problem, schedule = fixture()
    corrupted = replace(schedule, assignments=(*schedule.assignments, by_op(schedule)["L2:saw"]))
    assert rules(problem, corrupted) == {"unknown_op"}


def test_not_eligible():
    problem, schedule = fixture()
    assert rules(problem, with_changes(schedule, "L8:press", machine="SAE")) == {"not_eligible"}


def test_duration():
    problem, schedule = fixture()
    a = by_op(schedule)["L2:saw"]
    assert rules(problem, with_changes(schedule, "L2:saw", end=a.end - 1)) == {"duration"}
    assert rules(problem, with_changes(schedule, "L2:saw", end=a.end + 1)) == {"duration"}


def test_precedence():
    problem, schedule = fixture()
    # Its 15 minute setup would begin one minute short of the 8 minute transport lag behind L1:age.
    assert rules(problem, shift(schedule, "L1:saw", -1)) == {"precedence"}


def test_precedence_counts_a_load_members_share_of_the_setup():
    machine = MachineInfo("OF1", AGE, 6, True, initial_setup_class="T66")
    first = furnace_op("a", minutes=180, setup="T5", family="T5")
    second = furnace_op("b", minutes=180, setup="T5", family="T5", preds=(Pred("x:saw", 8),))
    before = Op("x:saw", SAW, "x", "A", {"SAE": 10}, "L6000")
    saw = MachineInfo("SAE", SAW, initial_setup_class="L6000")
    problem = Problem(0, (before, first, second), (machine, saw), (OrderInfo("A", 1000),))
    # b is ready at 18. a carries the load's setup, but b goes in with it, so the setup cannot begin before 18.
    good = Schedule(
        "edd",
        "feasible",
        (
            Assignment("x:saw", "SAE", 0, 10),
            Assignment("a:age", "OF1", 48, 228, 30, "B-1"),
            Assignment("b:age", "OF1", 48, 228, 0, "B-1"),
        ),
    )
    assert validate_schedule(problem, good) == []
    early = with_changes(
        with_changes(good, "a:age", start=47, end=227), "b:age", start=47, end=227
    )
    assert rules(problem, early) == {"precedence"}


def test_release():
    problem, schedule = fixture()
    assert by_op(schedule)["L8:press"].start == 300
    assert rules(problem, shift(schedule, "L8:press", -1)) == {"release"}


def test_release_counts_the_setup():
    machine = MachineInfo("SAE", SAW, initial_setup_class="L3000")
    op = Op("a:saw", SAW, "a", "A", {"SAE": 20}, "L6000", release=100)
    problem = Problem(0, (op,), (machine,), (OrderInfo("A", 1000),))
    assert validate_schedule(problem, Schedule("edd", "feasible", (Assignment(op.id, "SAE", 115, 135, 15),))) == []
    assert rules(problem, Schedule("edd", "feasible", (Assignment(op.id, "SAE", 114, 134, 15),))) == {"release"}


def test_overlap():
    problem, schedule = fixture()
    items = by_op(schedule)
    assert items["L1:saw"].end > items["L1:saw"].start + 5
    assert rules(problem, shift(schedule, "L2:saw", -5)) == {"overlap"}


def test_overlap_with_a_frozen_op():
    problem, schedule = fixture()
    # Move the first pending load one minute into the frozen one.
    corrupted = schedule
    for op_id in ("L3:age", "L4:age", "L5:age"):
        corrupted = shift(corrupted, op_id, -1)
    assert rules(problem, corrupted) == {"overlap"}


def test_setup_wrong_minutes():
    problem, schedule = fixture()
    assert by_op(schedule)["L3:saw"].setup_before == 15
    assert rules(problem, with_changes(schedule, "L3:saw", setup_before=0)) == {"setup"}
    assert rules(problem, with_changes(schedule, "L2:saw", setup_before=15)) == {"setup"}


def test_setup_no_time_for_it():
    problem, schedule = fixture()
    # The T5 load claims its 30 minutes but starts only 29 after the previous load ended.
    corrupted = shift(shift(schedule, "L11:age", -1), "L7:age", -1)
    assert rules(problem, corrupted) == {"setup"}


def test_setup_counts_the_initial_class():
    machine = MachineInfo("OF1", AGE, 6, True, initial_setup_class="T66")
    op = furnace_op("L1", minutes=180, setup="T5", family="T5")
    problem = Problem(0, (op,), (machine,), (OrderInfo("A", 1000),))
    good = Schedule("edd", "feasible", (Assignment(op.id, "OF1", 30, 210, 30, "B-1"),))
    assert validate_schedule(problem, good) == []
    assert rules(problem, with_changes(good, op.id, start=0, end=180, setup_before=0)) == {"setup"}
    assert rules(problem, with_changes(good, op.id, start=29, end=209)) == {"setup"}


def test_capacity():
    problem, schedule = fixture()
    # B-1 already holds 6 baskets, which is what OF1 takes. L6 is alone in the load behind it and has no predecessors.
    target = by_op(schedule)["L3:age"]
    corrupted = with_changes(schedule, "L6:age", start=target.start, end=target.end, batch_id=target.batch_id)
    assert rules(problem, corrupted) == {"capacity"}


def test_batch_family():
    problem, schedule = fixture()
    # L10 has the same class and length as B-2 but its own family, and B-2 has room for it.
    target = by_op(schedule)["L1:age"]
    corrupted = with_changes(schedule, "L10:age", start=target.start, end=target.end, batch_id=target.batch_id)
    assert rules(problem, corrupted) == {"batch_family"}


def test_batch_sync():
    problem, schedule = fixture()
    assert rules(problem, shift(schedule, "L7:age", 1)) == {"batch_sync"}


def test_batch_sync_furnace_op_needs_a_load():
    problem, schedule = fixture()
    assert rules(problem, with_changes(schedule, "L10:age", batch_id=None)) == {"batch_sync"}


def test_batch_sync_only_furnaces_run_loads():
    problem, schedule = fixture()
    assert rules(problem, with_changes(schedule, "L2:saw", batch_id="B-9")) == {"batch_sync"}


def test_downtime():
    problem, schedule = fixture()
    corrupted = schedule
    for op_id in ("L11:age", "L7:age"):
        corrupted = with_changes(corrupted, op_id, start=2900, end=3080)
    assert rules(problem, corrupted) == {"downtime"}


def test_downtime_includes_the_setup():
    problem, schedule = fixture()
    # The op itself clears the downtime [3000,3100) but its 30 minute setup would run from 3080.
    corrupted = schedule
    for op_id in ("L11:age", "L7:age"):
        corrupted = with_changes(corrupted, op_id, start=3110, end=3290)
    assert rules(problem, corrupted) == {"downtime"}
    # Ten minutes later everything is clear.
    corrupted = schedule
    for op_id in ("L11:age", "L7:age"):
        corrupted = with_changes(corrupted, op_id, start=3130, end=3310)
    assert validate_schedule(problem, corrupted) == []


def test_frozen():
    problem, schedule = fixture()
    a = by_op(schedule)["L0:age"]
    assert rules(problem, with_changes(schedule, "L0:age", start=a.start - 1)) == {"frozen"}


def test_frozen_batch_a_frozen_op_keeps_its_id():
    problem, schedule = fixture()
    assert rules(problem, with_changes(schedule, "L0:age", batch_id="B-77")) == {"frozen_batch"}


def test_frozen_batch_a_pending_op_never_joins():
    problem, schedule = fixture()
    assert rules(problem, with_changes(schedule, "L10:age", batch_id="F-1")) == {"frozen_batch"}


def test_start_before_now():
    problem, schedule = fixture()
    assert by_op(schedule)[CAST_ID].start == problem.now
    assert rules(problem, shift(schedule, CAST_ID, -1)) == {"start_before_now"}


def test_start_before_available_from():
    problem, schedule = fixture()
    machines = tuple(replace(m, available_from=150) if m.id == "GUSS" else m for m in problem.machines)
    assert rules(replace(problem, machines=machines), schedule) == {"start_before_now"}


def test_billet_balance():
    problem, schedule = fixture()
    # Without its melt predecessor nothing but the replay can stop L2 from pressing before the melt has arrived.
    ops = tuple(replace(op, preds=()) if op.id == "L2:press" else op for op in problem.ops)
    open_problem = replace(problem, ops=ops)
    assert validate_schedule(open_problem, schedule) == []
    items = by_op(schedule)
    assert items["L1:press"].end == 160
    assert items[CAST_ID].end + 8 == 201
    corrupted = with_changes(schedule, "L2:press", start=160, end=220)
    assert rules(open_problem, corrupted) == {"billet_balance"}
    # The very minute the melt arrives is fine: supply before demand.
    assert validate_schedule(open_problem, with_changes(schedule, "L2:press", start=201, end=261)) == []
    assert rules(open_problem, with_changes(schedule, "L2:press", start=200, end=260)) == {"billet_balance"}


def test_billet_balance_counts_from_the_begin_of_the_setup():
    """StartOperation takes the billets before the press starts to set up, so a setup cannot run ahead of the melt."""
    machine = MachineInfo("PR1", PRESS, initial_setup_class="6063|M-2001")
    op = Op("a:press", PRESS, "a", "A", {"PR1": 60}, "6060|M-1001", alloy="6060", billets_in=8)
    problem = Problem(0, (op,), (machine,), (OrderInfo("A", 1000),), billet_supply=(BilletSupply("6060", 8, 100),))
    good = Schedule("edd", "feasible", (Assignment(op.id, "PR1", 155, 215, 55),))
    assert validate_schedule(problem, good) == []
    assert rules(problem, with_changes(good, op.id, start=154, end=214)) == {"billet_balance"}


def test_billet_balance_ignores_a_running_press():
    """A frozen press took its billets before the snapshot, so it must not be counted against the store again."""
    machine = MachineInfo("PR1", PRESS, initial_setup_class="6060|M-1001")
    op = Op(
        "L1:press",
        PRESS,
        "L1",
        "A",
        {"PR1": 60},
        "6060|M-1001",
        frozen=Frozen("PR1", 0, 60),
        alloy="6060",
        billets_in=8,
    )
    problem = Problem(30, (op,), (machine,), (OrderInfo("A", 1000),))
    assert validate_schedule(problem, Schedule("edd", "feasible", (Assignment(op.id, "PR1", 0, 60),))) == []


ALL_RULES = {
    "unassigned",
    "unknown_op",
    "not_eligible",
    "duration",
    "precedence",
    "release",
    "overlap",
    "setup",
    "capacity",
    "batch_family",
    "batch_sync",
    "downtime",
    "frozen",
    "frozen_batch",
    "start_before_now",
    "billet_balance",
}


@pytest.mark.parametrize("rule", sorted(ALL_RULES))
def test_every_documented_rule_has_a_test_here(rule):
    """The list on model.Violation is the contract; a rule added there without a test must fail this."""
    from werkhalle.scheduling import model

    documented = model.__file__
    with open(documented, encoding="utf-8") as handle:
        source = handle.read()
    section = source[source.index("class Violation") : source.index("class Unit")]
    assert rule in section
    assert any(name.startswith(f"test_{rule}") for name in globals())


def test_no_undocumented_rule_is_listed():
    from werkhalle.scheduling import model

    with open(model.__file__, encoding="utf-8") as handle:
        source = handle.read()
    section = source[source.index("rule: str") : source.index("op_ids: tuple[str, ...]")]
    listed = {word.strip(" ,#\n") for word in section.replace("rule: str", "").split()}
    assert {word for word in listed if word} == ALL_RULES


def _load_problem(capacity: int) -> tuple[Problem, Schedule]:
    """One furnace load of two ops with two baskets each, so four baskets in all, on a furnace of the given capacity."""
    machines = (MachineInfo("OF1", AGE, capacity, True, initial_setup_class="T66"),)
    ops = (furnace_op("L1"), furnace_op("L2"))
    orders = (OrderInfo("A", 10_000),)
    problem = Problem(now=0, ops=ops, machines=machines, orders=orders)
    schedule = Schedule("test", "feasible", tuple(Assignment(op.id, "OF1", 0, 300, 0, "B-1") for op in ops), 0.0)
    return problem, schedule


def test_capacity_exactly_one_basket_over():
    assert "capacity" not in rules(*_load_problem(4))
    assert "capacity" in rules(*_load_problem(3))


def test_billet_balance_a_shortage_of_exactly_one():
    def problem_with(stock: int) -> tuple[Problem, Schedule]:
        press = Op("L1:press", PRESS, "L1", "A", {"PR1": 60}, "6060|M-1001", alloy="6060", billets_in=8)
        problem = Problem(now=0, ops=(press,), machines=(MachineInfo("PR1", PRESS, initial_setup_class="6060|M-1001"),),
                          orders=(OrderInfo("A", 10_000),), billet_supply=(BilletSupply("6060", stock, 0),))
        return problem, Schedule("test", "feasible", (Assignment("L1:press", "PR1", 0, 60, 0, None),), 0.0)

    assert "billet_balance" not in rules(*problem_with(8))
    assert "billet_balance" in rules(*problem_with(7))


def test_frozen_a_changed_end_alone_is_caught():
    frozen = Frozen("PR1", 0, 60)
    press = Op("L1:press", PRESS, "L1", "A", {"PR1": 60}, "6060|M-1001", frozen=frozen)
    problem = Problem(now=30, ops=(press,), machines=(MachineInfo("PR1", PRESS, initial_setup_class="6060|M-1001"),),
                      orders=(OrderInfo("A", 10_000),))
    good = Schedule("test", "feasible", (Assignment("L1:press", "PR1", 0, 60, 0, None),), 0.0)
    assert "frozen" not in rules(problem, good)
    assert "frozen" in rules(problem, with_changes(good, "L1:press", end=61))


def test_overlap_with_an_earlier_longer_unit_after_a_short_one_inside_it():
    """The third unit starts after the short one ended but inside the long one, which is not the one just before it."""
    def press(lot: str, minutes: int) -> Op:
        return Op(f"{lot}:press", PRESS, lot, "A", {"PR1": minutes}, "6060|M-1001")

    ops = (press("L1", 100), press("L2", 10), press("L3", 10))
    problem = Problem(now=0, ops=ops, machines=(MachineInfo("PR1", PRESS, initial_setup_class="6060|M-1001"),),
                      orders=(OrderInfo("A", 10_000),))
    schedule = Schedule("test", "feasible", (
        Assignment("L1:press", "PR1", 0, 100, 0, None),
        Assignment("L2:press", "PR1", 10, 20, 0, None),
        Assignment("L3:press", "PR1", 50, 60, 0, None),
    ), 0.0)
    overlapping = [v for v in validate_schedule(problem, schedule) if v.rule == "overlap"]
    assert any("L1:press" in v.op_ids and "L3:press" in v.op_ids for v in overlapping)
