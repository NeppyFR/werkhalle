"""The digital twin: runs a plan on a fork of the plant, with the same simcore and the same dispatch as the live MES.

It starts only from the snapshot read over OPC UA and shares no memory with the plant. Pure, and safe in a spawn pool: top-level
functions on plain data, with JSON-serialisable answers from `evaluate_*`.
"""
from __future__ import annotations

import math
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from time import perf_counter

from ..domain import catalog
from ..domain.catalog import MACHINE_ORDER, MACHINES, PACK, PRODUCTS
from ..domain.orders import Lot, Order, split_order
from ..domain.state import FAULT_REMAINING_FLOOR_MIN
from ..execution import Tracker, next_commands, plan_finished, stalled
from ..scheduling import Downtime, OrderInfo, Plan, Schedule, decode_edd, keep_schedule, validate_schedule
from ..simcore import SimEngine, planning_state_from_snapshot
from .validation import MAX_MINUTES, MAX_SCENARIO_ITEMS, MAX_SLOWDOWN_FACTOR, is_minutes, is_number, order_fields, whole_quantity
from .planning import (
    Candidate,
    CandidateSet,
    assignment_json,
    candidate_json,
    finalize_candidates,
    make_candidates,
    plan_data_json,
    prepare_problem,
    schedule_from_candidate_json,
    solve_all,
)

# Everything that exists only inside a fork. So a twin id can never be taken for a real order, lot, melt or load.
Z_PREFIX = "Z-"

MAX_FRAMES = 600
RUSH_WEIGHT = 3
_NO_DUE = 1 << 60
_MIN_HORIZON_MIN = 2000.0
_EVENT_BATCH = 5000


_SCENARIO_KEYS = {
    "machine_down": ("type", "machine", "at_offset_min", "duration_min", "code"),
    "slowdown": ("type", "machine", "factor", "at_offset_min", "duration_min"),
    "rush_order": ("type", "product_id", "qty_bars", "due_in_min"),
}


def _whole(value: float) -> int:
    return int(math.ceil(value - 1e-9))


@dataclass
class TwinRun:
    """One plan run to its end on one fork. Completions and makespan are whole minutes, like those of a plan."""

    makespan: int
    tardiness: int  # total, minutes
    late_orders: int
    setups: int
    energy_kwh: float
    order_completion: dict[str, int]
    unfinished_orders: list[str]
    stalled: bool
    wall_seconds: float
    sim_minutes: float
    speedup: float
    timeline: list[dict] = field(default_factory=list)
    frames: list[dict] = field(default_factory=list)
    # Beyond the plan's list: the plan's objective with the twin's figures, and how the run ended.
    weighted_tardiness: int = 0
    setup_minutes: int = 0
    objective: int = 0
    horizon_hit: bool = False
    refusals: int = 0
    started_at: float = 0.0
    ended_at: float = 0.0
    finished_at: dict[str, float] = field(default_factory=dict)
    lots_packed: int = 0
    op_states: dict[str, str] = field(default_factory=dict)  # per op of the plan, at the end of the run

    def kpis(self) -> dict:
        return {
            "makespan": self.makespan,
            "total_tardiness": self.tardiness,
            "late_orders": self.late_orders,
            "setups": self.setups,
            "energy_kwh": self.energy_kwh,
            "unfinished_orders": list(self.unfinished_orders),
            "stalled": self.stalled,
            "objective": self.objective,
        }

    def twin_json(self) -> dict:
        """The `twin` block of a `Candidate`."""
        return {**self.kpis(), "speedup": int(self.speedup)}


