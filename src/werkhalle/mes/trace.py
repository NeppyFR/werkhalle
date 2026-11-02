"""Genealogy: who became what, built from the plant's trace events (PLAN sections 4 and 9).

The material flow is a DAG with quantities on the edges:

    melt -> billet -> strand -> basket -> package -> order

Two kinds of node sit beside that flow, because they group material without being a step of it:

- A furnace load holds baskets of unrelated lots. Routing the flow through it would make every lot in the load look like an
  ancestor of every other, so it is a side node on its baskets and only expands when it is the query root.
- A lot holds the strands it pressed. A lot can span two melts, so routing the flow through it would make both melts ancestors
  of every strand. It is a side grouping too, and only appears when it is the root.

A query is `ancestors(root) | descendants(root)`, never an undirected closure. That is what keeps a recall from melt 1 away from
a lot of melt 2 that merely shared a furnace with it.

Quantities: melt and billet in Bolzen, strand and basket in Stangen (good bars), package and order in Stück. Balances that hold:
out-edges of a melt sum to its qty; the in-edge of a strand equals its qty; in-edges of a basket and of a package sum to their
qty; out-edges of a basket sum to its good pieces (bars in minus saw scrap); in-edges of an order sum to its qty.
"""
from __future__ import annotations

import json
import sqlite3
from collections import defaultdict
from collections.abc import Iterable, Mapping
from typing import Any

from ..domain import catalog
from .db import RunDb, chunked

NODE_CAP = 400
SEARCH_MAX_CHARS = 100
SEARCH_RECENT_MAX = 1000
SEARCH_LIMIT = 30
RECALL_LABEL = "möglich betroffen"
DIRECTION_NOTE = "Vorfahren und Nachkommen des gewählten Knotens"

NODE_TYPES = ("melt", "billet", "strand", "basket", "load", "package", "order", "lot")

_UNIT_BILLET = "Bolzen"
_UNIT_BAR = "Stangen"
_UNIT_PIECE = "Stück"
_UNIT_BASKET = "Körbe"

_PRODUCT_BY_DIE = {p.die: p for p in catalog.PRODUCTS.values()}

_UPSERT_NODE = """
INSERT INTO nodes(id, type, label, t, qty, unit, attrs, data) VALUES(?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT(id) DO UPDATE SET
    label = COALESCE(excluded.label, label),
    t = COALESCE(excluded.t, t),
    qty = COALESCE(excluded.qty, qty),
    unit = COALESCE(excluded.unit, unit),
    attrs = json_patch(attrs, excluded.attrs),
    data = json_patch(data, excluded.data)
"""
_INSERT_EDGE = "INSERT OR IGNORE INTO edges(src, dst, qty, unit) VALUES(?, ?, ?, ?)"
_INSERT_MEMBER = "INSERT OR IGNORE INTO memberships(group_id, member_id, qty, unit) VALUES(?, ?, ?, ?)"
_INSERT_EVENT = "INSERT OR IGNORE INTO events(seq, t, type, data) VALUES(?, ?, ?, ?)"


def format_de(value: float, decimals: int = 0) -> str:
    """German number format: `1.234,5`."""
    text = f"{value:,.{decimals}f}"
    return text.replace(",", "\0").replace(".", ",").replace("\0", ".")


