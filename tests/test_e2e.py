"""Definition of Done item 3 (PLAN section 12): real processes through the real launcher, nothing shared with the test.

The children start from a parent environment without PYTHONIOENCODING and PYTHONUTF8, so the encoding path a user's machine takes
is the one under test, and the launcher runs in a console of its own, so its Ctrl+C is a real Ctrl+C.
"""
from __future__ import annotations

import asyncio
import json
import os
import random
import re
import socket
import subprocess
import sys
import time
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import pytest
from asyncua import Client, ua

from werkhalle.__main__ import descendants, process_created

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"

pytestmark = [pytest.mark.e2e, pytest.mark.skipif(sys.platform != "win32", reason="the launcher is built for Windows")]

_CREATE_NEW_CONSOLE = 0x00000010
_STARTF_USESHOWWINDOW = 0x00000001
_SW_HIDE = 0
_CTRL_C_SCRIPT = (
    "import ctypes, sys\n"
    "k = ctypes.windll.kernel32\n"
    "k.FreeConsole()\n"
    "if not k.AttachConsole(int(sys.argv[1])): sys.exit(2)\n"
    "k.SetConsoleCtrlHandler(None, True)\n"
    "sys.exit(0 if k.GenerateConsoleCtrlEvent(0, 0) else 3)\n"
)


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def port_is_free(port: int) -> bool:
    with socket.socket() as sock:
        try:
            sock.bind(("127.0.0.1", port))
        except OSError:
            return False
    return True


def clean_parent_env() -> dict:
    """What a double-clicked start.cmd sees: no encoding variables of a developer's shell."""
    env = {k: v for k, v in os.environ.items() if k not in ("PYTHONIOENCODING", "PYTHONUTF8")}
    env["PYTHONPATH"] = str(SRC)
    return env


def hidden_console() -> dict:
    startup = subprocess.STARTUPINFO()
    startup.dwFlags |= _STARTF_USESHOWWINDOW
    startup.wShowWindow = _SW_HIDE
    return {"creationflags": _CREATE_NEW_CONSOLE, "startupinfo": startup}


def press_ctrl_c(console_pid: int) -> None:
    """A real Ctrl+C: a helper attaches to the launcher's console and raises the event there."""
    subprocess.run([sys.executable, "-c", _CTRL_C_SCRIPT, str(console_pid)], check=True, timeout=20, capture_output=True,
                   env=clean_parent_env())


class Launched:
    def __init__(self, tmp: Path, *, factor: int = 120, seed: int = 42, extra: tuple[str, ...] = (), http_port: int | None = None,
                 plant_port: int | None = None) -> None:
        self.tmp = tmp
        self.http_port = http_port or free_port()
        self.plant_port = plant_port or free_port()
        self.data_dir = tmp / "data"
        self.log_dir = tmp / "logs"
        self.output_path = tmp / "launcher.out"
        self.argv = [
            sys.executable, "-m", "werkhalle", "--http-port", str(self.http_port), "--plant-port", str(self.plant_port),
            "--time-factor", str(factor), "--seed", str(seed), "--solver-limit", "3", "--data-dir", str(self.data_dir),
            "--log-dir", str(self.log_dir), "--no-browser", "--no-random-failures", "--no-auto-supply", *extra,
        ]
        self.process: subprocess.Popen | None = None
        self._output = None
        self._tree: set[int] = set()

    def start(self) -> None:
        self._output = open(self.output_path, "wb")
        self.process = subprocess.Popen(self.argv, env=clean_parent_env(), cwd=str(ROOT), stdin=subprocess.DEVNULL, stdout=self._output,
                                        stderr=subprocess.STDOUT, **hidden_console())

    def output(self) -> str:
        return self.output_path.read_bytes().decode("utf-8", "replace")

    def pid_file(self) -> dict:
        return json.loads((self.log_dir / "werkhalle.pid").read_text(encoding="utf-8"))

    def note_tree(self) -> set[int]:
        """Remember everything below the launcher, so a later check can tell that none of it survived."""
        self._tree |= set(descendants(self.process.pid))
        return self._tree

    def survivors(self) -> list[int]:
        return [pid for pid in self._tree if process_created(pid) is not None]

    def stop(self, wait: float = 40.0) -> int:
        """Ctrl+C, as a person would. Whatever the outcome, this kills the tree, so a failing test leaves nothing behind."""
        code = None
        if self.process.poll() is None:
            try:
                press_ctrl_c(self.process.pid)
                code = self.process.wait(timeout=wait)
            except (subprocess.TimeoutExpired, subprocess.CalledProcessError):
                subprocess.run(["taskkill", "/PID", str(self.process.pid), "/T", "/F"], capture_output=True, check=False)
                self.process.wait(timeout=20)
        else:
            code = self.process.returncode
        self._cleanup()
        return code

    def _cleanup(self) -> None:
        if self._output is not None:
            self._output.close()
            self._output = None
        for pid in self._tree:
            if process_created(pid) is not None:
                subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True, check=False)


