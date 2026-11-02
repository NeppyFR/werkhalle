"""docs/API.md against a running real plant and a running real MES, both as separate processes on free ports."""
from __future__ import annotations

import asyncio
import json
import os
import socket
import subprocess
import sys
import time
from collections import Counter
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from websockets.sync.client import connect as ws_connect

from werkhalle.domain import catalog
from werkhalle.mes.opcua_client import DataChange, PlantNotConnected
from werkhalle.mes.runtime import ApiError, Runtime
from werkhalle.mes.seed import seed_orders
from werkhalle.mes.shadow import FaultEvent

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
MACHINE_IDS = ["GUSS", "PR1", "PR2", "OF1", "OF2", "SAE", "VP"]
SOLVER_LIMIT = 2


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def child_env() -> dict:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(SRC)
    env["PYTHONUTF8"] = "1"
    env.pop("PYTHONIOENCODING", None)
    return env


def stop_tree(proc: subprocess.Popen) -> None:
    if proc.poll() is None:
        subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True, check=False)
    proc.wait(timeout=20)


def port_is_free(port: int) -> bool:
    with socket.socket() as sock:
        try:
            sock.bind(("127.0.0.1", port))
        except OSError:
            return False
    return True


def wait_for(predicate, timeout: float = 30.0, step: float = 0.1, what: str = "condition"):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(step)
    raise AssertionError(f"{what} not reached within {timeout} s")


def has_keys(obj: dict, keys: str, where: str = "") -> None:
    missing = [k for k in keys.split() if k not in obj]
    assert not missing, f"{where or 'object'} is missing {missing}: {sorted(obj)}"


class System(SimpleNamespace):
    http: httpx.Client
    plant_port: int
    http_port: int

    def get(self, path: str, **kwargs):
        return self.http.get(path, **kwargs)

    def post(self, path: str, body: dict | None = None):
        return self.http.post(path, json=body if body is not None else {})

    def ok(self, path: str) -> dict:
        response = self.get(path)
        assert response.status_code == 200, f"{path}: {response.status_code} {response.text}"
        return response.json()

    def state(self) -> dict:
        return self.ok("/api/state")

    def machine(self, machine_id: str) -> dict:
        return next(m for m in self.state()["machines"] if m["id"] == machine_id)

    def open_proposal(self, status: str = "open") -> dict | None:
        current = self.ok("/api/proposals")["open"]
        return current if current is not None and current["status"] == status else None

    def clear_proposal(self) -> None:
        current = self.ok("/api/proposals")["open"]
        if current is not None:
            self.post(f"/api/proposals/{current['id']}/reject")

    def repair_all(self) -> None:
        for row in self.state()["machines"]:
            if row["status"] == "FAULT":
                self.post(f"/api/machines/{row['id']}/repair")
        wait_for(lambda: all(m["status"] != "FAULT" for m in self.state()["machines"]), 10, what="all machines repaired")
        self.clear_proposal()


