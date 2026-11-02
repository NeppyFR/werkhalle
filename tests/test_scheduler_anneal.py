"""Annealing and the serial decoder it shares with decode_edd."""
from __future__ import annotations

import itertools
import random

from test_scheduler_support import NO_RUSH, assert_valid, by_op, lot, order, realistic_lots, state_at

from werkhalle.domain.catalog import AGE, SAW
from werkhalle.domain.orders import Order, split_order
from werkhalle.domain.state import MachineState
from werkhalle.scheduling import (
    Downtime,
    Frozen,
    MachineInfo,
    Op,
    OrderInfo,
    Problem,
    Schedule,
    build_problem,
    compute_metrics,
    decode_edd,
    retime,
    solve_anneal,
    solve_cpsat,
    validate_schedule,
)
from werkhalle.scheduling import anneal
from werkhalle.scheduling.decoder import AUTO, WAIT_GENES, Compiled, assignments_of, decode, decode_best_rule


def wait_gene_problem() -> Problem:
    """ST-200, 93 bars: 3 lots of 8 billets, stock exactly 24, all on PR2. Their 360 minute soak fits one load if held."""
    lots = split_order(Order("A-1", "ST-200", 93, NO_RUSH), 1)
    assert [the_lot.n_billets for the_lot in lots] == [8, 8, 8]
    state = state_at(store={"6060": 0, "6063": 0, "6082": 24})
    return build_problem(state, lots, [OrderInfo("A-1", NO_RUSH)]).problem


def test_wait_gene_case_edd_opens_loads_too_early():
    """Hand-derived: presses end at 96, 192, 288, so lots reach the furnaces at 104, 200, 296; last pack ends at 875."""
    problem = wait_gene_problem()
    edd = decode_edd(problem)
    assert_valid(problem, edd)
    assert compute_metrics(problem, edd).makespan == 875


def test_wait_gene_case_annealing_holds_a_load_and_finds_the_optimum():
    """Two lots share OF1, whose opener waits for the second from 104 to 200 (setup to 230, soak to 590); the third goes
    alone on OF2, with T6 mounted, 296 to 656. Saw 613 to 631 and 664 to 673, pack 640 to 718. Objective 718 + 2 * 55.

    The plan's analysis gave about 759 from a model where setups ran ahead of the material. The plant does not do that,
    and with setup after arrival this split beats one big load, which reaches only 784 (see the next test).
    """
    problem = wait_gene_problem()
    schedule = solve_anneal(problem, time_limit_s=20, seed=1, max_iterations=20_000)
    assert_valid(problem, schedule)
    metrics = compute_metrics(problem, schedule)
    assert (metrics.makespan, metrics.setup_minutes, metrics.objective) == (718, 55, 828)
    loads: dict[str, list] = {}
    for a in schedule.assignments:
        if problem.op(a.op_id).kind == AGE:
            loads.setdefault(a.batch_id, []).append(a)
    assert sorted(len(members) for members in loads.values()) == [1, 2]
    assert sorted(members[0].start for members in loads.values()) == [230, 296]


def test_wait_gene_case_is_proven_optimal_by_cpsat():
    problem = wait_gene_problem()
    optimal = solve_cpsat(problem, time_limit_s=10, workers=4)
    assert optimal.status == "optimal"
    assert compute_metrics(problem, optimal).objective == 828
    assert compute_metrics(problem, optimal).makespan == 718
    annealed = solve_anneal(problem, time_limit_s=20, seed=2, max_iterations=20_000)
    assert compute_metrics(problem, annealed).objective == compute_metrics(problem, optimal).objective


def test_wait_gene_held_by_the_opener_lets_later_lots_join():
    problem = wait_gene_problem()
    c = Compiled(problem)
    priority = c.edd_priority()
    genes = c.machine_genes()

    def result_with(wait_of_first: int) -> tuple[int, int, int]:
        wait = c.wait_genes()
        wait[priority[0]] = wait_of_first
        decoded = decode(c, priority, genes, wait, keep=True)
        schedule = retime(problem, Schedule("edd", "feasible", tuple(assignments_of(c, decoded))))
        assert_valid(problem, schedule)
        metrics = compute_metrics(problem, schedule)
        return metrics.makespan, metrics.setup_minutes, metrics.objective

    # Lot 1 is ready at 104, lot 2 at 200, lot 3 at 296. Saw and pack cost 15 + 10 of setup in every case.
    # No hold: three lots on two furnaces, the last soak ends at 824, plus the 30 minute setup of the other furnace.
    assert result_with(0) == (875, 55, 985)
    # Holding 240 covers lot 3 too: one load on the furnace with T6 mounted.
    assert result_with(240) == (784, 25, 834)
    # Holding 180 only reaches 284. Lot 3 takes the other furnace, which finishes the order sooner but needs its
    # setup, and setup is priced: 737 + 2 * 55 loses to 784 + 2 * 25.
    assert result_with(180) == (737, 55, 847)