def scenario_items(scenario: Sequence[Mapping]) -> list[dict]:
    """A checked copy of a scenario, with the known keys only. Raises ValueError on anything the twin could not honour."""
    if len(scenario) > MAX_SCENARIO_ITEMS:
        raise ValueError(f"Ein Szenario hat höchstens {MAX_SCENARIO_ITEMS} Einträge.")
    items: list[dict] = []
    for raw in scenario:
        if not isinstance(raw, Mapping):
            raise ValueError("Ein Szenarioeintrag muss ein Objekt sein.")
        kind = raw.get("type")
        if not isinstance(kind, str) or kind not in _SCENARIO_KEYS:
            raise ValueError(f"Unbekannter Szenariotyp {kind!r}.")
        item = {key: raw[key] for key in _SCENARIO_KEYS[kind] if key in raw}
        if kind in ("machine_down", "slowdown"):
            machine = item.get("machine")
            if not isinstance(machine, str) or machine not in MACHINES:
                raise ValueError(f"Unbekannte Maschine {machine!r}.")
            if "at_offset_min" in item and not is_minutes(item["at_offset_min"]):
                raise ValueError(f"Der Beginn muss eine Zahl von 0 bis {MAX_MINUTES:.0f} Minuten sein.")
            if not is_minutes(item.get("duration_min")) or item["duration_min"] <= 0:
                raise ValueError(f"Die Dauer muss über 0 und höchstens {MAX_MINUTES:.0f} Minuten sein.")
        if kind == "machine_down":
            code = item.get("code")
            if code is not None and not isinstance(code, str):
                raise ValueError("Der Störcode muss ein Text sein.")
            if code:
                try:
                    catalog.fault_spec(item["machine"], code)
                except KeyError:
                    raise ValueError(f"{item['machine']} kennt den Störcode {code} nicht.") from None
        elif kind == "slowdown":
            factor = item.get("factor")
            if not is_number(factor) or not 1 < factor <= MAX_SLOWDOWN_FACTOR:
                raise ValueError(f"Eine Verlangsamung braucht einen Faktor über 1 und bis {MAX_SLOWDOWN_FACTOR:.0f}.")
        else:
            product = item.get("product_id")
            if not isinstance(product, str) or product not in PRODUCTS:
                raise ValueError(f"Unbekanntes Produkt {product!r}.")
            quantity = whole_quantity(item.get("qty_bars"))
            if quantity is None:
                raise ValueError("Ein Eilauftrag braucht eine ganze Menge ab 1.")
            item["qty_bars"] = quantity
            if not is_number(item.get("due_in_min")) or item["due_in_min"] > MAX_MINUTES:
                raise ValueError("Ein Eilauftrag braucht einen Liefertermin.")
        items.append(item)
    return items


def api_scenario_items(scenario: Sequence[Mapping]) -> list[dict]:
    """`scenario_items` plus the rules of POST /api/orders for rush orders: they may become real orders on apply."""
    items = scenario_items(scenario)
    for item in items:
        if item["type"] == "rush_order":
            order_fields(item["product_id"], item["qty_bars"], item["due_in_min"])
    return items


def _install_scenario(engine: SimEngine, scenario: Sequence[Mapping], snapshot: Mapping) -> None:
    t0 = engine.t
    for item in scenario:
        kind = item["type"]
        if kind == "machine_down":
            machine = item["machine"]
            offset = float(item.get("at_offset_min", 0.0))
            duration = float(item["duration_min"])
            code = item.get("code", "")
            if offset > 0:
                engine.schedule_fault(machine, t0 + offset, code, duration)
                continue
            # Now, not on the next event: dispatch must already see it. A machine already down stays down for the longer of both.
            fault = snapshot["machines"][machine]["fault"]
            if fault is not None:
                remaining = max(fault["estimate_min"] - fault["elapsed_min"], FAULT_REMAINING_FLOOR_MIN)
                duration = max(duration, remaining)
                engine.repair(machine)
            engine.inject_fault(machine, code, duration)
        elif kind == "slowdown":
            engine.schedule_slowdown(
                item["machine"], float(item["factor"]), t0 + float(item.get("at_offset_min", 0.0)), float(item["duration_min"])
            )
        # A rush order is not a plant event: its lots are in the plan the variant runs.


def _planned_end(plan: Plan) -> int:
    return max((a.end for a in plan.schedule.assignments), default=plan.problem.now)


def default_horizon_min(plan: Plan, scenario: Sequence[Mapping] = (), snapshot: Mapping | None = None) -> float:
    """Long enough that only a plan that does not finish can run into it, even with noise and random faults.

    A plan older than the snapshot can have planned much too little, so one op after another of what it still holds counts as
    well, and so does every repair the plan may not know about.
    """
    span = 4.0 * (_planned_end(plan) - plan.problem.now)
    serial = sum(min(op.durations.values(), default=0) for op in plan.problem.ops)
    extra = sum(float(item.get("duration_min", 0.0)) for item in scenario)
    if snapshot is not None:
        for machine in snapshot["machines"].values():
            if machine["fault"]:
                extra += max(machine["fault"]["estimate_min"] - machine["fault"]["elapsed_min"], FAULT_REMAINING_FLOOR_MIN)
    return max(_MIN_HORIZON_MIN, span, float(serial)) + 2.0 * extra


def _frame(engine: SimEngine) -> dict:
    material = engine.material_view()
    in_machine = material["in_machine"]
    machines = {}
    for machine_id in MACHINE_ORDER:
        view = engine.machine_view(machine_id)
        machines[machine_id] = {
            "status": view["Status"],
            "progress": round(view["Progress"], 3),
            "lot_ids": list(in_machine.get(machine_id, {}).get("lot_ids", ())),
        }
    return {"t": round(engine.t, 2), "machines": machines, "material": material}