class Recorder:
    """The independent client's own record of what the plant published."""

    def __init__(self, names: dict) -> None:
        self.names = names
        self.rows: list[tuple[str, object, float]] = []

    def datachange_notification(self, node, val, data) -> None:
        stamp = data.monitored_item.Value.SourceTimestamp
        self.rows.append((self.names[node.nodeid], val, stamp.timestamp() if stamp is not None else None))

    def of(self, name: str) -> list[tuple[object, float]]:
        return [(val, ts) for row_name, val, ts in self.rows if row_name == name]


@asynccontextmanager
async def independent_client(port: int):
    """No werkhalle code in here. This is what an external system or a real machine controller would be."""
    client = Client(f"opc.tcp://127.0.0.1:{port}/werkhalle/", timeout=10)
    client.application_uri = "urn:werkhalle:e2e-test"
    await client.connect()
    try:
        yield client
    finally:
        await client.disconnect()


async def wait_until(predicate, timeout: float, what: str, step: float = 0.1):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if asyncio.iscoroutine(value):
            value = await value
        if value:
            return value
        await asyncio.sleep(step)
    raise AssertionError(f"{what} not reached within {timeout} s")


async def wait_healthy(http: httpx.AsyncClient, launched: Launched, timeout: float = 120.0) -> dict:
    async def check():
        assert launched.process.poll() is None, f"launcher exited early:\n{launched.output()}"
        try:
            health = (await http.get("/api/health")).json()
        except httpx.HTTPError:
            return None
        return health if health["ok"] and health["plan_revision"] >= 1 else None

    return await wait_until(check, timeout, "health ok with a plan", 0.25)


def pending_st200(plan: dict) -> list[dict]:
    return [a for a in plan["assignments"] if a["product_id"] == "ST-200" and a["kind"] == "press" and a["state"] == "pending"]


async def ensure_pending_st200(http: httpx.AsyncClient) -> dict:
    """Seed 42 holds several ST-200 lots, but at a high factor all of them could have started. Then one gets ordered."""
    plan = (await http.get("/api/plan")).json()
    if pending_st200(plan):
        return plan
    answer = await http.post("/api/orders", json={"product_id": "ST-200", "qty_bars": 200, "due_in_min": 4000})
    assert answer.status_code == 200, answer.text

    async def planned():
        current = (await http.get("/api/plan")).json()
        return current if pending_st200(current) else None

    return await wait_until(planned, 40, "a pending ST-200 lot in the plan", 0.25)


def read_logs(launched: Launched) -> tuple[str, str]:
    """Strict UTF-8: a log the children wrote in the console's code page would fail right here."""
    return (
        (launched.log_dir / "mes.log").read_bytes().decode("utf-8"),
        (launched.log_dir / "plant.log").read_bytes().decode("utf-8"),
    )


