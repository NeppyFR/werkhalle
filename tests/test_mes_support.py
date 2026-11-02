"""The MES's small parts that need no plant: seed orders, the order book, the process pools."""
from __future__ import annotations

import json
import os
import random
import socket
import subprocess
import sys
import time
from concurrent.futures.process import BrokenProcessPool
from pathlib import Path
from types import SimpleNamespace

import pytest

import werkhalle.__main__ as launcher
from werkhalle.__main__ import process_created
from werkhalle.domain import catalog
from werkhalle.domain.orders import Lot, Order, split_order
from werkhalle.mes.orderbook import OrderBook, base_row
from werkhalle.mes.pools import ManagedPool, worker_ping
from werkhalle.mes.seed import (
    SEED_LOTS,
    SEED_ORDERS,
    SUPPLY_CAP_LOTS,
    load_estimate_min,
    next_supply,
    seed_orders,
    stage_load_min,
)

SRC = Path(__file__).resolve().parents[1] / "src"


@pytest.mark.parametrize("seed", [1, 2, 3, 7, 42, 99, 12345])
def test_seed_book_has_the_promised_shape(seed):
    book = seed_orders(seed)
    assert len(book) == SEED_ORDERS
    assert {s.order.product_id for s in book} == set(catalog.PRODUCTS)
    assert sum(len(s.lots) for s in book) == SEED_LOTS
    for seeded in book:
        order = seeded.order
        product = catalog.PRODUCTS[order.product_id]
        assert order.due_min >= catalog.min_lead_min(product), "never below the minimum lead"
        assert order.created_min == 0.0
        assert order.qty_bars % product.bars_per_package == 0, "whole packages"
        assert [lot.id for lot in seeded.lots] == [lot.id for lot in split_order(order, int(seeded.lots[0].id.split("-")[1]))]
        assert all(1 <= lot.n_billets <= catalog.MAX_BILLETS_PER_LOT for lot in seeded.lots)
        billets = sum(lot.n_billets for lot in seeded.lots)
        assert billets * product.bars_per_billet >= order.qty_bars, "a lot delivers at least what was ordered, before scrap"


def test_seed_is_deterministic_and_differs_between_seeds():
    assert seed_orders(42) == seed_orders(42)
    assert seed_orders(42) != seed_orders(43)


def test_seed_ids_are_contiguous():
    book = seed_orders(5)
    assert [s.order.id for s in book] == [f"A-{1001 + i}" for i in range(len(book))]
    lot_ids = [lot.id for s in book for lot in s.lots]
    assert lot_ids == [f"L-{1 + i:05d}" for i in range(len(lot_ids))]


def test_some_seed_orders_are_tight_against_the_load_and_most_are_not():
    book = seed_orders(42)
    horizon = load_estimate_min(lot for s in book for lot in s.lots)
    slack = {s.order.id: s.order.due_min - catalog.min_lead_min(catalog.PRODUCTS[s.order.product_id]) for s in book}
    tight = [order for order, value in slack.items() if value <= 0.3 * horizon]
    assert 1 <= len(tight) <= 4
    assert any(s.order.due_min > horizon for s in book)


def test_due_dates_are_relative_to_now():
    later = seed_orders(42, now=1000.0)
    for early, shifted in zip(seed_orders(42), later):
        assert shifted.order.due_min == early.order.due_min + 1000
        assert shifted.order.created_min == 1000.0


def test_stage_load_counts_each_stage():
    lot = Lot("L-1", "A-1", "ST-200", 8)
    load = stage_load_min([lot])
    # One lot alone: only PR2 can press it, but the estimate shares the press stage over both presses on purpose.
    assert load[catalog.PRESS] == catalog.planned_minutes(catalog.PRESS, 8 * 11.0) / 2
    assert load[catalog.CAST] == catalog.planned_minutes(catalog.CAST, catalog.MELT_MIN)
    assert load_estimate_min([lot]) == max(load.values())
    assert load_estimate_min([]) == 0.0