def run_closed_loop(
    snapshot: Mapping,
    plan: Plan,
    *,
    scenario: Sequence[Mapping] = (),
    seed: int = 1,
    stochastic: bool = False,
    horizon_min: float | None = None,
    record_frames: bool = False,
    frame_step_min: float = 5,
    random_failures: bool | None = None,
    record_timeline: bool = True,
) -> TwinRun:
    """Fork the plant and dispatch `plan` on it, event by event, until the plan is done, stalls or reaches the horizon.

    A deterministic fork takes each repair as estimated and each op as planned. `random_failures` defaults to `stochastic`.
    """
    wall_started = perf_counter()
    scenario = scenario_items(scenario)
    engine = SimEngine.from_snapshot(
        snapshot,
        seed=seed,
        stochastic=stochastic,
        random_failures=stochastic if random_failures is None else random_failures,
    )
    t0 = engine.t
    if record_timeline:
        engine.set_timeline_recording(True)
    energy_before = engine.total_energy_kwh()
    tracker = Tracker.from_state(planning_state_from_snapshot(snapshot))
    run_id = snapshot["run_id"]
    _install_scenario(engine, scenario, snapshot)

    horizon = default_horizon_min(plan, scenario, snapshot) if horizon_min is None else float(horizon_min)
    horizon_t = t0 + horizon
    finished_at: dict[str, float] = {}
    setups = 0
    setup_minutes = 0.0
    frames: list[dict] = []
    next_frame = t0
    frame_step = max(float(frame_step_min), 1e-6)
    refusals = 0
    is_stalled = False
    horizon_hit = False

    def drain() -> None:
        nonlocal setups, setup_minutes
        while True:
            events = engine.events_after(tracker.last_seq, _EVENT_BATCH)["events"]
            if not events:
                return
            for event in events:
                tracker.apply(event)
                kind = event["type"]
                if kind == "op_finished":
                    for op_id in event["op_ids"]:
                        finished_at[op_id] = event["t"]
                elif kind == "op_started" and event["setup_min"] > 0:
                    setups += 1
                    setup_minutes += event["setup_min"]

    def frames_before(when: float) -> None:
        nonlocal next_frame, frame_step, frames
        while record_frames and next_frame < when:
            # Nothing is due before `when`, so moving the clock here changes nothing about the run.
            engine.run_until(next_frame)
            frames.append(_frame(engine))
            next_frame += frame_step
            if len(frames) >= MAX_FRAMES:
                # The horizon is only an upper bound, so the step cannot be known up front. Thinning keeps the whole run in the replay.
                frames = frames[::2]
                frame_step *= 2

    while True:
        drain()
        if plan_finished(plan, tracker):
            break
        views = {machine_id: engine.machine_view(machine_id) for machine_id in MACHINE_ORDER}
        issued = False
        # A refused command simply comes up again after the next event, which may be what it was waiting for.
        for command in next_commands(plan, tracker, views, frozenset(), run_id):
            if engine.start_operation(command)["ok"]:
                issued = True
            else:
                refusals += 1
        if issued:
            drain()
        elif stalled(plan, tracker, views, False):
            is_stalled = True
            break
        when = engine.next_event_time()
        if when is None:
            is_stalled = True
            break
        if when > horizon_t:
            frames_before(horizon_t)
            engine.run_until(horizon_t)
            drain()
            horizon_hit = not plan_finished(plan, tracker)
            break
        frames_before(when)
        engine.run_until(when)

    energy = engine.total_energy_kwh() - energy_before
    wall = max(perf_counter() - wall_started, 1e-6)
    sim_minutes = engine.t - t0
    return _result(
        plan, tracker, finished_at, setups, setup_minutes, energy, wall, sim_minutes, t0, horizon_t, is_stalled, horizon_hit,
        refusals, engine.timeline if record_timeline else [], frames, engine.t,
    )


