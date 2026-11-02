"""Genealogy and the per-run database, driven by event streams with the schema of PLAN section 4."""
from __future__ import annotations

import json
import threading
from collections import defaultdict

import pytest

from werkhalle.mes.db import RunDb, open_run_db, remove_older_runs, run_db_path
from werkhalle.mes.trace import RECALL_LABEL, TraceStore, format_de, format_sim_time


class Plant:
    """Writes realistic trace events. Bars and pieces are handed in explicitly, so each test can state its own quantities."""

    def __init__(self) -> None:
        self.events: list[dict] = []
        self.t = 100.0

    def _emit(self, type_: str, **fields) -> dict:
        self.t += 1.0
        event = {"seq": len(self.events) + 1, "t": self.t, "type": type_, **fields}
        self.events.append(event)
        return event

    def melt(self, melt_id: str, alloy: str, n: int, plan_id: str = "M-6060-00001", temp_c: float = 721.0) -> list[str]:
        number = melt_id.split("-")[1]
        billets = [f"B-{number}-{i:02d}" for i in range(1, n + 1)]
        self._emit("melt_cast", machine="GUSS", melt_id=melt_id, plan_id=plan_id, alloy=alloy, billets=billets, temp_c=temp_c,
                   t_start=self.t - 90.0)
        self._emit("melt_arrived", melt_id=melt_id, plan_id=plan_id, alloy=alloy, n_billets=n)
        return billets

    def extrude(self, lot_id: str, order_id: str, billet_id: str, melt_id: str, strand_id: str, bars_good: int, *,
                bars_scrap: int = 0, die: str = "M-1001", machine: str = "PR1") -> None:
        self._emit("billet_extruded", machine=machine, lot_id=lot_id, order_id=order_id, billet_id=billet_id, melt_id=melt_id,
                   strand_id=strand_id, die=die, bars_total=bars_good + bars_scrap, bars_good=bars_good, bars_scrap=bars_scrap,
                   temp_c=488.0, t_start=self.t - 7.0)

    def fill(self, lot_id: str, baskets: dict[str, list[tuple[str, int]]]) -> None:
        self._emit("baskets_filled", machine="PR1", lot_id=lot_id, baskets=[
            {"id": basket, "bars": sum(b for _, b in strands), "strands": [{"id": s, "bars": b} for s, b in strands]}
            for basket, strands in baskets.items()
        ])

    def furnace(self, load_id: str, lot_ids: list[str], baskets: list[str], machine: str = "OF2") -> None:
        self._emit("furnace_load_done", machine=machine, load_id=load_id, batch_id="B-r1-1", recipe="T66", lot_ids=lot_ids,
                   baskets=baskets, soak_temp_avg=185.2, soak_min=300.0, in_band_min=296.0, fault_min=0.0, t_start=self.t - 300.0)

    def saw(self, lot_id: str, baskets: dict[str, tuple[int, int]]) -> None:
        rows = [{"id": b, "bars_in": good + scrap, "pieces_good": good, "pieces_scrap": scrap} for b, (good, scrap) in baskets.items()]
        self._emit("sawn", machine="SAE", lot_id=lot_id, baskets=rows, bars_in=sum(r["bars_in"] for r in rows),
                   pieces_good=sum(r["pieces_good"] for r in rows), pieces_scrap=sum(r["pieces_scrap"] for r in rows),
                   length_mm=6000, t_start=self.t - 20.0)

    def pack(self, lot_id: str, packages: dict[str, list[tuple[str, int]]]) -> None:
        self._emit("packed", machine="VP", lot_id=lot_id, t_start=self.t - 6.0, packages=[
            {"id": p, "pieces": sum(n for _, n in baskets), "baskets": [{"id": b, "pieces": n} for b, n in baskets]}
            for p, baskets in packages.items()
        ])

    def finished(self, kind: str, op_ids: list[str], lot_ids: list[str], energy_kwh: float, machine: str = "PR1") -> None:
        self._emit("op_finished", machine=machine, op_ids=op_ids, kind=kind, lot_ids=lot_ids, energy_kwh=energy_kwh, run_min=10.0,
                   setup_min=0.0, fault_min=0.0)


@pytest.fixture
def db(tmp_path):
    database = RunDb(tmp_path / "werkhalle.db")
    yield database
    database.close()


def load(db: RunDb, events: list[dict], **kwargs) -> TraceStore:
    store = TraceStore(db, **kwargs)
    store.consume_many(events)
    store.flush()
    return store


def ids(graph: dict, node_type: str | None = None) -> set[str]:
    return {n["id"] for n in graph["nodes"] if node_type is None or n["type"] == node_type}


