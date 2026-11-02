"""The order book the plant starts with, and the orders auto-supply adds later (PLAN section 9).

Due dates come from a load estimate, so the schedule is neither hopeless nor empty of pressure: most orders are reachable,
a few are tight enough that the lateness metrics do real work. No due date is ever below `catalog.min_lead_min`, the same
limit `POST /api/orders` enforces.
"""
from __future__ import annotations

import math
import random
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from ..domain import catalog
from ..domain.orders import Lot, Order, split_order

SEED_ORDERS = 11
SEED_LOTS = 22
SEED_TIGHT_ORDERS = 3
FIRST_ORDER_NO = 1001
FIRST_LOT_NO = 1

SUPPLY_BELOW_LOTS = 12
SUPPLY_TARGET_LOTS = 18
SUPPLY_CAP_LOTS = 28

_FURNACE_BASKETS = sum(catalog.MACHINES[m].capacity for m in catalog.MACHINES_BY_KIND[catalog.AGE])
_PRESSES = len(catalog.MACHINES_BY_KIND[catalog.PRESS])
_REALISED_LOAD = 1.6  # with this the better startup plan leaves 0 to 3 of the 11 seed orders late, measured on eight seeds


@dataclass(frozen=True)
class SeededOrder:
    order: Order
    lots: tuple[Lot, ...]


def stage_load_min(lots: Iterable[Lot]) -> dict[str, float]:
    """Planned minutes per stage, divided by the machines that share the stage's work."""
    press = age = saw = pack = 0.0
    billets: dict[str, int] = {}
    for lot in lots:
        product = lot.product
        press += catalog.planned_minutes(catalog.PRESS, min(product.press_min_per_billet.values()) * lot.n_billets)
        age += catalog.RECIPES[product.recipe].soak_min * lot.n_baskets
        saw += catalog.planned_minutes(catalog.SAW, lot.planned_bars * catalog.SAW_MIN_PER_BAR)
        pack += catalog.planned_minutes(catalog.PACK, lot.n_packages * catalog.PACK_MIN_PER_PACKAGE)
        billets[product.alloy] = billets.get(product.alloy, 0) + lot.n_billets
    cast_minutes = catalog.planned_minutes(catalog.CAST, catalog.MELT_MIN)
    cast = sum(math.ceil(n / catalog.BILLETS_PER_MELT) * cast_minutes for n in billets.values())
    return {
        catalog.CAST: float(cast),
        catalog.PRESS: press / _PRESSES,
        catalog.AGE: age / _FURNACE_BASKETS,
        catalog.SAW: saw,
        catalog.PACK: pack,
    }


def load_estimate_min(lots: Iterable[Lot]) -> float:
    """How long the bottleneck stage is busy with this work. A rough estimate on purpose: it only has to scale due dates."""
    load = stage_load_min(lots)
    return max(load.values(), default=0.0)


def _quantity(product: catalog.Product, n_lots: int, rng: random.Random) -> int:
    """A round order quantity that splits into exactly `n_lots` lots."""
    billets = catalog.MAX_BILLETS_PER_LOT * n_lots - rng.randint(0, 2)
    bars = int(billets * product.bars_per_billet / (1 + catalog.SCRAP_MARGIN))
    return max(product.bars_per_package, bars - bars % product.bars_per_package)


def _ceil_to(value: float, step: int) -> int:
    return int(math.ceil(value / step) * step)


def _due(product_id: str, now: float, wanted_lead: float) -> int:
    lead = catalog.min_lead_min(catalog.PRODUCTS[product_id])
    return int(math.ceil(now + max(lead, wanted_lead)))


def _lot_counts(rng: random.Random, n_orders: int, total_lots: int) -> list[int]:
    counts = [1] * n_orders
    while sum(counts) < total_lots:
        index = rng.randrange(n_orders)
        if counts[index] < 3:
            counts[index] += 1
    return counts


