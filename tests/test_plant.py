"""The OPC UA server, exercised through a real asyncua client over a real socket.

One server for the whole module (start-up takes a couple of seconds). Each test works on its own machine or restores
what it changed, because the plant keeps running in between.
"""
from __future__ import annotations

import asyncio
import dis
import inspect
import json
import socket
import time
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio
from asyncua import Client, ua
from test_simcore_support import RUN_ID, FifoDriver, age_cmd, assert_conserved, cast_cmd, pack_cmd, press_cmd, saw_cmd

from werkhalle.domain import catalog as cat
from werkhalle.domain.orders import Lot
from werkhalle.domain.state import PlanningState
from werkhalle.plant import ALLOWED_FACTORS, NAMESPACE, PlantServer, SimClock
from werkhalle.plant.server import FAST_FIELDS, SLOW_FIELDS
from werkhalle.simcore import SimEngine, planning_state_from_snapshot

pytestmark = pytest.mark.asyncio(loop_scope="module")

MACHINE_VARIABLES = {
    "Name": ua.VariantType.String,
    "Kind": ua.VariantType.String,
    "Unit": ua.VariantType.String,
    "Status": ua.VariantType.String,
    "Temperature": ua.VariantType.Double,
    "TemperatureSetpoint": ua.VariantType.Double,
    "PowerKW": ua.VariantType.Double,
    "EnergyKWh": ua.VariantType.Double,
    "TotalCount": ua.VariantType.UInt64,
    "GoodCount": ua.VariantType.UInt64,
    "ScrapCount": ua.VariantType.UInt64,
    "RunMinutes": ua.VariantType.Double,
    "SetupMinutes": ua.VariantType.Double,
    "FaultMinutes": ua.VariantType.Double,
    "IdleMinutes": ua.VariantType.Double,
    "IdealMinutes": ua.VariantType.Double,
    "SetupClass": ua.VariantType.String,
    "CurrentOperation": ua.VariantType.String,
    "Progress": ua.VariantType.Double,
    "AlarmId": ua.VariantType.UInt64,
    "AlarmActive": ua.VariantType.Boolean,
    "AlarmCode": ua.VariantType.String,
    "AlarmText": ua.VariantType.String,
    "AlarmSeverity": ua.VariantType.UInt16,
    "EstimatedRepairMin": ua.VariantType.Double,
    "FaultCount": ua.VariantType.UInt64,
}
PLANT_VARIABLES = {
    "RunId": ua.VariantType.String,
    "SimTimeMin": ua.VariantType.Double,
    "TimeFactor": ua.VariantType.Double,
    "RandomFailures": ua.VariantType.Boolean,
    "Heartbeat": ua.VariantType.UInt64,
    "EventSeq": ua.VariantType.UInt64,
    "Material": ua.VariantType.String,
    "TotalPowerKW": ua.VariantType.Double,
    "TotalEnergyKWh": ua.VariantType.Double,
    "SnapshotCalls": ua.VariantType.UInt64,
}
MACHINE_METHODS = {"StartOperation", "InjectFault", "Repair", "AckAlarm"}
PLANT_METHODS = {"GetEvents", "GetStateSnapshot"}


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def plant():
    engine = SimEngine(seed=11, stochastic=True, random_failures=False, run_id=RUN_ID)
    async with PlantServer(engine, SimClock(factor=120.0), free_port()) as server:
        yield server


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def client(plant):
    connection = Client(plant.endpoint, timeout=10, watchdog_intervall=5)
    await connection.connect()
    yield connection
    await connection.disconnect()


class Recorder:
    """Keeps every notification in arrival order, as a plain def like the MES's handler.

    `batch` tells publishes apart: asyncua calls a plain handler for all items of one publish without yielding, so a
    call_soon scheduled on the first item runs only after the last one.
    """

    def __init__(self):
        self.items: list[dict] = []
        self.batch = 0
        self._batch_open = False

    def _close_batch(self):
        self._batch_open = False
        self.batch += 1

    def datachange_notification(self, node, val, data):
        if not self._batch_open:
            self._batch_open = True
            asyncio.get_running_loop().call_soon(self._close_batch)
        dv = data.monitored_item.Value
        self.items.append(
            {
                "name": node.nodeid.Identifier.rsplit(".", 1)[-1],
                "value": val,
                "source_ts": dv.SourceTimestamp,
                "server_ts": dv.ServerTimestamp,
                "wall": time.time(),
                "batch": self.batch,
            }
        )

    def values(self, name: str) -> list:
        return [row["value"] for row in self.items if row["name"] == name]

    def index(self, name: str, value) -> int:
        return next(i for i, row in enumerate(self.items) if row["name"] == name and row["value"] == value)