def two_lots_one_load() -> Plant:
    """Lot 1 from melt 1 and lot 2 from melt 2, separate orders, sharing furnace load O-00001."""
    plant = Plant()
    plant.melt("S-00001", "6060", 2)
    plant.melt("S-00002", "6060", 2, plan_id="M-6060-00002")
    plant.extrude("L-00001", "A-1001", "B-00001-01", "S-00001", "ST-00001", 8)
    plant.extrude("L-00001", "A-1001", "B-00001-02", "S-00001", "ST-00002", 8)
    plant.fill("L-00001", {"K-00001": [("ST-00001", 8), ("ST-00002", 8)]})
    plant.extrude("L-00002", "A-1002", "B-00002-01", "S-00002", "ST-00003", 8, machine="PR2")
    plant.extrude("L-00002", "A-1002", "B-00002-02", "S-00002", "ST-00004", 8, machine="PR2")
    plant.fill("L-00002", {"K-00002": [("ST-00003", 8), ("ST-00004", 8)]})
    plant.furnace("O-00001", ["L-00001", "L-00002"], ["K-00001", "K-00002"])
    plant.saw("L-00001", {"K-00001": (16, 0)})
    plant.saw("L-00002", {"K-00002": (16, 0)})
    plant.pack("L-00001", {"P-00001": [("K-00001", 16)]})
    plant.pack("L-00002", {"P-00002": [("K-00002", 16)]})
    return plant


