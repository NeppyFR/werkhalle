"""Turns one consistent plant state plus the open lots into a scheduling problem (PLAN section 5 and 6).

Melt allocation is what keeps the billet store from ever deadlocking a press. Within an alloy, lots are ranked by
(due, id) and each depends on every supply tier up to the one covering its cumulative demand. Melts of one alloy are
chained, so any set of started lots has demand no larger than the supply already arrived for its highest-ranked member,
whatever order the lots actually start in.
"""
from __future__ import annotations

import math
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, replace

from ..domain import catalog
from ..domain.catalog import AGE, BILLETS_PER_MELT, CAST, LOT_ROUTE, MACHINES, PACK, PRESS, SAW, TRANSPORT_MIN
from ..domain.orders import Lot
from ..domain.state import FAULT_REMAINING_FLOOR_MIN, MachineState, PlanningState
from .model import BilletSupply, Downtime, Frozen, MachineInfo, Op, OrderInfo, Pred, Problem, Weights

# Also matches ids from the twin's Z- namespace.
_MELT_ID = re.compile(r"(?:^|-)M-([0-9A-Za-z]+)-(\d+)$")


@dataclass(frozen=True)
class BuildResult:
    problem: Problem
    melt_counter: int  # next free melt number; pass it to the next build, it never resets
    notes: tuple[str, ...] = ()


@dataclass(frozen=True)
class _Slowdown:
    machine: str
    start: float
    end: float
    multiplier: float


def melt_plan_id(alloy: str, number: int) -> str:
    return f"M-{alloy}-{number:05d}"


def cast_op_id(plan_id: str) -> str:
    return f"{plan_id}:{CAST}"


def _parse_melt(plan_id: str) -> tuple[str, int] | None:
    match = _MELT_ID.search(plan_id)
    return (match.group(1), int(match.group(2))) if match else None


def _whole_minutes(value: float) -> int:
    return int(math.ceil(value - 1e-9))


def _normalise_downtimes(items: Iterable[Downtime], now: int) -> tuple[Downtime, ...]:
    """Sorted, clipped to the future and non-overlapping, which is what every consumer assumes."""
    merged: list[Downtime] = []
    for item in sorted(items, key=lambda d: (d.start, d.end)):
        start = max(item.start, now)
        if item.end <= start:
            continue
        if merged and start <= merged[-1].end:
            last = merged[-1]
            reason = last.reason if item.reason in ("", last.reason) else f"{last.reason}, {item.reason}".strip(", ")
            merged[-1] = Downtime(last.start, max(last.end, item.end), reason)
        else:
            merged.append(Downtime(start, item.end, item.reason))
    return tuple(merged)


def _parse_slowdowns(slowdowns: Iterable[Mapping], state_now: float) -> list[_Slowdown]:
    parsed = []
    for item in slowdowns:
        # A duration multiplier, as in SimEngine.schedule_slowdown. Below 1 it would not be a slowdown at all.
        factor = float(item.get("factor", 1.0))
        if factor <= 1.0:
            continue
        start = float(item["at_min"]) if "at_min" in item else state_now + float(item.get("at_offset_min", 0.0))
        parsed.append(_Slowdown(item["machine"], start, start + float(item["duration_min"]), factor))
    return parsed


def _frozen_end(now: int, work: float, downtimes: Sequence[Downtime], slowdowns: Sequence[_Slowdown]) -> int:
    """When the running op should finish: its remaining work pushed through every overlapping downtime."""
    work = max(0.0, work)
    for slowdown in slowdowns:
        overlap = min(now + work, slowdown.end) - max(now, slowdown.start)
        if overlap > 0:
            work += overlap * (slowdown.multiplier - 1.0)
    time = float(now)
    left = work
    for downtime in downtimes:
        if downtime.end <= time:
            continue
        if downtime.start >= time + left:
            break
        left -= max(0.0, downtime.start - time)
        time = float(downtime.end)
    return max(now + 1, _whole_minutes(time + left))


