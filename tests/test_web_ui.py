"""Regression tests for the logic in the web UI, run in Node. The browser checks of layout and flows are not part of pytest."""

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

WEB = Path(__file__).resolve().parents[1] / "src" / "werkhalle" / "web"
JS = WEB / "js"

node = shutil.which("node")
needs_node = pytest.mark.skipif(node is None, reason="Node ist nicht installiert")


def run_module(script: str):
    completed = subprocess.run(
        [node, "--input-type=module"],
        input=script,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    return json.loads(completed.stdout.strip().splitlines()[-1])


def url(relative: str) -> str:
    return (JS / relative).as_uri()


def fmt_call(expression: str):
    result = run_module(
        f"import {{ fmt }} from {json.dumps(url('core/format.js'))};\n"
        f"console.log(JSON.stringify({expression}));\n"
    )
    return result.replace(" ", " ") if isinstance(result, str) else result


@needs_node
@pytest.mark.parametrize(
    ("minutes", "text"),
    [
        (35, "35 min"),
        (260, "4 h 20 min"),
        (1440, "24 h"),
        (1475, "24 h 35 min"),
        (1789, "29 h 49 min"),
        (2879, "47 h 59 min"),
        (2880, "2 T"),
        (2915, "2 T 35 min"),
        (3060, "2 T 3 h"),
    ],
)
def test_duration_keeps_its_precision_past_a_day(minutes, text):
    assert fmt_call(f"fmt.duration({minutes})") == text


@needs_node
def test_a_signed_duration_past_a_day_keeps_its_sign():
    assert fmt_call("fmt.duration(-1475, { signed: true })") == "−24 h 35 min"
    assert fmt_call("fmt.duration(1475, { signed: true })") == "+24 h 35 min"


@needs_node
@pytest.mark.parametrize(
    ("estimate", "since", "now", "text"),
    [
        (960, 100, 100, "noch ca. 16 h"),
        (960, 100, 160, "noch ca. 15 h"),
        (960, 100, 1000, "noch ca. 1 h"),
        (960, 100, 1040, "noch ca. 20 min"),
        (960, 100, 1060, "Reparatur überfällig"),
        (960, 100, 5000, "Reparatur überfällig"),
    ],
)
def test_repair_shows_what_is_left_not_the_total(estimate, since, now, text):
    assert fmt_call(f"fmt.repairLeft({estimate}, {since}, {now})") == text


@needs_node
def test_repair_without_an_estimate_says_nothing():
    assert fmt_call("fmt.repairLeft(0, 0, 10)") is None
    assert fmt_call("fmt.repairLeft(null, 0, 10)") is None


@needs_node
def test_repair_long_form_names_the_repair():
    assert fmt_call("fmt.repairLeft(960, 0, 60, { long: true })") == "Reparatur noch ca. 15 h"
    assert fmt_call("fmt.repairLeft(960, 0, 2000, { long: true })") == "Reparatur überfällig"


@needs_node
@pytest.mark.parametrize(
    ("expression", "text"),
    [
        ("fmt.opId('L-00041:press')", "L-00041 · Pressen"),
        ("fmt.opId('M-6060-00002:cast')", "M-6060-00002 · Gießen"),
        ("fmt.opId(null)", "–"),
        ("fmt.ops(['L-1:age', 'L-2:age'], 'age')", "L-1, L-2 · Auslagern"),
        ("fmt.setupClass('6082|M-3001')", "Legierung 6082 · Matrize M-3001"),
        ("fmt.setupClass('6082')", "Legierung 6082"),
        ("fmt.setupClass('T6')", "Zustand T6"),
        ("fmt.setupClass('L6000')", "Länge 6.000 mm"),
        ("fmt.setupClass('B-10')", "10 Stangen je Paket"),
        ("fmt.setupClass(null)", "–"),
        ("fmt.orderId('Z-A-0001')", "Eilauftrag (neu)"),
        ("fmt.orderId('Z-A-0002')", "Eilauftrag 2 (neu)"),
        ("fmt.orderId('A-1027')", "A-1027"),
        ("fmt.machineShort('PR2')", "Presse 2"),
        ("fmt.waiting('Kein Auftrag')", "kein Auftrag"),
        ("fmt.waiting('Kein Plan')", "wartet auf Plan"),
        ("fmt.waiting('Entscheidung offen')", "wartet: Entscheidung"),
        ("fmt.waiting('Material')", "wartet: Material"),
    ],
)
def test_internal_identifiers_get_readable_labels(expression, text):
    assert fmt_call(expression) == text


@needs_node
def test_waiting_for_a_plan_explains_itself():
    assert "Plan" in fmt_call("fmt.waitingTip('Kein Plan')")


@needs_node
def test_a_dead_mes_is_called_gone_after_several_reconnects_and_recovers():
    # Timers run by hand, so the backoff takes no real time.
    script = f"""
    globalThis.location = {{ protocol: 'http:', host: 'x' }};
    Object.defineProperty(globalThis, 'document', {{ value: {{ hidden: false }}, configurable: true }});
    const sockets = [];
    globalThis.WebSocket = class {{ constructor() {{ sockets.push(this); }} close() {{ this.onclose?.(); }} }};
    const timers = [];
    globalThis.setTimeout = (fn) => {{ timers.push(fn); return timers.length; }};
    globalThis.clearTimeout = () => {{}};
    globalThis.setInterval = () => 0;
    const {{ createStore }} = await import({json.dumps(url('core/store.js'))});
    const never = () => new Promise(() => {{}});
    const store = createStore({{ api: {{ get: never, post: never }}, toast() {{}} }});
    store.start();
    const gone = [];
    for (let i = 0; i < 7; i += 1) {{
      sockets.at(-1).onclose();
      gone.push(Boolean(store.link().gone));
      timers.shift()();
    }}
    const detail = store.link().detail;
    sockets.at(-1).onopen();
    console.log(JSON.stringify({{ gone, detail, after: Boolean(store.link().gone) }}));
    process.exit(0);
    """
    result = run_module(script)
    assert result["gone"] == [False, False, False, False, True, True, True]
    assert "start.cmd" in result["detail"]
    assert result["after"] is False


STUB_DOM = "Object.defineProperty(globalThis, 'document', { value: { addEventListener() {}, createElementNS() { return { setAttribute() {}, append() {} }; }, createElement() { return { setAttribute() {}, append() {} }; } }, configurable: true }); globalThis.ResizeObserver = class { observe() {} disconnect() {} }; globalThis.window = { matchMedia: () => ({ matches: false }) };"


@needs_node
def test_order_form_refuses_a_fraction_instead_of_rounding_it():
    result = run_module(
        f"{STUB_DOM}\nconst {{ quantityProblem }} = await import({json.dumps(url('views/auftraege.js'))});\n"
        "console.log(JSON.stringify(['7.5', '8', '0', '-5', '', 'x'].map(quantityProblem)));\n"
    )
    assert "ganze Zahl" in result[0]
    assert result[1] is None
    assert all("größer als null" in text for text in result[2:])


@needs_node
def test_oee_trends_clip_to_the_window_and_a_fault_from_before_it_is_not_zero():
    result = run_module(
        f"{STUB_DOM}\nconst {{ clipToWindow, carriedFault }} = await import({json.dumps(url('views/oee.js'))});\n"
        "const t = [100, 500, 900, 1300];\n"
        "console.log(JSON.stringify({\n"
        "  window: clipToWindow(t, { PR2: [1, 2, 3, 4] }, '8h', 1300),\n"
        "  start: clipToWindow(t, { PR2: [1, 2, 3, 4] }, 'start', 1300),\n"
        "  empty: clipToWindow(t, { PR2: [1, 2, 3, 4] }, '8h', 9000),\n"
        "  carried: carriedFault({ fault_count: 0, minutes: { fault: 305 } }),\n"
        "  counted: carriedFault({ fault_count: 2, minutes: { fault: 305 } }),\n"
        "  none: carriedFault({ fault_count: 0, minutes: { fault: 0 } }),\n"
        "}));\n"
    )
    assert result["window"] == {"t": [900, 1300], "series": {"PR2": [3, 4]}}
    assert result["start"]["t"] == [100, 500, 900, 1300]
    assert result["empty"] == {"t": [], "series": {"PR2": []}}
    assert result["carried"] is True
    assert result["counted"] is False
    assert result["none"] is False


@needs_node
def test_a_scenario_on_a_machine_already_down_says_so():
    result = run_module(
        f"{STUB_DOM}\nconst {{ alreadyDownNotes }} = await import({json.dumps(url('views/zwilling.js'))});\n"
        f"const {{ fmt }} = await import({json.dumps(url('core/format.js'))});\n"
        "const store = { clock: { now: () => 1000 }, machine: (id) => ({ PR2: { status: 'FAULT', est_repair_min: 960, status_since: 500 }, OF2: { status: 'RUN' }, OF1: { status: 'FAULT', est_repair_min: 60, status_since: 100 } })[id] };\n"
        "const items = [{ type: 'machine_down', machine: 'PR2' }, { type: 'machine_down', machine: 'OF2' }, { type: 'rush_order' }];\n"
        "console.log(JSON.stringify({\n"
        "  down: alreadyDownNotes(items, store, fmt),\n"
        "  overdue: alreadyDownNotes([{ type: 'machine_down', machine: 'OF1' }], store, fmt),\n"
        "  none: alreadyDownNotes([{ type: 'machine_down', machine: 'OF2' }], store, fmt),\n"
        "}));\n"
    )
    assert len(result["down"]) == 1
    down = result["down"][0].replace(" ", " ")
    assert down.startswith("Presse 2 steht bereits, Reparatur bis ca. Tag")
    assert "überfällig" in result["overdue"][0]
    assert result["none"] == []


def _call_arguments(source: str, start: int) -> list[str]:
    depth = 0
    args = []
    arg_start = start + 1
    quote = None
    i = start
    while i < len(source):
        char = source[i]
        if quote:
            if char == "\\":
                i += 2
                continue
            if char == quote:
                quote = None
        elif char in "'\"`":
            quote = char
        elif char in "([{":
            depth += 1
        elif char in ")]}":
            depth -= 1
            if depth == 0:
                args.append(source[arg_start:i])
                return args
        elif char == "," and depth == 1:
            args.append(source[arg_start:i])
            arg_start = i + 1
        i += 1
    return args


def test_no_native_append_takes_a_child_that_can_be_null():
    # Element.append(null) prints the word "null" on screen. appendAll() from dom.js skips it.
    offenders = []
    for path in JS.rglob("*.js"):
        source = path.read_text(encoding="utf-8")
        for match in re.finditer(r"\.append\(", source):
            for argument in _call_arguments(source, match.end() - 1):
                text = argument.strip()
                if text.startswith("..."):
                    continue
                if re.search(r"\?\s[^:]*:\s*(null|undefined)\s*$", text, re.S) or "tagRow(" in text:
                    line = source[: match.start()].count("\n") + 1
                    offenders.append(f"{path.relative_to(JS)}:{line}")
    assert offenders == []


def test_the_drawer_closes_on_a_route_change():
    source = (WEB / "app.js").read_text(encoding="utf-8")
    assert re.search(r"router\.onChange\(\(\) => ctx\.drawer\.close\(\)\)", source)


def test_web_readme_lists_every_component():
    readme = (WEB / "README.md").read_text(encoding="utf-8")
    for path in (JS / "components").glob("*.js"):
        assert path.name in readme, path.name


@needs_node
@pytest.mark.parametrize(
    ("expression", "text"),
    [
        ("fmt.lotId('Z-L-00001')", "Eil-Los 1"),
        ("fmt.lotId('Z-L-00012')", "Eil-Los 12"),
        ("fmt.lotId('L-00041')", "L-00041"),
        ("fmt.lotId('Z-M-6082-00014')", "M-6082 (neu)"),
        ("fmt.lotId('M-6082-00014')", "M-6082-00014"),
        ("fmt.lotId('Z-B-A-3')", "B-A-3 (neu)"),
        ("fmt.opId('Z-L-00001:press')", "Eil-Los 1 · Pressen"),
        ("fmt.ops(['Z-L-00001:press', 'Z-L-00002:press'], 'press')", "Eil-Los 1, Eil-Los 2 · Pressen"),
    ],
)
def test_ids_that_only_exist_in_the_twin_get_a_readable_label(expression, text):
    assert fmt_call(expression) == text


def test_every_lot_the_hall_and_the_replay_strip_show_goes_through_the_label():
    hall = (JS / "components" / "hall.js").read_text(encoding="utf-8")
    assert "${m.lots[0]}" not in hall
    assert "${e.lot_id}" not in hall
    assert "${entry.lot_id}" not in hall
    zwilling = (JS / "views" / "zwilling.js").read_text(encoding="utf-8")
    assert "${lots[0]}" not in zwilling
    assert ": lots[0];" not in zwilling


@needs_node
def test_order_form_checks_the_due_limit_in_the_hours_it_asks_for():
    result = run_module(
        f"{STUB_DOM}\nconst {{ dueProblem }} = await import({json.dumps(url('views/auftraege.js'))});\n"
        "console.log(JSON.stringify(['2000', '1666.7', '1666', '10', '0', '', 'x'].map(dueProblem)));\n"
    )
    assert all("Stunden" in text and "Minuten" not in text for text in result[:2])
    assert result[2] is None
    assert result[3] is None
    assert all("Zukunft" in text for text in result[4:])


@needs_node
def test_the_also_line_believes_the_live_machine_not_the_frozen_proposal():
    result = run_module(
        f"{STUB_DOM}\nconst {{ alsoLine }} = await import({json.dumps(url('components/failure-panel.js'))});\n"
        "const live = { PR1: { status: 'IDLE' }, SAE: { status: 'FAULT', alarm: { code: 'S-402' } } };\n"
        "const left = (fault) => (fault.machine === 'SAE' ? 'noch 46 h' : 'noch 2 T');\n"
        "const line = (others) => alsoLine(others, (id) => live[id], left);\n"
        "console.log(JSON.stringify({\n"
        "  mixed: line([{ machine: 'PR1', code: 'P-201', remaining_min: 3000 }, { machine: 'SAE', code: 'S-401', remaining_min: 2800 }]),\n"
        "  backOnly: line([{ machine: 'PR1', code: 'P-201', remaining_min: 3000 }]),\n"
        "  noRow: line([{ machine: 'OF2', code: 'O-302', remaining_min: 60 }]),\n"
        "  none: line([]),\n"
        "}));\n"
    )
    assert result["mixed"] == "auch: SAE S-402, noch 46 h · wieder bereit: PR1"
    assert result["backOnly"] == "wieder bereit: PR1"
    assert result["noRow"] == "auch: OF2 O-302, noch 2 T"
    assert result["none"] == ""


@needs_node
def test_the_pending_line_does_not_call_a_new_order_a_fault():
    result = run_module(
        f"{STUB_DOM}\nconst {{ pendingLine }} = await import({json.dumps(url('components/failure-panel.js'))});\n"
        "console.log(JSON.stringify({\n"
        "  fault: pendingLine({ machine: 'OF1', code: 'O-301', text: 'x' }),\n"
        "  order: pendingLine({ machine: null, code: '', text: 'neuer Auftrag A-1013' }),\n"
        "  blocked: pendingLine({ machine: null, code: '', text: 'neuer Auftrag A-1013', accept_blocked: 'Zu wenig Bolzen' }),\n"
        "}));\n"
    )
    assert result["fault"].startswith("Neue Störung OF1 O-301:")
    assert result["fault"].endswith("bleibt übernehmbar.")
    assert result["order"].startswith("Neuer Auftrag A-1013:")
    assert "Störung" not in result["order"]
    assert "Zu wenig Bolzen" in result["blocked"]
    assert "bleibt übernehmbar" not in result["blocked"]


def test_planung_loads_past_faults_when_it_opens_and_when_the_plan_changes():
    source = (JS / "views" / "planung.js").read_text(encoding="utf-8")
    assert re.search(r"store\.refetchPlan\(\);\s+loadPastFaults\(\);\s+restore\(\);", source)
    assert re.search(r"lastRevision = plan\.revision;\s+refreshStale\(\);\s+loadPastFaults\(\);", source)