class TestGenealogyRules:
    def test_split_melt_reaches_both_lots_and_stays_out_of_sibling_billets(self, db):
        plant = Plant()
        plant.melt("S-00001", "6060", 2)
        plant.extrude("L-00001", "A-1001", "B-00001-01", "S-00001", "ST-00001", 8)
        plant.extrude("L-00002", "A-1002", "B-00001-02", "S-00001", "ST-00002", 8)
        plant.fill("L-00001", {"K-00001": [("ST-00001", 8)]})
        plant.fill("L-00002", {"K-00002": [("ST-00002", 8)]})
        plant.saw("L-00001", {"K-00001": (8, 0)})
        plant.saw("L-00002", {"K-00002": (8, 0)})
        plant.pack("L-00001", {"P-00001": [("K-00001", 8)]})
        plant.pack("L-00002", {"P-00002": [("K-00002", 8)]})
        store = load(db, plant.events)

        forward = store.graph("S-00001")
        assert ids(forward, "package") == {"P-00001", "P-00002"}
        assert ids(forward, "order") == {"A-1001", "A-1002"}
        assert forward["summary"]["orders"] == ["A-1001", "A-1002"]
        assert forward["summary"]["packages"] == 2
        assert forward["summary"]["pieces"] == 16

        # From one package, only its own billet: the sibling billet shares a melt but is no ancestor of this package.
        backward = store.graph("P-00001")
        assert ids(backward, "melt") == {"S-00001"}
        assert ids(backward, "billet") == {"B-00001-01"}
        assert "P-00002" not in ids(backward)
        assert "A-1002" not in ids(backward)

    def test_lot_spanning_two_melts(self, db):
        plant = Plant()
        plant.melt("S-00001", "6060", 1)
        plant.melt("S-00002", "6060", 1, plan_id="M-6060-00002")
        plant.extrude("L-00001", "A-1001", "B-00001-01", "S-00001", "ST-00001", 8)
        plant.extrude("L-00001", "A-1001", "B-00002-01", "S-00002", "ST-00002", 8)
        plant.fill("L-00001", {"K-00001": [("ST-00001", 8)], "K-00002": [("ST-00002", 8)]})
        plant.saw("L-00001", {"K-00001": (8, 0), "K-00002": (8, 0)})
        plant.pack("L-00001", {"P-00001": [("K-00001", 8)], "P-00002": [("K-00002", 8)]})
        store = load(db, plant.events)

        # A melt reaches only what its own billets became, although both are the same lot.
        forward = store.graph("S-00001")
        assert ids(forward, "strand") == {"ST-00001"}
        assert ids(forward, "package") == {"P-00001"}
        assert "S-00002" not in ids(forward)
        assert "P-00002" not in ids(forward)

        # The order sees both melts.
        backward = store.graph("A-1001")
        assert ids(backward, "melt") == {"S-00001", "S-00002"}
        assert backward["summary"]["melts"] == ["S-00001", "S-00002"]

        # The lot as root groups its strands and shows both melts as ancestors.
        lot = store.graph("L-00001")
        assert lot["root_type"] == "lot"
        assert ids(lot, "melt") == {"S-00001", "S-00002"}
        assert ids(lot, "package") == {"P-00001", "P-00002"}
        relation = {n["id"]: n["relation"] for n in lot["nodes"]}
        assert relation["L-00001"] == "root"
        assert relation["ST-00001"] == "descendant"
        assert relation["B-00001-01"] == "ancestor"

    def test_shared_furnace_load_does_not_link_unrelated_lots(self, db):
        store = load(db, two_lots_one_load().events)

        forward = store.graph("S-00001")
        assert ids(forward, "package") == {"P-00001"}
        assert "P-00002" not in ids(forward)
        assert "K-00002" not in ids(forward)
        assert "A-1002" not in ids(forward)
        assert forward["summary"]["orders"] == ["A-1001"]

        # The load is attached as a side node, and it reports where the material was without pulling in the other lot.
        load_node = next(n for n in forward["nodes"] if n["id"] == "O-00001")
        assert load_node["type"] == "load"
        assert load_node["relation"] == "side"
        assert forward["summary"]["loads"] == ["O-00001"]
        assert {"from": "K-00001", "to": "O-00001", "qty": 1, "unit": "Körbe", "side": True} in forward["edges"]

    def test_load_rooted_recall_includes_both_lots(self, db):
        store = load(db, two_lots_one_load().events)
        recall = store.graph("O-00001")
        assert recall["root"] == "O-00001"
        assert recall["root_type"] == "load"
        assert recall["recall_label"] == RECALL_LABEL == "möglich betroffen"
        assert ids(recall, "package") == {"P-00001", "P-00002"}
        assert ids(recall, "melt") == {"S-00001", "S-00002"}
        assert recall["summary"]["orders"] == ["A-1001", "A-1002"]
        assert recall["summary"]["pieces"] == 32
        assert recall["summary"]["loads"] == ["O-00001"]
        relation = {n["id"]: n["relation"] for n in recall["nodes"]}
        assert relation["O-00001"] == "root"
        assert relation["K-00001"] == "descendant"
        assert relation["P-00002"] == "descendant"
        assert relation["S-00002"] == "ancestor"

    def test_basket_mixing_two_melts_is_a_conservative_recall(self, db):
        plant = Plant()
        plant.melt("S-00001", "6060", 1)
        plant.melt("S-00002", "6060", 1, plan_id="M-6060-00002")
        plant.extrude("L-00001", "A-1001", "B-00001-01", "S-00001", "ST-00001", 8)
        plant.extrude("L-00001", "A-1001", "B-00002-01", "S-00002", "ST-00002", 8)
        plant.fill("L-00001", {"K-00001": [("ST-00001", 8), ("ST-00002", 8)]})
        plant.saw("L-00001", {"K-00001": (16, 0)})
        plant.pack("L-00001", {"P-00001": [("K-00001", 16)]})
        store = load(db, plant.events)

        forward = store.graph("S-00001")
        assert ids(forward, "package") == {"P-00001"}
        # The basket's other strand is not an ancestor of melt 1, so an undirected closure is what would show it.
        assert "ST-00002" not in ids(forward)
        assert "S-00002" not in ids(forward)

    def test_unknown_id_gives_none(self, db):
        store = load(db, two_lots_one_load().events)
        assert store.graph("S-99999") is None