def _result(
    plan: Plan, tracker: Tracker, finished_at: Mapping[str, float], setups: int, setup_minutes: float, energy: float, wall: float,
    sim_minutes: float, t0: float, horizon_t: float, is_stalled: bool, horizon_hit: bool, refusals: int, timeline: list[dict],
    frames: list[dict], ended_at: float,
) -> TwinRun:
    problem = plan.problem
    horizon_end = _whole(horizon_t)
    base = _whole(t0)
    lots_of: dict[str, list[Lot]] = {}
    for lot in plan.lots:
        lots_of.setdefault(lot.order_id, []).append(lot)

    completion: dict[str, int] = {}
    unfinished: list[str] = []
    lots_packed = 0
    total_tardiness = weighted = late = 0
    for order in problem.orders:
        lots = lots_of.get(order.id, [])
        pack_ids = [lot.op_id(PACK) for lot in lots]
        done = [op_id for op_id in pack_ids if tracker.op_state(op_id) == "done"]
        lots_packed += len(done)
        if len(done) < len(pack_ids):
            # So a variant cannot look better by not delivering.
            unfinished.append(order.id)
            end = horizon_end
        else:
            ends = [_whole(finished_at[op_id]) for op_id in done if op_id in finished_at]
            if order.completed_lots_end is not None:
                ends.append(order.completed_lots_end)
            if not ends:
                continue
            end = max(ends)
        completion[order.id] = end
        tardiness = max(0, end - order.due)
        if tardiness:
            late += 1
            total_tardiness += tardiness
            weighted += order.weight * tardiness

    if unfinished or is_stalled or horizon_hit:
        makespan = horizon_end - base
    else:
        makespan = max((_whole(t) for t in finished_at.values()), default=base) - base
    setup_total = _whole(setup_minutes)
    weights = problem.weights
    objective = weights.tardiness * weighted + weights.makespan * makespan + weights.setup * setup_total
    return TwinRun(
        makespan=makespan,
        tardiness=total_tardiness,
        late_orders=late,
        setups=setups,
        energy_kwh=round(energy, 1),
        order_completion=completion,
        unfinished_orders=unfinished,
        stalled=is_stalled,
        wall_seconds=wall,
        sim_minutes=sim_minutes,
        speedup=sim_minutes * 60.0 / wall,
        timeline=timeline,
        frames=frames,
        weighted_tardiness=weighted,
        setup_minutes=setup_total,
        objective=objective,
        horizon_hit=horizon_hit,
        refusals=refusals,
        started_at=t0,
        ended_at=ended_at,
        finished_at=dict(finished_at),
        lots_packed=lots_packed,
        op_states={op.id: tracker.op_state(op.id) for op in problem.ops},
    )


def run_assignments_json(plan: Plan, run: TwinRun) -> list[dict]:
    """The plan's rows as the twin actually ran them, for the variant's Gantt. Other rows keep their planned times."""
    rows: dict[str, dict] = {}
    for row in run.timeline:
        if row["type"] != "op":
            continue
        members = sorted(row["op_ids"])
        for index, op_id in enumerate(members):
            planned = plan.problem.has_op(op_id)
            op = plan.problem.op(op_id) if planned else None
            lot_id = op.lot_id if op is not None else (row["lot_ids"][index] if index < len(row["lot_ids"]) else None)
            lot = plan.lots_by_id.get(lot_id) if lot_id is not None else None
            rows[op_id] = {
                "op_id": op_id,
                "kind": row["kind"],
                "machine": row["machine"],
                "start": round(row["run_start"] if row["run_start"] is not None else row["start"], 2),
                "end": round(row["end"], 2),
                "setup_before": round(row["setup_min"], 2) if index == 0 else 0,
                "batch_id": row["batch_id"],
                "lot_id": lot_id,
                "order_id": op.order_id if op is not None else None,
                "product_id": lot.product_id if lot is not None else None,
                "state": "done",
                "frozen": op is not None and op.frozen is not None,
            }
    for assignment in plan.schedule.assignments:
        if assignment.op_id not in rows and plan.problem.has_op(assignment.op_id):
            rows[assignment.op_id] = assignment_json(plan, assignment, run.op_states.get(assignment.op_id))
    return sorted(rows.values(), key=lambda r: (r["start"], r["machine"], r["op_id"]))


def variant_json(label: str, plan: Plan, run: TwinRun, **extra) -> dict:
    return {
        "label": label,
        **extra,
        "kpis": run.kpis(),
        "order_completion": dict(run.order_completion),
        "speedup": int(run.speedup),
        "wall_seconds": round(run.wall_seconds, 4),
        "sim_minutes": round(run.sim_minutes, 1),
        "assignments": run_assignments_json(plan, run),
        "frames": run.frames,
    }


def run_rank(run: TwinRun) -> tuple[int, int]:
    """What "better" means between runs: delivering first, then the objective both solvers optimise."""
    return (1 if run.stalled or run.horizon_hit or run.unfinished_orders else 0, run.objective)


