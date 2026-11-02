"""Shared by the execution, planning and twin tests: a closed loop without OPC UA, and plans that really occur.

Holds no tests of its own. It lives under this name so that every file of this group matches test_execution*.py.
"""
from __future__ import annotations

from dataclasses import dataclass

from werkhalle.domain import catalog
from werkhalle.domain.catalog import MACHINE_ORDER
from werkhalle.domain.orders import Lot
from werkhalle.execution import Tracker, next_commands, plan_finished, stalled
from werkhalle.scheduling import Assignment, OrderInfo, Plan, Schedule, build_problem, decode_edd, keep_schedule
from werkhalle.simcore import SimEngine

RUN_ID = "r-exec"
NO_RUSH = 100_000


def engine_at_start(seed: int = 1, stochastic: bool = False, random_failures: bool | None = None) -> SimEngine:
    failures = stochastic if random_failures is None else random_failures
    return SimEngine(seed=seed, stochastic=stochastic, random_failures=failures, run_id=RUN_ID)


def lot(lot_id: str, product: str = "FR-60", n_billets: int = 8, order: str = "A-1") -> Lot:
    return Lot(lot_id, order, product, n_billets)


def order(order_id: str = "A-1", due: int = NO_RUSH, weight: int = 1) -> OrderInfo:
    return OrderInfo(order_id, due, weight)


def plan_from(engine: SimEngine, lots, orders, solve=decode_edd, revision: int = 1, previous: Plan | None = None) -> Plan:
    """A plan built from what the engine says now, as the MES would build it."""
    state = engine.planning_state()
    pending = [] if previous is None else [
        op.id[: -len(":cast")] for op in previous.problem.ops if op.kind == "cast" and op.id not in state.started_op_ids
    ]
    result = build_problem(state, lots, orders, pending_melts=pending, batch_prefix=f"B-r{revision}")
    schedule = solve(result.problem) if previous is None else keep_schedule(result.problem, previous.schedule)
    return Plan(revision, result.problem, schedule, tuple(lots), created_min=engine.t)


def hand_plan(
    engine: SimEngine,
    lots,
    orders,
    sequence: dict[str, list[list[str]]],
    *,
    drop_ops: tuple[str, ...] = (),
) -> Plan:
    """A plan with exactly the given machine sequences: {machine: [[op, ...] per unit]}. Times just keep the order."""
    state = engine.planning_state()
    problem = build_problem(state, lots, orders, batch_prefix="B-t").problem
    assignments = []
    tick = 0
    batch = 0
    for machine, units in sequence.items():
        for unit in units:
            tick += 10
            batch_id = None
            if catalog.MACHINES[machine].kind == catalog.AGE:
                batch += 1
                batch_id = f"B-t-{batch}"
            for op_id in unit:
                assignments.append(Assignment(op_id, machine, tick, tick + 1, 0, batch_id))
    if drop_ops:
        problem = _without_ops(problem, drop_ops)
    return Plan(1, problem, Schedule("edd", "feasible", tuple(assignments)), tuple(lots), created_min=engine.t)


def _without_ops(problem, op_ids):
    from dataclasses import replace

    return replace(problem, ops=tuple(op for op in problem.ops if op.id not in op_ids))


def views_of(engine: SimEngine) -> dict[str, dict]:
    return {machine: engine.machine_view(machine) for machine in MACHINE_ORDER}


def sync(engine: SimEngine, tracker: Tracker) -> list[dict]:
    """Bring the tracker up to date. Returns the new events."""
    seen: list[dict] = []
    while True:
        events = engine.events_after(tracker.last_seq, 5000)["events"]
        if not events:
            return seen
        for event in events:
            tracker.apply(event)
        seen.extend(events)


@dataclass
class LoopResult:
    stalled: bool
    steps: int
    refusals: list[dict]


class ClosedLoop:
    """Engine, tracker and next_commands, wired directly. The test's own loop, independent of the twin's."""

    def __init__(self, engine: SimEngine, plan: Plan, run_id: str | None = None) -> None:
        self.engine = engine
        self.plan = plan
        self.run_id = engine.run_id if run_id is None else run_id
        self.tracker = Tracker.from_state(engine.planning_state())
        self.first_seq = self.tracker.last_seq
        self.events: list[dict] = []
        self.finished_at: dict[str, float] = {}
        self.commands: list[dict] = []
        self.refusals: list[dict] = []
        self.steps = 0

    def collect(self) -> None:
        for event in sync(self.engine, self.tracker):
            self.events.append(event)
            if event["type"] == "op_finished":
                for op_id in event["op_ids"]:
                    self.finished_at[op_id] = event["t"]

    def dispatch(self) -> list[dict]:
        self.collect()
        commands = next_commands(self.plan, self.tracker, views_of(self.engine), frozenset(), self.run_id)
        for command in commands:
            result = self.engine.start_operation(command)
            self.commands.append(command)
            if not result["ok"]:
                self.refusals.append({"command": command, **result})
        self.collect()
        return commands

    def done(self) -> bool:
        return plan_finished(self.plan, self.tracker)

    def is_stalled(self) -> bool:
        return stalled(self.plan, self.tracker, views_of(self.engine), False)

    def run(self, until: float | None = None, on_step=None, limit: int = 200_000) -> LoopResult:
        """Run to the end of the plan or to time `until`, whichever is first. Stops on a stall and reports it."""
        while self.steps < limit:
            self.dispatch()
            if self.done():
                return LoopResult(False, self.steps, self.refusals)
            when = self.engine.next_event_time()
            if when is None:
                return LoopResult(True, self.steps, self.refusals)
            if until is not None and when > until:
                self.engine.run_until(until)
                self.collect()
                return LoopResult(False, self.steps, self.refusals)
            self.engine.run_until(when)
            self.steps += 1
            if on_step is not None:
                on_step(self)
        raise AssertionError("does not finish")


def midrun(
    lots,
    orders,
    until: float,
    *,
    seed: int = 1,
    stochastic: bool = False,
    random_failures: bool | None = None,
    solve=decode_edd,
) -> tuple[SimEngine, Plan]:
    """The engine, running the plan, at time `until`."""
    engine = engine_at_start(seed, stochastic, random_failures)
    plan = plan_from(engine, lots, orders, solve)
    loop = ClosedLoop(engine, plan)
    assert not loop.run(until=until).stalled
    return engine, plan


def open_lots_of(lots, snapshot: dict) -> list[Lot]:
    finished = set(snapshot["finished_op_ids"])
    return [item for item in lots if item.op_id("pack") not in finished]