class TestQuantities:
    def build(self) -> Plant:
        plant = Plant()
        plant.melt("S-00001", "6060", 3)
        plant.melt("S-00002", "6060", 3, plan_id="M-6060-00002")
        plant.extrude("L-00001", "A-1001", "B-00001-01", "S-00001", "ST-00001", 8)
        plant.extrude("L-00001", "A-1001", "B-00001-02", "S-00001", "ST-00002", 7, bars_scrap=1)
        plant.extrude("L-00001", "A-1001", "B-00001-03", "S-00001", "ST-00003", 8)
        plant.extrude("L-00001", "A-1001", "B-00002-01", "S-00002", "ST-00004", 8)
        plant.fill("L-00001", {"K-00001": [("ST-00001", 8), ("ST-00002", 7)], "K-00002": [("ST-00003", 8), ("ST-00004", 8)]})
        plant.extrude("L-00002", "A-1002", "B-00002-02", "S-00002", "ST-00005", 8)
        plant.extrude("L-00002", "A-1002", "B-00002-03", "S-00002", "ST-00006", 8)
        plant.fill("L-00002", {"K-00003": [("ST-00005", 8), ("ST-00006", 8)]})
        plant.furnace("O-00001", ["L-00001"], ["K-00001", "K-00002"])
        plant.furnace("O-00002", ["L-00002"], ["K-00003"], machine="OF1")
        plant.saw("L-00001", {"K-00001": (14, 1), "K-00002": (16, 0)})
        plant.saw("L-00002", {"K-00003": (16, 0)})
        plant.pack("L-00001", {"P-00001": [("K-00001", 14), ("K-00002", 2)], "P-00002": [("K-00002", 14)]})
        plant.pack("L-00002", {"P-00003": [("K-00003", 16)]})
        return plant

    def test_edges_sum_to_node_quantities_at_every_level(self, db):
        store = load(db, self.build().events)
        nodes = {r["id"]: r for r in db.query("SELECT id, type, qty, data FROM nodes")}
        edges = db.query("SELECT src, dst, qty, unit FROM edges")
        incoming: dict[str, float] = defaultdict(float)
        outgoing: dict[str, float] = defaultdict(float)
        for edge in edges:
            incoming[edge["dst"]] += edge["qty"]
            outgoing[edge["src"]] += edge["qty"]

        counted = defaultdict(int)
        for node_id, node in nodes.items():
            counted[node["type"]] += 1
            if node["type"] == "melt":
                assert outgoing[node_id] == node["qty"], node_id
            elif node["type"] == "billet":
                assert incoming[node_id] == node["qty"] == 1, node_id
            elif node["type"] == "strand":
                assert incoming[node_id] == node["qty"], node_id
                assert outgoing[node_id] == node["qty"], node_id  # whole-strand rule: all of it in one basket
            elif node["type"] == "basket":
                assert incoming[node_id] == node["qty"], node_id
                data = json.loads(node["data"])
                assert outgoing[node_id] == data["pieces_good"], node_id
                assert data["bars_in"] == data["pieces_good"] + data["pieces_scrap"], node_id
            elif node["type"] in ("package", "order"):
                assert incoming[node_id] == node["qty"], node_id
        assert dict(counted) == {"melt": 2, "billet": 6, "strand": 6, "basket": 3, "load": 2, "package": 3, "order": 2, "lot": 2}

        # Units along the flow.
        assert {e["unit"] for e in edges if e["src"].startswith("S-")} == {"Bolzen"}
        assert {e["unit"] for e in edges if e["src"].startswith("B-")} == {"Stangen"}
        assert {e["unit"] for e in edges if e["src"].startswith("ST-")} == {"Stangen"}
        assert {e["unit"] for e in edges if e["src"].startswith("K-")} == {"Stück"}
        assert {e["unit"] for e in edges if e["src"].startswith("P-")} == {"Stück"}

        # Load and lot memberships balance too.
        for group, qty in (("O-00001", 2), ("O-00002", 1)):
            members = db.query("SELECT qty FROM memberships WHERE group_id = ?", (group,))
            assert sum(m["qty"] for m in members) == qty == nodes[group]["qty"]
        members = db.query("SELECT qty FROM memberships WHERE group_id = 'L-00001'")
        assert sum(m["qty"] for m in members) == nodes["L-00001"]["qty"] == 31

        assert store.delivered_pieces("A-1001") == 30
        assert store.delivered_pieces("A-1002") == 16
        assert store.lot_pieces("L-00001") == 30

    def test_every_billet_resolves_to_a_melt(self, db):
        load(db, self.build().events)
        billets = [r["id"] for r in db.query("SELECT id FROM nodes WHERE type = 'billet'")]
        assert len(billets) == 6
        for billet in billets:
            parents = db.query(
                "SELECT n.id, n.type FROM edges e JOIN nodes n ON n.id = e.src WHERE e.dst = ?", (billet,))
            assert [p["type"] for p in parents] == ["melt"], billet

    def test_billet_resolves_to_a_melt_when_melt_cast_is_missing(self, db):
        # melt_cast fell out of the plant's ring before this MES started.
        plant = Plant()
        plant.extrude("L-00001", "A-1001", "B-00009-01", "S-00009", "ST-00001", 8)
        store = load(db, plant.events)
        graph = store.graph("ST-00001")
        assert ids(graph, "melt") == {"S-00009"}
        assert next(n for n in graph["nodes"] if n["id"] == "S-00009")["label"] == "Schmelze S-00009"

    def test_graph_edges_only_connect_returned_nodes(self, db):
        store = load(db, self.build().events)
        for root in ("S-00002", "O-00001", "L-00001", "P-00001", "A-1001", "K-00002", "ST-00004", "B-00002-01"):
            graph = store.graph(root)
            present = ids(graph)
            assert root in present
            for edge in graph["edges"]:
                assert edge["from"] in present and edge["to"] in present


