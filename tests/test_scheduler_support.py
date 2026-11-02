"""Shared helpers for the scheduler tests: plant states, an instance generator, an independent billet replay.

Holds no tests of its own. It lives under this name so that every scheduler test file matches test_scheduler*.py.
"""
from __future__ import annotations

import random
from dataclasses import dataclass, field

from werkhalle.domain import catalog
from werkhalle.domain.catalog import AGE, CAST, PACK, PRESS, SAW, TRANSPORT_MIN
from werkhalle.domain.orders import Lot, Order, split_order
from werkhalle.domain.state import LotInTransit, MachineState, MeltInTransit, PlanningState
from werkhalle.scheduling import Downtime, OrderInfo, Problem, Schedule

NO_RUSH = 100_000


def machine_states(**overrides: MachineState) -> tuple[MachineState, ...]:
    return tuple(
        overrides.get(m, MachineState(id=m, status="IDLE", setup_class=catalog.INITIAL_SETUP_CLASS[m]))
        for m in catalog.MACHINES
    )


def state_at(
    now: float = 0.0,
    store: dict[str, int] | None = None,
    started: tuple[str, ...] = (),
    finished: tuple[str, ...] = (),
    melts: tuple[MeltInTransit, ...] = (),
    lots: tuple[LotInTransit, ...] = (),
    **machines: MachineState,
) -> PlanningState:
    """An idle plant at `now`, except for the machines given by id. Started includes what is finished."""
    return PlanningState(
        run_id="test",
        seq=0,
        now=now,
        machines=machine_states(**machines),
        billet_store=dict(catalog.INITIAL_BILLET_STOCK if store is None else store),
        melts_in_transit=melts,
        lots_in_transit=lots,
        started_op_ids=frozenset(started) | frozenset(finished),
        finished_op_ids=frozenset(finished),
    )


def lot(lot_id: str, product: str = "FR-60", n_billets: int = 8, order: str = "A-1") -> Lot:
    return Lot(lot_id, order, product, n_billets)


def order(order_id: str = "A-1", due: int = NO_RUSH, weight: int = 1, completed: int | None = None) -> OrderInfo:
    return OrderInfo(order_id, due, weight, completed)


def by_op(schedule: Schedule):
    return {a.op_id: a for a in schedule.assignments}


def assert_valid(problem: Problem, schedule: Schedule) -> None:
    from werkhalle.scheduling import validate_schedule

    violations = validate_schedule(problem, schedule)
    assert not violations, [f"{v.rule}: {v.message}" for v in violations[:6]]


def replay_billets(problem: Problem, schedule: Schedule) -> dict[str, int]:
    """Lowest level each alloy's store reaches. Deliberately not the validator's code, so that they check each other."""
    items = by_op(schedule)
    timeline: dict[str, list[tuple[int, int]]] = {}
    for supply in problem.billet_supply:
        timeline.setdefault(supply.alloy, []).append((supply.available_at, supply.count))
    for op in problem.ops:
        if op.kind == CAST:
            timeline.setdefault(op.alloy, []).append((items[op.id].end + TRANSPORT_MIN, op.billets_out))
        elif op.kind == PRESS and op.frozen is None:
            # When the press begins, with its setup: that is when the plant takes the billets.
            timeline.setdefault(op.alloy, []).append((items[op.id].start - items[op.id].setup_before, -op.billets_in))
    lowest: dict[str, int] = {}
    for alloy, events in timeline.items():
        stock = 0
        low = 0
        # Arrivals first within one minute: billets arriving at t can be taken at t.
        for _, change in sorted(events, key=lambda e: (e[0], -e[1])):
            stock += change
            low = min(low, stock)
        lowest[alloy] = low
    return lowest


def realistic_lots(target: int = 25, seed: int = 7, start: float = 0.0) -> tuple[list[Lot], list[OrderInfo]]:
    rng = random.Random(seed)
    lots: list[Lot] = []
    orders: list[OrderInfo] = []
    number = 1
    k = 1000
    products = list(catalog.PRODUCTS)
    while len(lots) < target:
        k += 1
        the_order = Order(f"A-{k}", rng.choice(products), rng.randint(60, 260), int(start) + rng.randint(800, 3500))
        new = split_order(the_order, number)
        if len(lots) + len(new) > target:
            continue
        number += len(new)
        lots.extend(new)
        orders.append(OrderInfo(the_order.id, int(the_order.due_min), rng.choice((1, 1, 1, 3))))
    return lots, orders


@dataclass
class Instance:
    """Everything a random mid-run build takes, from one consistent snapshot."""

    state: PlanningState
    lots: list[Lot]
    orders: list[OrderInfo]
    pending: list[str]
    counter: int
    downtimes: dict[str, list[Downtime]] = field(default_factory=dict)
    slowdowns: list[dict] = field(default_factory=list)


