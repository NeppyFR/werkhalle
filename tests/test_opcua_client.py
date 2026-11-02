"""PlantClient and Shadow against a real asyncua server with the node layout of PLAN section 4.

The stand-in writes values the way the plant does: in a block with no suspension, each with a wall-clock SourceTimestamp. The server and
the client share one event loop here, which keeps a burst inside one publish cycle by construction.
"""
from __future__ import annotations

import asyncio
import json
import socket
import time
from collections.abc import Callable
from datetime import datetime, timezone

import pytest
from asyncua import Server, ua

from werkhalle.domain import catalog

from werkhalle.mes.alarms import AlarmTracker
from werkhalle.mes.db import RunDb
from werkhalle.mes.oee import WINDOW_START, OeeTracker
from werkhalle.mes.opcua_client import CHANGE_RING, METHOD_RING, DataChange, PlantClient, PlantNotConnected
from werkhalle.mes.shadow import FaultEvent, Shadow
from werkhalle.mes.trace import TraceStore

S, D, U64, U32, U16, B = (ua.VariantType.String, ua.VariantType.Double, ua.VariantType.UInt64, ua.VariantType.UInt32,
                          ua.VariantType.UInt16, ua.VariantType.Boolean)

PLANT_VARS = {
    "RunId": ("", S), "SimTimeMin": (0.0, D), "TimeFactor": (120.0, D), "RandomFailures": (True, B), "Heartbeat": (0, U64),
    "EventSeq": (0, U64), "Material": ("{}", S), "TotalPowerKW": (0.0, D), "TotalEnergyKWh": (0.0, D), "SnapshotCalls": (0, U64),
}
MACHINE_VARS = {
    "Name": ("", S), "Kind": ("press", S), "Unit": ("Stangen", S), "Status": ("IDLE", S),
    "Temperature": (430.0, D), "TemperatureSetpoint": (430.0, D), "PowerKW": (90.0, D), "EnergyKWh": (0.0, D),
    "TotalCount": (0, U64), "GoodCount": (0, U64), "ScrapCount": (0, U64),
    "RunMinutes": (0.0, D), "SetupMinutes": (0.0, D), "FaultMinutes": (0.0, D), "IdleMinutes": (0.0, D), "IdealMinutes": (0.0, D),
    "SetupClass": ("", S), "CurrentOperation": ("", S), "Progress": (0.0, D),
    "AlarmId": (0, U64), "AlarmActive": (False, B), "AlarmCode": ("", S), "AlarmText": ("", S), "AlarmSeverity": (0, U16),
    "EstimatedRepairMin": (0.0, D), "FaultCount": (0, U64),
}


_shelf: list = []


@pytest.fixture(scope="session", autouse=True)
def address_space_shelf(tmp_path_factory):
    """The standard address space takes 1.5 s per server to build, and 10 ms to load from a shelf made once."""
    _shelf.append(tmp_path_factory.mktemp("opcua") / "standard_address_space")
    yield
    _shelf.clear()


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def result(ok: bool = True, reason: str = "") -> list[ua.Variant]:
    return [ua.Variant(json.dumps({"ok": ok, "reason": reason}), S)]


class StandIn:
    """A small plant address space with scriptable behaviour."""

    def __init__(self, port: int, run_id: str, machines: tuple[str, ...] = ("PR1", "OF1", "XY9"), fault_counts: dict | None = None):
        self.url = f"opc.tcp://127.0.0.1:{port}/werkhalle/"
        self.port = port
        self.run_id = run_id
        self.machines = machines
        self.fault_counts = fault_counts or {}
        self.server: Server | None = None
        self.idx = 0
        self.events: list[dict] = []
        self.oldest_seq = 1
        self.get_events_delay = 0.0
        self.get_events_inflight = 0
        self.get_events_max_inflight = 0
        self.get_events_after: list[int] = []
        self.calls: list[tuple] = []
        self.snapshot = {"run_id": run_id, "seq": 0, "t": 12.0}
        self.refuse_next_fault = False

    def nodeid(self, machine: str | None, name: str) -> ua.NodeId:
        return ua.NodeId(f"Werkhalle.{machine or 'Plant'}.{name}", self.idx)

    async def start(self) -> None:
        server = Server()
        await server.init(_shelf[0])
        server.set_endpoint(self.url)
        server.set_server_name("Stand-in")
        server.set_security_policy([ua.SecurityPolicyType.NoSecurity])
        self.idx = await server.register_namespace("urn:werkhalle:plant")
        root = await server.nodes.objects.add_object(ua.NodeId("Werkhalle", self.idx), ua.QualifiedName("Werkhalle", self.idx))
        plant = await root.add_object(ua.NodeId("Werkhalle.Plant", self.idx), ua.QualifiedName("Plant", self.idx))
        for name, (default, vtype) in PLANT_VARS.items():
            value = self.run_id if name == "RunId" else default
            node = await plant.add_variable(self.nodeid(None, name), ua.QualifiedName(name, self.idx), ua.Variant(value, vtype))
            if name in ("TimeFactor", "RandomFailures"):
                await node.set_writable()
        await plant.add_method(ua.NodeId("Werkhalle.Plant.GetEvents", self.idx), ua.QualifiedName("GetEvents", self.idx),
                               self._get_events, [U64, U32], [S])
        await plant.add_method(ua.NodeId("Werkhalle.Plant.GetStateSnapshot", self.idx),
                               ua.QualifiedName("GetStateSnapshot", self.idx), self._get_snapshot, [], [S])
        machines = await root.add_object(ua.NodeId("Werkhalle.Machines", self.idx), ua.QualifiedName("Machines", self.idx))
        for machine in self.machines:
            obj = await machines.add_object(ua.NodeId(f"Werkhalle.{machine}", self.idx), ua.QualifiedName(machine, self.idx))
            for name, (default, vtype) in MACHINE_VARS.items():
                value = f"Maschine {machine}" if name == "Name" else default
                if name == "FaultCount":
                    value = self.fault_counts.get(machine, 0)
                await obj.add_variable(self.nodeid(machine, name), ua.QualifiedName(name, self.idx), ua.Variant(value, vtype))
            await self._machine_method(obj, machine, "StartOperation", [S])
            await self._machine_method(obj, machine, "InjectFault", [S, D])
            await self._machine_method(obj, machine, "Repair", [])
            await self._machine_method(obj, machine, "AckAlarm", [U64])
        await server.start()
        self.server = server

    async def _machine_method(self, obj, machine: str, name: str, inputs: list) -> None:
        async def handler(_parent, *args):
            self.calls.append((machine, name, *[a.Value for a in args], *[a.VariantType for a in args]))
            if name == "InjectFault" and self.refuse_next_fault:
                self.refuse_next_fault = False
                return result(False, "ALREADY_FAULTED")
            return result()

        await obj.add_method(ua.NodeId(f"Werkhalle.{machine}.{name}", self.idx), ua.QualifiedName(name, self.idx), handler, inputs, [S])

    async def _get_events(self, _parent, after, max_count):
        self.get_events_after.append(after.Value)
        self.get_events_inflight += 1
        self.get_events_max_inflight = max(self.get_events_max_inflight, self.get_events_inflight)
        try:
            if self.get_events_delay:
                await asyncio.sleep(self.get_events_delay)
            matching = [e for e in self.events if e["seq"] > after.Value and e["seq"] >= self.oldest_seq][: max_count.Value]
            newest = self.events[-1]["seq"] if self.events else 0
            return [ua.Variant(json.dumps({"oldest_seq": self.oldest_seq, "newest_seq": newest, "events": matching}), S)]
        finally:
            self.get_events_inflight -= 1

    async def _get_snapshot(self, _parent):
        self.calls.append(("Plant", "GetStateSnapshot"))
        return [ua.Variant(json.dumps(self.snapshot), S)]

    async def stop(self) -> None:
        if self.server is not None:
            server, self.server = self.server, None
            await server.stop()
            # Otherwise the shelf is closed by the garbage collector, on whatever thread that happens to run.
            shelf = getattr(server.iserver.aspace._nodes, "source", None)
            if shelf is not None:
                shelf.close()

    async def write(self, machine: str | None, name: str, value, ts: datetime | None = None) -> datetime:
        """One write with a wall-clock SourceTimestamp. Has no real suspension point, so a run of these is a plant fast block."""
        ts = ts or datetime.now(timezone.utc)
        table = PLANT_VARS if machine is None else MACHINE_VARS
        variant = ua.Variant(value, table[name][1])
        await self.server.write_attribute_value(self.nodeid(machine, name), ua.DataValue(variant, SourceTimestamp=ts, ServerTimestamp=ts))
        return ts

    async def read(self, machine: str | None, name: str):
        return await self.server.get_node(self.nodeid(machine, name)).read_value()

    async def add_events(self, count: int) -> None:
        for _ in range(count):
            self.events.append({"seq": len(self.events) + 1, "t": float(len(self.events)), "type": "lot_arrived", "lot_id": "L-1",
                                "stage": "age"})
        await self.write(None, "EventSeq", self.events[-1]["seq"])

    async def fault_block(self, machine: str, alarm_id: int, count: int, code: str = "P-204", text: str = "Hauptzylinder undicht") -> datetime:
        """The fast block of PLAN section 4: Alarm*, EstimatedRepairMin, FaultCount, then Status last."""
        await self.write(machine, "AlarmId", alarm_id)
        await self.write(machine, "AlarmActive", True)
        await self.write(machine, "AlarmCode", code)
        await self.write(machine, "AlarmText", text)
        await self.write(machine, "AlarmSeverity", 900)
        await self.write(machine, "EstimatedRepairMin", 240.0)
        await self.write(machine, "FaultCount", count)
        return await self.write(machine, "Status", "FAULT")


