"""The scheduler against the real plant model: states that actually occur, and plans that actually run.

Uses a minimal head-of-line dispatcher of its own, so that these tests depend only on simcore's engine API. The real
one lives in `execution` and has its own closed-loop tests.
"""
from __future__ import annotations

import random

import pytest
from test_scheduler_support import assert_valid, by_op, realistic_lots, replay_billets

from werkhalle.domain import catalog
from werkhalle.scheduling import Plan, build_problem, compute_metrics, decode_edd, keep_schedule, solve_anneal, solve_cpsat
from werkhalle.simcore import SimEngine

RUN_ID = "r-sched"


class Loop:
    """Executes the current plan on an engine and can rebuild the problem from any state the engine reaches."""

    def __init__(self, seed: int, stochastic: bool, lots, orders) -> None:
        self.engine = SimEngine(seed=seed, stochastic=stochastic, random_failures=stochastic, run_id=RUN_ID)
        self.engine.run_until(0.0)
        self.lots = list(lots)
        self.lots_by_id = {lot.id: lot for lot in lots}
        self.orders = list(orders)
        self.counter = 1
        self.revision = 0
        self.plan: Plan | None = None
        self.seq = 0
        self.finished_at: dict[str, float] = {}

    def build(self):
        state = self.engine.planning_state()
        open_lots = [lot for lot in self.lots if lot.op_id("pack") not in state.finished_op_ids]
        pending: list[str] = []
        if self.plan is not None:
            pending = [
                op.id[: -len(":cast")]
                for op in self.plan.problem.ops
                if op.kind == "cast" and op.id not in state.started_op_ids
            ]
        result = build_problem(
            state, open_lots, self.orders, pending_melts=pending, melt_counter=self.counter, batch_prefix=f"B-r{self.revision + 1}"
        )
        return state, open_lots, result

    def adopt(self, solve) -> None:
        _, open_lots, result = self.build()
        schedule = solve(result.problem)
        assert_valid(result.problem, schedule)
        self.counter = result.melt_counter
        self.revision += 1
        self.plan = Plan(self.revision, result.problem, schedule, tuple(open_lots), created_min=self.engine.t)

    def _command(self, machine: str, op_ids: tuple[str, ...], batch_id: str | None) -> dict:
        first = self.plan.problem.op(op_ids[0])
        command = {"machine": machine, "run_id": RUN_ID, "op_ids": list(op_ids), "kind": first.kind}
        if first.kind == "cast":
            command.update(melt_plan_id=op_ids[0][: -len(":cast")], alloy=first.alloy)
        elif first.kind == "age":
            command.update(batch_id=batch_id, recipe=first.setup_class, lot_ids=[op_id.split(":")[0] for op_id in op_ids])
        elif first.kind == "press":
            lot = self.lots_by_id[first.lot_id]
            command.update(lot_id=lot.id, order_id=lot.order_id, product_id=lot.product_id, n_billets=lot.n_billets)
        else:
            command.update(lot_id=first.lot_id)
        return command

    def dispatch(self) -> None:
        snapshot = self.engine.export_snapshot()
        started = set(snapshot["started_op_ids"])
        finished = set(snapshot["finished_op_ids"])
        for machine, units in self.plan.sequences.items():
            if snapshot["machines"][machine]["status"] != "IDLE":
                continue
            for unit in units:
                waiting = [op_id for op_id in unit.op_ids if op_id not in started]
                if not waiting:
                    continue
                # Strict head of line: one look at the first unit not started yet.
                preds_done = all(
                    pred.op_id in finished for op_id in waiting for pred in self.plan.problem.op(op_id).preds
                )
                if preds_done:
                    result = self.engine.start_operation(self._command(machine, tuple(waiting), unit.batch_id))
                    if result["ok"]:
                        snapshot = self.engine.export_snapshot()
                        started = set(snapshot["started_op_ids"])
                        finished = set(snapshot["finished_op_ids"])
                    else:
                        # A lot or melt still in transit; anything else would be a bug worth seeing.
                        assert result["reason"] == "NO_MATERIAL", result
                break

    def collect_events(self) -> None:
        batch = self.engine.events_after(self.seq, 5000)
        for event in batch["events"]:
            self.seq = event["seq"]
            if event["type"] == "op_finished":
                for op_id in event["op_ids"]:
                    self.finished_at[op_id] = event["t"]

    def done(self) -> bool:
        finished = self.engine.export_snapshot()["finished_op_ids"]
        return all(lot.op_id("pack") in finished for lot in self.lots)

    def run(self, on_event=None, limit: int = 200_000) -> None:
        """Run until every lot is packed. Failing to get there with no event left is the stall the rules must prevent."""
        steps = 0
        while not self.done():
            self.dispatch()
            self.collect_events()
            when = self.engine.next_event_time()
            assert when is not None, f"stalled at {self.engine.t}"
            self.engine.run_until(when)
            steps += 1
            assert steps < limit, "does not finish"
            if on_event is not None:
                on_event(steps)
        self.collect_events()


def small_instance(seed: int, target: int = 12):
    return realistic_lots(target=target, seed=seed)


