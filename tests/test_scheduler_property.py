"""Random mid-run plant states. Whatever the state: build, solve, retime, validate must hold, and billets must balance.

PLAN section 5 asks for this on random mid-run states, not just t=0, because that is where a press could deadlock.
"""
from __future__ import annotations

import random
from dataclasses import replace

import pytest
from test_scheduler_support import assert_valid, by_op, random_midrun, replay_billets

from werkhalle.scheduling import (
    build_problem,
    compute_metrics,
    decode_edd,
    keep_schedule,
    retime,
    solve_anneal,
    solve_cpsat,
    validate_schedule,
)
from werkhalle.scheduling.decoder import AUTO, WAIT_GENES, Compiled, decode_schedule

SEEDS = range(60)
SOLVER_SEEDS = (3, 11, 17, 29)


def build(seed: int):
    instance = random_midrun(seed)
    result = build_problem(
        instance.state,
        instance.lots,
        instance.orders,
        pending_melts=instance.pending,
        melt_counter=instance.counter,
        downtimes=instance.downtimes,
        slowdowns=instance.slowdowns,
        batch_prefix=f"B-r{seed}",
    )
    return instance, result


def assert_all_good(problem, schedule) -> None:
    assert_valid(problem, schedule)
    assert all(low >= 0 for low in replay_billets(problem, schedule).values()), replay_billets(problem, schedule)
    items = by_op(schedule)
    frozen_loads = {op.frozen.batch_id for op in problem.ops if op.frozen and op.frozen.batch_id}
    for op in problem.ops:
        a = items[op.id]
        if op.frozen:
            assert (a.machine, a.start, a.end, a.batch_id, a.setup_before) == (
                op.frozen.machine,
                op.frozen.start,
                op.frozen.end,
                op.frozen.batch_id,
                0,
            )
        else:
            assert a.batch_id not in frozen_loads
            assert a.start >= problem.now
            setup = a.setup_before
            for downtime in problem.machine(a.machine).downtimes:
                assert a.end <= downtime.start or a.start - setup >= downtime.end, (a, downtime)


@pytest.mark.parametrize("seed", SEEDS)
def test_decode_on_a_random_state(seed):
    _, result = build(seed)
    problem = result.problem
    assert_all_good(problem, decode_edd(problem))


@pytest.mark.parametrize("seed", SEEDS)
def test_any_priority_order_stays_valid(seed):
    """The melt rules promise safety whatever order lots actually start in, so try orders that fight the due dates."""
    _, result = build(seed)
    problem = result.problem
    c = Compiled(problem)
    rng = random.Random(seed)
    priority = c.edd_priority()
    for _ in range(8):
        rng.shuffle(priority)
        genes = [rng.randrange(AUTO, len(options)) for options in c.options]
        wait = [rng.choice(WAIT_GENES) for _ in range(c.n_groups)]
        assert_all_good(problem, decode_schedule(c, priority, genes, wait, "anneal"))


@pytest.mark.parametrize("seed", SEEDS)
def test_retime_is_a_fixed_point_and_keep_schedule_is_valid(seed):
    instance, result = build(seed)
    problem = result.problem
    schedule = decode_edd(problem)
    assert retime(problem, schedule).assignments == schedule.assignments
    assert_all_good(problem, keep_schedule(problem, schedule))


@pytest.mark.parametrize("seed", SEEDS)
def test_keep_schedule_carries_a_plan_over_to_a_changed_set_of_lots(seed):
    instance, result = build(seed)
    rng = random.Random(seed)
    previous = decode_edd(result.problem)
    pending = [op.id[: -len(":cast")] for op in result.problem.ops if op.kind == "cast" and op.frozen is None]
    # The next rebuild has lost some not-yet-started lots.
    started = instance.state.started_op_ids
    lots = [
        lot for lot in instance.lots if lot.op_id("press") in started or rng.random() > 0.3
    ]
    next_build = build_problem(
        instance.state, lots, instance.orders, pending_melts=pending, melt_counter=result.melt_counter
    )
    assert_all_good(next_build.problem, keep_schedule(next_build.problem, previous))