async def wait_until(predicate: Callable[[], bool], timeout: float = 15.0, what: str = "") -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError(f"timed out waiting for {what or predicate}")
        await asyncio.sleep(0.01)


@pytest.fixture
async def plant():
    stand_in = StandIn(free_port(), "r-eins")
    await stand_in.start()
    yield stand_in
    await stand_in.stop()


@pytest.fixture
async def client(plant):
    plant_client = PlantClient(plant.url, stale_after_s=1.0)
    yield plant_client
    await plant_client.stop()


class Recorder:
    """Collects what a client delivers."""

    def __init__(self, client: PlantClient) -> None:
        self.changes: list[DataChange] = []
        self.batches: list[list[DataChange]] = []
        self.events: list[dict] = []
        self.gaps: list[tuple[int, int]] = []
        self.connection: list[tuple[bool, str]] = []
        self.stale: list[bool] = []
        self.runs: list[tuple[str | None, str]] = []
        self.log: list[str] = []
        client.add_datachange_callback(self._on_change)
        client.add_batch_callback(self._on_batch)
        client.add_event_callback(self._on_event)
        client.add_gap_callback(lambda after, oldest: self.gaps.append((after, oldest)))
        client.add_connection_callback(self._on_connection)
        client.add_stale_callback(self.stale.append)
        client.add_run_changed_callback(self._on_run)

    async def _on_change(self, change: DataChange) -> None:
        self.changes.append(change)

    async def _on_batch(self, batch: list[DataChange]) -> None:
        self.batches.append(batch)
        self.log.append(f"batch:{len(batch)}")

    async def _on_event(self, event: dict) -> None:
        self.events.append(event)

    def _on_connection(self, connected: bool, reason: str) -> None:
        self.connection.append((connected, reason))
        self.log.append(f"connection:{connected}")

    async def _on_run(self, old: str | None, new: str) -> None:
        self.runs.append((old, new))
        self.log.append(f"run:{new}")

    def values(self, machine: str | None, name: str) -> list:
        return [c.value for c in self.changes if c.machine == machine and c.name == name]

    def seen(self, machine: str | None, name: str) -> bool:
        return any(c.machine == machine and c.name == name for c in self.changes)


async def started(client: PlantClient) -> Recorder:
    recorder = Recorder(client)
    await client.start()
    await wait_until(lambda: recorder.seen("XY9", "FaultCount") or recorder.seen("PR1", "FaultCount"), what="initial values")
    return recorder


class TestBrowse:
    async def test_finds_machines_variables_and_methods_without_hard_coding(self, plant, client):
        await client.connect()
        assert client.run_id == "r-eins"
        assert client.machine_ids() == ["PR1", "OF1", "XY9"]  # XY9 is in no catalogue
        assert client.namespace_uri == "urn:werkhalle:plant"
        table = client.nodes_table()
        assert table["endpoint"] == plant.url
        assert table["namespace"] == "urn:werkhalle:plant"
        assert len(table["nodes"]) == len(PLANT_VARS) + 3 * len(MACHINE_VARS)
        by_path = {n["path"]: n for n in table["nodes"]}
        status = by_path["Werkhalle/Machines/XY9/Status"]
        assert status["node_id"] == f"ns={plant.idx};s=Werkhalle.XY9.Status"
        assert status["machine"] == "XY9"
        assert status["name"] == "Status"
        assert by_path["Werkhalle/Plant/RunId"]["machine"] is None

    async def test_node_table_fills_from_data_changes(self, plant, client):
        recorder = await started(client)
        by_path = {n["path"]: n for n in client.nodes_table()["nodes"]}
        status = by_path["Werkhalle/Machines/PR1/Status"]
        assert status["value"] == "IDLE"
        assert status["type"] == "String"
        assert status["changes"] == 1
        assert status["source_ts"] is not None
        assert by_path["Werkhalle/Machines/PR1/AlarmSeverity"]["type"] == "UInt16"
        await plant.write("PR1", "Status", "RUN")
        await wait_until(lambda: "RUN" in recorder.values("PR1", "Status"))
        status = next(n for n in client.nodes_table()["nodes"] if n["path"] == "Werkhalle/Machines/PR1/Status")
        assert status["value"] == "RUN"
        assert status["changes"] == 2

    async def test_connect_retries_until_the_server_appears(self):
        port = free_port()
        client = PlantClient(f"opc.tcp://127.0.0.1:{port}/werkhalle/", connect_retry_s=0.1)
        with pytest.raises(TimeoutError):
            await client.connect(timeout=0.3)

        stand_in = StandIn(port, "r-spaet")
        task = asyncio.create_task(client.start())
        await asyncio.sleep(0.3)
        await stand_in.start()
        try:
            await asyncio.wait_for(task, 20.0)
            assert client.run_id == "r-spaet"
            assert client.connected
        finally:
            await client.stop()
            await stand_in.stop()


class TestDataChanges:
    async def test_first_notifications_are_flagged_baseline(self, plant, client):
        recorder = await started(client)
        await wait_until(lambda: recorder.seen("XY9", "Status"))
        first = {c.path: c for c in reversed(recorder.changes)}
        assert all(c.baseline for c in first.values())
        await plant.write("PR1", "Progress", 0.5)
        await wait_until(lambda: 0.5 in recorder.values("PR1", "Progress"))
        assert [c for c in recorder.changes if c.name == "Progress" and c.value == 0.5][0].baseline is False

    async def test_changes_reach_the_callback_in_order_with_timestamps(self, plant, client):
        recorder = await started(client)
        written: list[tuple[float, datetime]] = []
        for burst in range(5):
            for i in range(20):
                value = burst * 100.0 + i
                written.append((value, await plant.write("PR1", "Temperature", value)))
            await asyncio.sleep(0.03)
        expected = [v for v, _ in written]
        await wait_until(lambda: recorder.values("PR1", "Temperature")[-1] == expected[-1])
        changes = [c for c in recorder.changes if c.machine == "PR1" and c.name == "Temperature"][1:]
        assert [c.value for c in changes] == expected
        for change, (_, ts) in zip(changes, written):
            assert change.source_ts == pytest.approx(ts.timestamp(), abs=1e-5)
            assert change.received_ts >= change.source_ts - 0.01
            assert change.latency_ms >= 0.0
            assert change.path == "Werkhalle/Machines/PR1/Temperature"

    async def test_a_burst_of_one_publish_arrives_as_one_batch(self, plant, client):
        recorder = await started(client)
        for value in ("RUN", "FAULT", "RUN", "IDLE"):
            await plant.write("PR1", "Status", value)
        await wait_until(lambda: len(recorder.values("PR1", "Status")) == 5, what="the whole burst")
        assert recorder.values("PR1", "Status") == ["IDLE", "RUN", "FAULT", "RUN", "IDLE"]
        carrying = [b for b in recorder.batches if any(c.machine == "PR1" and c.name == "Status" and not c.baseline for c in b)]
        assert len(carrying) == 1
        assert [c.value for c in carrying[0] if c.name == "Status"] == ["RUN", "FAULT", "RUN", "IDLE"]

    async def test_slow_callback_loses_nothing_and_keeps_order(self, plant, client):
        seen = []

        async def slow(change: DataChange) -> None:
            if change.machine == "PR1" and change.name == "Progress":
                await asyncio.sleep(0.005)
                seen.append(change.value)

        client.add_datachange_callback(slow)
        recorder = await started(client)
        values = [i / 200 for i in range(1, 101)]
        for value in values:
            await plant.write("PR1", "Progress", value)
            await asyncio.sleep(0.001)
        await wait_until(lambda: seen and seen[-1] == values[-1], what="slow consumer to catch up")
        assert seen == [0.0, *values]
        assert recorder.values("PR1", "Progress")[-1] == values[-1]

    async def test_failing_callback_does_not_stop_the_others(self, plant, client):
        def boom(_change):
            raise RuntimeError("kaputt")

        client.add_datachange_callback(boom)
        recorder = await started(client)
        await plant.write("PR1", "Status", "RUN")
        await wait_until(lambda: "RUN" in recorder.values("PR1", "Status"))

    async def test_change_ring_is_bounded_and_shortens_large_strings(self, plant, client):
        await started(client)
        material = json.dumps({"t": 1.0, "padding": "x" * 5000})
        await plant.write(None, "Material", material)
        await wait_until(lambda: any(r["path"].endswith("Material") and r.get("truncated") for r in client.recent_changes(500)["changes"]))
        row = next(r for r in client.recent_changes(500)["changes"] if r["path"].endswith("Material") and r.get("truncated"))
        assert len(row["value"]) == 120
        node = next(n for n in client.nodes_table()["nodes"] if n["path"].endswith("Material"))
        assert node["truncated"] is True
        assert len(node["value"]) == 120

        for i in range(CHANGE_RING + 100):
            await plant.write("PR1", "PowerKW", float(i))
            if i % 50 == 0:
                await asyncio.sleep(0.01)
        await wait_until(lambda: client.recent_changes(1)["changes"][0]["value"] == float(CHANGE_RING + 99))
        changes = client.recent_changes(10_000)["changes"]
        assert len(changes) == CHANGE_RING
        assert changes[0]["value"] == float(CHANGE_RING + 99)  # newest first
        assert changes[0]["node_id"].endswith("Werkhalle.PR1.PowerKW")
        assert {"node_id", "path", "value", "source_ts", "received_wall_ts", "latency_ms"} <= set(changes[0])
        assert len(client.recent_changes(5)["changes"]) == 5
        assert client.counters()["datachanges_total"] >= CHANGE_RING + 100

    async def test_rate_counts_recent_seconds(self, plant, client):
        await started(client)
        for i in range(150):
            await plant.write("PR1", "PowerKW", float(i))
            await asyncio.sleep(0.01)
        await wait_until(lambda: client.per_second() > 0)
        assert client.recent_changes(1)["per_second"] > 0
        assert client.counters()["datachanges_per_s"] > 0


