"""Candidate plans: build one problem, run the solvers, then retime, validate and score each result the same way.

Pure, and safe in a spawn pool: everything here is a top-level function on plain data, and nothing imports asyncua, fastapi or
the plant. `make_candidates` does it all in one call. The MES can instead run `prepare_problem`, `solve_candidate` per solver in
its own worker, and `finalize_candidates`.
"""
from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from time import perf_counter

from ..domain.catalog import CAST
from ..domain.orders import Lot, lot_from_dict
from ..domain.state import PlanningState
from ..scheduling import (
    Assignment,
    BilletSupply,
    BuildResult,
    Downtime,
    Frozen,
    MachineInfo,
    Metrics,
    Op,
    OrderInfo,
    Plan,
    Pred,
    Problem,
    Schedule,
    Violation,
    Weights,
    build_problem,
    compute_metrics,
    decode_edd,
    keep_schedule,
    retime,
    solve_anneal,
    solve_cpsat,
    validate_schedule,
)

SOLVER_LABELS = {"cpsat": "CP-SAT", "anneal": "Simulated Annealing", "keep": "Aktueller Plan", "edd": "EDD"}
_CAST_SUFFIX = f":{CAST}"


@dataclass(frozen=True)
class Candidate:
    """One solver's result, already retimed, validated and scored. `plan` has no assignments if the solver found nothing."""

    plan: Plan
    metrics: Metrics | None
    violations: tuple[Violation, ...]
    status: str
    solve_seconds: float
    note: str = ""

    @property
    def solver(self) -> str:
        return self.plan.schedule.solver

    @property
    def valid(self) -> bool:
        return self.status != "no_solution" and not self.violations


@dataclass(frozen=True)
class CandidateSet:
    problem: Problem
    melt_counter: int  # next free melt number; only the set that gets applied may advance the MES's counter
    candidates: dict[str, Candidate]
    notes: tuple[str, ...] = ()
    lots: tuple[Lot, ...] = field(default=())


def pending_melts_of(plan: Plan | None, state: PlanningState) -> list[str]:
    """Melts of the plan not started in `state`, in chain order, which is the order the builder lists them in."""
    if plan is None:
        return []
    return [
        op.id[: -len(_CAST_SUFFIX)]
        for op in plan.problem.ops
        if op.kind == CAST and op.id not in state.started_op_ids
    ]


def prepare_problem(
    state: PlanningState,
    open_lots: Sequence[Lot],
    orders: Sequence[OrderInfo],
    active_plan: Plan | None,
    *,
    revision: int,
    melt_counter: int,
    downtimes: Mapping[str, Sequence[Downtime]] = {},
    slowdowns: Sequence[dict] = (),
    extra_melts: Sequence[str] = (),
    batch_prefix: str | None = None,
    weights: Weights = Weights(),
) -> BuildResult:
    """The problem for plan `revision`, reusing the active plan's pending melts. `extra_melts` go behind them in the chain."""
    return build_problem(
        state,
        open_lots,
        orders,
        pending_melts=[*pending_melts_of(active_plan, state), *extra_melts],
        melt_counter=melt_counter,
        downtimes=downtimes,
        slowdowns=slowdowns,
        batch_prefix=batch_prefix if batch_prefix is not None else f"B-r{revision}",
        weights=weights,
    )


