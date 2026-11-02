"""Carries a plan computed from an older snapshot over to what the plant has done since, at the moment it is accepted.

The result either is a validated plan that can be executed as it stands, or `plan` is None with a reason: the MES answers
`409 veraltet` and re-solves from a fresh snapshot.
"""
from __future__ import annotations

import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, replace

from ..domain import catalog
from ..domain.catalog import AGE, BILLETS_PER_MELT, CAST, LOT_ROUTE, MACHINES, PACK, PRESS, SAW, TRANSPORT_MIN
from ..domain.orders import Lot
from ..scheduling.model import BilletSupply, Downtime, Frozen, MachineInfo, Op, OrderInfo, Plan, Pred, Problem
from ..scheduling.retime import keep_schedule
from ..scheduling.validate import validate_schedule
from .tracker import DONE, PENDING, RUNNING, RunningOp, Tracker

_NO_DUE = 1 << 60
_CAST_SUFFIX = f":{CAST}"


class _NotOnAnyMachine(Exception):
    """The tracker knows an op started but not where: a gap, which also set resync_needed. Nothing sensible to freeze."""


@dataclass(frozen=True)
class RebaseResult:
    plan: Plan | None
    reason: str = ""
    dropped: tuple[str, ...] = ()  # ops the plan still wanted to start: started or done meanwhile, or their lot is gone


def _whole(value: float) -> int:
    return int(math.ceil(value - 1e-9))


def _new_lot_op(lot: Lot, kind: str) -> Op:
    """Same durations and classes as the builder gives a lot's op, which `execution` must not import."""
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
        durations = {f: soak for f in catalog.MACHINES_BY_KIND[AGE] if size <= MACHINES[f].capacity}
    elif kind == SAW:
        minutes = catalog.planned_minutes(SAW, lot.planned_bars * catalog.SAW_MIN_PER_BAR)
        durations = {m: minutes for m in catalog.MACHINES_BY_KIND[SAW]}
    else:
        minutes = catalog.planned_minutes(PACK, lot.n_packages * catalog.PACK_MIN_PER_PACKAGE)
        durations = {m: minutes for m in catalog.MACHINES_BY_KIND[PACK]}
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


def _running_record(tracker: Tracker, op_id: str) -> RunningOp | None:
    for running in tracker.running():
        if op_id in running.op_ids:
            return running
    return None


def _frozen(op: Op, tracker: Tracker, now: int, prefix: str) -> Op:
    """The op as it is on its machine now. Times are an estimate: a running op's real end is the plant's business."""
    if op.frozen is not None:
        end = max(op.frozen.end, now + 1)
        return replace(
            op,
            durations={op.frozen.machine: end - op.frozen.start},
            frozen=replace(op.frozen, end=end),
            preds=(),
            billets_in=0,
        )
    running = _running_record(tracker, op.id)
    if running is None:
        raise _NotOnAnyMachine(op.id)
    machine = running.machine
    started = running.started_min if running.started_min is not None else now
    estimate = running.est_end_min
    if estimate is None:
        estimate = started + running.setup_min + op.durations.get(machine, max(op.durations.values(), default=1))
    start = min(now, math.floor(started))
    end = max(now + 1, _whole(estimate))
    batch_id = None
    if MACHINES[machine].kind == AGE:
        batch_id = running.batch_id or f"{prefix}-L-{machine}"
    return replace(
        op,
        durations={machine: end - start},
        frozen=Frozen(machine, start, end, batch_id),
        preds=(),
        release=0,
        billets_in=0,
    )


def _unknown_cast(running: RunningOp, tracker: Tracker, now: int) -> Op:
    """A cast of an older plan that this plan no longer lists: it occupies the line, and its billets arrive."""
    started = running.started_min if running.started_min is not None else now
    planned = catalog.planned_minutes(CAST, catalog.MELT_MIN)
    start = min(now, math.floor(started))
    end = max(now + 1, _whole(running.est_end_min or started + running.setup_min + planned))
    return Op(
        id=running.op_ids[0],
        kind=CAST,
        lot_id=None,
        order_id=None,
        durations={running.machine: end - start},
        setup_class=tracker.setup_class(running.machine) or "",
        alloy=tracker.setup_class(running.machine),
        billets_out=BILLETS_PER_MELT,
        frozen=Frozen(running.machine, start, end),
    )