class TestFaultDetection:
    async def attached(self, client: PlantClient) -> tuple[Recorder, Shadow, AlarmTracker, list[FaultEvent]]:
        faults: list[FaultEvent] = []
        shadow = Shadow(on_fault=faults.append)
        alarms = AlarmTracker(sim_clock=shadow.sim_time_at)
        shadow.attach(client, alarms=alarms)
        recorder = await started(client)
        await wait_until(lambda: shadow.status("XY9") is not None)
        return recorder, shadow, alarms, faults

    async def test_fault_fires_with_status_timestamp_and_sensible_latency(self, plant, client):
        _, shadow, alarms, faults = await self.attached(client)
        await plant.write(None, "SimTimeMin", 500.0)
        await plant.write("PR1", "Status", "RUN")
        await asyncio.sleep(0.25)
        ts = await plant.fault_block("PR1", 3, 1)
        await wait_until(lambda: faults, what="fault callback")
        fault = faults[0]
        assert fault.machine == "PR1"
        assert fault.source_ts == pytest.approx(ts.timestamp(), abs=1e-5)
        assert fault.baseline is False
        assert fault.status == "FAULT"
        # Published every 100 ms, so normally far below a second. The bound only rules out a timestamp mix-up on a shared machine.
        assert 0.0 <= fault.latency_ms < 5000.0
        assert fault.latency_ms == pytest.approx((fault.received_ts - fault.source_ts) * 1000.0)
        assert fault.alarm_id == 3
        assert fault.code == "P-204"
        assert fault.text == "Hauptzylinder undicht"
        assert fault.severity == 900
        assert fault.est_repair_min == 240.0
        assert fault.fault_count == 1
        assert fault.t >= 500.0

        # The alarm row exists when the callback runs, and it has the Status timestamp the failure chain measures.
        row = alarms.get("PR1:3")
        assert row["source_ts"] == pytest.approx(ts.timestamp(), abs=1e-5)
        assert row["latency_ms"] == pytest.approx(fault.latency_ms, abs=0.1)
        assert row["active"] is True
        assert [r["id"] for r in alarms.active()] == ["PR1:3"]

        machine = next(m for m in shadow.to_state_machines() if m["id"] == "PR1")
        assert machine["status"] == "FAULT"
        assert machine["alarm"] == {"active": True, "id": 3, "code": "P-204", "text": "Hauptzylinder undicht", "severity": 900}
        assert machine["est_repair_min"] == 240.0
        assert machine["fault_count"] == 1

        await asyncio.sleep(0.3)
        assert len(faults) == 1

    async def test_fault_inside_a_burst_with_other_variables_is_delivered(self, plant, client):
        recorder, shadow, alarms, faults = await self.attached(client)
        await plant.write("PR1", "Temperature", 481.0)
        await plant.write("PR1", "Status", "RUN")
        await plant.write("PR1", "PowerKW", 300.0)
        fault_ts = await plant.fault_block("PR1", 7, 1, code="P-201", text="Hydraulikdruck zu niedrig")
        await plant.write("PR1", "Temperature", 482.0)
        await plant.write("PR1", "Status", "RUN")
        await plant.write("PR1", "PowerKW", 310.0)
        await plant.write("PR1", "Status", "IDLE")

        await wait_until(lambda: len(recorder.values("PR1", "Status")) == 5, what="the whole burst")
        await wait_until(lambda: faults, what="fault callback")
        assert recorder.values("PR1", "Status") == ["IDLE", "RUN", "FAULT", "RUN", "IDLE"]
        assert len(faults) == 1
        fault = faults[0]
        assert fault.source_ts == pytest.approx(fault_ts.timestamp(), abs=1e-5)
        assert fault.status == "IDLE"  # at the end of the batch; the runtime can tell the fault is already over
        assert fault.code == "P-201"
        assert 0.0 <= fault.latency_ms < 5000.0
        assert alarms.get("PR1:7")["code"] == "P-201"
        assert shadow.status("PR1") == "IDLE"

    async def test_two_faults_in_one_publish_fire_twice(self, plant, client):
        _, _, _, faults = await self.attached(client)
        first = await plant.fault_block("PR1", 3, 1)
        await plant.write("PR1", "Status", "RUN")
        second = await plant.fault_block("PR1", 4, 2, code="P-202", text="Matrizenbruch")
        await wait_until(lambda: len(faults) == 2)
        assert [f.source_ts for f in faults] == pytest.approx([first.timestamp(), second.timestamp()], abs=1e-5)
        assert all(f.baseline is False for f in faults)

    async def test_warning_does_not_fire_a_fault(self, plant, client):
        recorder, _, alarms, faults = await self.attached(client)
        await plant.write("PR1", "AlarmId", 2)
        await plant.write("PR1", "AlarmActive", True)
        await plant.write("PR1", "AlarmCode", "P-W01")
        await plant.write("PR1", "AlarmText", "Blocktemperatur über Sollwert")
        await plant.write("PR1", "AlarmSeverity", 500)
        await wait_until(lambda: alarms.get("PR1:2") is not None and alarms.get("PR1:2")["severity"] == 500)
        assert faults == []

    async def test_fault_count_alone_fires_once_and_status_does_not_repeat_it(self, plant, client):
        _, _, _, faults = await self.attached(client)
        await plant.write("PR1", "FaultCount", 1)
        await wait_until(lambda: len(faults) == 1)
        assert faults[0].fault_count == 1
        await asyncio.sleep(0.15)
        await plant.write("PR1", "Status", "FAULT")
        await asyncio.sleep(0.3)
        assert len(faults) == 1

    async def test_mid_run_attach_with_an_active_fault_fires_nothing(self):
        stand_in = StandIn(free_port(), "r-mitte", fault_counts={"PR1": 4})
        await stand_in.start()
        await stand_in.fault_block("PR1", 9, 5)
        client = PlantClient(stand_in.url)
        try:
            _, shadow, alarms, faults = await self.attached(client)
            await asyncio.sleep(0.3)
            assert faults == []
            assert shadow.status("PR1") == "FAULT"
            row = alarms.get("PR1:9")
            assert row["active"] is True
            assert row["latency_ms"] is None  # a baseline says nothing about the transport
            # The next fault is a real one.
            await stand_in.write("PR1", "Status", "IDLE")
            await asyncio.sleep(0.15)
            await stand_in.fault_block("PR1", 10, 6)
            await wait_until(lambda: faults)
            assert faults[0].alarm_id == 10
            assert faults[0].baseline is False
        finally:
            await client.stop()
            await stand_in.stop()