async def wait_for(condition, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while not condition():
        assert time.monotonic() < deadline, "condition not met in time"
        await asyncio.sleep(0.02)


def var(client: Client, ns: int, path: str):
    return client.get_node(ua.NodeId(f"Werkhalle.{path}", ns))


async def call(client: Client, ns: int, machine: str, method: str, *args) -> dict:
    obj = client.get_node(ua.NodeId(f"Werkhalle.{machine}", ns))
    raw = await obj.call_method(ua.NodeId(f"Werkhalle.{machine}.{method}", ns), *args)
    return json.loads(raw)


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def ns(client) -> int:
    return await client.get_namespace_index(NAMESPACE)


async def test_endpoint_is_literal_loopback_and_security_none(plant, client):
    assert plant.endpoint.startswith("opc.tcp://127.0.0.1:")
    assert plant.endpoint.endswith("/werkhalle/")
    endpoints = await client.get_endpoints()
    assert endpoints
    assert {e.SecurityMode for e in endpoints} == {ua.MessageSecurityMode.None_}


async def test_namespace_and_node_ids_match_the_contract(client, ns):
    assert ns == 2
    status = client.get_node("ns=2;s=Werkhalle.PR2.Status")
    assert await status.read_value() in ("IDLE", "SETUP", "RUN", "FAULT")


async def test_browse_finds_the_whole_tree(client, ns):
    objects = client.nodes.objects
    root = await objects.get_child([f"{ns}:Werkhalle"])
    assert {n.Name for n in [await c.read_browse_name() for c in await root.get_children()]} == {"Plant", "Machines"}

    plant_node = await root.get_child([f"{ns}:Plant"])
    names = {}
    for child in await plant_node.get_children():
        names[(await child.read_browse_name()).Name] = child
    assert set(names) == set(PLANT_VARIABLES) | PLANT_METHODS
    for name, vtype in PLANT_VARIABLES.items():
        assert await names[name].read_data_type_as_variant_type() == vtype, name

    machines_node = await root.get_child([f"{ns}:Machines"])
    machine_nodes = {(await m.read_browse_name()).Name: m for m in await machines_node.get_children()}
    assert list(machine_nodes) == list(cat.MACHINE_ORDER)
    for machine_id, machine in machine_nodes.items():
        children = {(await c.read_browse_name()).Name: c for c in await machine.get_children()}
        assert set(children) == set(MACHINE_VARIABLES) | MACHINE_METHODS, machine_id
        for name in ("Status", "TotalCount", "AlarmSeverity", "AlarmActive", "Temperature"):
            assert await children[name].read_data_type_as_variant_type() == MACHINE_VARIABLES[name]
        assert children["Status"].nodeid.Identifier == f"Werkhalle.{machine_id}.Status"
        assert children["Status"].nodeid.NamespaceIndex == ns
        assert await children["Name"].read_value() == cat.MACHINES[machine_id].name
        assert await children["Kind"].read_value() == cat.MACHINES[machine_id].kind
        assert await children["Unit"].read_value() == cat.MACHINES[machine_id].unit
        arguments = await children["InjectFault"].get_children()
        assert arguments, "methods advertise their arguments"


async def test_every_variable_has_the_declared_variant_type(client, ns):
    for name, vtype in MACHINE_VARIABLES.items():
        value = await var(client, ns, f"VP.{name}").read_data_value()
        assert value.Value.VariantType == vtype, name
    for name, vtype in PLANT_VARIABLES.items():
        value = await var(client, ns, f"Plant.{name}").read_data_value()
        assert value.Value.VariantType == vtype, name


async def test_every_handler_is_a_coroutine_function_without_suspension_points(plant):
    handlers = plant.method_handlers
    assert set(handlers) == {f"Plant.{m}" for m in PLANT_METHODS} | {
        f"{machine}.{m}" for machine in cat.MACHINE_ORDER for m in MACHINE_METHODS
    }
    aspace = plant.server.iserver.aspace
    idx = 2
    for key, handler in handlers.items():
        assert inspect.iscoroutinefunction(handler), key
        # asyncua decides between the event loop and a worker thread on the registered callback.
        node = aspace[ua.NodeId(f"Werkhalle.{key}", idx)]
        assert inspect.iscoroutinefunction(node.call), key
        code = handler.__func__.__code__ if inspect.ismethod(handler) else handler.__code__
        ops = {instruction.opname for instruction in dis.get_instructions(code)}
        assert "GET_AWAITABLE" not in ops, f"{key} has a suspending await"
        assert "run_until" in code.co_names or "_advance" in code.co_names, key


class _EngineProbe:
    """Stands in for the engine and notes the order in which a handler touches it."""

    def __init__(self, engine):
        self._engine = engine
        self.accessed: list[str] = []

    def __getattr__(self, name):
        self.accessed.append(name)
        return getattr(self._engine, name)


def _sample_args(key: str) -> list[ua.Variant]:
    method = key.rsplit(".", 1)[-1]
    if method == "StartOperation":
        return [ua.Variant("{}", ua.VariantType.String)]
    if method == "InjectFault":
        return [ua.Variant("nope", ua.VariantType.String), ua.Variant(1.0, ua.VariantType.Double)]
    if method == "AckAlarm":
        return [ua.Variant(10**6, ua.VariantType.UInt64)]
    if method == "GetEvents":
        return [ua.Variant(10**9, ua.VariantType.UInt64), ua.Variant(1, ua.VariantType.UInt32)]
    return []


async def test_every_handler_touches_the_engine_with_run_until_first(plant):
    """A command right after an idle stretch must see the plant at the clock's time, not at the last tick."""
    real = plant.engine
    for key, handler in plant.method_handlers.items():
        probe = _EngineProbe(real)
        plant.engine = probe
        try:
            result = await handler(ua.NodeId("x", 2), *_sample_args(key))
        finally:
            plant.engine = real
        assert probe.accessed[0] == "run_until", (key, probe.accessed)
        assert isinstance(result[0].Value, str), key
    assert plant.engine is real


async def test_inject_fault_arrives_as_datachange_with_source_timestamp(plant, client, ns):
    recorder = Recorder()
    subscription = await client.create_subscription(50, recorder)
    names = ["AlarmId", "AlarmActive", "AlarmCode", "AlarmText", "AlarmSeverity", "EstimatedRepairMin", "FaultCount", "Status"]
    # Status subscribed first: order of arrival must still follow the order of the writes.
    handles = await subscription.subscribe_data_change(
        [var(client, ns, f"GUSS.{name}") for name in reversed(names)], queuesize=100
    )
    await wait_for(lambda: len(recorder.items) >= len(names))
    recorder.items.clear()

    before = datetime.now(timezone.utc)
    sent = time.time()
    result = await call(client, ns, "GUSS", "InjectFault", "G-102", ua.Variant(100000.0, ua.VariantType.Double))
    assert result == {"ok": True, "reason": ""}
    await wait_for(lambda: "FAULT" in recorder.values("Status"))
    await wait_for(lambda: len(recorder.values("EstimatedRepairMin")) >= 1)
    status = recorder.items[recorder.index("Status", "FAULT")]
    assert before - timedelta(seconds=1) <= status["source_ts"] <= datetime.now(timezone.utc) + timedelta(seconds=1)
    assert status["source_ts"].tzinfo is not None
    assert status["wall"] - sent < 2.0

    # Alarm fields no later than Status, and with the very same SourceTimestamp.
    status_index = recorder.index("Status", "FAULT")
    for name, value in (
        ("AlarmCode", "G-102"),
        ("AlarmText", cat.fault_spec("GUSS", "G-102").text),
        ("AlarmSeverity", 900),
        ("AlarmId", (await var(client, ns, "GUSS.AlarmId").read_value())),
        ("AlarmActive", True),
        ("EstimatedRepairMin", 100000.0),
        ("FaultCount", (await var(client, ns, "GUSS.FaultCount").read_value())),
    ):
        index = recorder.index(name, value)
        assert index < status_index, name
        assert recorder.items[index]["source_ts"] == status["source_ts"], name

    recorder.items.clear()
    assert await call(client, ns, "GUSS", "Repair") == {"ok": True, "reason": ""}
    await wait_for(lambda: "IDLE" in recorder.values("Status"))
    assert False in recorder.values("AlarmActive")
    assert recorder.index("AlarmActive", False) < recorder.index("Status", "IDLE")
    assert 0.0 in recorder.values("EstimatedRepairMin")
    # Code and text of a cleared alarm stay, the id stays too; AlarmActive is what says it is over.
    assert await var(client, ns, "GUSS.AlarmCode").read_value() == "G-102"
    await subscription.unsubscribe(handles)
    await subscription.delete()


async def test_status_changing_twice_in_one_publish_still_carries_the_alarm_in_that_publish(plant, client, ns):
    """asyncua groups a publish by monitored item, in order of first trigger.

    So with an earlier Status change in the same publishing interval, Status=FAULT can sit ahead of the alarm fields
    inside one publish. They never arrive later than that publish: a consumer that drains its queue before reacting
    always has them. Reported to the MES side as a rule.
    """
    recorder = Recorder()
    subscription = await client.create_subscription(400, recorder)
    await subscription.subscribe_data_change(
        [var(client, ns, f"OF2.{name}") for name in ("Status", "AlarmId", "AlarmActive", "AlarmCode", "AlarmSeverity")],
        queuesize=100,
    )
    await wait_for(lambda: len(recorder.items) >= 5)
    recorder.items.clear()
    engine = plant.engine
    assert engine.inject_fault("OF2", "O-301", 1000.0)["ok"]
    plant._publish_fast()
    assert engine.repair("OF2")["ok"]
    plant._publish_fast()
    assert engine.inject_fault("OF2", "O-302", 1000.0)["ok"]
    plant._publish_fast()
    await wait_for(lambda: recorder.values("Status").count("FAULT") == 2 and "O-302" in recorder.values("AlarmCode"))
    last_fault = max(i for i, row in enumerate(recorder.items) if row["name"] == "Status" and row["value"] == "FAULT")
    alarm = recorder.items[recorder.index("AlarmCode", "O-302")]
    assert alarm["batch"] <= recorder.items[last_fault]["batch"]
    assert alarm["source_ts"] == recorder.items[last_fault]["source_ts"]
    assert engine.repair("OF2")["ok"]
    plant._publish_fast()
    await subscription.delete()


async def test_inject_and_repair_refusals_over_opcua(client, ns):
    assert await call(client, ns, "OF2", "Repair") == {"ok": False, "reason": "NOT_FAULTED"}
    assert await call(client, ns, "OF2", "InjectFault", "G-101", 5.0) == {"ok": False, "reason": "BAD_COMMAND"}
    assert (await call(client, ns, "OF2", "InjectFault", "", 0.0))["ok"]
    assert await call(client, ns, "OF2", "InjectFault", "", 0.0) == {"ok": False, "reason": "ALREADY_FAULTED"}
    assert await var(client, ns, "OF2.AlarmCode").read_value() == cat.MACHINES["OF2"].faults[0].code
    assert await var(client, ns, "OF2.EstimatedRepairMin").read_value() == cat.MACHINES["OF2"].faults[0].repair_mean_min
    assert await call(client, ns, "OF2", "Repair") == {"ok": True, "reason": ""}
    assert await var(client, ns, "OF2.Status").read_value() == "IDLE"


async def test_ack_alarm_over_opcua(client, ns):
    alarm_before = await var(client, ns, "OF1.AlarmId").read_value()
    assert (await call(client, ns, "OF1", "InjectFault", "", 5000.0))["ok"]
    alarm_id = await var(client, ns, "OF1.AlarmId").read_value()
    assert alarm_id == alarm_before + 1
    stale = await call(client, ns, "OF1", "AckAlarm", ua.Variant(alarm_id + 7, ua.VariantType.UInt64))
    assert stale == {"ok": False, "reason": "STALE_ALARM"}
    ok = await call(client, ns, "OF1", "AckAlarm", ua.Variant(alarm_id, ua.VariantType.UInt64))
    assert ok == {"ok": True, "reason": ""}
    assert (await call(client, ns, "OF1", "Repair"))["ok"]


async def test_start_operation_refusals_and_acceptance(plant, client, ns):
    start = lambda machine, cmd: call(client, ns, machine, "StartOperation", json.dumps(cmd))  # noqa: E731
    cmd = press_cmd("L-P1", "FR-60", 2, "PR1")
    assert await start("PR1", {**cmd, "run_id": "other"}) == {"ok": False, "reason": "STALE_RUN"}
    assert await call(client, ns, "PR1", "StartOperation", "{not json") == {"ok": False, "reason": "BAD_COMMAND"}
    assert await call(client, ns, "PR1", "StartOperation", "[1,2]") == {"ok": False, "reason": "BAD_COMMAND"}
    assert await start("PR1", {**cmd, "n_billets": 0}) == {"ok": False, "reason": "BAD_COMMAND"}
    assert await start("SAE", cmd) == {"ok": False, "reason": "NOT_ELIGIBLE"}
    assert await start("PR1", press_cmd("L-P1", "ST-200", 2, "PR1")) == {"ok": False, "reason": "NOT_ELIGIBLE"}
    assert await start("PR1", {**cmd, "n_billets": 100_000}) == {"ok": False, "reason": "NO_MATERIAL"}

    assert (await call(client, ns, "PR1", "InjectFault", "P-201", 9000.0))["ok"]
    assert await start("PR1", cmd) == {"ok": False, "reason": "MACHINE_FAULT"}
    assert (await call(client, ns, "PR1", "Repair"))["ok"]

    # The node addresses the machine, a machine key inside the JSON cannot redirect it.
    assert await start("PR1", {**cmd, "machine": "PR2"}) == {"ok": True, "reason": ""}
    assert await var(client, ns, "PR1.Status").read_value() in ("RUN", "SETUP")
    current = json.loads(await var(client, ns, "PR1.CurrentOperation").read_value())
    assert current["op_ids"] == ["L-P1:press"]
    assert current["order_ids"] == ["A-1"]
    assert current["product_id"] == "FR-60"
    assert current["phase"] == "RUN"
    assert await var(client, ns, "PR1.SetupClass").read_value() == "6060|M-1001"
    assert await start("PR1", cmd) == {"ok": False, "reason": "ALREADY_STARTED"}
    assert await start("PR1", press_cmd("L-P2", "FR-60", 1, "PR1")) == {"ok": False, "reason": "MACHINE_BUSY"}
    assert plant.engine.machine_view("PR2")["CurrentOperation"] == ""


async def test_get_events_pull_and_event_seq(plant, client, ns):
    plant_obj = client.get_node(ua.NodeId("Werkhalle.Plant", ns))
    method = ua.NodeId("Werkhalle.Plant.GetEvents", ns)
    u64 = ua.VariantType.UInt64
    u32 = ua.VariantType.UInt32
    first = json.loads(await plant_obj.call_method(method, ua.Variant(0, u64), ua.Variant(3, u32)))
    assert first["oldest_seq"] == 1
    assert [e["seq"] for e in first["events"]] == [1, 2, 3]
    assert first["events"][0]["type"] == "melt_cast"
    assert first["events"][0]["plan_id"] == cat.INITIAL_MELT_PLAN_ID
    newest = first["newest_seq"]
    await wait_for_seq(client, ns, newest)
    assert (await call(client, ns, "GUSS", "StartOperation", json.dumps(cast_cmd("M-6063-90001", "6063"))))["ok"]
    seq_after = await var(client, ns, "Plant.EventSeq").read_value()
    assert seq_after > newest
    pulled = json.loads(await plant_obj.call_method(method, ua.Variant(newest, u64), ua.Variant(500, u32)))
    started = [e for e in pulled["events"] if e["type"] == "op_started" and e["op_ids"] == ["M-6063-90001:cast"]]
    assert started and started[0]["machine"] == "GUSS"
    assert all(e["seq"] > newest for e in pulled["events"])
    default_count = json.loads(await plant_obj.call_method(method, ua.Variant(0, u64), ua.Variant(0, u32)))
    assert default_count["events"], "maxCount 0 falls back to a sane default"


async def wait_for_seq(client, ns, seq) -> None:
    deadline = time.monotonic() + 5.0
    while await var(client, ns, "Plant.EventSeq").read_value() < seq:
        assert time.monotonic() < deadline
        await asyncio.sleep(0.02)


async def test_event_seq_notifies_a_subscriber(client, ns):
    recorder = Recorder()
    subscription = await client.create_subscription(50, recorder)
    await subscription.subscribe_data_change(var(client, ns, "Plant.EventSeq"))
    await wait_for(lambda: recorder.items)
    recorder.items.clear()
    assert (await call(client, ns, "VP", "StartOperation", json.dumps({
        "machine": "VP", "run_id": RUN_ID, "op_ids": ["x:pack"], "kind": "pack", "lot_id": "x"})))["reason"] == "NO_MATERIAL"
    # A refusal emits nothing; a successful start does.
    await asyncio.sleep(0.2)
    assert recorder.values("EventSeq") == []
    assert (await call(client, ns, "PR2", "StartOperation", json.dumps(press_cmd("L-E1", "ST-200", 1, "PR2"))))["ok"]
    await wait_for(lambda: recorder.values("EventSeq"))
    await subscription.delete()


async def test_get_state_snapshot_parses_and_counts_calls(plant, client, ns):
    plant_obj = client.get_node(ua.NodeId("Werkhalle.Plant", ns))
    method = ua.NodeId("Werkhalle.Plant.GetStateSnapshot", ns)
    calls_var = var(client, ns, "Plant.SnapshotCalls")
    before = await calls_var.read_value()
    raw = await plant_obj.call_method(method)
    assert await calls_var.read_value() == before + 1
    snapshot = json.loads(raw)
    assert snapshot["run_id"] == RUN_ID
    assert set(snapshot["machines"]) == set(cat.MACHINE_ORDER)
    state = planning_state_from_snapshot(snapshot)
    assert isinstance(state, PlanningState)
    assert state.run_id == RUN_ID
    # A fork from it is a working engine.
    fork = SimEngine.from_snapshot(snapshot)
    fork.run_until(fork.t + 5000.0)
    # Reading variables and pulling events does not count as a snapshot.
    await var(client, ns, "PR1.Status").read_value()
    await asyncio.sleep(0.2)
    assert await calls_var.read_value() == before + 1
    await plant_obj.call_method(method)
    assert await calls_var.read_value() == before + 2
    assert plant.snapshot_calls == before + 2


async def test_material_json_matches_the_contract(client, ns):
    await asyncio.sleep(0.6)
    material = json.loads(await var(client, ns, "Plant.Material").read_value())
    assert set(material) == {"t", "transports", "buffers", "in_machine"}
    assert set(material["buffers"]) == {"BILLET_STORE", "AGE_IN", "SAW_IN", "PACK_IN", "SHIP"}
    assert set(material["buffers"]["BILLET_STORE"]) == set(cat.ALLOYS)


async def test_slow_signals_move_and_heartbeat_ticks_about_twice_a_second(client, ns):
    heartbeat = var(client, ns, "Plant.Heartbeat")
    sim = var(client, ns, "Plant.SimTimeMin")
    h0 = await heartbeat.read_value()
    t0 = await sim.read_value()
    idle0 = await var(client, ns, "SAE.IdleMinutes").read_value()
    energy0 = await var(client, ns, "Plant.TotalEnergyKWh").read_value()
    begin = time.monotonic()
    await asyncio.sleep(1.6)
    elapsed = time.monotonic() - begin
    h1 = await heartbeat.read_value()
    t1 = await sim.read_value()
    rate = (h1 - h0) / elapsed
    assert 0.5 <= rate <= 3.0, rate
    assert t1 > t0
    assert await var(client, ns, "SAE.IdleMinutes").read_value() > idle0
    assert await var(client, ns, "Plant.TotalEnergyKWh").read_value() > energy0
    assert await var(client, ns, "Plant.TotalPowerKW").read_value() > 0.0
    assert await var(client, ns, "Plant.RunId").read_value() == RUN_ID


async def test_time_factor_clamp_revert_and_apply(plant, client, ns):
    node = var(client, ns, "Plant.TimeFactor")
    original = plant.clock.factor
    assert await node.read_value() == original
    await node.write_value(ua.Variant(77.0, ua.VariantType.Double))
    await wait_for(lambda: plant.clock.factor == original)
    deadline = time.monotonic() + 5.0
    while await node.read_value() != original:
        assert time.monotonic() < deadline, "invalid write was not reverted"
        await asyncio.sleep(0.02)
    assert plant.clock.factor == original

    # Wrong type never reaches the node.
    with pytest.raises(ua.UaStatusCodeError):
        await node.write_value(ua.Variant(240, ua.VariantType.Int64))

    await node.write_value(ua.Variant(240.0, ua.VariantType.Double))
    await wait_for(lambda: plant.clock.factor == 240.0)
    assert await node.read_value() == 240.0

    await node.write_value(ua.Variant(0.0, ua.VariantType.Double))
    await wait_for(lambda: plant.clock.factor == 0.0)
    paused = plant.clock.now()
    await asyncio.sleep(0.2)
    assert plant.clock.now() == paused
    assert plant.engine.t <= paused

    await node.write_value(ua.Variant(original, ua.VariantType.Double))
    await wait_for(lambda: plant.clock.factor == original)
    assert 0.0 in ALLOWED_FACTORS


async def test_random_failures_is_writable(plant, client, ns):
    node = var(client, ns, "Plant.RandomFailures")
    assert await node.read_value() is False
    await node.write_value(ua.Variant(True, ua.VariantType.Boolean))
    await wait_for(lambda: plant.engine.random_failures is True)
    await node.write_value(ua.Variant(False, ua.VariantType.Boolean))
    await wait_for(lambda: plant.engine.random_failures is False)


async def test_read_only_variables_cannot_be_written(client, ns):
    with pytest.raises(ua.UaStatusCodeError):
        await var(client, ns, "PR1.Status").write_value(ua.Variant("IDLE", ua.VariantType.String))
    with pytest.raises(ua.UaStatusCodeError):
        await var(client, ns, "Plant.SnapshotCalls").write_value(ua.Variant(0, ua.VariantType.UInt64))


# ---------------------------------------------------------------------- publishing rules, seen from inside


class WriteLog:
    def __init__(self, plant):
        self.plant = plant
        self.rows: list[tuple[str, object, datetime]] = []
        self.original = plant._write_now

    def __enter__(self):
        def spy(nodeid, value):
            self.rows.append((nodeid.Identifier, value.Value.Value, value.SourceTimestamp))
            return self.original(nodeid, value)

        self.plant._write_now = spy
        return self

    def __exit__(self, *exc_info):
        del self.plant._write_now


async def test_fast_block_write_order_with_status_last_and_one_timestamp(plant):
    with WriteLog(plant) as log:
        assert plant.engine.inject_fault("SAE", "S-401", 60_000.0)["ok"]
        plant._publish_fast()
    rows = [row for row in log.rows if row[0].startswith("Werkhalle.SAE.")]
    names = [row[0].rsplit(".", 1)[-1] for row in rows]
    assert names[-1] == "Status"
    assert [n for n in FAST_FIELDS if n in names] == names, "written in the documented order"
    assert names.index("AlarmActive") < names.index("EstimatedRepairMin") < names.index("Status")
    assert all(n.startswith("Alarm") for n in names[: names.index("EstimatedRepairMin")])
    assert {row[2] for row in rows} == {rows[0][2]}, "one SourceTimestamp for the whole block"
    assert rows[0][2].tzinfo is not None
    assert abs((datetime.now(timezone.utc) - rows[0][2]).total_seconds()) < 5
    assert plant.engine.repair("SAE")["ok"]
    plant._publish_fast()


async def test_fast_block_cannot_suspend(plant):
    """`_publish_fast` is a plain function, and the write underneath raises if asyncua ever really suspends."""
    assert not inspect.iscoroutinefunction(plant._publish_fast)
    assert not inspect.iscoroutinefunction(plant._write_now)

    async def suspending_write(nodeid, attr, value):
        await asyncio.sleep(0)

    aspace = plant._aspace
    original = aspace.write_attribute_value
    aspace.write_attribute_value = suspending_write
    try:
        with pytest.raises(RuntimeError, match="suspended"):
            plant._write_now(ua.NodeId("Werkhalle.SAE.Status", 2), ua.DataValue(ua.Variant("IDLE", ua.VariantType.String)))
    finally:
        del aspace.write_attribute_value
    assert aspace.write_attribute_value == original


async def test_deadbands_avoid_pointless_writes(plant):
    machine = "VP"
    await asyncio.sleep(0.05)
    with WriteLog(plant) as log:
        plant._publish_fast()
        plant._publish_fast()
        plant._publish_slow()
        plant._publish_slow()
    # An idle machine whose temperature has settled writes nothing but its idle counters.
    names = [row[0].rsplit(".", 1)[-1] for row in log.rows if row[0].startswith(f"Werkhalle.{machine}.")]
    assert not set(names) & set(FAST_FIELDS)
    assert "Temperature" not in names
    assert "PowerKW" not in names
    assert "TotalCount" not in names
    assert set(names) <= {"IdleMinutes", "EnergyKWh"}


async def test_slow_fields_are_all_real_machine_variables():
    assert set(FAST_FIELDS) | set(SLOW_FIELDS) <= set(MACHINE_VARIABLES)
    assert not set(FAST_FIELDS) & set(SLOW_FIELDS)
    assert set(MACHINE_VARIABLES) - set(FAST_FIELDS) - set(SLOW_FIELDS) == {"Name", "Kind", "Unit"}


async def test_progress_deadband_lets_the_end_through(plant):
    progress = plant._machine_vars["SAE"]["Progress"]
    with WriteLog(plant) as log:
        plant._put(progress, 0.5, datetime.now(timezone.utc))
        plant._put(progress, 0.505, datetime.now(timezone.utc))  # within the deadband
        plant._put(progress, 0.0, datetime.now(timezone.utc))  # exact zero always goes out
    assert [row[1] for row in log.rows] == [0.5, 0.0]


async def _pull_all_events(client: Client, ns: int) -> list[dict]:
    plant_obj = client.get_node(ua.NodeId("Werkhalle.Plant", ns))
    method = ua.NodeId("Werkhalle.Plant.GetEvents", ns)
    events: list[dict] = []
    last = 0
    while True:
        page = json.loads(
            await plant_obj.call_method(method, ua.Variant(last, ua.VariantType.UInt64), ua.Variant(200, ua.VariantType.UInt32))
        )
        events += page["events"]
        if not page["events"]:
            assert last == page["newest_seq"]
            return events
        assert page["oldest_seq"] == 1
        last = page["events"][-1]["seq"]


async def test_closed_loop_over_real_opcua_conserves_everything():
    """Lots driven only through method calls, the trace read only through GetEvents, a fork only from GetStateSnapshot."""
    engine = SimEngine(seed=6, stochastic=True, random_failures=False, run_id=RUN_ID)
    # 160 sim minutes per real second: the protocol stays real, the test stays short.
    clock = SimClock(factor=480.0, wall=lambda: time.monotonic() * 20.0, max_step_s=1e9)
    lots = [
        Lot("L-00001", "A-1", "FR-60", 8),
        Lot("L-00002", "A-2", "KK-80", 6),
        Lot("L-00003", "A-3", "ST-200", 8),
        Lot("L-00004", "A-4", "FP-120", 5),
    ]

    def commands(lot: Lot) -> list[tuple[str, dict]]:
        product = lot.product
        press = min(product.press_min_per_billet)
        furnace = "OF2" if lot.id == "L-00003" else "OF1"
        return [
            (press, press_cmd(lot.id, lot.product_id, lot.n_billets, press, lot.order_id)),
            (furnace, age_cmd([lot.id], product.recipe, furnace, f"B-r1-{lot.id}")),
            ("SAE", saw_cmd(lot.id)),
            ("VP", pack_cmd(lot.id)),
        ]

    async with PlantServer(engine, clock, free_port()) as server:
        connection = Client(server.endpoint, timeout=10, watchdog_intervall=5)
        await connection.connect()
        try:
            ns = await connection.get_namespace_index(NAMESPACE)
            plant_obj = connection.get_node(ua.NodeId("Werkhalle.Plant", ns))
            pending = {lot.id: commands(lot) for lot in lots}
            snapshot = None
            deadline = time.monotonic() + 60.0
            while True:
                assert time.monotonic() < deadline, "lots did not get through"
                for lot_id, queue in pending.items():
                    if not queue:
                        continue
                    machine, cmd = queue[0]
                    result = await call(connection, ns, machine, "StartOperation", json.dumps(cmd))
                    if result["ok"]:
                        queue.pop(0)
                    else:
                        assert result["reason"] in ("MACHINE_BUSY", "NO_MATERIAL"), (lot_id, cmd, result)
                if snapshot is None and sum(len(queue) for queue in pending.values()) <= 11:
                    snapshot = json.loads(
                        await plant_obj.call_method(ua.NodeId("Werkhalle.Plant.GetStateSnapshot", ns))
                    )
                    history_at_fork = await _pull_all_events(connection, ns)
                material = json.loads(await var(connection, ns, "Plant.Material").read_value())
                if material["buffers"]["SHIP"]["lots_total"] == len(lots):
                    break
                await asyncio.sleep(0.02)
            events = await _pull_all_events(connection, ns)
            assert [e["seq"] for e in events] == list(range(1, len(events) + 1))
            assert_conserved(events, lots, complete=True)
            assert sum(p["pieces"] for e in events if e["type"] == "packed" for p in e["packages"]) == material["buffers"]["SHIP"]["pieces"]

            assert snapshot is not None
            fork = SimEngine.from_snapshot(snapshot)
            FifoDriver(fork, lots).run(horizon=snapshot["t"] + 50_000.0)
            fork_events = fork.events_after(snapshot["seq"], 10**9)["events"]
            history = [e for e in history_at_fork if e["seq"] <= snapshot["seq"]]
            assert len(history) == snapshot["seq"]
            assert_conserved(history + fork_events, lots, complete=True)
        finally:
            await connection.disconnect()


async def test_driver_failure_propagates_out_of_run():
    engine = SimEngine(seed=1, run_id=RUN_ID)
    server = PlantServer(engine, SimClock(factor=120.0), free_port())
    await server.start()
    try:
        calls = {"n": 0}
        original = engine.run_until

        def explode(t):
            calls["n"] += 1
            if calls["n"] > 2:
                raise ValueError("boom")
            return original(t)

        engine.run_until = explode
        with pytest.raises(ValueError, match="boom"):
            await asyncio.wait_for(server.run(), timeout=10.0)
    finally:
        await server.stop()