def test_next_supply_tops_up_to_the_target_and_respects_the_cap():
    rng = random.Random(1)
    open_lots = [Lot(f"L-{i:05d}", "A-1", "FR-60", 8) for i in range(1, 7)]
    created = next_supply(rng, now=500.0, open_lots=open_lots, next_order_no=2000, next_lot_no=100)
    assert created
    total = len(open_lots) + sum(len(s.lots) for s in created)
    assert 18 <= total <= SUPPLY_CAP_LOTS
    assert [s.order.id for s in created] == [f"A-{2000 + i}" for i in range(len(created))]
    for seeded in created:
        lead = catalog.min_lead_min(catalog.PRODUCTS[seeded.order.product_id])
        assert seeded.order.due_min - 500.0 >= lead
        assert seeded.order.created_min == 500.0
    full = [Lot(f"L-{i:05d}", "A-1", "FR-60", 8) for i in range(1, 25)]
    assert next_supply(rng, now=0.0, open_lots=full, next_order_no=1, next_lot_no=1) == []
    nearly = [Lot(f"L-{i:05d}", "A-1", "FR-60", 8) for i in range(1, SUPPLY_CAP_LOTS)]
    capped = next_supply(rng, now=0.0, open_lots=nearly, next_order_no=1, next_lot_no=100, target_lots=SUPPLY_CAP_LOTS)
    assert sum(len(s.lots) for s in capped) == 1


class States(dict):
    def __call__(self, op_id: str) -> str:
        return self.get(op_id, "pending")


def make_book() -> tuple[OrderBook, Order]:
    book = OrderBook()
    order = Order("A-1001", "FR-60", 100, 900.0, 1, 0.0)
    book.add(order, split_order(order, 1))
    return book, order


def test_order_status_from_packed_pieces():
    book, order = make_book()
    record = book.orders[order.id]
    states = States()
    assert book.status(record, states, 0) == "offen"
    states[record.lots[0].op_id(catalog.PRESS)] = "running"
    assert book.status(record, states, 0) == "in_arbeit"
    for lot in record.lots:
        states[lot.op_id(catalog.PACK)] = "done"
    assert book.status(record, states, 100) == "fertig"
    assert book.status(record, states, 120) == "fertig"
    assert book.status(record, states, 99) == "unterlieferung"


def test_order_row_lateness_uses_the_plan_until_it_is_finished():
    book, order = make_book()
    record = book.orders[order.id]
    states = States()
    row = book.order_row(record, op_state=states, delivered=0, planned_completion=950, op_machine={}, planned_machine={})
    assert row["planned_completion"] == 950 and row["lateness_min"] == 50 and row["late"] is True and row["completed_min"] is None
    row = book.order_row(record, op_state=states, delivered=0, planned_completion=None, op_machine={}, planned_machine={})
    assert row["planned_completion"] is None and row["lateness_min"] is None and row["late"] is False
    book.mark_finished(record, 880.0)
    row = book.order_row(record, op_state=states, delivered=100, planned_completion=950, op_machine={}, planned_machine={})
    assert row["completed_min"] == 880.0 and row["lateness_min"] == -20 and row["late"] is False


def test_lot_row_follows_the_route():
    book, order = make_book()
    lot = book.orders[order.id].lots[0]
    states = States()
    planned = {lot.op_id(catalog.PRESS): "PR1", lot.op_id(catalog.AGE): "OF2"}
    row = book.lot_row(lot, states, {}, planned)
    assert (row["stage"], row["state"], row["machine"]) == ("press", "wartend", "PR1")
    states[lot.op_id(catalog.PRESS)] = "running"
    row = book.lot_row(lot, states, {lot.op_id(catalog.PRESS): "PR2"}, planned)
    assert (row["stage"], row["state"], row["machine"]) == ("press", "laeuft", "PR2")
    states[lot.op_id(catalog.PRESS)] = "done"
    row = book.lot_row(lot, states, {}, planned)
    assert (row["stage"], row["state"], row["machine"]) == ("age", "wartend", "OF2")
    for kind in catalog.LOT_ROUTE:
        states[lot.op_id(kind)] = "done"
    row = book.lot_row(lot, states, {}, planned)
    assert (row["stage"], row["state"], row["machine"]) == ("fertig", "fertig", None)