def test_a_wait_gene_nobody_uses_does_not_delay_anything():
    problem = build_problem(state_at(), [lot("L-1")], [order()]).problem
    c = Compiled(problem)
    base = decode(c, c.edd_priority(), c.machine_genes(), c.wait_genes())
    held = [240] * c.n_groups
    assert decode(c, c.edd_priority(), c.machine_genes(), held) == base


def test_decoder_objective_is_exactly_compute_metrics_of_what_it_placed():
    lots, orders = realistic_lots(target=20, seed=9)
    state = state_at(
        now=20.0, OF1=MachineState("OF1", "FAULT", "T66", fault_code="O-301", fault_remaining_min=60.0)
    )
    downtimes = {
        "PR1": [Downtime(100, 180)],
        "OF2": [Downtime(300, 350)],
        "SAE": [Downtime(600, 640)],
        "VP": [Downtime(700, 730)],
    }
    problem = build_problem(state, lots, orders, downtimes=downtimes).problem
    c = Compiled(problem)
    rng = random.Random(1)
    priority = c.edd_priority()
    for _ in range(50):
        rng.shuffle(priority)
        genes = [rng.randrange(AUTO, len(options)) for options in c.options]
        wait = [rng.choice(WAIT_GENES) for _ in range(c.n_groups)]
        decoded = decode(c, priority, genes, wait, keep=True)
        raw = Schedule("anneal", "feasible", tuple(assignments_of(c, decoded)))
        # The decode is already a valid schedule, not just a fitness estimate.
        assert_valid(problem, raw)
        assert compute_metrics(problem, raw).objective == decoded.objective
        retimed = retime(problem, raw)
        assert_valid(problem, retimed)
        assert compute_metrics(problem, retimed).objective <= decoded.objective


def test_decoder_keeps_setups_clear_of_downtimes_in_arbitrary_orders():
    """A saw changing between lengths, jobs released all over, downtimes between them. Checked before any retime."""
    rng = random.Random(21)
    ops = tuple(
        Op(
            f"L{i}:saw",
            SAW,
            f"L{i}",
            "A",
            {"SAE": rng.randint(5, 40)},
            rng.choice(("L3000", "L6000", "L7000")),
            release=rng.randint(0, 400),
        )
        for i in range(14)
    )
    downtimes = (Downtime(120, 150), Downtime(260, 275), Downtime(420, 500))
    machine = MachineInfo("SAE", SAW, initial_setup_class="L6000", downtimes=downtimes)
    problem = Problem(0, ops, (machine,), (OrderInfo("A", NO_RUSH),))
    c = Compiled(problem)
    priority = c.edd_priority()
    inserted = 0
    for _ in range(400):
        rng.shuffle(priority)
        decoded = decode(c, priority, c.machine_genes(), c.wait_genes(), keep=True)
        order_on_machine = [unit[0] for unit in decoded.units[0]]
        inserted += order_on_machine != [c.stages[0][g] for g in priority]
        assert_valid(problem, Schedule("anneal", "feasible", tuple(assignments_of(c, decoded))))
    assert inserted > 300, "the test needs gap insertions to mean anything"


def test_decoder_places_the_setup_behind_a_downtime_before_any_retime():
    machine = MachineInfo("OF1", AGE, 6, True, initial_setup_class="T66", downtimes=(Downtime(0, 75),))
    op = Op("L1:age", AGE, "L1", "A", {"OF1": 180}, "T5", size=2, batch_family="T5")
    c = Compiled(Problem(0, (op,), (machine,), (OrderInfo("A", NO_RUSH),)))
    decoded = decode(c, c.edd_priority(), c.machine_genes(), c.wait_genes(), keep=True)
    assert decoded.starts[0] == [105]


def test_edd_takes_lots_by_due_date_whatever_their_ids():
    ops = tuple(Op(f"{x}:saw", SAW, x, x.upper(), {"SAE": 30}, "L6000") for x in ("p", "q", "r"))
    machine = MachineInfo("SAE", SAW, initial_setup_class="L6000")
    problem = Problem(0, ops, (machine,), (OrderInfo("P", 500), OrderInfo("Q", 100), OrderInfo("R", 300)))
    items = by_op(decode_edd(problem))
    assert (items["q:saw"].start, items["r:saw"].start, items["p:saw"].start) == (0, 30, 60)


