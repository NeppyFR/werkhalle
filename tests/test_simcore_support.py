"""Shared by the simcore tests: a small FIFO controller and the conservation check on trace events."""
from __future__ import annotations

from collections import Counter, defaultdict

from werkhalle.domain import catalog as cat
from werkhalle.domain.orders import Lot
from werkhalle.simcore import SimEngine

RUN_ID = "r-test"


def press_cmd(lot: str, product: str, n: int, machine: str, order: str = "A-1") -> dict:
    return {
        "machine": machine,
        "run_id": RUN_ID,
        "op_ids": [f"{lot}:press"],
        "kind": "press",
        "lot_id": lot,
        "order_id": order,
        "product_id": product,
        "n_billets": n,
    }


def cast_cmd(plan_id: str, alloy: str) -> dict:
    return {
        "machine": "GUSS",
        "run_id": RUN_ID,
        "op_ids": [f"{plan_id}:cast"],
        "kind": "cast",
        "melt_plan_id": plan_id,
        "alloy": alloy,
    }


def age_cmd(lots: list[str], recipe: str, machine: str, batch: str = "B-1") -> dict:
    return {
        "machine": machine,
        "run_id": RUN_ID,
        "op_ids": [f"{lot}:age" for lot in lots],
        "kind": "age",
        "batch_id": batch,
        "recipe": recipe,
        "lot_ids": list(lots),
    }


def saw_cmd(lot: str) -> dict:
    return {"machine": "SAE", "run_id": RUN_ID, "op_ids": [f"{lot}:saw"], "kind": "saw", "lot_id": lot}


def pack_cmd(lot: str) -> dict:
    return {"machine": "VP", "run_id": RUN_ID, "op_ids": [f"{lot}:pack"], "kind": "pack", "lot_id": lot}


def make_lots(n: int, products: tuple[str, ...] = ("FR-60", "KK-80", "ST-200", "FP-120", "LS-40", "CB-90")) -> list[Lot]:
    sizes = (8, 8, 5, 8, 3, 8, 7)
    return [
        Lot(f"L-{i:05d}", f"A-{1000 + i}", products[i % len(products)], sizes[i % len(sizes)])
        for i in range(1, n + 1)
    ]