def _rush_of(scenario: Sequence[Mapping], now: float) -> tuple[list[Lot], list[OrderInfo], list[dict]]:
    lots: list[Lot] = []
    orders: list[OrderInfo] = []
    described: list[dict] = []
    number = 1
    for item in scenario:
        if item["type"] != "rush_order":
            continue
        order_id = f"{Z_PREFIX}A-{len(orders) + 1:04d}"
        due = _whole(now + float(item["due_in_min"]))
        order = Order(order_id, item["product_id"], int(item["qty_bars"]), due, weight=RUSH_WEIGHT, created_min=now)
        fork_lots = [replace(lot, id=f"{Z_PREFIX}{lot.id}") for lot in split_order(order, number)]
        number += len(fork_lots)
        lots.extend(fork_lots)
        orders.append(OrderInfo(order_id, due, RUSH_WEIGHT))
        described.append(
            {
                "order_id": order_id,
                "product_id": item["product_id"],
                "qty_bars": int(item["qty_bars"]),
                "due_min": due,
                "lot_ids": [lot.id for lot in fork_lots],
            }
        )
    return lots, orders, described


def _downtimes_of(scenario: Sequence[Mapping], now: float) -> dict[str, list[Downtime]]:
    downtimes: dict[str, list[Downtime]] = {}
    for item in scenario:
        if item["type"] == "machine_down":
            at = now + float(item.get("at_offset_min", 0.0))
            reason = item.get("code") or "Szenario"
            downtimes.setdefault(item["machine"], []).append(
                Downtime(math.floor(at), _whole(at + float(item["duration_min"])), reason)
            )
    return downtimes


def _fork_melts(lots: Sequence[Lot], counter: int) -> list[str]:
    """Enough fork-only melts for any allocation. The builder reuses them in order, and drops the surplus from the chain's tail."""
    demand: dict[str, int] = {}
    for lot in lots:
        demand[lot.product.alloy] = demand.get(lot.product.alloy, 0) + lot.n_billets
    melts: list[str] = []
    for alloy, billets in sorted(demand.items()):
        for _ in range(math.ceil(billets / catalog.BILLETS_PER_MELT)):
            melts.append(f"{Z_PREFIX}M-{alloy}-{counter:05d}")
            counter += 1
    return melts


def _current_plan(
    state,
    lots: Sequence[Lot],
    orders: Sequence[OrderInfo],
    active_plan: Plan | None,
    *,
    revision: int,
    melt_counter: int,
    melts: Sequence[str],
    prefix: str,
    rush_orders: Sequence[OrderInfo] = (),
) -> Plan:
    """The plan as it stands at the fork: sequences kept, anything new at machine and chain tails.

    A rush lot ranks last for the melts here. By due date it would take billets that lots ahead of it in the sequence count on,
    and those would then wait for a melt at the tail of the chain, which is no part of appending naively.
    """
    rush_ids = {order.id for order in rush_orders}
    ranked = [replace(order, due=_NO_DUE) if order.id in rush_ids else order for order in orders]
    built = prepare_problem(
        state, lots, ranked, active_plan, revision=revision, melt_counter=melt_counter, extra_melts=melts, batch_prefix=prefix
    )
    problem = replace(built.problem, orders=tuple(orders))
    schedule = keep_schedule(problem, active_plan.schedule) if active_plan is not None else decode_edd(problem)
    return Plan(revision, problem, schedule, tuple(lots), created_min=state.now, source="twin")


def monte_carlo_rows(
    snapshot: Mapping,
    plan: Plan,
    scenario: Sequence[Mapping],
    seeds: Sequence[int],
    horizon_min: float | None = None,
) -> list[dict]:
    """One row per seed, stochastic with random failures. Top-level, so that a pool can run it in small chunks."""
    rows = []
    for seed in seeds:
        run = run_closed_loop(
            snapshot, plan, scenario=scenario, seed=seed, stochastic=True, horizon_min=horizon_min, record_timeline=False
        )
        rows.append({"seed": seed, "makespan": run.makespan, "total_tardiness": run.tardiness, "unfinished": len(run.unfinished_orders)})
    return rows


def _percentile(sorted_values: Sequence[float], fraction: float) -> float:
    if not sorted_values:
        return 0
    position = fraction * (len(sorted_values) - 1)
    low = math.floor(position)
    high = min(low + 1, len(sorted_values) - 1)
    value = sorted_values[low] + (sorted_values[high] - sorted_values[low]) * (position - low)
    return round(value, 1)


def monte_carlo_percentiles(rows: Sequence[Mapping]) -> dict:
    """P10, P50 and P90 of the kind `monte_carlo` holds per variant in docs/API.md."""
    result = {}
    for key in ("makespan", "total_tardiness"):
        values = sorted(row[key] for row in rows)
        result[key] = {"p10": _percentile(values, 0.1), "p50": _percentile(values, 0.5), "p90": _percentile(values, 0.9)}
    return result


def _delta(after: TwinRun, before: TwinRun) -> dict:
    return {
        "makespan": after.makespan - before.makespan,
        "total_tardiness": after.tardiness - before.tardiness,
        "objective": after.objective - before.objective,
    }