def _lot_op(lot: Lot, kind: str, machines: Mapping[str, MachineInfo]) -> Op:
    """An op of a lot at planning rate, before any state-dependent preds, release or freezing."""
    product = lot.product
    size = 1
    family = None
    billets_in = 0
    if kind == PRESS:
        durations = {
            press: catalog.planned_minutes(PRESS, catalog.ideal_press_min(product, press, lot.n_billets))
            for press in product.press_min_per_billet
        }
        billets_in = lot.n_billets
    elif kind == AGE:
        soak = catalog.planned_minutes(AGE, catalog.RECIPES[product.recipe].soak_min)
        size = lot.n_baskets
        family = product.recipe
        durations = {f: soak for f in catalog.MACHINES_BY_KIND[AGE] if size <= machines[f].capacity}
    elif kind == SAW:
        minutes = catalog.planned_minutes(SAW, lot.planned_bars * catalog.SAW_MIN_PER_BAR)
        durations = {m: minutes for m in catalog.MACHINES_BY_KIND[SAW]}
    elif kind == PACK:
        minutes = catalog.planned_minutes(PACK, lot.n_packages * catalog.PACK_MIN_PER_PACKAGE)
        durations = {m: minutes for m in catalog.MACHINES_BY_KIND[PACK]}
    else:
        raise ValueError(kind)
    return Op(
        id=lot.op_id(kind),
        kind=kind,
        lot_id=lot.id,
        order_id=lot.order_id,
        durations=durations,
        setup_class=catalog.setup_class(kind, product),
        size=size,
        batch_family=family,
        alloy=product.alloy,
        billets_in=billets_in,
    )


def _cast_op(plan_id: str, alloy: str, previous: str | None) -> Op:
    minutes = catalog.planned_minutes(CAST, catalog.MELT_MIN)
    return Op(
        id=cast_op_id(plan_id),
        kind=CAST,
        lot_id=None,
        order_id=None,
        durations={m: minutes for m in catalog.MACHINES_BY_KIND[CAST]},
        setup_class=alloy,
        preds=(Pred(previous, 0),) if previous else (),
        alloy=alloy,
        billets_out=BILLETS_PER_MELT,
    )


@dataclass
class _Tier:
    count: int
    release: int = 0  # free store and melts in transit
    cast_op: str | None = None  # running or planned casts


def _allocate_melts(
    state: PlanningState,
    now: int,
    demand: Sequence[Lot],
    due_of: Mapping[str, int],
    pending_melts: Sequence[str],
    melt_counter: int,
    running_casts: Mapping[str, Op],
    notes: list[str],
) -> tuple[list[Op], dict[str, tuple[int, tuple[Pred, ...]]], list[BilletSupply], int]:
    """Rules 1 to 3 of PLAN section 5. Returns cast ops, per lot (release, melt preds), billet supply, next counter."""
    lots_of: dict[str, list[Lot]] = {}
    for lot in demand:
        lots_of.setdefault(lot.product.alloy, []).append(lot)

    # The counter never goes back behind any id that exists, even if a caller passes a stale one.
    started_casts = {op_id[: -len(CAST) - 1] for op_id in state.started_op_ids if op_id.endswith(f":{CAST}")}
    numbers = [melt_counter]
    for plan_id in (*pending_melts, *started_casts, *(m.plan_id for m in state.melts_in_transit)):
        parsed = _parse_melt(plan_id)
        if parsed:
            numbers.append(parsed[1] + 1)
    counter = max(numbers)

    pending_of: dict[str, list[str]] = {}
    seen: set[str] = set()
    for plan_id in pending_melts:
        parsed = _parse_melt(plan_id)
        if parsed is None:
            notes.append(f"Schmelze {plan_id}: Kennung nicht lesbar, verworfen.")
        elif plan_id not in seen and plan_id not in started_casts:
            seen.add(plan_id)
            pending_of.setdefault(parsed[0], []).append(plan_id)

    alloys = sorted({*catalog.ALLOYS, *lots_of, *state.billet_store, *pending_of, *running_casts})
    casts: list[Op] = []
    per_lot: dict[str, tuple[int, tuple[Pred, ...]]] = {}
    supply: list[BilletSupply] = []
    for alloy in alloys:
        lots = sorted(lots_of.get(alloy, ()), key=lambda lot: (due_of[lot.order_id], lot.id))
        needed = sum(lot.n_billets for lot in lots)
        tiers: list[_Tier] = []

        store = max(0, state.billet_store.get(alloy, 0))
        if store:
            tiers.append(_Tier(store, release=now))
            supply.append(BilletSupply(alloy, store, now))
        transit = [m for m in state.melts_in_transit if m.alloy == alloy]
        for melt in sorted(transit, key=lambda m: (m.arrives_min, m.melt_id)):
            arrival = max(now, _whole_minutes(melt.arrives_min))
            tiers.append(_Tier(melt.n_billets, release=arrival))
            supply.append(BilletSupply(alloy, melt.n_billets, arrival))
        chain_tail: str | None = None
        running = running_casts.get(alloy)
        if running is not None:
            tiers.append(_Tier(running.billets_out, cast_op=running.id))
            chain_tail = running.id

        covered = sum(tier.count for tier in tiers)
        pending = pending_of.get(alloy, [])
        used = 0
        while covered < needed:
            if used < len(pending):
                plan_id = pending[used]
                used += 1
            else:
                plan_id = melt_plan_id(alloy, counter)
                counter += 1
            op = _cast_op(plan_id, alloy, chain_tail)
            casts.append(op)
            tiers.append(_Tier(op.billets_out, cast_op=op.id))
            chain_tail = op.id
            covered += op.billets_out
        if used < len(pending):
            notes.append(f"Legierung {alloy}: {len(pending) - used} überzählige Schmelzen entfallen ({pending[used]} ff.).")

        cumulative_demand = 0
        cumulative_supply = 0
        index = 0
        release = now
        melt_preds: list[Pred] = []
        for lot in lots:
            cumulative_demand += lot.n_billets
            while cumulative_supply < cumulative_demand:
                tier = tiers[index]
                index += 1
                cumulative_supply += tier.count
                release = max(release, tier.release)
                if tier.cast_op:
                    melt_preds.append(Pred(tier.cast_op, TRANSPORT_MIN))
            per_lot[lot.id] = (release, tuple(melt_preds))
    return casts, per_lot, supply, counter


