"""Orders and lots. An order is what the customer wants; a lot is what runs through the plant."""
from __future__ import annotations

import math
from dataclasses import asdict, dataclass

from . import catalog


@dataclass(frozen=True)
class Order:
    id: str  # "A-1001"
    product_id: str
    qty_bars: int
    due_min: float
    weight: int = 1  # tardiness weight; rush orders use 3
    created_min: float = 0.0


@dataclass(frozen=True)
class Lot:
    id: str  # "L-00012"
    order_id: str
    product_id: str
    n_billets: int

    @property
    def product(self) -> catalog.Product:
        return catalog.PRODUCTS[self.product_id]

    @property
    def planned_bars(self) -> int:
        return self.n_billets * self.product.bars_per_billet

    @property
    def n_baskets(self) -> int:
        return math.ceil(self.n_billets / self.product.strands_per_basket)

    @property
    def n_packages(self) -> int:
        return math.ceil(self.planned_bars / self.product.bars_per_package)

    def op_id(self, kind: str) -> str:
        return f"{self.id}:{kind}"

    def to_dict(self) -> dict:
        data = asdict(self)
        data.update(planned_bars=self.planned_bars, n_baskets=self.n_baskets, n_packages=self.n_packages)
        return data


def lot_from_dict(data: dict) -> Lot:
    return Lot(id=data["id"], order_id=data["order_id"], product_id=data["product_id"], n_billets=data["n_billets"])


def split_order(order: Order, next_lot_no: int) -> list[Lot]:
    """Split an order into lots of at most MAX_BILLETS_PER_LOT billets, with scrap margin.

    The billet count is rounded up and spread as evenly as possible, so no lot is a tiny leftover.
    """
    if order.qty_bars <= 0:
        raise ValueError("order quantity must be positive")
    product = catalog.PRODUCTS[order.product_id]
    total_billets = math.ceil(order.qty_bars * (1 + catalog.SCRAP_MARGIN) / product.bars_per_billet - 1e-9)
    n_lots = math.ceil(total_billets / catalog.MAX_BILLETS_PER_LOT)
    base, extra = divmod(total_billets, n_lots)
    lots = []
    for i in range(n_lots):
        lots.append(
            Lot(
                id=f"L-{next_lot_no + i:05d}",
                order_id=order.id,
                product_id=order.product_id,
                n_billets=base + (1 if i < extra else 0),
            )
        )
    return lots