@pytest.mark.parametrize("factor", [120, 480])
async def test_failure_chain_end_to_end(tmp_path, factor):
    launched = Launched(tmp_path, factor=factor)
    launched.start()
    try:
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{launched.http_port}", trust_env=False, timeout=15.0) as http:
            health = await wait_healthy(http, launched)
            assert health["plant_module_loaded"] is False
            plan = await ensure_pending_st200(http)
            assert plan["revision"] >= 1
            launched.note_tree()

            async with independent_client(launched.plant_port) as opc:
                namespace = await opc.get_namespace_index("urn:werkhalle:plant")
                status_node = opc.get_node(ua.NodeId("Werkhalle.PR2.Status", namespace))
                snapshots_node = opc.get_node(ua.NodeId("Werkhalle.Plant.SnapshotCalls", namespace))
                recorder = Recorder({status_node.nodeid: "Status", snapshots_node.nodeid: "SnapshotCalls"})
                subscription = await opc.create_subscription(50, recorder)
                await subscription.subscribe_data_change([status_node, snapshots_node], queuesize=100)
                await wait_until(lambda: recorder.of("SnapshotCalls"), 5, "the first SnapshotCalls value")
                calls_before_fault = await snapshots_node.read_value()

                # The MES never sees this call: it can only learn about the fault from the DataChange.
                result = await opc.get_node(ua.NodeId("Werkhalle.PR2", namespace)).call_method(
                    ua.NodeId("Werkhalle.PR2.InjectFault", namespace), "P-204", ua.Variant(100000.0, ua.VariantType.Double),
                )
                assert json.loads(result) == {"ok": True, "reason": ""}

                async def open_proposal():
                    current = (await http.get("/api/proposals")).json()["open"]
                    return current if current is not None and current["status"] == "open" else None

                proposal = await wait_until(open_proposal, 40, "the proposal", 0.1)

                await wait_until(lambda: any(v == "FAULT" for v, _ in recorder.of("Status")), 5, "FAULT at the independent client")
                status_fault_ts = next(ts for v, ts in recorder.of("Status") if v == "FAULT")

                # The trigger: it is PR2, and its code and estimate came from the plant, over OPC UA.
                trigger = proposal["trigger"]
                assert trigger["kind"] == "fault"
                assert trigger["machine"] == "PR2"
                assert trigger["code"] == "P-204"
                assert trigger["est_repair_min"] == 100000

                # The alarm: its source timestamp is exactly that of the Status DataValue.
                state = (await http.get("/api/state")).json()
                alarm = next(a for a in state["alarms_active"] if a["machine"] == "PR2")
                assert alarm["code"] == "P-204" and alarm["severity"] == 900
                assert alarm["source_ts"] == pytest.approx(status_fault_ts, abs=1e-6)
                assert alarm["latency_ms"] is not None and 0 <= alarm["latency_ms"] < 5000

                chain = proposal["chain"]
                assert chain["injected_wall_ts"] is None, "this fault did not come through the MES"
                assert chain["source_ts"] == pytest.approx(status_fault_ts, abs=1e-6)
                assert chain["detected_wall_ts"] >= chain["source_ts"]
                assert chain["latency_ms"] == pytest.approx(alarm["latency_ms"], abs=1.0)

                # Forked only after detection, by the MES's own record and by the plant's counter.
                assert chain["forked_wall_ts"] >= chain["detected_wall_ts"]
                calls = [(v, ts) for v, ts in recorder.of("SnapshotCalls") if v > calls_before_fault]
                assert calls, "the pipeline must have called GetStateSnapshot"
                assert calls[0][1] >= chain["detected_wall_ts"] - 0.001, "a snapshot was taken before the fault was detected"
                assert chain["snapshot_seq"] > 0

                # The downtime length: the rest of the 100000 minutes, from the snapshot.
                recommended = proposal["recommended"]
                assert recommended in ("cpsat", "anneal"), proposal["summary"]
                candidate = proposal["candidates"][recommended]
                assert candidate["valid"] is True
                downtime = next(d for d in candidate["downtimes"] if d["machine"] == "PR2")
                assert downtime["reason"] == "P-204"
                assert abs(downtime["end"] - (trigger["t"] + 100000)) <= 5
                assert not any(
                    a["machine"] == "PR2" and a["start"] < downtime["end"] and not a["frozen"] for a in candidate["assignments"]
                ), "nothing is planned on PR2 during the downtime"

                # Accept: the plan names this proposal and is exactly one revision on.
                revision_before = (await http.get("/api/plan")).json()["revision"]
                assert proposal["based_on_revision"] == revision_before
                accepted = await http.post(f"/api/proposals/{proposal['id']}/accept", json={})
                assert accepted.status_code == 200, accepted.text
                assert accepted.json()["revision"] == revision_before + 1
                plan = (await http.get("/api/plan")).json()
                assert plan["revision"] == revision_before + 1
                assert plan["source"] == "proposal"
                assert plan["source_proposal_id"] == proposal["id"]
                assert any(d["machine"] == "PR2" and d["reason"] == "P-204" for d in plan["downtimes"])

                health = (await http.get("/api/health")).json()
                assert health["plant_module_loaded"] is False
                assert health["pool_plant_module_loaded"] is False
                assert health["connected"] is True

                # The plant keeps working on the new plan, and PR2 is still down only because the plant says so.
                events_before = (await http.get("/api/state")).json()["counters"]["last_event_seq"]

                async def advanced():
                    return (await http.get("/api/state")).json()["counters"]["last_event_seq"] > events_before

                await wait_until(advanced, 60, "events after the accept", 0.25)
                state = (await http.get("/api/state")).json()
                assert next(m for m in state["machines"] if m["id"] == "PR2")["status"] == "FAULT"
                assert state["stalled"] is False
                launched.note_tree()
    finally:
        code = launched.stop()

    assert code == 0, launched.output()
    assert not launched.survivors()
    assert port_is_free(launched.plant_port) and port_is_free(launched.http_port)
    assert not (launched.log_dir / "werkhalle.pid").exists()
    mes_log, plant_log = read_logs(launched)
    assert "Traceback" not in mes_log, mes_log
    assert "Traceback" not in plant_log, plant_log
    # Real umlauts through the real path, with no help from the parent's environment.
    assert "Störung" in mes_log and "Anlage läuft" in plant_log
    output = launched.output()
    assert "Leitstand bereit" in output and "beendet" in output


