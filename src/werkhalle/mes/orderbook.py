"""The order book: orders, their lots, and the rows of `GET /api/orders`.

An order's delivered quantity is the sum of pieces actually packed, reported by the trace store; `qty_bars` is what the customer
wants. Once all lots of an order are packed the order is finished, and short of the quantity it reads `unterlieferung`.
"""
from __future__ import annotations

import math
from collections import deque
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass

from ..domain import catalog
from ..domain.catalog import LOT_ROUTE, PACK, PRESS
from ..domain.orders import Lot, Order, split_order
from ..scheduling.model import OrderInfo
from .seed import FIRST_LOT_NO, FIRST_ORDER_NO

STATUS_OPEN = "offen"
STATUS_ACTIVE = "in_arbeit"
STATUS_DONE = "fertig"
STATUS_SHORT = "unterlieferung"

LOT_WAITING = "wartend"
LOT_RUNNING = "laeuft"
LOT_DONE = "fertig"

# An endless auto-supplied run must not make the order list longer than a screen can use.
MAX_FINISHED_ROWS = 200


OpState = Callable[[str], str]


@dataclass
class OrderRecord:
    order: Order
    lots: tuple[Lot, ...]
    completed_min: float | None = None


class OrderBook:
    """Orders and lots of one run. Open and finished are kept apart, so periodic work does not grow with the length of a run."""

    def __init__(self) -> None:
        self.orders: dict[str, OrderRecord] = {}
        self.lots: dict[str, Lot] = {}
        self.next_order_no = FIRST_ORDER_NO
        self.next_lot_no = FIRST_LOT_NO
        self._open_lots: dict[str, Lot] = {}
        self._open_orders: dict[str, OrderRecord] = {}
        self._finished_orders: deque[str] = deque(maxlen=MAX_FINISHED_ROWS)
        self._lot_end: dict[str, int] = {}
        self.finished_late = 0

    def add(self, order: Order, lots: Iterable[Lot]) -> OrderRecord:
        lots = tuple(lots)
        record = OrderRecord(order, lots)
        self.orders[order.id] = record
        self._open_orders[order.id] = record
        for lot in lots:
            self.lots[lot.id] = lot
            if lot.id not in self._lot_end:
                self._open_lots[lot.id] = lot
        self._advance_counters(order.id, lots)
        return record

    def _advance_counters(self, order_id: str, lots: Iterable[Lot]) -> None:
        self.next_order_no = max(self.next_order_no, int(order_id.rsplit("-", 1)[1]) + 1)
        for lot in lots:
            self.next_lot_no = max(self.next_lot_no, int(lot.id.rsplit("-", 1)[1]) + 1)

    def create(self, product_id: str, qty_bars: int, due_min: float, weight: int, now: float) -> OrderRecord:
        order = Order(f"A-{self.next_order_no}", product_id, qty_bars, due_min, weight, now)
        return self.add(order, split_order(order, self.next_lot_no))

    def open_lots(self, op_state: OpState) -> list[Lot]:
        return [lot for lot in self._open_lots.values() if op_state(lot.op_id(PACK)) != "done"]

    def open_lots_of_snapshot(self, finished_op_ids: Iterable[str]) -> list[Lot]:
        """Open lots as of one snapshot: PLAN section 5, rule 4 wants store, op state and lot state from the same instant."""
        finished = set(finished_op_ids)
        return [lot for lot in self._open_lots.values() if lot.op_id(PACK) not in finished]

    def open_orders(self) -> list[OrderRecord]:
        return list(self._open_orders.values())

    def finished_orders(self) -> list[OrderRecord]:
        """The most recently finished orders, newest first."""
        return [self.orders[order_id] for order_id in reversed(self._finished_orders)]

    @property
    def lots_open(self) -> int:
        return len(self._open_lots)

    @property
    def lots_done(self) -> int:
        return len(self.lots) - len(self._open_lots)

    def note_lot_finished(self, lot_id: str, t: float) -> None:
        self._lot_end[lot_id] = int(math.ceil(t))
        self._open_lots.pop(lot_id, None)

    def all_lots_finished(self, record: OrderRecord) -> bool:
        return all(lot.id in self._lot_end for lot in record.lots)

    def mark_finished(self, record: OrderRecord, t: float) -> None:
        record.completed_min = t
        if t > record.order.due_min:
            self.finished_late += 1
        self._open_orders.pop(record.order.id, None)
        self._finished_orders.append(record.order.id)

    def order_infos(self, open_lots: Iterable[Lot]) -> list[OrderInfo]:
        ids = {lot.order_id for lot in open_lots}
        infos = []
        for order_id in sorted(ids):
            record = self.orders[order_id]
            ends = [self._lot_end[lot.id] for lot in record.lots if lot.id in self._lot_end]
            infos.append(OrderInfo(
                id=order_id,
                due=int(math.ceil(record.order.due_min)),
                weight=record.order.weight,
                completed_lots_end=max(ends) if ends else None,
            ))
        return infos

    # --- JSON ---

    def status(self, record: OrderRecord, op_state: OpState, delivered: int) -> str:
        states = [op_state(lot.op_id(PACK)) for lot in record.lots]
        if all(state == "done" for state in states):
            return STATUS_DONE if delivered >= record.order.qty_bars else STATUS_SHORT
        started = any(op_state(lot.op_id(kind)) != "pending" for lot in record.lots for kind in LOT_ROUTE)
        return STATUS_ACTIVE if started else STATUS_OPEN

    def lot_row(self, lot: Lot, op_state: OpState, op_machine: Mapping[str, str], planned_machine: Mapping[str, str]) -> dict:
        for kind in LOT_ROUTE:
            op_id = lot.op_id(kind)
            state = op_state(op_id)
            if state == "done":
                continue
            running = state == "running"
            machine = op_machine.get(op_id) if running else planned_machine.get(op_id)
            return {"id": lot.id, "n_billets": lot.n_billets, "stage": kind,
                    "state": LOT_RUNNING if running else LOT_WAITING, "machine": machine}
        return {"id": lot.id, "n_billets": lot.n_billets, "stage": "fertig", "state": LOT_DONE, "machine": None}

    def order_row(self, record: OrderRecord, *, op_state: OpState, delivered: int, planned_completion: int | None,
                  op_machine: Mapping[str, str], planned_machine: Mapping[str, str], now: float | None = None) -> dict:
        order = record.order
        status = self.status(record, op_state, delivered)
        finish = record.completed_min if record.completed_min is not None else planned_completion
        effective = effective_finish(record, planned_completion, now)
        lateness = None if effective is None else round(effective - order.due_min)
        return {
            "id": order.id,
            "product_id": order.product_id,
            "product_name": catalog.PRODUCTS[order.product_id].name,
            "qty_bars": order.qty_bars,
            "due_min": order.due_min,
            "weight": order.weight,
            "created_min": order.created_min,
            "status": status,
            "delivered_pieces": delivered,
            "lots_total": len(record.lots),
            "lots_done": sum(1 for lot in record.lots if op_state(lot.op_id(PACK)) == "done"),
            "planned_completion": finish,
            "lateness_min": lateness,
            "late": effective is not None and effective > order.due_min,
            "completed_min": record.completed_min,
            "lots": [self.lot_row(lot, op_state, op_machine, planned_machine) for lot in record.lots],
        }

    def missing_from(self, problem_has_op: Callable[[str], bool], op_state: OpState) -> list[Lot]:
        """Open lots an active plan knows nothing about and whose press has not started: they need the next plan."""
        return [
            lot for lot in self._open_lots.values()
            if not problem_has_op(lot.op_id(PRESS)) and op_state(lot.op_id(PRESS)) == "pending"
        ]


def effective_finish(record: OrderRecord, planned: float | None, now: float | None) -> float | None:
    """When an order can finish at the earliest: an open order cannot finish in the past, whatever a stale plan says."""
    if record.completed_min is not None:
        return record.completed_min
    if now is None:
        return planned
    return now if planned is None else max(planned, now)


def base_row(record: OrderRecord) -> dict:
    """What the database keeps of an order, enough to rebuild the book after an MES restart."""
    order = record.order
    return {
        "id": order.id, "product_id": order.product_id, "qty_bars": order.qty_bars, "due_min": order.due_min,
        "weight": order.weight, "created_min": order.created_min, "completed_min": record.completed_min,
    }


__all__ = ["OrderBook", "OrderRecord", "base_row", "effective_finish"]
