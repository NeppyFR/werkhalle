"""`python -m werkhalle`: starts plant and MES, opens the browser, and stops everything again (PLAN section 10).

The launcher imports nothing but the standard library. It has to run, and say something useful, even when a dependency
of a child is missing, and it must not hold the plant's code in its own process.

Windows facts this file is built around:

- The venv's python.exe is a redirector with the real interpreter as its child, and the MES spawns pool workers below
  that. Killing a pid alone leaves survivors, hence `taskkill /T /F` plus a Job Object that kills whatever is left when the
  launcher itself goes away.
- A child that spawned its first process before joining the job would have an escaped grandchild, so children start
  suspended and only run once they are in the job.
- The launcher itself must stay outside the job: the browser it opens would otherwise die with it.
"""
from __future__ import annotations

import argparse
import ctypes
import http.client
import json
import os
import signal
import socket
import subprocess
import sys
import time
import urllib.request
import webbrowser
from ctypes import wintypes
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src"

HEALTH_TIMEOUT_S = 180.0
PLAN_WAIT_S = 60.0
PORT_SEARCH_SPAN = 100
POLL_S = 0.25
STOP_WAIT_S = 15.0
TAIL_LINES = 15
FACTORS = (0, 60, 120, 240, 480)

_CREATE_SUSPENDED = 0x00000004
_CREATE_NEW_PROCESS_GROUP = 0x00000200
_CREATE_NO_WINDOW = 0x08000000
_STILL_ACTIVE = 259
_PROCESS_QUERY_LIMITED = 0x1000
_SYNCHRONIZE = 0x00100000
_JOB_LIMIT_KILL_ON_CLOSE = 0x2000
_JOB_EXTENDED_LIMIT_INFO = 9
_TH32CS_SNAPPROCESS = 0x00000002
_INVALID_HANDLE = wintypes.HANDLE(-1).value


class _BasicLimit(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_longlong),
        ("PerJobUserTimeLimit", ctypes.c_longlong),
        ("LimitFlags", wintypes.DWORD),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", wintypes.DWORD),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", wintypes.DWORD),
        ("SchedulingClass", wintypes.DWORD),
    ]


class _IoCounters(ctypes.Structure):
    _fields_ = [(name, ctypes.c_ulonglong) for name in (
        "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
        "ReadTransferCount", "WriteTransferCount", "OtherTransferCount",
    )]