def _apply_slowdowns(ops: list[Op], slowdowns: Sequence[_Slowdown], now: int) -> list[Op]:
    """Scale the duration of every op that could fall inside a window; an approximation the report labels as such."""
    if not slowdowns:
        return ops
    by_id = {op.id: op for op in ops}
    earliest: dict[str, int] = {}
    for op in ops:  # casts in chain order, then each lot in route order: predecessors always come first
        if op.frozen is not None:
            earliest[op.id] = op.frozen.start
            continue
        ready = max(now, op.release)
        for pred in op.preds:
            before = by_id.get(pred.op_id)
            if before is not None:
                ends_at = before.frozen.end if before.frozen else earliest[before.id] + min(before.durations.values())
                ready = max(ready, ends_at + pred.lag)
        earliest[op.id] = ready
    load: dict[str, int] = {}
    for op in ops:
        if op.frozen is None:
            for machine, minutes in op.durations.items():
                load[machine] = load.get(machine, 0) + minutes

    multipliers: dict[tuple[str, str], float] = {}
    for slowdown in slowdowns:
        for op in ops:
            if op.frozen is not None or slowdown.machine not in op.durations:
                continue
            # Between "as early as possible" and "behind everything else on this machine".
            first = earliest[op.id]
            if first < slowdown.end and slowdown.start < first + load[slowdown.machine]:
                # All of a family scale together, or its ops could no longer share a load.
                key = (op.batch_family or op.id, slowdown.machine)
                multipliers[key] = max(multipliers.get(key, 1.0), slowdown.multiplier)
    if not multipliers:
        return ops

    result = []
    for op in ops:
        durations = dict(op.durations)
        if op.frozen is None:
            for machine in durations:
                multiplier = multipliers.get((op.batch_family or op.id, machine), 1.0)
                durations[machine] = _whole_minutes(durations[machine] * multiplier)
        result.append(replace(op, durations=durations))
    return result


def _running_ops(
    state: PlanningState, now: int, downtimes: Mapping[str, tuple[Downtime, ...]], slowdowns: Sequence[_Slowdown], prefix: str
) -> dict[str, tuple[MachineState, Frozen]]:
    running: dict[str, tuple[MachineState, Frozen]] = {}
    for machine_state in state.machines:
        pending_ids = [op_id for op_id in machine_state.op_ids if op_id not in state.finished_op_ids]
        if not pending_ids:
            continue
        machine_slowdowns = [s for s in slowdowns if s.machine == machine_state.id]
        end = _frozen_end(now, machine_state.remaining_work_min, downtimes.get(machine_state.id, ()), machine_slowdowns)
        started = machine_state.started_min
        start = now if started is None else min(now, math.floor(started))
        batch_id = None
        if machine_state.id in MACHINES and MACHINES[machine_state.id].kind == AGE:
            batch_id = machine_state.batch_id or f"{prefix}-L-{machine_state.id}"
        for op_id in pending_ids:
            running[op_id] = (machine_state, Frozen(machine_state.id, start, end, batch_id))
    return running