def solve_candidate(
    problem: Problem,
    solver: str,
    limit: float = 8.0,
    seed: int = 1,
    hint: Schedule | None = None,
    *,
    workers: int = 8,
) -> Schedule:
    """Run one solver. Never raises: a failure is a `no_solution` schedule that says why, so the others stay usable.

    `solver` is cpsat, anneal, keep or edd. `hint` is a schedule of any problem: a warm start for cpsat, the plan to carry over
    for keep. Annealing ignores it and starts from the best rule decode.
    """
    started = perf_counter()
    try:
        if solver == "cpsat":
            return solve_cpsat(problem, time_limit_s=limit, seed=seed, workers=workers, hint=hint)
        if solver == "anneal":
            return solve_anneal(problem, time_limit_s=limit, seed=seed)
        if solver == "keep":
            if hint is None:
                raise ValueError("kein bisheriger Plan")
            return keep_schedule(problem, hint)
        if solver == "edd":
            schedule = decode_edd(problem)
            return Schedule(schedule.solver, schedule.status, schedule.assignments, perf_counter() - started, note=schedule.note)
        raise ValueError(f"unbekannter Solver {solver}")
    except Exception as error:  # noqa: BLE001 - whatever a solver does, the other candidates must survive it
        return Schedule(
            solver=solver,
            status="no_solution",
            assignments=(),
            solve_seconds=perf_counter() - started,
            note=f"{SOLVER_LABELS.get(solver, solver)} fehlgeschlagen: {error}",
        )


def _judged(problem: Problem, plan_of: Callable[[Schedule], Plan], schedule: Schedule) -> Candidate:
    """Retime, then validate and score the retimed plan, because times the dispatcher would not keep are fiction."""
    try:
        timed = retime(problem, schedule)
    except Exception as error:  # noqa: BLE001 - a broken result counts as no result, not as a crash of the whole set
        timed = Schedule(
            solver=schedule.solver,
            status="no_solution",
            assignments=(),
            solve_seconds=schedule.solve_seconds,
            note=f"Nachterminierung fehlgeschlagen: {error}",
        )
    if timed.status == "no_solution":
        return Candidate(plan_of(timed), None, (), "no_solution", timed.solve_seconds, timed.note)
    return Candidate(
        plan_of(timed),
        compute_metrics(problem, timed),
        tuple(validate_schedule(problem, timed)),
        timed.status,
        timed.solve_seconds,
        timed.note,
    )


def finalize_candidates(
    build: BuildResult,
    schedules: Mapping[str, Schedule],
    *,
    revision: int,
    lots: Iterable[Lot],
    created_min: float | None = None,
    source: str = "manual",
    source_proposal_id: str | None = None,
) -> CandidateSet:
    """Assemble what the solvers returned. Every schedule is retimed, validated and scored against `build.problem`."""
    lots = tuple(lots)
    problem = build.problem

    def plan_of(schedule: Schedule) -> Plan:
        return Plan(
            revision=revision,
            problem=problem,
            schedule=schedule,
            lots=lots,
            created_min=problem.now if created_min is None else created_min,
            source=source,
            source_proposal_id=source_proposal_id,
        )

    candidates = {name: _judged(problem, plan_of, schedule) for name, schedule in schedules.items()}
    return CandidateSet(problem, build.melt_counter, candidates, build.notes, lots)


def solve_all(
    problem: Problem,
    active_schedule: Schedule | None,
    *,
    limit: float,
    seed: int = 1,
    workers: int = 8,
    hint: Schedule | None = None,
) -> dict[str, Schedule]:
    """cpsat and anneal side by side on threads, as CP-SAT spends its time outside the GIL, and keep if there is a plan to keep.

    The cpsat hint defaults to the active plan. `solve_cpsat` takes the better of that and its own EDD decode as its start, so
    it can only be better than either, also when there is no plan and the EDD decode is all it has.
    """
    start_from = hint if hint is not None else active_schedule
    # Loaded here, not by the solver's thread: next to a running annealer the import took 4 s instead of under 1 s, ahead of CP-SAT's own limit.
    try:
        from ortools.sat.python import cp_model  # noqa: F401
    except ImportError:
        pass  # solve_candidate turns this into a failed candidate, and the others go on
    with ThreadPoolExecutor(max_workers=2, thread_name_prefix="solver") as threads:
        cpsat = threads.submit(solve_candidate, problem, "cpsat", limit, seed, start_from, workers=workers)
        anneal = threads.submit(solve_candidate, problem, "anneal", limit, seed)
        schedules = {"cpsat": cpsat.result(), "anneal": anneal.result()}
    if active_schedule is not None:
        schedules["keep"] = solve_candidate(problem, "keep", 0.0, seed, active_schedule)
    return schedules