def format_sim_time(t: float) -> str:
    """UI time format, `Tag 2 · 14:30`. t=0 is Tag 1, 06:00."""
    total = int(t // 1) + 6 * 60
    day, minutes = divmod(total, 24 * 60)
    return f"Tag {day + 1} · {minutes // 60:02d}:{minutes % 60:02d}"


def _with_unit(value: float | None, unit: str, decimals: int = 0) -> str | None:
    return None if value is None else f"{format_de(value, decimals)} {unit}"


def _clean(attrs: Mapping[str, Any]) -> dict[str, Any]:
    # json_patch reads a null as "delete this key".
    return {k: v for k, v in attrs.items() if v is not None}


def _plain_qty(value: float | None) -> float | int | None:
    if value is None:
        return None
    return int(value) if float(value).is_integer() else value


class TraceStore:
    """Consumes trace events on the event loop and answers genealogy queries on any thread.

    `consume` only updates small dicts and queues SQL for the writer thread, so it is safe to call from the OPC UA event pump.
    It ignores an event whose seq is not above `last_seq`, so a repeated delivery changes nothing. Queries read committed data,
    which lags `consume` by up to `RunDb.commit_interval_s`; `flush` closes that gap.

    The in-memory totals (delivered pieces, energy) must only be touched from one thread, the event loop.
    Opening a store on an existing database replays its stored events, so `last_seq` and the totals survive an MES restart.
    """

    def __init__(self, db: RunDb, *, node_cap: int = NODE_CAP) -> None:
        self._db = db
        self.node_cap = node_cap
        self.last_seq = 0
        self._replaying = False

        self._lot_order: dict[str, str] = {}
        self._lots_by_order: dict[str, list[str]] = {}
        self._lots_by_order_size = -1
        self._lot_product: dict[str, catalog.Product] = {}
        self._lot_started: set[str] = set()
        self._lot_bars: dict[str, int] = defaultdict(int)
        self._lot_baskets: dict[str, set[str]] = defaultdict(set)
        self._lot_pieces: dict[str, int] = defaultdict(int)
        self._lot_melt_billets: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
        self._lot_kwh: dict[str, float] = defaultdict(float)
        self._plan_kwh: dict[str, float] = defaultdict(float)
        self._melt_plan: dict[str, str] = {}
        self._melt_billets: dict[str, int] = {}
        self._order_seen: set[str] = set()
        self._order_pieces: dict[str, int] = defaultdict(int)
        self._order_kg: dict[str, float] = defaultdict(float)
        self._pending_packages: dict[str, list[tuple[str, int, float | None]]] = defaultdict(list)

        self._handlers = {
            "melt_cast": self._on_melt_cast,
            "melt_arrived": self._on_melt_arrived,
            "billet_extruded": self._on_billet_extruded,
            "baskets_filled": self._on_baskets_filled,
            "furnace_load_done": self._on_furnace_load_done,
            "sawn": self._on_sawn,
            "packed": self._on_packed,
            "op_finished": self._on_op_finished,
        }
        self._restore()

    # --- consuming events ---

    def consume(self, event: Mapping[str, Any]) -> bool:
        """Apply one trace event. Returns False when its seq was already applied, and then changes nothing."""
        seq = event["seq"]
        if seq <= self.last_seq:
            return False
        self.last_seq = seq
        if not self._replaying:
            self._db.submit(_INSERT_EVENT, (seq, event.get("t"), event["type"], json.dumps(event, ensure_ascii=False)))
        handler = self._handlers.get(event["type"])
        if handler is not None:
            handler(event)
        return True

    def consume_many(self, events: Iterable[Mapping[str, Any]]) -> int:
        """Apply events in order. Returns how many were new."""
        return sum(1 for event in events if self.consume(event))

    def flush(self, timeout: float = 30.0) -> bool:
        """Block until everything consumed so far is visible to queries. Not for the event loop in production paths."""
        return self._db.flush(timeout)

    def register_order(self, order_id: str, product_id: str | None = None, qty_bars: int | None = None,
                       due_min: float | None = None, created_min: float | None = None) -> None:
        """Make an order searchable from its creation, before any material exists for it. Safe to repeat."""
        product = catalog.PRODUCTS.get(product_id) if product_id else None
        self._order_seen.add(order_id)
        self._node(order_id, "order", f"Auftrag {order_id}", t=created_min, unit=_UNIT_PIECE, attrs={
            "Produkt": product.name if product else product_id,
            "Bestellt": _with_unit(qty_bars, _UNIT_BAR),
            "Liefertermin": format_sim_time(due_min) if due_min is not None else None,
        })

    def register_lot(self, lot_id: str, order_id: str, product_id: str | None = None, n_billets: int | None = None,
                     t: float | None = None) -> None:
        """Tell the store which order and product a lot belongs to, ahead of its first event. Safe to repeat."""
        self._lot_order[lot_id] = order_id
        product = catalog.PRODUCTS.get(product_id) if product_id else None
        if product is not None:
            self._lot_product[lot_id] = product
        self._node(lot_id, "lot", f"Los {lot_id}", t=t, unit=_UNIT_BAR, attrs={
            "Auftrag": order_id,
            "Produkt": product.name if product else product_id,
            "Bolzen geplant": n_billets,
        }, data={"order_id": order_id, "product_id": product_id})
        self._attach_pending_packages(lot_id)

    def _node(self, node_id: str, node_type: str, label: str | None = None, *, t: float | None = None,
              qty: float | None = None, unit: str | None = None, attrs: Mapping[str, Any] | None = None,
              data: Mapping[str, Any] | None = None) -> None:
        if self._replaying:
            return
        self._db.submit(_UPSERT_NODE, (
            node_id, node_type, label, t, qty, unit,
            json.dumps(_clean(attrs or {}), ensure_ascii=False),
            json.dumps(_clean(data or {}), ensure_ascii=False),
        ))

    def _edge(self, src: str, dst: str, qty: float, unit: str) -> None:
        if not self._replaying:
            self._db.submit(_INSERT_EDGE, (src, dst, qty, unit))

    def _member(self, group_id: str, member_id: str, qty: float, unit: str) -> None:
        if not self._replaying:
            self._db.submit(_INSERT_MEMBER, (group_id, member_id, qty, unit))

    def _on_melt_cast(self, ev: Mapping[str, Any]) -> None:
        melt_id, alloy, billets = ev["melt_id"], ev["alloy"], ev["billets"]
        plan_id = ev.get("plan_id")
        self._melt_plan[melt_id] = plan_id
        self._melt_billets[melt_id] = len(billets)
        t = ev.get("t")
        temperature = _with_unit(ev.get("temp_c"), "°C")
        t_start = ev.get("t_start")
        initial_stock = plan_id == catalog.INITIAL_MELT_PLAN_ID
        self._node(melt_id, "melt", f"Schmelze {melt_id} · {alloy}", t=t, qty=len(billets), unit=_UNIT_BILLET, attrs={
            "Legierung": alloy,
            "Plan": "Anfangsbestand" if initial_stock else plan_id,
            "Bolzen": len(billets),
            "Temperatur": temperature,
            "Anlage": ev.get("machine"),
            "Gussbeginn": format_sim_time(t_start) if t_start is not None and not initial_stock else None,
            "Gussende": format_sim_time(t) if t is not None and not initial_stock else None,
        }, data={"alloy": alloy, "plan_id": plan_id})
        for billet_id in billets:
            self._node(billet_id, "billet", f"Bolzen {billet_id}", t=t, qty=1, unit=_UNIT_BILLET, attrs={
                "Legierung": alloy,
                "Schmelze": melt_id,
                "Temperatur": temperature,
            })
            self._edge(melt_id, billet_id, 1, _UNIT_BILLET)

    def _on_melt_arrived(self, ev: Mapping[str, Any]) -> None:
        melt_id, alloy = ev["melt_id"], ev.get("alloy")
        label = f"Schmelze {melt_id} · {alloy}" if alloy else f"Schmelze {melt_id}"
        self._node(melt_id, "melt", label, unit=_UNIT_BILLET, attrs={
            "Im Lager seit": format_sim_time(ev["t"]) if ev.get("t") is not None else None,
        })

    def _on_billet_extruded(self, ev: Mapping[str, Any]) -> None:
        billet_id, melt_id, strand_id = ev["billet_id"], ev["melt_id"], ev["strand_id"]
        lot_id, order_id = ev["lot_id"], ev.get("order_id")
        bars_good = ev["bars_good"]
        t = ev.get("t")
        temperature = _with_unit(ev.get("temp_c"), "°C")
        product = _PRODUCT_BY_DIE.get(ev.get("die"))
        if product is not None:
            self._lot_product.setdefault(lot_id, product)
        product = self._lot_product.get(lot_id)
        if order_id:
            self._lot_order[lot_id] = order_id
            self._ensure_order(order_id, t, product)

        # The event names the melt, so a billet still resolves to it when its melt_cast fell out of the plant's event ring.
        if melt_id not in self._melt_billets:
            self._node(melt_id, "melt", f"Schmelze {melt_id}", unit=_UNIT_BILLET)
        self._edge(melt_id, billet_id, 1, _UNIT_BILLET)
        self._node(billet_id, "billet", f"Bolzen {billet_id}", qty=1, unit=_UNIT_BILLET, attrs={
            "Schmelze": melt_id,
            "Los": lot_id,
            "Presse": ev.get("machine"),
        })
        self._node(strand_id, "strand", f"Strang {strand_id}", t=t, qty=bars_good, unit=_UNIT_BAR, attrs={
            "Schmelze": melt_id,
            "Bolzen": billet_id,
            "Los": lot_id,
            "Auftrag": order_id,
            "Presse": ev.get("machine"),
            "Matrize": ev.get("die"),
            "Temperatur": temperature,
            "Stangen gesamt": ev.get("bars_total"),
            "Stangen gut": bars_good,
            "Stangen Ausschuss": ev.get("bars_scrap"),
        })
        self._edge(billet_id, strand_id, bars_good, _UNIT_BAR)

        self._lot_bars[lot_id] += bars_good
        self._lot_melt_billets[lot_id][melt_id] += 1
        first = lot_id not in self._lot_started
        self._lot_started.add(lot_id)
        self._node(lot_id, "lot", f"Los {lot_id}", t=t if first else None, qty=self._lot_bars[lot_id], unit=_UNIT_BAR, attrs={
            "Auftrag": order_id,
            "Produkt": product.name if product else None,
        }, data={"order_id": order_id, "product_id": product.id if product else None})
        self._member(lot_id, strand_id, bars_good, _UNIT_BAR)
        self._attach_pending_packages(lot_id)

    def _ensure_order(self, order_id: str, t: float | None, product: catalog.Product | None) -> None:
        if order_id in self._order_seen:
            return
        self._order_seen.add(order_id)
        self._node(order_id, "order", f"Auftrag {order_id}", t=t, unit=_UNIT_PIECE, attrs={
            "Produkt": product.name if product else None,
        })

    def _on_baskets_filled(self, ev: Mapping[str, Any]) -> None:
        lot_id = ev["lot_id"]
        t = ev.get("t")
        for basket in ev["baskets"]:
            self._lot_baskets[lot_id].add(basket["id"])
            self._node(basket["id"], "basket", f"Korb {basket['id']}", t=t, qty=basket["bars"], unit=_UNIT_BAR, attrs={
                "Los": lot_id,
                "Presse": ev.get("machine"),
                "Stangen": basket["bars"],
            })
            for strand in basket["strands"]:
                self._edge(strand["id"], basket["id"], strand["bars"], _UNIT_BAR)

    def _on_furnace_load_done(self, ev: Mapping[str, Any]) -> None:
        load_id, baskets = ev["load_id"], ev["baskets"]
        t_start = ev.get("t_start")
        self._node(load_id, "load", f"Ofenlast {load_id}", t=ev.get("t"), qty=len(baskets), unit=_UNIT_BASKET, attrs={
            "Ofen": ev.get("machine"),
            "Rezept": ev.get("recipe"),
            "Charge": ev.get("batch_id"),
            "Lose": ", ".join(ev.get("lot_ids", [])),
            "Körbe": len(baskets),
            "Haltetemperatur": _with_unit(ev.get("soak_temp_avg"), "°C", 1),
            "Haltezeit": _with_unit(ev.get("soak_min"), "min"),
            "Zeit im Band": _with_unit(ev.get("in_band_min"), "min"),
            "Störzeit": _with_unit(ev.get("fault_min"), "min"),
            "Beginn": format_sim_time(t_start) if t_start is not None else None,
            "Ende": format_sim_time(ev["t"]) if ev.get("t") is not None else None,
        })
        for basket_id in baskets:
            self._member(load_id, basket_id, 1, _UNIT_BASKET)
            self._node(basket_id, "basket", f"Korb {basket_id}", attrs={
                "Ofenlast": load_id,
                "Rezept": ev.get("recipe"),
            })

    def _on_sawn(self, ev: Mapping[str, Any]) -> None:
        for basket in ev["baskets"]:
            self._node(basket["id"], "basket", f"Korb {basket['id']}", attrs={
                "Säge": ev.get("machine"),
                "Stück gut": basket["pieces_good"],
                "Stück Ausschuss": basket["pieces_scrap"],
                "Länge": _with_unit(ev.get("length_mm"), "mm"),
            }, data={
                "bars_in": basket["bars_in"],
                "pieces_good": basket["pieces_good"],
                "pieces_scrap": basket["pieces_scrap"],
            })

    def _on_packed(self, ev: Mapping[str, Any]) -> None:
        lot_id = ev["lot_id"]
        product = self._lot_product.get(lot_id)
        t = ev.get("t")
        for package in ev["packages"]:
            package_id, pieces = package["id"], package["pieces"]
            kg = pieces * product.kg_per_bar if product is not None else None
            self._lot_pieces[lot_id] += pieces
            self._node(package_id, "package", f"Paket {package_id}", t=t, qty=pieces, unit=_UNIT_PIECE, attrs={
                "Los": lot_id,
                "Anlage": ev.get("machine"),
                "Stück": pieces,
                "Gewicht": _with_unit(kg, "kg", 1),
                "Produkt": product.name if product else None,
            }, data={"lot_id": lot_id, "kg": kg})
            for basket in package["baskets"]:
                self._edge(basket["id"], package_id, basket["pieces"], _UNIT_PIECE)
            self._link_package(package_id, pieces, kg, lot_id)

    def _link_package(self, package_id: str, pieces: int, kg: float | None, lot_id: str) -> None:
        order_id = self._lot_order.get(lot_id)
        if order_id is None:
            # Only possible after an event gap; the next register_lot or billet of this lot closes it.
            self._pending_packages[lot_id].append((package_id, pieces, kg))
            return
        self._ensure_order(order_id, None, self._lot_product.get(lot_id))
        self._order_pieces[order_id] += pieces
        if kg is not None:
            self._order_kg[order_id] += kg
        self._edge(package_id, order_id, pieces, _UNIT_PIECE)
        self._node(package_id, "package", attrs={"Auftrag": order_id})
        self._node(order_id, "order", qty=self._order_pieces[order_id], unit=_UNIT_PIECE, attrs={
            "Geliefert": _with_unit(self._order_pieces[order_id], _UNIT_PIECE),
        })

    def _attach_pending_packages(self, lot_id: str) -> None:
        for package_id, pieces, kg in self._pending_packages.pop(lot_id, ()):
            self._link_package(package_id, pieces, kg, lot_id)

    def _on_op_finished(self, ev: Mapping[str, Any]) -> None:
        kwh = float(ev.get("energy_kwh") or 0.0)
        lot_ids = list(ev.get("lot_ids") or ())
        if lot_ids:
            # A furnace burns for the whole load, so each lot pays for the baskets it occupied.
            weights = [len(self._lot_baskets.get(lot_id, ())) for lot_id in lot_ids]
            if sum(weights) <= 0:
                weights = [1] * len(lot_ids)
            total = sum(weights)
            for lot_id, weight in zip(lot_ids, weights):
                self._lot_kwh[lot_id] += kwh * weight / total
            return
        op_ids = list(ev.get("op_ids") or ())
        for op_id in op_ids:
            self._plan_kwh[op_id.split(":", 1)[0]] += kwh / len(op_ids)

    # --- totals for the order book and /api/energy, event loop only ---

    def delivered_pieces(self, order_id: str) -> int:
        """Sum of packed pieces of an order. The order status compares this against `qty_bars`."""
        return self._order_pieces.get(order_id, 0)

    def lot_pieces(self, lot_id: str) -> int:
        """Sum of packed pieces of a lot."""
        return self._lot_pieces.get(lot_id, 0)

    def order_energy_kwh(self, order_id: str) -> float:
        """Energy attributed to an order: its lots' ops, plus each melt's cast energy per billet the lot consumed."""
        total = 0.0
        for lot_id in self._lots_of_order().get(order_id, ()):
            total += self._lot_kwh.get(lot_id, 0.0)
            for melt_id, n_billets in self._lot_melt_billets.get(lot_id, {}).items():
                melt_total = self._melt_billets.get(melt_id, 0)
                if melt_total > 0:
                    total += self._plan_kwh.get(self._melt_plan.get(melt_id, ""), 0.0) * n_billets / melt_total
        return total

    def _lots_of_order(self) -> dict[str, list[str]]:
        # Rebuilt only when a lot came in, so that many orders do not cost a walk over all lots each.
        if self._lots_by_order_size != len(self._lot_order):
            lots: dict[str, list[str]] = defaultdict(list)
            for lot_id, owner in self._lot_order.items():
                lots[owner].append(lot_id)
            self._lots_by_order = dict(lots)
            self._lots_by_order_size = len(self._lot_order)
        return self._lots_by_order

    def energy_by_order(self, order_ids=None, *, finished=None) -> list[dict]:
        """The `by_order` rows of `GET /api/energy`: `{order_id, energy_kwh, kg, kwh_per_kg}`, by order id.

        `order_ids` limits the rows. `kwh_per_kg` is None until the order has shipped something and, when `finished` is given, until
        it is in there: the energy of all its lots over the kilos of the first one shipped would read far too high.
        """
        rows = []
        for order_id in sorted(set(self._lot_order.values()) if order_ids is None else set(order_ids)):
            kwh = self.order_energy_kwh(order_id)
            kg = self._order_kg.get(order_id, 0.0)
            rows.append({
                "order_id": order_id,
                "energy_kwh": round(kwh, 2),
                "kg": round(kg, 2),
                "kwh_per_kg": round(kwh / kg, 3) if kg > 0 and (finished is None or order_id in finished) else None,
            })
        return rows

    def _restore(self) -> None:
        self._replaying = True
        try:
            for row in self._db.query("SELECT id, data FROM nodes WHERE type = 'lot'"):
                data = json.loads(row["data"])
                if data.get("order_id"):
                    self._lot_order[row["id"]] = data["order_id"]
                product = catalog.PRODUCTS.get(data.get("product_id") or "")
                if product is not None:
                    self._lot_product[row["id"]] = product
            for row in self._db.query("SELECT data FROM events ORDER BY seq"):
                self.consume(json.loads(row["data"]))
        finally:
            self._replaying = False

    # --- queries, thread-safe, reading committed data ---

    def node(self, node_id: str) -> dict | None:
        """One node as in the graph JSON, without `relation`, or None."""
        rows = self._db.query("SELECT * FROM nodes WHERE id = ?", (node_id,))
        return _node_json(rows[0]) if rows else None

    def search(self, q: str, limit: int = SEARCH_LIMIT) -> dict:
        """`GET /api/trace/search`: nodes whose id contains `q`, case-insensitive, prefix matches first, at most 30."""
        needle = q.strip()[:SEARCH_MAX_CHARS]
        if not needle:
            return {"results": []}
        escaped = needle.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        rows = self._db.query(
            """SELECT id, type, label, t FROM nodes WHERE id LIKE ? ESCAPE '\\'
               ORDER BY CASE WHEN id LIKE ? ESCAPE '\\' THEN 0 ELSE 1 END, id LIMIT ?""",
            (f"%{escaped}%", f"{escaped}%", min(limit, SEARCH_LIMIT)),
        )
        return {"results": [_search_row(r) for r in rows]}

    def recent(self, limit: int = 20) -> dict:
        """`GET /api/trace/recent`: newest packages and melts first."""
        rows = self._db.query(
            """SELECT id, type, label, t FROM nodes WHERE type IN ('package', 'melt')
               ORDER BY t DESC, id DESC LIMIT ?""",
            (max(0, min(limit, SEARCH_RECENT_MAX)),),
        )
        return {"results": [_search_row(r) for r in rows]}

    def graph(self, root_id: str) -> dict | None:
        """`GET /api/trace/graph/{id}`, or None when the id is unknown.

        Traversal follows edge direction only: ancestors and descendants of the root. A load or lot root starts from its
        members, so `load` recalls every lot in it. `summary` always covers the full traversal, also when `truncated` says the
        node list stopped at the cap, because a recall figure must not depend on what fits on screen.
        """
        with self._db.read_transaction() as conn:
            root = conn.execute("SELECT * FROM nodes WHERE id = ?", (root_id,)).fetchone()
            if root is None:
                return None
            root_type = root["type"]
            group_edges: list[dict] = []
            if root_type in ("load", "lot"):
                members = conn.execute(
                    "SELECT member_id, qty, unit FROM memberships WHERE group_id = ? ORDER BY member_id", (root_id,)
                ).fetchall()
                starts = [m["member_id"] for m in members]
                descendants = starts + _walk(conn, starts, "src", "dst")
                ancestors = _walk(conn, starts, "dst", "src")
                group_edges = [{"from": root_id, "to": m["member_id"], "qty": _plain_qty(m["qty"]), "unit": m["unit"],
                                "side": True} for m in members]
            else:
                descendants = _walk(conn, [root_id], "src", "dst")
                ancestors = _walk(conn, [root_id], "dst", "src")

            closure = [root_id, *ancestors, *descendants]
            rows = _fetch_nodes(conn, closure)
            baskets = [i for i in closure if i in rows and rows[i]["type"] == "basket"]
            side_edges = _side_edges(conn, baskets) if root_type != "load" else []
            load_ids = {e["to"] for e in side_edges} | ({root_id} if root_type == "load" else set())
            load_rows = _fetch_nodes(conn, sorted(load_ids - set(closure)))

            summary = _summary(closure, rows, sorted(load_ids))
            selected, truncated = self._select(root_id, ancestors, descendants, load_rows)
            relation = {root_id: "root"}
            relation.update({i: "ancestor" for i in ancestors})
            relation.update({i: "descendant" for i in descendants})
            relation.update({i: "side" for i in load_rows})

            all_rows = {**rows, **load_rows}
            nodes = [{**_node_json(all_rows[i]), "relation": relation[i]} for i in selected if i in all_rows]
            edges = _flow_edges(conn, selected)
            chosen = set(selected)
            edges += [e for e in group_edges + side_edges if e["from"] in chosen and e["to"] in chosen]

        return {
            "root": root_id,
            "root_type": root_type,
            "truncated": truncated,
            "direction_note": DIRECTION_NOTE,
            "nodes": nodes,
            "edges": edges,
            "summary": summary,
            "recall_label": RECALL_LABEL,
        }

    def _select(self, root_id: str, ancestors: list[str], descendants: list[str],
                side_rows: Mapping[str, Any]) -> tuple[list[str], bool]:
        """Which ids make the node list: nearest first on both sides, so a cut keeps the neighbourhood of the root."""
        selected = [root_id]
        seen = {root_id}
        queues = [list(reversed(descendants)), list(reversed(ancestors))]
        while len(selected) < self.node_cap and any(queues):
            for pending in queues:
                if pending and len(selected) < self.node_cap:
                    node_id = pending.pop()
                    if node_id not in seen:
                        seen.add(node_id)
                        selected.append(node_id)
        for node_id in side_rows:
            if len(selected) >= self.node_cap:
                break
            selected.append(node_id)
            seen.add(node_id)
        total = len({root_id, *ancestors, *descendants, *side_rows})
        return selected, total > len(selected)


def _walk(conn: sqlite3.Connection, starts: Iterable[str], from_col: str, to_col: str) -> list[str]:
    """Breadth-first along edges in one direction. Returns reached ids, nearest first, without the starts."""
    seen = set(starts)
    frontier = list(seen)
    reached: list[str] = []
    while frontier:
        following: list[str] = []
        for chunk in chunked(sorted(frontier)):
            marks = ",".join("?" * len(chunk))
            for row in conn.execute(f"SELECT {to_col} AS next_id FROM edges WHERE {from_col} IN ({marks})", chunk):
                next_id = row["next_id"]
                if next_id not in seen:
                    seen.add(next_id)
                    following.append(next_id)
        following.sort()
        reached.extend(following)
        frontier = following
    return reached


def _fetch_nodes(conn: sqlite3.Connection, ids: Iterable[str]) -> dict[str, sqlite3.Row]:
    rows: dict[str, sqlite3.Row] = {}
    for chunk in chunked(ids):
        marks = ",".join("?" * len(chunk))
        for row in conn.execute(f"SELECT * FROM nodes WHERE id IN ({marks})", chunk):
            rows[row["id"]] = row
    return rows


def _side_edges(conn: sqlite3.Connection, baskets: Iterable[str]) -> list[dict]:
    """Edges from baskets to their furnace load. Strand memberships are never selected, because members here are baskets."""
    edges = []
    for chunk in chunked(baskets):
        marks = ",".join("?" * len(chunk))
        query = f"SELECT group_id, member_id, qty, unit FROM memberships WHERE member_id IN ({marks}) ORDER BY member_id"
        for row in conn.execute(query, chunk):
            edges.append({"from": row["member_id"], "to": row["group_id"], "qty": _plain_qty(row["qty"]),
                          "unit": row["unit"], "side": True})
    return edges


def _flow_edges(conn: sqlite3.Connection, selected: Iterable[str]) -> list[dict]:
    chosen = set(selected)
    edges = []
    for chunk in chunked(sorted(chosen)):
        marks = ",".join("?" * len(chunk))
        for row in conn.execute(f"SELECT src, dst, qty, unit FROM edges WHERE src IN ({marks}) ORDER BY src, dst", chunk):
            if row["dst"] in chosen:
                edges.append({"from": row["src"], "to": row["dst"], "qty": _plain_qty(row["qty"]), "unit": row["unit"]})
    return edges


def _summary(closure: Iterable[str], rows: Mapping[str, sqlite3.Row], loads: list[str]) -> dict:
    packages = pieces = 0
    kg = 0.0
    orders: list[str] = []
    melts: list[str] = []
    for node_id in closure:
        row = rows.get(node_id)
        if row is None:
            continue
        if row["type"] == "package":
            packages += 1
            pieces += int(row["qty"] or 0)
            kg += json.loads(row["data"]).get("kg") or 0.0
        elif row["type"] == "order":
            orders.append(node_id)
        elif row["type"] == "melt":
            melts.append(node_id)
    return {
        "packages": packages,
        "orders": sorted(orders),
        "pieces": pieces,
        "melts": sorted(melts),
        "loads": loads,
        "kg": round(kg, 2),
    }


def _node_json(row: sqlite3.Row) -> dict:
    qty = _plain_qty(row["qty"])
    return {
        "id": row["id"],
        "type": row["type"],
        "label": row["label"],
        "t": row["t"],
        "qty": qty if qty is not None else 0,
        "unit": row["unit"],
        "attrs": json.loads(row["attrs"]),
    }


def _search_row(row: sqlite3.Row) -> dict:
    return {"id": row["id"], "type": row["type"], "label": row["label"], "t": row["t"]}


__all__ = ["DIRECTION_NOTE", "NODE_CAP", "NODE_TYPES", "RECALL_LABEL", "SEARCH_LIMIT", "TraceStore", "format_de",
           "format_sim_time"]