def seed_orders(seed: int, *, now: float = 0.0, first_order_no: int = FIRST_ORDER_NO,
                first_lot_no: int = FIRST_LOT_NO) -> list[SeededOrder]:
    """The start-up order book: all six products, deterministic from `seed`."""
    rng = random.Random(seed)
    products = sorted(catalog.PRODUCTS)
    extra = [rng.choice(products) for _ in range(SEED_ORDERS - len(products))]
    chosen = products + extra
    rng.shuffle(chosen)
    counts = _lot_counts(rng, len(chosen), SEED_LOTS)

    orders: list[tuple[str, int, list[Lot]]] = []
    next_lot = first_lot_no
    for index, (product_id, n_lots) in enumerate(zip(chosen, counts)):
        order_id = f"A-{first_order_no + index}"
        qty = _quantity(catalog.PRODUCTS[product_id], n_lots, rng)
        lots = split_order(Order(order_id, product_id, qty, 0.0), next_lot)
        next_lot += len(lots)
        orders.append((product_id, qty, lots))

    horizon = load_estimate_min(lot for _, _, lots in orders for lot in lots)
    # Every due date is a share of the load horizon on top of the minimum lead, so the queue length decides who is at risk.
    positions = list(range(len(orders)))
    rng.shuffle(positions)
    tight = set(positions[:SEED_TIGHT_ORDERS])
    result: list[SeededOrder] = []
    for index, (product_id, qty, lots) in enumerate(orders):
        lead = catalog.min_lead_min(catalog.PRODUCTS[product_id])
        share = (positions[index] + 1) / len(orders)
        if index in tight:
            wanted = lead + 0.45 * horizon * share
        else:
            # The real makespan is about twice the bottleneck load (melts, setups, furnace loads that do not fill up).
            wanted = lead + _REALISED_LOAD * horizon * (0.45 + 0.65 * share)
        due = _due(product_id, now, _ceil_to(wanted, 10))
        order = Order(lots[0].order_id, product_id, qty, due, 1, now)
        result.append(SeededOrder(order, tuple(lots)))
    return result


def next_supply(rng: random.Random, *, now: float, open_lots: Sequence[Lot], next_order_no: int, next_lot_no: int,
                target_lots: int = SUPPLY_TARGET_LOTS, cap_lots: int = SUPPLY_CAP_LOTS) -> list[SeededOrder]:
    """Orders that bring the open lots up to `target_lots`, never past `cap_lots`.

    Due dates assume the work already on the books comes first, plus some slack.
    """
    open_count = len(open_lots)
    created: list[SeededOrder] = []
    known = list(open_lots)
    products = sorted(catalog.PRODUCTS)
    while open_count < min(target_lots, cap_lots):
        product_id = rng.choice(products)
        n_lots = min(rng.randint(1, 3), cap_lots - open_count)
        product = catalog.PRODUCTS[product_id]
        order_id = f"A-{next_order_no}"
        qty = _quantity(product, n_lots, rng)
        lots = split_order(Order(order_id, product_id, qty, 0.0), next_lot_no)
        known.extend(lots)
        wanted = catalog.min_lead_min(product) + load_estimate_min(known) * _REALISED_LOAD * rng.uniform(0.9, 1.4)
        due = _due(product_id, now, _ceil_to(wanted, 10))
        created.append(SeededOrder(Order(order_id, product_id, qty, due, 1, now), tuple(lots)))
        next_order_no += 1
        next_lot_no += len(lots)
        open_count += len(lots)
    return created


__all__ = [
    "FIRST_LOT_NO", "FIRST_ORDER_NO", "SEED_LOTS", "SEED_ORDERS", "SUPPLY_BELOW_LOTS", "SUPPLY_CAP_LOTS", "SUPPLY_TARGET_LOTS",
    "SeededOrder", "load_estimate_min", "next_supply", "seed_orders", "stage_load_min",
]