class TestEventPump:
    async def test_burst_is_delivered_exactly_once_and_in_order(self, plant, client):
        plant.get_events_delay = 0.02
        recorder = await started(client)
        total = 0
        for burst in (3, 700, 1, 50, 1200, 2, 40, 900):
            await plant.add_events(burst)
            total += burst
            await asyncio.sleep(0.015)
        await wait_until(lambda: client.last_event_seq == total, timeout=30.0, what="pump to catch up")
        assert [e["seq"] for e in recorder.events] == list(range(1, total + 1))
        assert plant.get_events_max_inflight == 1
        assert plant.get_events_after == sorted(plant.get_events_after)
        assert client.counters()["events_total"] == total
        assert client.counters()["last_event_seq"] == total
        # Pumped until caught up, in pages of at most 500, without one call per EventSeq change.
        assert client.get_events_calls >= total // 500

    async def test_no_calls_while_nothing_new(self, plant, client):
        await started(client)
        await asyncio.sleep(0.3)
        assert client.get_events_calls == 0

    async def test_events_written_before_subscribing_are_pumped(self, plant, client):
        await plant.add_events(5)
        recorder = await started(client)
        await wait_until(lambda: len(recorder.events) == 5)

    async def test_resume_from_a_known_seq(self, plant, client):
        await plant.add_events(10)
        client.set_last_event_seq(7)
        recorder = await started(client)
        await wait_until(lambda: client.last_event_seq == 10)
        assert [e["seq"] for e in recorder.events] == [8, 9, 10]
        assert plant.get_events_after[0] == 7

    async def test_gap_is_reported_when_the_ring_moved_on(self, plant, client):
        await plant.add_events(30)
        plant.oldest_seq = 21
        recorder = await started(client)
        await wait_until(lambda: client.last_event_seq == 30)
        assert recorder.gaps == [(0, 21)]
        assert [e["seq"] for e in recorder.events] == list(range(21, 31))

    async def test_stale_event_seq_cannot_cause_a_hot_loop(self, plant, client):
        # EventSeq claims more than the server can deliver: the answer corrects the client instead of a tight retry.
        await plant.write(None, "EventSeq", 50)
        await started(client)
        await asyncio.sleep(0.3)
        assert client.get_events_calls == 1
        assert client.newest_event_seq == 0


class TestMethods:
    async def test_method_ring_records_calls_with_args_result_and_ms(self, plant, client):
        await started(client)
        assert await client.start_operation("PR1", {"op_ids": ["L-1:press"], "kind": "press", "n_billets": 8}) == {"ok": True, "reason": ""}
        assert await client.inject_fault("OF1", "O-301", 75.0) == {"ok": True, "reason": ""}
        assert await client.repair("OF1") == {"ok": True, "reason": ""}
        assert await client.ack_alarm("XY9", 12) == {"ok": True, "reason": ""}
        assert await client.get_state_snapshot() == plant.snapshot
        await client.set_time_factor(240)
        await client.set_random_failures(False)

        calls = client.method_calls()["calls"]
        assert [c["method"] for c in calls] == ["Write RandomFailures", "Write TimeFactor", "GetStateSnapshot", "AckAlarm", "Repair",
                                                "InjectFault", "StartOperation"]
        by_method = {c["method"]: c for c in calls}
        start = by_method["StartOperation"]
        assert start["node"] == "PR1"
        assert json.loads(start["args"])["op_ids"] == ["L-1:press"]
        assert json.loads(start["result"]) == {"ok": True, "reason": ""}
        assert start["ms"] >= 0.0
        assert start["wall_ts"] == pytest.approx(time.time(), abs=60.0)
        assert by_method["InjectFault"]["node"] == "OF1"
        assert json.loads(by_method["InjectFault"]["args"]) == {"code": "O-301", "repairMin": 75.0}
        assert by_method["GetStateSnapshot"]["node"] == "Plant"
        assert by_method["Write TimeFactor"]["args"] == "240.0"
        assert by_method["Write TimeFactor"]["result"] == "ok"

    async def test_arguments_reach_the_server_with_the_right_types(self, plant, client):
        await started(client)
        await client.start_operation("PR1", {"kind": "press"})
        await client.inject_fault("OF1", "", 0)  # an int repair time still goes out as a Double
        await client.ack_alarm("XY9", 12)

        machine, name, text, text_type = plant.calls[0]
        assert (machine, name, text_type) == ("PR1", "StartOperation", S)
        assert json.loads(text) == {"kind": "press", "run_id": "r-eins"}
        assert plant.calls[1] == ("OF1", "InjectFault", "", 0.0, S, D)
        assert plant.calls[2] == ("XY9", "AckAlarm", 12, U64)

    async def test_command_run_id_is_kept_when_given(self, plant, client):
        await started(client)
        await client.start_operation("PR1", {"kind": "press", "run_id": "r-alt"})
        assert json.loads(plant.calls[0][2])["run_id"] == "r-alt"

    async def test_writes_use_the_declared_types(self, plant, client):
        await started(client)
        await client.set_time_factor(480)
        await client.set_random_failures(False)
        assert await plant.read(None, "TimeFactor") == 480.0
        node = plant.server.get_node(plant.nodeid(None, "TimeFactor"))
        assert (await node.read_data_value()).Value.VariantType == D
        assert await plant.read(None, "RandomFailures") is False

    async def test_refusal_is_returned_not_raised(self, plant, client):
        await started(client)
        plant.refuse_next_fault = True
        assert await client.inject_fault("PR1") == {"ok": False, "reason": "ALREADY_FAULTED"}
        assert "ALREADY_FAULTED" in client.method_calls()["calls"][0]["result"]

    async def test_method_ring_is_bounded_and_clips_large_results(self, plant, client):
        await started(client)
        plant.snapshot = {"run_id": "r-eins", "padding": "x" * 5000}
        await client.get_state_snapshot()
        row = client.method_calls()["calls"][0]
        assert len(row["result"]) < 400
        assert "Zeichen" in row["result"]
        for _ in range(METHOD_RING + 10):
            await client.repair("PR1")
        assert len(client.method_calls(10_000)["calls"]) == METHOD_RING
        assert len(client.method_calls(3)["calls"]) == 3

    async def test_not_connected_raises_and_is_logged(self, plant, client):
        await started(client)
        await client.stop()
        with pytest.raises(PlantNotConnected):
            await client.repair("PR1")
        with pytest.raises(PlantNotConnected):
            await client.set_time_factor(120)
        calls = client.method_calls()["calls"]
        assert "FEHLER" in calls[0]["result"]
        assert len(calls) == 2

    async def test_get_events_is_not_in_the_ring(self, plant, client):
        await plant.add_events(3)
        recorder = await started(client)
        await wait_until(lambda: len(recorder.events) == 3)
        assert client.method_calls()["calls"] == []


class TestHeartbeat:
    async def test_stale_when_heartbeat_stops_and_fresh_when_it_returns(self, plant):
        client = PlantClient(plant.url, stale_after_s=2.0)
        shadow = Shadow()
        shadow.attach(client)
        try:
            recorder = await started(client)
            for i in range(1, 6):
                await plant.write(None, "Heartbeat", i)
                await asyncio.sleep(0.1)
            assert client.stale is False
            assert recorder.stale == []
            await wait_until(lambda: client.stale, what="stale flag")
            assert recorder.stale == [True]
            assert shadow.stale is True
            await plant.write(None, "Heartbeat", 6)
            await wait_until(lambda: not client.stale, what="stale flag to clear")
            assert recorder.stale == [True, False]
            assert shadow.stale is False
        finally:
            await client.stop()


async def restart_server(plant: StandIn, run_id: str, **kwargs) -> StandIn:
    await plant.stop()
    fresh = StandIn(plant.port, run_id, **kwargs)
    for attempt in range(50):
        try:
            await fresh.start()
            return fresh
        except OSError:
            await asyncio.sleep(0.2)
    raise AssertionError("port did not come free")