class _ExtendedLimit(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", _BasicLimit),
        ("IoInfo", _IoCounters),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


class _ProcessEntry(ctypes.Structure):
    _fields_ = [
        ("dwSize", wintypes.DWORD),
        ("cntUsage", wintypes.DWORD),
        ("th32ProcessID", wintypes.DWORD),
        ("th32DefaultHeapID", ctypes.c_size_t),
        ("th32ModuleID", wintypes.DWORD),
        ("cntThreads", wintypes.DWORD),
        ("th32ParentProcessID", wintypes.DWORD),
        ("pcPriClassBase", ctypes.c_long),
        ("dwFlags", wintypes.DWORD),
        ("szExeFile", ctypes.c_wchar * 260),
    ]


def _kernel32():
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateJobObjectW.restype = wintypes.HANDLE
    kernel.CreateJobObjectW.argtypes = (ctypes.c_void_p, wintypes.LPCWSTR)
    kernel.SetInformationJobObject.argtypes = (wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD)
    kernel.AssignProcessToJobObject.argtypes = (wintypes.HANDLE, wintypes.HANDLE)
    kernel.TerminateJobObject.argtypes = (wintypes.HANDLE, wintypes.UINT)
    kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    kernel.SetConsoleCtrlHandler.argtypes = (ctypes.c_void_p, wintypes.BOOL)
    kernel.GetExitCodeProcess.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
    kernel.GetProcessTimes.argtypes = (wintypes.HANDLE, *[ctypes.POINTER(wintypes.FILETIME)] * 4)
    kernel.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    kernel.CreateToolhelp32Snapshot.argtypes = (wintypes.DWORD, wintypes.DWORD)
    kernel.Process32FirstW.argtypes = (wintypes.HANDLE, ctypes.POINTER(_ProcessEntry))
    kernel.Process32NextW.argtypes = (wintypes.HANDLE, ctypes.POINTER(_ProcessEntry))
    return kernel


class Job:
    """A Job Object that kills its members when the last handle closes, which includes the launcher being killed."""

    def __init__(self) -> None:
        self._kernel = _kernel32()
        self.handle = self._kernel.CreateJobObjectW(None, None)
        if not self.handle:
            raise ctypes.WinError(ctypes.get_last_error())
        info = _ExtendedLimit()
        info.BasicLimitInformation.LimitFlags = _JOB_LIMIT_KILL_ON_CLOSE
        if not self._kernel.SetInformationJobObject(self.handle, _JOB_EXTENDED_LIMIT_INFO, ctypes.byref(info), ctypes.sizeof(info)):
            raise ctypes.WinError(ctypes.get_last_error())

    def add(self, process_handle: int) -> None:
        if not self._kernel.AssignProcessToJobObject(self.handle, process_handle):
            raise ctypes.WinError(ctypes.get_last_error())

    def terminate(self) -> None:
        self._kernel.TerminateJobObject(self.handle, 1)

    def close(self) -> None:
        if self.handle:
            self._kernel.CloseHandle(self.handle)
            self.handle = None


def _resume(process_handle: int) -> None:
    # subprocess closes the main thread's handle at once, so ResumeThread is out of reach. This is how a debugger does it.
    status = ctypes.WinDLL("ntdll").NtResumeProcess(wintypes.HANDLE(process_handle))
    if status != 0:
        raise OSError(f"NtResumeProcess: Status {status:#x}")


def process_table() -> list[tuple[int, int, str]]:
    """All processes as (pid, parent pid, exe name)."""
    kernel = _kernel32()
    snapshot = kernel.CreateToolhelp32Snapshot(_TH32CS_SNAPPROCESS, 0)
    if snapshot == _INVALID_HANDLE:
        raise ctypes.WinError(ctypes.get_last_error())
    rows: list[tuple[int, int, str]] = []
    try:
        entry = _ProcessEntry()
        entry.dwSize = ctypes.sizeof(entry)
        ok = kernel.Process32FirstW(snapshot, ctypes.byref(entry))
        while ok:
            rows.append((entry.th32ProcessID, entry.th32ParentProcessID, entry.szExeFile))
            ok = kernel.Process32NextW(snapshot, ctypes.byref(entry))
    finally:
        kernel.CloseHandle(snapshot)
    return rows


def descendants(pid: int) -> list[int]:
    """Pids of every process below `pid`. Parent pids can be reused, so only processes younger than their parent count."""
    table = process_table()
    children: dict[int, list[int]] = {}
    for child, parent, _ in table:
        children.setdefault(parent, []).append(child)
    found: list[int] = []
    stack = [pid]
    while stack:
        current = stack.pop()
        current_born = process_created(current)
        for child in children.get(current, ()):
            if child == current or child in found:
                continue
            child_born = process_created(child)
            if current_born is not None and child_born is not None and child_born < current_born:
                continue
            found.append(child)
            stack.append(child)
    return found


def process_created(pid: int) -> int | None:
    """Creation time of a live process, or None when it is gone. With the pid it identifies a process despite pid reuse."""
    kernel = _kernel32()
    handle = kernel.OpenProcess(_PROCESS_QUERY_LIMITED, False, pid)
    if not handle:
        return None
    try:
        code = wintypes.DWORD()
        if not kernel.GetExitCodeProcess(handle, ctypes.byref(code)) or code.value != _STILL_ACTIVE:
            return None
        created, exited, system, user = (wintypes.FILETIME() for _ in range(4))
        if not kernel.GetProcessTimes(handle, ctypes.byref(created), ctypes.byref(exited), ctypes.byref(system), ctypes.byref(user)):
            return None
        return (created.dwHighDateTime << 32) | created.dwLowDateTime
    finally:
        kernel.CloseHandle(handle)


def kill_tree(pid: int) -> None:
    """taskkill /T /F. Failure is not an error here: the process may already be gone."""
    subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True, check=False)


def say(text: str) -> None:
    print(text, flush=True)