class TestNodeCap:
    def build_big_order(self, lots: int) -> Plant:
        plant = Plant()
        for i in range(1, lots + 1):
            melt = f"S-{i:05d}"
            billets = plant.melt(melt, "6060", 8, plan_id=f"M-6060-{i:05d}")
            lot = f"L-{i:05d}"
            strands = []
            for j, billet in enumerate(billets, start=1):
                strand = f"ST-{i:03d}{j:02d}"
                plant.extrude(lot, "A-1001", billet, melt, strand, 8)
                strands.append((strand, 8))
            basket = f"K-{i:05d}"
            plant.fill(lot, {basket: strands})
            plant.saw(lot, {basket: (64, 0)})
            package = f"P-{i:05d}"
            plant.pack(lot, {package: [(basket, 64)]})
        return plant

    def test_under_the_cap_is_not_truncated(self, db):
        store = load(db, self.build_big_order(4).events)
        graph = store.graph("A-1001")
        assert graph["truncated"] is False
        assert len(graph["nodes"]) == 1 + 4 * (1 + 8 + 8 + 1 + 1)

    def test_over_the_cap_is_truncated_and_summary_stays_complete(self, db):
        store = load(db, self.build_big_order(30).events)
        graph = store.graph("A-1001")
        assert graph["truncated"] is True
        assert len(graph["nodes"]) == 400
        assert graph["nodes"][0]["id"] == "A-1001"
        # Nearest first, so what survives is the neighbourhood of the root.
        assert ids(graph, "package") == {f"P-{i:05d}" for i in range(1, 31)}
        # A recall figure must not depend on what fits on screen.
        assert graph["summary"]["packages"] == 30
        assert graph["summary"]["pieces"] == 30 * 64
        assert len(graph["summary"]["melts"]) == 30
        present = ids(graph)
        for edge in graph["edges"]:
            assert edge["from"] in present and edge["to"] in present

    def test_custom_cap(self, db):
        store = load(db, self.build_big_order(2).events, node_cap=10)
        graph = store.graph("S-00001")
        assert graph["truncated"] is True
        assert len(graph["nodes"]) == 10


class TestIdempotence:
    def test_duplicate_events_change_nothing(self, db):
        plant = two_lots_one_load()
        plant.finished("press", ["L-00001:press"], ["L-00001"], 50.0)
        store = TraceStore(db)
        assert store.consume_many(plant.events) == len(plant.events)
        store.flush()
        before = store.graph("O-00001")
        delivered = store.delivered_pieces("A-1001")
        energy = store.order_energy_kwh("A-1001")
        counts = [db.query(f"SELECT COUNT(*) AS n FROM {table}")[0]["n"] for table in ("nodes", "edges", "memberships", "events")]

        assert store.consume_many(plant.events) == 0
        for event in plant.events[:5]:
            assert store.consume(event) is False
        store.flush()

        assert store.graph("O-00001") == before
        assert store.delivered_pieces("A-1001") == delivered == 16
        assert store.order_energy_kwh("A-1001") == energy
        assert [db.query(f"SELECT COUNT(*) AS n FROM {table}")[0]["n"] for table in ("nodes", "edges", "memberships", "events")] == counts

    def test_reopening_a_run_restores_seq_and_totals_without_rewriting(self, tmp_path):
        path = tmp_path / "werkhalle.db"
        plant = two_lots_one_load()
        plant.finished("press", ["L-00001:press"], ["L-00001"], 50.0)

        first = RunDb(path)
        store = TraceStore(first)
        store.register_lot("L-00003", "A-1003", "KK-80", 4)
        store.consume_many(plant.events)
        store.flush()
        expected = store.graph("O-00001")
        first.close()

        second = RunDb(path)
        try:
            resumed = TraceStore(second)
            assert resumed.last_seq == plant.events[-1]["seq"]
            assert resumed.delivered_pieces("A-1001") == 16
            assert resumed.order_energy_kwh("A-1001") == pytest.approx(50.0)
            assert resumed.graph("O-00001") == expected
            assert resumed.consume(plant.events[0]) is False

            # New events continue from there, and the restored lot-to-product map still prices packages.
            plant.pack("L-00001", {"P-00099": [("K-00001", 1)]})
            assert resumed.consume(plant.events[-1]) is True
            resumed.flush()
            assert resumed.delivered_pieces("A-1001") == 17
            assert resumed.node("P-00099")["attrs"]["Gewicht"] == "7,2 kg"
        finally:
            second.close()