def test_open_lots_and_counts():
    book, order = make_book()
    record = book.orders[order.id]
    states = States()
    first, *others = record.lots
    assert book.lots_open == len(record.lots) and book.lots_done == 0
    book.note_lot_finished(first.id, 100.4)
    assert book.lots_open == len(others) and book.lots_done == 1
    assert first not in book.open_lots(states)
    assert book.all_lots_finished(record) is False
    assert [info.completed_lots_end for info in book.order_infos(book.open_lots(states))] == [101]
    if others:
        states[others[0].op_id(catalog.PACK)] = "done"
        assert others[0] not in book.open_lots(states), "the tracker's word counts, also ahead of the event history"
        assert others[0] not in book.open_lots_of_snapshot([others[0].op_id(catalog.PACK)])
    for lot in others:
        book.note_lot_finished(lot.id, 200.0)
    assert book.all_lots_finished(record) is True
    book.mark_finished(record, 200.0)
    assert book.open_orders() == [] and book.finished_orders() == [record]
    assert base_row(record)["completed_min"] == 200.0


def test_book_counters_never_go_back_when_restoring():
    book = OrderBook()
    order = Order("A-2500", "FR-60", 16, 900.0)
    book.add(order, [Lot("L-00777", order.id, "FR-60", 8)])
    created = book.create("FR-60", 16, 900.0, 1, 0.0)
    assert created.order.id == "A-2501"
    assert created.lots[0].id == "L-00778"


def test_missing_lots_are_those_with_an_unknown_unstarted_press():
    book, order = make_book()
    record = book.orders[order.id]
    states = States()
    assert book.missing_from(lambda op_id: False, states) == list(record.lots)
    assert book.missing_from(lambda op_id: True, states) == []
    states[record.lots[0].op_id(catalog.PRESS)] = "running"
    assert record.lots[0] not in book.missing_from(lambda op_id: False, states)


def _crash_until_marker(marker: str) -> str:
    """Pool job: kills its worker the first time, as a crashed solver would, and works once the pool was rebuilt."""
    path = Path(marker)
    if not path.exists():
        path.write_text("x", encoding="utf-8")
        os._exit(7)
    return "fertig"


async def test_pool_is_rebuilt_once_and_says_so(tmp_path):
    messages: list[str] = []
    pool = ManagedPool("test", 1, on_retry=messages.append)
    try:
        assert await pool.run(_crash_until_marker, str(tmp_path / "marker")) == "fertig"
        assert len(messages) == 1 and "neu aufgebaut" in messages[0]
        with pytest.raises(BrokenProcessPool):
            await pool.run(os._exit, 7)
        assert len(messages) == 2
    finally:
        pool.shutdown()


async def test_pool_reports_whether_the_plant_module_got_into_a_worker():
    pool = ManagedPool("test", 1)
    try:
        await pool.warm_up()
        assert pool.plant_module_loaded is False
        pid, loaded = await pool.run(worker_ping)
        assert loaded is False and pid != os.getpid()
    finally:
        pool.shutdown()


