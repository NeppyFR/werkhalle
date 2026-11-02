"""OPC UA server around SimEngine: the address space of PLAN section 4 and the driver loop of section 2.

Everything that touches the engine runs on the event loop and has no suspension point: method handlers are `async def`
(asyncua runs sync ones on a worker thread, where they would race the engine) and contain no `await`, and the fast block
is a plain function. A DataChange therefore always shows a state the engine really was in.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import secrets
import sys
import time
from collections.abc import Callable
from datetime import datetime, timezone

from asyncua import Server, ua

from ..domain import catalog as cat
from ..simcore import SimEngine
from .clock import ALLOWED_FACTORS, SimClock

log = logging.getLogger("werkhalle.plant")

NAMESPACE = "urn:werkhalle:plant"
TICK_S = 0.05
SLOW_PERIOD_S = 0.5
MAX_EVENTS_PER_CALL = 5000
DEFAULT_EVENTS_PER_CALL = 500

_STRING = ua.VariantType.String
_DOUBLE = ua.VariantType.Double
_UINT64 = ua.VariantType.UInt64
_UINT16 = ua.VariantType.UInt16
_UINT32 = ua.VariantType.UInt32
_BOOL = ua.VariantType.Boolean

# name -> (variant type, deadband). A write within the deadband of the last written value is skipped.
_MACHINE_VARIABLES: dict[str, tuple[ua.VariantType, float]] = {
    "Name": (_STRING, 0.0),
    "Kind": (_STRING, 0.0),
    "Unit": (_STRING, 0.0),
    "Status": (_STRING, 0.0),
    "Temperature": (_DOUBLE, 0.05),
    "TemperatureSetpoint": (_DOUBLE, 0.0),
    "PowerKW": (_DOUBLE, 0.0),
    "EnergyKWh": (_DOUBLE, 0.01),
    "TotalCount": (_UINT64, 0.0),
    "GoodCount": (_UINT64, 0.0),
    "ScrapCount": (_UINT64, 0.0),
    "RunMinutes": (_DOUBLE, 0.01),
    "SetupMinutes": (_DOUBLE, 0.01),
    "FaultMinutes": (_DOUBLE, 0.01),
    "IdleMinutes": (_DOUBLE, 0.01),
    "IdealMinutes": (_DOUBLE, 0.01),
    "SetupClass": (_STRING, 0.0),
    "CurrentOperation": (_STRING, 0.0),
    "Progress": (_DOUBLE, 0.01),
    "AlarmId": (_UINT64, 0.0),
    "AlarmActive": (_BOOL, 0.0),
    "AlarmCode": (_STRING, 0.0),
    "AlarmText": (_STRING, 0.0),
    "AlarmSeverity": (_UINT16, 0.0),
    "EstimatedRepairMin": (_DOUBLE, 0.0),
    "FaultCount": (_UINT64, 0.0),
}

# Written in this order with one timestamp and no suspension. Status goes last, so a client that sees a new Status has
# already been sent everything that explains it. AlarmId and AlarmActive close the alarm group, so they can serve as
# its commit: when they arrive, code, text and severity of that alarm are already there.
FAST_FIELDS = (
    "AlarmCode",
    "AlarmText",
    "AlarmSeverity",
    "AlarmId",
    "AlarmActive",
    "EstimatedRepairMin",
    "FaultCount",
    "CurrentOperation",
    "SetupClass",
    "Progress",
    "Status",
)
SLOW_FIELDS = (
    "Temperature",
    "TemperatureSetpoint",
    "PowerKW",
    "EnergyKWh",
    "TotalCount",
    "GoodCount",
    "ScrapCount",
    "RunMinutes",
    "SetupMinutes",
    "FaultMinutes",
    "IdleMinutes",
    "IdealMinutes",
)


def _argument(name: str, vtype: ua.VariantType) -> ua.Argument:
    argument = ua.Argument()
    argument.Name = name
    argument.DataType = ua.NodeId(vtype.value)
    argument.ValueRank = -1
    return argument


def _reply(result: dict) -> list[ua.Variant]:
    return [ua.Variant(json.dumps(result, ensure_ascii=False), _STRING)]


class _Var:
    __slots__ = ("nodeid", "vtype", "deadband", "last")

    def __init__(self, nodeid: ua.NodeId, vtype: ua.VariantType, deadband: float, last):
        self.nodeid = nodeid
        self.vtype = vtype
        self.deadband = deadband
        self.last = last


class PlantServer:
    """Build with an engine and a clock, then `await start()` and `await run()`, or use `async with`."""

    def __init__(self, engine: SimEngine, clock: SimClock, port: int, tick_s: float = TICK_S):
        self.engine = engine
        self.clock = clock
        self.port = port
        self.endpoint = f"opc.tcp://127.0.0.1:{port}/werkhalle/"
        self.tick_s = tick_s
        self.server = Server()
        self.snapshot_calls = 0
        self.heartbeat = 0
        self.method_handlers: dict[str, Callable] = {}
        self.driver_task: asyncio.Task | None = None
        self._aspace = None
        self._plant_vars: dict[str, _Var] = {}
        self._machine_vars: dict[str, dict[str, _Var]] = {}
        self._material_key = ""
        self._next_slow = 0.0

    # ------------------------------------------------------------------ lifecycle

    async def start(self) -> None:
        server = self.server
        await server.init()
        server.set_endpoint(self.endpoint)
        server.set_server_name("Werkhalle Anlage")
        server.set_security_policy([ua.SecurityPolicyType.NoSecurity])
        self._aspace = server.iserver.aspace
        await self._build_address_space()
        self._advance()
        self._publish_fast()
        self._publish_slow()
        await server.start()

    async def stop(self) -> None:
        if self.driver_task is not None:
            self.driver_task.cancel()
            try:
                await self.driver_task
            except (asyncio.CancelledError, Exception):
                pass
            self.driver_task = None
        await self.server.stop()

    async def __aenter__(self) -> "PlantServer":
        await self.start()
        self.driver_task = asyncio.create_task(self.run(), name="plant-driver")
        return self

    async def __aexit__(self, *exc_info) -> None:
        await self.stop()

    async def run(self) -> None:
        """The driver. Sleeps one short tick at a time, never until the next event. An exception here ends the process."""
        while True:
            self.tick()
            await asyncio.sleep(self.tick_s)

    def tick(self) -> None:
        self._poll_settings()
        self._advance()
        self._publish_fast()
        if time.monotonic() >= self._next_slow:
            self._next_slow = time.monotonic() + SLOW_PERIOD_S
            self._publish_slow()

    def _advance(self) -> None:
        self.engine.run_until(self.clock.now())

    # ------------------------------------------------------------------ address space

    async def _build_address_space(self) -> None:
        server = self.server
        idx = await server.register_namespace(NAMESPACE)

        def nid(path: str) -> ua.NodeId:
            return ua.NodeId(f"Werkhalle.{path}", idx)

        def qn(name: str) -> ua.QualifiedName:
            return ua.QualifiedName(name, idx)

        engine = self.engine
        root = await server.nodes.objects.add_folder(ua.NodeId("Werkhalle", idx), qn("Werkhalle"))
        plant = await root.add_object(nid("Plant"), qn("Plant"))
        machines = await root.add_folder(nid("Machines"), qn("Machines"))

        async def add_plant_var(name: str, value, vtype: ua.VariantType, deadband: float = 0.0, writable: bool = False):
            node = await plant.add_variable(nid(f"Plant.{name}"), qn(name), value, vtype)
            if writable:
                await node.set_writable()
            self._plant_vars[name] = _Var(node.nodeid, vtype, deadband, value)

        await add_plant_var("RunId", engine.run_id, _STRING)
        await add_plant_var("SimTimeMin", engine.t, _DOUBLE)
        await add_plant_var("TimeFactor", self.clock.factor, _DOUBLE, writable=True)
        await add_plant_var("RandomFailures", engine.random_failures, _BOOL, writable=True)
        await add_plant_var("Heartbeat", 0, _UINT64)
        await add_plant_var("EventSeq", engine.seq, _UINT64)
        await add_plant_var("Material", "", _STRING)
        await add_plant_var("TotalPowerKW", 0.0, _DOUBLE, deadband=0.05)
        await add_plant_var("TotalEnergyKWh", 0.0, _DOUBLE, deadband=0.01)
        await add_plant_var("SnapshotCalls", 0, _UINT64)

        await self._add_method(
            plant, nid("Plant.GetEvents"), qn("GetEvents"), "Plant.GetEvents", self._handle_get_events,
            [_argument("afterSeq", _UINT64), _argument("maxCount", _UINT32)],
        )
        await self._add_method(
            plant, nid("Plant.GetStateSnapshot"), qn("GetStateSnapshot"), "Plant.GetStateSnapshot",
            self._handle_get_state_snapshot, [],
        )

        for machine_id in cat.MACHINE_ORDER:
            obj = await machines.add_object(nid(machine_id), qn(machine_id))
            view = engine.machine_view(machine_id)
            variables: dict[str, _Var] = {}
            for name, (vtype, deadband) in _MACHINE_VARIABLES.items():
                node = await obj.add_variable(nid(f"{machine_id}.{name}"), qn(name), view[name], vtype)
                variables[name] = _Var(node.nodeid, vtype, deadband, view[name])
            self._machine_vars[machine_id] = variables
            await self._add_machine_methods(obj, machine_id, nid, qn)

    async def _add_method(self, parent, nodeid, qname, key: str, handler: Callable, inputs: list[ua.Argument]) -> None:
        self.method_handlers[key] = handler
        await parent.add_method(nodeid, qname, handler, inputs, [_argument("result", _STRING)])

    async def _add_machine_methods(self, obj, machine_id: str, nid, qn) -> None:
        async def start_operation(parent, *args):
            self._advance()
            cmd: dict | None = None
            if args and isinstance(args[0].Value, str):
                try:
                    decoded = json.loads(args[0].Value)
                except ValueError:
                    decoded = None
                if isinstance(decoded, dict):
                    cmd = decoded
            if cmd is None:
                result = {"ok": False, "reason": "BAD_COMMAND"}
            else:
                # The node addresses the machine; a machine key inside the JSON cannot redirect the command.
                cmd["machine"] = machine_id
                result = self.engine.start_operation(cmd)
            self._publish_fast()
            return _reply(result)

        async def inject_fault(parent, *args):
            self._advance()
            code = args[0].Value if len(args) > 0 else ""
            repair_min = args[1].Value if len(args) > 1 else 0.0
            if not isinstance(code, str) or isinstance(repair_min, bool) or not isinstance(repair_min, (int, float)):
                result = {"ok": False, "reason": "BAD_COMMAND"}
            else:
                result = self.engine.inject_fault(machine_id, code, float(repair_min))
            self._publish_fast()
            return _reply(result)

        async def repair(parent, *args):
            self._advance()
            result = self.engine.repair(machine_id)
            self._publish_fast()
            return _reply(result)

        async def ack_alarm(parent, *args):
            self._advance()
            alarm_id = args[0].Value if args else 0
            result = self.engine.ack_alarm(machine_id, alarm_id if isinstance(alarm_id, int) else 0)
            self._publish_fast()
            return _reply(result)

        for name, handler, inputs in (
            ("StartOperation", start_operation, [_argument("json", _STRING)]),
            ("InjectFault", inject_fault, [_argument("code", _STRING), _argument("repairMin", _DOUBLE)]),
            ("Repair", repair, []),
            ("AckAlarm", ack_alarm, [_argument("alarmId", _UINT64)]),
        ):
            await self._add_method(obj, nid(f"{machine_id}.{name}"), qn(name), f"{machine_id}.{name}", handler, inputs)

    # ------------------------------------------------------------------ plant-level methods

    async def _handle_get_events(self, parent, *args):
        self._advance()
        after = args[0].Value if len(args) > 0 else 0
        count = args[1].Value if len(args) > 1 else DEFAULT_EVENTS_PER_CALL
        if not isinstance(after, int) or not isinstance(count, int):
            after, count = 0, DEFAULT_EVENTS_PER_CALL
        count = min(count if count > 0 else DEFAULT_EVENTS_PER_CALL, MAX_EVENTS_PER_CALL)
        result = self.engine.events_after(after, count)
        self._publish_fast()
        return _reply(result)

    async def _handle_get_state_snapshot(self, parent, *args):
        self._advance()
        snapshot = self.engine.export_snapshot()
        self.snapshot_calls += 1
        self._publish_fast()
        self._put(self._plant_vars["SnapshotCalls"], self.snapshot_calls, datetime.now(timezone.utc))
        return _reply(snapshot)

    # ------------------------------------------------------------------ publishing

    def _write_now(self, nodeid: ua.NodeId, value: ua.DataValue) -> None:
        """Run asyncua's coroutine write to completion on the spot.

        The chain (attribute store, datachange callback, subscription queue) has awaits but no real suspension point.
        Driving it here makes that a checked fact instead of an assumption: if a future asyncua really suspended, this
        raises, instead of letting other coroutines see a half-written block.
        """
        write = self._aspace.write_attribute_value(nodeid, ua.AttributeIds.Value, value)
        try:
            write.send(None)
        except StopIteration:
            return
        write.close()
        raise RuntimeError(f"asyncua suspended while writing {nodeid}")

    def _put(self, var: _Var, value, stamp: datetime) -> None:
        if value == var.last:
            return
        # Exact zero always goes out, so a finished Progress or a cleared value never sticks at its last step.
        if var.deadband and value != 0.0 and abs(value - var.last) < var.deadband:
            return
        var.last = value
        self._write_now(
            var.nodeid, ua.DataValue(Value=ua.Variant(value, var.vtype), SourceTimestamp=stamp, ServerTimestamp=stamp)
        )

    def _publish_fast(self) -> None:
        engine = self.engine
        stamp = datetime.now(timezone.utc)
        for machine_id, variables in self._machine_vars.items():
            view = engine.machine_view(machine_id)
            for name in FAST_FIELDS:
                self._put(variables[name], view[name], stamp)
        self._put(self._plant_vars["EventSeq"], engine.seq, stamp)

    def _publish_slow(self) -> None:
        engine = self.engine
        stamp = datetime.now(timezone.utc)
        plant = self._plant_vars
        self.heartbeat += 1
        self._put(plant["Heartbeat"], self.heartbeat, stamp)
        self._put(plant["SimTimeMin"], engine.t, stamp)
        for machine_id, variables in self._machine_vars.items():
            view = engine.machine_view(machine_id)
            for name in SLOW_FIELDS:
                self._put(variables[name], view[name], stamp)
        self._put(plant["TotalPowerKW"], engine.total_power_kw(), stamp)
        self._put(plant["TotalEnergyKWh"], engine.total_energy_kwh(), stamp)
        material = engine.material_view()
        t = material.pop("t")
        key = json.dumps(material, ensure_ascii=False)
        if key != self._material_key:
            self._material_key = key
            material["t"] = t
            self._put(plant["Material"], json.dumps(material, ensure_ascii=False), stamp)

    def _poll_settings(self) -> None:
        """Clients write TimeFactor and RandomFailures straight into the nodes; apply or revert them here."""
        factor_var = self._plant_vars["TimeFactor"]
        written = self._aspace.read_attribute_value(factor_var.nodeid, ua.AttributeIds.Value).Value.Value
        if written != self.clock.factor:
            if self.clock.set_factor(written):
                log.info("Zeitfaktor jetzt %s", self.clock.factor)
                factor_var.last = self.clock.factor
            else:
                log.warning("Ungültiger Zeitfaktor %r verworfen, erlaubt sind %s", written, ALLOWED_FACTORS)
                factor_var.last = written
                self._put(factor_var, self.clock.factor, datetime.now(timezone.utc))
        failures_var = self._plant_vars["RandomFailures"]
        written = self._aspace.read_attribute_value(failures_var.nodeid, ua.AttributeIds.Value).Value.Value
        if written != failures_var.last:
            failures_var.last = written
            self.engine.set_random_failures(bool(written))
            log.info("Zufallsstörungen %s", "an" if written else "aus")


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="python -m werkhalle.plant", description="Simulierte Anlage als OPC-UA-Server")
    parser.add_argument("--port", type=int, default=4840)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--time-factor", type=float, default=120.0, choices=ALLOWED_FACTORS)
    parser.add_argument("--no-random-failures", action="store_true")
    return parser.parse_args(argv)


def _configure_logging() -> None:
    logging.basicConfig(stream=sys.stderr, level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    # asyncua logs every session and method call at INFO, and warns at every start that security None has no signing.
    logging.getLogger("asyncua").setLevel(logging.WARNING)
    logging.getLogger("asyncua.server.server").setLevel(logging.ERROR)


def new_run_id() -> str:
    """Random rather than seeded: a restarted plant with the same seed must still read as a new run to the MES."""
    return f"r-{secrets.token_hex(3)}"


async def _serve(args: argparse.Namespace) -> None:
    run_id = new_run_id()
    engine = SimEngine(seed=args.seed, stochastic=True, random_failures=not args.no_random_failures, run_id=run_id)
    plant = PlantServer(engine, SimClock(factor=args.time_factor), port=args.port)
    try:
        await plant.start()
        log.info("Anlage läuft: %s RunId=%s Seed=%s Faktor=%s", plant.endpoint, run_id, args.seed, args.time_factor)
        await plant.run()
    except Exception:
        # asyncio.run would now cancel and await every asyncua task; the exit status must not depend on them finishing.
        log.exception("Anlagensimulation abgebrochen")
        logging.shutdown()
        os._exit(1)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    _configure_logging()
    try:
        asyncio.run(_serve(args))
    except KeyboardInterrupt:
        pass
    return 0