def _usable_note(notes: Sequence[str]) -> list[str]:
    # The fork's own melt reserve is meant to have a surplus, so that says nothing to anyone.
    return [note for note in notes if f"{Z_PREFIX}M-" not in note]


def evaluate_scenario(
    snapshot: Mapping,
    active_plan: Plan | None,
    open_lots: Sequence[Lot],
    orders: Sequence[OrderInfo],
    scenario: Sequence[Mapping],
    *,
    revision: int,
    melt_counter: int,
    solver_limit_s: float,
    seed: int = 1,
    monte_carlo: int = 0,
    workers: int = 8,
    record_frames: bool = True,
) -> dict:
    """Variants A, B and C of docs/API.md, in the shape of `GET /api/twin/runs/{id}` without its `id`.

    Raises ValueError on a scenario it cannot honour. Extra keys: `rush_orders`, what the fork invented, which the two-step apply
    needs; `notes`; `timing`. `warm_start_schedule` turns a candidate into the warm start of that apply.
    """
    scenario = scenario_items(scenario)
    state = planning_state_from_snapshot(snapshot)
    rush_lots, rush_orders, rush_described = _rush_of(scenario, state.now)
    all_lots = [*open_lots, *rush_lots]
    all_orders = [*orders, *rush_orders]
    melts = _fork_melts(all_lots, melt_counter)
    wall_started = perf_counter()

    plan_a = _current_plan(
        state, open_lots, orders, active_plan, revision=revision, melt_counter=melt_counter, melts=melts, prefix=f"{Z_PREFIX}B-A"
    )
    plan_b = _current_plan(
        state, all_lots, all_orders, active_plan, revision=revision, melt_counter=melt_counter, melts=melts,
        prefix=f"{Z_PREFIX}B-B", rush_orders=rush_orders,
    )
    build = prepare_problem(
        state,
        all_lots,
        all_orders,
        active_plan,
        revision=revision,
        melt_counter=melt_counter,
        downtimes=_downtimes_of(scenario, state.now),
        slowdowns=[item for item in scenario if item["type"] == "slowdown"],
        extra_melts=melts,
        batch_prefix=f"{Z_PREFIX}B-r{revision}",
    )
    schedules = solve_all(build.problem, None, limit=solver_limit_s, seed=seed, workers=workers, hint=plan_b.schedule)
    candidate_set = finalize_candidates(build, schedules, revision=revision, lots=all_lots, created_min=state.now, source="twin")
    solved_seconds = perf_counter() - wall_started

    plans = [plan_a, plan_b, *(c.plan for c in candidate_set.candidates.values() if c.valid)]
    horizon = max(default_horizon_min(plan, scenario, snapshot) for plan in plans)
    run_a = run_closed_loop(snapshot, plan_a, seed=seed, horizon_min=horizon, record_frames=record_frames)
    run_b = run_closed_loop(snapshot, plan_b, scenario=scenario, seed=seed, horizon_min=horizon, record_frames=record_frames)

    runs: dict[str, TwinRun] = {}
    for solver, candidate in candidate_set.candidates.items():
        if candidate.valid:
            runs[solver] = run_closed_loop(snapshot, candidate.plan, scenario=scenario, seed=seed, horizon_min=horizon, record_timeline=False)
    recommended = min(runs, key=lambda solver: run_rank(runs[solver]), default=None)

    notes = _usable_note(build.notes)
    variants = {
        "A": variant_json("Aktueller Plan, ungestört", plan_a, run_a),
        "B": variant_json("Aktueller Plan mit Szenario", plan_b, run_b),
        "C": None,
    }
    run_c: TwinRun | None = None
    plan_c: Plan | None = None
    if recommended is not None:
        plan_c = candidate_set.candidates[recommended].plan
        run_c = run_closed_loop(snapshot, plan_c, scenario=scenario, seed=seed, horizon_min=horizon, record_frames=record_frames)
        variants["C"] = variant_json("Neuer Plan mit Szenario", plan_c, run_c, solver=recommended)
    else:
        notes.append("Kein gültiger neuer Plan für dieses Szenario.")
    if any(item["type"] == "slowdown" for item in scenario):
        notes.append("Die Verlangsamung ist in Variante C näherungsweise eingeplant.")
    for label, plan in (("A", plan_a), ("B", plan_b)):
        violations = validate_schedule(plan.problem, plan.schedule)
        if violations:
            notes.append(f"Variante {label} verletzt Planregeln: {violations[0].message}")

    empty = {"makespan": {"p10": 0, "p50": 0, "p90": 0}, "total_tardiness": {"p10": 0, "p50": 0, "p90": 0}}
    monte = {"n": 0, "B": empty, "C": empty}
    if monte_carlo > 0:
        seeds = [seed + i for i in range(monte_carlo)]
        monte = {
            "n": monte_carlo,
            "B": monte_carlo_percentiles(monte_carlo_rows(snapshot, plan_b, scenario, seeds, horizon)),
            "C": monte_carlo_percentiles(monte_carlo_rows(snapshot, plan_c, scenario, seeds, horizon)) if plan_c is not None else empty,
        }
    return {
        "status": "ready",
        "created_min": snapshot["t"],
        "forked_seq": snapshot["seq"],
        "scenario": scenario,
        "note": " ".join(notes),
        "variants": variants,
        "candidates": {
            solver: candidate_json(candidate, runs[solver].twin_json() if solver in runs else None)
            for solver, candidate in candidate_set.candidates.items()
            if solver != "keep"
        },
        "recommended": recommended,
        "delta_b_vs_a": _delta(run_b, run_a),
        "delta_c_vs_b": _delta(run_c, run_b) if run_c is not None else {},
        "monte_carlo": monte,
        "applicable": recommended is not None,
        "rush_orders": rush_described,
        "timing": {"solve_seconds": round(solved_seconds, 3), "total_seconds": round(perf_counter() - wall_started, 3)},
    }


