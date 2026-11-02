"""Input rules shared by the REST routes and the twin's scenario items. Pure: no state, no I/O, nothing heavy to import."""
from __future__ import annotations

import math
from typing import Any

from ..domain import catalog

MAX_MINUTES = 100_000.0
MAX_QTY = 1_000_000
MAX_LOTS_PER_ORDER = 16
MAX_SCENARIO_ITEMS = 50
MAX_SLOWDOWN_FACTOR = 100.0


class OrderRuleError(ValueError):
    """An order or rush item the plant must not book. `code` and `extra` go into the error answer."""

    def __init__(self, code: str, message: str, **extra: Any) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.extra = extra


def is_number(value: Any) -> bool:
    """A real, finite number. JSON brings bool, NaN, Infinity and integers too large for a float through the same door."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


def is_minutes(value: Any, *, low: float = 0.0, high: float = MAX_MINUTES) -> bool:
    return is_number(value) and low <= value <= high


def whole_quantity(value: Any) -> int | None:
    """The quantity as an int, or None if it is not a whole number between 1 and MAX_QTY."""
    if not is_number(value) or value < 1 or value > MAX_QTY or value != int(value):
        return None
    return int(value)


def order_fields(product_id: Any, qty: Any, due_in: Any) -> tuple[int, float]:
    """The rules of POST /api/orders, which a twin rush order follows as well. Returns quantity and due offset."""
    if not isinstance(product_id, str) or product_id not in catalog.PRODUCTS:
        raise OrderRuleError("bad_order", "Produkt, Menge und Termin müssen angegeben werden.")
    quantity = whole_quantity(qty)
    if quantity is None:
        raise OrderRuleError("bad_order", f"Die Menge muss eine ganze Zahl zwischen 1 und {MAX_QTY} sein.")
    if not is_number(due_in) or due_in > MAX_MINUTES:
        raise OrderRuleError("bad_order", f"Der Termin muss eine Zahl bis {MAX_MINUTES:.0f} Minuten sein.")
    product = catalog.PRODUCTS[product_id]
    lead = catalog.min_lead_min(product)
    if due_in < lead:
        raise OrderRuleError("due_too_soon", f"Termin zu knapp: frühestens in {lead} min möglich.", min_lead_min=lead)
    billets = quantity * (1 + catalog.SCRAP_MARGIN) / product.bars_per_billet
    if billets / catalog.MAX_BILLETS_PER_LOT > MAX_LOTS_PER_ORDER:
        raise OrderRuleError("bad_order", f"Die Menge ergäbe mehr als {MAX_LOTS_PER_ORDER} Lose. Bitte aufteilen.")
    return quantity, float(due_in)


__all__ = ["MAX_LOTS_PER_ORDER", "MAX_MINUTES", "MAX_QTY", "MAX_SCENARIO_ITEMS", "MAX_SLOWDOWN_FACTOR", "OrderRuleError",
           "is_minutes", "is_number", "order_fields", "whole_quantity"]