@dataclass
class _Tier:
    count: int
    release: int = 0
    cast_op: str | None = None


def _allocate_supply(
    ops: list[Op],
    tracker: Tracker,
    lots: Mapping[str, Lot],
    due_of: Mapping[str, int],
    now: int,
) -> tuple[list[Op], list[BilletSupply], dict[str, int]]:
    """PLAN section 5, rule 2 and 3, with no new melts: a rebase has to get by on the supply there is."""
    pending_press = [op for op in ops if op.billets_in and op.frozen is None]
    supply: list[BilletSupply] = []
    releases: dict[str, tuple[int, tuple[Pred, ...]]] = {}
    shortfall: dict[str, int] = {}
    for alloy in catalog.ALLOYS:
        tiers: list[_Tier] = []
        store = tracker.free_billets(alloy, lots)
        if store:
            tiers.append(_Tier(store, release=now))
            supply.append(BilletSupply(alloy, store, now))
        moving = [m for m in tracker.melts_in_transit() if m.alloy == alloy]
        for melt in sorted(moving, key=lambda m: (m.arrives_min, m.melt_id)):
            arrival = max(now, _whole(melt.arrives_min))
            tiers.append(_Tier(melt.n_billets, release=arrival))
            supply.append(BilletSupply(alloy, melt.n_billets, arrival))
        # In chain order, the order the problem lists them in, so that one already running comes first.
        for op in ops:
            if op.kind == CAST and op.alloy == alloy:
                tiers.append(_Tier(op.billets_out, cast_op=op.id))

        ranked = sorted(
            (op for op in pending_press if op.alloy == alloy), key=lambda op: (due_of.get(op.order_id, _NO_DUE), op.lot_id)
        )
        needed = sum(op.billets_in for op in ranked)
        available = sum(tier.count for tier in tiers)
        if needed > available:
            shortfall[alloy] = needed - available
            continue
        cumulative_demand = 0
        cumulative_supply = 0
        index = 0
        release = now
        melt_preds: list[Pred] = []
        for op in ranked:
            cumulative_demand += op.billets_in
            while cumulative_supply < cumulative_demand:
                tier = tiers[index]
                index += 1
                cumulative_supply += tier.count
                release = max(release, tier.release)
                if tier.cast_op:
                    melt_preds.append(Pred(tier.cast_op, TRANSPORT_MIN))
            releases[op.id] = (release, tuple(melt_preds))
    result = []
    for op in ops:
        if op.id in releases:
            release, melt_preds = releases[op.id]
            others = tuple(pred for pred in op.preds if not pred.op_id.endswith(_CAST_SUFFIX))
            op = replace(op, release=release, preds=others + melt_preds)
        result.append(op)
    return result, supply, shortfall


def _release_lots_on_their_way(ops: list[Op], tracker: Tracker) -> list[Op]:
    """A predecessor that is done no longer says when its lot arrives, so the release has to."""
    result = []
    for op in ops:
        moving = tracker.lot_arrival(op.lot_id) if op.lot_id is not None and op.frozen is None else None
        if moving is not None and moving[0] == op.kind:
            op = replace(op, release=max(op.release, _whole(moving[1])))
        result.append(op)
    return result


def _machines(old: Problem, tracker: Tracker, now: int, blocked_until: Mapping[str, int]) -> tuple[MachineInfo, ...]:
    machines = []
    for machine in old.machines:
        downtimes = tuple(
            Downtime(max(d.start, now), d.end, d.reason) for d in machine.downtimes if d.end > now
        )
        machines.append(
            replace(
                machine,
                available_from=max(now, machine.available_from, blocked_until.get(machine.id, 0)),
                initial_setup_class=tracker.setup_class(machine.id) or machine.initial_setup_class,
                downtimes=downtimes,
            )
        )
    return tuple(machines)