class TestReconnect:
    async def test_restart_with_a_new_run_id_fires_on_run_changed_first(self, plant, client):
        faults: list[FaultEvent] = []
        shadow = Shadow(on_fault=faults.append)
        alarms = AlarmTracker(sim_clock=shadow.sim_time_at)
        shadow.attach(client, alarms=alarms)
        recorder = await started(client)
        await plant.add_events(5)
        await wait_until(lambda: client.last_event_seq == 5)
        await plant.fault_block("PR1", 3, 1)
        await wait_until(lambda: faults)

        fresh = await restart_server(plant, "r-zwei", fault_counts={"PR1": 0})
        try:
            await wait_until(lambda: recorder.runs, timeout=30.0, what="on_run_changed")
            assert recorder.runs == [("r-eins", "r-zwei")]
            assert client.run_id == "r-zwei"
            await wait_until(lambda: client.connected)
            assert [c for c, _ in recorder.connection] == [True, False, True]

            # The hard reset comes before anything of the new run reaches a consumer.
            index = recorder.log.index("run:r-zwei")
            await wait_until(lambda: any(c.name == "RunId" and c.value == "r-zwei" for c in recorder.changes), what="new data")
            first_new_batch = next(i for i, b in enumerate(recorder.batches) if any(c.value == "r-zwei" for c in b))
            batches_before_reset = sum(1 for entry in recorder.log[:index] if entry.startswith("batch"))
            assert first_new_batch >= batches_before_reset
            assert recorder.log.index("connection:True", index) > index

            # The shadow and alarms start over: no alarm of the old run, no fault replayed from the new baseline.
            await wait_until(lambda: shadow.status("PR1") == "IDLE", what="new run values")
            assert alarms.rows() == []
            assert len(faults) == 1

            # Heartbeats of the new run clear the stale flag the outage may have set.
            for beat in range(1, 200):
                await fresh.write(None, "Heartbeat", beat)
                if not client.stale:
                    break
                await asyncio.sleep(0.05)
            assert client.stale is False

            # Events restart at 1 and are pumped from scratch.
            events_before = len(recorder.events)
            await fresh.add_events(3)
            await wait_until(lambda: len(recorder.events) == events_before + 3)
            assert [e["seq"] for e in recorder.events[-3:]] == [1, 2, 3]
            assert client.last_event_seq == 3

            # A fault in the new run works, with FaultCount starting over.
            await fresh.fault_block("PR1", 1, 1)
            await wait_until(lambda: len(faults) == 2)
            assert faults[1].baseline is False
            assert alarms.get("PR1:1") is not None

            assert await client.repair("PR1") == {"ok": True, "reason": ""}
            assert fresh.calls[-1][:2] == ("PR1", "Repair")
        finally:
            await client.stop()
            await fresh.stop()

    async def test_old_run_values_in_the_queue_never_reach_the_new_run(self, plant, client):
        recorder = await started(client)
        fresh = await restart_server(plant, "r-zwei")
        try:
            await wait_until(lambda: recorder.runs, timeout=30.0)
            await wait_until(lambda: any(c.name == "RunId" and c.value == "r-zwei" for c in recorder.changes), what="new RunId value")
            run_ids = [c.value for c in recorder.changes if c.name == "RunId"]
            assert run_ids == ["r-eins", "r-zwei"]
            # Everything delivered after the change is flagged as baseline first, once per node.
            after = recorder.changes[next(i for i, c in enumerate(recorder.changes) if c.value == "r-zwei"):]
            assert any(c.baseline for c in after)
        finally:
            await client.stop()
            await fresh.stop()

    async def test_restart_with_the_same_run_id_resumes_and_treats_values_as_baseline(self, plant, client):
        faults: list[FaultEvent] = []
        shadow = Shadow(on_fault=faults.append)
        shadow.attach(client)
        recorder = await started(client)
        await plant.add_events(4)
        await wait_until(lambda: client.last_event_seq == 4)
        await plant.write("PR1", "Status", "RUN")
        await wait_until(lambda: shadow.status("PR1") == "RUN")
        before = len(recorder.changes)

        # OF1 faulted while the MES was away. XY9 is in FAULT with an unchanged count, so it was already known.
        await plant.fault_block("XY9", 2, 1)
        await wait_until(lambda: len(faults) == 1)

        fresh = await restart_server(plant, "r-eins", fault_counts={"OF1": 1, "XY9": 1})
        fresh.events = list(plant.events)
        try:
            await wait_until(lambda: [c for c, _ in recorder.connection] == [True, False, True], timeout=30.0, what="reconnect")
            await wait_until(lambda: any(c.name == "FaultCount" and c.machine == "OF1" for c in recorder.changes[before:]), what="baseline")
            assert recorder.runs == []
            assert client.run_id == "r-eins"
            assert client.last_event_seq == 4  # not reset
            fresh_changes = [c for c in recorder.changes[before:] if c.machine == "OF1" and c.name == "FaultCount"]
            assert fresh_changes[0].baseline is True

            await wait_until(lambda: len(faults) == 2, what="fault found by FaultCount")
            assert faults[1].machine == "OF1"
            assert faults[1].baseline is True
            assert faults[1].fault_count == 1
            await asyncio.sleep(0.3)
            assert len(faults) == 2  # XY9's unchanged count fires nothing

            # New events continue from the old seq, with no duplicates.
            await fresh.add_events(2)
            await wait_until(lambda: client.last_event_seq == 6)
            assert [e["seq"] for e in recorder.events] == [1, 2, 3, 4, 5, 6]
        finally:
            await client.stop()
            await fresh.stop()

    async def test_dropped_transport_with_the_server_alive_recovers(self, plant, client):
        faults: list[FaultEvent] = []
        shadow = Shadow(on_fault=faults.append)
        shadow.attach(client)
        recorder = await started(client)

        client._client.uaclient.protocol.transport.abort()
        await wait_until(lambda: [c for c, _ in recorder.connection] == [True, False, True], timeout=30.0, what="loss and reconnect")
        assert recorder.runs == []
        assert client.connected

        await asyncio.sleep(0.3)
        await plant.fault_block("PR1", 1, 1)
        await wait_until(lambda: faults, timeout=10.0, what="fault after reconnect")
        assert faults[0].machine == "PR1"
        assert shadow.status("PR1") == "FAULT"
        assert shadow.connected is True

    async def test_calls_fail_fast_during_an_outage(self, plant, client):
        recorder = await started(client)
        await plant.stop()
        await wait_until(lambda: not client.connected, what="loss")
        assert recorder.connection[-1][0] is False
        started_at = time.monotonic()
        with pytest.raises(PlantNotConnected):
            await client.inject_fault("PR1")
        assert time.monotonic() - started_at < 5.0  # no waiting on the 10 s request timeout
        await wait_until(lambda: client.stale, what="stale during the outage")

    async def test_new_run_with_a_different_address_space_is_subscribed_again(self, plant, client):
        shadow = Shadow()
        shadow.attach(client)
        recorder = await started(client)
        fresh = await restart_server(plant, "r-zwei", machines=("PR1", "NEU"))
        try:
            await wait_until(lambda: recorder.runs, timeout=30.0)
            assert client.machine_ids() == ["PR1", "NEU"]
            await wait_until(lambda: shadow.status("NEU") == "IDLE", timeout=15.0, what="the new machine")
            await fresh.write("NEU", "Status", "RUN")
            await wait_until(lambda: shadow.status("NEU") == "RUN")
            assert set(shadow.machine_ids()) == {"PR1", "NEU"}
        finally:
            await client.stop()
            await fresh.stop()


def change(machine: str | None, name: str, value, ts: float = 1000.0, *, baseline: bool = False, received: float | None = None) -> DataChange:
    received_ts = ts + 0.05 if received is None else received
    path = f"Werkhalle/Machines/{machine}/{name}" if machine else f"Werkhalle/Plant/{name}"
    return DataChange(node_id=path, path=path, machine=machine, name=name, value=value, source_ts=ts, received_ts=received_ts,
                      latency_ms=(received_ts - ts) * 1000.0, baseline=baseline)