def make_candidates(
    state: PlanningState,
    open_lots: Sequence[Lot],
    orders: Sequence[OrderInfo],
    active_plan: Plan | None,
    *,
    revision: int,
    melt_counter: int,
    downtimes: Mapping[str, Sequence[Downtime]] = {},
    slowdowns: Sequence[dict] = (),
    solver_limit_s: float = 8.0,
    seed: int = 1,
    workers: int = 8,
    source: str = "manual",
) -> CandidateSet:
    """All candidates for plan `revision` from one consistent state. `keep` is there only if there is an active plan."""
    build = prepare_problem(
        state,
        open_lots,
        orders,
        active_plan,
        revision=revision,
        melt_counter=melt_counter,
        downtimes=downtimes,
        slowdowns=slowdowns,
    )
    schedules = solve_all(
        build.problem,
        active_plan.schedule if active_plan is not None else None,
        limit=solver_limit_s,
        seed=seed,
        workers=workers,
    )
    return finalize_candidates(build, schedules, revision=revision, lots=open_lots, created_min=state.now, source=source)


# ---------------------------------------------------------------------- JSON of docs/API.md


def assignment_json(plan: Plan, assignment: Assignment, state: str | None = None) -> dict:
    op = plan.problem.op(assignment.op_id)
    lot = plan.lots_by_id.get(op.lot_id) if op.lot_id is not None else None
    return {
        "op_id": assignment.op_id,
        "kind": op.kind,
        "machine": assignment.machine,
        "start": assignment.start,
        "end": assignment.end,
        "setup_before": assignment.setup_before,
        "batch_id": assignment.batch_id,
        "lot_id": op.lot_id,
        "order_id": op.order_id,
        "product_id": lot.product_id if lot is not None else None,
        "state": state if state is not None else ("running" if op.frozen is not None else "pending"),
        "frozen": op.frozen is not None,
    }


def assignments_json(plan: Plan, op_state: Callable[[str], str] | None = None) -> list[dict]:
    """The plan's rows. `op_state` is `tracker.op_state`; without it a frozen op counts as running and the rest as pending."""
    rows = []
    for assignment in plan.schedule.assignments:
        if plan.problem.has_op(assignment.op_id):
            rows.append(assignment_json(plan, assignment, op_state(assignment.op_id) if op_state else None))
    return rows


def downtimes_json(problem: Problem) -> list[dict]:
    return [
        {"machine": machine.id, "start": downtime.start, "end": downtime.end, "reason": downtime.reason}
        for machine in problem.machines
        for downtime in machine.downtimes
    ]


def violation_json(violation: Violation) -> dict:
    return {"rule": violation.rule, "op_ids": list(violation.op_ids), "message": violation.message}


def metrics_json(metrics: Metrics | None) -> dict | None:
    return metrics.to_dict() if metrics is not None else None


def candidate_json(candidate: Candidate, twin: Mapping | None = None) -> dict:
    """The `Candidate` of docs/API.md. `twin` comes from `TwinRun.twin_json()`."""
    schedule = candidate.plan.schedule
    return {
        "solver": schedule.solver,
        "label": SOLVER_LABELS.get(schedule.solver, schedule.solver),
        "status": candidate.status,
        "valid": candidate.valid,
        "violations": [violation_json(v) for v in candidate.violations],
        "solve_seconds": round(candidate.solve_seconds, 3),
        "best_bound": schedule.best_bound,
        "iterations": schedule.iterations,
        "note": candidate.note,
        "metrics": metrics_json(candidate.metrics),
        "assignments": assignments_json(candidate.plan),
        "downtimes": downtimes_json(candidate.plan.problem),
        "twin": dict(twin) if twin is not None else None,
    }


# ---------------------------------------------------------------------- a plan as JSON, so that evaluate_* results stay plain data


def problem_to_json(problem: Problem) -> dict:
    return asdict(problem)