def test_edd_takes_the_machine_that_finishes_first():
    """FR-60 on PR1 ends at 61. On PR2 it would need 55 of setup and 70 of work."""
    problem = build_problem(state_at(), [lot("L-1")], [order()]).problem
    schedule = decode_edd(problem)
    assert by_op(schedule)["L-1:press"].machine == "PR1"
    # And of two furnaces finishing together, the one needing no setup: the plain optimum, 428 with no setups at all.
    assert by_op(schedule)["L-1:age"].machine == "OF1"
    assert compute_metrics(problem, schedule).objective == 428


def test_annealing_can_overrule_the_earliest_finish_machine_choice():
    """Two T6 lots ready 300 apart, the first due at 360 so that it cannot wait for the second.

    Earliest finish puts the second on OF1: setup from 300, end 690, but T66 to T6 is priced. 690 + 2 * 30 = 750.
    OF2 has T6 mounted and is busy only until 360: end 720, no setup. 720 is better, and only a machine gene gets there.
    """
    ops = (
        Op("a:age", AGE, "a", "A", {"OF1": 360, "OF2": 360}, "T6", size=2, batch_family="T6"),
        Op("b:age", AGE, "b", "B", {"OF1": 360, "OF2": 360}, "T6", release=300, size=2, batch_family="T6"),
    )
    machines = (
        MachineInfo("OF1", AGE, 6, True, initial_setup_class="T66"),
        MachineInfo("OF2", AGE, 10, True, initial_setup_class="T6"),
    )
    problem = Problem(0, ops, machines, (OrderInfo("A", 360), OrderInfo("B", NO_RUSH)))
    assert compute_metrics(problem, decode_edd(problem)).objective == 750
    annealed = solve_anneal(problem, time_limit_s=30, seed=1, max_iterations=2000)
    assert compute_metrics(problem, annealed).objective == 720
    assert solve_cpsat(problem, time_limit_s=10, workers=4).best_bound == 720


def test_decoder_is_valid_for_arbitrary_genes_with_tight_batching():
    """Only OF1, all one recipe: nearly every decode has joins that shift loads, the riskiest thing the decoder does."""
    ops = tuple(
        Op(f"L{i}:age", AGE, f"L{i}", "A", {"OF1": 300}, "T66", release=i * 50, size=2, batch_family="T66")
        for i in range(12)
    )
    # Where a latecomer delaying a load would run into the downtime.
    machine = MachineInfo("OF1", AGE, 6, True, initial_setup_class="T66", downtimes=(Downtime(650, 700),))
    problem = Problem(0, ops, (machine,), (OrderInfo("A", NO_RUSH),))
    c = Compiled(problem)
    rng = random.Random(5)
    priority = c.edd_priority()
    joined = 0
    for _ in range(300):
        rng.shuffle(priority)
        wait = [rng.choice(WAIT_GENES) for _ in range(c.n_groups)]
        decoded = decode(c, priority, c.machine_genes(), wait, keep=True)
        joined += sum(len(unit) > 1 for units in decoded.units for unit in units)
        raw = Schedule("anneal", "feasible", tuple(assignments_of(c, decoded)))
        assert_valid(problem, raw)
    assert joined > 300


def test_annealing_is_never_worse_than_the_edd_it_starts_from():
    lots, orders = realistic_lots(target=12, seed=6)
    problem = build_problem(state_at(), lots, orders).problem
    edd = compute_metrics(problem, decode_edd(problem)).objective
    for seed in (1, 2, 3):
        schedule = solve_anneal(problem, time_limit_s=10, seed=seed, max_iterations=1500)
        assert_valid(problem, schedule)
        assert compute_metrics(problem, schedule).objective <= edd


def test_annealing_improves_on_edd_when_there_is_something_to_gain():
    lots, orders = realistic_lots(target=20, seed=7)
    problem = build_problem(state_at(), lots, orders).problem
    edd = compute_metrics(problem, decode_edd(problem)).objective
    schedule = solve_anneal(problem, time_limit_s=30, seed=1, max_iterations=4000)
    assert compute_metrics(problem, schedule).objective < edd


def test_same_seed_and_iterations_repeat_exactly():
    lots, orders = realistic_lots(target=10, seed=8)
    problem = build_problem(state_at(), lots, orders).problem
    a = solve_anneal(problem, time_limit_s=30, seed=11, max_iterations=800)
    b = solve_anneal(problem, time_limit_s=30, seed=11, max_iterations=800)
    assert a.iterations == b.iterations == 800
    assert a.assignments == b.assignments