def port_is_free(port: int) -> bool:
    """A plain bind with no SO_REUSEADDR, which is what tells us whether the child's own bind would succeed."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        try:
            probe.bind(("127.0.0.1", port))
        except OSError:
            return False
    return True


def pick_port(wanted: int, taken: set[int], label: str) -> int:
    for port in range(wanted, wanted + PORT_SEARCH_SPAN):
        if port not in taken and port_is_free(port):
            if port != wanted:
                say(f"Port {wanted} ({label}) ist belegt, nehme {port}.")
            return port
    raise SystemExit(f"Kein freier Port für {label} zwischen {wanted} und {wanted + PORT_SEARCH_SPAN - 1} gefunden.")


def child_env() -> dict[str, str]:
    env = dict(os.environ)
    env["PYTHONUTF8"] = "1"
    env["PYTHONPATH"] = str(SRC)
    # An inherited encoding setting would hide encoding bugs behind a developer's own shell configuration.
    env.pop("PYTHONIOENCODING", None)
    return env


def clear_leftovers(pid_file: Path) -> None:
    """Kill what an earlier launcher left behind, but only processes that are provably its own."""
    try:
        recorded = json.loads(pid_file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    for name, entry in recorded.items():
        try:
            pid, created = int(entry["pid"]), int(entry["created"])
        except (KeyError, TypeError, ValueError):
            continue
        if pid != os.getpid() and process_created(pid) == created:
            say(f"Altes {name}-Exemplar (PID {pid}) wird beendet.")
            kill_tree(pid)
    try:
        pid_file.unlink()
    except OSError:
        pass


class Child:
    """One supervised child process with its log file."""

    def __init__(self, name: str, label: str, argv: list[str], log_path: Path) -> None:
        self.name = name
        self.label = label
        self.argv = argv
        self.log_path = log_path
        self.process: subprocess.Popen | None = None
        self._tail_pos = 0

    def start(self, job: Job) -> None:
        with open(self.log_path, "wb") as log:
            self.process = subprocess.Popen(
                self.argv,
                env=child_env(),
                cwd=str(ROOT),
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=subprocess.STDOUT,
                # Own console and process group: Ctrl+C reaches only the launcher, which then stops the children in order.
                creationflags=_CREATE_SUSPENDED | _CREATE_NEW_PROCESS_GROUP | _CREATE_NO_WINDOW,
            )
        try:
            job.add(int(self.process._handle))
        except OSError as exc:
            # Some hosts forbid nested jobs. taskkill still stops the tree; only a hard kill of the launcher could leave survivors.
            say(f"Hinweis: Job-Objekt für {self.name} nicht verfügbar ({exc}).")
        _resume(int(self.process._handle))

    def exit_code(self) -> int | None:
        return None if self.process is None else self.process.poll()

    def stop(self) -> None:
        if self.process is None:
            return
        if self.process.poll() is None:
            kill_tree(self.process.pid)
        try:
            self.process.wait(timeout=STOP_WAIT_S)
        except subprocess.TimeoutExpired:
            self.process.kill()

    def read_new_errors(self) -> list[str]:
        """New complete lines of the log that a person should see on the console."""
        try:
            with open(self.log_path, "rb") as log:
                log.seek(self._tail_pos)
                data = log.read()
        except OSError:
            return []
        end = data.rfind(b"\n")
        if end < 0:
            return []
        self._tail_pos += end + 1
        lines = data[:end].decode("utf-8", "replace").splitlines()
        return [line for line in lines if " ERROR " in line or " CRITICAL " in line or line.startswith("Traceback")]

    def last_lines(self, count: int = TAIL_LINES) -> list[str]:
        try:
            return self.log_path.read_text(encoding="utf-8", errors="replace").splitlines()[-count:]
        except OSError:
            return []


def health(port: int) -> dict | None:
    # No proxy: a system-wide proxy setting must not intercept 127.0.0.1.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(f"http://127.0.0.1:{port}/api/health", timeout=2.0) as response:
            return json.loads(response.read().decode("utf-8"))
    except (OSError, ValueError, http.client.HTTPException):
        # HTTPException too: a connect to a free port in the ephemeral range can land on itself and read back its own request.
        return None


class _StopRequested(Exception):
    pass


def _raise_stop(_signum, _frame) -> None:
    raise _StopRequested


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="python -m werkhalle", description="Werkhalle: Anlage und MES starten")
    parser.add_argument("--http-port", type=int, default=8321)
    parser.add_argument("--plant-port", type=int, default=4840)
    parser.add_argument("--time-factor", type=int, default=120, choices=FACTORS)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--solver-limit", type=float, default=8.0)
    parser.add_argument("--data-dir", default="data")
    parser.add_argument("--log-dir", default=str(ROOT / "logs"))
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--no-random-failures", action="store_true")
    parser.add_argument("--no-auto-supply", action="store_true")
    return parser.parse_args(argv)


def run(args: argparse.Namespace) -> int:
    log_dir = Path(args.log_dir).resolve()
    data_dir = Path(args.data_dir).resolve()
    log_dir.mkdir(parents=True, exist_ok=True)
    data_dir.mkdir(parents=True, exist_ok=True)
    pid_file = log_dir / "werkhalle.pid"
    clear_leftovers(pid_file)

    plant_port = pick_port(args.plant_port, set(), "OPC UA")
    http_port = pick_port(args.http_port, {plant_port}, "HTTP")
    endpoint = f"opc.tcp://127.0.0.1:{plant_port}/werkhalle/"
    url = f"http://127.0.0.1:{http_port}/"

    plant_argv = [sys.executable, "-m", "werkhalle.plant", "--port", str(plant_port), "--seed", str(args.seed),
                  "--time-factor", str(args.time_factor)]
    if args.no_random_failures:
        plant_argv.append("--no-random-failures")
    mes_argv = [sys.executable, "-m", "werkhalle.mes", "--plant-url", endpoint, "--http-port", str(http_port),
                "--data-dir", str(data_dir), "--seed", str(args.seed), "--solver-limit", str(args.solver_limit)]
    if args.no_auto_supply:
        mes_argv.append("--no-auto-supply")
    children = [
        Child("plant", "Die Anlage", plant_argv, log_dir / "plant.log"),
        Child("mes", "Das MES", mes_argv, log_dir / "mes.log"),
    ]

    job = Job()
    code = 0
    previous = {sig: signal.signal(sig, _raise_stop) for sig in (signal.SIGINT, signal.SIGBREAK, signal.SIGTERM)}
    try:
        say("Werkhalle startet ...")
        say(f"  OPC-UA-Server: {endpoint}")
        say(f"  Protokolle:    {log_dir}")
        for child in children:
            child.start(job)
        _write_pid_file(pid_file, children)
        code = _supervise(children, http_port, url, args)
    except _StopRequested:
        say("Werkhalle wird beendet ...")
    finally:
        # From here on a second Ctrl+C must not abandon the stop half way.
        for sig in previous:
            signal.signal(sig, signal.SIG_IGN)
        for child in reversed(children):
            child.stop()
        job.terminate()
        job.close()
        try:
            pid_file.unlink()
        except OSError:
            pass
        for sig, handler in previous.items():
            signal.signal(sig, handler)
    say("Alle Prozesse sind beendet." if code == 0 else "Alle Prozesse sind beendet. Die Werkhalle wurde mit Fehler beendet.")
    return code


def _write_pid_file(pid_file: Path, children: list[Child]) -> None:
    rows = {"Launcher": {"pid": os.getpid(), "created": process_created(os.getpid())}}
    for child in children:
        pid = child.process.pid
        rows[child.name] = {"pid": pid, "created": process_created(pid)}
    pid_file.write_text(json.dumps(rows), encoding="utf-8")


def _supervise(children: list[Child], http_port: int, url: str, args: argparse.Namespace) -> int:
    deadline = time.monotonic() + HEALTH_TIMEOUT_S
    up = False
    connected_at: float | None = None
    next_health = 0.0
    while True:
        for child in children:
            for line in child.read_new_errors():
                say(f"[{child.name}] {line}")
            code = child.exit_code()
            if code is not None:
                say(f"{child.label} ist unerwartet beendet worden (Exit-Code {code}). Die Werkhalle wird gestoppt.")
                say(f"Die letzten Zeilen aus {child.log_path}:")
                for line in child.last_lines():
                    say(f"  {line}")
                return 1
        if not up and time.monotonic() >= next_health:
            next_health = time.monotonic() + 0.5
            reply = health(http_port)
            if reply is not None and reply.get("ok") and connected_at is None:
                connected_at = time.monotonic()
                say("MES verbunden, der Startplan wird berechnet ...")
            # A hall without a plan stands still, so the browser waits for it. Not for ever: a page beats a window that never opens.
            planned = reply is not None and reply.get("ok") and (reply.get("plan_revision") or 0) >= 1
            if planned or (connected_at is not None and time.monotonic() - connected_at >= PLAN_WAIT_S):
                up = True
                say(f"Leitstand bereit: {url}")
                if reply is not None and reply.get("plant_module_loaded"):
                    say("WARNUNG: Das MES hat das Anlagenmodul geladen. Das darf nicht sein.")
                if not args.no_browser:
                    _open_browser(url)
                say("Beenden mit Strg+C.")
            elif connected_at is None and time.monotonic() >= deadline:
                say(f"Das MES ist nach {HEALTH_TIMEOUT_S:.0f} s nicht bereit. Die Werkhalle wird gestoppt.")
                return 1
        time.sleep(POLL_S)


def _open_browser(url: str) -> None:
    """A browser that will not open must not take the plant down with it: the address is on the console."""
    try:
        opened = webbrowser.open(url)
    except Exception:
        opened = False
    if not opened:
        say(f"Der Browser konnte nicht geöffnet werden. Bitte {url} von Hand öffnen.")


def main(argv: list[str] | None = None) -> int:
    if sys.platform != "win32":
        raise SystemExit("Der Starter ist für Windows gebaut. Plant und MES lassen sich direkt starten: python -m werkhalle.plant, python -m werkhalle.mes")
    # A parent that runs in a process group of its own hands down "ignore Ctrl+C". Python's handler alone does not undo that, and
    # the launcher could then not be stopped.
    _kernel32().SetConsoleCtrlHandler(None, False)
    # Text that the console's code page cannot show must not kill the launcher in the middle of a stop.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="replace")
    return run(parse_args(argv))


if __name__ == "__main__":
    sys.exit(main())