def build_problem(
    state: PlanningState,
    open_lots: Sequence[Lot],
    orders: Sequence[OrderInfo],
    *,
    pending_melts: Sequence[str] = (),
    melt_counter: int = 1,
    downtimes: Mapping[str, Sequence[Downtime]] = {},
    slowdowns: Sequence[dict] = (),
    batch_prefix: str = "B",
    weights: Weights = Weights(),
) -> BuildResult:
    """Build from a single snapshot (rule 4): store, op state and lot state must all come from `state`."""
    now = _whole_minutes(state.now)
    notes: list[str] = []
    lots = sorted({lot.id: lot for lot in open_lots}.values(), key=lambda lot: lot.id)
    due_of = {order.id: order.due for order in orders}
    for lot in lots:
        if lot.order_id not in due_of:
            raise ValueError(f"lot {lot.id} refers to unknown order {lot.order_id}")

    machine_downtimes: dict[str, list[Downtime]] = {machine: list(items) for machine, items in downtimes.items()}
    for machine_state in state.machines:
        if machine_state.status == "FAULT":
            remaining = machine_state.fault_remaining_min
            length = _whole_minutes(remaining if remaining > 0 else FAULT_REMAINING_FLOOR_MIN)
            machine_downtimes.setdefault(machine_state.id, []).append(
                Downtime(now, now + length, machine_state.fault_code)
            )
    normal_downtimes = {machine: _normalise_downtimes(items, now) for machine, items in machine_downtimes.items()}

    parsed_slowdowns = _parse_slowdowns(slowdowns, state.now)
    running = _running_ops(state, now, normal_downtimes, parsed_slowdowns, batch_prefix)

    states = {m.id: m for m in state.machines}
    machines: dict[str, MachineInfo] = {}
    for machine_id, spec in MACHINES.items():
        known = states.get(machine_id)
        machines[machine_id] = MachineInfo(
            id=machine_id,
            kind=spec.kind,
            capacity=spec.capacity,
            is_batch=spec.kind == AGE,
            available_from=now,
            initial_setup_class=known.setup_class if known else catalog.INITIAL_SETUP_CLASS.get(machine_id),
            downtimes=normal_downtimes.get(machine_id, ()),
        )

    # A running cast is frozen and keeps its billets_out. Its melt is the supply tier just before the pending melts.
    running_casts: dict[str, Op] = {}
    for op_id, (machine_state, frozen) in running.items():
        if machine_state.kind == CAST or op_id.endswith(f":{CAST}"):
            parsed = _parse_melt(op_id[: -len(CAST) - 1])
            alloy = parsed[0] if parsed else machine_state.setup_class
            base = _cast_op(op_id[: -len(CAST) - 1], alloy, None)
            running_casts[alloy] = replace(
                base, durations={frozen.machine: frozen.end - frozen.start}, frozen=frozen
            )

    # Rule 1: demand is only lots whose press has not started.
    demand = [lot for lot in lots if lot.op_id(PRESS) not in state.started_op_ids]
    casts, melt_dependencies, supply, counter = _allocate_melts(
        state, now, demand, due_of, pending_melts, melt_counter, running_casts, notes
    )

    transit = {f"{item.lot_id}:{item.stage}": _whole_minutes(item.arrives_min) for item in state.lots_in_transit}
    ops: list[Op] = [*running_casts.values(), *casts]
    known_running: set[str] = {op.id for op in running_casts.values()}
    for lot in lots:
        previous: str | None = None
        for kind in LOT_ROUTE:
            op_id = lot.op_id(kind)
            if op_id in state.finished_op_ids:
                previous = None
                continue
            base = _lot_op(lot, kind, machines)
            if op_id in running:
                _, frozen = running[op_id]
                known_running.add(op_id)
                ops.append(
                    replace(
                        base,
                        durations={frozen.machine: frozen.end - frozen.start},
                        frozen=frozen,
                        billets_in=0,  # its billets left the store when it started
                    )
                )
                previous = op_id
                continue
            if op_id in state.started_op_ids:
                notes.append(f"{op_id} ist gestartet, läuft aber auf keiner Maschine; übersprungen.")
                previous = None
                continue
            preds: list[Pred] = []
            release = 0
            if previous is not None:
                preds.append(Pred(previous, TRANSPORT_MIN))
            if kind == PRESS:
                release, melt_preds = melt_dependencies[lot.id]
                preds.extend(melt_preds)
            if op_id in transit:
                release = max(release, transit[op_id])
            ops.append(replace(base, preds=tuple(preds), release=release))
            previous = op_id

    for op_id, (machine_state, frozen) in running.items():
        if op_id not in known_running:
            notes.append(f"{op_id} auf {machine_state.id} gehört zu keinem offenen Los; Maschine bleibt bis {frozen.end} belegt.")
            machine = machines.get(machine_state.id)
            if machine is not None:
                machines[machine_state.id] = replace(machine, available_from=max(machine.available_from, frozen.end))

    ops = _apply_slowdowns(ops, parsed_slowdowns, now)
    problem = Problem(
        now=now,
        ops=tuple(ops),
        machines=tuple(machines.values()),
        orders=tuple(orders),
        weights=weights,
        billet_supply=tuple(supply),
        batch_prefix=batch_prefix,
    )
    return BuildResult(problem=problem, melt_counter=counter, notes=tuple(notes))