def test_different_seeds_search_differently():
    lots, orders = realistic_lots(target=10, seed=8)
    problem = build_problem(state_at(), lots, orders).problem
    results = {solve_anneal(problem, time_limit_s=30, seed=seed, max_iterations=800).assignments for seed in range(1, 6)}
    assert len(results) > 1


def test_time_limit_ends_the_run():
    lots, orders = realistic_lots(target=25)
    problem = build_problem(state_at(), lots, orders).problem
    schedule = solve_anneal(problem, time_limit_s=0.3, seed=1)
    # Loose on purpose: the machine is shared. It must stop near the limit, not run to some fixed count.
    assert schedule.solve_seconds < 3.0
    assert schedule.iterations >= 1
    assert schedule.status == "feasible"
    assert_valid(problem, schedule)


def test_decode_rate_allows_tens_of_thousands_in_ten_seconds():
    """Plan requirement. A slow bound for a shared machine; the measured rate is in the build report."""
    lots, orders = realistic_lots(target=25)
    problem = build_problem(state_at(), lots, orders).problem
    schedule = solve_anneal(problem, time_limit_s=2.0, seed=1)
    assert schedule.iterations / schedule.solve_seconds >= 500


def test_nothing_to_plan_returns_the_frozen_ops():
    frozen = Frozen("SAE", 10, 120)
    op = Op("f:saw", SAW, "f", "A", {"SAE": 30}, "L6000", frozen=frozen)
    problem = Problem(100, (op,), (MachineInfo("SAE", SAW, initial_setup_class="L6000"),), (OrderInfo("A", NO_RUSH),))
    schedule = solve_anneal(problem, time_limit_s=1)
    assert by_op(schedule)["f:saw"].end == 120
    assert schedule.iterations == 0
    assert validate_schedule(problem, schedule) == []


def test_no_eligible_machine_is_no_solution():
    op = Op("a:saw", SAW, "a", "A", {"SAE": 30}, "L6000")
    problem = Problem(0, (op,), (MachineInfo("VP", "pack"),), (OrderInfo("A", NO_RUSH),))
    assert solve_anneal(problem, time_limit_s=1).status == "no_solution"
    edd = decode_edd(problem)
    assert edd.status == "no_solution"
    assert validate_schedule(problem, edd)[0].rule == "unassigned"


def test_eligibility_and_capacity_hold_under_annealing():
    lots = [lot(f"L-{i:02d}", "ST-200" if i % 3 == 0 else "FR-60", 8) for i in range(1, 13)]
    store = {"6060": 200, "6063": 0, "6082": 200}
    problem = build_problem(state_at(store=store), lots, [order()]).problem
    schedule = solve_anneal(problem, time_limit_s=30, seed=3, max_iterations=1500)
    assert_valid(problem, schedule)
    items = by_op(schedule)
    for i in range(3, 13, 3):
        assert items[f"L-{i:02d}:press"].machine == "PR2"
    load_size: dict[tuple[str, str], int] = {}
    capacity = {"OF1": 6, "OF2": 10}
    for a in schedule.assignments:
        if problem.op(a.op_id).kind == AGE:
            key = (a.machine, a.batch_id)
            load_size[key] = load_size.get(key, 0) + problem.op(a.op_id).size
    assert load_size
    assert all(size <= capacity[machine] for (machine, _), size in load_size.items())
    assert any(size >= 4 for size in load_size.values()), "batching should happen at all"


def test_reheats_stay_within_the_planned_cycles_when_the_machine_speeds_up(monkeypatch):
    """Each cycle plans from the rate so far. With the rate rising every cycle ends early, which once meant dozens of
    tiny cycles and, without a floor on the cooling rate, a temperature that could reach zero."""
    calls = itertools.count()
    monkeypatch.setattr(anneal, "perf_counter", lambda: 0.01 * next(calls) ** 0.5)
    schedule = solve_anneal(wait_gene_problem(), time_limit_s=0.3, seed=1)
    assert_valid(wait_gene_problem(), schedule)
    reheats = int(schedule.note.split(", ")[1].split()[0])
    assert 1 <= reheats <= anneal._CYCLES - 1


def test_annealing_starts_from_the_best_rule_and_keeps_it_if_nothing_beats_it():
    lots, orders = realistic_lots(target=12, seed=6)
    problem = build_problem(state_at(), lots, orders).problem
    rule = compute_metrics(problem, decode_best_rule(problem)).objective
    for iterations in (1, 200):
        schedule = solve_anneal(problem, time_limit_s=10, seed=1, max_iterations=iterations)
        assert_valid(problem, schedule)
        assert compute_metrics(problem, schedule).objective <= rule
