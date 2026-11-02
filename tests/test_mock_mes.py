"""Holds tools/mock_mes.py to docs/API.md, because the UI agents build against it until the real backend is up."""
from __future__ import annotations

import importlib.util
import json
import sys
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
MOCK_PATH = ROOT / "tools" / "mock_mes.py"


def load_mock():
    if "mock_mes_under_test" in sys.modules:
        return sys.modules["mock_mes_under_test"]
    spec = importlib.util.spec_from_file_location("mock_mes_under_test", MOCK_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules["mock_mes_under_test"] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def mock_module():
    return load_mock()


@pytest.fixture()
def harness(mock_module):
    mock = mock_module.Mock(factor=240, seed=3, random_failures=False, auto_supply=True, warm_start=500)
    with TestClient(mock_module.create_app(mock)) as client:
        yield mock, client


def has_keys(obj: dict, keys: str, where: str = ""):
    missing = [k for k in keys.split() if k not in obj]
    assert not missing, f"{where or 'object'} is missing {missing}: {sorted(obj)}"


def wait_for(predicate, timeout=15.0, step=0.05):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(step)
    raise AssertionError("condition not reached in time")


ASSIGNMENT = "op_id kind machine start end setup_before batch_id lot_id order_id product_id state frozen"
METRICS = "makespan total_tardiness weighted_tardiness late_orders max_tardiness setups setup_minutes objective order_completion"
CANDIDATE = "solver label status valid violations solve_seconds best_bound iterations note metrics assignments downtimes twin"
TWIN = "makespan total_tardiness late_orders setups energy_kwh unfinished_orders stalled speedup"
ORDER = "id product_id product_name qty_bars due_min weight created_min status delivered_pieces lots_total lots_done planned_completion lateness_min late completed_min lots"


def check_material(material: dict):
    has_keys(material, "t transports buffers in_machine", "material")
    has_keys(material["buffers"], "BILLET_STORE AGE_IN SAW_IN PACK_IN SHIP", "buffers")
    has_keys(material["buffers"]["SHIP"], "packages pieces kg lots_total last_lots", "SHIP")
    assert set(material["buffers"]["BILLET_STORE"]) == {"6060", "6063", "6082"}
    for transport in material["transports"]:
        assert set(transport) == {"id", "kind", "lot_id", "count", "from", "to", "t0", "t1"}
        assert transport["kind"] in ("billets", "baskets", "pieces", "packages")
    for entry in material["buffers"]["AGE_IN"] + material["buffers"]["SAW_IN"]:
        has_keys(entry, "lot_id baskets")
    for entry in material["buffers"]["PACK_IN"]:
        has_keys(entry, "lot_id pieces")
    for row in material["in_machine"].values():
        has_keys(row, "lot_ids kind billets_left baskets")


def check_state(state: dict):
    has_keys(state, "run_id connected stale sim machines material kpi plan proposal alarms_active stalled counters", "state")
    has_keys(state["sim"], "t factor wall_ts random_failures auto_supply", "sim")
    assert [m["id"] for m in state["machines"]] == ["GUSS", "PR1", "PR2", "OF1", "OF2", "SAE", "VP"]
    for machine in state["machines"]:
        has_keys(machine, "id name kind unit capacity status status_since temperature setpoint power_kw energy_kwh total good scrap "
                          "setup_class op progress alarm est_repair_min fault_count oee next_op waiting_on trend", machine["id"])
        assert machine["status"] in ("IDLE", "RUN", "SETUP", "FAULT")
        assert machine["waiting_on"] in (None, "Material", "Vorgänger", "Störung", "Kein Auftrag")
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


def test_state_shape_and_size(mock_module, harness):
    mock, client = harness
    state = client.get("/api/state").json()
    check_state(state)
    for machine in mock.sim.machines.values():
        machine.trend = {"temperature": [round(machine.temperature)] * 120, "power_kw": [round(machine.power)] * 120}
    packed = json.dumps(mock.state_json(), ensure_ascii=False, separators=(",", ":")).encode()
    assert len(packed) < 12 * 1024, f"state with full trends is {len(packed)} bytes"


def test_static_files_are_served_with_javascript_type(harness):
    _, client = harness
    index = client.get("/")
    assert index.status_code == 200 and "Werkhalle" in index.text
    script = client.get("/app.js")
    assert script.status_code == 200
    assert "javascript" in script.headers["content-type"]


def test_catalog_orders_and_new_order(harness):
    _, client = harness
    catalog = client.get("/api/catalog").json()
    has_keys(catalog, "machines products recipes time_factors", "catalog")
    has_keys(catalog["machines"][0], "id name kind unit capacity faults")
    has_keys(catalog["machines"][0]["faults"][0], "code text repair_mean_min")
    has_keys(catalog["products"][0], "id name alloy recipe die presses kg_per_bar bars_per_billet min_lead_min")
    has_keys(catalog["recipes"][0], "id temp_c soak_min")
    assert catalog["time_factors"] == [0, 60, 120, 240, 480]

    orders = client.get("/api/orders").json()["orders"]
    has_keys(orders[0], ORDER, "order")
    has_keys(orders[0]["lots"][0], "id n_billets stage state machine", "order lot")

    too_soon = client.post("/api/orders", json={"product_id": "KK-80", "qty_bars": 200, "due_in_min": 5})
    assert too_soon.status_code == 400
    body = too_soon.json()
    assert body["error"] == "due_too_soon" and body["min_lead_min"] > 5 and body["message"]

    created = client.post("/api/orders", json={"product_id": "KK-80", "qty_bars": 200, "due_in_min": 2000, "rush": True})
    assert created.status_code == 200
    has_keys(created.json(), "order replan")
    assert created.json()["order"]["weight"] == 3


def test_plan_solve_candidates_and_apply(harness):
    mock, client = harness
    plan = client.get("/api/plan").json()
    has_keys(plan, "revision solver source source_proposal_id created_min now metrics assignments downtimes orders", "plan")
    has_keys(plan["metrics"], METRICS, "metrics")
    has_keys(plan["assignments"][0], ASSIGNMENT, "assignment")
    has_keys(plan["orders"][0], "id due weight")

    started = client.post("/api/plan/solve", json={"time_limit_s": 1, "seed": 1}).json()
    assert started["status"] == "computing"
    ready = wait_for(lambda: (lambda r: r if r["status"] == "ready" else None)(client.get(f"/api/plan/candidates/{started['id']}").json()))
    has_keys(ready, "id status created_min time_limit_s lots ops candidates recommended stale", "candidate set")
    assert set(ready["candidates"]) == {"cpsat", "anneal", "keep"}
    for candidate in ready["candidates"].values():
        has_keys(candidate, CANDIDATE, "candidate")
        has_keys(candidate["twin"], TWIN, "candidate twin")

    before = mock.revision
    applied = client.post("/api/plan/apply", json={"candidates_id": started["id"], "solver": "anneal"})
    if ready["stale"]:
        assert applied.status_code == 409 and applied.json()["error"] == "veraltet"
    else:
        assert applied.status_code == 200 and applied.json()["revision"] == before + 1


def test_fault_goes_through_datachange_then_proposal_and_accept(harness):
    mock, client = harness
    sent = client.post("/api/machines/PR1/fault", json={"code": "P-204", "repair_min": 240})
    assert sent.status_code == 200
    has_keys(sent.json(), "ok sent_wall_ts")
    # The answer must not have changed the machine: only the later status change does.
    assert mock.sim.machines["PR1"].status != "FAULT"
    wait_for(lambda: mock.sim.machines["PR1"].status == "FAULT")

    assert client.post("/api/machines/PR1/fault", json={"code": "P-204"}).status_code == 409
    assert client.post("/api/machines/OF1/repair").status_code == 409

    active = client.get("/api/state").json()["alarms_active"]
    alarm = next(a for a in active if a["machine"] == "PR1")
    has_keys(alarm, "id machine alarm_id code text severity t detected_wall_ts latency_ms acked", "alarm")
    assert client.post(f"/api/alarms/{alarm['id']}/ack").status_code == 200
    rows = client.get("/api/alarms?limit=10").json()["alarms"]
    assert rows[0]["acked"] is True and "cleared_t" in rows[0]

    proposal = wait_for(lambda: (lambda o: o if o and o["status"] == "open" else None)(client.get("/api/proposals").json()["open"]), timeout=20)
    has_keys(proposal, "id status status_reason trigger chain based_on_revision age_min keep candidates recommended beats_keep summary delta", "proposal")
    has_keys(proposal["trigger"], "kind machine code text est_repair_min t", "trigger")
    has_keys(proposal["chain"], "injected_wall_ts source_ts detected_wall_ts latency_ms forked_wall_ts solved_wall_ts twin_wall_ts ready_wall_ts replan_seconds snapshot_seq", "chain")
    has_keys(proposal["delta"], "makespan total_tardiness late_orders setups", "delta")
    has_keys(proposal["keep"], CANDIDATE, "keep")
    chain = proposal["chain"]
    assert chain["injected_wall_ts"] < chain["source_ts"] < chain["detected_wall_ts"] < chain["forked_wall_ts"] <= chain["ready_wall_ts"]

    before = mock.revision
    assert client.post(f"/api/proposals/{proposal['id']}/accept", json={"solver": "keep"}).status_code == 422
    accepted = client.post(f"/api/proposals/{proposal['id']}/accept", json={})
    assert accepted.status_code == 200 and accepted.json()["revision"] == before + 1
    plan = client.get("/api/plan").json()
    assert plan["source"] == "proposal" and plan["source_proposal_id"] == proposal["id"]
    assert any(d["machine"] == "PR1" and d["reason"] == "P-204" for d in plan["downtimes"])

    closed = client.get("/api/proposals").json()
    assert closed["open"] is None
    assert closed["history"][0]["status"] == "accepted" and "assignments" not in closed["history"][0]["keep"]


def test_stale_accept_answers_409_and_starts_a_new_proposal(harness):
    mock, client = harness
    client.post("/api/machines/PR2/fault", json={"code": "P-201", "repair_min": 240})
    proposal = wait_for(lambda: (lambda o: o if o and o["status"] == "open" else None)(client.get("/api/proposals").json()["open"]), timeout=20)
    client.post("/api/_mock/force_stale")
    answer = client.post(f"/api/proposals/{proposal['id']}/accept", json={})
    assert answer.status_code == 409
    body = answer.json()
    assert body["error"] == "veraltet" and body["message"] and body["new_proposal_id"]
    assert mock.proposals[proposal["id"]]["status"] == "superseded"


def test_repair_supersedes_the_open_proposal(harness):
    mock, client = harness
    client.post("/api/machines/SAE/fault", json={"code": "S-401", "repair_min": 240})
    wait_for(lambda: client.get("/api/proposals").json()["open"], timeout=20)
    assert client.post("/api/machines/SAE/repair").status_code == 200
    assert client.get("/api/proposals").json()["open"] is None
    assert mock.sim.machines["SAE"].status != "FAULT"
    assert client.get("/api/proposals").json()["history"][0]["status"] == "superseded"


def test_reject_closes_the_proposal(harness):
    _, client = harness
    client.post("/api/machines/VP/fault", json={"code": "V-501", "repair_min": 120})
    proposal = wait_for(lambda: client.get("/api/proposals").json()["open"], timeout=20)
    assert client.post(f"/api/proposals/{proposal['id']}/reject").json() == {"ok": True}
    assert client.get("/api/proposals").json()["open"] is None


def test_settings_validate_time_factor(harness):
    mock, client = harness
    assert client.post("/api/plant/settings", json={"time_factor": 60, "random_failures": True, "auto_supply": False}).status_code == 200
    assert mock.factor == 60 and mock.sim.random_failures is True and mock.sim.auto_supply is False
    assert client.post("/api/plant/settings", json={"time_factor": 77}).status_code == 400
    assert mock.factor == 60


def test_oee_energy_and_log(harness):
    _, client = harness
    for window in ("start", "8h"):
        oee = client.get(f"/api/oee?window={window}").json()
        has_keys(oee, "window t plant machines trend", "oee")
        has_keys(oee["plant"], "oee oee_iso availability performance quality")
        has_keys(oee["machines"][0], "id name oee oee_iso availability availability_iso performance quality minutes counts fault_count mtbf_min mttr_min")
        has_keys(oee["machines"][0]["minutes"], "run setup fault idle no_demand starved planned planned_net ideal")
        has_keys(oee["machines"][0]["counts"], "total good scrap unit")
        has_keys(oee["trend"], "t oee")
    energy = client.get("/api/energy").json()
    has_keys(energy, "t total_kwh power_kw kwh_per_kg machines trend by_order", "energy")
    has_keys(energy["machines"][0], "id name energy_kwh power_kw share kwh_per_unit unit")
    has_keys(energy["trend"], "t kw")
    assert "total" in energy["trend"]["kw"]
    has_keys(energy["by_order"][0], "order_id energy_kwh kg kwh_per_kg")
    entry = client.get("/api/log?limit=5").json()["entries"][0]
    has_keys(entry, "t wall_ts level source text")


def test_trace_graph_and_search(harness):
    _, client = harness
    recent = client.get("/api/trace/recent?limit=20").json()["results"]
    assert recent
    has_keys(recent[0], "id type label t")
    results = client.get("/api/trace/search?q=S-000").json()["results"]
    assert results and all("S-000" in r["id"] for r in results[:5])
    assert len(client.get("/api/trace/search?q=-").json()["results"]) <= 30

    melt = next(r for r in results if r["type"] == "melt")
    graph = client.get(f"/api/trace/graph/{melt['id']}").json()
    has_keys(graph, "root root_type truncated direction_note nodes edges summary recall_label", "graph")
    assert graph["root"] == melt["id"] and graph["root_type"] == "melt"
    has_keys(graph["nodes"][0], "id type label t qty unit attrs relation", "node")
    has_keys(graph["edges"][0], "from to qty unit", "edge")
    has_keys(graph["summary"], "packages orders pieces melts loads kg", "summary")
    ids = {n["id"] for n in graph["nodes"]}
    assert all(e["from"] in ids and e["to"] in ids for e in graph["edges"])
    assert client.get("/api/trace/graph/GIBTS-NICHT").status_code == 404


def test_shared_load_does_not_link_lots_unless_it_is_the_root(mock_module):
    trace = mock_module.Trace()
    trace.node("S-1", "melt", "m1", 0, 2, "Bolzen", {})
    trace.node("S-2", "melt", "m2", 0, 2, "Bolzen", {})
    for basket, melt, package in (("K-1", "S-1", "P-1"), ("K-2", "S-2", "P-2")):
        trace.node(basket, "basket", basket, 1, 1, "Stangen", {})
        trace.node(package, "package", package, 2, 1, "Stück", {})
        trace.edge(melt, basket, 1, "Stangen")
        trace.edge(basket, package, 1, "Stück")
    trace.node("O-1", "load", "load", 1, 2, "Körbe", {})
    trace.load_baskets["O-1"] = ["K-1", "K-2"]
    trace.load_of.update({"K-1": "O-1", "K-2": "O-1"})

    forward = {n["id"]: n for n in trace.graph("S-1")["nodes"]}
    assert "P-1" in forward and "P-2" not in forward
    assert forward["O-1"]["relation"] == "side"

    recall = {n["id"] for n in trace.graph("O-1")["nodes"]}
    assert {"P-1", "P-2", "S-1", "S-2"} <= recall


def test_twin_run_has_frames_and_apply_makes_a_proposal(harness):
    _, client = harness
    started = client.post("/api/twin/run", json={
        "scenario": [{"type": "machine_down", "machine": "PR2", "at_offset_min": 0, "duration_min": 240},
                     {"type": "rush_order", "product_id": "KK-80", "qty_bars": 200, "due_in_min": 600}],
        "monte_carlo": 2, "seed": 1, "time_limit_s": 1}).json()
    assert started["status"] == "computing"
    run = wait_for(lambda: (lambda r: r if r["status"] != "computing" else None)(client.get(f"/api/twin/runs/{started['id']}").json()), timeout=40)
    assert run["status"] == "ready", run.get("note")
    has_keys(run, "id status created_min forked_seq scenario note variants candidates recommended delta_b_vs_a delta_c_vs_b monte_carlo applicable", "run")
    assert set(run["variants"]) == {"A", "B", "C"} and run["variants"]["C"]["solver"] in ("cpsat", "anneal")
    for variant in run["variants"].values():
        has_keys(variant, "label kpis order_completion speedup wall_seconds sim_minutes assignments frames", "variant")
        has_keys(variant["kpis"], "makespan total_tardiness late_orders setups energy_kwh unfinished_orders stalled", "kpis")
        frames = variant["frames"]
        assert 0 < len(frames) <= 600
        has_keys(frames[0], "t machines material", "frame")
        assert set(frames[0]["machines"]) == {"GUSS", "PR1", "PR2", "OF1", "OF2", "SAE", "VP"}
        has_keys(frames[0]["machines"]["PR2"], "status progress lot_ids", "frame machine")
        check_material(frames[0]["material"])
        assert frames[1]["t"] - frames[0]["t"] == pytest.approx(5, abs=1)
    has_keys(run["delta_b_vs_a"], "makespan total_tardiness")
    has_keys(run["monte_carlo"], "n B C")
    has_keys(run["monte_carlo"]["B"]["makespan"], "p10 p50 p90")
    has_keys(run["candidates"]["cpsat"], CANDIDATE, "twin candidate")

    answer = client.post(f"/api/twin/runs/{run['id']}/apply", json={"solver": "cpsat"}).json()
    proposal = wait_for(lambda: (lambda o: o if o and o["id"] == answer["proposal_id"] and o["status"] == "open" else None)(client.get("/api/proposals").json()["open"]), timeout=20)
    assert proposal["trigger"]["kind"] == "scenario"


def test_opcua_monitor(harness):
    _, client = harness
    wait_for(lambda: client.get("/api/opcua/changes").json()["changes"], timeout=5)
    nodes = client.get("/api/opcua/nodes").json()
    has_keys(nodes, "endpoint namespace nodes")
    status = next(n for n in nodes["nodes"] if n["node_id"] == "ns=2;s=Werkhalle.PR2.Status")
    has_keys(status, "node_id path machine name type value source_ts changes")
    assert status["path"] == "Werkhalle/Machines/PR2/Status"
    material = next(n for n in nodes["nodes"] if n["name"] == "Material")
    assert material["truncated"] is True and len(material["value"]) == 120

    changes = client.get("/api/opcua/changes?limit=5").json()
    has_keys(changes, "changes per_second")
    assert len(changes["changes"]) <= 5
    has_keys(changes["changes"][0], "node_id path value source_ts received_wall_ts latency_ms")
    calls = client.get("/api/opcua/methods").json()["calls"]
    assert calls
    has_keys(calls[0], "wall_ts method node args result ms")


def test_health(harness):
    _, client = harness
    health = client.get("/api/health").json()
    has_keys(health, "ok connected run_id plan_revision plant_module_loaded snapshot_calls pool_plant_module_loaded version", "health")
    assert health["ok"] is True and health["plant_module_loaded"] is False


def test_websocket_sends_state_and_discrete_messages(harness):
    _, client = harness
    with client.websocket_connect("/ws") as socket:
        first = json.loads(socket.receive_text())
        assert first["type"] == "state"
        check_state(first["data"])
        client.post("/api/orders", json={"product_id": "FR-60", "qty_bars": 64, "due_in_min": 3000})
        seen = set()
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and not {"order", "plan", "log"} <= seen:
            seen.add(json.loads(socket.receive_text())["type"])
        assert {"order", "plan", "log"} <= seen


def test_errors_use_the_contract_envelope(harness):
    _, client = harness
    for response in (client.get("/api/plan/candidates/nope"), client.get("/api/twin/runs/nope"), client.post("/api/machines/XX/fault", json={})):
        assert response.status_code == 404
        has_keys(response.json(), "error message", "error")


def test_nothing_under_src_imports_the_mock():
    offenders = [p for p in (ROOT / "src").rglob("*.py") if "mock_mes" in p.read_text(encoding="utf-8")]
    assert offenders == []
