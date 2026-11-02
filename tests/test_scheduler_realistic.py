"""Both solvers on one realistic instance, at the size the auto-supply keeps the plant at."""
from __future__ import annotations

from dataclasses import replace

import pytest
from test_scheduler_support import assert_valid, by_op, realistic_lots, replay_billets, state_at

from werkhalle.domain.catalog import AGE
from werkhalle.mes.seed import seed_orders
from werkhalle.scheduling import (
    Downtime,
    OrderInfo,
    build_problem,
    compute_metrics,
    decode_edd,
    retime,
    solve_anneal,
    solve_cpsat,
)
from werkhalle.scheduling.decoder import decode_best_rule


@pytest.fixture(scope="module")
def instance():
    lots, orders = realistic_lots(target=25, seed=7)
    assert len(lots) == 25
    result = build_problem(state_at(), lots, orders)
    return result.problem, result


def _check_plant_rules(problem, schedule) -> None:
    assert_valid(problem, schedule)
    assert all(low >= 0 for low in replay_billets(problem, schedule).values())
    items = by_op(schedule)
    capacity = {m.id: m.capacity for m in problem.machines}
    loads: dict[str, list] = {}
    for op in problem.ops:
        a = items[op.id]
        assert a.machine in op.durations
        if op.kind == AGE:
            loads.setdefault(a.batch_id, []).append((op, a))
    for members in loads.values():
        assert len({op.batch_family for op, _ in members}) == 1
        assert len({(a.machine, a.start, a.end) for _, a in members}) == 1
        assert sum(op.size for op, _ in members) <= capacity[members[0][1].machine]
    assert any(len(members) > 1 for members in loads.values()), "a plan this busy should batch"


def test_the_instance_needs_melts(instance):
    problem, result = instance
    assert sum(op.kind == "cast" for op in problem.ops) >= 3
    assert result.melt_counter >= 4


def test_cpsat_with_hint_is_at_least_as_good_as_the_hint(instance):
    problem, _ = instance
    hint_objective = compute_metrics(problem, decode_edd(problem)).objective
    schedule = solve_cpsat(problem, time_limit_s=3, seed=1, workers=8)
    assert schedule.status in ("optimal", "feasible")
    _check_plant_rules(problem, schedule)
    metrics = compute_metrics(problem, schedule)
    assert metrics.objective <= hint_objective
    # No bound only if CP-SAT found nothing of its own in time, and then it must be the hint that came back.
    assert schedule.best_bound <= metrics.objective if schedule.best_bound is not None else metrics.objective == hint_objective
    print(f"cpsat {metrics.objective} vs edd {hint_objective}, bound {schedule.best_bound}, {schedule.solve_seconds:.2f} s")


def test_annealing_is_valid_and_at_least_as_good_as_edd(instance):
    problem, _ = instance
    edd = compute_metrics(problem, decode_edd(problem)).objective
    schedule = solve_anneal(problem, time_limit_s=3, seed=1)
    _check_plant_rules(problem, schedule)
    metrics = compute_metrics(problem, schedule)
    assert metrics.objective <= edd
    print(f"anneal {metrics.objective} vs edd {edd}, {schedule.iterations} decodes in {schedule.solve_seconds:.2f} s")


def test_a_problem_survives_pickling_for_the_process_pools(instance):
    """Plans go through spawn executors, so problem and schedule must both round-trip."""
    import pickle

    problem, _ = instance
    schedule = decode_edd(problem)
    again = pickle.loads(pickle.dumps(problem))
    assert again.op(problem.ops[0].id) == problem.ops[0]
    assert pickle.loads(pickle.dumps(schedule)) == schedule


def _seed_book_problem(seed: int, state=None):
    book = seed_orders(seed)
    lots = [the_lot for s in book for the_lot in s.lots]
    orders = [OrderInfo(s.order.id, int(s.order.due_min), s.order.weight) for s in book]
    return build_problem(state or state_at(), lots, orders).problem


@pytest.mark.parametrize("seed", [1, 7, 42])
def test_the_rule_start_plan_beats_edd_and_stays_valid(seed):
    problem = _seed_book_problem(seed)
    rule = decode_best_rule(problem)
    assert_valid(problem, rule)
    assert compute_metrics(problem, rule).objective < compute_metrics(problem, decode_edd(problem)).objective


@pytest.mark.parametrize("seed", [1, 7, 42])
def test_the_rule_start_plan_copes_with_a_press_breakdown(seed):
    problem = _seed_book_problem(seed)
    machines = tuple(
        replace(m, downtimes=(Downtime(0, 480),)) if m.id == "PR2" else m for m in problem.machines
    )
    broken = replace(problem, machines=machines)
    rule = decode_best_rule(broken)
    assert_valid(broken, rule)
    assert compute_metrics(broken, rule).objective <= compute_metrics(broken, decode_edd(broken)).objective


def test_a_good_plan_leaves_a_few_seed_orders_late_not_most():
    """PLAN asks for a few tight orders. Measured on eight seeds, better of both solvers at 8 s: 0 to 3 of 11, mean 2.25."""
    late = []
    for seed in (1, 2, 3, 7, 42, 100):
        problem = _seed_book_problem(seed)
        schedule = solve_anneal(problem, time_limit_s=30, seed=1, max_iterations=2000)
        assert_valid(problem, schedule)
        late.append(compute_metrics(problem, retime(problem, schedule)).late_orders)
    assert max(late) <= 5, late
    assert 1 <= sum(late) / len(late) <= 4, late