def check_everything_valid(loop: Loop) -> None:
    state, _, result = loop.build()
    problem = result.problem
    edd = decode_edd(problem)
    assert_valid(problem, edd)
    assert all(low >= 0 for low in replay_billets(problem, edd).values())
    assert_valid(problem, keep_schedule(problem, loop.plan.schedule))
    # Running ops are on the machines the engine says they are on.
    on_machines = {op_id: m.id for m in state.machines for op_id in m.op_ids if op_id not in state.finished_op_ids}
    frozen = {op.id: op.frozen.machine for op in problem.ops if op.frozen is not None}
    assert {k: v for k, v in on_machines.items() if k in frozen} == frozen


@pytest.mark.parametrize("seed", [1, 2, 3])
def test_every_state_of_a_noisy_run_with_faults_gives_valid_plans(seed):
    """Random failures and duration noise on. Every state is rebuilt, decoded, kept and validated."""
    lots, orders = small_instance(seed)
    loop = Loop(seed, True, lots, orders)
    loop.adopt(decode_edd)
    checked = 0

    def on_event(step: int) -> None:
        nonlocal checked
        check_everything_valid(loop)
        checked += 1

    loop.run(on_event)
    assert checked > 100
    assert len({op_id for op_id in loop.finished_at if op_id.endswith(":pack")}) == len(lots)


@pytest.mark.parametrize("seed", [4, 5])
def test_replanning_along_the_way_reaches_the_end_without_a_stall(seed):
    """Replanning mid-run with different solvers: the melt rules have to hold across rebuilds, or a press deadlocks."""
    lots, orders = small_instance(seed)
    loop = Loop(seed, True, lots, orders)
    loop.adopt(decode_edd)
    rng = random.Random(seed)

    def on_event(step: int) -> None:
        if step % 15 == 0:
            loop.adopt(decode_edd if rng.random() < 0.5 else (lambda p: solve_anneal(p, time_limit_s=10, seed=seed, max_iterations=60)))

    loop.run(on_event)
    assert loop.revision > 2


def test_two_machines_faulted_at_once_on_the_real_engine():
    lots, orders = small_instance(6)
    loop = Loop(6, False, lots, orders)
    loop.adopt(decode_edd)
    injected = False

    def on_event(step: int) -> None:
        nonlocal injected
        if not injected and loop.engine.t > 150:
            assert loop.engine.inject_fault("PR2", "P-204", 120.0)["ok"]
            assert loop.engine.inject_fault("OF1", "O-301", 200.0)["ok"]
            injected = True
            state, _, result = loop.build()
            problem = result.problem
            assert len(problem.machine("PR2").downtimes) == 1
            assert len(problem.machine("OF1").downtimes) == 1
            assert problem.machine("PR2").downtimes[0].reason == "P-204"
            assert problem.machine("OF1").downtimes[0].reason == "O-301"
            for solver in (
                decode_edd,
                lambda p: solve_cpsat(p, time_limit_s=1.5, workers=4),
                lambda p: solve_anneal(p, time_limit_s=1.0),
                lambda p: keep_schedule(p, loop.plan.schedule),
            ):
                schedule = solver(problem)
                assert_valid(problem, schedule)
                for a in schedule.assignments:
                    for machine in ("PR2", "OF1"):
                        down = problem.machine(machine).downtimes[0]
                        if a.machine == machine and problem.op(a.op_id).frozen is None:
                            assert a.start - a.setup_before >= down.end or a.end <= down.start
            loop.adopt(lambda p: solve_cpsat(p, time_limit_s=1.5, workers=4))

    loop.run(on_event)
    assert injected


@pytest.mark.parametrize(
    "solve",
    [decode_edd, lambda p: solve_anneal(p, time_limit_s=10, seed=1, max_iterations=800), lambda p: solve_cpsat(p, time_limit_s=2, workers=4)],
    ids=["edd", "anneal", "cpsat"],
)
def test_deterministic_engine_finishes_each_op_when_the_plan_says(solve):
    """stochastic=False takes exactly planned_minutes, so the plan's times have to come true, or they are fiction."""
    lots, orders = small_instance(7, target=15)
    loop = Loop(7, False, lots, orders)
    loop.adopt(solve)
    planned = by_op(loop.plan.schedule)
    loop.run()
    late = {
        op_id: (planned[op_id].end, at)
        for op_id, at in loop.finished_at.items()
        if op_id in planned and at > planned[op_id].end + 1e-6
    }
    assert not late, list(late.items())[:5]
    metrics = compute_metrics(loop.plan.problem, loop.plan.schedule)
    last = max(at for op_id, at in loop.finished_at.items() if op_id.endswith(":pack"))
    assert last <= metrics.makespan + 1e-6


def test_planned_durations_match_what_the_engine_takes():
    """Same catalogue functions on both sides: a mismatch here would make every plan drift."""
    for product_id in catalog.PRODUCTS:
        lots = [catalog_lot(product_id)]
        problem = build_problem(fresh_state(), lots, [order_info()]).problem
        schedule = decode_edd(problem)
        loop = Loop(1, False, lots, [order_info()])
        loop.adopt(decode_edd)
        loop.run()
        for op_id, a in by_op(schedule).items():
            assert loop.finished_at[op_id] == pytest.approx(a.end, abs=1e-6), (product_id, op_id)


def catalog_lot(product_id: str):
    from werkhalle.domain.orders import Lot

    return Lot("L-00001", "A-1", product_id, 8)


def order_info():
    from werkhalle.scheduling import OrderInfo

    return OrderInfo("A-1", 100_000)


def fresh_state():
    return SimEngine(seed=1, stochastic=False, random_failures=False, run_id=RUN_ID).planning_state()