_STAGES = (
    "pending",
    "pending",
    "pending",
    "press_running",
    "to_age",
    "at_age",
    "age_running",
    "to_saw",
    "at_saw",
    "saw_running",
    "to_pack",
    "at_pack",
    "pack_running",
    "to_ship",
)


def random_midrun(seed: int) -> Instance:
    """A random but physically consistent plant at some instant of a run.

    Machines run at most what fits, a furnace load is one recipe within capacity, every lot's progress is a prefix of
    its route, and running presses took their billets, so that the store is already net of them.
    """
    rng = random.Random(seed)
    now = 0.0 if seed % 7 == 0 else rng.uniform(1.0, 6000.0)
    int_now = int(now)

    lots: list[Lot] = []
    orders: list[OrderInfo] = []
    number = 1
    for k in range(rng.randint(2, 9)):
        the_order = Order(
            f"A-{k + 1}",
            rng.choice(list(catalog.PRODUCTS)),
            rng.randint(20, 220),
            int_now + rng.randint(-300, 3000),
            weight=rng.choice((1, 1, 3)),
        )
        new = split_order(the_order, number)
        number += len(new)
        lots.extend(new)
        orders.append(OrderInfo(the_order.id, int(the_order.due_min), the_order.weight))

    started: set[str] = set()
    finished: set[str] = set()
    lots_in_transit: list[LotInTransit] = []
    lots_buffered: dict[str, str] = {}
    press_running: dict[str, Lot] = {}
    saw_lot: Lot | None = None
    pack_lot: Lot | None = None
    # Furnace -> recipe and lots of its running load.
    loads: dict[str, tuple[str, list[Lot]]] = {}
    open_lots: list[Lot] = []
    completed_end: dict[str, int] = {}

    def finish_through(the_lot: Lot, kinds: tuple[str, ...]) -> None:
        for kind in kinds:
            finished.add(the_lot.op_id(kind))

    for the_lot in rng.sample(lots, len(lots)):
        stage = rng.choice(_STAGES)
        product = the_lot.product
        arrival = now + rng.uniform(0.0, TRANSPORT_MIN)
        if stage == "press_running":
            free = [p for p in product.press_min_per_billet if p not in press_running]
            if not free:
                stage = "pending"
            else:
                press_running[rng.choice(free)] = the_lot
                started.add(the_lot.op_id(PRESS))
        elif stage == "age_running":
            finish_through(the_lot, (PRESS,))
            options = []
            for furnace in catalog.MACHINES_BY_KIND[AGE]:
                recipe, members = loads.get(furnace, (product.recipe, []))
                used = sum(m.n_baskets for m in members)
                if recipe == product.recipe and used + the_lot.n_baskets <= catalog.MACHINES[furnace].capacity:
                    options.append(furnace)
            if not options:
                stage = "at_age"
            else:
                furnace = rng.choice(options)
                loads.setdefault(furnace, (product.recipe, []))[1].append(the_lot)
                started.add(the_lot.op_id(AGE))
        elif stage == "saw_running":
            finish_through(the_lot, (PRESS, AGE))
            if saw_lot is None:
                saw_lot = the_lot
                started.add(the_lot.op_id(SAW))
            else:
                stage = "at_saw"
        elif stage == "pack_running":
            finish_through(the_lot, (PRESS, AGE, SAW))
            if pack_lot is None:
                pack_lot = the_lot
                started.add(the_lot.op_id(PACK))
            else:
                stage = "at_pack"

        if stage in ("to_age", "at_age"):
            finish_through(the_lot, (PRESS,))
        if stage in ("to_saw", "at_saw"):
            finish_through(the_lot, (PRESS, AGE))
        if stage in ("to_pack", "at_pack"):
            finish_through(the_lot, (PRESS, AGE, SAW))
        if stage == "to_ship":
            finish_through(the_lot, (PRESS, AGE, SAW, PACK))
            completed_end[the_lot.order_id] = max(completed_end.get(the_lot.order_id, 0), int(now) - rng.randint(0, 200))
        if stage.startswith("to_") and stage != "to_ship":
            lots_in_transit.append(LotInTransit(the_lot.id, stage[3:], arrival))
        elif stage.startswith("at_"):
            lots_buffered[the_lot.id] = stage[3:]
        # A lot in transit to shipping is usually already gone from the open list, but need not be.
        if stage != "to_ship" or rng.random() < 0.5:
            open_lots.append(the_lot)

    orders = [
        OrderInfo(o.id, o.due, o.weight, completed_end.get(o.id)) if o.id in completed_end else o for o in orders
    ]

    def other_state(machine_id: str, op_ids, kind, lot_ids, batch_id, setup_class: str | None) -> MachineState:
        has_op = bool(op_ids)
        roll = rng.random()
        if roll < 0.2:
            status = "FAULT"
        elif has_op:
            status = "SETUP" if roll < 0.35 else "RUN"
        else:
            status = "IDLE"
        fault = status == "FAULT"
        estimate = rng.uniform(5.0, 500.0) if fault else 0.0
        elapsed = rng.uniform(0.0, 200.0) if fault else 0.0
        return MachineState(
            id=machine_id,
            status=status,
            setup_class=setup_class,
            op_ids=tuple(op_ids),
            kind=kind,
            lot_ids=tuple(lot_ids),
            batch_id=batch_id,
            started_min=(now - rng.uniform(0.0, 400.0)) if has_op else None,
            remaining_work_min=rng.choice((0.0, rng.uniform(0.1, 400.0))) if has_op else 0.0,
            fault_code=catalog.MACHINES[machine_id].faults[0].code if fault else "",
            fault_estimate_min=estimate,
            fault_elapsed_min=elapsed,
            fault_remaining_min=max(estimate - elapsed, 15.0) if fault else 0.0,
        )

    def idle_class(kind: str) -> str | None:
        if rng.random() < 0.1:
            return None
        return catalog.setup_class(kind, rng.choice(list(catalog.PRODUCTS.values())))

    machines: list[MachineState] = []
    running_cast_alloy = rng.choice(catalog.ALLOYS) if rng.random() < 0.4 else None
    next_melt = rng.randint(1, 40)
    if running_cast_alloy:
        plan_id = f"M-{running_cast_alloy}-{next_melt:05d}"
        next_melt += 1
        op_id = f"{plan_id}:cast"
        started.add(op_id)
        machines.append(other_state("GUSS", (op_id,), CAST, (), None, running_cast_alloy))
    else:
        machines.append(other_state("GUSS", (), None, (), None, idle_class(CAST)))
    for press in catalog.MACHINES_BY_KIND[PRESS]:
        the_lot = press_running.get(press)
        if the_lot is not None:
            machines.append(
                other_state(press, (the_lot.op_id(PRESS),), PRESS, (the_lot.id,), None, catalog.setup_class(PRESS, the_lot.product))
            )
        else:
            machines.append(other_state(press, (), None, (), None, idle_class(PRESS)))
    for furnace in catalog.MACHINES_BY_KIND[AGE]:
        if furnace in loads:
            recipe, members = loads[furnace]
            machines.append(
                other_state(
                    furnace,
                    tuple(m.op_id(AGE) for m in members),
                    AGE,
                    tuple(m.id for m in members),
                    f"B-r0-{furnace}",
                    recipe,
                )
            )
        else:
            machines.append(other_state(furnace, (), None, (), None, idle_class(AGE)))
    for machine_id, the_lot, kind in (("SAE", saw_lot, SAW), ("VP", pack_lot, PACK)):
        if the_lot is not None:
            machines.append(
                other_state(machine_id, (the_lot.op_id(kind),), kind, (the_lot.id,), None, catalog.setup_class(kind, the_lot.product))
            )
        else:
            machines.append(other_state(machine_id, (), None, (), None, idle_class(kind)))

    melts_in_transit = []
    for _ in range(rng.choice((0, 0, 1, 2))):
        alloy = rng.choice(catalog.ALLOYS)
        melts_in_transit.append(
            MeltInTransit(f"S-{next_melt:05d}", f"M-{alloy}-{next_melt:05d}", alloy, 24, now + rng.uniform(0.0, 8.0))
        )
        started.add(f"M-{alloy}-{next_melt:05d}:cast")
        finished.add(f"M-{alloy}-{next_melt:05d}:cast")
        next_melt += 1

    pending = []
    for _ in range(rng.choice((0, 1, 2, 4))):
        alloy = rng.choice(catalog.ALLOYS)
        pending.append(f"M-{alloy}-{next_melt:05d}")
        next_melt += 1

    downtimes: dict[str, list[Downtime]] = {}
    for _ in range(rng.choice((0, 0, 1, 2))):
        machine_id = rng.choice(list(catalog.MACHINES))
        begin = int_now + rng.randint(-50, 800)
        downtimes.setdefault(machine_id, []).append(Downtime(begin, begin + rng.randint(5, 300), "Test"))
    slowdowns = []
    if rng.random() < 0.25:
        slowdowns.append(
            {
                "type": "slowdown",
                "machine": rng.choice(list(catalog.MACHINES)),
                "factor": rng.choice((1.5, 2.0, 3.0)),
                "at_offset_min": rng.randint(0, 400),
                "duration_min": rng.randint(30, 600),
            }
        )

    state = PlanningState(
        run_id="prop",
        seq=seed,
        now=now,
        machines=tuple(machines),
        billet_store={alloy: rng.choice((0, 0, 4, 8, 13, 24, 40)) for alloy in catalog.ALLOYS},
        melts_in_transit=tuple(melts_in_transit),
        lots_in_transit=tuple(lots_in_transit),
        lots_buffered=lots_buffered,
        started_op_ids=frozenset(started) | frozenset(finished),
        finished_op_ids=frozenset(finished),
    )
    return Instance(state, open_lots, orders, pending, next_melt, downtimes, slowdowns)