async def test_no_descendant_survives_a_ctrl_c(tmp_path):
    launched = Launched(tmp_path)
    launched.start()
    try:
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{launched.http_port}", trust_env=False, timeout=15.0) as http:
            await wait_healthy(http, launched)
        tree = launched.note_tree()
        # Plant and MES, each a venv redirector with the real interpreter below it, and the pool workers below the MES.
        assert len(tree) >= 6, tree
    finally:
        code = launched.stop()
    assert code == 0, launched.output()
    assert launched.survivors() == []
    assert port_is_free(launched.plant_port) and port_is_free(launched.http_port)


async def test_killing_the_launcher_alone_takes_everything_with_it(tmp_path):
    """The Job Object's job: no Ctrl+C, no cleanup code, just the handle closing."""
    launched = Launched(tmp_path)
    launched.start()
    try:
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{launched.http_port}", trust_env=False, timeout=15.0) as http:
            await wait_healthy(http, launched)
        tree = launched.note_tree()
        assert tree
        subprocess.run(["taskkill", "/PID", str(launched.process.pid), "/F"], capture_output=True, check=False)
        launched.process.wait(timeout=20)
        await wait_until(lambda: not launched.survivors(), 20, "the job to kill everything")
    finally:
        launched.stop()
    assert port_is_free(launched.plant_port) and port_is_free(launched.http_port)


async def test_a_dying_child_stops_the_other_with_a_clear_message(tmp_path):
    launched = Launched(tmp_path)
    launched.start()
    try:
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{launched.http_port}", trust_env=False, timeout=15.0) as http:
            await wait_healthy(http, launched)
        launched.note_tree()
        plant_pid = launched.pid_file()["plant"]["pid"]
        subprocess.run(["taskkill", "/PID", str(plant_pid), "/T", "/F"], capture_output=True, check=False)
        code = await asyncio.to_thread(launched.process.wait, 40)
    finally:
        launched.stop()
    assert code == 1
    output = launched.output()
    assert "Die Anlage" in output and "unerwartet beendet" in output
    assert launched.survivors() == []
    assert port_is_free(launched.http_port)