class FifoDriver:
    """Starts whatever can start, oldest first.

    Its only state is the engine plus the static lot list, so it can pick up on any fork. Cast decisions compare total
    supply with total demand, so it cannot deadlock a press: that is what the melt rule of PLAN section 5 is for.
    """

    def __init__(self, engine: SimEngine, lots: list[Lot]):
        self.engine = engine
        self.lots = lots
        self.run_id = engine.run_id
        state = engine.planning_state()
        self.started = set(state.started_op_ids)
        self.batch_no = sum(1 for op_id in self.started if op_id.endswith(":age"))
        self._cast_count: Counter[str] = Counter(
            op_id.split("-")[1] for op_id in self.started if op_id.endswith(":cast")
        )
        self._demand: Counter[str] = Counter()
        for lot in lots:
            self._demand[lot.product.alloy] += lot.n_billets
        self._lots_by_id = {lot.id: lot for lot in lots}
        self._pending_press = [lot for lot in lots if lot.op_id("press") not in self.started]

    def done(self) -> bool:
        return self.engine.material_view()["buffers"]["SHIP"]["lots_total"] >= len(self.lots)

    def _start(self, cmd: dict) -> None:
        result = self.engine.start_operation(cmd)
        assert result == {"ok": True, "reason": ""}, (cmd, result, self.engine.t)
        self.started.update(cmd["op_ids"])

    def step(self) -> None:
        engine = self.engine
        for machine_id in cat.MACHINE_ORDER:
            if engine.machine_view(machine_id)["Status"] != "IDLE":
                continue
            kind = cat.MACHINES[machine_id].kind
            if kind == cat.CAST:
                self._try_cast()
            elif kind == cat.PRESS:
                self._try_press(machine_id)
            elif kind == cat.AGE:
                self._try_age(machine_id)
            elif kind == cat.SAW:
                self._try_saw()
            else:
                self._try_pack()

    def _try_cast(self) -> None:
        for alloy in cat.ALLOYS:
            supply = cat.INITIAL_BILLET_STOCK[alloy] + cat.BILLETS_PER_MELT * self._cast_count[alloy]
            if supply < self._demand[alloy]:
                self._cast_count[alloy] += 1
                self._start(
                    {
                        "machine": "GUSS",
                        "run_id": self.run_id,
                        "op_ids": [f"M-{alloy}-{self._cast_count[alloy]:05d}:cast"],
                        "kind": "cast",
                        "melt_plan_id": f"M-{alloy}-{self._cast_count[alloy]:05d}",
                        "alloy": alloy,
                    }
                )
                return

    def _try_press(self, machine_id: str) -> None:
        store = self.engine.material_view()["buffers"]["BILLET_STORE"]
        for lot in self._pending_press:
            product = lot.product
            if machine_id in product.press_min_per_billet and store[product.alloy] >= lot.n_billets:
                self._pending_press.remove(lot)
                self._start(
                    {
                        "machine": machine_id,
                        "run_id": self.run_id,
                        "op_ids": [lot.op_id("press")],
                        "kind": "press",
                        "lot_id": lot.id,
                        "order_id": lot.order_id,
                        "product_id": lot.product_id,
                        "n_billets": lot.n_billets,
                    }
                )
                return

    def _try_age(self, machine_id: str) -> None:
        rows = self.engine.material_view()["buffers"]["AGE_IN"]
        if not rows:
            return
        recipe = self._lots_by_id[rows[0]["lot_id"]].product.recipe
        chosen: list[str] = []
        room = cat.MACHINES[machine_id].capacity
        for row in rows:
            if self._lots_by_id[row["lot_id"]].product.recipe == recipe and row["baskets"] <= room:
                chosen.append(row["lot_id"])
                room -= row["baskets"]
        if chosen:
            self.batch_no += 1
            self._start(
                {
                    "machine": machine_id,
                    "run_id": self.run_id,
                    "op_ids": [f"{lot_id}:age" for lot_id in chosen],
                    "kind": "age",
                    "batch_id": f"B-t-{self.batch_no}",
                    "recipe": recipe,
                    "lot_ids": chosen,
                }
            )

    def _try_saw(self) -> None:
        rows = self.engine.material_view()["buffers"]["SAW_IN"]
        if rows:
            lot_id = rows[0]["lot_id"]
            self._start(
                {"machine": "SAE", "run_id": self.run_id, "op_ids": [f"{lot_id}:saw"], "kind": "saw", "lot_id": lot_id}
            )

    def _try_pack(self) -> None:
        rows = self.engine.material_view()["buffers"]["PACK_IN"]
        if rows:
            lot_id = rows[0]["lot_id"]
            self._start(
                {"machine": "VP", "run_id": self.run_id, "op_ids": [f"{lot_id}:pack"], "kind": "pack", "lot_id": lot_id}
            )

    def run(self, horizon: float = 1_000_000.0) -> None:
        """Run until every lot has arrived at SHIP. Fails on a stall or a horizon hit."""
        engine = self.engine
        while not self.done():
            self.step()
            next_time = engine.next_event_time()
            assert next_time is not None, f"stalled at t={engine.t}"
            assert next_time <= horizon, f"horizon hit at t={engine.t}"
            engine.run_until(next_time)

    def run_until_time(self, until: float) -> None:
        engine = self.engine
        while engine.t < until and not self.done():
            self.step()
            next_time = engine.next_event_time()
            if next_time is None or next_time > until:
                engine.run_until(until)
                return
            engine.run_until(next_time)