class TestShadow:
    def make(self, now: float = 1000.0) -> tuple[Shadow, list[FaultEvent], list[float]]:
        clock = [now]
        faults: list[FaultEvent] = []
        return Shadow(on_fault=faults.append, clock=lambda: clock[0]), faults, clock

    async def test_state_machine_object_matches_the_api_shape(self):
        shadow, _, _ = self.make()
        await shadow.apply_batch([
            change("PR2", "Name", "Presse 2 (35 MN)", baseline=True), change("PR2", "Kind", "press", baseline=True),
            change("PR2", "Unit", "Stangen", baseline=True), change("PR2", "Status", "RUN", baseline=True),
            change("PR2", "Temperature", 488.23), change("PR2", "TemperatureSetpoint", 490.0), change("PR2", "PowerKW", 451.04),
            change("PR2", "EnergyKWh", 1234.56), change("PR2", "TotalCount", 320), change("PR2", "GoodCount", 316),
            change("PR2", "ScrapCount", 4), change("PR2", "SetupClass", "6082|M-3001"), change("PR2", "Progress", 0.4212),
            change("PR2", "CurrentOperation", json.dumps({"op_ids": ["L-00001:press"], "kind": "press", "lot_ids": ["L-00001"],
                                                          "order_ids": ["A-1001"], "product_id": "ST-200"})),
            change("PR2", "FaultCount", 1, baseline=True), change("PR2", "EstimatedRepairMin", 0.0),
        ])
        machine = shadow.to_state_machines()[0]
        expected_keys = {"id", "name", "kind", "unit", "capacity", "status", "status_since", "temperature", "setpoint", "power_kw",
                         "energy_kwh", "total", "good", "scrap", "setup_class", "op", "progress", "alarm", "est_repair_min", "fault_count",
                         "oee", "next_op", "waiting_on", "trend"}
        assert set(machine) == expected_keys
        assert machine["id"] == "PR2"
        assert machine["name"] == "Presse 2 (35 MN)"
        assert machine["capacity"] == 1
        assert machine["status"] == "RUN"
        assert machine["temperature"] == 488.2
        assert machine["setpoint"] == 490.0
        assert machine["power_kw"] == 451.0
        assert machine["energy_kwh"] == 1234.6
        assert (machine["total"], machine["good"], machine["scrap"]) == (320, 316, 4)
        assert machine["setup_class"] == "6082|M-3001"
        assert machine["progress"] == 0.421
        assert machine["op"] == {"op_ids": ["L-00001:press"], "kind": "press", "lot_ids": ["L-00001"], "order_ids": ["A-1001"],
                                 "product_id": "ST-200", "phase": "RUN", "batch_id": None}
        assert machine["alarm"] == {"active": False, "id": 0, "code": "", "text": "", "severity": 0}
        assert machine["oee"] is None and machine["next_op"] is None and machine["waiting_on"] is None
        assert machine["trend"] == {"temperature": [488], "power_kw": [451]}
        json.dumps(machine)

    async def test_furnace_capacity_comes_from_the_catalogue_unknown_machine_gets_one(self):
        shadow, _, _ = self.make()
        await shadow.apply_batch([change("OF2", "Status", "IDLE", baseline=True), change("XY9", "Status", "IDLE", baseline=True)])
        by_id = {m["id"]: m for m in shadow.to_state_machines()}
        assert by_id["OF2"]["capacity"] == 10
        assert by_id["XY9"]["capacity"] == 1

    async def test_op_is_none_when_idle_and_phase_follows_status(self):
        shadow, _, _ = self.make()
        await shadow.apply_batch([change("PR1", "Status", "SETUP"), change("PR1", "CurrentOperation", '{"op_ids":["L-1:press"]}')])
        assert shadow.to_state_machines()[0]["op"]["phase"] == "SETUP"
        await shadow.apply_batch([change("PR1", "Status", "RUN")])
        assert shadow.to_state_machines()[0]["op"]["phase"] == "RUN"
        await shadow.apply_batch([change("PR1", "Status", "IDLE"), change("PR1", "CurrentOperation", "")])
        assert shadow.to_state_machines()[0]["op"] is None

    async def test_broken_operation_json_does_not_break_the_shadow(self):
        shadow, _, _ = self.make()
        await shadow.apply_batch([change("PR1", "CurrentOperation", "{kaputt")])
        assert shadow.to_state_machines()[0]["op"] is None

    async def test_status_since_is_in_sim_minutes_from_the_source_timestamp(self):
        shadow, _, clock = self.make(now=1000.0)
        await shadow.apply_batch([change(None, "TimeFactor", 120.0, 999.0), change(None, "SimTimeMin", 500.0, 1000.0)])
        # One second after that SimTimeMin at factor 120 is 2 sim minutes later.
        await shadow.apply_batch([change("PR1", "Status", "RUN", 1001.0)])
        machine = shadow.to_state_machines()[0]
        assert machine["status_since"] == pytest.approx(502.0)
        # The same status again changes nothing.
        await shadow.apply_batch([change("PR1", "Status", "RUN", 1001.5)])
        assert shadow.to_state_machines()[0]["status_since"] == pytest.approx(502.0)
        await shadow.apply_batch([change("PR1", "Status", "FAULT", 1001.5)])
        assert shadow.to_state_machines()[0]["status_since"] == pytest.approx(503.0)

    async def test_status_since_in_the_first_batch_uses_that_batchs_sim_time(self):
        shadow, _, _ = self.make(now=1000.0)
        # Machines before plant values in delivery order, as happens on the first publish.
        await shadow.apply_batch([change("PR1", "Status", "RUN", 1000.0, baseline=True), change(None, "TimeFactor", 120.0, 1000.0, baseline=True),
                                  change(None, "SimTimeMin", 800.0, 1000.0, baseline=True)])
        assert shadow.to_state_machines()[0]["status_since"] == pytest.approx(800.0)

    async def test_sim_state_interpolates_and_stops_extrapolating(self):
        shadow, _, clock = self.make(now=1000.0)
        await shadow.apply_batch([change(None, "TimeFactor", 240.0, 1000.0), change(None, "SimTimeMin", 100.0, 1000.0),
                                  change(None, "RandomFailures", True, 1000.0)])
        clock[0] = 1000.5
        state = shadow.sim_state()
        assert state["t"] == pytest.approx(102.0)
        assert state["factor"] == 240.0
        assert state["wall_ts"] == 1000.5
        assert state["random_failures"] is True
        clock[0] = 1100.0  # the plant went quiet: 2 s of extrapolation at most
        assert shadow.sim_state()["t"] == pytest.approx(108.0)

    async def test_sim_time_zero_before_first_value(self):
        shadow, _, _ = self.make()
        assert shadow.sim_time_at() == 0.0
        assert shadow.sim_state()["t"] == 0.0
        assert shadow.material == {}

    async def test_plant_totals_and_material(self):
        shadow, _, _ = self.make()
        material = {"t": 1.0, "transports": [], "buffers": {"BILLET_STORE": {"6060": 12}}}
        await shadow.apply_batch([change(None, "TotalPowerKW", 1200.56), change(None, "TotalEnergyKWh", 9000.14), change(None, "SnapshotCalls", 3),
                                  change(None, "EventSeq", 77), change(None, "Heartbeat", 5), change(None, "RunId", "r-x"),
                                  change(None, "Material", json.dumps(material))])
        assert shadow.plant_totals() == {"power_kw": 1200.6, "energy_kwh": 9000.1, "snapshot_calls": 3, "event_seq": 77, "heartbeat": 5}
        assert shadow.material == material
        assert shadow.run_id == "r-x"
        await shadow.apply_batch([change(None, "Material", "{kaputt")])
        assert shadow.material == material  # keeps the last good one

    async def test_trends_sample_at_one_hertz_and_are_bounded(self):
        shadow, _, clock = self.make(now=2000.0)
        for i in range(400):
            clock[0] = 2000.0 + i * 0.25
            await shadow.apply_batch([change("PR1", "Temperature", 400.0 + i), change("PR1", "PowerKW", float(i))])
        trend = shadow.to_state_machines()[0]["trend"]
        assert len(trend["temperature"]) == 100  # 400 batches over 100 s
        for i in range(400, 1000):
            clock[0] = 2000.0 + i * 0.25
            await shadow.apply_batch([change("PR1", "Temperature", 400.0 + i), change("PR1", "PowerKW", float(i))])
        trend = shadow.to_state_machines()[0]["trend"]
        assert len(trend["temperature"]) == 120
        assert len(trend["power_kw"]) == 120
        assert trend["temperature"][-1] == 1396

    async def test_counters_and_meta_for_oee(self):
        shadow, _, _ = self.make()
        await shadow.apply_batch([change("PR1", "Name", "Presse 1"), change("PR1", "Unit", "Stangen"), change("PR1", "RunMinutes", 700.0),
                                  change("PR1", "SetupMinutes", 100.0), change("PR1", "FaultMinutes", 60.0), change("PR1", "IdleMinutes", 300.0),
                                  change("PR1", "IdealMinutes", 640.0), change("PR1", "TotalCount", 320), change("PR1", "GoodCount", 316),
                                  change("PR1", "ScrapCount", 4), change("PR1", "FaultCount", 2, baseline=True)])
        assert shadow.counters("PR1") == {"run": 700.0, "setup": 100.0, "fault": 60.0, "idle": 300.0, "ideal": 640.0, "total": 320, "good": 316,
                                          "scrap": 4, "fault_count": 2}
        assert shadow.machines_meta() == [{"id": "PR1", "name": "Presse 1", "unit": "Stangen"}]
        assert shadow.counters("NIX")["run"] == 0

    async def test_full_state_stays_within_budget(self):
        shadow, _, clock = self.make(now=3000.0)
        operation = json.dumps({"op_ids": ["L-00001:press"], "kind": "press", "lot_ids": ["L-00001"], "order_ids": ["A-1001"],
                                "product_id": "ST-200", "phase": "RUN", "batch_id": None})
        for spec in catalog.MACHINES.values():
            await shadow.apply_batch([
                change(spec.id, "Name", spec.name), change(spec.id, "Kind", spec.kind), change(spec.id, "Unit", spec.unit),
                change(spec.id, "Status", "RUN"), change(spec.id, "SetupClass", "6082|M-3001"), change(spec.id, "Progress", 0.123),
                change(spec.id, "CurrentOperation", operation), change(spec.id, "EnergyKWh", 12345.6), change(spec.id, "TotalCount", 12345),
                change(spec.id, "GoodCount", 12300), change(spec.id, "ScrapCount", 45),
            ])
        for i in range(130):
            clock[0] = 3000.0 + i
            await shadow.apply_batch([c for spec in catalog.MACHINES.values()
                                      for c in (change(spec.id, "Temperature", spec.temp_run_c + i % 3),
                                                change(spec.id, "PowerKW", spec.power_run_kw - i % 4))])
        machines = shadow.to_state_machines()
        assert all(len(m["trend"]["temperature"]) == 120 for m in machines)
        for machine in machines:
            machine["oee"] = {"oee": 0.7123, "oee_iso": 0.4123, "availability": 0.8123, "performance": 0.9123, "quality": 0.9812}
            machine["next_op"] = "L-00002:press"
        size = len(json.dumps(machines, separators=(",", ":"), ensure_ascii=False).encode("utf-8"))
        print(f"machines JSON with full trends: {size} bytes")
        # Every machine busy with an op and a next op is the worst case for this array. Sim, material, kpi, plan and counters add about 1.3 KB.
        assert size < 11_300, size
        capped = shadow.to_state_machines(trend_samples=100)
        assert len(capped[0]["trend"]["temperature"]) == 100
        assert capped[0]["trend"]["temperature"][-1] == machines[0]["trend"]["temperature"][-1]
        assert shadow.to_state_machines(trend_samples=0)[0]["trend"] == {"temperature": [], "power_kw": []}

    async def test_faults_fire_once_per_fault_count_after_a_baseline(self):
        shadow, faults, _ = self.make()
        await shadow.apply_batch([change("PR1", "FaultCount", 3, baseline=True), change("PR1", "Status", "RUN", baseline=True)])
        assert faults == []
        await shadow.apply_batch([change("PR1", "Status", "FAULT", 1001.0), change("PR1", "FaultCount", 4, 1000.9)])
        assert [f.fault_count for f in faults] == [4]
        assert faults[0].source_ts == 1001.0
        assert faults[0].baseline is False

    async def test_status_arriving_ahead_of_alarm_values_in_a_batch_still_gets_them(self):
        shadow, faults, _ = self.make()
        await shadow.apply_batch([change("PR1", "FaultCount", 0, baseline=True), change("PR1", "Status", "RUN", baseline=True)])
        await shadow.apply_batch([change("PR1", "Status", "FAULT", 1001.0), change("PR1", "AlarmId", 8, 1001.0), change("PR1", "AlarmCode", "P-202", 1001.0),
                                  change("PR1", "AlarmText", "Matrizenbruch", 1001.0), change("PR1", "AlarmSeverity", 900, 1001.0),
                                  change("PR1", "EstimatedRepairMin", 150.0, 1001.0), change("PR1", "FaultCount", 1, 1001.0)])
        assert faults[0].alarm_id == 8
        assert faults[0].code == "P-202"
        assert faults[0].est_repair_min == 150.0
        assert faults[0].fault_count == 1

    async def test_baseline_status_fault_alone_fires_nothing(self):
        shadow, faults, _ = self.make()
        await shadow.apply_batch([change("PR1", "FaultCount", 2, baseline=True), change("PR1", "Status", "FAULT", baseline=True)])
        await shadow.apply_batch([change("PR1", "FaultCount", 2, baseline=True), change("PR1", "Status", "FAULT", baseline=True)])
        assert faults == []

    async def test_fault_during_outage_found_by_count_even_when_already_repaired(self):
        shadow, faults, _ = self.make()
        await shadow.apply_batch([change("PR1", "FaultCount", 2, baseline=True), change("PR1", "Status", "RUN", baseline=True)])
        await shadow.apply_batch([change("PR1", "FaultCount", 3, 1500.0, baseline=True, received=1600.0), change("PR1", "Status", "IDLE", 1550.0, baseline=True, received=1600.0)])
        assert len(faults) == 1
        assert faults[0].baseline is True
        assert faults[0].status == "IDLE"
        assert faults[0].source_ts == 1500.0
        # A later real fault is reported on its own, from its Status.
        await shadow.apply_batch([change("PR1", "Status", "FAULT", 1700.0), change("PR1", "FaultCount", 4, 1700.0)])
        assert len(faults) == 2
        assert faults[1].baseline is False
        assert faults[1].source_ts == 1700.0

    async def test_fault_during_outage_still_active_uses_the_status_timestamp(self):
        shadow, faults, _ = self.make()
        await shadow.apply_batch([change("PR1", "FaultCount", 2, baseline=True), change("PR1", "Status", "RUN", baseline=True)])
        await shadow.apply_batch([change("PR1", "FaultCount", 3, 1500.0, baseline=True), change("PR1", "Status", "FAULT", 1500.2, baseline=True)])
        assert len(faults) == 1
        assert faults[0].source_ts == 1500.2
        assert faults[0].baseline is True

    async def test_stand_in_without_fault_count_still_fires_on_status(self):
        shadow, faults, _ = self.make()
        await shadow.apply_batch([change("PR1", "Status", "RUN", baseline=True)])
        await shadow.apply_batch([change("PR1", "Status", "FAULT", 1001.0)])
        await shadow.apply_batch([change("PR1", "Status", "RUN", 1002.0)])
        await shadow.apply_batch([change("PR1", "Status", "FAULT", 1003.0)])
        assert len(faults) == 2

    async def test_async_and_failing_fault_callbacks(self):
        seen = []

        async def on_fault(event: FaultEvent) -> None:
            seen.append(event.machine)
            raise RuntimeError("kaputt")

        shadow = Shadow(on_fault=on_fault)
        await shadow.apply_batch([change("PR1", "Status", "RUN", baseline=True)])
        await shadow.apply_batch([change("PR1", "Status", "FAULT", 1001.0)])
        assert seen == ["PR1"]
        assert shadow.status("PR1") == "FAULT"

    async def test_reset_forgets_everything_including_alarms(self):
        faults: list[FaultEvent] = []
        shadow = Shadow(on_fault=faults.append)
        alarms = AlarmTracker(sim_clock=shadow.sim_time_at)
        shadow._alarms = alarms
        await shadow.apply_batch([change("PR1", "FaultCount", 5, baseline=True), change("PR1", "AlarmId", 1, baseline=True), change(None, "SimTimeMin", 9.0)])
        assert alarms.rows()
        shadow.reset()
        assert shadow.machine_ids() == []
        assert alarms.rows() == []
        assert shadow.sim_time_at() == 0.0
        # A smaller FaultCount in the new run is history, not a fault.
        await shadow.apply_batch([change("PR1", "FaultCount", 1, baseline=True)])
        assert faults == []