def blocked_port_with_a_free_neighbour() -> socket.socket:
    """A listener outside the ephemeral range. A port the OS is about to hand out as a source port would connect to itself."""
    for port in random.sample(range(20000, 40000), 200):
        if not port_is_free(port + 1):
            continue
        blocker = socket.socket()
        try:
            blocker.bind(("127.0.0.1", port))
        except OSError:
            blocker.close()
            continue
        blocker.listen()
        return blocker
    raise AssertionError("no free port pair found")


async def test_taken_ports_move_on_and_say_so(tmp_path):
    with blocked_port_with_a_free_neighbour() as blocker:
        taken = blocker.getsockname()[1]
        launched = Launched(tmp_path, http_port=taken)
        launched.start()
        try:
            def moved():
                match = re.search(rf"Port {taken} \(HTTP\) ist belegt, nehme (\d+)", launched.output())
                return int(match.group(1)) if match else None

            actual = await wait_until(moved, 30, "the port message", 0.1)
            assert actual == taken + 1
            launched.http_port = actual
            async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{actual}", trust_env=False, timeout=15.0) as http:
                await wait_healthy(http, launched)
            launched.note_tree()
        finally:
            code = launched.stop()
    assert code == 0, launched.output()
    assert launched.survivors() == []


def test_start_cmd_bytes():
    data = (ROOT / "start.cmd").read_bytes()
    assert all(byte < 128 for byte in data), "start.cmd must be ASCII"
    assert data.count(b"\n") == data.count(b"\r\n") and data.count(b"\n") > 10, "start.cmd must use CRLF everywhere"
    text = data.decode("ascii")
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.lower().startswith(("if ", "for ")):
            assert not stripped.endswith("("), f"no parenthesised blocks: {line}"
        assert stripped != ")", f"no parenthesised blocks: {line}"
    assert 'cd /d "%~dp0"' in text
    assert 'set "PYTHONPATH=%CD%\\src"' in text
    assert ".venv\\.deps-ok" in text and "py -3.13" in text and "pause" in text and "exit /b 1" in text
    attributes = (ROOT / ".gitattributes").read_text(encoding="utf-8")
    assert "*.cmd text eol=crlf" in attributes


async def test_start_cmd_end_to_end(tmp_path):
    """On the existing venv, from this project's path, which holds a space. The marker is current, so pip stays out of it."""
    assert " " in str(ROOT), "this checkout's path has no space, so this test proves less than it claims"
    http_port, plant_port = free_port(), free_port()
    output_path = tmp_path / "start.out"
    env = clean_parent_env()
    env.pop("PYTHONPATH", None)
    arguments = (f'--no-browser --http-port {http_port} --plant-port {plant_port} --seed 3 --solver-limit 2 --no-auto-supply '
                 f'--no-random-failures --data-dir "{tmp_path / "data"}" --log-dir "{tmp_path / "logs"}"')
    # cmd strips the outer quotes of what follows /c, so the whole line needs a pair of its own, as in a shortcut's target.
    command = f'cmd.exe /c ""{ROOT / "start.cmd"}" {arguments}"'
    with open(output_path, "wb") as output:
        process = subprocess.Popen(
            command, env=env, cwd=str(tmp_path), stdin=subprocess.DEVNULL, stdout=output, stderr=subprocess.STDOUT, **hidden_console(),
        )
    try:
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{http_port}", trust_env=False, timeout=15.0) as http:
            async def healthy():
                assert process.poll() is None, output_path.read_bytes().decode("utf-8", "replace")
                try:
                    health = (await http.get("/api/health")).json()
                except httpx.HTTPError:
                    return None
                return health if health["ok"] and health["plan_revision"] >= 1 else None

            health = await wait_until(healthy, 180, "start.cmd bringing up the system", 0.25)
            assert health["plant_module_loaded"] is False
            state = (await http.get("/api/state")).json()
            assert state["connected"] is True and state["run_id"]
        # The launcher polls health on its own clock, so it announces the plan a moment after this test can see it.
        await wait_until(lambda: "Leitstand bereit" in output_path.read_bytes().decode("utf-8", "replace"),
                         20, "the launcher announcing the Leitstand")
        tree = set(descendants(process.pid))
        assert len(tree) >= 6, tree
        press_ctrl_c(process.pid)
        # With stdin at NUL, cmd's "Batch-Vorgang abbrechen" prompt answers itself.
        process.wait(timeout=40)
    finally:
        if process.poll() is None:
            subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"], capture_output=True, check=False)
            process.wait(timeout=20)
    await wait_until(lambda: not [pid for pid in tree if process_created(pid) is not None], 20, "every descendant gone")
    assert port_is_free(http_port) and port_is_free(plant_port)
    output = output_path.read_bytes().decode("utf-8", "replace")
    assert "Leitstand bereit" in output and "beendet" in output, output