def problem_from_json(data: Mapping) -> Problem:
    def op_from(item: Mapping) -> Op:
        frozen = item["frozen"]
        return Op(
            id=item["id"],
            kind=item["kind"],
            lot_id=item["lot_id"],
            order_id=item["order_id"],
            durations=dict(item["durations"]),
            setup_class=item["setup_class"],
            preds=tuple(Pred(p["op_id"], p["lag"]) for p in item["preds"]),
            release=item["release"],
            size=item["size"],
            batch_family=item["batch_family"],
            frozen=Frozen(frozen["machine"], frozen["start"], frozen["end"], frozen["batch_id"]) if frozen else None,
            alloy=item["alloy"],
            billets_in=item["billets_in"],
            billets_out=item["billets_out"],
        )

    def machine_from(item: Mapping) -> MachineInfo:
        return MachineInfo(
            id=item["id"],
            kind=item["kind"],
            capacity=item["capacity"],
            is_batch=item["is_batch"],
            available_from=item["available_from"],
            initial_setup_class=item["initial_setup_class"],
            downtimes=tuple(Downtime(d["start"], d["end"], d["reason"]) for d in item["downtimes"]),
        )

    return Problem(
        now=data["now"],
        ops=tuple(op_from(item) for item in data["ops"]),
        machines=tuple(machine_from(item) for item in data["machines"]),
        orders=tuple(
            OrderInfo(o["id"], o["due"], o["weight"], o["completed_lots_end"]) for o in data["orders"]
        ),
        weights=Weights(**data["weights"]),
        billet_supply=tuple(BilletSupply(s["alloy"], s["count"], s["available_at"]) for s in data["billet_supply"]),
        batch_prefix=data["batch_prefix"],
    )


def plan_data_json(candidate_set: CandidateSet, revision: int, created_min: float) -> dict:
    """What `plan_from_result` needs besides a candidate's assignments. Not part of the API answer: strip it before sending."""
    return {
        "revision": revision,
        "created_min": created_min,
        "melt_counter": candidate_set.melt_counter,
        "problem": problem_to_json(candidate_set.problem),
        "lots": [{"id": lot.id, "order_id": lot.order_id, "product_id": lot.product_id, "n_billets": lot.n_billets}
                 for lot in candidate_set.lots],
    }


def schedule_from_candidate_json(candidate: Mapping) -> Schedule:
    return Schedule(
        solver=candidate["solver"],
        status=candidate["status"],
        assignments=tuple(
            Assignment(a["op_id"], a["machine"], a["start"], a["end"], a["setup_before"], a["batch_id"])
            for a in candidate["assignments"]
        ),
        solve_seconds=candidate["solve_seconds"],
        best_bound=candidate["best_bound"],
        iterations=candidate["iterations"],
        note=candidate["note"],
    )


def plan_from_result(
    result: Mapping,
    solver: str,
    *,
    revision: int | None = None,
    source: str = "proposal",
    source_proposal_id: str | None = None,
) -> Plan:
    """The `Plan` behind one candidate of an `evaluate_replan` result, for the accept path: pass it to `rebase`."""
    data = result["plan_data"]
    candidate = result["keep"] if solver == "keep" else result["candidates"][solver]
    return Plan(
        revision=data["revision"] if revision is None else revision,
        problem=problem_from_json(data["problem"]),
        schedule=schedule_from_candidate_json(candidate),
        lots=tuple(lot_from_dict(item) for item in data["lots"]),
        created_min=data["created_min"],
        source=source,
        source_proposal_id=source_proposal_id,
    )


__all__ = [
    "Candidate",
    "CandidateSet",
    "SOLVER_LABELS",
    "assignment_json",
    "assignments_json",
    "candidate_json",
    "downtimes_json",
    "finalize_candidates",
    "make_candidates",
    "metrics_json",
    "pending_melts_of",
    "plan_data_json",
    "plan_from_result",
    "prepare_problem",
    "problem_from_json",
    "problem_to_json",
    "schedule_from_candidate_json",
    "solve_all",
    "solve_candidate",
    "violation_json",
]
