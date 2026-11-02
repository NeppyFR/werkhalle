"""The hard rule of PLAN section 1: `werkhalle.plant` is unreachable from mes, execution, scheduling and simcore.

The graph is built from the source, so it sees function-local imports, which `sys.modules` in a test process would not.
"""
from __future__ import annotations

import ast
import inspect
import sys
from collections import deque
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src"
PACKAGE_ROOT = SRC / "werkhalle"

# PLAN section 1, "may import". Third-party names are checked only where the plan lists them.
ALLOWED_INTERNAL = {
    "domain": {"domain"},
    "simcore": {"simcore", "domain"},
    "plant": {"plant", "domain", "simcore"},
    "scheduling": {"scheduling", "domain"},
    "execution": {"execution", "domain", "scheduling"},
    "mes": {"mes", "domain", "simcore", "scheduling", "execution"},
}
GUARDED = ("mes", "execution", "scheduling", "simcore")
BANNED_DYNAMIC = {"importlib", "__import__", "imp", "runpy"}
EXECUTION_MAY_SEE_IN_SCHEDULING = {"werkhalle.scheduling.model", "werkhalle.scheduling.retime", "werkhalle.scheduling.validate"}


def module_name(path: Path) -> str:
    parts = list(path.relative_to(SRC).with_suffix("").parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def all_modules() -> dict[str, Path]:
    return {module_name(p): p for p in PACKAGE_ROOT.rglob("*.py") if "__pycache__" not in p.parts}


def resolve_from(module: str, path: Path, node: ast.ImportFrom) -> str:
    """The absolute name a `from ... import` refers to."""
    if node.level == 0:
        return node.module or ""
    package = module if path.name == "__init__.py" else module.rpartition(".")[0]
    for _ in range(node.level - 1):
        package = package.rpartition(".")[0]
    return f"{package}.{node.module}" if node.module else package


def imports_of(module: str, path: Path) -> set[str]:
    """Every module this file can load: top level, inside functions, `from x import y` where y may itself be a module."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            base = resolve_from(module, path, node)
            names.add(base)
            names.update(f"{base}.{alias.name}" for alias in node.names)
    return names


def build_graph() -> dict[str, set[str]]:
    modules = all_modules()
    graph: dict[str, set[str]] = {}
    for module, path in modules.items():
        edges: set[str] = set()
        for name in imports_of(module, path):
            if name in modules:
                edges.add(name)
            # Importing a.b.c runs a and a.b too, so a package __init__ is an edge of everything below it.
            parts = name.split(".")
            for i in range(1, len(parts)):
                parent = ".".join(parts[:i])
                if parent in modules:
                    edges.add(parent)
        package_parts = module.split(".")
        for i in range(1, len(package_parts)):
            parent = ".".join(package_parts[:i])
            if parent in modules:
                edges.add(parent)
        edges.discard(module)
        graph[module] = edges
    return graph


def reachable(graph: dict[str, set[str]], start: str) -> set[str]:
    seen = {start}
    queue = deque([start])
    while queue:
        for target in graph.get(queue.popleft(), ()):
            if target not in seen:
                seen.add(target)
                queue.append(target)
    return seen


@pytest.fixture(scope="module")
def graph() -> dict[str, set[str]]:
    return build_graph()


def guarded_modules() -> list[str]:
    return [m for m in all_modules() if m.split(".")[1:2] and m.split(".")[1] in GUARDED]


def test_the_scanner_sees_what_it_claims_to_see():
    # If these fail, every other test here passes vacuously.
    modules = all_modules()
    assert {"werkhalle.mes.runtime", "werkhalle.plant.server", "werkhalle.simcore.engine", "werkhalle.execution.tracker"} <= set(modules)
    assert len(guarded_modules()) >= 20
    sample = ast.parse("def f():\n    from ..plant.server import x\n    import werkhalle.plant\n")
    function_imports = [n for n in ast.walk(sample) if isinstance(n, (ast.Import, ast.ImportFrom))]
    assert len(function_imports) == 2
    assert "werkhalle.plant.server" in build_graph()["werkhalle.plant.__main__"]


def test_the_scanner_catches_a_forbidden_function_local_import(tmp_path):
    root = tmp_path / "werkhalle" / "mes"
    root.mkdir(parents=True)
    file = root / "sneaky.py"
    file.write_text("def f():\n    from ..plant import server\n", encoding="utf-8")
    module = "werkhalle.mes.sneaky"
    tree = ast.parse(file.read_text(encoding="utf-8"))
    node = next(n for n in ast.walk(tree) if isinstance(n, ast.ImportFrom))
    package = module.rpartition(".")[0]
    for _ in range(node.level - 1):
        package = package.rpartition(".")[0]
    assert f"{package}.{node.module}" == "werkhalle.plant"


@pytest.mark.parametrize("package", GUARDED)
def test_plant_is_unreachable(graph, package):
    starts = [m for m in graph if m == f"werkhalle.{package}" or m.startswith(f"werkhalle.{package}.")]
    assert starts, package
    for start in starts:
        hits = sorted(m for m in reachable(graph, start) if m == "werkhalle.plant" or m.startswith("werkhalle.plant."))
        assert not hits, f"{start} reaches {hits}"


def test_the_top_level_launcher_does_not_import_the_plant(graph):
    assert not (reachable(graph, "werkhalle.__main__") - {"werkhalle", "werkhalle.__main__"})


@pytest.mark.parametrize("module", guarded_modules())
def test_no_dynamic_imports(module):
    path = all_modules()[module]
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            assert not {alias.name.split(".")[0] for alias in node.names} & BANNED_DYNAMIC, f"{module}:{node.lineno}"
        elif isinstance(node, ast.ImportFrom):
            assert (node.module or "").split(".")[0] not in BANNED_DYNAMIC, f"{module}:{node.lineno}"
        elif isinstance(node, ast.Name):
            assert node.id not in BANNED_DYNAMIC, f"{module}:{node.lineno}: {node.id}"
        elif isinstance(node, ast.Attribute):
            assert node.attr not in {"import_module", "__import__"}, f"{module}:{node.lineno}: {node.attr}"
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            assert node.func.id not in {"exec", "eval"}, f"{module}:{node.lineno}: {node.func.id}"


@pytest.mark.parametrize("package", sorted(ALLOWED_INTERNAL))
def test_packages_import_only_what_the_plan_allows(graph, package):
    allowed = ALLOWED_INTERNAL[package]
    for module, edges in graph.items():
        if module.split(".")[1:2] != [package]:
            continue
        for target in edges:
            parts = target.split(".")
            if len(parts) < 2:
                continue
            assert parts[1] in allowed, f"{module} imports {target}, {package} may only use {sorted(allowed)}"


def test_execution_sees_scheduling_only_through_its_contract(graph):
    for module, edges in graph.items():
        if not module.startswith("werkhalle.execution"):
            continue
        for target in edges:
            if target == "werkhalle.scheduling" or not target.startswith("werkhalle.scheduling"):
                continue
            assert target in EXECUTION_MAY_SEE_IN_SCHEDULING, f"{module} imports {target}"


def third_party_of(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            names.add(node.module.split(".")[0])
    return names - set(sys.stdlib_module_names) - {"werkhalle"}


def test_third_party_imports_stay_in_their_packages():
    limits = {"domain": set(), "simcore": set(), "execution": set(), "scheduling": {"ortools"}, "plant": {"asyncua"}}
    for module, path in all_modules().items():
        parts = module.split(".")
        if len(parts) > 1 and parts[1] in limits:
            extra = third_party_of(path) - limits[parts[1]]
            assert not extra, f"{module} imports {sorted(extra)}"


def test_break_machine_tool_is_independent_of_the_package():
    # An independent OPC UA client must not share code with the plant or the MES, or it would prove nothing.
    tool = Path(__file__).resolve().parents[1] / "tools" / "break_machine.py"
    tree = ast.parse(tool.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            assert not any(alias.name.split(".")[0] == "werkhalle" for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            assert (node.module or "").split(".")[0] != "werkhalle"


def test_importing_the_mes_does_not_load_the_plant_or_the_solver():
    import subprocess

    code = (
        "import sys, werkhalle.mes.runtime, werkhalle.mes.app\n"
        "bad = [m for m in sys.modules if m == 'werkhalle.plant' or m.startswith('werkhalle.plant.')]\n"
        "assert not bad, bad\n"
        "assert 'ortools' not in sys.modules, 'the event loop process must not pay for ortools'\n"
        "print('ok')\n"
    )
    env = {"PYTHONPATH": str(SRC), "PYTHONUTF8": "1", "SYSTEMROOT": _system_root(), "SYSTEMDRIVE": _system_drive()}
    done = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True, timeout=120)
    assert done.returncode == 0, done.stderr
    assert done.stdout.strip() == "ok"


def _system_root() -> str:
    import os

    return os.environ.get("SYSTEMROOT", r"C:\Windows")


def _system_drive() -> str:
    """Without it, Windows creates a literal %SystemDrive% folder in the child's working directory."""
    import os

    return os.environ.get("SYSTEMDRIVE", "C:")


async def test_every_plant_method_handler_is_a_coroutine_function():
    from werkhalle.plant import PlantServer, SimClock
    from werkhalle.simcore import SimEngine

    import socket

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    plant = PlantServer(SimEngine(seed=1, run_id="r-arch"), SimClock(factor=0.0), port)
    await plant.start()
    try:
        assert len(plant.method_handlers) >= 2 + 4 * 7
        for key, handler in plant.method_handlers.items():
            assert inspect.iscoroutinefunction(handler), key
    finally:
        await plant.stop()