def rebase(
    plan: Plan,
    tracker: Tracker,
    open_lot_ids: Iterable[str | Lot],
    *,
    new_lots: Sequence[Lot] = (),
    new_orders: Sequence[OrderInfo] = (),
    now: float | None = None,
) -> RebaseResult:
    """Drop what started or finished, reduce loads, append what is missing at machine tails, re-check the billets.

    `open_lot_ids` lists the lots that must be in the plan. A lot the plan does not know needs its data, in `new_lots` (or as a
    `Lot` in `open_lot_ids`), and its order in `new_orders` for its rank; without the data it stays missing and the plan is None.
    """
    old = plan.problem
    prefix = old.batch_prefix
    wanted = list(open_lot_ids)
    open_ids = {getattr(item, "id", item) for item in wanted}
    lots: dict[str, Lot] = dict(plan.lots_by_id)
    lots.update((item.id, item) for item in wanted if isinstance(item, Lot))
    lots.update((lot.id, lot) for lot in new_lots)
    missing = sorted(lot_id for lot_id in open_ids if lot_id not in lots)
    if missing:
        return RebaseResult(None, f"Lose fehlen im Plan: {', '.join(missing)}.")

    now_int = _whole(max(old.now, tracker.t, now if now is not None else old.now))
    known = {op.id for op in old.ops}
    ops: list[Op] = []
    dropped: list[str] = []
    try:
        for op in old.ops:
            state = RUNNING if op.frozen is not None and tracker.op_state(op.id) == PENDING else tracker.op_state(op.id)
            if state == DONE:
                if op.frozen is None:
                    dropped.append(op.id)
            elif state == RUNNING:
                if op.frozen is None:
                    dropped.append(op.id)
                ops.append(_frozen(op, tracker, now_int, prefix))
            elif op.lot_id is not None and op.lot_id not in open_ids:
                dropped.append(op.id)
            else:
                ops.append(op)
        for lot_id in sorted(open_ids):
            lot = lots[lot_id]
            previous: str | None = None
            for kind in LOT_ROUTE:
                op_id = lot.op_id(kind)
                state = tracker.op_state(op_id)
                if op_id in known:
                    previous = op_id if state != DONE else None
                    continue
                if state == DONE:
                    previous = None
                    continue
                op = _new_lot_op(lot, kind)
                if state == RUNNING:
                    ops.append(_frozen(op, tracker, now_int, prefix))
                else:
                    preds = (Pred(previous, TRANSPORT_MIN),) if previous is not None else ()
                    ops.append(replace(op, preds=preds))
                previous = op_id
    except _NotOnAnyMachine as error:
        return RebaseResult(None, f"{error.args[0]} ist gestartet, aber auf keiner Maschine bekannt.", tuple(dropped))

    # What runs and is not in the plan still occupies its machine.
    in_plan = {op.id for op in ops}
    blocked_until: dict[str, int] = {}
    for running in tracker.running():
        if all(op_id in in_plan for op_id in running.op_ids):
            continue
        if running.kind == CAST:
            ops.insert(0, _unknown_cast(running, tracker, now_int))
        else:
            started = running.started_min if running.started_min is not None else now_int
            estimate = running.est_end_min or started + running.setup_min
            blocked_until[running.machine] = max(now_int + 1, _whole(estimate))

    orders = list(old.orders)
    seen_orders = {order.id for order in orders}
    orders.extend(order for order in new_orders if order.id not in seen_orders)
    due_of = {order.id: order.due for order in orders}

    ops = _release_lots_on_their_way(ops, tracker)
    ops, supply, shortfall = _allocate_supply(ops, tracker, lots, due_of, now_int)
    if shortfall:
        text = ", ".join(f"{alloy}: {count}" for alloy, count in sorted(shortfall.items()))
        return RebaseResult(None, f"Zu wenig Bolzen für die offenen Lose ({text}).", tuple(dropped))

    problem = replace(
        old,
        now=now_int,
        ops=tuple(ops),
        machines=_machines(old, tracker, now_int, blocked_until),
        orders=tuple(orders),
        billet_supply=tuple(supply),
    )
    carried = keep_schedule(problem, plan.schedule)
    if carried.status == "no_solution":
        return RebaseResult(None, "Maschinenfolge und Vorgänger passen nicht mehr zusammen.", tuple(dropped))
    violations = validate_schedule(problem, carried)
    if violations:
        return RebaseResult(None, f"Plan nicht mehr gültig: {violations[0].message}", tuple(dropped))
    schedule = replace(plan.schedule, assignments=carried.assignments, note=carried.note)
    in_problem = {op.lot_id for op in ops}
    kept_lots = tuple(lot for lot in lots.values() if lot.id in in_problem)
    return RebaseResult(replace(plan, problem=problem, schedule=schedule, lots=kept_lots), "", tuple(dropped))


__all__ = ["RebaseResult", "rebase"]