def test_workers_die_with_their_parent():
    script = (
        "import asyncio, json, os\n"
        "from werkhalle.mes.pools import ManagedPool, worker_ping\n"
        "async def main():\n"
        "    pool = ManagedPool('test', 2)\n"
        "    loop = asyncio.get_running_loop()\n"
        "    pid, _ = await pool.run(worker_ping)\n"
        "    print(json.dumps({'parent': os.getpid(), 'worker': pid}), flush=True)\n"
        "    await asyncio.sleep(120)\n"
        "if __name__ == '__main__':\n"
        "    asyncio.run(main())\n"
    )
    env = dict(os.environ, PYTHONPATH=str(SRC), PYTHONUTF8="1")
    process = subprocess.Popen([sys.executable, "-c", script], env=env, stdout=subprocess.PIPE, stdin=subprocess.DEVNULL, text=True)
    try:
        line = process.stdout.readline()
        assert line, "the helper did not report its pids"
        pids = json.loads(line)
        assert process_created(pids["worker"]) is not None
        # Only the interpreter, not its tree: this is what a crash or a hard kill of the MES looks like to a worker.
        subprocess.run(["taskkill", "/PID", str(pids["parent"]), "/F"], capture_output=True, check=False)
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline and process_created(pids["worker"]) is not None:
            time.sleep(0.1)
        assert process_created(pids["worker"]) is None, "a worker outlived its parent"
    finally:
        subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"], capture_output=True, check=False)
        process.wait(timeout=20)
        process.stdout.close()


def test_mes_entry_point_does_nothing_on_import():
    """A spawn worker imports it as __mp_main__. Starting a server there would be a fork bomb of a kind."""
    code = "import sys\nimport werkhalle.mes.__main__\nassert 'uvicorn' not in sys.modules\nassert 'asyncua' not in sys.modules\nprint('ok')\n"
    env = dict(os.environ, PYTHONPATH=str(SRC), PYTHONUTF8="1")
    done = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stderr
    assert done.stdout.strip() == "ok"



class _FakeChild:
    name = "plant"
    label = "Die Anlage"
    log_path = Path("nirgendwo.log")

    def __init__(self, stop_when) -> None:
        self.stop_when = stop_when

    def read_new_errors(self) -> list[str]:
        return []

    def exit_code(self) -> int | None:
        return 3 if self.stop_when() else None

    def last_lines(self) -> list[str]:
        return ["letzte Zeile"]


@pytest.mark.parametrize("no_browser", [False, True])
def test_browser_opens_once_and_only_after_the_first_plan(monkeypatch, capsys, no_browser):
    opened: list[str] = []
    replies = iter([None, {"ok": False}, {"ok": True, "plan_revision": 0, "plant_module_loaded": False},
                    {"ok": True, "plan_revision": 1, "plant_module_loaded": False}])
    asked: list[object] = []

    def fake_health(_port):
        assert not opened, "the browser opens on the first plan, and the launcher then stops asking"
        reply = next(replies, {"ok": True, "plan_revision": 1, "plant_module_loaded": False})
        asked.append(reply)
        return reply

    monkeypatch.setattr(launcher, "health", fake_health)
    monkeypatch.setattr(launcher, "POLL_S", 0.01)
    monkeypatch.setattr(launcher.webbrowser, "open", opened.append)
    child = _FakeChild(lambda: len(asked) >= 4 and (bool(opened) or no_browser))

    code = launcher._supervise([child], 1234, "http://127.0.0.1:1234/", SimpleNamespace(no_browser=no_browser))
    assert code == 1
    assert opened == ([] if no_browser else ["http://127.0.0.1:1234/"])
    assert len(asked) == 4, "once there is a plan, the launcher stops asking"
    output = capsys.readouterr().out
    assert "Leitstand bereit" in output and "unerwartet beendet" in output and "letzte Zeile" in output


@pytest.mark.parametrize("failure", [ValueError("No closing quotation"), False])
def test_a_browser_that_will_not_open_only_costs_a_message(monkeypatch, capsys, failure):
    def broken_open(_url):
        if isinstance(failure, Exception):
            raise failure
        return failure

    monkeypatch.setattr(launcher.webbrowser, "open", broken_open)
    launcher._open_browser("http://127.0.0.1:1234/")
    assert "http://127.0.0.1:1234/ von Hand" in capsys.readouterr().out