@pytest.fixture(scope="module")
def system(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("api")
    plant_port, http_port = free_port(), free_port()
    url = f"opc.tcp://127.0.0.1:{plant_port}/werkhalle/"
    plant_log = open(tmp / "plant.log", "wb")
    mes_log = open(tmp / "mes.log", "wb")
    plant = subprocess.Popen(
        [sys.executable, "-m", "werkhalle.plant", "--port", str(plant_port), "--seed", "7", "--time-factor", "240",
         "--no-random-failures"],
        env=child_env(), stdout=plant_log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
    )
    mes = subprocess.Popen(
        [sys.executable, "-m", "werkhalle.mes", "--plant-url", url, "--http-port", str(http_port), "--data-dir", str(tmp / "data"),
         "--seed", "7", "--solver-limit", str(SOLVER_LIMIT), "--no-auto-supply"],
        env=child_env(), stdout=mes_log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
    )
    http = httpx.Client(base_url=f"http://127.0.0.1:{http_port}", trust_env=False, timeout=15.0)
    try:
        def ready():
            assert mes.poll() is None, (tmp / "mes.log").read_text(encoding="utf-8")
            assert plant.poll() is None, (tmp / "plant.log").read_text(encoding="utf-8")
            try:
                health = http.get("/api/health").json()
            except httpx.HTTPError:
                return None
            return health if health["ok"] and health["plan_revision"] >= 1 else None

        wait_for(ready, 120, 0.25, "plan revision 1")
        yield System(http=http, plant_port=plant_port, http_port=http_port, tmp=tmp, plant=plant, mes=mes)
    finally:
        http.close()
        stop_tree(mes)
        stop_tree(plant)
        plant_log.close()
        mes_log.close()
    assert port_is_free(plant_port) and port_is_free(http_port), "a port is still taken after the stop"
    log = (tmp / "mes.log").read_text(encoding="utf-8")
    assert "Traceback" not in log, log


ASSIGNMENT = "op_id kind machine start end setup_before batch_id lot_id order_id product_id state frozen"
METRICS = "makespan total_tardiness weighted_tardiness late_orders max_tardiness setups setup_minutes objective order_completion"
CANDIDATE = "solver label status valid violations solve_seconds best_bound iterations note metrics assignments downtimes twin"
TWIN = "makespan total_tardiness late_orders setups energy_kwh unfinished_orders stalled speedup"
ORDER = ("id product_id product_name qty_bars due_min weight created_min status delivered_pieces lots_total lots_done "
         "planned_completion lateness_min late completed_min lots")
PROPOSAL = "id status status_reason trigger chain based_on_revision age_min keep candidates recommended beats_keep summary delta"
CHAIN = "injected_wall_ts source_ts detected_wall_ts latency_ms forked_wall_ts solved_wall_ts twin_wall_ts ready_wall_ts replan_seconds snapshot_seq"
ALARM = "id machine alarm_id code text severity t source_ts detected_wall_ts latency_ms active acked cleared_t"


def check_material(material: dict) -> None:
    has_keys(material, "t transports buffers in_machine", "material")
    has_keys(material["buffers"], "BILLET_STORE AGE_IN SAW_IN PACK_IN SHIP", "buffers")
    has_keys(material["buffers"]["SHIP"], "packages pieces kg lots_total last_lots", "SHIP")
    assert set(material["buffers"]["BILLET_STORE"]) == {"6060", "6063", "6082"}
    for transport in material["transports"]:
        assert set(transport) == {"id", "kind", "lot_id", "count", "from", "to", "t0", "t1"}
    for row in material["in_machine"].values():
        has_keys(row, "lot_ids kind billets_left baskets")


def check_state(state: dict) -> None:
    has_keys(state, "run_id connected stale sim machines material kpi plan proposal alarms_active stalled counters", "state")
    has_keys(state["sim"], "t factor wall_ts random_failures auto_supply", "sim")
    assert [m["id"] for m in state["machines"]] == MACHINE_IDS
    for machine in state["machines"]:
        has_keys(machine, "id name kind unit capacity status status_since temperature setpoint power_kw energy_kwh total good scrap "
                          "setup_class op progress alarm est_repair_min fault_count oee next_op waiting_on trend", machine["id"])
        assert machine["status"] in ("IDLE", "RUN", "SETUP", "FAULT")
        assert machine["waiting_on"] in (None, "Material", "Vorgänger", "Störung", "Kein Auftrag", "Kein Plan", "Entscheidung offen")
        has_keys(machine["alarm"], "active id code text severity", "alarm")
        has_keys(machine["oee"], "oee oee_iso availability performance quality", "oee")
        has_keys(machine["trend"], "temperature power_kw", "trend")
        if machine["op"]:
            has_keys(machine["op"], "op_ids kind lot_ids order_ids product_id phase batch_id", "op")
    check_material(state["material"])
    has_keys(state["kpi"], "oee_plant power_kw energy_kwh kwh_per_kg packages kg_shipped orders_open orders_late lots_open lots_done", "kpi")
    has_keys(state["plan"], "revision solver source created_min metrics", "plan")
    has_keys(state["plan"]["metrics"], "makespan total_tardiness late_orders setups setup_minutes objective", "plan.metrics")
    has_keys(state["counters"], "datachanges_total datachanges_per_s events_total last_event_seq", "counters")


def test_health(system):
    health = system.ok("/api/health")
    has_keys(health, "ok connected run_id plan_revision plant_module_loaded snapshot_calls pool_plant_module_loaded version", "health")
    assert health["ok"] is True and health["connected"] is True
    assert health["plant_module_loaded"] is False
    assert health["pool_plant_module_loaded"] is False
    assert health["snapshot_calls"] >= 1
    assert health["version"] == "1"


def test_state_shape_and_size(system):
    response = system.get("/api/state")
    state = response.json()
    check_state(state)
    assert state["connected"] is True and state["stale"] is False
    assert len(response.content) < 12 * 1024


def test_state_stays_under_the_limit_when_everything_is_full(system):
    """Runs the builder against full trends in a throwaway Runtime, which a live plant would need minutes to reach."""
    from werkhalle.mes.runtime import STATE_LIMIT_BYTES

    runtime = Runtime(plant_url="opc.tcp://127.0.0.1:1/", data_dir=str(system.tmp), client=MagicMock())
    runtime.client.run_id = "r-test"
    runtime.client.machine_ids.return_value = MACHINE_IDS
    runtime.client.connected = True
    runtime.client.stale = False
    runtime.client.counters.return_value = {"datachanges_total": 123456, "datachanges_per_s": 180, "events_total": 9000, "last_event_seq": 9000}
    state = system.state()
    runtime.shadow.to_state_machines = lambda trend_samples=120: [
        {**m, "op": {"op_ids": ["L-00001:age", "L-00002:age", "L-00003:age"], "kind": "age", "lot_ids": ["L-00001", "L-00002", "L-00003"],
                     "order_ids": ["A-1001", "A-1002"], "product_id": "FR-60", "phase": "RUN", "batch_id": "B-r3-7"},
         "trend": {"temperature": [488] * trend_samples, "power_kw": [451] * trend_samples}}
        for m in state["machines"]
    ]
    busy_material = dict(state["material"])
    busy_material["buffers"] = dict(busy_material["buffers"])
    busy_material["buffers"]["AGE_IN"] = [{"lot_id": f"L-{i:05d}", "baskets": 3} for i in range(20)]
    busy_material["transports"] = [{"id": f"T-{i}", "kind": "baskets", "lot_id": f"L-{i:05d}", "count": 3, "from": "PR1", "to": "AGE_IN", "t0": 120.0, "t1": 128.0} for i in range(6)]
    runtime.shadow._material = busy_material
    text = runtime.state_text()
    assert len(text.encode("utf-8")) <= STATE_LIMIT_BYTES
    parsed = json.loads(text)
    assert parsed["machines"][0]["trend"]["temperature"], "the trends shrink, they do not vanish"


def test_static_files(system):
    index = system.get("/")
    assert index.status_code == 200 and "Werkhalle" in index.text
    script = system.get("/app.js")
    assert script.status_code == 200
    assert "javascript" in script.headers["content-type"]
    assert "css" in system.get("/css/tokens.css").headers["content-type"]


def test_catalog(system):
    data = system.ok("/api/catalog")
    has_keys(data, "machines products recipes time_factors", "catalog")
    assert [m["id"] for m in data["machines"]] == MACHINE_IDS
    has_keys(data["machines"][1], "id name kind unit capacity faults")
    has_keys(data["machines"][1]["faults"][0], "code text repair_mean_min")
    assert {p["id"] for p in data["products"]} == set(catalog.PRODUCTS)
    has_keys(data["products"][0], "id name alloy recipe die presses kg_per_bar bars_per_billet min_lead_min")
    has_keys(data["recipes"][0], "id temp_c soak_min")
    assert data["time_factors"] == [0, 60, 120, 240, 480]


def test_seeded_order_book(system):
    orders = system.ok("/api/orders")["orders"]
    assert 10 <= len(orders) <= 16
    has_keys(orders[0], ORDER, "order")
    has_keys(orders[0]["lots"][0], "id n_billets stage state machine", "lot")
    assert {o["product_id"] for o in orders} == set(catalog.PRODUCTS)
    assert 18 <= sum(o["lots_total"] for o in orders) <= 30
    for order in orders:
        assert order["status"] in ("offen", "in_arbeit", "fertig", "unterlieferung")
        assert order["due_min"] - order["created_min"] >= catalog.min_lead_min(catalog.PRODUCTS[order["product_id"]])
        for lot in order["lots"]:
            assert lot["stage"] in ("press", "age", "saw", "pack", "fertig")
            assert lot["state"] in ("wartend", "laeuft", "fertig")
    assert any(o["planned_completion"] is not None for o in orders)


def test_plan_shape(system):
    plan = system.ok("/api/plan")
    has_keys(plan, "revision solver source source_proposal_id created_min now metrics assignments downtimes orders", "plan")
    has_keys(plan["metrics"], METRICS, "metrics")
    has_keys(plan["assignments"][0], ASSIGNMENT, "assignment")
    has_keys(plan["orders"][0], "id due weight")
    assert plan["revision"] >= 1 and plan["solver"] in ("cpsat", "anneal", "keep")
    assert {a["state"] for a in plan["assignments"]} <= {"pending", "running", "done"}


def test_plant_actually_runs_after_start(system):
    start = system.state()["sim"]["t"]
    wait_for(lambda: system.state()["sim"]["t"] > start + 2, 10, what="time moving")
    wait_for(lambda: any(m["status"] in ("RUN", "SETUP") for m in system.state()["machines"]), 30, what="a machine working")
    wait_for(lambda: system.state()["counters"]["events_total"] > 10, 30, what="trace events")


def test_post_order_validates_and_rolls_the_plan(system):
    too_soon = system.post("/api/orders", {"product_id": "KK-80", "qty_bars": 200, "due_in_min": 5})
    assert too_soon.status_code == 400
    body = too_soon.json()
    assert body["error"] == "due_too_soon" and body["min_lead_min"] == catalog.min_lead_min(catalog.PRODUCTS["KK-80"]) and body["message"]
    assert system.post("/api/orders", {"product_id": "NOPE", "qty_bars": 20, "due_in_min": 2000}).status_code == 400
    assert system.post("/api/orders", {"product_id": "KK-80", "qty_bars": 0, "due_in_min": 2000}).status_code == 400

    system.clear_proposal()
    before = system.ok("/api/plan")["revision"]
    created = system.post("/api/orders", {"product_id": "KK-80", "qty_bars": 200, "due_in_min": 2000, "rush": True})
    assert created.status_code == 200, created.text
    has_keys(created.json(), "order replan")
    order = created.json()["order"]
    has_keys(order, ORDER, "new order")
    assert order["weight"] == 3 and order["status"] == "offen"

    def in_plan():
        plan = system.ok("/api/plan")
        return plan if any(a["order_id"] == order["id"] for a in plan["assignments"]) else None

    plan = wait_for(in_plan, 40, 0.25, "the new order in the plan")
    assert plan["revision"] > before
    assert plan["source"] == "rolling"
    row = next(o for o in system.ok("/api/orders")["orders"] if o["id"] == order["id"])
    assert row["planned_completion"] is not None and row["lateness_min"] is not None


def test_solve_candidates_and_apply(system):
    system.repair_all()
    started = system.post("/api/plan/solve", {"time_limit_s": 1, "seed": 3}).json()
    assert started["status"] == "computing" and started["id"].startswith("cs-")

    def ready():
        data = system.ok(f"/api/plan/candidates/{started['id']}")
        return data if data["status"] != "computing" else None

    result = wait_for(ready, 40, 0.25, "candidates")
    assert result["status"] == "ready", result
    has_keys(result, "id status created_min time_limit_s lots ops candidates recommended stale", "candidate set")
    assert set(result["candidates"]) == {"cpsat", "anneal", "keep"}
    assert result["lots"] > 0 and result["ops"] > 0
    for candidate in result["candidates"].values():
        has_keys(candidate, CANDIDATE, "candidate")
        if candidate["valid"]:
            has_keys(candidate["twin"], TWIN, "candidate twin")
    assert result["candidates"][result["recommended"]]["valid"] is True

    assert system.post("/api/plan/apply", {"candidates_id": "cs-nope", "solver": "anneal"}).status_code == 404
    before = system.ok("/api/plan")["revision"]
    answer = system.post("/api/plan/apply", {"candidates_id": started["id"], "solver": result["recommended"]})
    if answer.status_code == 409:
        # Only a plan that moved on in between may refuse this; with no other writer here that is a rolling replan.
        assert answer.json()["error"] == "veraltet"
        assert system.ok("/api/plan")["revision"] > before or result["stale"]
    else:
        assert answer.status_code == 200, answer.text
        assert answer.json()["revision"] == before + 1
        plan = system.ok("/api/plan")
        assert plan["revision"] == before + 1 and plan["source"] == "manual"
        assert system.ok(f"/api/plan/candidates/{started['id']}")["stale"] is True
        second = system.post("/api/plan/apply", {"candidates_id": started["id"], "solver": result["recommended"]})
        assert second.status_code == 409 and second.json()["error"] == "veraltet"


def proposal_for(system: System, machine: str, code: str, repair_min: float = 480.0) -> dict:
    system.repair_all()
    sent = system.post(f"/api/machines/{machine}/fault", {"code": code, "repair_min": repair_min})
    assert sent.status_code == 200, sent.text
    return wait_for(lambda: system.open_proposal(), 40, 0.1, f"a proposal for {machine}")


def test_fault_rest_only_sends_and_the_chain_is_measured(system):
    system.repair_all()
    assert system.post("/api/machines/XX/fault", {}).status_code == 404
    assert system.post("/api/machines/PR1/fault", {"code": "G-101"}).status_code == 400

    sent = system.post("/api/machines/PR1/fault", {"code": "P-202", "repair_min": 480})
    assert sent.status_code == 200, sent.text
    has_keys(sent.json(), "ok sent_wall_ts")
    sent_wall = sent.json()["sent_wall_ts"]

    second = None
    wait_for(lambda: system.machine("PR1")["status"] == "FAULT", 10, 0.02, "FAULT through the DataChange")
    second = system.post("/api/machines/PR1/fault", {"code": "P-202"})
    assert second.status_code == 409 and second.json()["error"] == "ALREADY_FAULTED"
    assert system.post("/api/machines/SAE/repair").status_code == 409

    changes = system.ok("/api/opcua/changes?limit=500")["changes"]
    status_row = next(c for c in changes if c["path"] == "Werkhalle/Machines/PR1/Status" and c["value"] == "FAULT")
    # The status reached the MES after the REST call went out, and as a DataChange with the plant's timestamp.
    assert status_row["received_wall_ts"] >= status_row["source_ts"] >= sent_wall - 0.001

    state = system.state()
    alarm = next(a for a in state["alarms_active"] if a["machine"] == "PR1")
    has_keys(alarm, ALARM, "alarm")
    assert alarm["source_ts"] == pytest.approx(status_row["source_ts"], abs=1e-3)

    proposal = wait_for(lambda: system.open_proposal(), 40, 0.1, "the proposal")
    has_keys(proposal, PROPOSAL, "proposal")
    has_keys(proposal["trigger"], "kind machine code text est_repair_min t", "trigger")
    assert proposal["trigger"]["kind"] == "fault" and proposal["trigger"]["machine"] == "PR1" and proposal["trigger"]["code"] == "P-202"
    assert proposal["trigger"]["est_repair_min"] == 480
    chain = proposal["chain"]
    has_keys(chain, CHAIN, "chain")
    assert chain["injected_wall_ts"] == pytest.approx(sent_wall, abs=1e-3)
    assert chain["injected_wall_ts"] <= chain["source_ts"] + 0.001
    assert chain["source_ts"] <= chain["detected_wall_ts"] <= chain["forked_wall_ts"] <= chain["solved_wall_ts"]
    assert chain["solved_wall_ts"] <= chain["twin_wall_ts"] <= chain["ready_wall_ts"]
    assert chain["latency_ms"] is not None and 0 <= chain["latency_ms"] < 5000
    assert chain["replan_seconds"] > 0 and chain["snapshot_seq"] > 0
    has_keys(proposal["delta"], "makespan total_tardiness late_orders setups", "delta")
    has_keys(proposal["keep"], CANDIDATE, "keep")
    assert set(proposal["candidates"]) == {"cpsat", "anneal"}
    has_keys(proposal["candidates"]["cpsat"], CANDIDATE, "cpsat")
    assert proposal["summary"]
    assert proposal["recommended"] in ("cpsat", "anneal")

    summary = system.state()["proposal"]
    assert summary["id"] == proposal["id"] and summary["status"] == "open"
    assert any(row["machine"] == "PR1" for row in proposal["keep"]["downtimes"])


def test_accept_makes_the_plan_and_keep_is_refused(system):
    proposal = proposal_for(system, "OF1", "O-301")
    assert system.post(f"/api/proposals/{proposal['id']}/accept", {"solver": "keep"}).status_code == 422
    before = system.ok("/api/plan")["revision"]
    accepted = system.post(f"/api/proposals/{proposal['id']}/accept", {})
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["revision"] == before + 1
    plan = system.ok("/api/plan")
    assert plan["revision"] == before + 1
    assert plan["source"] == "proposal" and plan["source_proposal_id"] == proposal["id"]
    assert any(d["machine"] == "OF1" and d["reason"] == "O-301" for d in plan["downtimes"])
    closed = system.ok("/api/proposals")
    assert closed["open"] is None
    entry = next(p for p in closed["history"] if p["id"] == proposal["id"])
    assert entry["status"] == "accepted" and "assignments" not in entry["keep"]
    assert system.post(f"/api/proposals/{proposal['id']}/accept", {}).status_code == 409


def test_reject_closes_the_proposal(system):
    proposal = proposal_for(system, "SAE", "S-401")
    assert system.post(f"/api/proposals/{proposal['id']}/reject").json() == {"ok": True}
    assert system.ok("/api/proposals")["open"] is None
    assert system.post("/api/proposals/pr-999/reject").status_code == 404


def test_repair_supersedes_the_open_proposal_and_a_late_accept_gets_409(system):
    proposal = proposal_for(system, "VP", "V-501")
    assert system.post("/api/machines/VP/repair").status_code == 200
    wait_for(lambda: system.ok("/api/proposals")["open"] is None, 10, what="superseded")
    entry = next(p for p in system.ok("/api/proposals")["history"] if p["id"] == proposal["id"])
    assert entry["status"] == "superseded" and entry["status_reason"]
    late = system.post(f"/api/proposals/{proposal['id']}/accept", {})
    assert late.status_code == 409
    has_keys(late.json(), "error message new_proposal_id")
    assert late.json()["error"] == "veraltet"


def test_new_fault_keeps_the_open_proposal_until_its_re_solve_replaces_it(system):
    first = proposal_for(system, "PR1", "P-201")
    assert system.post("/api/machines/OF2/fault", {"code": "O-302", "repair_min": 480}).status_code == 200

    def noted():
        current = system.ok("/api/proposals")["open"]
        return current if current is not None and current["id"] == first["id"] and current["pending_update"] else None

    note = wait_for(noted, 10, 0.05, "the note on the open proposal")
    assert note["status"] == "open" and note["pending_update"]["machine"] == "OF2"

    def replaced():
        current = system.ok("/api/proposals")["open"]
        return current if current is not None and current["id"] != first["id"] else None

    ready = wait_for(replaced, 40, 0.1, "the replacement")
    assert ready["status"] == "open", "never a gap without an accept button"
    assert ready["trigger"]["machine"] == "PR1" and ready["pending_update"] is None
    assert [row["machine"] for row in ready["trigger"]["also"]] == ["OF2"]
    has_keys(ready["trigger"]["also"][0], "machine code text remaining_min")
    assert ready["chain"]["detected_wall_ts"] == first["chain"]["detected_wall_ts"], "the re-solve keeps the real chain"
    assert ready["chain"]["injected_wall_ts"] == first["chain"]["injected_wall_ts"] is not None
    entry = next(p for p in system.ok("/api/proposals")["history"] if p["id"] == first["id"])
    assert entry["status"] == "superseded"
    # Both faulted machines are in the plan: the snapshot brings every one of them.
    machines = {d["machine"] for d in ready["candidates"]["cpsat"]["downtimes"]}
    assert {"PR1", "OF2"} <= machines
    system.repair_all()


def test_short_fault_through_this_mes_still_gets_its_proposal(system):
    """The operator asked for the chain. Skipping short faults is for random and external ones."""
    system.clear_proposal()
    system.repair_all()
    assert system.post("/api/machines/VP/fault", {"code": "V-502", "repair_min": 1}).status_code == 200
    wait_for(lambda: any(p["trigger"]["machine"] == "VP" for p in [*system.ok("/api/proposals")["history"], system.ok("/api/proposals")["open"] or {"trigger": {}}]),
             15, 0.1, "a proposal for the short fault")
    system.clear_proposal()
    system.repair_all() if system.machine("VP")["status"] == "FAULT" else None


def test_alarms_list_and_ack(system):
    system.repair_all()
    assert system.post("/api/machines/SAE/fault", {"code": "S-402", "repair_min": 480}).status_code == 200
    alarm = wait_for(lambda: next((a for a in system.state()["alarms_active"] if a["machine"] == "SAE"), None), 10, 0.05, "the alarm")
    assert alarm["code"] == "S-402" and alarm["severity"] == 900 and alarm["acked"] is False
    assert system.post(f"/api/alarms/{alarm['id']}/ack").status_code == 200
    rows = system.ok("/api/alarms?limit=10")["alarms"]
    row = next(r for r in rows if r["id"] == alarm["id"])
    assert row["acked"] is True and row["cleared_t"] is None
    has_keys(row, ALARM, "alarm row")
    assert system.post("/api/alarms/SAE:9999/ack").status_code == 404
    assert system.post("/api/machines/SAE/repair").status_code == 200

    def cleared():
        return next(r for r in system.ok("/api/alarms?limit=10")["alarms"] if r["id"] == alarm["id"])["cleared_t"] is not None

    wait_for(cleared, 10, 0.05, "the clear")
    system.repair_all()


def test_settings(system):
    assert system.post("/api/plant/settings", {"time_factor": 77}).status_code == 400
    assert system.post("/api/plant/settings", {"time_factor": 480, "random_failures": False, "auto_supply": False}).json() == {"ok": True}
    wait_for(lambda: system.state()["sim"]["factor"] == 480, 5, 0.05, "the factor to reach the shadow")
    assert system.state()["sim"]["auto_supply"] is False
    assert system.post("/api/plant/settings", {"time_factor": 240}).status_code == 200
    wait_for(lambda: system.state()["sim"]["factor"] == 240, 5, 0.05, "the factor to return")


def test_log(system):
    entries = system.ok("/api/log?limit=5")["entries"]
    assert 0 < len(entries) <= 5
    has_keys(entries[0], "t wall_ts level source text")
    assert entries[0]["level"] in ("info", "warn", "error")
    assert entries[0]["source"] in ("opcua", "mes", "plan", "twin")


def test_oee_and_energy(system):
    wait_for(lambda: system.ok("/api/oee?window=start")["machines"][1]["minutes"]["run"] > 0, 60, 0.5, "run minutes")
    for window in ("start", "8h"):
        oee = system.ok(f"/api/oee?window={window}")
        has_keys(oee, "window t plant machines trend", "oee")
        assert oee["window"] == window
        has_keys(oee["plant"], "oee oee_iso availability performance quality")
        assert [m["id"] for m in oee["machines"]] == MACHINE_IDS
        has_keys(oee["machines"][0], "id name oee oee_iso availability availability_iso performance quality minutes counts fault_count mtbf_min mttr_min")
        has_keys(oee["machines"][0]["minutes"], "run setup fault idle no_demand starved planned planned_net ideal")
        has_keys(oee["machines"][0]["counts"], "total good scrap unit")
        has_keys(oee["trend"], "t oee")
        for machine in oee["machines"]:
            for key in ("oee", "oee_iso", "availability", "performance", "quality"):
                assert machine[key] is None or 0.0 <= machine[key] <= 1.0, (machine["id"], key, machine[key])
    assert system.get("/api/oee?window=year").status_code == 400

    energy = system.ok("/api/energy")
    has_keys(energy, "t total_kwh power_kw kwh_per_kg machines trend by_order", "energy")
    assert [m["id"] for m in energy["machines"]] == MACHINE_IDS
    has_keys(energy["machines"][0], "id name energy_kwh power_kw share kwh_per_unit unit")
    assert energy["total_kwh"] > 0
    assert sum(m["share"] for m in energy["machines"]) == pytest.approx(1.0, abs=0.02)
    has_keys(energy["trend"], "t kw")
    assert "total" in energy["trend"]["kw"]
    if energy["by_order"]:
        has_keys(energy["by_order"][0], "order_id energy_kwh kg kwh_per_kg")


def test_idle_machines_are_classified(system):
    """waiting_on feeds OEE: a machine with nothing to do is no_demand, one with a blocked head op is starved."""
    wait_for(lambda: any(m["waiting_on"] for m in system.state()["machines"]), 30, 0.25, "an idle machine with a reason")
    machines = system.ok("/api/oee?window=start")["machines"]
    wait_for(lambda: any(m["minutes"]["starved"] > 0 or m["minutes"]["no_demand"] > 0 for m in system.ok("/api/oee?window=start")["machines"]),
             30, 0.5, "idle minutes classified")
    assert machines


def test_trace(system):
    results = wait_for(lambda: system.ok("/api/trace/search?q=S-000")["results"], 10, 0.2, "melts in the trace")
    has_keys(results[0], "id type label t")
    assert all("S-000" in r["id"] for r in results)
    assert len(system.ok("/api/trace/search?q=-")["results"]) <= 30
    assert system.ok("/api/trace/search?q=")["results"] == []
    assert any(r["id"].startswith("A-") for r in system.ok("/api/trace/search?q=A-10")["results"]), "orders are searchable from creation"

    recent = system.ok("/api/trace/recent?limit=20")["results"]
    assert recent and all(r["type"] in ("melt", "package") for r in recent)

    melt = next(r for r in results if r["type"] == "melt")
    graph = system.ok(f"/api/trace/graph/{melt['id']}")
    has_keys(graph, "root root_type truncated direction_note nodes edges summary recall_label", "graph")
    assert graph["root"] == melt["id"] and graph["root_type"] == "melt"
    assert graph["recall_label"] == "möglich betroffen"
    has_keys(graph["nodes"][0], "id type label t qty unit attrs relation", "node")
    has_keys(graph["summary"], "packages orders pieces melts loads kg", "summary")
    ids = {n["id"] for n in graph["nodes"]}
    assert all(e["from"] in ids and e["to"] in ids for e in graph["edges"])
    missing = system.get("/api/trace/graph/GIBTS-NICHT")
    assert missing.status_code == 404 and missing.json()["error"] == "not_found"


def test_twin_run_and_two_step_apply(system):
    system.repair_all()
    assert system.post("/api/twin/run", {"scenario": [{"type": "nonsense"}]}).status_code == 400
    assert system.post("/api/twin/run", {"scenario": [{"type": "machine_down", "machine": "XX", "duration_min": 10}]}).status_code == 400
    started = system.post("/api/twin/run", {
        "scenario": [{"type": "machine_down", "machine": "PR2", "at_offset_min": 0, "duration_min": 240},
                     {"type": "rush_order", "product_id": "KK-80", "qty_bars": 200, "due_in_min": 600}],
        "monte_carlo": 2, "seed": 1, "time_limit_s": 1,
    }).json()
    assert started["status"] == "computing" and started["id"].startswith("tw-")

    def done():
        data = system.ok(f"/api/twin/runs/{started['id']}")
        return data if data["status"] != "computing" else None

    run = wait_for(done, 60, 0.25, "the twin run")
    assert run["status"] == "ready", run
    has_keys(run, "id status created_min forked_seq scenario note variants candidates recommended delta_b_vs_a delta_c_vs_b monte_carlo applicable", "run")
    assert set(run["variants"]) == {"A", "B", "C"}
    for variant in run["variants"].values():
        has_keys(variant, "label kpis order_completion speedup wall_seconds sim_minutes assignments frames", "variant")
        has_keys(variant["kpis"], "makespan total_tardiness late_orders setups energy_kwh unfinished_orders stalled", "kpis")
        assert 0 < len(variant["frames"]) <= 600
        has_keys(variant["frames"][0], "t machines material", "frame")
        assert set(variant["frames"][0]["machines"]) == set(MACHINE_IDS)
    assert run["variants"]["C"]["solver"] in ("cpsat", "anneal")
    has_keys(run["delta_b_vs_a"], "makespan total_tardiness")
    assert run["monte_carlo"]["n"] == 2
    has_keys(run["monte_carlo"]["B"]["makespan"], "p10 p50 p90")
    has_keys(run["candidates"]["cpsat"], CANDIDATE, "twin candidate")
    assert run["forked_seq"] > 0

    orders_before = {o["id"] for o in system.ok("/api/orders")["orders"]}
    # A twin run touches nothing real.
    assert not any(o.startswith("Z-") for o in orders_before)
    answer = system.post(f"/api/twin/runs/{run['id']}/apply", {"solver": run["recommended"]})
    assert answer.status_code == 200, answer.text
    proposal_id = answer.json()["proposal_id"]

    def proposal_ready():
        current = system.ok("/api/proposals")["open"]
        return current if current is not None and current["id"] == proposal_id and current["status"] == "open" else None

    proposal = wait_for(proposal_ready, 40, 0.2, "the scenario proposal")
    assert proposal["trigger"]["kind"] == "scenario"
    new_orders = {o["id"] for o in system.ok("/api/orders")["orders"]} - orders_before
    assert len(new_orders) == 1 and not any(o.startswith("Z-") for o in new_orders)
    # The second step plans from live state, so the hypothetical PR2 downtime is not in it.
    assert not any(d["machine"] == "PR2" for d in proposal["candidates"]["cpsat"]["downtimes"])
    assert not any(a["op_id"].startswith("Z-") or (a["batch_id"] or "").startswith("Z-") for a in proposal["candidates"]["cpsat"]["assignments"])
    assert system.post(f"/api/twin/runs/{run['id']}/apply", {"solver": run["recommended"]}).status_code == 409
    accepted = system.post(f"/api/proposals/{proposal_id}/accept", {})
    assert accepted.status_code == 200, accepted.text


def test_opcua_monitor(system):
    nodes = system.ok("/api/opcua/nodes")
    has_keys(nodes, "endpoint namespace nodes")
    assert nodes["namespace"] == "urn:werkhalle:plant"
    assert str(system.plant_port) in nodes["endpoint"]
    status = next(n for n in nodes["nodes"] if n["path"] == "Werkhalle/Machines/PR2/Status")
    has_keys(status, "node_id path machine name type value source_ts changes")
    assert status["machine"] == "PR2" and status["type"] == "String"
    material = next(n for n in nodes["nodes"] if n["name"] == "Material")
    assert material.get("truncated") is True and len(material["value"]) == 120

    changes = system.ok("/api/opcua/changes?limit=5")
    has_keys(changes, "changes per_second")
    assert 0 < len(changes["changes"]) <= 5
    has_keys(changes["changes"][0], "node_id path value source_ts received_wall_ts latency_ms")
    assert len(system.ok("/api/opcua/changes?limit=100000")["changes"]) <= 500

    calls = system.ok("/api/opcua/methods")["calls"]
    assert calls
    has_keys(calls[0], "wall_ts method node args result ms")
    assert {c["method"] for c in calls} >= {"StartOperation"}


def test_snapshot_never_refreshes_the_shadow(system):
    """The counter only moves for forks: an idle system neither forks nor polls the snapshot method for its live view."""
    system.repair_all()
    wait_for(lambda: system.state()["proposal"] is None, 10, what="no open proposal")
    time.sleep(0.5)
    before = system.ok("/api/health")["snapshot_calls"]
    for _ in range(5):
        system.state()
        system.ok("/api/plan")
        system.ok("/api/orders")
        time.sleep(0.2)
    assert system.ok("/api/health")["snapshot_calls"] == before


def test_websocket_sends_state_and_discrete_messages(system):
    system.repair_all()
    url = f"ws://127.0.0.1:{system.http_port}/ws"
    with ws_connect(url, proxy=None, open_timeout=10) as socket_:
        first = json.loads(socket_.recv(timeout=10))
        assert first["type"] == "state"
        check_state(first["data"])
        assert system.post("/api/orders", {"product_id": "FR-60", "qty_bars": 64, "due_in_min": 3000}).status_code == 200
        seen: Counter = Counter()
        largest = 0
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline and not {"order", "plan", "log", "state"} <= set(seen):
            raw = socket_.recv(timeout=10)
            message = json.loads(raw)
            seen[message["type"]] += 1
            if message["type"] == "state":
                largest = max(largest, len(raw.encode("utf-8") if isinstance(raw, str) else raw))
        assert {"order", "plan", "log", "state"} <= set(seen), seen
        assert largest < 12 * 1024

    # Faster than about 4 Hz would be a bug in the loop, far slower one in the slot logic.
    with ws_connect(url, proxy=None, open_timeout=10) as socket_:
        started = time.monotonic()
        states = 0
        while time.monotonic() - started < 3.0:
            if json.loads(socket_.recv(timeout=5))["type"] == "state":
                states += 1
        assert 4 <= states <= 20, states


def test_websocket_client_that_leaves_is_forgotten(system):
    url = f"ws://127.0.0.1:{system.http_port}/ws"
    for _ in range(3):
        with ws_connect(url, proxy=None, open_timeout=10) as socket_:
            socket_.recv(timeout=10)
    # Still alive and answering after clients came and went.
    assert system.ok("/api/health")["ok"] is True


def test_errors_use_the_contract_envelope(system):
    for response in (system.get("/api/plan/candidates/nope"), system.get("/api/twin/runs/nope"), system.post("/api/machines/XX/repair"),
                     system.get("/api/gibtsnicht")):
        assert response.status_code == 404
        has_keys(response.json(), "error message", "error")
    bad = system.http.post("/api/orders", content=b"{kein json", headers={"content-type": "application/json"})
    assert bad.status_code == 400
    has_keys(bad.json(), "error message", "error")


def test_plan_stays_valid_and_every_lot_is_accounted_for(system):
    """Whatever the earlier tests did to the plant, the plan has an op for every open lot and no op of a finished one twice."""
    system.repair_all()
    plan = system.ok("/api/plan")
    planned_lots = {a["lot_id"] for a in plan["assignments"] if a["lot_id"]}
    for order in system.ok("/api/orders")["orders"]:
        for lot in order["lots"]:
            if lot["stage"] == "press" and lot["state"] == "wartend":
                # Rolling replans run within seconds; a lot may wait that long, not longer.
                def in_plan():
                    return lot["id"] in {a["lot_id"] for a in system.ok("/api/plan")["assignments"]}

                assert lot["id"] in planned_lots or wait_for(in_plan, 30, 0.5, f"{lot['id']} in the plan")
    ids = [a["op_id"] for a in plan["assignments"]]
    assert len(ids) == len(set(ids))


def _make_change(machine: str, name: str, value, source_ts: float, *, received: float | None = None) -> DataChange:
    received = source_ts + 0.05 if received is None else received
    return DataChange(node_id=f"ns=2;s=Werkhalle.{machine}.{name}", path=f"Werkhalle/Machines/{machine}/{name}", machine=machine,
                      name=name, value=value, source_ts=source_ts, received_ts=received, latency_ms=(received - source_ts) * 1000.0,
                      baseline=False)


def _fake_client() -> MagicMock:
    client = MagicMock()
    client.connected = True
    client.stale = False
    client.run_id = "r-fake"
    client.inject_fault = AsyncMock(return_value={"ok": True, "reason": ""})
    client.repair = AsyncMock(return_value={"ok": True, "reason": ""})
    return client


async def test_inject_fault_does_not_touch_mes_state_until_the_datachange(tmp_path):
    """With a plant that never answers with a DataChange, nothing may change. Then the DataChange does all of it."""
    runtime = Runtime(plant_url="opc.tcp://127.0.0.1:1/", data_dir=str(tmp_path), client=_fake_client())
    runtime.shadow.attach(runtime.client, alarms=runtime.alarms)
    t0 = time.time()
    await runtime.shadow.apply_batch([_make_change("PR2", "Status", "RUN", t0 - 1.0)])

    answer = await runtime.inject_fault("PR2", {"code": "P-204", "repair_min": 240})
    assert answer["ok"] is True and answer["sent_wall_ts"] >= t0
    runtime.client.inject_fault.assert_awaited_once_with("PR2", "P-204", 240.0)
    assert runtime.shadow.status("PR2") == "RUN", "the REST call must not mark the machine"
    assert runtime.alarms.active() == []
    assert runtime._open is None

    # Bootstrapped with a plan: the fault's proposal is only opened by the DataChange. The pipeline itself is not running here.
    runtime._bootstrapped = True
    runtime.plan = object()
    runtime.revision = 1
    runtime.shadow._plant["TimeFactor"] = 120.0
    source = time.time()
    await runtime.shadow.apply_batch([
        _make_change("PR2", "AlarmCode", "P-204", source),
        _make_change("PR2", "AlarmText", "Hauptzylinder undicht", source),
        _make_change("PR2", "AlarmSeverity", 900, source),
        _make_change("PR2", "AlarmId", 3, source),
        _make_change("PR2", "AlarmActive", True, source),
        _make_change("PR2", "EstimatedRepairMin", 240.0, source),
        _make_change("PR2", "FaultCount", 1, source),
        _make_change("PR2", "Status", "FAULT", source),
    ])
    assert runtime.shadow.status("PR2") == "FAULT"
    proposal = runtime._open
    assert proposal is not None and proposal.status == "computing"
    assert proposal.trigger["machine"] == "PR2" and proposal.trigger["code"] == "P-204" and proposal.trigger["est_repair_min"] == 240.0
    assert proposal.chain["injected_wall_ts"] == answer["sent_wall_ts"]
    assert proposal.chain["source_ts"] == source
    assert proposal.chain["detected_wall_ts"] == source + 0.05
    assert proposal.chain["latency_ms"] == pytest.approx(50.0, abs=0.2)
    row = runtime.alarms.active()[0]
    assert row["id"] == "PR2:3" and row["source_ts"] == source


async def test_external_fault_has_no_injected_timestamp_and_refused_call_leaves_none(tmp_path):
    client = _fake_client()
    client.inject_fault = AsyncMock(return_value={"ok": False, "reason": "ALREADY_FAULTED"})
    runtime = Runtime(plant_url="opc.tcp://127.0.0.1:1/", data_dir=str(tmp_path), client=client)
    with pytest.raises(ApiError) as caught:
        await runtime.inject_fault("PR1", {"code": "P-201"})
    assert caught.value.status == 409 and caught.value.code == "ALREADY_FAULTED"
    assert runtime._injected == {}


async def test_inject_fault_rejects_unknown_codes_without_calling_the_plant(tmp_path):
    runtime = Runtime(plant_url="opc.tcp://127.0.0.1:1/", data_dir=str(tmp_path), client=_fake_client())
    for body in ({"code": "G-101"}, {"code": "P-201", "repair_min": -1}, {"code": "P-201", "repair_min": "viel"}):
        with pytest.raises(ApiError) as caught:
            await runtime.inject_fault("PR1", body)
        assert caught.value.status == 400
    runtime.client.inject_fault.assert_not_awaited()
    with pytest.raises(ApiError) as caught:
        await runtime.inject_fault("XX", {})
    assert caught.value.status == 404


def test_order_during_an_open_proposal_goes_onto_the_active_plan_at_once(system):
    proposal = proposal_for(system, "PR1", "P-203")
    revision = system.ok("/api/plan")["revision"]
    created = system.post("/api/orders", {"product_id": "FP-120", "qty_bars": 40, "due_in_min": 4000})
    assert created.status_code == 200, created.text
    assert created.json()["replan"] == proposal["id"]
    lot_ids = {lot["id"] for lot in created.json()["order"]["lots"]}

    def appended():
        current = system.ok("/api/plan")
        return current if lot_ids <= {a["lot_id"] for a in current["assignments"]} else None

    current = wait_for(appended, 6, 0.1, "the new lots in the active plan, without a solver")
    assert current["revision"] > revision and current["source"] == "rolling"
    # Still broken: the appended plan knows that as well, from its own snapshot.
    assert any(d["machine"] == "PR1" for d in current["downtimes"])
    system.clear_proposal()
    system.repair_all()


def _bootstrapped_runtime(tmp_path, client: MagicMock | None = None) -> Runtime:
    runtime = Runtime(plant_url="opc.tcp://127.0.0.1:1/", data_dir=str(tmp_path), client=client or _fake_client())
    runtime.db = MagicMock()
    runtime.trace = MagicMock()
    runtime.trace.delivered_pieces.return_value = 0
    runtime.tracker = MagicMock()
    runtime.tracker.op_state.return_value = "pending"
    runtime._bootstrapped = True
    runtime.plan = MagicMock()
    runtime.revision = 4
    runtime.shadow._plant["TimeFactor"] = 120.0
    runtime.shadow._plant["SimTimeMin"] = 100.0
    return runtime


def _open_proposal(runtime: Runtime, machine: str = "PR2") -> "object":
    proposal = runtime._new_proposal(
        trigger={"kind": "fault", "machine": machine, "code": "P-204", "text": "x", "est_repair_min": 240.0, "t": 100.0},
        chain={"injected_wall_ts": 1.0, "source_ts": 2.0, "detected_wall_ts": 3.0, "latency_ms": 5.0},
        supersede_reason="",
    )
    proposal.status = "open"
    proposal.based_on_revision = runtime.revision
    proposal.candidates = {"cpsat": {"valid": True, "assignments": [1]}, "anneal": {"valid": False, "assignments": [2]}}
    proposal.keep = {"valid": True, "assignments": [3]}
    proposal.recommended = "cpsat"
    proposal.result = {"melt_counter": 9}
    return proposal


def _set_status(runtime: Runtime, machine: str, status: str) -> None:
    runtime.shadow._machine(machine).values["Status"] = status


def test_accept_refused_by_rebase_answers_409_and_starts_a_new_proposal(tmp_path, monkeypatch):
    runtime = _bootstrapped_runtime(tmp_path)
    proposal = _open_proposal(runtime)
    _set_status(runtime, "PR2", "FAULT")
    monkeypatch.setattr("werkhalle.mes.runtime.plan_from_result", lambda *a, **k: object())
    runtime._rebase = lambda plan: SimpleNamespace(plan=None, reason="Zu wenig Bolzen", dropped=())
    with pytest.raises(ApiError) as caught:
        runtime.accept_proposal(proposal.id, None)
    error = caught.value
    assert error.status == 409 and error.code == "veraltet" and error.message
    assert proposal.status == "superseded"
    fresh = runtime._open
    assert fresh is not None and fresh.id != proposal.id and fresh.status == "computing"
    assert error.extra["new_proposal_id"] == fresh.id
    assert fresh.trigger["machine"] == "PR2" and fresh.chain["source_ts"] == 2.0 and fresh.chain["forked_wall_ts"] is None
    assert runtime.revision == 4, "nothing was applied"
    assert runtime.melt_counter == 1, "nothing was applied, so the counter stays"


def test_accept_on_a_moved_plan_or_a_recovered_machine_is_stale(tmp_path, monkeypatch):
    called = []
    monkeypatch.setattr("werkhalle.mes.runtime.plan_from_result", lambda *a, **k: called.append(1))

    runtime = _bootstrapped_runtime(tmp_path)
    proposal = _open_proposal(runtime)
    runtime.revision = 5
    with pytest.raises(ApiError) as caught:
        runtime.accept_proposal(proposal.id, "cpsat")
    assert caught.value.status == 409 and proposal.status == "superseded"

    runtime = _bootstrapped_runtime(tmp_path)
    proposal = _open_proposal(runtime)
    proposal.assumed_faults = frozenset({"PR2"})
    _set_status(runtime, "PR2", "RUN")
    with pytest.raises(ApiError) as caught:
        runtime.accept_proposal(proposal.id, "cpsat")
    assert caught.value.status == 409 and proposal.status == "superseded"
    assert "PR2 läuft wieder" in caught.value.message, "the 409 names the real reason"
    assert called == [], "a stale candidate is not even rebased"


def test_accept_rejects_keep_invalid_and_computing_candidates(tmp_path):
    runtime = _bootstrapped_runtime(tmp_path)
    proposal = _open_proposal(runtime)
    for solver, status in (("keep", 422), ("anneal", 422), ("nonsense", 422)):
        with pytest.raises(ApiError) as caught:
            runtime.accept_proposal(proposal.id, solver)
        assert caught.value.status == status, solver
    assert proposal.status == "open"
    proposal.status = "computing"
    with pytest.raises(ApiError) as caught:
        runtime.accept_proposal(proposal.id, "cpsat")
    assert caught.value.status == 409 and caught.value.code == "noch_nicht_fertig" and proposal.status == "computing"
    with pytest.raises(ApiError) as caught:
        runtime.accept_proposal("pr-404", None)
    assert caught.value.status == 404


def test_machine_leaving_fault_supersedes_and_replans_what_is_still_down(tmp_path):
    runtime = _bootstrapped_runtime(tmp_path)
    proposal = _open_proposal(runtime)
    proposal.assumed_faults = frozenset({"PR2", "OF1"})
    _set_status(runtime, "PR2", "RUN")
    _set_status(runtime, "OF1", "FAULT")
    runtime.shadow.values("OF1").update(EstimatedRepairMin=300.0, AlarmCode="O-301", AlarmText="Umluftgebläse defekt")
    runtime._on_machine_recovered("PR2")
    assert proposal.status == "superseded" and "läuft wieder" in proposal.status_reason
    fresh = runtime._open
    assert fresh is not None and fresh.trigger["machine"] == "OF1" and fresh.trigger["code"] == "O-301" and fresh.status == "computing"


def test_unrelated_machine_leaving_fault_changes_nothing(tmp_path):
    runtime = _bootstrapped_runtime(tmp_path)
    proposal = _open_proposal(runtime)
    proposal.assumed_faults = frozenset({"PR2"})
    runtime._on_machine_recovered("SAE")
    assert proposal.status == "open" and runtime._open is proposal


def test_secondary_fault_clearing_keeps_the_proposal_and_it_stays_acceptable(tmp_path, monkeypatch):
    runtime = _bootstrapped_runtime(tmp_path)
    proposal = _open_proposal(runtime)
    proposal.assumed_faults = frozenset({"PR2", "OF1"})
    _set_status(runtime, "PR2", "FAULT")
    _set_status(runtime, "OF1", "RUN")
    runtime._on_machine_recovered("OF1")
    assert runtime._open is proposal and proposal.status == "open"
    monkeypatch.setattr("werkhalle.mes.runtime.plan_from_result", lambda *a, **k: "plan")
    monkeypatch.setattr(runtime, "_rebase", lambda plan: SimpleNamespace(plan=None, reason="Zu wenig Bolzen"))
    with pytest.raises(ApiError) as caught:
        runtime.accept_proposal(proposal.id, "cpsat")
    assert "Zu wenig Bolzen" in caught.value.message, "got as far as the rebase, whose reason the 409 carries"


def test_new_fault_on_an_open_proposal_leaves_a_note_and_keeps_it_open(tmp_path):
    runtime = _bootstrapped_runtime(tmp_path)
    proposal = _open_proposal(runtime)
    _set_status(runtime, "OF1", "FAULT")
    runtime._on_fault(FaultEvent(machine="OF1", source_ts=time.time(), received_ts=time.time(), latency_ms=5.0, t=100.0, status="FAULT",
                                 alarm_id=1, code="O-301", text="Gebläse", severity=900, est_repair_min=9000.0, fault_count=1, baseline=False))
    assert runtime._open is proposal and proposal.status == "open"
    assert proposal.pending_update == {"machine": "OF1", "code": "O-301", "text": "Gebläse"}
    assert runtime._proposal_summary()["pending_update"]["machine"] == "OF1"


def test_pending_fault_gets_its_own_proposal_after_a_reject(tmp_path):
    runtime = _bootstrapped_runtime(tmp_path)
    proposal = _open_proposal(runtime)
    _set_status(runtime, "OF1", "FAULT")
    runtime.shadow.values("OF1").update(EstimatedRepairMin=300.0, AlarmCode="O-301", AlarmText="Gebläse")
    runtime._mark_pending(proposal, "OF1", "O-301", "Gebläse")
    runtime.reject_proposal(proposal.id)
    fresh = runtime._open
    assert fresh is not None and fresh.trigger["machine"] == "OF1" and fresh.status == "computing"


def test_re_solve_for_a_remaining_fault_keeps_the_chain(tmp_path):
    runtime = _bootstrapped_runtime(tmp_path)
    _set_status(runtime, "OF1", "FAULT")
    runtime.shadow.values("OF1").update(EstimatedRepairMin=300.0, AlarmCode="O-301", AlarmText="Gebläse")
    runtime._fault_chains["OF1"] = {"injected_wall_ts": 11.0, "source_ts": 12.0, "detected_wall_ts": 13.0, "latency_ms": 62.0}
    runtime._replan_remaining_faults()
    chain = runtime._open.chain
    assert (chain["injected_wall_ts"], chain["source_ts"], chain["detected_wall_ts"], chain["latency_ms"]) == (11.0, 12.0, 13.0, 62.0)


def test_trigger_also_lists_the_other_faults_with_their_remaining_time(tmp_path):
    runtime = _bootstrapped_runtime(tmp_path)
    _set_status(runtime, "PR2", "FAULT")
    _set_status(runtime, "OF1", "FAULT")
    runtime.shadow.values("OF1").update(EstimatedRepairMin=300.0, AlarmCode="O-301", AlarmText="Gebläse")
    runtime.shadow._machine("OF1").status_since = runtime.shadow.sim_time_at() - 40.0
    rows = runtime._other_faults("PR2")
    assert rows == [{"machine": "OF1", "code": "O-301", "text": "Gebläse", "remaining_min": pytest.approx(260.0, abs=1.0)}]


def test_auto_supply_keeps_feeding_the_plant_while_a_proposal_is_undecided(tmp_path):
    runtime = _bootstrapped_runtime(tmp_path)
    runtime.auto_supply = True
    proposal = _open_proposal(runtime)
    runtime._auto_supply()
    assert runtime.book.orders, "the plant must not idle behind an undecided proposal"
    assert runtime._open is proposal and proposal.status == "open"
    spawned = []
    runtime.plan.problem.has_op = lambda op_id: False
    runtime._spawn = lambda coro, name: (coro.close(), spawned.append(name))[0]
    runtime._maybe_roll()
    assert spawned == ["mes-append"], "supplied lots take the same path to the active plan as a manual order"


def test_a_recovered_secondary_machine_leaves_trigger_also(tmp_path):
    runtime = _bootstrapped_runtime(tmp_path)
    proposal = _open_proposal(runtime)
    proposal.trigger["also"] = [{"machine": "OF1", "code": "O-301", "text": "x", "remaining_min": 90.0},
                                {"machine": "SAE", "code": "S-401", "text": "y", "remaining_min": 40.0}]
    runtime._on_machine_recovered("OF1")
    assert [row["machine"] for row in proposal.trigger["also"]] == ["SAE"]
    assert runtime._open is proposal and proposal.status == "open"
    assert [row["machine"] for row in runtime._proposal_summary()["trigger"]["also"]] == ["SAE"]


def test_an_accept_bound_to_fail_is_flagged_and_cleared_again(tmp_path, monkeypatch):
    runtime = _bootstrapped_runtime(tmp_path)
    proposal = _open_proposal(runtime)
    monkeypatch.setattr("werkhalle.mes.runtime.plan_from_result", lambda *a, **k: "plan")
    monkeypatch.setattr(runtime, "_maybe_roll", lambda: None)
    outcome = SimpleNamespace(plan=None, reason="Zu wenig Bolzen für die offenen Lose (6082: 75)")
    monkeypatch.setattr(runtime, "_rebase", lambda plan: outcome)
    runtime.create_order({"product_id": "KK-80", "qty_bars": 200, "due_in_min": 6000})
    assert proposal.pending_update["accept_blocked"] == outcome.reason
    assert proposal.pending_update["text"].startswith("neuer Auftrag")
    monkeypatch.setattr(runtime, "_rebase", lambda plan: SimpleNamespace(plan="rebased", reason=""))
    runtime._flag_doomed_accept(proposal)
    assert "accept_blocked" not in proposal.pending_update and proposal.pending_update["text"].startswith("neuer Auftrag")


def test_a_broken_accept_check_never_costs_the_order(tmp_path, monkeypatch):
    runtime = _bootstrapped_runtime(tmp_path)
    proposal = _open_proposal(runtime)
    monkeypatch.setattr(runtime, "_maybe_roll", lambda: None)
    monkeypatch.setattr("werkhalle.mes.runtime.plan_from_result", MagicMock(side_effect=KeyError("x")))
    answer = runtime.create_order({"product_id": "KK-80", "qty_bars": 200, "due_in_min": 6000})
    assert answer["replan"] == proposal.id and "accept_blocked" not in proposal.pending_update


@pytest.mark.parametrize("seed", ["x", 1e999, 1.5, True, [1]])
def test_a_seed_that_is_no_integer_is_refused(seed):
    from werkhalle.mes.runtime import _seed_of

    with pytest.raises(ApiError) as caught:
        _seed_of(seed)
    assert caught.value.status == 400 and caught.value.code == "bad_request"


def test_a_missing_seed_defaults_and_a_large_one_fits_32_bits():
    from werkhalle.mes.runtime import _seed_of

    assert _seed_of(None) == 1 and _seed_of(7) == 7 and 0 <= _seed_of(2 ** 40 + 3) < 2 ** 30


def test_idle_with_unplanned_lots_reads_kein_plan_and_counts_as_starved(tmp_path):
    from werkhalle.mes.oee import STARVED, idle_reason

    runtime = _bootstrapped_runtime(tmp_path)
    for seeded in seed_orders(3)[:1]:
        runtime.book.add(seeded.order, seeded.lots)
    runtime.plan = None
    _set_status(runtime, "PR1", "IDLE")
    runtime._update_waits()
    assert runtime._heads["PR1"].waiting_on == "Kein Plan"
    assert idle_reason("Kein Plan") == STARVED


def test_idle_behind_an_open_proposal_reads_entscheidung_and_counts_as_starved(tmp_path, monkeypatch):
    from werkhalle.execution.dispatcher import Head
    from werkhalle.mes.oee import STARVED, idle_reason

    runtime = _bootstrapped_runtime(tmp_path)
    for seeded in seed_orders(3)[:1]:
        runtime.book.add(seeded.order, seeded.lots)
    # Every open op is in the plan, but none of it is on this machine.
    runtime.plan = SimpleNamespace(problem=SimpleNamespace(has_op=lambda op_id: True))
    monkeypatch.setattr("werkhalle.mes.runtime.machine_heads",
                        lambda *args, **kwargs: {"PR1": Head("PR1", None, "Kein Auftrag")})

    runtime._update_waits()
    assert runtime._heads["PR1"].waiting_on == "Kein Auftrag"

    runtime._open = object()
    runtime._update_waits()
    assert runtime._heads["PR1"].waiting_on == "Entscheidung offen"
    assert idle_reason("Entscheidung offen") == STARVED


def test_slow_pipeline_cannot_stick_and_an_injected_fault_is_never_skipped(tmp_path):
    runtime = _bootstrapped_runtime(tmp_path)
    runtime._last_pipeline_s = 500.0
    assert runtime._pipeline_estimate_s() <= runtime.solver_limit_s + 4.0
    runtime.shadow._plant["TimeFactor"] = 480.0
    _set_status(runtime, "PR2", "FAULT")
    runtime._injected["PR2"] = time.time()
    runtime._on_fault(FaultEvent(machine="PR2", source_ts=time.time(), received_ts=time.time(), latency_ms=5.0, t=100.0, status="FAULT",
                                 alarm_id=1, code="P-204", text="x", severity=900, est_repair_min=1.0, fault_count=1, baseline=False))
    assert runtime._open is not None and runtime._open.trigger["machine"] == "PR2"


async def test_refused_repeat_of_a_fault_post_keeps_the_first_timestamp(tmp_path):
    client = _fake_client()
    client.inject_fault = AsyncMock(side_effect=[{"ok": True}, {"ok": False, "reason": "ALREADY_FAULTED"}])
    runtime = _bootstrapped_runtime(tmp_path, client)
    first = await runtime.inject_fault("PR2", {"code": "P-204", "repair_min": 240})
    with pytest.raises(ApiError) as caught:
        await runtime.inject_fault("PR2", {"code": "P-204", "repair_min": 240})
    assert caught.value.status == 409
    assert runtime._injected["PR2"] == first["sent_wall_ts"]


@pytest.mark.parametrize("repair", [float("inf"), float("nan"), 1e12, -1, "5", True, 10**400])
async def test_bad_repair_times_never_reach_the_plant(tmp_path, repair):
    client = _fake_client()
    client.inject_fault = AsyncMock(return_value={"ok": True})
    runtime = _bootstrapped_runtime(tmp_path, client)
    with pytest.raises(ApiError) as caught:
        await runtime.inject_fault("PR2", {"code": "P-204", "repair_min": repair})
    assert caught.value.status == 400
    client.inject_fault.assert_not_called()


@pytest.mark.parametrize("body", [
    {"product_id": ["x"], "qty_bars": 10, "due_in_min": 4000},
    {"product_id": "KK-80", "qty_bars": 20.5, "due_in_min": 4000},
    {"product_id": "KK-80", "qty_bars": 0.5, "due_in_min": 4000},
    {"product_id": "KK-80", "qty_bars": float("nan"), "due_in_min": 4000},
    {"product_id": "KK-80", "qty_bars": 20, "due_in_min": float("inf")},
    {"product_id": "KK-80", "qty_bars": 20, "due_in_min": 1e308},
    {"product_id": "KK-80", "qty_bars": 10**400, "due_in_min": 4000},
    {"product_id": "KK-80", "qty_bars": 20, "due_in_min": 4000, "rush": "ja"},
])
def test_bad_orders_are_refused_without_booking(tmp_path, body):
    runtime = _bootstrapped_runtime(tmp_path)
    with pytest.raises(ApiError) as caught:
        runtime.create_order(body)
    assert caught.value.status == 400 and not runtime.book.orders


async def test_settings_take_real_booleans_only(tmp_path):
    client = _fake_client()
    client.set_random_failures = AsyncMock()
    runtime = _bootstrapped_runtime(tmp_path, client)
    for body in ({"random_failures": "false"}, {"auto_supply": 0}, {"time_factor": False}, {"time_factor": [60]}):
        with pytest.raises(ApiError) as caught:
            await runtime.apply_settings(body)
        assert caught.value.status == 400
    client.set_random_failures.assert_not_called()


def test_a_bad_float_cannot_poison_the_json(tmp_path):
    from werkhalle.mes.runtime import to_json

    assert json.loads(to_json({"a": float("inf"), "b": [float("nan"), 1.5]})) == {"a": None, "b": [None, 1.5]}


def test_solve_and_twin_answer_503_with_the_line_down(tmp_path):
    client = _fake_client()
    client.connected = False
    runtime = _bootstrapped_runtime(tmp_path, client)
    with pytest.raises(PlantNotConnected):
        runtime.start_solve({})
    with pytest.raises(PlantNotConnected):
        runtime.start_twin({"scenario": []})


def test_computing_candidate_sets_are_not_evicted(tmp_path):
    from werkhalle.mes.runtime import _evict

    entries = {f"cs-{i}": {"status": "computing"} for i in range(12)}
    _evict(entries, 8)
    assert len(entries) == 12
    entries["cs-0"]["status"] = "ready"
    entries["cs-1"]["status"] = "failed"
    _evict(entries, 8)
    assert "cs-0" not in entries and "cs-1" not in entries and len(entries) == 10


def test_pruning_frames_survives_a_run_without_variant_c(tmp_path):
    runtime = _bootstrapped_runtime(tmp_path)
    for number in range(7):
        runtime._twin_runs[f"tw-{number}"] = {"status": "ready", "variants": {"A": {"frames": [1]}, "C": None}}
    runtime._prune_twin_frames()
    assert runtime._twin_runs["tw-0"]["variants"]["A"]["frames"] == []
    assert runtime._twin_runs["tw-6"]["variants"]["A"]["frames"] == [1]


def test_german_status_words_in_the_log(tmp_path):
    runtime = _bootstrapped_runtime(tmp_path)
    proposal = _open_proposal(runtime)
    runtime.reject_proposal(proposal.id)
    assert runtime.log_json(1)["entries"][0]["text"].startswith(f"Vorschlag {proposal.id}: verworfen.")


def test_kpi_counts_an_overdue_order_as_late_whatever_the_plan_says(tmp_path):
    runtime = _bootstrapped_runtime(tmp_path)
    seeded = seed_orders(3)[0]
    record = runtime.book.add(seeded.order, seeded.lots)
    runtime.shadow._plant["SimTimeMin"] = record.order.due_min + 50.0
    runtime.shadow._sim_t = record.order.due_min + 50.0
    runtime._metrics = SimpleNamespace(order_completion={record.order.id: int(record.order.due_min) - 10})
    kpi = runtime._kpi({})
    assert kpi["orders_late"] == 1 and kpi["orders_late_total"] == 1
    row = runtime._order_row(record)
    assert row["late"] is True and row["lateness_min"] >= 49


def test_closed_proposal_keeps_no_plans_or_rows(tmp_path):
    runtime = _bootstrapped_runtime(tmp_path)
    proposal = _open_proposal(runtime)
    runtime.reject_proposal(proposal.id)
    assert proposal.status == "rejected" and proposal.result is None
    assert "assignments" not in proposal.keep and "assignments" not in proposal.candidates["cpsat"]
    history = runtime.proposals_json()["history"]
    assert [p["id"] for p in history] == [proposal.id] and runtime.proposals_json()["open"] is None


def test_history_is_bounded(tmp_path):
    from werkhalle.mes.runtime import PROPOSAL_HISTORY

    runtime = _bootstrapped_runtime(tmp_path)
    for _ in range(PROPOSAL_HISTORY + 5):
        runtime.reject_proposal(_open_proposal(runtime).id)
    assert len(runtime.proposals_json()["history"]) == PROPOSAL_HISTORY
    assert len(runtime._proposals) == PROPOSAL_HISTORY


def test_open_proposal_expires_after_its_time(tmp_path):
    from werkhalle.mes.runtime import PROPOSAL_TTL_MIN

    runtime = _bootstrapped_runtime(tmp_path)
    proposal = _open_proposal(runtime)
    runtime._expire_proposal()
    assert proposal.status == "open"
    runtime.shadow._plant["TimeFactor"] = 0.0
    runtime.shadow._sim_t = 100.0 + PROPOSAL_TTL_MIN + 1
    runtime._expire_proposal()
    assert proposal.status == "expired" and runtime._open is None


def test_proposal_for_a_fault_still_running_is_refreshed_instead_of_expiring(tmp_path):
    from werkhalle.mes.runtime import PROPOSAL_TTL_MIN

    runtime = _bootstrapped_runtime(tmp_path)
    proposal = _open_proposal(runtime)
    _set_status(runtime, "PR2", "FAULT")
    runtime.shadow._plant["TimeFactor"] = 0.0
    late = 100.0 + PROPOSAL_TTL_MIN + 1
    runtime.shadow._sim_t = late
    runtime._pipeline_wake.clear()
    runtime._expire_proposal()
    assert runtime._open is proposal and proposal.status == "open", "the button stays, the machine is still down"
    assert proposal.pending_update["machine"] is None, "no second proposal for this fault after an accept"
    assert runtime._pipeline_wake.is_set() and proposal.trigger["t"] == pytest.approx(late), "one refresh per period, no spinning"
    generation = proposal.refresh_gen
    runtime._expire_proposal()
    assert proposal.refresh_gen == generation

    _set_status(runtime, "PR2", "RUN")
    runtime.shadow._sim_t = late + PROPOSAL_TTL_MIN + 1
    runtime._expire_proposal()
    assert proposal.status == "expired" and runtime._open is None


def _command(machine: str = "PR1", op_id: str = "L-00001:press") -> dict:
    return {"run_id": "r-fake", "machine": machine, "op_ids": [op_id], "kind": "press", "lot_id": "L-00001", "order_id": "A-1", "product_id": "FR-60", "n_billets": 8}


async def _send(runtime: Runtime, command: dict) -> None:
    runtime.dispatcher.commanded.update(command["op_ids"])
    runtime._in_flight[command["machine"]] = asyncio.current_task()
    await runtime._send_command(command["machine"], command, runtime._epoch)


async def test_a_definite_refusal_clears_the_command_and_backs_off(tmp_path):
    client = _fake_client()
    client.start_operation = AsyncMock(return_value={"ok": False, "reason": "MACHINE_BUSY"})
    runtime = _bootstrapped_runtime(tmp_path, client)
    await _send(runtime, _command())
    assert runtime.dispatcher.commanded == set()
    assert runtime._in_flight == {}
    assert runtime.dispatcher.last_refusal["reason"] == "MACHINE_BUSY"
    assert runtime.dispatcher._retry_at["PR1"] > time.monotonic()


async def test_already_started_and_ok_keep_the_op_commanded(tmp_path):
    for answer in ({"ok": True, "reason": ""}, {"ok": False, "reason": "ALREADY_STARTED"}):
        client = _fake_client()
        client.start_operation = AsyncMock(return_value=answer)
        runtime = _bootstrapped_runtime(tmp_path, client)
        await _send(runtime, _command())
        assert runtime.dispatcher.commanded == {"L-00001:press"}, answer
        assert "PR1" not in runtime.dispatcher._retry_at, answer


async def test_a_lost_answer_keeps_the_op_commanded_until_the_resend(tmp_path):
    client = _fake_client()
    client.start_operation = AsyncMock(side_effect=asyncio.TimeoutError())
    runtime = _bootstrapped_runtime(tmp_path, client)
    await _send(runtime, _command())
    assert runtime.dispatcher.commanded == {"L-00001:press"}, "the plant may have started it"
    machine_ops, retry_at = runtime._unknown["PR1"]
    assert machine_ops == ("L-00001:press",) and retry_at > time.monotonic()
    # When the retry is due, next_commands may send it again, and the plant's duplicate protection decides.
    runtime._unknown["PR1"] = (machine_ops, time.monotonic() - 1)
    runtime.plan = MagicMock(sequences={})
    runtime.plan.problem.ops = ()
    runtime.client.machine_ids.return_value = []
    runtime._dispatch_once()
    assert runtime._unknown == {} and "L-00001:press" not in runtime.dispatcher.commanded


async def test_a_call_that_never_left_clears_at_once(tmp_path):
    client = _fake_client()
    client.start_operation = AsyncMock(side_effect=PlantNotConnected("getrennt"))
    runtime = _bootstrapped_runtime(tmp_path, client)
    await _send(runtime, _command())
    assert runtime.dispatcher.commanded == set() and runtime._unknown == {}


def test_dispatch_pauses_without_a_connection_or_a_heartbeat(tmp_path):
    client = _fake_client()
    runtime = _bootstrapped_runtime(tmp_path, client)
    assert runtime._dispatch_paused() is False
    client.connected = False
    assert runtime._dispatch_paused() is True
    client.connected = True
    client.stale = True
    assert runtime._dispatch_paused() is True


def test_at_most_one_command_per_machine_is_in_flight(tmp_path, monkeypatch):
    runtime = _bootstrapped_runtime(tmp_path)
    runtime.shadow._machine("PR1").values["Status"] = "IDLE"
    runtime.shadow._machine("PR2").values["Status"] = "IDLE"
    runtime._in_flight["PR1"] = MagicMock()
    seen = {}

    def fake_poll(_plan, _tracker, views, _now):
        seen.update(views)
        return []

    runtime.dispatcher.poll = fake_poll
    runtime._dispatch_once()
    assert set(seen) == {"PR2"}, "a machine with a call under way is not even offered to the dispatcher"


def test_stall_raises_an_alarm_and_a_proposal_once_and_clears(tmp_path, monkeypatch):
    runtime = _bootstrapped_runtime(tmp_path)
    runtime.shadow._material = {"transports": []}
    monkeypatch.setattr("werkhalle.mes.runtime.STALL_CONFIRM_S", 0.0)
    monkeypatch.setattr("werkhalle.mes.runtime.stalled", lambda *a: True)
    runtime._check_stall()
    assert runtime.stalled_now is False, "one look is not enough: the state lags the events"
    runtime._check_stall()
    assert runtime.stalled_now is True
    active = runtime._active_alarms()
    assert len(active) == 1 and active[0]["machine"] == "MES" and active[0]["code"] == "MES-STALL" and active[0]["active"] is True
    assert runtime._open is not None and runtime._open.trigger["kind"] == "stall"
    first = runtime._open
    runtime._check_stall()
    assert runtime._open is first and len(runtime._mes_alarms) == 1

    monkeypatch.setattr("werkhalle.mes.runtime.stalled", lambda *a: False)
    runtime._check_stall()
    assert runtime.stalled_now is False and runtime._active_alarms() == []
    row = runtime.alarms_json(10)["alarms"][0]
    assert row["cleared_t"] is not None and row["active"] is False


async def test_mes_alarm_is_acked_locally(tmp_path):
    client = _fake_client()
    client.ack_alarm = AsyncMock()
    runtime = _bootstrapped_runtime(tmp_path, client)
    runtime._raise_mes_alarm("MES-STALL", "x")
    row_id = next(iter(runtime._mes_alarms))
    assert await runtime.ack_alarm(row_id) == {"ok": True}
    assert runtime._mes_alarms[row_id]["acked"] is True
    client.ack_alarm.assert_not_awaited()


def test_no_stall_check_while_a_command_is_out_or_the_plant_is_paused(tmp_path, monkeypatch):
    monkeypatch.setattr("werkhalle.mes.runtime.STALL_CONFIRM_S", 0.0)
    monkeypatch.setattr("werkhalle.mes.runtime.stalled", lambda *a: True)
    runtime = _bootstrapped_runtime(tmp_path)
    runtime.shadow._material = {"transports": []}
    runtime.dispatcher.commanded.add("L-1:press")
    runtime._check_stall()
    runtime._check_stall()
    assert runtime.stalled_now is False
    runtime.dispatcher.commanded.clear()
    runtime.shadow._plant["TimeFactor"] = 0.0
    runtime._check_stall()
    runtime._check_stall()
    assert runtime.stalled_now is False


def test_no_material_with_no_planned_supply_proposes_a_replan(tmp_path, monkeypatch):
    runtime = _bootstrapped_runtime(tmp_path)
    monkeypatch.setattr("werkhalle.mes.runtime.billet_shortfall", lambda plan, tracker: {"6060": 8})
    runtime.dispatcher.replan_needed = True
    runtime._on_refusal("PR1", _command(), ("L-00001:press",), "NO_MATERIAL")
    assert runtime._open is not None and runtime._open.trigger["kind"] == "no_material"
    assert runtime._open.trigger["machine"] == "PR1" and "6060" in runtime._open.trigger["text"]
    assert runtime.dispatcher.replan_needed is False


def test_short_fault_is_skipped_and_a_long_one_is_not(tmp_path):
    runtime = _bootstrapped_runtime(tmp_path)
    runtime._last_pipeline_s = 10.0  # at factor 120 the plant moves 20 minutes while this runs
    _set_status(runtime, "VP", "FAULT")

    def event(minutes: float) -> FaultEvent:
        return FaultEvent(machine="VP", source_ts=time.time(), received_ts=time.time(), latency_ms=5.0, t=100.0, status="FAULT",
                          alarm_id=1, code="V-502", text="x", severity=900, est_repair_min=minutes, fault_count=1, baseline=False)

    runtime._on_fault(event(19.0))
    assert runtime._open is None
    assert "zu kurz für Neuplanung" in runtime.log_json(1)["entries"][0]["text"]
    runtime._on_fault(event(21.0))
    assert runtime._open is not None and runtime._open.trigger["machine"] == "VP"


def test_fault_already_over_gets_no_proposal(tmp_path):
    runtime = _bootstrapped_runtime(tmp_path)
    _set_status(runtime, "VP", "RUN")
    runtime._on_fault(FaultEvent(machine="VP", source_ts=1.0, received_ts=1.1, latency_ms=100.0, t=100.0, status="RUN", alarm_id=1, code="V-502",
                                 text="x", severity=900, est_repair_min=500.0, fault_count=1, baseline=False))
    assert runtime._open is None


def test_baseline_fault_is_a_real_fault_without_a_latency(tmp_path):
    runtime = _bootstrapped_runtime(tmp_path)
    _set_status(runtime, "VP", "FAULT")
    runtime._on_fault(FaultEvent(machine="VP", source_ts=1.0, received_ts=50.0, latency_ms=49000.0, t=100.0, status="FAULT", alarm_id=1, code="V-502",
                                 text="x", severity=900, est_repair_min=500.0, fault_count=1, baseline=True))
    assert runtime._open is not None and runtime._open.chain["latency_ms"] is None


def test_auto_supply_tops_up_only_when_low_and_only_when_on(tmp_path):
    runtime = _bootstrapped_runtime(tmp_path)
    runtime.auto_supply = True
    for seeded in seed_orders(3):
        runtime.book.add(seeded.order, seeded.lots)
    before = len(runtime.book.orders)
    runtime._auto_supply()
    assert len(runtime.book.orders) == before, "22 open lots are plenty"

    for lot_id in list(runtime.book.lots)[:14]:
        runtime.book.note_lot_finished(lot_id, 10.0)
    runtime.auto_supply = False
    runtime._auto_supply()
    assert len(runtime.book.orders) == before, "switched off"

    runtime.auto_supply = True
    runtime._auto_supply()
    assert len(runtime.book.orders) > before
    assert 18 <= runtime.book.lots_open <= 28
    assert runtime.trace.register_order.called and runtime.trace.register_lot.called and runtime.db.save_order.called
    orders = runtime.orders_json()["orders"]
    assert orders[0]["created_min"] >= orders[-1]["created_min"], "newest open first"
    runtime._auto_supply()
    assert 18 <= runtime.book.lots_open <= 28, "no second top-up while there is enough"


def test_rolling_starts_for_missing_lots_only_without_an_open_proposal(tmp_path):
    runtime = _bootstrapped_runtime(tmp_path)
    for seeded in seed_orders(3)[:1]:
        runtime.book.add(seeded.order, seeded.lots)
    runtime.plan.problem.has_op.return_value = True
    spawned = []
    runtime._spawn = lambda coro, name="": (spawned.append(name), coro.close())[0] or MagicMock()
    runtime._maybe_roll()
    assert spawned == [], "every lot is in the plan"
    runtime.plan.problem.has_op.return_value = False
    proposal = _open_proposal(runtime)
    runtime._maybe_roll()
    assert spawned == ["mes-append"], "a proposal is open: the lots go onto the active plan, without a solver"
    spawned.clear()
    runtime._rolling = None
    runtime.reject_proposal(proposal.id)
    runtime._roll_not_before = time.monotonic() + 100
    runtime._maybe_roll()
    assert spawned == [], "still inside the retry delay"
    runtime._roll_not_before = 0.0
    runtime._maybe_roll()
    assert spawned == ["mes-roll"]
    runtime._maybe_roll()
    assert spawned == ["mes-roll"], "only one at a time"