@pytest.mark.parametrize("seed", SEEDS)
def test_rebuild_from_the_same_snapshot_keeps_melt_ids(seed):
    instance, result = build(seed)
    planned = [op.id for op in result.problem.ops if op.kind == "cast" and op.frozen is None]
    rebuilt = build_problem(
        instance.state,
        instance.lots,
        instance.orders,
        pending_melts=[op_id[: -len(":cast")] for op_id in planned],
        melt_counter=result.melt_counter,
    )
    assert [op.id for op in rebuilt.problem.ops if op.kind == "cast" and op.frozen is None] == planned
    assert rebuilt.melt_counter == result.melt_counter


@pytest.mark.parametrize("seed", SOLVER_SEEDS)
def test_both_solvers_on_a_random_state(seed):
    _, result = build(seed)
    problem = result.problem
    edd = compute_metrics(problem, decode_edd(problem)).objective

    exact = solve_cpsat(problem, time_limit_s=1.5, seed=seed, workers=4)
    assert exact.status in ("optimal", "feasible")
    assert_all_good(problem, exact)
    assert retime(problem, exact).assignments == exact.assignments
    assert compute_metrics(problem, exact).objective <= edd

    annealed = solve_anneal(problem, time_limit_s=1.5, seed=seed)
    assert_all_good(problem, annealed)
    assert compute_metrics(problem, annealed).objective <= edd


def test_the_generator_really_produces_hard_states():
    """If the instances were all easy, the tests above would prove nothing."""
    frozen_press = frozen_cast = frozen_load = pending_cast = shared_load = 0
    released_press = faulted = lots_travelling = melts_travelling = both_faulted = paused = 0
    for seed in SEEDS:
        instance, result = build(seed)
        problem = result.problem
        for op in problem.ops:
            if op.frozen:
                frozen_press += op.kind == "press"
                frozen_cast += op.kind == "cast"
                frozen_load += op.kind == "age"
            else:
                pending_cast += op.kind == "cast"
                released_press += op.kind == "press" and op.release > problem.now
        machines = instance.state.machines
        down = sum(m.status == "FAULT" for m in machines)
        faulted += down > 0
        both_faulted += down >= 2
        paused += any(m.status == "FAULT" and m.op_ids for m in machines)
        lots_travelling += bool(instance.state.lots_in_transit)
        melts_travelling += bool(instance.state.melts_in_transit)
        loads: dict[str, int] = {}
        for a in decode_edd(problem).assignments:
            if a.batch_id:
                loads[a.batch_id] = loads.get(a.batch_id, 0) + 1
        shared_load += any(count > 1 for count in loads.values())
    assert frozen_press >= 15
    assert frozen_cast >= 10
    assert frozen_load >= 15
    assert pending_cast >= 30
    assert released_press >= 5
    assert faulted >= 30
    assert both_faulted >= 15
    assert paused >= 15
    assert lots_travelling >= 30
    assert melts_travelling >= 15
    assert shared_load >= 20


def test_without_the_melt_rules_the_replay_catches_a_shortage():
    """Remove what the builder adds and the same schedules run out of billets: so billet_balance is not vacuous."""
    caught = 0
    for seed in SEEDS:
        _, result = build(seed)
        problem = result.problem
        if not any(op.kind == "cast" and op.frozen is None for op in problem.ops):
            continue
        cast_ids = {op.id for op in problem.ops if op.kind == "cast"}
        loose = tuple(
            replace(op, preds=tuple(p for p in op.preds if p.op_id not in cast_ids), release=0)
            if op.kind == "press" and op.frozen is None
            else op
            for op in problem.ops
        )
        loose_problem = replace(problem, ops=loose)
        schedule = decode_edd(loose_problem)
        rules = {v.rule for v in validate_schedule(loose_problem, schedule)}
        assert rules <= {"billet_balance"}, rules
        caught += "billet_balance" in rules
        if "billet_balance" in rules:
            assert any(low < 0 for low in replay_billets(loose_problem, schedule).values())
    assert caught >= 10