def warm_start_schedule(result: Mapping, solver: str, lot_ids: Mapping[str, str] = {}) -> Schedule:
    """The twin plan's sequences, for step two of apply: pass it as `hint` when re-solving from live state.

    `lot_ids` maps fork-only lot ids to the real ones the normal order path gave them, so that rush lots keep their places. Other
    fork-only ops are dropped, as they exist nowhere else; load ids lose their prefix, so that the real plan carries no Z- id.
    """
    schedule = schedule_from_candidate_json(result["candidates"][solver])
    assignments = []
    for assignment in schedule.assignments:
        op_id = assignment.op_id
        if op_id.startswith(Z_PREFIX):
            lot_id, _, kind = op_id.rpartition(":")
            if lot_id not in lot_ids:
                continue
            op_id = f"{lot_ids[lot_id]}:{kind}"
        batch_id = assignment.batch_id.removeprefix(Z_PREFIX) if assignment.batch_id else assignment.batch_id
        assignments.append(replace(assignment, op_id=op_id, batch_id=batch_id))
    return replace(schedule, assignments=tuple(assignments))


def _trigger_from(snapshot: Mapping) -> dict | None:
    """A fault's facts come from the snapshot. The most recent fault, if several machines are down, is what set this off."""
    faults = [(data["fault"]["elapsed_min"], machine_id, data["fault"]) for machine_id, data in snapshot["machines"].items() if data["fault"]]
    if not faults:
        return None
    elapsed, machine_id, fault = min(faults, key=lambda item: item[0])
    return {
        "kind": "fault",
        "machine": machine_id,
        "code": fault["code"],
        "text": fault["text"],
        "est_repair_min": fault["estimate_min"],
        "t": snapshot["t"] - elapsed,
    }


def _replan_summary(best: TwinRun | None, keep: TwinRun | None, beats: bool, label: str | None) -> tuple[str, dict | None]:
    if best is None:
        return "Kein gültiger Kandidat gefunden. Der aktuelle Plan bleibt in Kraft.", None
    if keep is None:
        return f"Es gibt keinen aktuellen Plan zum Vergleich. Empfohlen wird {label}.", None
    delta = {
        "makespan": best.makespan - keep.makespan,
        "total_tardiness": best.tardiness - keep.tardiness,
        "late_orders": best.late_orders - keep.late_orders,
        "setups": best.setups - keep.setups,
    }
    if not beats:
        return "Kein Kandidat ist besser als der aktuelle Plan. Der beste geprüfte Kandidat lässt sich trotzdem übernehmen.", delta
    if keep.unfinished_orders and not best.unfinished_orders:
        return "Der aktuelle Plan wird nicht fertig, die Neuplanung liefert alle Aufträge.", delta
    if delta["total_tardiness"] < 0:
        return f"Neuplanung spart {-delta['total_tardiness']} min Verspätung gegenüber dem aktuellen Plan.", delta
    # The same minutes can matter more on another order: the objective weighs them by priority.
    weighted = keep.weighted_tardiness - best.weighted_tardiness
    if weighted > 0:
        return f"Neuplanung senkt die nach Priorität gewichtete Verspätung um {weighted} min.", delta
    if delta["makespan"] < 0:
        if delta["total_tardiness"] == 0:
            return f"Neuplanung verkürzt die Gesamtdauer um {-delta['makespan']} min, die Verspätung bleibt gleich.", delta
        return (
            f"Neuplanung verkürzt die Gesamtdauer um {-delta['makespan']} min, "
            f"bei {delta['total_tardiness']} min mehr Verspätung.",
            delta,
        )
    saved = keep.setup_minutes - best.setup_minutes
    if saved > 0:
        return f"Neuplanung spart {saved} min Rüstzeit gegenüber dem aktuellen Plan.", delta
    return f"Neuplanung verbessert die Zielfunktion um {keep.objective - best.objective} Punkte.", delta


