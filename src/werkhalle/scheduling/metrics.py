"""KPIs of a schedule. The objective here is the one both solvers optimise; they never compute their own."""
from __future__ import annotations

from .model import Metrics, Problem, Schedule


def compute_metrics(problem: Problem, schedule: Schedule) -> Metrics:
    latest_end = problem.now
    setups = 0
    setup_total = 0
    completion: dict[str, int] = {
        order.id: order.completed_lots_end for order in problem.orders if order.completed_lots_end is not None
    }
    for assignment in schedule.assignments:
        if not problem.has_op(assignment.op_id):
            continue
        latest_end = max(latest_end, assignment.end)
        if assignment.setup_before > 0:
            setups += 1
            setup_total += assignment.setup_before
        order_id = problem.op(assignment.op_id).order_id
        if order_id is not None:
            completion[order_id] = max(completion.get(order_id, assignment.end), assignment.end)

    total_tardiness = weighted_tardiness = late_orders = max_tardiness = 0
    for order in problem.orders:
        end = completion.get(order.id)
        if end is None:
            continue
        tardiness = max(0, end - order.due)
        if tardiness:
            late_orders += 1
            total_tardiness += tardiness
            weighted_tardiness += order.weight * tardiness
            max_tardiness = max(max_tardiness, tardiness)

    makespan = latest_end - problem.now
    weights = problem.weights
    objective = weights.tardiness * weighted_tardiness + weights.makespan * makespan + weights.setup * setup_total
    return Metrics(
        makespan=makespan,
        total_tardiness=total_tardiness,
        weighted_tardiness=weighted_tardiness,
        late_orders=late_orders,
        max_tardiness=max_tardiness,
        setups=setups,
        setup_minutes=setup_total,
        objective=objective,
        order_completion=completion,
    )