def test_pick_port_moves_on_past_a_taken_port(capsys):
    with socket.socket() as blocker:
        blocker.bind(("127.0.0.1", 0))
        blocker.listen()
        taken = blocker.getsockname()[1]
        chosen = launcher.pick_port(taken, set(), "HTTP")
        assert chosen != taken and chosen > taken
        assert f"Port {taken} (HTTP) ist belegt, nehme {chosen}." in capsys.readouterr().out
        assert launcher.pick_port(chosen, {chosen}, "OPC UA") != chosen, "the second child must not get the first one's port"


def test_a_port_is_free_only_if_a_plain_bind_works():
    with socket.socket() as blocker:
        blocker.bind(("127.0.0.1", 0))
        taken = blocker.getsockname()[1]
        assert launcher.port_is_free(taken) is False
    assert launcher.port_is_free(taken) is True


def test_child_env_is_what_the_plan_says(monkeypatch):
    monkeypatch.setenv("PYTHONIOENCODING", "latin-1")
    env = launcher.child_env()
    assert env["PYTHONUTF8"] == "1"
    assert env["PYTHONPATH"] == str(SRC)
    assert Path(env["PYTHONPATH"]).is_absolute()
    assert "PYTHONIOENCODING" not in env


def test_leftovers_are_killed_only_if_they_really_are_ours(tmp_path):
    env = dict(launcher.child_env())
    sleeper = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"], env=env, stdin=subprocess.DEVNULL,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    other = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"], env=env, stdin=subprocess.DEVNULL,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        pid_file = tmp_path / "werkhalle.pid"
        pid_file.write_text(json.dumps({
            "mes": {"pid": sleeper.pid, "created": process_created(sleeper.pid)},
            # The pid of a live process, but a creation time that does not match: its pid was reused by something else.
            "plant": {"pid": other.pid, "created": 1},
            "broken": {"pid": "x"},
        }), encoding="utf-8")
        launcher.clear_leftovers(pid_file)
        assert sleeper.wait(timeout=15) is not None
        assert other.poll() is None, "a process that merely has a recorded pid must live"
        assert not pid_file.exists()
        launcher.clear_leftovers(pid_file)
    finally:
        for process in (sleeper, other):
            subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"], capture_output=True, check=False)
            process.wait(timeout=15)


def test_open_order_lateness_follows_the_real_clock():
    book = OrderBook()
    order = Order("A-1", "FR-60", 100, 900.0, 1, 0.0)
    record = book.add(order, split_order(order, 1))
    states = lambda op_id: "pending"  # noqa: E731
    row = book.order_row(record, op_state=states, delivered=0, planned_completion=800, op_machine={}, planned_machine={}, now=950.0)
    assert row["late"] is True and row["lateness_min"] == 50 and row["planned_completion"] == 800
    row = book.order_row(record, op_state=states, delivered=0, planned_completion=None, op_machine={}, planned_machine={}, now=901.0)
    assert row["late"] is True and row["lateness_min"] == 1
    row = book.order_row(record, op_state=states, delivered=0, planned_completion=None, op_machine={}, planned_machine={}, now=500.0)
    assert row["late"] is False
    book.mark_finished(record, 950.0)
    assert book.finished_late == 1


def test_oee_trend_stays_inside_the_8h_window_and_a_running_fault_counts():
    from werkhalle.mes.oee import CounterSample, OeeTracker, difference

    tracker = OeeTracker()
    for t in range(0, 2001, 10):
        tracker.update("PR1", float(t), {"run": t * 0.5, "idle": t * 0.5, "fault_count": 1}, status="RUN")
    assert tracker.trend("start", ["PR1"])["t"][0] == 60
    assert min(tracker.trend("8h", ["PR1"])["t"]) >= 2000 - 480
    baseline = CounterSample(t=0.0, fault=10.0, fault_count=1, in_fault=True)
    assert difference(baseline, CounterSample(t=100.0, fault=110.0, fault_count=1)).fault_count == 1