def evaluate_candidate_set(
    snapshot: Mapping,
    candidate_set: CandidateSet,
    *,
    trigger: Mapping | None = None,
    based_on_revision: int | None = None,
    revision: int | None = None,
) -> dict:
    """Twin `keep` against every candidate of a finished set. The second half of `evaluate_replan`, for the two-pool path."""
    runs: dict[str, TwinRun] = {}
    for solver, candidate in candidate_set.candidates.items():
        # An invalid keep is still what the plant would run. An invalid new plan is never going to.
        if candidate.valid or (solver == "keep" and candidate.status != "no_solution"):
            runs[solver] = run_closed_loop(snapshot, candidate.plan, record_timeline=False)

    def rows(solver: str, candidate: Candidate) -> dict:
        return candidate_json(candidate, runs[solver].twin_json() if solver in runs else None)

    keep = candidate_set.candidates.get("keep")
    new = {solver: candidate for solver, candidate in candidate_set.candidates.items() if solver != "keep"}
    usable = [solver for solver, candidate in new.items() if candidate.valid and solver in runs]
    recommended = min(usable, key=lambda solver: run_rank(runs[solver]), default=None)
    best = runs.get(recommended) if recommended else None
    keep_run = runs.get("keep")
    if best is None:
        beats = False
    elif keep_run is None:
        beats = True
    else:
        beats = run_rank(best) < run_rank(keep_run)
    label = new[recommended].plan.schedule.solver if recommended else None
    summary, delta = _replan_summary(best, keep_run, beats, label)
    chosen_revision = revision if revision is not None else (
        next(iter(candidate_set.candidates.values())).plan.revision if candidate_set.candidates else 0
    )
    return {
        "trigger": dict(trigger) if trigger is not None else _trigger_from(snapshot),
        "based_on_revision": based_on_revision,
        "snapshot_seq": snapshot["seq"],
        "keep": rows("keep", keep) if keep is not None else None,
        "candidates": {solver: rows(solver, candidate) for solver, candidate in new.items()},
        "recommended": recommended,
        "beats_keep": beats,
        "summary": summary,
        "delta": delta,
        "melt_counter": candidate_set.melt_counter,
        "notes": list(candidate_set.notes),
        "plan_data": plan_data_json(candidate_set, chosen_revision, snapshot["t"]),
        "twin_wall_ts": time.time(),
    }


def evaluate_replan(
    snapshot: Mapping,
    active_plan: Plan | None,
    open_lots: Sequence[Lot],
    orders: Sequence[OrderInfo],
    *,
    revision: int,
    melt_counter: int,
    solver_limit_s: float,
    seed: int = 1,
    trigger: Mapping | None = None,
    workers: int = 8,
) -> dict:
    """Both solvers from the snapshot, then `keep` against each in the twin. The proposal fields of docs/API.md, as plain data.

    Faulted machines and their downtimes come from the snapshot. Extra keys: `melt_counter`, `plan_data` for `plan_from_result`
    (strip it before sending the proposal to a client), `notes`, and `snapshot_seq` for the chain.
    """
    state = planning_state_from_snapshot(snapshot)
    candidate_set = make_candidates(
        state,
        open_lots,
        orders,
        active_plan,
        revision=revision,
        melt_counter=melt_counter,
        solver_limit_s=solver_limit_s,
        seed=seed,
        workers=workers,
        source="proposal",
    )
    solved_wall_ts = time.time()
    result = evaluate_candidate_set(
        snapshot,
        candidate_set,
        trigger=trigger,
        based_on_revision=active_plan.revision if active_plan is not None else None,
        revision=revision,
    )
    result["solved_wall_ts"] = solved_wall_ts
    return result


__all__ = [
    "MAX_FRAMES",
    "api_scenario_items",
    "RUSH_WEIGHT",
    "TwinRun",
    "Z_PREFIX",
    "default_horizon_min",
    "evaluate_candidate_set",
    "evaluate_replan",
    "evaluate_scenario",
    "monte_carlo_percentiles",
    "monte_carlo_rows",
    "run_assignments_json",
    "run_closed_loop",
    "run_rank",
    "scenario_items",
    "variant_json",
    "warm_start_schedule",
]