def assert_conserved(events: list[dict], lots: list[Lot], engine: SimEngine | None = None, *, complete: bool = False) -> None:
    """Every quantity on a trace edge must balance. `complete` also requires that all lots are through."""
    casts = [e for e in events if e["type"] == "melt_cast"]
    melt_of_billet: dict[str, str] = {}
    melt_alloy: dict[str, str] = {}
    for cast in casts:
        melt_alloy[cast["melt_id"]] = cast["alloy"]
        for billet in cast["billets"]:
            assert billet not in melt_of_billet, f"{billet} cast twice"
            melt_of_billet[billet] = cast["melt_id"]

    lots_by_id = {lot.id: lot for lot in lots}
    strands: dict[str, dict] = {}
    extruded_billets: set[str] = set()
    for event in (e for e in events if e["type"] == "billet_extruded"):
        billet = event["billet_id"]
        assert billet in melt_of_billet, f"{billet} has no melt node"
        assert melt_of_billet[billet] == event["melt_id"], billet
        assert billet not in extruded_billets, f"{billet} extruded twice"
        extruded_billets.add(billet)
        lot = lots_by_id[event["lot_id"]]
        assert melt_alloy[event["melt_id"]] == lot.product.alloy, "wrong alloy on the press"
        assert event["bars_total"] == lot.product.bars_per_billet
        assert event["bars_good"] + event["bars_scrap"] == event["bars_total"]
        assert event["die"] == lot.product.die
        strands[event["strand_id"]] = event

    baskets: dict[str, dict] = {}
    basket_lot: dict[str, str] = {}
    strand_seen: Counter[str] = Counter()
    for event in (e for e in events if e["type"] == "baskets_filled"):
        product = lots_by_id[event["lot_id"]].product
        for basket in event["baskets"]:
            assert basket["id"] not in baskets
            assert 1 <= len(basket["strands"]) <= product.strands_per_basket, "whole-strand rule"
            assert sum(s["bars"] for s in basket["strands"]) == basket["bars"], "strand bars into basket"
            for strand in basket["strands"]:
                strand_seen[strand["id"]] += 1
                assert strands[strand["id"]]["bars_good"] == strand["bars"]
                assert strands[strand["id"]]["lot_id"] == event["lot_id"]
            baskets[basket["id"]] = basket
            basket_lot[basket["id"]] = event["lot_id"]
    assert all(count == 1 for count in strand_seen.values()), "a strand straddles baskets"

    lot_baskets: dict[str, list[str]] = defaultdict(list)
    for basket_id, lot_id in basket_lot.items():
        lot_baskets[lot_id].append(basket_id)

    furnace_baskets: Counter[str] = Counter()
    for event in (e for e in events if e["type"] == "furnace_load_done"):
        expected = sorted(b for lot_id in event["lot_ids"] for b in lot_baskets[lot_id])
        assert sorted(event["baskets"]) == expected, "a load must hold all baskets of its lots"
        assert len(event["baskets"]) <= cat.MACHINES[event["machine"]].capacity
        furnace_baskets.update(event["baskets"])
        assert all(lots_by_id[lot_id].product.recipe == event["recipe"] for lot_id in event["lot_ids"])
    assert all(count == 1 for count in furnace_baskets.values())

    sawn_pieces: dict[str, int] = {}
    sawn_lots: dict[str, dict] = {}
    for event in (e for e in events if e["type"] == "sawn"):
        assert event["lot_id"] not in sawn_lots
        sawn_lots[event["lot_id"]] = event
        assert sorted(row["id"] for row in event["baskets"]) == sorted(lot_baskets[event["lot_id"]])
        for row in event["baskets"]:
            assert row["id"] in furnace_baskets, "sawn before ageing"
            assert row["bars_in"] == baskets[row["id"]]["bars"], "basket bars into saw"
            assert row["pieces_good"] + row["pieces_scrap"] == row["bars_in"]
            sawn_pieces[row["id"]] = row["pieces_good"]
        assert sum(row["bars_in"] for row in event["baskets"]) == event["bars_in"]
        assert sum(row["pieces_good"] for row in event["baskets"]) == event["pieces_good"]
        assert sum(row["pieces_scrap"] for row in event["baskets"]) == event["pieces_scrap"]
        assert event["length_mm"] == lots_by_id[event["lot_id"]].product.length_mm

    packed_from_basket: Counter[str] = Counter()
    packed_lots: set[str] = set()
    package_ids: set[str] = set()
    packed_total = 0
    for event in (e for e in events if e["type"] == "packed"):
        assert event["lot_id"] not in packed_lots
        packed_lots.add(event["lot_id"])
        per_package = lots_by_id[event["lot_id"]].product.bars_per_package
        for package in event["packages"]:
            assert package["id"] not in package_ids
            package_ids.add(package["id"])
            assert 1 <= package["pieces"] <= per_package
            assert sum(b["pieces"] for b in package["baskets"]) == package["pieces"], "basket pieces into package"
            for basket in package["baskets"]:
                assert basket_lot[basket["id"]] == event["lot_id"]
                packed_from_basket[basket["id"]] += basket["pieces"]
            packed_total += package["pieces"]
        assert sum(p["pieces"] for p in event["packages"]) == sawn_lots[event["lot_id"]]["pieces_good"]
    for lot_id in packed_lots:
        for basket_id in lot_baskets[lot_id]:
            assert packed_from_basket[basket_id] == sawn_pieces[basket_id], "pieces per basket"

    if complete:
        assert packed_lots == set(lots_by_id), "not every lot was packed"
        assert packed_total == sum(sawn["pieces_good"] for sawn in sawn_lots.values())
        total_billets = sum(lot.n_billets for lot in lots)
        assert len(extruded_billets) == total_billets
        arrivals = [e for e in events if e["type"] == "lot_arrived" and e["stage"] == "ship"]
        assert sorted(e["lot_id"] for e in arrivals) == sorted(lots_by_id)
        if engine is not None:
            ship = engine.material_view()["buffers"]["SHIP"]
            assert ship["pieces"] == packed_total
            assert ship["packages"] == len(package_ids)
            store = engine.export_snapshot()["billet_store"]
            left = sum(len(billets) for billets in store.values())
            assert left + len(extruded_billets) == len(melt_of_billet), "billets lost or invented"