class TestSearchAndLabels:
    def test_search_by_id_prefix_and_substring(self, db):
        store = load(db, two_lots_one_load().events)
        results = store.search("S-000")["results"]
        assert [r["id"] for r in results] == ["S-00001", "S-00002"]
        assert results[0] == {"id": "S-00001", "type": "melt", "label": "Schmelze S-00001 · 6060", "t": results[0]["t"]}

        assert [r["id"] for r in store.search("a-1002")["results"]] == ["A-1002"]
        assert {r["type"] for r in store.search("00001")["results"]} >= {"melt", "strand", "basket", "load", "package", "lot"}
        # Prefix matches come first.
        first = store.search("ST-")["results"][0]
        assert first["type"] == "strand"

    def test_search_edge_cases(self, db):
        store = load(db, two_lots_one_load().events)
        assert store.search("")["results"] == []
        assert store.search("   ")["results"] == []
        assert store.search("%")["results"] == []
        assert store.search("_")["results"] == []
        assert store.search("nichts")["results"] == []

    def test_search_returns_at_most_30(self, db):
        plant = Plant()
        for i in range(1, 11):
            plant.melt(f"S-{i:05d}", "6060", 4, plan_id=f"M-6060-{i:05d}")
        store = load(db, plant.events)
        assert len(store.search("B-")["results"]) == 30
        assert len(store.search("B-", limit=100)["results"]) == 30

    def test_recent_lists_newest_packages_and_melts_first(self, db):
        store = load(db, two_lots_one_load().events)
        results = store.recent(limit=3)["results"]
        assert [r["id"] for r in results] == ["P-00002", "P-00001", "S-00002"]
        assert {r["type"] for r in store.recent()["results"]} == {"package", "melt"}

    def test_labels_and_attrs_are_german_with_units(self, db):
        store = load(db, two_lots_one_load().events)
        graph = store.graph("S-00001")
        nodes = {n["id"]: n for n in graph["nodes"]}

        billet = nodes["B-00001-01"]
        assert billet["label"] == "Bolzen B-00001-01"
        assert billet["qty"] == 1
        assert billet["unit"] == "Bolzen"
        assert billet["attrs"]["Legierung"] == "6060"
        assert billet["attrs"]["Schmelze"] == "S-00001"
        assert billet["attrs"]["Temperatur"] == "721 °C"
        assert isinstance(billet["t"], float)
        assert nodes["S-00001"]["attrs"]["Bolzen"] == 2
        assert nodes["ST-00001"]["attrs"]["Temperatur"] == "488 °C"
        assert nodes["K-00001"]["label"] == "Korb K-00001"
        assert nodes["K-00001"]["attrs"]["Ofenlast"] == "O-00001"
        assert nodes["P-00001"]["label"] == "Paket P-00001"
        assert nodes["P-00001"]["attrs"]["Gewicht"] == "115,2 kg"
        assert nodes["P-00001"]["attrs"]["Auftrag"] == "A-1001"
        assert nodes["A-1001"]["label"] == "Auftrag A-1001"
        assert nodes["A-1001"]["qty"] == 16
        assert nodes["A-1001"]["unit"] == "Stück"
        assert nodes["O-00001"]["attrs"]["Haltetemperatur"] == "185,2 °C"
        assert nodes["O-00001"]["attrs"]["Haltezeit"] == "300 min"
        assert graph["summary"]["kg"] == pytest.approx(115.2)
        assert graph["direction_note"] == "Vorfahren und Nachkommen des gewählten Knotens"

    def test_initial_stock_is_marked_and_has_no_cast_times(self, db):
        plant = Plant()
        plant.events.append({"seq": 1, "t": 0.0, "type": "melt_cast", "machine": "GUSS", "melt_id": "S-00001", "plan_id": "BESTAND",
                             "alloy": "6060", "billets": ["B-00001-01"], "temp_c": 700.0, "t_start": -98.0})
        store = load(db, plant.events)
        attrs = store.node("S-00001")["attrs"]
        assert attrs["Plan"] == "Anfangsbestand"
        assert "Gussbeginn" not in attrs
        assert "Gussende" not in attrs

    def test_number_and_time_formats(self):
        assert format_de(1234.5, 1) == "1.234,5"
        assert format_de(721) == "721"
        assert format_de(1234567.891, 2) == "1.234.567,89"
        assert format_sim_time(0) == "Tag 1 · 06:00"
        assert format_sim_time(1920) == "Tag 2 · 14:00"
        assert format_sim_time(1950.7) == "Tag 2 · 14:30"
        assert format_sim_time(1079.9) == "Tag 1 · 23:59"
        assert format_sim_time(1080) == "Tag 2 · 00:00"

    def test_registered_order_and_lot_are_searchable_before_any_material(self, db):
        store = TraceStore(db)
        store.register_order("A-2000", "KK-80", 200, 600.0, 10.0)
        store.register_lot("L-00500", "A-2000", "KK-80", 8)
        store.flush()
        assert [r["id"] for r in store.search("A-2000")["results"]] == ["A-2000"]
        assert store.search("L-00500")["results"][0]["type"] == "lot"
        order = store.node("A-2000")
        assert order["qty"] == 0
        assert order["attrs"]["Produkt"] == "Kühlkörper KK-80"
        assert order["attrs"]["Bestellt"] == "200 Stangen"
        assert order["attrs"]["Liefertermin"] == "Tag 1 · 16:00"
        graph = store.graph("L-00500")
        assert graph["root_type"] == "lot"
        assert ids(graph) == {"L-00500"}