class TestAgainstTheRealPlant:
    """The real address space and engine behind the real protocol. Only this test file may touch `werkhalle.plant`."""

    @pytest.fixture
    async def real(self):
        plant_server = pytest.importorskip("werkhalle.plant.server")
        plant_clock = pytest.importorskip("werkhalle.plant.clock")
        simcore = pytest.importorskip("werkhalle.simcore")
        engine = simcore.SimEngine(seed=6, stochastic=True, random_failures=False, run_id="r-echt")
        # 160 sim minutes per real second: the protocol stays real, the test stays short.
        clock = plant_clock.SimClock(factor=480.0, wall=lambda: time.monotonic() * 20.0, max_step_s=1e9)
        async with plant_server.PlantServer(engine, clock, free_port()) as server:
            yield server

    @staticmethod
    async def run_commands(client: PlantClient, shadow: Shadow, queues: list[list[tuple[str, dict]]], lots_total: int) -> None:
        deadline = time.monotonic() + 90.0
        while True:
            assert time.monotonic() < deadline, "lots did not get through"
            for queue in queues:
                if queue:
                    machine, command = queue[0]
                    answer = await client.start_operation(machine, command)
                    if answer["ok"]:
                        queue.pop(0)
                    else:
                        assert answer["reason"] in ("MACHINE_BUSY", "NO_MATERIAL"), (command, answer)
            ship = shadow.material.get("buffers", {}).get("SHIP", {})
            if ship.get("lots_total") == lots_total:
                return
            await asyncio.sleep(0.02)

    async def test_two_lots_sharing_a_furnace_load_end_to_end(self, real, tmp_path):
        client = PlantClient(real.endpoint)
        shadow = Shadow()
        alarms = AlarmTracker(sim_clock=shadow.sim_time_at)
        shadow.attach(client, alarms=alarms)
        db = RunDb(tmp_path / "werkhalle.db")
        trace = TraceStore(db)
        client.add_event_callback(trace.consume)
        oee = OeeTracker()
        await client.start()
        try:
            assert client.run_id == "r-echt"
            assert client.machine_ids() == list(catalog.MACHINE_ORDER)
            await wait_until(lambda: shadow.material and shadow.status("VP") == "IDLE", what="first values")

            trace.register_order("A-1", "FR-60", 60)
            trace.register_order("A-2", "FP-120", 20)
            trace.register_lot("L-00001", "A-1", "FR-60", 8)
            trace.register_lot("L-00002", "A-2", "FP-120", 4)
            age = {"op_ids": ["L-00001:age", "L-00002:age"], "kind": "age", "batch_id": "B-r1-1", "recipe": "T66",
                   "lot_ids": ["L-00001", "L-00002"]}
            queues = [
                [("PR1", {"op_ids": ["L-00001:press"], "kind": "press", "lot_id": "L-00001", "order_id": "A-1", "product_id": "FR-60", "n_billets": 8}),
                 ("OF2", age),
                 ("SAE", {"op_ids": ["L-00001:saw"], "kind": "saw", "lot_id": "L-00001"}),
                 ("VP", {"op_ids": ["L-00001:pack"], "kind": "pack", "lot_id": "L-00001"})],
                [("PR2", {"op_ids": ["L-00002:press"], "kind": "press", "lot_id": "L-00002", "order_id": "A-2", "product_id": "FP-120", "n_billets": 4}),
                 ("SAE", {"op_ids": ["L-00002:saw"], "kind": "saw", "lot_id": "L-00002"}),
                 ("VP", {"op_ids": ["L-00002:pack"], "kind": "pack", "lot_id": "L-00002"})],
            ]

            async def sample_oee():
                while True:
                    for machine in client.machine_ids():
                        oee.update(machine, shadow.sim_time_at(), shadow.counters(machine), status=shadow.status(machine) or "")
                    await asyncio.sleep(0.05)

            sampler = asyncio.create_task(sample_oee())
            try:
                await self.run_commands(client, shadow, queues, 2)
                await wait_until(lambda: client.last_event_seq >= real.engine.seq and shadow.counters("VP")["total"] > 0, what="last events")
                await asyncio.sleep(1.0)  # one more slow publish, so the counters include the end of the last op
            finally:
                sampler.cancel()
            for machine in client.machine_ids():
                oee.update(machine, shadow.sim_time_at(), shadow.counters(machine), status=shadow.status(machine) or "")

            trace.flush()
            assert client.events_total == client.last_event_seq == trace.last_seq

            # Genealogy from real events: every level balances.
            nodes = {r["id"]: r for r in db.query("SELECT id, type, qty, data FROM nodes")}
            incoming: dict[str, float] = {}
            outgoing: dict[str, float] = {}
            for edge in db.query("SELECT src, dst, qty FROM edges"):
                incoming[edge["dst"]] = incoming.get(edge["dst"], 0) + edge["qty"]
                outgoing[edge["src"]] = outgoing.get(edge["src"], 0) + edge["qty"]
            types = {n["type"] for n in nodes.values()}
            assert types == {"melt", "billet", "strand", "basket", "load", "package", "order", "lot"}
            for node_id, node in nodes.items():
                if node["type"] == "melt":
                    assert outgoing[node_id] == node["qty"], node_id
                elif node["type"] == "billet":
                    assert incoming[node_id] == 1, node_id
                elif node["type"] == "strand":
                    assert incoming[node_id] == node["qty"] == outgoing[node_id], node_id
                elif node["type"] == "basket":
                    assert incoming[node_id] == node["qty"], node_id
                    assert outgoing[node_id] == json.loads(node["data"])["pieces_good"], node_id
                elif node["type"] in ("package", "order"):
                    assert incoming[node_id] == node["qty"], node_id

            # The load links the lots only when it is the root.
            load_id = next(i for i, n in nodes.items() if n["type"] == "load")
            recall = trace.graph(load_id)
            assert recall["summary"]["orders"] == ["A-1", "A-2"]
            melt_id = "S-00001"  # the 6060 stock, which both lots draw from
            assert melt_id in recall["summary"]["melts"]
            package = next(i for i, n in nodes.items() if n["type"] == "package" and json.loads(n["data"])["lot_id"] == "L-00001")
            one_package = trace.graph(package)
            assert "A-2" not in one_package["summary"]["orders"]
            assert one_package["summary"]["loads"] == [load_id]

            # Delivered pieces agree with what the plant says it shipped, and energy reached the orders.
            ship = shadow.material["buffers"]["SHIP"]
            assert trace.delivered_pieces("A-1") + trace.delivered_pieces("A-2") == ship["pieces"]
            kg = sum(r["kg"] for r in trace.energy_by_order())
            assert kg == pytest.approx(ship["kg"], abs=0.5)
            assert all(r["energy_kwh"] > 0 for r in trace.energy_by_order())

            # OEE from the mirrored counters.
            result = oee.result("PR1", WINDOW_START)
            assert result.total == 64  # 8 billets x 8 bars
            assert result.good + result.scrap == result.total
            assert 0.0 < result.availability <= 1.0
            assert 0.0 < result.availability_net <= 1.0
            assert 0.0 < result.quality <= 1.0
            assert result.performance_uncapped <= 1.0 + 1e-6  # PLAN section 3: ideal accrues at the op's rate
            furnace = oee.result("OF2", WINDOW_START)
            # 8 billets at 4 strands per basket plus 4 billets at 4 per basket: 3 of 10 baskets.
            assert furnace.performance == pytest.approx(0.3, abs=0.02)
            data = oee.response(WINDOW_START, shadow.machines_meta())
            assert [m["id"] for m in data["machines"]] == list(catalog.MACHINE_ORDER)
            assert data["machines"][1]["name"] == catalog.MACHINES["PR1"].name
            assert data["plant"]["oee"] is not None

            # Node table, rings, state shape.
            table = client.nodes_table()
            assert {n["machine"] for n in table["nodes"]} == {None, *catalog.MACHINE_ORDER}
            assert all(n["type"] for n in table["nodes"])
            assert client.counters()["datachanges_total"] > 100
            state = {m["id"]: m for m in shadow.to_state_machines()}
            assert state["OF2"]["capacity"] == 10
            assert state["OF2"]["kind"] == "age"
            assert state["PR1"]["total"] == 64
            assert state["PR1"]["trend"]["temperature"]
            assert shadow.sim_state()["factor"] == 480.0
            json.dumps(state)
        finally:
            await client.stop()
            db.close()

    async def test_fault_chain_and_settings(self, real):
        faults: list[FaultEvent] = []
        client = PlantClient(real.endpoint)
        shadow = Shadow(on_fault=faults.append)
        alarms = AlarmTracker(sim_clock=shadow.sim_time_at)
        shadow.attach(client, alarms=alarms)
        await client.start()
        try:
            await wait_until(lambda: shadow.status("PR2") == "IDLE", what="first values")
            snapshots_before = shadow.plant_totals()["snapshot_calls"]
            delivered: list[str] = []
            client.add_datachange_callback(lambda c: delivered.append(c.value) if c.machine == "PR2" and c.name == "Status" else None)

            assert await client.inject_fault("PR2", "P-204", 100000.0) == {"ok": True, "reason": ""}
            # The call itself changes nothing here: until a Status DataChange was delivered, the shadow still says IDLE.
            if "FAULT" not in delivered:
                assert shadow.status("PR2") == "IDLE"
            assert await client.inject_fault("PR2", "P-204", 100000.0) == {"ok": False, "reason": "ALREADY_FAULTED"}
            await wait_until(lambda: faults, what="fault over DataChange")
            fault = faults[0]
            assert fault.machine == "PR2"
            assert fault.code == "P-204"
            assert fault.text == "Hauptzylinder undicht"
            assert fault.severity == 900
            assert fault.est_repair_min == 100000.0
            assert fault.baseline is False
            assert 0.0 <= fault.latency_ms < 5000.0

            # An independent read of the Status DataValue carries the same timestamp the alarm row has.
            status_node = client._client.get_node(ua.NodeId("Werkhalle.PR2.Status", await client._client.get_namespace_index("urn:werkhalle:plant")))
            data_value = await status_node.read_data_value()
            row = alarms.get(f"PR2:{fault.alarm_id}")
            assert data_value.Value.Value == "FAULT"
            assert row["source_ts"] == pytest.approx(data_value.SourceTimestamp.timestamp(), abs=1e-5)
            assert fault.source_ts == row["source_ts"]
            assert row["active"] is True

            # Ack and repair go to the plant, and the clear comes back as a DataChange.
            assert (await client.ack_alarm("PR2", fault.alarm_id))["ok"] is True
            assert alarms.mark_acked(row["id"])["acked"] is True
            assert (await client.repair("PR2"))["ok"] is True
            await wait_until(lambda: not alarms.get(row["id"])["active"], what="clear")
            assert alarms.get(row["id"])["cleared_t"] is not None
            await wait_until(lambda: shadow.status("PR2") == "IDLE", what="repair")
            assert (await client.repair("PR2")) == {"ok": False, "reason": "NOT_FAULTED"}

            # Settings are writes, and the shadow only shows what comes back.
            await client.set_time_factor(240)
            await wait_until(lambda: real.clock.factor == 240.0, what="factor applied")
            await wait_until(lambda: shadow.time_factor == 240.0, what="factor mirrored")
            await client.set_time_factor(77)
            await asyncio.sleep(0.3)
            await wait_until(lambda: shadow.time_factor == 240.0, what="invalid factor reverted")
            await client.set_random_failures(True)
            await wait_until(lambda: shadow.sim_state()["random_failures"] is True, what="failures mirrored")

            snapshot = await client.get_state_snapshot()
            assert snapshot["run_id"] == "r-echt"
            await wait_until(lambda: shadow.plant_totals()["snapshot_calls"] == snapshots_before + 1, what="SnapshotCalls")

            answer = await client.start_operation("PR1", {"op_ids": ["L-9:press"], "kind": "press", "lot_id": "L-9", "order_id": "A-9",
                                                          "product_id": "FR-60", "n_billets": 2, "run_id": "r-falsch"})
            assert answer == {"ok": False, "reason": "STALE_RUN"}
            assert client.method_calls()["calls"][0]["method"] == "StartOperation"
        finally:
            await client.stop()
