"""`python -m werkhalle.plant` as a real process: flags, logging, exit status, clean port."""
from __future__ import annotations

import asyncio
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest
from asyncua import Client, ua

from werkhalle.plant.server import new_run_id

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"


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


def stop(proc: subprocess.Popen) -> None:
    """The venv python.exe is a launcher with the real interpreter as its child, so the whole tree has to go."""
    if proc.poll() is None:
        if sys.platform == "win32":
            subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True, check=False)
        else:
            proc.kill()
    proc.wait(timeout=15)


def port_is_free(port: int) -> bool:
    with socket.socket() as sock:
        try:
            sock.bind(("127.0.0.1", port))
        except OSError:
            return False
    return True


async def connect(url: str, proc: subprocess.Popen, timeout: float = 60.0) -> Client:
    deadline = time.monotonic() + timeout
    while True:
        assert proc.poll() is None, f"plant exited early with {proc.returncode}"
        client = Client(url, timeout=10, watchdog_intervall=5)
        try:
            await client.connect()
            return client
        except (OSError, asyncio.TimeoutError, TimeoutError):
            assert time.monotonic() < deadline, "plant did not come up"
            await asyncio.sleep(0.2)


async def test_module_entry_point_serves_and_honours_flags(tmp_path):
    port = free_port()
    log_path = tmp_path / "plant.log"
    with open(log_path, "w", encoding="utf-8") as log:
        proc = subprocess.Popen(
            [sys.executable, "-m", "werkhalle.plant", "--port", str(port), "--seed", "9", "--time-factor", "240",
             "--no-random-failures"],
            env=child_env(), stdout=subprocess.DEVNULL, stderr=log,
        )
    try:
        client = await connect(f"opc.tcp://127.0.0.1:{port}/werkhalle/", proc)
        try:
            ns = await client.get_namespace_index("urn:werkhalle:plant")
            run_id = await client.get_node(ua.NodeId("Werkhalle.Plant.RunId", ns)).read_value()
            assert run_id.startswith("r-") and len(run_id) > 2
            factor = await client.get_node(ua.NodeId("Werkhalle.Plant.TimeFactor", ns)).read_value()
            assert factor == 240.0
            assert await client.get_node(ua.NodeId("Werkhalle.Plant.RandomFailures", ns)).read_value() is False
            status = await client.get_node(ua.NodeId("Werkhalle.PR1.Status", ns)).read_value()
            assert status == "IDLE"
            sim0 = await client.get_node(ua.NodeId("Werkhalle.Plant.SimTimeMin", ns)).read_value()
            await asyncio.sleep(1.2)
            sim1 = await client.get_node(ua.NodeId("Werkhalle.Plant.SimTimeMin", ns)).read_value()
            assert sim1 > sim0
            obj = client.get_node(ua.NodeId("Werkhalle.PR1", ns))
            result = await obj.call_method(ua.NodeId("Werkhalle.PR1.InjectFault", ns), "P-204", 100000.0)
            assert json.loads(result) == {"ok": True, "reason": ""}
            assert await client.get_node(ua.NodeId("Werkhalle.PR1.Status", ns)).read_value() == "FAULT"
        finally:
            await client.disconnect()
    finally:
        stop(proc)
    assert port_is_free(port), "port still taken after stopping the plant"
    text = log_path.read_text(encoding="utf-8")
    assert str(port) in text and run_id in text, text
    assert "asyncua" not in text, "asyncua stays quiet in a normal run:\n" + text


def test_run_ids_are_not_derived_from_the_seed():
    ids = {new_run_id() for _ in range(50)}
    assert len(ids) == 50
    assert all(run_id.startswith("r-") for run_id in ids)


def test_driver_exception_makes_the_process_exit_non_zero():
    port = free_port()
    code = (
        "import werkhalle.simcore.engine as e, werkhalle.plant.server as s\n"
        "orig = e.SimEngine.run_until\n"
        "state = {'n': 0}\n"
        "def boom(self, t):\n"
        "    state['n'] += 1\n"
        "    if state['n'] > 5:\n"
        "        raise RuntimeError('driver broke on purpose')\n"
        "    return orig(self, t)\n"
        "e.SimEngine.run_until = boom\n"
        f"raise SystemExit(s.main(['--port', '{port}', '--seed', '1']))\n"
    )
    with tempfile.TemporaryFile("w+", encoding="utf-8") as log:
        proc = subprocess.Popen([sys.executable, "-c", code], env=child_env(), stdout=subprocess.DEVNULL, stderr=log)
        try:
            exit_code = proc.wait(timeout=60)
        finally:
            stop(proc)
        log.seek(0)
        text = log.read()
    assert exit_code != 0
    assert "driver broke on purpose" in text
    assert port_is_free(port)


def test_port_in_use_exits_non_zero():
    with socket.socket() as blocker:
        blocker.bind(("127.0.0.1", 0))
        blocker.listen()
        port = blocker.getsockname()[1]
        proc = subprocess.run(
            [sys.executable, "-m", "werkhalle.plant", "--port", str(port)],
            env=child_env(), capture_output=True, text=True, timeout=60,
        )
    assert proc.returncode != 0
    assert proc.stdout == ""


@pytest.mark.parametrize("factor", ["77", "abc"])
def test_invalid_time_factor_flag_is_refused(factor):
    proc = subprocess.run(
        [sys.executable, "-m", "werkhalle.plant", "--port", str(free_port()), "--time-factor", factor],
        env=child_env(), capture_output=True, text=True, timeout=60,
    )
    assert proc.returncode == 2
    assert "time-factor" in proc.stderr