class DirectSystem:
    """Plant and MES started directly, without the launcher, so that either can be killed and restarted alone."""

    def __init__(self, tmp: Path, factor: int = 240) -> None:
        self.tmp = tmp
        self.plant_port = free_port()
        self.http_port = free_port()
        self.factor = factor
        self.plant: subprocess.Popen | None = None
        self.mes: subprocess.Popen | None = None
        self._logs: list = []
        self._tree: set[int] = set()

    def _env(self) -> dict:
        env = clean_parent_env()
        env["PYTHONUTF8"] = "1"
        return env

    def _spawn(self, name: str, argv: list[str]) -> subprocess.Popen:
        log = open(self.tmp / f"{name}.log", "ab")
        self._logs.append(log)
        return subprocess.Popen(argv, env=self._env(), stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT)

    def start_plant(self) -> None:
        self.plant = self._spawn("plant", [sys.executable, "-m", "werkhalle.plant", "--port", str(self.plant_port), "--seed", "11",
                                           "--time-factor", str(self.factor), "--no-random-failures"])

    def start_mes(self) -> None:
        self.mes = self._spawn("mes", [sys.executable, "-m", "werkhalle.mes", "--plant-url", f"opc.tcp://127.0.0.1:{self.plant_port}/werkhalle/",
                                       "--http-port", str(self.http_port), "--data-dir", str(self.tmp / "data"), "--seed", "11",
                                       "--solver-limit", "2", "--no-auto-supply"])

    def note_trees(self) -> None:
        for process in (self.plant, self.mes):
            if process is not None and process.poll() is None:
                self._tree |= set(descendants(process.pid))

    def kill(self, process: subprocess.Popen | None) -> None:
        if process is None:
            return
        self.note_trees()
        if process.poll() is None:
            subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"], capture_output=True, check=False)
        process.wait(timeout=20)

    def stop(self) -> None:
        self.kill(self.mes)
        self.kill(self.plant)
        for log in self._logs:
            log.close()
        for pid in self._tree:
            if process_created(pid) is not None:
                subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True, check=False)


async def planned_system(http: httpx.AsyncClient, system: DirectSystem, minimum: int = 1, timeout: float = 120.0) -> dict:
    async def check():
        assert system.mes.poll() is None, (system.tmp / "mes.log").read_bytes().decode("utf-8", "replace")
        try:
            health = (await http.get("/api/health")).json()
        except httpx.HTTPError:
            return None
        return health if health["ok"] and health["plan_revision"] >= minimum else None

    return await wait_until(check, timeout, f"plan revision {minimum}", 0.25)