class TestPackageWithoutKnownOrder:
    def test_package_attaches_when_the_lot_becomes_known(self, db):
        # billet_extruded fell out of the ring, so at `packed` nothing says which order this lot belongs to.
        plant = Plant()
        plant.pack("L-00007", {"P-00001": [("K-00001", 10)]})
        store = load(db, plant.events)
        assert store.graph("P-00001")["summary"]["orders"] == []
        assert store.delivered_pieces("A-1007") == 0

        store.register_lot("L-00007", "A-1007", "FR-60")
        store.flush()
        assert store.graph("P-00001")["summary"]["orders"] == ["A-1007"]
        assert store.delivered_pieces("A-1007") == 10


class TestEnergy:
    def test_furnace_energy_is_split_by_baskets_and_cast_energy_by_billets(self, db):
        plant = Plant()
        plant.melt("S-00001", "6060", 4, plan_id="M-6060-00001")
        plant.finished("cast", ["M-6060-00001:cast"], [], 400.0, machine="GUSS")
        plant.extrude("L-00001", "A-1001", "B-00001-01", "S-00001", "ST-00001", 8)
        plant.extrude("L-00001", "A-1001", "B-00001-02", "S-00001", "ST-00002", 8)
        plant.extrude("L-00001", "A-1001", "B-00001-03", "S-00001", "ST-00003", 8)
        plant.extrude("L-00002", "A-1002", "B-00001-04", "S-00001", "ST-00004", 8)
        plant.fill("L-00001", {"K-00001": [("ST-00001", 8)], "K-00002": [("ST-00002", 8)], "K-00003": [("ST-00003", 8)]})
        plant.fill("L-00002", {"K-00004": [("ST-00004", 8)]})
        plant.finished("press", ["L-00001:press"], ["L-00001"], 100.0)
        plant.finished("press", ["L-00002:press"], ["L-00002"], 40.0)
        plant.finished("age", ["L-00001:age", "L-00002:age"], ["L-00001", "L-00002"], 800.0, machine="OF2")
        plant.saw("L-00001", {"K-00001": (8, 0), "K-00002": (8, 0), "K-00003": (8, 0)})
        plant.pack("L-00001", {"P-00001": [("K-00001", 8), ("K-00002", 8), ("K-00003", 8)]})
        store = load(db, plant.events)

        # A-1001: press 100 + 3/4 of the furnace 600 + 3/4 of the melt 300 = 1000. A-1002: 40 + 200 + 100 = 340.
        assert store.order_energy_kwh("A-1001") == pytest.approx(1000.0)
        assert store.order_energy_kwh("A-1002") == pytest.approx(340.0)
        rows = {r["order_id"]: r for r in store.energy_by_order()}
        assert rows["A-1001"]["energy_kwh"] == pytest.approx(1000.0)
        assert rows["A-1001"]["kg"] == pytest.approx(24 * 7.2)
        assert rows["A-1001"]["kwh_per_kg"] == pytest.approx(1000.0 / (24 * 7.2), abs=1e-3)
        assert rows["A-1002"]["kg"] == 0
        assert rows["A-1002"]["kwh_per_kg"] is None