async def test_plant_restart_is_a_hard_reset(tmp_path):
    """A new RunId means a new world: nothing computed for the old run may land in the new one."""
    system = DirectSystem(tmp_path)
    system.start_plant()
    system.start_mes()
    try:
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{system.http_port}", trust_env=False, timeout=15.0) as http:
            first = await planned_system(http, system)
            old_run = first["run_id"]
            answer = await http.post("/api/orders", json={"product_id": "KK-80", "qty_bars": 200, "due_in_min": 2000})
            assert answer.status_code == 200, answer.text
            extra_lots = {lot["id"] for lot in answer.json()["order"]["lots"]}
            system.note_trees()

            system.kill(system.plant)

            async def disconnected():
                state = (await http.get("/api/state")).json()
                return state if state["connected"] is False else None

            await wait_until(disconnected, 15, "the connection loss", 0.05)
            assert (await http.get("/api/health")).json()["ok"] is False, "ok means connected"

            system.start_plant()

            async def new_run():
                health = (await http.get("/api/health")).json()
                return health if health["ok"] and health["run_id"] != old_run else None

            await wait_until(new_run, 60, "the new run", 0.1)
            health = await planned_system(http, system, 1, 60)
            assert health["plan_revision"] == 1, "the revision count starts over with the run"
            plan = (await http.get("/api/plan")).json()
            assert plan["source"] == "startup"

            orders = (await http.get("/api/orders")).json()["orders"]
            lots = {lot["id"] for order in orders for lot in order["lots"]}
            assert len(orders) == 11, "a fresh seed, without the extra order of the old run"
            assert len(lots) == 22 and not (lots & extra_lots)
            assert all(o["delivered_pieces"] == 0 and o["created_min"] > 0 or o["delivered_pieces"] == 0 for o in orders)

            state = (await http.get("/api/state")).json()
            assert state["run_id"] != old_run and state["stalled"] is False
            log = (await http.get("/api/log?limit=50")).json()["entries"]
            assert any("neu gestartet" in e["text"] for e in log), log[:5]
            assert (await http.get("/api/proposals")).json() == {"open": None, "history": []}

            async def working_again():
                current = (await http.get("/api/state")).json()
                return any(m["status"] in ("RUN", "SETUP") for m in current["machines"])

            await wait_until(working_again, 30, "the new run working", 0.25)
            system.note_trees()
    finally:
        system.stop()
    log = (tmp_path / "mes.log").read_bytes().decode("utf-8")
    assert "Traceback" not in log, log
    data = list((tmp_path / "data").glob("run-*"))
    assert len(data) == 1, f"older run files are removed: {data}"


async def test_mes_restart_resumes_the_same_run(tmp_path):
    """The plant keeps running. The new MES finds its order book in the run's database and does not start anything twice."""
    system = DirectSystem(tmp_path)
    system.start_plant()
    system.start_mes()
    try:
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{system.http_port}", trust_env=False, timeout=15.0) as http:
            first = await planned_system(http, system)
            run_id = first["run_id"]

            async def work_under_way():
                return (await http.get("/api/state")).json()["counters"]["events_total"] > 15

            await wait_until(work_under_way, 60, "work under way", 0.25)
            answer = await http.post("/api/orders", json={"product_id": "CB-90", "qty_bars": 40, "due_in_min": 3000})
            assert answer.status_code == 200, answer.text
            await asyncio.sleep(0.5)  # the database's commit interval
            order_ids = {o["id"] for o in (await http.get("/api/orders")).json()["orders"]}
            system.note_trees()

            system.kill(system.mes)
            await asyncio.sleep(0.5)
            system.start_mes()

            health = await planned_system(http, system, 1, 90)
            assert health["run_id"] == run_id
            orders = (await http.get("/api/orders")).json()["orders"]
            assert {o["id"] for o in orders} == order_ids, "the same book, not a new seed"

            async def more_commands():
                calls = (await http.get("/api/opcua/methods")).json()["calls"]
                return [c for c in calls if c["method"] == "StartOperation"]

            calls = await wait_until(more_commands, 60, "commands from the new MES", 0.25)
            refused = [c for c in calls if "ALREADY_STARTED" in c["result"]]
            assert not refused, f"something that ran before the restart was commanded again: {refused[:3]}"
            system.note_trees()
    finally:
        system.stop()
    log = (tmp_path / "mes.log").read_bytes().decode("utf-8")
    assert "Traceback" not in log, log