class TestRunDb:
    def test_consume_never_writes_on_the_calling_thread(self, tmp_path, monkeypatch):
        database = RunDb(tmp_path / "w.db")
        try:
            threads = set()
            original = database._commit

            def spy(conn, statements):
                threads.add(threading.current_thread().name)
                original(conn, statements)

            monkeypatch.setattr(database, "_commit", spy)
            store = TraceStore(database)
            store.consume_many(two_lots_one_load().events)
            store.flush()
            assert threads == {"werkhalle-db"}
        finally:
            database.close()

    def test_wal_mode(self, db):
        assert db.query("PRAGMA journal_mode")[0][0].lower() == "wal"

    def test_flush_makes_writes_visible_and_batches(self, tmp_path, monkeypatch):
        database = RunDb(tmp_path / "w.db", commit_interval_s=0.05)
        try:
            commits = []
            original = database._commit
            monkeypatch.setattr(database, "_commit", lambda conn, st: (commits.append(len(st)), original(conn, st)))
            for i in range(500):
                database.set_meta(f"k{i}", i)
            assert database.flush()
            assert database.get_meta("k499") == 499
            assert database.get_meta("fehlt", "x") == "x"
            assert len(commits) < 50  # many statements, few transactions
            assert sum(commits) == 500
        finally:
            database.close()

    def test_one_bad_statement_does_not_lose_the_batch(self, db):
        db.set_meta("vorher", 1)
        db.submit("INSERT INTO gibt_es_nicht VALUES (1)")
        db.set_meta("nachher", 2)
        db.flush()
        assert db.get_meta("vorher") == 1
        assert db.get_meta("nachher") == 2
        assert db.write_errors == 1

    def test_orders_lots_alarms_log_round_trip(self, db):
        db.save_order({"id": "A-1", "qty_bars": 10, "status": "offen"})
        db.save_order({"id": "A-1", "qty_bars": 10, "status": "fertig"})
        db.save_order({"id": "A-2", "qty_bars": 5})
        db.save_lot({"id": "L-1", "order_id": "A-1", "n_billets": 2})
        db.save_lot({"id": "L-2", "order_id": "A-2", "n_billets": 1})
        db.save_alarm({"id": "PR2:3", "machine": "PR2", "t": 10.0, "code": "P-204", "text": "Hauptzylinder undicht"})
        db.save_alarm({"id": "PR2:4", "machine": "PR2", "t": 20.0})
        db.append_log({"t": 1.0, "wall_ts": 2.0, "level": "info", "source": "mes", "text": "Störung erkannt"})
        db.append_log({"t": 2.0, "wall_ts": 3.0, "level": "warn", "source": "opcua", "text": "zweite Zeile"})
        db.flush()

        assert [o["status"] for o in db.load_orders() if o["id"] == "A-1"] == ["fertig"]
        assert len(db.load_orders()) == 2
        assert [lot["id"] for lot in db.load_lots("A-2")] == ["L-2"]
        assert len(db.load_lots()) == 2
        assert [a["id"] for a in db.load_alarms()] == ["PR2:4", "PR2:3"]
        assert db.load_alarms(limit=1)[0]["id"] == "PR2:4"
        assert db.load_alarms()[1]["text"] == "Hauptzylinder undicht"
        log = db.load_log()
        assert log[0]["text"] == "zweite Zeile"
        assert log[1] == {"t": 1.0, "wall_ts": 2.0, "level": "info", "source": "mes", "text": "Störung erkannt"}

    def test_close_commits_what_is_queued_and_is_repeatable(self, tmp_path):
        path = tmp_path / "w.db"
        database = RunDb(path)
        database.set_meta("a", 1)
        database.close()
        database.close()
        with pytest.raises(RuntimeError):
            database.set_meta("b", 2)
        reopened = RunDb(path)
        try:
            assert reopened.get_meta("a") == 1
        finally:
            reopened.close()

    def test_paths_per_run_and_removal_of_older_runs(self, tmp_path):
        assert run_db_path(tmp_path, "r-ab12") == tmp_path / "run-r-ab12" / "werkhalle.db"
        # The RunId comes from the wire, so it must not be able to leave the data directory.
        escaped = run_db_path(tmp_path, "../../böse")
        assert escaped.parent.parent == tmp_path

        older = open_run_db(tmp_path, "r-old")
        older.close()
        (tmp_path / "anderes").mkdir()
        current = open_run_db(tmp_path, "r-new")
        try:
            assert not (tmp_path / "run-r-old").exists()
            assert (tmp_path / "run-r-new" / "werkhalle.db").exists()
            assert (tmp_path / "anderes").exists()
            assert remove_older_runs(tmp_path / "gibt-es-nicht", "x") == []
        finally:
            current.close()

    def test_open_run_db_can_keep_older_runs(self, tmp_path):
        open_run_db(tmp_path, "r-old").close()
        open_run_db(tmp_path, "r-new", remove_older=False).close()
        assert (tmp_path / "run-r-old").exists()
