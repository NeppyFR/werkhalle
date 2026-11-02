"""Development stand-in for the Werkhalle MES. Dev only: nothing under src/ may ever import this file.

    .venv/Scripts/python.exe tools/mock_mes.py --port 8399

Serves src/werkhalle/web and implements every endpoint of docs/API.md with a small time-stepped fake plant, so the
UI can be built and verified before the real plant and MES run. The data shapes match docs/API.md key for key; the
numbers are plausible, not physically faithful. There is no OPC UA here, and no CP-SAT: "solvers" are a greedy list
scheduler with different lot orders. tests/test_mock_mes.py holds the shapes to the contract.

Extra endpoints that exist only here, for exercising rare UI states:
    POST /api/_mock/force_stale           the next accept or apply answers 409 veraltet
    POST /api/_mock/link {"connected":false,"stale":false}   pretend the OPC UA link dropped or went stale
"""
from __future__ import annotations

import argparse
import asyncio
import copy
import json
import math
import mimetypes
import random
import statistics
import sys
import time
import uuid
from collections import deque
from contextlib import asynccontextmanager
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import uvicorn  # noqa: E402
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect  # noqa: E402
from fastapi.exceptions import RequestValidationError  # noqa: E402
from fastapi.responses import JSONResponse  # noqa: E402
from fastapi.staticfiles import StaticFiles  # noqa: E402
from starlette.exceptions import HTTPException as StarletteHTTPException  # noqa: E402

from werkhalle.domain import catalog  # noqa: E402
from werkhalle.domain.orders import Order, split_order  # noqa: E402

WEB_DIR = SRC / "werkhalle" / "web"
LAG = catalog.TRANSPORT_MIN
FACTORS = (0, 60, 120, 240, 480)
SOLVER_LABEL = {"cpsat": "CP-SAT", "anneal": "Simulated Annealing", "keep": "Aktueller Plan"}
FAULT_FLOOR = 15.0
STAGE_OF_KIND = {"press": "press", "age": "age", "saw": "saw", "pack": "pack"}
PREV_KIND = {"age": "press", "saw": "age", "pack": "saw"}


def spec_of(machine_id: str) -> catalog.MachineSpec:
    return catalog.MACHINES[machine_id]


class Machine:
    def __init__(self, machine_id: str):
        spec = spec_of(machine_id)
        self.id = machine_id
        self.status = "IDLE"
        self.status_since = 0.0
        self.setup_class = catalog.INITIAL_SETUP_CLASS[machine_id]
        self.unit: dict | None = None
        self.setup_left = 0.0
        self.work_left = 0.0
        self.work_total = 0.0
        self.progress = 0.0
        self.temperature = spec.temp_idle_c
        self.energy = 0.0
        self.total = 0
        self.good = 0
        self.scrap = 0
        self.run = 0.0
        self.setup = 0.0
        self.fault = 0.0
        self.idle = 0.0
        self.ideal = 0.0
        self.no_demand = 0.0
        self.starved = 0.0
        self.alarm_seq = 0
        self.alarm = {"active": False, "id": 0, "code": "", "text": "", "severity": 0}
        self.est_repair = 0.0
        self.repair_left = 0.0
        self.fault_started = 0.0
        self.fault_count = 0
        self.resume_status = "IDLE"
        self.waiting_on: str | None = "Kein Auftrag"
        self.next_op: str | None = None
        self.warn_until = 0.0
        self.warn_delta = 0.0
        self.slow_until = 0.0
        self.slow_factor = 1.0
        self.power = spec.power_idle_kw
        self.setpoint = spec.temp_idle_c
        self.trend: dict | None = None


class Sim:
    """A fake plant plus its dispatcher. `fork()` deep-copies it for twin runs, like a snapshot would."""

    def __init__(self, seed=1, random_failures=True, auto_supply=True, run_id="r-mock", stochastic=True):
        self.rng = random.Random(seed)
        self.run_id = run_id
        self.t = 0.0
        self.random_failures = random_failures
        self.auto_supply = auto_supply
        self.stochastic = stochastic
        self.machines = {m: Machine(m) for m in catalog.MACHINE_ORDER}
        self.store: dict[str, list[tuple[str, str]]] = {a: [] for a in catalog.ALLOYS}
        self.counter = dict(melt=0, strand=0, basket=0, load=0, package=0, lot=0, order=1000, transport=0, melt_plan=0, batch=0, event=0)
        self.orders: dict[str, dict] = {}
        self.lots: dict[str, dict] = {}
        self.age_in: list[dict] = []
        self.saw_in: list[dict] = []
        self.pack_in: list[dict] = []
        self.transports: list[dict] = []
        self.ship = dict(packages=0, pieces=0, kg=0.0, lots_total=0, last_lots=[])
        self.op_state: dict[str, str] = {}
        self.history: list[dict] = []
        self.queues: dict[str, list[dict]] = {m: [] for m in catalog.MACHINE_ORDER}
        self.assign_by_op: dict[str, dict] = {}
        self.order_energy: dict[str, float] = {}
        self.samples: list[dict] = []
        self.next_sample = 0.0
        self.scheduled: list[dict] = []
        self.stalled = False
        self.sink = None
        self.fork_no = 0
        self._seed_stock()

    # ---------------------------------------------------------------- forks
    def fork(self, stochastic=False, seed=1) -> "Sim":
        sink, self.sink = self.sink, None
        samples, self.samples = self.samples, []
        try:
            clone = copy.deepcopy(self)
        finally:
            self.sink = sink
            self.samples = samples
        clone.random_failures = False
        clone.auto_supply = False
        clone.stochastic = stochastic
        clone.rng = random.Random(seed)
        clone.next_sample = float("inf")
        # A candidate from this fork may be installed later, so its new melt ids must never meet ones minted here.
        self.fork_no += 1
        clone.counter["melt_plan"] += self.fork_no * 1000
        return clone

    # ---------------------------------------------------------------- ids and small helpers
    def _next(self, key: str) -> int:
        self.counter[key] += 1
        return self.counter[key]

    def _emit(self, name: str, *args, **kwargs):
        if self.sink is not None:
            getattr(self.sink, name)(*args, **kwargs)

    def _seed_stock(self):
        for alloy, count in catalog.INITIAL_BILLET_STOCK.items():
            remaining = count
            while remaining > 0:
                n = min(remaining, catalog.BILLETS_PER_MELT)
                melt_no = self._next("melt")
                melt_id = f"S-{melt_no:05d}"
                billets = [f"B-{melt_no:05d}-{i + 1:02d}" for i in range(n)]
                self.store[alloy].extend((b, melt_id) for b in billets)
                self._emit("melt", melt_id, alloy, billets, -catalog.MELT_MIN, 712.0)
                remaining -= n

    def attach_sink(self, sink):
        self.sink = sink
        for alloy, billets in self.store.items():
            by_melt: dict[str, list[str]] = {}
            for billet, melt in billets:
                by_melt.setdefault(melt, []).append(billet)
            for melt, ids in by_melt.items():
                sink.melt(melt, alloy, ids, -catalog.MELT_MIN, 712.0)

    # ---------------------------------------------------------------- orders
    def add_order(self, product_id: str, qty_bars: int, due_min: float, weight=1) -> dict:
        order_id = f"A-{self._next('order')}"
        order = Order(order_id, product_id, qty_bars, due_min, weight, self.t)
        lots = split_order(order, self.counter["lot"] + 1)
        self.counter["lot"] += len(lots)
        row = dict(id=order_id, product_id=product_id, qty_bars=qty_bars, due_min=due_min, weight=weight,
                   created_min=round(self.t, 1), delivered_pieces=0, completed_min=None, lots=[l.id for l in lots])
        self.orders[order_id] = row
        for lot in lots:
            product = lot.product
            self.lots[lot.id] = dict(
                id=lot.id, order_id=order_id, product_id=product_id, n_billets=lot.n_billets,
                n_baskets=lot.n_baskets, n_packages=lot.n_packages, planned_bars=lot.planned_bars,
                stage="press", state="wartend", machine=None, baskets=[], bars=0, pieces=0, done_t=None,
                alloy=product.alloy, recipe=product.recipe,
            )
            for kind in catalog.LOT_ROUTE:
                self.op_state[lot.op_id(kind)] = "pending"
        self._emit("order", row)
        return row

    def generate_order(self) -> dict:
        ids = list(catalog.PRODUCTS)
        product_id = ids[self.counter["order"] % len(ids)]
        product = catalog.PRODUCTS[product_id]
        bars = product.bars_per_billet * self.rng.choice((8, 12, 16, 22))
        open_lots = sum(1 for l in self.lots.values() if l["stage"] != "fertig")
        lead = catalog.min_lead_min(product) + 200 + open_lots * self.rng.randint(45, 75)
        return self.add_order(product_id, bars, round(self.t + lead), self.rng.choice((1, 1, 1, 2)))

    def open_lots(self) -> list[dict]:
        return [l for l in self.lots.values() if l["stage"] != "fertig"]

    # ---------------------------------------------------------------- plan installation and dispatch
    def install(self, assignments: list[dict]):
        queues: dict[str, list[dict]] = {m: [] for m in catalog.MACHINE_ORDER}
        self.assign_by_op = {a["op_id"]: a for a in assignments}
        pending = sorted((a for a in assignments if a["state"] == "pending"), key=lambda a: (a["start"], a["op_id"]))
        for a in pending:
            self.op_state.setdefault(a["op_id"], "pending")
            q = queues[a["machine"]]
            if a["batch_id"] and q and q[-1].get("batch_id") == a["batch_id"]:
                q[-1]["op_ids"].append(a["op_id"])
                q[-1]["lot_ids"].append(a["lot_id"])
            else:
                q.append(dict(op_ids=[a["op_id"]], kind=a["kind"], batch_id=a["batch_id"],
                              lot_ids=[a["lot_id"]] if a["lot_id"] else [], setup_before=a["setup_before"]))
        self.queues = queues
        self.stalled = False

    def _head(self, machine_id: str) -> dict | None:
        q = self.queues[machine_id]
        while q and all(self.op_state.get(o) != "pending" for o in q[0]["op_ids"]):
            q.pop(0)
        return q[0] if q else None

    def _blocked_reason(self, unit: dict) -> str | None:
        kind = unit["kind"]
        if kind == "cast":
            return None
        if kind == "press":
            lot = self.lots[unit["lot_ids"][0]]
            return None if len(self.store[lot["alloy"]]) >= lot["n_billets"] else "Material"
        buffer = {"age": self.age_in, "saw": self.saw_in, "pack": self.pack_in}[kind]
        present = {b["lot_id"] for b in buffer}
        for lot_id in unit["lot_ids"]:
            if lot_id not in present:
                prev = self.assign_by_op.get(f"{lot_id}:{PREV_KIND[kind]}")
                if prev and self.machines[prev["machine"]].status == "FAULT":
                    return "Störung"
                return "Vorgänger"
        return None

    def _dispatch(self):
        for machine in self.machines.values():
            if machine.status != "IDLE":
                continue
            unit = self._head(machine.id)
            machine.next_op = unit["op_ids"][0] if unit else None
            if unit is None:
                machine.waiting_on = "Kein Auftrag"
                continue
            reason = self._blocked_reason(unit)
            machine.waiting_on = reason
            if reason is None:
                self._start(machine, unit)

    def _start(self, machine: Machine, head: dict):
        # A partly started load is reduced to its pending members, as the real dispatcher does.
        op_ids = [o for o in head["op_ids"] if self.op_state.get(o) == "pending"]
        lot_ids = [o.split(":")[0] for o in op_ids] if head["kind"] != "cast" else []
        unit = dict(head, op_ids=op_ids, lot_ids=lot_ids, started=self.t, energy=0.0, billets_done=0)
        kind = unit["kind"]
        for op in unit["op_ids"]:
            self.op_state[op] = "running"
        lots = [self.lots[l] for l in unit["lot_ids"]]
        if kind == "cast":
            alloy = unit["op_ids"][0].split("-")[1]
            unit.update(alloy=alloy, product_id=None)
            new_class = alloy
            ideal = catalog.MELT_MIN
        else:
            lot = lots[0]
            product = catalog.PRODUCTS[lot["product_id"]]
            unit.update(product_id=product.id)
            new_class = catalog.setup_class(kind, product)
            if kind == "press":
                moved = self.store[lot["alloy"]][: lot["n_billets"]]
                del self.store[lot["alloy"]][: lot["n_billets"]]
                unit.update(billets=moved, n_billets=lot["n_billets"], baskets_made=0, strands=[])
                ideal = catalog.ideal_press_min(product, machine.id, lot["n_billets"])
            elif kind == "age":
                ideal = float(catalog.RECIPES[product.recipe].soak_min)
                unit["baskets"] = sum(l["n_baskets"] for l in lots)
                self.age_in[:] = [b for b in self.age_in if b["lot_id"] not in unit["lot_ids"]]
            elif kind == "saw":
                ideal = lot["bars"] * catalog.SAW_MIN_PER_BAR
                self.saw_in[:] = [b for b in self.saw_in if b["lot_id"] not in unit["lot_ids"]]
            else:
                ideal = lot["n_packages"] * catalog.PACK_MIN_PER_PACKAGE
                self.pack_in[:] = [b for b in self.pack_in if b["lot_id"] not in unit["lot_ids"]]
        setup_min = catalog.setup_minutes(kind, machine.setup_class, new_class)
        noise = self.rng.uniform(0.97, 1.03) if self.stochastic and kind != "age" else 1.0
        machine.setup_class = new_class
        machine.setup_left = float(setup_min)
        machine.work_total = max(1.0, ideal * catalog.PLANNING_ALLOWANCE[kind] * noise)
        machine.work_left = machine.work_total
        unit["ideal_rate"] = (float(ideal) / machine.work_total) if kind != "age" else unit["baskets"] / spec_of(machine.id).capacity
        machine.progress = 0.0
        machine.unit = unit
        machine.waiting_on = None
        machine.next_op = None
        self._set_status(machine, "SETUP" if setup_min else "RUN")
        for lot in lots:
            lot.update(stage=STAGE_OF_KIND[kind], state="laeuft", machine=machine.id)
        self._emit("op_started", machine.id, unit)

    def _set_status(self, machine: Machine, status: str):
        machine.status = status
        machine.status_since = self.t

    # ---------------------------------------------------------------- stepping
    def step(self, dt: float):
        self.t += dt
        self._arrivals()
        for event in [e for e in self.scheduled if e["at"] <= self.t]:
            self.scheduled.remove(event)
            if event["type"] == "fault":
                self.inject_fault(event["machine"], event.get("code", ""), event.get("repair_min", 0.0))
            elif event["type"] == "slowdown":
                machine = self.machines[event["machine"]]
                machine.slow_factor = event["factor"]
                machine.slow_until = self.t + event["duration"]
        for machine in self.machines.values():
            self._step_machine(machine, dt)
        self._dispatch()
        if self.random_failures:
            self._random_events(dt)
        if self.auto_supply:
            self._auto_supply()
        if self.t >= self.next_sample:
            self._take_sample()
            self.next_sample = self.t + 30.0
        running = any(m.unit for m in self.machines.values()) or self.transports or any(m.status == "FAULT" for m in self.machines.values())
        self.stalled = (not running) and any(self.op_state.get(a) == "pending" for a in self.assign_by_op)

    def _step_machine(self, machine: Machine, dt: float):
        spec = spec_of(machine.id)
        status = machine.status
        if status == "FAULT":
            machine.fault += dt
            machine.repair_left -= dt
            setpoint, power = spec.temp_fault_c, spec.power_fault_kw
            if machine.repair_left <= 0:
                self.repair(machine.id)
        elif status == "SETUP":
            machine.setup += dt
            machine.setup_left -= dt
            setpoint = self._recipe_temp(machine) if spec.kind == catalog.AGE else spec.temp_setup_c
            power = spec.power_setup_kw
            if machine.setup_left <= 0:
                self._set_status(machine, "RUN")
                self._emit("phase", machine.id, "RUN")
        elif status == "RUN":
            rate = machine.slow_factor if machine.slow_until > self.t else 1.0
            machine.run += dt
            machine.work_left -= dt * rate
            machine.progress = min(1.0, 1 - machine.work_left / machine.work_total)
            machine.ideal += dt * machine.unit["ideal_rate"] * rate
            setpoint = self._recipe_temp(machine) if spec.kind == catalog.AGE else spec.temp_run_c
            power = spec.power_run_kw * (1 + 0.03 * math.sin(self.t * 0.21))
            if spec.kind == catalog.PRESS:
                self._press_progress(machine)
            if machine.work_left <= 0:
                self._finish(machine)
        else:
            machine.idle += dt
            if machine.waiting_on == "Kein Auftrag":
                machine.no_demand += dt
            elif machine.waiting_on:
                machine.starved += dt
            setpoint, power = spec.temp_idle_c, spec.power_idle_kw
        excursion = 0.0
        if machine.warn_until > self.t:
            excursion = machine.warn_delta
        elif machine.warn_until:
            machine.warn_until = 0.0
            if machine.alarm["active"] and machine.alarm["severity"] == 500:
                self._clear_alarm(machine)
        machine.setpoint = setpoint
        machine.temperature += (setpoint + excursion - machine.temperature) * (1 - math.exp(-dt / spec.temp_tau_min))
        machine.power = power
        machine.energy += power * dt / 60.0
        if machine.unit is not None:
            machine.unit["energy"] = machine.unit.get("energy", 0.0) + power * dt / 60.0

    def _recipe_temp(self, machine: Machine) -> float:
        cls = machine.setup_class
        return catalog.RECIPES[cls].temp_c if cls in catalog.RECIPES else spec_of(machine.id).temp_run_c

    def _press_progress(self, machine: Machine):
        unit = machine.unit
        lot = self.lots[unit["lot_ids"][0]]
        product = catalog.PRODUCTS[lot["product_id"]]
        done = min(unit["n_billets"], int(machine.progress * unit["n_billets"] + 1e-9))
        while unit["billets_done"] < done:
            billet_id, melt_id = unit["billets"][unit["billets_done"]]
            unit["billets_done"] += 1
            bars = product.bars_per_billet
            scrap = sum(1 for _ in range(bars) if self.rng.random() < catalog.PRESS_SCRAP_RATE) if self.stochastic else 0
            good = bars - scrap
            machine.total += bars
            machine.good += good
            machine.scrap += scrap
            lot["bars"] += good
            strand_id = f"ST-{self._next('strand'):05d}"
            unit["strands"].append(dict(id=strand_id, bars=good))
            self._emit("strand", billet_id, strand_id, good, self.t, machine.id, product)
            if len(unit["strands"]) % product.strands_per_basket == 0:
                unit["baskets_made"] += 1

    def _finish(self, machine: Machine):
        unit = machine.unit
        kind = unit["kind"]
        for op in unit["op_ids"]:
            self.op_state[op] = "done"
            base = self.assign_by_op.get(op, {})
            self.history.append(dict(
                op_id=op, kind=kind, machine=machine.id, start=int(unit["started"]), end=int(math.ceil(self.t)),
                setup_before=int(base.get("setup_before", 0)), batch_id=unit.get("batch_id"),
                lot_id=op.split(":")[0] if kind != "cast" else None,
                order_id=self.lots[op.split(":")[0]]["order_id"] if kind != "cast" else None,
                product_id=unit.get("product_id"), state="done", frozen=False))
        del self.history[:-360]
        machine.unit = None
        machine.work_left = 0.0
        machine.setup_left = 0.0
        machine.progress = 0.0
        self._set_status(machine, "IDLE")
        lots = [self.lots[l] for l in unit["lot_ids"]]
        for lot in lots:
            share = unit.get("energy", 0.0) / max(1, len(lots))
            self.order_energy[lot["order_id"]] = self.order_energy.get(lot["order_id"], 0.0) + share
        if kind == "cast":
            melt_no = self._next("melt")
            melt_id = f"S-{melt_no:05d}"
            billets = [f"B-{melt_no:05d}-{i + 1:02d}" for i in range(catalog.BILLETS_PER_MELT)]
            machine.total += len(billets)
            machine.good += len(billets)
            self._emit("melt", melt_id, unit["alloy"], billets, self.t - catalog.MELT_MIN, machine.temperature)
            self._transport("billets", None, len(billets), machine.id, "BILLET_STORE",
                            dict(billets=billets, melt_id=melt_id, alloy=unit["alloy"]))
        elif kind == "press":
            lot = lots[0]
            product = catalog.PRODUCTS[lot["product_id"]]
            per_basket = product.strands_per_basket
            strands = unit["strands"]
            for i in range(0, len(strands), per_basket):
                group = strands[i:i + per_basket]
                basket = dict(id=f"K-{self._next('basket'):05d}", bars=sum(s["bars"] for s in group), strands=group)
                lot["baskets"].append(basket)
                self._emit("basket", basket, self.t)
            lot.update(stage="age", state="wartend", machine=None)
            self._transport("baskets", lot["id"], len(lot["baskets"]), machine.id, "AGE_IN", dict(stage="age"))
        elif kind == "age":
            load_id = f"O-{self._next('load'):05d}"
            basket_ids = [b["id"] for lot in lots for b in lot["baskets"]]
            machine.total += len(basket_ids)
            machine.good += len(basket_ids)
            self._emit("load", load_id, basket_ids, unit.get("batch_id"), self.t, machine.id)
            for lot in lots:
                lot.update(stage="saw", state="wartend", machine=None)
                self._transport("baskets", lot["id"], len(lot["baskets"]), machine.id, "SAW_IN", dict(stage="saw"))
        elif kind == "saw":
            lot = lots[0]
            scrap = sum(1 for _ in range(lot["bars"]) if self.rng.random() < catalog.SAW_SCRAP_RATE) if self.stochastic else 0
            machine.total += lot["bars"]
            machine.good += lot["bars"] - scrap
            machine.scrap += scrap
            lot["pieces"] = lot["bars"] - scrap
            lot.update(stage="pack", state="wartend", machine=None)
            self._transport("pieces", lot["id"], lot["pieces"], machine.id, "PACK_IN", dict(stage="pack"))
        elif kind == "pack":
            lot = lots[0]
            product = catalog.PRODUCTS[lot["product_id"]]
            remaining = lot["pieces"]
            packages = []
            while remaining > 0:
                pieces = min(remaining, product.bars_per_package)
                packages.append(dict(id=f"P-{self._next('package'):05d}", pieces=pieces))
                remaining -= pieces
            machine.total += len(packages)
            machine.good += len(packages)
            self._emit("packages", lot, packages, self.t)
            lot.update(machine=None, state="laeuft")
            self._transport("packages", lot["id"], len(packages), machine.id, "SHIP", dict(stage="ship", pieces=lot["pieces"], n=len(packages)))
        self._emit("op_finished", machine.id, unit, kind)

    def _transport(self, kind, lot_id, count, source, target, payload):
        self.transports.append({
            "id": f"T-{self._next('transport')}", "kind": kind, "lot_id": lot_id, "count": count,
            "from": source, "to": target, "t0": round(self.t, 2), "t1": round(self.t + LAG, 2), "_payload": payload})

    def _arrivals(self):
        for item in [x for x in self.transports if x["t1"] <= self.t]:
            self.transports.remove(item)
            payload = item["_payload"]
            target = item["to"]
            if target == "BILLET_STORE":
                self.store[payload["alloy"]].extend((b, payload["melt_id"]) for b in payload["billets"])
            elif target == "AGE_IN":
                self.age_in.append(dict(lot_id=item["lot_id"], baskets=item["count"]))
            elif target == "SAW_IN":
                self.saw_in.append(dict(lot_id=item["lot_id"], baskets=item["count"]))
            elif target == "PACK_IN":
                self.pack_in.append(dict(lot_id=item["lot_id"], pieces=item["count"]))
            elif target == "SHIP":
                lot = self.lots[item["lot_id"]]
                product = catalog.PRODUCTS[lot["product_id"]]
                self.ship["packages"] += payload["n"]
                self.ship["pieces"] += payload["pieces"]
                self.ship["kg"] = round(self.ship["kg"] + payload["pieces"] * product.kg_per_bar, 1)
                self.ship["lots_total"] += 1
                self.ship["last_lots"] = (self.ship["last_lots"] + [lot["id"]])[-20:]
                lot.update(stage="fertig", state="fertig", machine=None, done_t=self.t)
                order = self.orders[lot["order_id"]]
                order["delivered_pieces"] += payload["pieces"]
                if all(self.lots[l]["stage"] == "fertig" for l in order["lots"]):
                    order["completed_min"] = round(self.t, 1)
                self._emit("order_changed", order)

    # ---------------------------------------------------------------- faults and alarms
    def _raise_alarm(self, machine: Machine, code: str, text: str, severity: int):
        machine.alarm_seq += 1
        machine.alarm = {"active": True, "id": machine.alarm_seq, "code": code, "text": text, "severity": severity}
        self._emit("alarm_raised", machine)

    def _clear_alarm(self, machine: Machine):
        self._emit("alarm_cleared", machine)
        machine.alarm = {"active": False, "id": machine.alarm["id"], "code": "", "text": "", "severity": 0}

    def inject_fault(self, machine_id: str, code: str = "", repair_min: float = 0.0, injected_wall_ts=None) -> dict:
        machine = self.machines[machine_id]
        if machine.status == "FAULT":
            return dict(ok=False, reason="ALREADY_FAULTED")
        fault = catalog.fault_spec(machine_id, code or None)
        machine.resume_status = machine.status
        machine.est_repair = float(repair_min or fault.repair_mean_min)
        if repair_min:
            machine.repair_left = float(repair_min)
        elif self.stochastic:
            machine.repair_left = self.rng.lognormvariate(math.log(fault.repair_mean_min), 0.35)
        else:
            machine.repair_left = fault.repair_mean_min
        machine.fault_started = self.t
        machine.fault_count += 1
        machine.warn_until = 0.0
        self._set_status(machine, "FAULT")
        self._raise_alarm(machine, fault.code, fault.text, 900)
        self._emit("fault", machine, fault, injected_wall_ts)
        return dict(ok=True, reason="")

    def repair(self, machine_id: str) -> dict:
        machine = self.machines[machine_id]
        if machine.status != "FAULT":
            return dict(ok=False, reason="NOT_FAULTED")
        self._clear_alarm(machine)
        machine.est_repair = 0.0
        machine.repair_left = 0.0
        self._set_status(machine, machine.resume_status if machine.unit else "IDLE")
        self._emit("repaired", machine)
        return dict(ok=True, reason="")

    def _random_events(self, dt: float):
        for machine in self.machines.values():
            spec = spec_of(machine.id)
            if machine.status in ("RUN", "SETUP") and self.rng.random() < dt / spec.mtbf_operating_min:
                self.inject_fault(machine.id)
            elif machine.status != "FAULT" and not machine.alarm["active"] and self.rng.random() < dt / spec.warning.mtbf_min:
                machine.warn_until = self.t + spec.warning.duration_min
                machine.warn_delta = spec.warning.delta_k
                self._raise_alarm(machine, spec.warning.code, spec.warning.text, 500)

    def _auto_supply(self):
        if len(self.open_lots()) < 12:
            while len(self.open_lots()) < 18:
                self.generate_order()
            self._emit("supply_added")

    # ---------------------------------------------------------------- views
    def material(self) -> dict:
        in_machine = {}
        for machine in self.machines.values():
            if not machine.unit:
                continue
            unit = machine.unit
            if unit["kind"] == "press":
                billets_left, baskets = unit["n_billets"] - unit["billets_done"], unit["baskets_made"]
            elif unit["kind"] == "age":
                billets_left, baskets = 0, unit["baskets"]
            else:
                billets_left, baskets = 0, 0
            in_machine[machine.id] = dict(lot_ids=list(unit["lot_ids"]), kind=unit["kind"], billets_left=billets_left, baskets=baskets)
        return {
            "t": round(self.t, 2),
            "transports": [{k: v for k, v in x.items() if not k.startswith("_")} for x in self.transports],
            "buffers": {
                "BILLET_STORE": {a: len(v) for a, v in self.store.items()},
                "AGE_IN": [dict(b) for b in self.age_in],
                "SAW_IN": [dict(b) for b in self.saw_in],
                "PACK_IN": [dict(b) for b in self.pack_in],
                "SHIP": dict(self.ship, last_lots=list(self.ship["last_lots"])),
            },
            "in_machine": in_machine,
        }

    def frame(self) -> dict:
        return {
            "t": round(self.t, 2),
            "machines": {m.id: dict(status=m.status, progress=round(m.progress, 3), lot_ids=list(m.unit["lot_ids"]) if m.unit else [])
                         for m in self.machines.values()},
            "material": self.material(),
        }

    def op_view(self, machine: Machine) -> dict | None:
        unit = machine.unit
        if not unit:
            return None
        order_ids = sorted({self.lots[l]["order_id"] for l in unit["lot_ids"]})
        phase = "SETUP" if machine.setup_left > 0 else "RUN"
        return dict(op_ids=list(unit["op_ids"]), kind=unit["kind"], lot_ids=list(unit["lot_ids"]), order_ids=order_ids,
                    product_id=unit.get("product_id"), phase=phase, batch_id=unit.get("batch_id"))

    def counters_of(self, machine: Machine) -> dict:
        return dict(run=machine.run, setup=machine.setup, fault=machine.fault, idle=machine.idle, ideal=machine.ideal,
                    no_demand=machine.no_demand, starved=machine.starved, total=machine.total, good=machine.good,
                    scrap=machine.scrap, energy=machine.energy, faults=machine.fault_count)

    def _take_sample(self):
        self.samples.append(dict(
            t=round(self.t, 1),
            counters={m.id: self.counters_of(m) for m in self.machines.values()},
            kw={m.id: round(m.power, 1) for m in self.machines.values()},
        ))
        if len(self.samples) > 400:
            del self.samples[::2]


def oee_figures(c: dict) -> dict:
    """OEE from cumulative counters, following PLAN section 9, None where undefined."""
    no_demand = min(c["no_demand"], c["idle"])
    planned = c["run"] + c["setup"] + c["fault"] + c["idle"] - no_demand
    starved = min(c["starved"], max(0.0, planned))
    planned_net = planned - starved
    availability = c["run"] / planned if planned > 0 else None
    availability_net = c["run"] / planned_net if planned_net > 0 else None
    performance = min(1.0, c["ideal"] / c["run"]) if c["run"] > 0 else None
    quality = c["good"] / c["total"] if c["total"] > 0 else None
    defined = None not in (availability, performance, quality)
    oee_iso = availability * performance * quality if defined else None
    oee_net = availability_net * performance * quality if defined and availability_net is not None else None
    return dict(oee=oee_net, oee_iso=oee_iso, availability=availability, availability_net=availability_net,
                performance=performance, quality=quality, no_demand=no_demand, planned=planned, planned_net=planned_net,
                starved=starved)


def _round(value, digits=3):
    return None if value is None else round(value, digits)


# ------------------------------------------------------------------------------------------------- scheduling
def _push(start: int, duration: int, setup: int, downs: list[dict]) -> int:
    for d in sorted(downs, key=lambda x: x["start"]):
        if start - setup < d["end"] and start + duration > d["start"]:
            start = d["end"] + setup
    return start


def build_schedule(sim: Sim, *, priority: list[str] | None = None, keep_machines: dict[str, str] | None = None,
                   batch_prefix="B", extra_downtimes: list[dict] | None = None, no_batch_join=False) -> dict:
    """A greedy list schedule over everything still open, in the same shape as a real plan."""
    now = int(math.ceil(sim.t))
    rows: list[dict] = []
    downtimes: list[dict] = list(extra_downtimes or [])
    avail = {m: now for m in sim.machines}
    cls = {m.id: m.setup_class for m in sim.machines.values()}
    ready: dict[str, int] = {}
    supply = {a: [(now, len(v))] for a, v in sim.store.items()}
    for item in sim.transports:
        if item["to"] == "BILLET_STORE":
            supply[item["_payload"]["alloy"]].append((int(math.ceil(item["t1"])), item["count"]))
        elif item["lot_id"]:
            ready[item["lot_id"]] = int(math.ceil(item["t1"]))

    for machine in sim.machines.values():
        if machine.status == "FAULT":
            remaining = max(machine.est_repair - (sim.t - machine.fault_started), FAULT_FLOOR)
            end = now + int(math.ceil(remaining))
            downtimes.append(dict(machine=machine.id, start=int(machine.fault_started), end=end, reason=machine.alarm["code"]))
            avail[machine.id] = end
    downs_of = lambda m: [d for d in downtimes if d["machine"] == m]  # noqa: E731
    for machine in sim.machines.values():
        if machine.unit:
            remaining = machine.setup_left + machine.work_left
            end = avail[machine.id] + int(math.ceil(max(1.0, remaining)))
            unit = machine.unit
            for op in unit["op_ids"]:
                lot_id = op.split(":")[0] if unit["kind"] != "cast" else None
                rows.append(dict(op_id=op, kind=unit["kind"], machine=machine.id, start=int(unit["started"]), end=end,
                                 setup_before=0, batch_id=unit.get("batch_id"), lot_id=lot_id,
                                 order_id=sim.lots[lot_id]["order_id"] if lot_id else None,
                                 product_id=unit.get("product_id"), state="running", frozen=True))
                if lot_id:
                    ready[lot_id] = end + LAG
            if unit["kind"] == "cast":
                supply[unit["alloy"]].append((end + LAG, catalog.BILLETS_PER_MELT))
            avail[machine.id] = end
    for buffer in (sim.age_in, sim.saw_in, sim.pack_in):
        for entry in buffer:
            ready.setdefault(entry["lot_id"], now)

    open_lots = sim.open_lots()
    due = {o["id"]: o["due_min"] for o in sim.orders.values()}
    if priority is None:
        ordering = sorted(open_lots, key=lambda l: (due[l["order_id"]], l["id"]))
    else:
        rank = {lot_id: i for i, lot_id in enumerate(priority)}
        ordering = sorted(open_lots, key=lambda l: (rank.get(l["id"], len(rank) + 1), due[l["order_id"]], l["id"]))

    def row(lot, kind, machine, start, end, setup, batch=None):
        return dict(op_id=f"{lot['id']}:{kind}", kind=kind, machine=machine, start=start, end=end, setup_before=setup,
                    batch_id=batch, lot_id=lot["id"], order_id=lot["order_id"], product_id=lot["product_id"],
                    state="pending", frozen=False)

    # Press pass, with melts added whenever cumulative demand exceeds what has been cast.
    used = {a: 0 for a in catalog.ALLOYS}
    for lot in ordering:
        if sim.op_state.get(f"{lot['id']}:press") != "pending":
            continue
        product = catalog.PRODUCTS[lot["product_id"]]
        alloy = lot["alloy"]
        used[alloy] += lot["n_billets"]
        while sum(n for _, n in supply[alloy]) < used[alloy]:
            melt_id = f"M-{alloy}-{sim._next('melt_plan'):05d}"
            setup = catalog.setup_minutes(catalog.CAST, cls["GUSS"], alloy)
            duration = catalog.planned_minutes(catalog.CAST, catalog.MELT_MIN)
            start = _push(avail["GUSS"] + setup, duration, setup, downs_of("GUSS"))
            end = start + duration
            rows.append(dict(op_id=f"{melt_id}:cast", kind="cast", machine="GUSS", start=start, end=end, setup_before=setup,
                             batch_id=None, lot_id=None, order_id=None, product_id=None, state="pending", frozen=False))
            sim.op_state[f"{melt_id}:cast"] = "pending"
            avail["GUSS"], cls["GUSS"] = end, alloy
            supply[alloy].append((end + LAG, catalog.BILLETS_PER_MELT))
        cumulative = 0
        supply_at = now
        for at, n in sorted(supply[alloy]):
            cumulative += n
            if cumulative >= used[alloy]:
                supply_at = at
                break
        target_class = catalog.setup_class(catalog.PRESS, product)
        options = [keep_machines[lot["id"]]] if keep_machines and lot["id"] in keep_machines else list(product.press_min_per_billet)
        best = None
        for press in options:
            setup = catalog.setup_minutes(catalog.PRESS, cls[press], target_class)
            duration = catalog.planned_minutes(catalog.PRESS, catalog.ideal_press_min(product, press, lot["n_billets"]))
            start = _push(max(avail[press] + setup, supply_at), duration, setup, downs_of(press))
            if best is None or start + duration + setup < best[0]:
                best = (start + duration + setup, press, start, duration, setup)
        _, press, start, duration, setup = best
        rows.append(row(lot, "press", press, start, start + duration, setup))
        avail[press], cls[press] = start + duration, target_class
        ready[lot["id"]] = start + duration + LAG

    # Furnace pass: a later lot may join the last load of a furnace if the recipe fits and it arrives soon enough.
    loads: dict[str, dict] = {}
    n_batch = 0
    finished_rows: list[dict] = []
    age_lots = sorted((l for l in open_lots if sim.op_state.get(f"{l['id']}:age") == "pending"), key=lambda l: (ready.get(l["id"], now), l["id"]))
    for lot in age_lots:
        soak = catalog.planned_minutes(catalog.AGE, catalog.RECIPES[lot["recipe"]].soak_min)
        arrival = ready.get(lot["id"], now)
        size = lot["n_baskets"]
        best = None
        for furnace in catalog.MACHINES_BY_KIND[catalog.AGE]:
            load = loads.get(furnace)
            capacity = spec_of(furnace).capacity
            if not no_batch_join and load and load["recipe"] == lot["recipe"] and load["used"] + size <= capacity and arrival <= load["start"] + 45:
                start = max(load["start"], arrival)
                candidate = (start + soak - 5, furnace, "join", start, load["setup"])
            else:
                setup = catalog.setup_minutes(catalog.AGE, cls[furnace], lot["recipe"])
                base = load["end"] if load else avail[furnace]
                start = _push(max(base + setup, arrival), soak, setup, downs_of(furnace))
                candidate = (start + soak, furnace, "open", start, setup)
            if size <= capacity and (best is None or candidate[0] < best[0]):
                best = candidate
        _, furnace, how, start, setup = best
        if how == "join":
            load = loads[furnace]
            load.update(start=start, end=start + soak, used=load["used"] + size)
            load["lots"].append(lot)
        else:
            n_batch += 1
            previous = loads.get(furnace)
            if previous:
                finished_rows.append(previous)
                avail[furnace] = previous["end"]
            loads[furnace] = dict(id=f"{batch_prefix}-{n_batch}", recipe=lot["recipe"], start=start, end=start + soak,
                                  used=size, setup=setup, lots=[lot], furnace=furnace)
            cls[furnace] = lot["recipe"]
    finished_rows.extend(loads.values())
    for load in finished_rows:
        avail[load["furnace"]] = max(avail[load["furnace"]], load["end"])
        for i, lot in enumerate(load["lots"]):
            rows.append(row(lot, "age", load["furnace"], load["start"], load["end"], load["setup"] if i == 0 else 0, load["id"]))
            ready[lot["id"]] = load["end"] + LAG

    # Saw and pack passes, in arrival order.
    for kind, machine_id in ((catalog.SAW, "SAE"), (catalog.PACK, "VP")):
        batch = sorted((l for l in open_lots if sim.op_state.get(f"{l['id']}:{kind}") == "pending"), key=lambda l: (ready.get(l["id"], now), l["id"]))
        for lot in batch:
            product = catalog.PRODUCTS[lot["product_id"]]
            target_class = catalog.setup_class(kind, product)
            ideal = (lot["bars"] or lot["planned_bars"]) * catalog.SAW_MIN_PER_BAR if kind == catalog.SAW else lot["n_packages"] * catalog.PACK_MIN_PER_PACKAGE
            duration = catalog.planned_minutes(kind, ideal)
            setup = catalog.setup_minutes(kind, cls[machine_id], target_class)
            start = _push(max(avail[machine_id] + setup, ready.get(lot["id"], now)), duration, setup, downs_of(machine_id))
            rows.append(row(lot, kind, machine_id, start, start + duration, setup))
            avail[machine_id], cls[machine_id] = start + duration, target_class
            ready[lot["id"]] = start + duration + LAG

    assignments = list(sim.history) + sorted(rows, key=lambda a: (a["start"], a["op_id"]))
    return dict(assignments=assignments, downtimes=downtimes, metrics=metrics_of(sim, assignments, now), now=now)


def metrics_of(sim: Sim, assignments: list[dict], now: int) -> dict:
    completion: dict[str, int] = {}
    for a in assignments:
        if a["kind"] == "pack" and a["order_id"]:
            completion[a["order_id"]] = max(completion.get(a["order_id"], 0), a["end"])
    tardy = weighted = late = worst = 0
    for order in sim.orders.values():
        if order["completed_min"] is not None:
            completion[order["id"]] = int(order["completed_min"])
        elif order["id"] not in completion:
            continue
        lateness = max(0, completion[order["id"]] - int(order["due_min"]))
        tardy += lateness
        weighted += lateness * order["weight"]
        late += 1 if lateness > 0 else 0
        worst = max(worst, lateness)
    open_rows = [a for a in assignments if a["state"] != "done"]
    makespan = max((a["end"] for a in open_rows), default=now) - now
    setups = sum(1 for a in open_rows if a["setup_before"] > 0)
    setup_minutes = sum(a["setup_before"] for a in open_rows)
    return dict(makespan=makespan, total_tardiness=tardy, weighted_tardiness=weighted, late_orders=late, max_tardiness=worst,
                setups=setups, setup_minutes=setup_minutes, objective=10 * weighted + makespan + 2 * setup_minutes,
                order_completion=completion)


def run_fork(base: Sim, assignments: list[dict], *, scenario=(), stochastic=False, seed=1, record_frames=False, horizon=4000) -> dict:
    """Runs a fork of the plant against a plan until everything open is delivered, the twin in miniature."""
    wall0 = time.perf_counter()
    sim = base.fork(stochastic=stochastic, seed=seed)
    start_t = sim.t
    start_energy = sum(m.energy for m in sim.machines.values())
    for event in scenario:
        sim.scheduled.append(dict(event))
    sim.install(copy.deepcopy(assignments))
    frames: list[dict] = []
    next_frame = sim.t
    while sim.t - start_t < horizon:
        if record_frames and sim.t >= next_frame and len(frames) < 600:
            frames.append(sim.frame())
            next_frame += 5.0
        sim.step(1.0)
        if not sim.open_lots():
            break
        if sim.stalled:
            break
    completion = {o["id"]: (int(o["completed_min"]) if o["completed_min"] is not None else int(sim.t))
                  for o in sim.orders.values() if o["completed_min"] is None or o["completed_min"] >= start_t}
    unfinished = [o["id"] for o in sim.orders.values() if o["completed_min"] is None]
    tardy = sum(max(0, completion[o["id"]] - int(o["due_min"])) for o in sim.orders.values() if o["id"] in completion)
    late = sum(1 for o in sim.orders.values() if o["id"] in completion and completion[o["id"]] > o["due_min"])
    setups = sum(1 for a in assignments if a["state"] == "pending" and a["setup_before"] > 0)
    wall = max(1e-4, time.perf_counter() - wall0)
    return dict(
        kpis=dict(makespan=int(sim.t - start_t), total_tardiness=tardy, late_orders=late, setups=setups,
                  energy_kwh=round(sum(m.energy for m in sim.machines.values()) - start_energy, 1),
                  unfinished_orders=unfinished, stalled=sim.stalled),
        order_completion=completion, speedup=int((sim.t - start_t) * 60 / wall), wall_seconds=round(wall, 3),
        sim_minutes=int(sim.t - start_t), frames=frames,
    )


def make_candidates(sim: Sim, revision: int, current_assignments: list[dict], scenario_downs=None) -> dict:
    """keep = current lot order and machines, retimed; cpsat = EDD with free machines; anneal = best of random swaps."""
    prefix = f"B-r{revision}"
    keep_machines = {a["lot_id"]: a["machine"] for a in current_assignments if a["kind"] == "press" and a["lot_id"]}
    order = [a["lot_id"] for a in sorted(current_assignments, key=lambda x: x["start"]) if a["kind"] == "press" and a["lot_id"]]

    def scored(sched):
        return sched["metrics"]["objective"]

    t0 = time.perf_counter()
    keep = build_schedule(sim, priority=order, keep_machines=keep_machines, batch_prefix=prefix, extra_downtimes=scenario_downs)
    cpsat = build_schedule(sim, batch_prefix=prefix, extra_downtimes=scenario_downs)
    cpsat_seconds = time.perf_counter() - t0
    t1 = time.perf_counter()
    base = sorted((l["id"] for l in sim.open_lots()), key=lambda i: (sim.orders[sim.lots[i]["order_id"]]["due_min"], i))
    best = build_schedule(sim, priority=base, batch_prefix=prefix, extra_downtimes=scenario_downs)
    rng = random.Random(int(sim.t) + 7)
    iterations = 0
    for _ in range(24):
        trial = list(base)
        for _ in range(3):
            i, j = rng.randrange(len(trial) or 1), rng.randrange(len(trial) or 1)
            if trial:
                trial[i], trial[j] = trial[j], trial[i]
        result = build_schedule(sim, priority=trial, batch_prefix=prefix, extra_downtimes=scenario_downs)
        iterations += 1
        if scored(result) < scored(best):
            best, base = result, trial
    anneal_seconds = time.perf_counter() - t1
    return dict(keep=keep, cpsat=cpsat, anneal=best, seconds=dict(keep=0.01, cpsat=cpsat_seconds + 2.4, anneal=anneal_seconds + 2.5), iterations=iterations)


def candidate_json(solver: str, sched: dict, twin: dict, *, seconds: float, iterations=None, note="") -> dict:
    return {
        "solver": solver, "label": SOLVER_LABEL[solver], "status": "feasible", "valid": True, "violations": [],
        "solve_seconds": round(seconds, 2),
        "best_bound": round(sched["metrics"]["objective"] * 0.82) if solver == "cpsat" else None,
        "iterations": iterations, "note": note, "metrics": sched["metrics"], "assignments": sched["assignments"],
        "downtimes": sched["downtimes"], "twin": dict(twin["kpis"], speedup=twin["speedup"]),
    }


def strip_assignments(candidate: dict | None) -> dict | None:
    if candidate is None:
        return None
    return {k: v for k, v in candidate.items() if k != "assignments"}


# ------------------------------------------------------------------------------------------------- traceability
class Trace:
    def __init__(self):
        self.nodes: dict[str, dict] = {}
        self.out: dict[str, list[dict]] = {}
        self.into: dict[str, list[dict]] = {}
        self.load_of: dict[str, str] = {}
        self.load_baskets: dict[str, list[str]] = {}
        self.lot_baskets: dict[str, list[str]] = {}
        self.recent: deque[str] = deque(maxlen=200)

    def node(self, node_id, kind, label, t, qty, unit, attrs):
        self.nodes[node_id] = dict(id=node_id, type=kind, label=label, t=round(t, 1), qty=qty, unit=unit, attrs=attrs)

    def edge(self, a, b, qty, unit):
        row = {"from": a, "to": b, "qty": qty, "unit": unit}
        self.out.setdefault(a, []).append(row)
        self.into.setdefault(b, []).append(row)

    def walk(self, start, table, key):
        seen, stack = set(), [start]
        while stack:
            current = stack.pop()
            for row in table.get(current, ()):
                nxt = row[key]
                if nxt not in seen:
                    seen.add(nxt)
                    stack.append(nxt)
        return seen

    def graph(self, root: str, cap=400) -> dict | None:
        if root not in self.nodes:
            return None
        kind = self.nodes[root]["type"]
        starts = [root]
        if kind == "load":
            starts = list(self.load_baskets.get(root, []))
        elif kind == "lot":
            starts = list(self.lot_baskets.get(root, []))
        anc, des = set(), set()
        for s in starts:
            anc |= self.walk(s, self.into, "from")
            des |= self.walk(s, self.out, "to")
        if kind in ("load", "lot"):
            des |= set(starts)
        ids = ({root} | anc | des)
        side = {self.load_of[n] for n in ids if n in self.load_of}
        truncated = len(ids) > cap
        ordered = sorted(ids, key=lambda n: (self.nodes[n]["t"], n))[:cap]
        keep = set(ordered) | (side if not truncated else set())
        nodes = []
        for node_id in keep:
            node = dict(self.nodes[node_id])
            if node_id == root:
                relation = "root"
            elif node_id in side:
                relation = "side"
            elif node_id in anc and node_id not in des:
                relation = "ancestor"
            else:
                relation = "descendant"
            node["relation"] = relation
            nodes.append(node)
        edges = [row for n in keep for row in self.out.get(n, ()) if row["to"] in keep]
        for basket, load in self.load_of.items():
            if basket in keep and load in keep:
                edges.append({"from": load, "to": basket, "qty": 1, "unit": "Korb"})
        if kind == "lot":
            edges.extend({"from": root, "to": basket, "qty": 1, "unit": "Korb"} for basket in starts if basket in keep)
        packages = [n for n in nodes if n["type"] == "package"]
        kg = sum(p["attrs"].get("_kg", 0.0) for p in packages)
        for n in nodes:
            n["attrs"] = {k: v for k, v in n["attrs"].items() if not k.startswith("_")}
        return dict(root=root, root_type=kind, truncated=truncated, direction_note="Vorfahren und Nachkommen des gewählten Knotens",
                    nodes=nodes, edges=edges,
                    summary=dict(packages=len(packages), orders=sorted(n["id"] for n in nodes if n["type"] == "order"),
                                 pieces=sum(p["qty"] for p in packages), melts=sorted(n["id"] for n in nodes if n["type"] == "melt"),
                                 loads=sorted(n["id"] for n in nodes if n["type"] == "load"), kg=round(kg, 1)),
                    recall_label="möglich betroffen")

    def result(self, node_id):
        node = self.nodes[node_id]
        return dict(id=node_id, type=node["type"], label=node["label"], t=node["t"])


# ------------------------------------------------------------------------------------------------- the server side
class WsClient:
    def __init__(self):
        self.state = None
        self.queue: deque[str] = deque(maxlen=200)
        self.wake = asyncio.Event()


class Mock:
    def __init__(self, factor=120, seed=1, random_failures=True, auto_supply=True, warm_start=360.0):
        self.run_id = "r-" + uuid.uuid4().hex[:4]
        self.factor = factor
        self.connected = True
        self.stale_flag = False
        self.force_stale = False
        self.trace = Trace()
        self.log: deque[dict] = deque(maxlen=500)
        self.alarms: list[dict] = []
        self.clients: set[WsClient] = set()
        self.changes: deque[dict] = deque(maxlen=500)
        self.methods: deque[dict] = deque(maxlen=200)
        self.change_counts: dict[str, int] = {}
        self.prev_nodes: dict[str, object] = {}
        self.datachanges_total = 0
        self.rate_window: deque[tuple[float, int]] = deque(maxlen=20)
        self.events_total = 0
        self.proposals: dict[str, dict] = {}
        self.open_proposal: str | None = None
        self.proposal_history: deque[str] = deque(maxlen=20)
        self.counter = dict(proposal=0, candidates=0, twin=0, pipeline=0)
        self.candidate_sets: dict[str, dict] = {}
        self.twin_runs: dict[str, dict] = {}
        self.tasks: set[asyncio.Task] = set()
        self.loop_started = False
        self.sim = Sim(seed=seed, random_failures=random_failures, auto_supply=auto_supply, run_id=self.run_id)
        self.sim.attach_sink(self)
        self.plan_doc: dict = {}
        self.revision = 0
        for _ in range(9):
            self.sim.generate_order()
        self._apply(build_schedule(self.sim, batch_prefix="B-r1"), solver="cpsat", source="startup")
        self.wall_anchor = time.time()
        if warm_start > 0:
            # Fast-forward so the hall is populated at once. Faults stay off so a demo starts calm.
            saved = self.sim.random_failures
            self.sim.random_failures = False
            self.sim.next_sample = 0.0
            for _ in range(int(warm_start)):
                self.sim.step(1.0)
            self.sim.random_failures = saved
            self._log("info", "mes", f"Mock gestartet, {int(warm_start)} Simulationsminuten vorgespult")

    # ---------------------------------------------------------------- sink for the Sim
    def melt(self, melt_id, alloy, billets, t, temp):
        self.trace.node(melt_id, "melt", f"Schmelze {melt_id} · {alloy}", t, len(billets), "Bolzen",
                        {"Legierung": alloy, "Temperatur": f"{temp:.0f} °C"})
        for billet in billets:
            self.trace.node(billet, "billet", f"Bolzen {billet}", t, 1, "Bolzen", {"Legierung": alloy, "Schmelze": melt_id, "Temperatur": f"{temp:.0f} °C"})
            self.trace.edge(melt_id, billet, 1, "Bolzen")
        self.trace.recent.append(melt_id)
        self._log("info", "opcua", f"melt_cast {melt_id} · {alloy} · {len(billets)} Bolzen")
        self.events_total += 1

    def order(self, row):
        self.trace.node(row["id"], "order", f"Auftrag {row['id']}", row["created_min"], row["qty_bars"], "Stück",
                        {"Produkt": row["product_id"], "Fällig": str(row["due_min"])})
        for lot_id in row["lots"]:
            self.trace.node(lot_id, "lot", f"Los {lot_id}", row["created_min"], 1, "Los", {"Auftrag": row["id"]})
        self._broadcast("order", self.order_json(row))

    def order_changed(self, row):
        self._broadcast("order", self.order_json(row))

    def strand(self, billet_id, strand_id, bars, t, machine, product):
        self.trace.node(strand_id, "strand", f"Strang {strand_id}", t, bars, "Stangen", {"Matrize": product.die, "Presse": machine, "Produkt": product.id})
        self.trace.edge(billet_id, strand_id, bars, "Stangen")
        self.events_total += 1

    def basket(self, basket, t):
        self.trace.node(basket["id"], "basket", f"Korb {basket['id']}", t, basket["bars"], "Stangen", {})
        for strand in basket["strands"]:
            self.trace.edge(strand["id"], basket["id"], strand["bars"], "Stangen")
        for lot in self.sim.lots.values():
            if basket in lot["baskets"]:
                self.trace.lot_baskets.setdefault(lot["id"], []).append(basket["id"])
                break
        self.events_total += 1

    def load(self, load_id, basket_ids, batch_id, t, machine):
        self.trace.node(load_id, "load", f"Ofencharge {load_id}", t, len(basket_ids), "Körbe", {"Ofen": machine, "Ladung": batch_id or ""})
        self.trace.load_baskets[load_id] = list(basket_ids)
        for basket in basket_ids:
            self.trace.load_of[basket] = load_id
        self.events_total += 1

    def packages(self, lot, packages, t):
        product = catalog.PRODUCTS[lot["product_id"]]
        cursor = 0
        baskets = lot["baskets"] or []
        for package in packages:
            self.trace.node(package["id"], "package", f"Paket {package['id']}", t, package["pieces"], "Stück",
                            {"Produkt": product.id, "Auftrag": lot["order_id"], "_kg": package["pieces"] * product.kg_per_bar})
            if baskets:
                basket = baskets[min(cursor, len(baskets) - 1)]
                self.trace.edge(basket["id"], package["id"], package["pieces"], "Stück")
            self.trace.edge(package["id"], lot["order_id"], package["pieces"], "Stück")
            self.trace.recent.append(package["id"])
            cursor += 1
        self.events_total += 1

    def op_started(self, machine_id, unit):
        self._log("info", "mes", f"{machine_id}: StartOperation {', '.join(unit['op_ids'])}")
        self.methods.append(dict(wall_ts=time.time(), method="StartOperation", node=machine_id,
                                 args=json.dumps(dict(op_ids=unit["op_ids"], kind=unit["kind"], run_id=self.run_id), ensure_ascii=False),
                                 result='{"ok":true}', ms=round(random.uniform(1.2, 4.5), 1)))
        self.events_total += 1

    def op_finished(self, machine_id, unit, kind):
        self._log("info", "opcua", f"op_finished {machine_id} · {', '.join(unit['op_ids'])}")
        self.events_total += 1

    def phase(self, machine_id, phase):
        pass

    def alarm_raised(self, machine: Machine):
        alarm = machine.alarm
        latency = int(random.uniform(38, 92))
        row = {"id": f"{machine.id}:{alarm['id']}", "machine": machine.id, "alarm_id": alarm["id"], "code": alarm["code"],
               "text": alarm["text"], "severity": alarm["severity"], "t": round(self.sim.t, 1),
               "detected_wall_ts": time.time(), "latency_ms": latency, "acked": False, "cleared_t": None}
        self.alarms.insert(0, row)
        del self.alarms[400:]
        self._broadcast("alarm", row)
        level = "error" if alarm["severity"] >= 900 else "warn"
        self._log(level, "opcua", f"{machine.id}: Alarm {alarm['code']} {alarm['text']} (DataChange nach {latency} ms)")

    def alarm_cleared(self, machine: Machine):
        alarm_id = f"{machine.id}:{machine.alarm['id']}"
        for row in self.alarms:
            if row["id"] == alarm_id:
                row["cleared_t"] = round(self.sim.t, 1)
                self._broadcast("alarm", row)
                break

    def fault(self, machine: Machine, fault, injected_wall_ts):
        self.start_proposal(machine, fault, injected_wall_ts)

    def repaired(self, machine: Machine):
        self._log("info", "opcua", f"{machine.id}: Status {machine.status} (Störung behoben)")
        prop = self.proposals.get(self.open_proposal) if self.open_proposal else None
        if prop and prop["trigger"].get("machine") == machine.id and prop["status"] in ("computing", "open"):
            self.close_proposal(prop, "superseded", f"{machine.id} ist wieder betriebsbereit, der Vorschlag gilt nicht mehr.")

    def supply_added(self):
        self._log("info", "mes", "Demo-Nachschub: neue Aufträge angelegt")
        self.rolling_replan()

    # ---------------------------------------------------------------- plumbing
    def _log(self, level, source, text):
        row = dict(t=round(self.sim.t, 1), wall_ts=time.time(), level=level, source=source, text=text)
        self.log.appendleft(row)
        self._broadcast("log", row)

    def _broadcast(self, kind, data):
        if not self.clients:
            return
        message = json.dumps({"type": kind, "data": data}, ensure_ascii=False)
        for client in self.clients:
            client.queue.append(message)
            client.wake.set()

    def _spawn(self, coro):
        task = asyncio.ensure_future(coro)
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)
        return task

    # ---------------------------------------------------------------- plans
    def _apply(self, schedule: dict, solver: str, source: str, proposal_id=None):
        self.revision += 1
        for row in schedule["assignments"]:
            if row["kind"] == "cast":
                self.sim.op_state.setdefault(row["op_id"], "pending")
        self.sim.install(schedule["assignments"])
        self.plan_doc = dict(revision=self.revision, solver=solver, source=source, source_proposal_id=proposal_id,
                             created_min=round(self.sim.t, 1), metrics=schedule["metrics"],
                             assignments=schedule["assignments"], downtimes=schedule["downtimes"])
        self._broadcast("plan", {"revision": self.revision, "solver": solver, "source": source, "source_proposal_id": proposal_id})
        self._log("info", "plan", f"Plan Revision {self.revision} aktiv ({SOLVER_LABEL.get(solver, solver)}, Quelle {source})")

    def rolling_replan(self):
        if self.open_proposal:
            self._log("info", "plan", "Rolling Replan zurückgestellt, ein Vorschlag ist offen")
            return
        self._apply(build_schedule(self.sim, batch_prefix=f"B-r{self.revision + 1}"), solver="cpsat", source="rolling")

    def plan_json(self) -> dict:
        doc = dict(self.plan_doc)
        rows = []
        for a in doc["assignments"]:
            state = self.sim.op_state.get(a["op_id"], a["state"])
            rows.append(dict(a, state=state))
        doc["assignments"] = rows
        doc["now"] = round(self.sim.t, 1)
        doc["orders"] = [dict(id=o["id"], due=int(o["due_min"]), weight=o["weight"]) for o in self.sim.orders.values() if o["completed_min"] is None]
        return doc

    # ---------------------------------------------------------------- proposals
    def start_proposal(self, machine: Machine, fault, injected_wall_ts, supersede_reason=None):
        current = self.proposals.get(self.open_proposal) if self.open_proposal else None
        if current and current["status"] in ("computing", "open"):
            self.close_proposal(current, "superseded", supersede_reason or f"Neue Störung an {machine.id}, Neuplanung läuft.")
        source_ts = time.time()
        injected = injected_wall_ts
        self.counter["proposal"] += 1
        latency = self.alarms[0]["latency_ms"] if self.alarms else 60
        prop = {
            "id": f"pr-{self.counter['proposal']}", "status": "computing", "status_reason": "", "pending_update": None,
            "trigger": dict(kind="fault", machine=machine.id, code=fault.code, text=fault.text, est_repair_min=machine.est_repair, t=round(self.sim.t, 1), also=[]),
            "chain": dict(injected_wall_ts=injected, source_ts=source_ts, detected_wall_ts=source_ts + latency / 1000.0, latency_ms=latency,
                          forked_wall_ts=None, solved_wall_ts=None, twin_wall_ts=None, ready_wall_ts=None, replan_seconds=None, snapshot_seq=self.events_total),
            "based_on_revision": self.revision, "age_min": 0.0, "keep": None, "candidates": {}, "recommended": None,
            "beats_keep": None, "summary": "", "delta": None,
        }
        self.proposals[prop["id"]] = prop
        self.open_proposal = prop["id"]
        self._broadcast("proposal", self.proposal_slim(prop))
        self._log("info", "plan", f"Zustand wird geforkt, Neuplanung für {machine.id} ({fault.code})")
        if self.loop_started:
            self._spawn(self._pipeline(prop))

    async def _pipeline(self, prop: dict):
        await asyncio.sleep(0.15)
        if prop["status"] != "computing":
            return
        prop["chain"]["forked_wall_ts"] = time.time()
        self._broadcast("proposal", self.proposal_slim(prop))
        fork = self.sim.fork()
        await asyncio.sleep(2.2)
        current = list(self.plan_doc["assignments"])
        revision = self.revision + 1
        built = await asyncio.to_thread(self._evaluate, fork, revision, current)
        if prop["status"] != "computing":
            return
        now = time.time()
        chain = prop["chain"]
        chain["solved_wall_ts"] = now - built["twin_seconds"]
        chain["twin_wall_ts"] = now
        chain["ready_wall_ts"] = now + 0.05
        chain["replan_seconds"] = round(chain["ready_wall_ts"] - chain["detected_wall_ts"], 2)
        keep = built["keep"]
        recommended = min(built["candidates"], key=lambda s: (built["candidates"][s]["twin"]["total_tardiness"], built["candidates"][s]["twin"]["makespan"]))
        best = built["candidates"][recommended]
        delta = {k: best["twin"][k] - keep["twin"][k] for k in ("makespan", "total_tardiness", "late_orders", "setups")}
        beats = (best["twin"]["total_tardiness"], best["twin"]["makespan"]) < (keep["twin"]["total_tardiness"], keep["twin"]["makespan"])
        if beats and delta["total_tardiness"] < 0:
            summary = f"Neuplanung spart {-delta['total_tardiness']} min Verspätung gegenüber dem aktuellen Plan."
        elif beats:
            summary = f"Neuplanung verkürzt den Makespan um {-delta['makespan']} min, die Verspätung bleibt gleich."
        else:
            summary = "Kein Kandidat ist besser als der aktuelle Plan. Der beste geprüfte Kandidat lässt sich trotzdem übernehmen."
        prop.update(status="open", keep=keep, candidates=built["candidates"], recommended=recommended, beats_keep=beats, summary=summary, delta=delta)
        self._broadcast("proposal", self.proposal_slim(prop))
        self._log("info", "plan", f"Vorschlag {prop['id']} bereit: {summary}")

    def _evaluate(self, fork: Sim, revision: int, current: list[dict]) -> dict:
        schedules = make_candidates(fork, revision, current)
        t0 = time.perf_counter()
        twins = {name: run_fork(fork, schedules[name]["assignments"]) for name in ("keep", "cpsat", "anneal")}
        keep = candidate_json("keep", schedules["keep"], twins["keep"], seconds=schedules["seconds"]["keep"], note="Bisherige Reihenfolge, neu terminiert")
        candidates = {
            "cpsat": candidate_json("cpsat", schedules["cpsat"], twins["cpsat"], seconds=schedules["seconds"]["cpsat"]),
            "anneal": candidate_json("anneal", schedules["anneal"], twins["anneal"], seconds=schedules["seconds"]["anneal"], iterations=schedules["iterations"]),
        }
        return dict(keep=keep, candidates=candidates, twin_seconds=time.perf_counter() - t0)

    def close_proposal(self, prop: dict, status: str, reason: str):
        prop["status"] = status
        prop["status_reason"] = reason
        if self.open_proposal == prop["id"]:
            self.open_proposal = None
        self.proposal_history.appendleft(prop["id"])
        self._broadcast("proposal", self.proposal_slim(prop))
        self._log("warn" if status == "superseded" else "info", "plan", f"Vorschlag {prop['id']}: {status}. {reason}".strip())

    def proposal_full(self, prop: dict | None) -> dict | None:
        if prop is None:
            return None
        return dict(prop, age_min=round(self.sim.t - prop["trigger"]["t"], 1))

    def proposal_slim(self, prop: dict | None) -> dict | None:
        """Everything except assignments, which the panel does not need and which would break the state size limit."""
        if prop is None:
            return None
        return dict(prop, age_min=round(self.sim.t - prop["trigger"]["t"], 1), keep=strip_assignments(prop["keep"]),
                    candidates={k: strip_assignments(v) for k, v in prop["candidates"].items()})

    # ---------------------------------------------------------------- JSON views
    def order_json(self, order: dict) -> dict:
        product = catalog.PRODUCTS[order["product_id"]]
        completion = self.plan_doc.get("metrics", {}).get("order_completion", {}).get(order["id"]) if self.plan_doc else None
        lots = [self.sim.lots[l] for l in order["lots"]]
        lots_done = sum(1 for l in lots if l["stage"] == "fertig")
        if order["completed_min"] is not None:
            status = "fertig" if order["delivered_pieces"] >= order["qty_bars"] else "unterlieferung"
        elif any(l["stage"] != "press" or l["state"] != "wartend" for l in lots):
            status = "in_arbeit"
        else:
            status = "offen"
        finish = order["completed_min"] if order["completed_min"] is not None else completion
        lateness = None if finish is None else round(finish - order["due_min"])
        return {
            "id": order["id"], "product_id": order["product_id"], "product_name": product.name, "qty_bars": order["qty_bars"],
            "due_min": order["due_min"], "weight": order["weight"], "created_min": order["created_min"], "status": status,
            "delivered_pieces": order["delivered_pieces"], "lots_total": len(lots), "lots_done": lots_done,
            "planned_completion": finish, "lateness_min": lateness, "late": bool(lateness and lateness > 0),
            "completed_min": order["completed_min"],
            "lots": [dict(id=l["id"], n_billets=l["n_billets"], stage=l["stage"], state=l["state"], machine=l["machine"]) for l in lots],
        }

    def machine_json(self, machine: Machine) -> dict:
        spec = spec_of(machine.id)
        figures = oee_figures(self.sim.counters_of(machine))
        trend = machine.trend or dict(temperature=[], power_kw=[])
        return {
            "id": machine.id, "name": spec.name, "kind": spec.kind, "unit": spec.unit, "capacity": spec.capacity,
            "status": machine.status, "status_since": round(machine.status_since, 1),
            "temperature": round(machine.temperature, 1), "setpoint": round(machine.setpoint, 1),
            "power_kw": round(machine.power, 1), "energy_kwh": round(machine.energy, 1),
            "total": machine.total, "good": machine.good, "scrap": machine.scrap, "setup_class": machine.setup_class,
            "op": self.sim.op_view(machine), "progress": round(machine.progress, 3), "alarm": dict(machine.alarm),
            "est_repair_min": round(max(0.0, machine.est_repair - (self.sim.t - machine.fault_started)), 1) if machine.status == "FAULT" else 0.0,
            "fault_count": machine.fault_count,
            "oee": {k: _round(figures[k]) for k in ("oee", "oee_iso", "availability", "performance", "quality")},
            "next_op": machine.next_op, "waiting_on": machine.waiting_on,
            "trend": dict(temperature=list(trend["temperature"]), power_kw=list(trend["power_kw"])),
        }

    def kpi_json(self) -> dict:
        sim = self.sim
        rows = [oee_figures(sim.counters_of(m))["oee"] for m in sim.machines.values()]
        values = [r for r in rows if r is not None]
        energy = sum(m.energy for m in sim.machines.values())
        orders = [self.order_json(o) for o in sim.orders.values()]
        kg = sim.ship["kg"]
        return dict(oee_plant=_round(sum(values) / len(values)) if values else None,
                    power_kw=round(sum(m.power for m in sim.machines.values()), 1), energy_kwh=round(energy, 1),
                    kwh_per_kg=round(energy / kg, 2) if kg > 0 else None, packages=sim.ship["packages"], kg_shipped=kg,
                    orders_open=sum(1 for o in orders if o["status"] in ("offen", "in_arbeit")),
                    orders_late=sum(1 for o in orders if o["late"] and o["completed_min"] is None),
                    orders_late_total=sum(1 for o in orders if o["late"]),
                    lots_open=len(sim.open_lots()), lots_done=sum(1 for l in sim.lots.values() if l["stage"] == "fertig"))

    def state_json(self) -> dict:
        sim = self.sim
        plan = self.plan_doc
        metrics = {k: plan["metrics"][k] for k in ("makespan", "total_tardiness", "late_orders", "setups", "setup_minutes", "objective")}
        active = [{k: v for k, v in row.items() if k != "cleared_t"} for row in self.alarms if row["cleared_t"] is None][:20]
        prop = self.proposals.get(self.open_proposal) if self.open_proposal else None
        return {
            "run_id": self.run_id, "connected": self.connected, "stale": self.stale_flag,
            "sim": dict(t=round(sim.t, 2), factor=self.factor, wall_ts=time.time(), random_failures=sim.random_failures, auto_supply=sim.auto_supply),
            "machines": [self.machine_json(m) for m in sim.machines.values()],
            "material": sim.material(), "kpi": self.kpi_json(),
            "plan": dict(revision=plan["revision"], solver=plan["solver"], source=plan["source"], created_min=plan["created_min"], metrics=metrics),
            "proposal": self.proposal_state_summary(prop), "alarms_active": active, "stalled": sim.stalled,
            "counters": dict(datachanges_total=self.datachanges_total, datachanges_per_s=self.change_rate(), events_total=self.events_total, last_event_seq=self.events_total),
        }

    def proposal_state_summary(self, prop: dict | None) -> dict | None:
        if prop is None:
            return None
        return dict(id=prop["id"], status=prop["status"], trigger=prop["trigger"], recommended=prop["recommended"], age_min=round(self.sim.t - prop["trigger"]["t"], 1),
                    pending_update=prop.get("pending_update"))

    def change_rate(self) -> int:
        if len(self.rate_window) < 2:
            return 0
        (t0, n0), (t1, n1) = self.rate_window[0], self.rate_window[-1]
        return int((n1 - n0) / max(0.001, t1 - t0))

    # ---------------------------------------------------------------- OPC UA monitor data
    def node_rows(self) -> list[tuple]:
        rows: list[tuple] = []
        sim = self.sim
        plant_values = [("RunId", "String", self.run_id), ("SimTimeMin", "Double", round(sim.t, 2)), ("TimeFactor", "Double", float(self.factor)),
                        ("RandomFailures", "Boolean", sim.random_failures), ("Heartbeat", "UInt64", int((time.time() - self.wall_anchor) * 2)),
                        ("EventSeq", "UInt64", self.events_total), ("Material", "String", json.dumps(sim.material(), ensure_ascii=False)),
                        ("TotalPowerKW", "Double", round(sum(m.power for m in sim.machines.values()), 1)),
                        ("TotalEnergyKWh", "Double", round(sum(m.energy for m in sim.machines.values()), 1)), ("SnapshotCalls", "UInt64", self.counter["proposal"] + 1)]
        for name, kind, value in plant_values:
            rows.append((f"ns=2;s=Werkhalle.Plant.{name}", f"Werkhalle/Plant/{name}", None, name, kind, value))
        for m in sim.machines.values():
            spec = spec_of(m.id)
            alarm = m.alarm
            values = [("Name", "String", spec.name), ("Kind", "String", spec.kind), ("Unit", "String", spec.unit), ("Status", "String", m.status),
                      ("Temperature", "Double", round(m.temperature, 1)), ("TemperatureSetpoint", "Double", round(m.setpoint, 1)),
                      ("PowerKW", "Double", round(m.power, 1)), ("EnergyKWh", "Double", round(m.energy, 1)), ("TotalCount", "UInt64", m.total),
                      ("GoodCount", "UInt64", m.good), ("ScrapCount", "UInt64", m.scrap), ("RunMinutes", "Double", round(m.run, 1)),
                      ("SetupMinutes", "Double", round(m.setup, 1)), ("FaultMinutes", "Double", round(m.fault, 1)), ("IdleMinutes", "Double", round(m.idle, 1)),
                      ("IdealMinutes", "Double", round(m.ideal, 1)), ("SetupClass", "String", m.setup_class),
                      ("CurrentOperation", "String", json.dumps(sim.op_view(m), ensure_ascii=False) if m.unit else ""), ("Progress", "Double", round(m.progress, 3)),
                      ("AlarmId", "UInt64", alarm["id"]), ("AlarmActive", "Boolean", alarm["active"]), ("AlarmCode", "String", alarm["code"]),
                      ("AlarmText", "String", alarm["text"]), ("AlarmSeverity", "UInt16", alarm["severity"]),
                      ("EstimatedRepairMin", "Double", round(m.est_repair, 1)), ("FaultCount", "UInt64", m.fault_count)]
            for name, kind, value in values:
                rows.append((f"ns=2;s=Werkhalle.{m.id}.{name}", f"Werkhalle/Machines/{m.id}/{name}", m.id, name, kind, value))
        return rows

    def record_changes(self):
        now = time.time()
        for node_id, path, _machine, name, _kind, value in self.node_rows():
            if name == "Material":
                value_key = len(value)
            else:
                value_key = value
            if self.prev_nodes.get(node_id) != value_key:
                self.prev_nodes[node_id] = value_key
                self.change_counts[node_id] = self.change_counts.get(node_id, 0) + 1
                self.datachanges_total += 1
                latency = random.randint(8, 70)
                shown = f"<{len(value)} Zeichen>" if isinstance(value, str) and len(value) > 120 else value
                self.changes.appendleft(dict(node_id=node_id, path=path, value=shown, source_ts=now - latency / 1000.0, received_wall_ts=now, latency_ms=latency))
        self.rate_window.append((now, self.datachanges_total))

    # ---------------------------------------------------------------- background loops
    async def tick_loop(self):
        last = time.perf_counter()
        while True:
            await asyncio.sleep(0.05)
            now = time.perf_counter()
            dt = min(2.0, now - last)
            last = now
            minutes = dt * self.factor / 60.0
            while minutes > 0:
                chunk = min(minutes, 1.0)
                self.sim.step(chunk)
                minutes -= chunk

    async def state_loop(self):
        next_trend = 0.0
        while True:
            await asyncio.sleep(0.25)
            now = time.time()
            if now >= next_trend:
                next_trend = now + 1.0
                for machine in self.sim.machines.values():
                    if machine.trend is None:
                        machine.trend = dict(temperature=deque(maxlen=120), power_kw=deque(maxlen=120))
                    trend = machine.trend
                    # Whole degrees here, or 840 trend numbers alone push the state past the 12 KB of docs/API.md.
                    trend["temperature"].append(round(machine.temperature))
                    trend["power_kw"].append(round(machine.power))
            self.record_changes()
            if self.clients:
                message = json.dumps({"type": "state", "data": self.state_json()}, ensure_ascii=False, separators=(",", ":"))
                for client in self.clients:
                    client.state = message
                    client.wake.set()

    # ---------------------------------------------------------------- twin
    async def run_twin(self, run: dict, scenario: list[dict], monte_carlo: int, seed: int):
        try:
            # The fork happens here on the loop, because the tick loop mutates the plant between awaits.
            base = self.sim.fork(seed=seed)
            current = list(self.plan_doc["assignments"])
            result = await asyncio.to_thread(self._twin_work, base, current, scenario, monte_carlo, seed)
            run.update(result)
            run["status"] = "ready"
            self._log("info", "twin", f"Zwilling {run['id']} fertig")
        except Exception as exc:  # noqa: BLE001
            run.update(status="failed", note=f"Zwillingslauf fehlgeschlagen: {exc}")
            self._log("error", "twin", run["note"])

    def _twin_work(self, base, current, scenario, monte_carlo, seed) -> dict:
        t = base.t
        events = []
        downs = []
        rush_lots: list[str] = []
        scenario_sim = base.fork(seed=seed)
        for item in scenario:
            if item["type"] == "machine_down":
                at = t + item.get("at_offset_min", 0)
                events.append(dict(type="fault", at=at, machine=item["machine"], code=item.get("code", ""), repair_min=item["duration_min"]))
                downs.append(dict(machine=item["machine"], start=int(at), end=int(at + item["duration_min"]), reason=item.get("code") or "Szenario"))
            elif item["type"] == "slowdown":
                events.append(dict(type="slowdown", at=t + item.get("at_offset_min", 0), machine=item["machine"], factor=item["factor"], duration=item["duration_min"]))
            elif item["type"] == "rush_order":
                order = scenario_sim.add_order(item["product_id"], item["qty_bars"], t + item["due_in_min"], 3)
                rush_lots.extend(order["lots"])
        order_prior = [a["lot_id"] for a in sorted(current, key=lambda x: x["start"]) if a["kind"] == "press" and a["lot_id"]]
        keep_machines = {a["lot_id"]: a["machine"] for a in current if a["kind"] == "press" and a["lot_id"]}
        sched_a = build_schedule(base, priority=order_prior, keep_machines=keep_machines, batch_prefix="Z-A")
        sched_b = build_schedule(scenario_sim, priority=order_prior + rush_lots, keep_machines=keep_machines, batch_prefix="Z-B", extra_downtimes=[])
        cands = make_candidates(scenario_sim, self.revision + 1, current, scenario_downs=downs)
        sched_c = cands["cpsat"]
        var_a = run_fork(base, sched_a["assignments"], record_frames=True, seed=seed)
        var_b = run_fork(scenario_sim, sched_b["assignments"], scenario=events, record_frames=True, seed=seed)
        var_c = run_fork(scenario_sim, sched_c["assignments"], scenario=events, record_frames=True, seed=seed)
        twin_anneal = run_fork(scenario_sim, cands["anneal"]["assignments"], scenario=events, seed=seed)
        candidates = {
            "cpsat": candidate_json("cpsat", cands["cpsat"], var_c, seconds=cands["seconds"]["cpsat"]),
            "anneal": candidate_json("anneal", cands["anneal"], twin_anneal, seconds=cands["seconds"]["anneal"], iterations=cands["iterations"]),
        }
        recommended = min(candidates, key=lambda s: (candidates[s]["twin"]["total_tardiness"], candidates[s]["twin"]["makespan"]))
        if recommended == "anneal":
            var_c = run_fork(scenario_sim, cands["anneal"]["assignments"], scenario=events, record_frames=True, seed=seed)
            sched_c = cands["anneal"]

        def variant(label, data, sched, **extra):
            return dict(label=label, kpis=data["kpis"], order_completion=data["order_completion"], speedup=data["speedup"],
                        wall_seconds=data["wall_seconds"], sim_minutes=data["sim_minutes"], assignments=sched["assignments"], frames=data["frames"], **extra)

        def delta(a, b):
            return dict(makespan=a["kpis"]["makespan"] - b["kpis"]["makespan"], total_tardiness=a["kpis"]["total_tardiness"] - b["kpis"]["total_tardiness"])

        monte = dict(n=0, B=_empty_percentiles(), C=_empty_percentiles())
        if monte_carlo > 0:
            n = min(monte_carlo, 12)
            runs_b = [run_fork(scenario_sim, sched_b["assignments"], scenario=events, stochastic=True, seed=seed + i) for i in range(n)]
            runs_c = [run_fork(scenario_sim, sched_c["assignments"], scenario=events, stochastic=True, seed=seed + i) for i in range(n)]
            monte = dict(n=n, B=_percentiles(runs_b), C=_percentiles(runs_c))
        note = "Ausfallzeit aus dem Szenario; Verlangsamung näherungsweise." if any(s["type"] == "slowdown" for s in scenario) else ""
        return dict(
            forked_seq=self.events_total, note=note,
            variants=dict(A=variant("Aktueller Plan, ungestört", var_a, sched_a), B=variant("Aktueller Plan mit Szenario", var_b, sched_b),
                          C=variant("Neuer Plan mit Szenario", var_c, sched_c, solver=recommended)),
            candidates=candidates, recommended=recommended, delta_b_vs_a=delta(var_b, var_a), delta_c_vs_b=delta(var_c, var_b),
            monte_carlo=monte, applicable=True,
        )


def _empty_percentiles() -> dict:
    return dict(makespan=dict(p10=0, p50=0, p90=0), total_tardiness=dict(p10=0, p50=0, p90=0))


def _percentiles(runs: list[dict]) -> dict:
    def stats(key):
        values = sorted(r["kpis"][key] for r in runs)
        if len(values) == 1:
            return dict(p10=values[0], p50=values[0], p90=values[0])
        q = statistics.quantiles(values, n=10, method="inclusive")
        return dict(p10=round(q[0]), p50=round(q[4]), p90=round(q[8]))
    return dict(makespan=stats("makespan"), total_tardiness=stats("total_tardiness"))


# ------------------------------------------------------------------------------------------------- HTTP
def bad_seed(body: dict) -> JSONResponse | None:
    seed = body.get("seed")
    if seed is None or (isinstance(seed, int) and not isinstance(seed, bool)):
        return None
    return error(400, "bad_request", "seed muss eine ganze Zahl sein.")


def error(status: int, code: str, message: str, **extra) -> JSONResponse:
    return JSONResponse(status_code=status, content=dict(error=code, message=message, **extra))


def create_app(mock: Mock, *, run_loops=True) -> FastAPI:
    for ext, kind in ((".js", "text/javascript"), (".mjs", "text/javascript"), (".css", "text/css"), (".svg", "image/svg+xml"), (".json", "application/json")):
        mimetypes.add_type(kind, ext)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        mock.loop_started = True
        background = [asyncio.create_task(mock.tick_loop()), asyncio.create_task(mock.state_loop())] if run_loops else []
        yield
        for task in background + list(mock.tasks):
            task.cancel()
        await asyncio.gather(*background, *list(mock.tasks), return_exceptions=True)

    app = FastAPI(title="Werkhalle Mock-MES", docs_url=None, redoc_url=None, lifespan=lifespan)

    @app.exception_handler(RequestValidationError)
    async def _bad_request(_request: Request, exc: RequestValidationError):
        return error(422, "bad_request", f"Ungültige Anfrage: {exc.errors()[0].get('msg', '')}")

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(_request: Request, exc: StarletteHTTPException):
        return error(exc.status_code, "not_found" if exc.status_code == 404 else "http_error", "Nicht gefunden." if exc.status_code == 404 else str(exc.detail))

    @app.middleware("http")
    async def _no_cache(request: Request, call_next):
        response = await call_next(request)
        if not request.url.path.startswith("/api"):
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.get("/api/health")
    async def health():
        return dict(ok=mock.connected, connected=mock.connected, run_id=mock.run_id, plan_revision=mock.revision,
                    plant_module_loaded=False, snapshot_calls=mock.counter["proposal"] + 1, pool_plant_module_loaded=False, version="1", mock=True)

    @app.get("/api/state")
    async def state():
        return mock.state_json()

    @app.get("/api/catalog")
    async def get_catalog():
        return dict(
            machines=[dict(id=m.id, name=m.name, kind=m.kind, unit=m.unit, capacity=m.capacity,
                           faults=[dict(code=f.code, text=f.text, repair_mean_min=f.repair_mean_min) for f in m.faults]) for m in catalog.MACHINES.values()],
            products=[dict(id=p.id, name=p.name, alloy=p.alloy, recipe=p.recipe, die=p.die, presses=list(p.press_min_per_billet),
                           kg_per_bar=p.kg_per_bar, bars_per_billet=p.bars_per_billet, min_lead_min=catalog.min_lead_min(p)) for p in catalog.PRODUCTS.values()],
            recipes=[dict(id=r.id, temp_c=r.temp_c, soak_min=r.soak_min) for r in catalog.RECIPES.values()],
            time_factors=list(FACTORS))

    @app.get("/api/orders")
    async def orders():
        rows = [mock.order_json(o) for o in mock.sim.orders.values()]
        rows.sort(key=lambda r: (r["completed_min"] is not None, -r["created_min"], r["id"]))
        return dict(orders=rows)

    @app.post("/api/orders")
    async def create_order(request: Request):
        body = await _json(request)
        product_id = body.get("product_id")
        qty = body.get("qty_bars")
        due_in = body.get("due_in_min")
        if product_id not in catalog.PRODUCTS or not isinstance(qty, (int, float)) or qty <= 0 or not isinstance(due_in, (int, float)):
            return error(400, "bad_order", "Produkt, Menge und Termin müssen angegeben werden.")
        lead = catalog.min_lead_min(catalog.PRODUCTS[product_id])
        if due_in < lead:
            return error(400, "due_too_soon", f"Termin zu knapp: frühestens in {lead} min möglich.", min_lead_min=lead)
        row = mock.sim.add_order(product_id, int(qty), round(mock.sim.t + due_in), 3 if body.get("rush") else 1)
        mock._log("info", "mes", f"Auftrag {row['id']} angelegt ({product_id}, {int(qty)} Stangen)")
        mock.rolling_replan()
        return dict(order=mock.order_json(row), replan=None)

    @app.get("/api/plan")
    async def plan():
        return mock.plan_json()

    @app.post("/api/plan/solve")
    async def plan_solve(request: Request):
        body = await _json(request)
        if (refused := bad_seed(body)) is not None:
            return refused
        mock.counter["candidates"] += 1
        cs_id = f"cs-{mock.counter['candidates']}"
        limit = float(body.get("time_limit_s", 8))
        entry = dict(id=cs_id, status="computing", created_min=round(mock.sim.t, 1), time_limit_s=limit, lots=len(mock.sim.open_lots()),
                     ops=sum(1 for a in mock.plan_doc["assignments"] if a["state"] != "done"), candidates={}, recommended=None, stale=False, _revision=mock.revision)
        mock.candidate_sets[cs_id] = entry
        mock._spawn(_solve(mock, entry, limit))
        return dict(id=cs_id, status="computing")

    @app.get("/api/plan/candidates/{cs_id}")
    async def plan_candidates(cs_id: str):
        entry = mock.candidate_sets.get(cs_id)
        if entry is None:
            return error(404, "not_found", "Kandidatensatz nicht gefunden.")
        return dict({k: v for k, v in entry.items() if not k.startswith("_")}, stale=entry["_revision"] != mock.revision)

    @app.post("/api/plan/apply")
    async def plan_apply(request: Request):
        body = await _json(request)
        entry = mock.candidate_sets.get(body.get("candidates_id"))
        solver = body.get("solver")
        if entry is None or entry["status"] != "ready":
            return error(404, "not_found", "Kandidatensatz nicht gefunden oder noch nicht fertig.")
        if mock.force_stale or entry["_revision"] != mock.revision:
            mock.force_stale = False
            return error(409, "veraltet", "Der Plan hat sich seit der Berechnung geändert. Bitte neu berechnen.")
        candidate = entry["candidates"].get(solver)
        if candidate is None or not candidate["valid"]:
            return error(422, "invalid", "Dieser Kandidat hat die Prüfung nicht bestanden.")
        mock._apply(dict(assignments=candidate["assignments"], metrics=candidate["metrics"], downtimes=candidate["downtimes"]), solver=solver, source="manual")
        return dict(revision=mock.revision)

    @app.get("/api/proposals")
    async def proposals():
        current = mock.proposals.get(mock.open_proposal) if mock.open_proposal else None
        history = [mock.proposal_slim(mock.proposals[p]) for p in mock.proposal_history]
        return {"open": mock.proposal_full(current), "history": history}

    @app.post("/api/proposals/{proposal_id}/accept")
    async def accept(proposal_id: str, request: Request):
        body = await _json(request)
        prop = mock.proposals.get(proposal_id)
        if prop is None:
            return error(404, "not_found", "Vorschlag nicht gefunden.")
        if mock.force_stale or prop["status"] != "open":
            mock.force_stale = False
            machine = mock.sim.machines.get(prop["trigger"].get("machine"))
            new_id = None
            if prop["status"] == "open":
                mock.close_proposal(prop, "superseded", "Der Anlagenzustand hat sich seit der Berechnung geändert.")
            if machine is not None and machine.status == "FAULT" and not mock.open_proposal:
                fault = catalog.fault_spec(machine.id, machine.alarm["code"] or None)
                mock.start_proposal(machine, fault, None)
                new_id = mock.open_proposal
            return error(409, "veraltet", "Der Vorschlag ist veraltet, die Anlage hat sich weiterbewegt.", new_proposal_id=new_id)
        solver = body.get("solver") or prop["recommended"]
        if solver == "keep" or solver not in prop["candidates"]:
            return error(422, "invalid", "Dieser Kandidat kann nicht übernommen werden.")
        candidate = prop["candidates"][solver]
        mock._apply(dict(assignments=candidate["assignments"], metrics=candidate["metrics"], downtimes=candidate["downtimes"]), solver=solver, source="proposal", proposal_id=proposal_id)
        mock.close_proposal(prop, "accepted", f"Übernommen als Revision {mock.revision}.")
        revision = mock.revision
        # Lots created after the fork are not in this plan yet; they roll in right away.
        if any(mock.sim.op_state.get(f"{lot['id']}:press") == "pending" and f"{lot['id']}:press" not in mock.sim.assign_by_op for lot in mock.sim.open_lots()):
            mock.rolling_replan()
        return dict(revision=revision)

    @app.post("/api/proposals/{proposal_id}/reject")
    async def reject(proposal_id: str):
        prop = mock.proposals.get(proposal_id)
        if prop is None:
            return error(404, "not_found", "Vorschlag nicht gefunden.")
        if prop["status"] in ("computing", "open"):
            mock.close_proposal(prop, "rejected", "Vom Bediener verworfen.")
        return dict(ok=True)

    @app.post("/api/machines/{machine_id}/fault")
    async def machine_fault(machine_id: str, request: Request):
        machine = mock.sim.machines.get(machine_id)
        if machine is None:
            return error(404, "not_found", "Maschine nicht gefunden.")
        if machine.status == "FAULT":
            return error(409, "ALREADY_FAULTED", f"{machine_id} ist bereits gestört.")
        body = await _json(request)
        code = body.get("code") or ""
        try:
            catalog.fault_spec(machine_id, code or None)
        except KeyError:
            return error(400, "BAD_COMMAND", f"Unbekannter Störcode {code}.")
        sent = time.time()
        mock.methods.append(dict(wall_ts=sent, method="InjectFault", node=machine_id, args=json.dumps(dict(code=code, repairMin=body.get("repair_min", 0))), result='{"ok":true}', ms=2.4))
        mock._log("info", "mes", f"{machine_id}: InjectFault per OPC UA gesendet ({code or 'Standard'})")

        async def _datachange():
            # The status arrives later, the way a real DataChange does. The POST answer never carries it.
            await asyncio.sleep(random.uniform(0.25, 0.6))
            if machine.status != "FAULT":
                mock.sim.inject_fault(machine_id, code, float(body.get("repair_min") or 0), injected_wall_ts=sent)

        mock._spawn(_datachange())
        return dict(ok=True, sent_wall_ts=sent)

    @app.post("/api/machines/{machine_id}/repair")
    async def machine_repair(machine_id: str):
        machine = mock.sim.machines.get(machine_id)
        if machine is None:
            return error(404, "not_found", "Maschine nicht gefunden.")
        if machine.status != "FAULT":
            return error(409, "NOT_FAULTED", f"{machine_id} ist nicht gestört.")
        mock.methods.append(dict(wall_ts=time.time(), method="Repair", node=machine_id, args="{}", result='{"ok":true}', ms=1.9))
        mock.sim.repair(machine_id)
        return dict(ok=True)

    @app.post("/api/alarms/{alarm_id}/ack")
    async def ack(alarm_id: str):
        for row in mock.alarms:
            if row["id"] == alarm_id:
                row["acked"] = True
                mock.methods.append(dict(wall_ts=time.time(), method="AckAlarm", node=row["machine"], args=json.dumps(dict(alarmId=row["alarm_id"])), result='{"ok":true}', ms=1.4))
                mock._broadcast("alarm", row)
                return dict(ok=True)
        return error(404, "not_found", "Alarm nicht gefunden.")

    @app.get("/api/alarms")
    async def alarms(limit: int = 200):
        return dict(alarms=mock.alarms[:limit])

    @app.post("/api/plant/settings")
    async def settings(request: Request):
        body = await _json(request)
        if "time_factor" in body:
            if body["time_factor"] not in FACTORS:
                return error(400, "bad_time_factor", "Ungültiger Zeitfaktor.")
            mock.factor = body["time_factor"]
        if "random_failures" in body:
            mock.sim.random_failures = bool(body["random_failures"])
        if "auto_supply" in body:
            mock.sim.auto_supply = bool(body["auto_supply"])
        return dict(ok=True)

    @app.get("/api/log")
    async def get_log(limit: int = 200):
        return dict(entries=list(mock.log)[:limit])

    @app.get("/api/oee")
    async def oee(window: str = "start"):
        return _oee_json(mock, window)

    @app.get("/api/energy")
    async def energy():
        return _energy_json(mock)

    @app.get("/api/trace/search")
    async def trace_search(q: str = ""):
        needle = q.strip().lower()
        if not needle:
            return dict(results=[])
        ids = [i for i in mock.trace.nodes if needle in i.lower()]
        order_hits = [i for i, n in mock.trace.nodes.items() if n["type"] != "order" and needle in str(n["attrs"].get("Auftrag", "")).lower()]
        return dict(results=[mock.trace.result(i) for i in dict.fromkeys(ids + order_hits)][:30])

    @app.get("/api/trace/recent")
    async def trace_recent(limit: int = 20):
        recent = list(mock.trace.recent)[::-1]
        return dict(results=[mock.trace.result(i) for i in recent[:limit]])

    @app.get("/api/trace/graph/{node_id}")
    async def trace_graph(node_id: str):
        graph = mock.trace.graph(node_id)
        if graph is None:
            return error(404, "not_found", f"{node_id} ist nicht bekannt.")
        return graph

    @app.post("/api/twin/run")
    async def twin_run(request: Request):
        body = await _json(request)
        if (refused := bad_seed(body)) is not None:
            return refused
        scenario = body.get("scenario") or []
        for item in scenario:
            if item.get("type") not in ("machine_down", "rush_order", "slowdown"):
                return error(400, "bad_scenario", f"Unbekannter Szenariotyp {item.get('type')}.")
        mock.counter["twin"] += 1
        run_id = f"tw-{mock.counter['twin']}"
        run = dict(id=run_id, status="computing", created_min=round(mock.sim.t, 1), forked_seq=mock.events_total, scenario=scenario, note="",
                   variants={}, candidates={}, recommended=None, delta_b_vs_a={}, delta_c_vs_b={}, monte_carlo=dict(n=0, B=_empty_percentiles(), C=_empty_percentiles()), applicable=False)
        mock.twin_runs[run_id] = run
        for old in sorted(mock.twin_runs, key=lambda k: int(k.split("-")[1]))[:-5]:
            for variant in mock.twin_runs[old].get("variants", {}).values():
                variant["frames"] = []
        mock._spawn(mock.run_twin(run, scenario, int(body.get("monte_carlo", 0)), body.get("seed") or 1))
        return dict(id=run_id, status="computing")

    @app.get("/api/twin/runs/{run_id}")
    async def twin_get(run_id: str):
        run = mock.twin_runs.get(run_id)
        if run is None:
            return error(404, "not_found", "Zwillingslauf nicht gefunden.")
        return run

    @app.post("/api/twin/runs/{run_id}/apply")
    async def twin_apply(run_id: str, request: Request):
        run = mock.twin_runs.get(run_id)
        if run is None or run["status"] != "ready":
            return error(404, "not_found", "Zwillingslauf nicht gefunden oder noch nicht fertig.")
        if not run["applicable"]:
            return error(409, "veraltet", "Dieser Lauf kann nicht mehr übernommen werden.")
        for item in run["scenario"]:
            if item["type"] == "rush_order":
                mock.sim.add_order(item["product_id"], item["qty_bars"], round(mock.sim.t + item["due_in_min"]), 3)
        current = mock.proposals.get(mock.open_proposal) if mock.open_proposal else None
        if current:
            mock.close_proposal(current, "superseded", "Neuer Vorschlag aus dem Zwilling.")
        mock.counter["proposal"] += 1
        now = time.time()
        prop = {
            "id": f"pr-{mock.counter['proposal']}", "status": "computing", "status_reason": "", "pending_update": None,
            "trigger": dict(kind="scenario", machine=None, code="", text="Szenario aus dem Zwilling", est_repair_min=0, t=round(mock.sim.t, 1), also=[]),
            "chain": dict(injected_wall_ts=None, source_ts=None, detected_wall_ts=now, latency_ms=None, forked_wall_ts=None, solved_wall_ts=None,
                          twin_wall_ts=None, ready_wall_ts=None, replan_seconds=None, snapshot_seq=mock.events_total),
            "based_on_revision": mock.revision, "age_min": 0.0, "keep": None, "candidates": {}, "recommended": None,
            "beats_keep": None, "summary": "", "delta": None,
        }
        mock.proposals[prop["id"]] = prop
        mock.open_proposal = prop["id"]
        mock._broadcast("proposal", mock.proposal_slim(prop))
        mock._spawn(mock._pipeline(prop))
        return dict(proposal_id=prop["id"])

    @app.get("/api/opcua/nodes")
    async def opcua_nodes():
        nodes = []
        for node_id, path, machine, name, kind, value in mock.node_rows():
            row = dict(node_id=node_id, path=path, machine=machine, name=name, type=kind, value=value, source_ts=time.time(), changes=mock.change_counts.get(node_id, 0))
            if isinstance(value, str) and len(value) > 120:
                row["value"] = value[:120]
                row["truncated"] = True
            nodes.append(row)
        return dict(endpoint="opc.tcp://127.0.0.1:4840/werkhalle/", namespace="urn:werkhalle:plant", nodes=nodes)

    @app.get("/api/opcua/changes")
    async def opcua_changes(limit: int = 200):
        return dict(changes=list(mock.changes)[:limit], per_second=mock.change_rate())

    @app.get("/api/opcua/methods")
    async def opcua_methods():
        return dict(calls=list(mock.methods)[::-1])

    @app.post("/api/_mock/force_stale")
    async def force_stale():
        mock.force_stale = True
        return dict(ok=True)

    @app.post("/api/_mock/link")
    async def link(request: Request):
        body = await _json(request)
        mock.connected = bool(body.get("connected", True))
        mock.stale_flag = bool(body.get("stale", False))
        return dict(ok=True)

    @app.websocket("/ws")
    async def ws(socket: WebSocket):
        await socket.accept()
        client = WsClient()
        client.state = json.dumps({"type": "state", "data": mock.state_json()}, ensure_ascii=False, separators=(",", ":"))
        client.wake.set()
        mock.clients.add(client)
        try:
            while True:
                await client.wake.wait()
                client.wake.clear()
                while client.queue:
                    await asyncio.wait_for(socket.send_text(client.queue.popleft()), 2.0)
                if client.state is not None:
                    message, client.state = client.state, None
                    await asyncio.wait_for(socket.send_text(message), 2.0)
        except (WebSocketDisconnect, asyncio.TimeoutError, RuntimeError):
            pass
        finally:
            mock.clients.discard(client)

    if WEB_DIR.is_dir():
        app.mount("/", StaticFiles(directory=WEB_DIR, html=True), name="web")
    return app


async def _json(request: Request) -> dict:
    raw = await request.body()
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {}


async def _solve(mock: Mock, entry: dict, limit: float):
    await asyncio.sleep(min(limit, 2.5))
    fork = mock.sim.fork()
    current = list(mock.plan_doc["assignments"])

    def work():
        schedules = make_candidates(fork, mock.revision + 1, current)
        twins = {name: run_fork(fork, schedules[name]["assignments"]) for name in ("keep", "cpsat", "anneal")}
        return schedules, twins

    schedules, twins = await asyncio.to_thread(work)
    entry["candidates"] = {
        "cpsat": candidate_json("cpsat", schedules["cpsat"], twins["cpsat"], seconds=schedules["seconds"]["cpsat"]),
        "anneal": candidate_json("anneal", schedules["anneal"], twins["anneal"], seconds=schedules["seconds"]["anneal"], iterations=schedules["iterations"]),
        "keep": candidate_json("keep", schedules["keep"], twins["keep"], seconds=schedules["seconds"]["keep"], note="Bisherige Reihenfolge, neu terminiert"),
    }
    entry["recommended"] = min(("cpsat", "anneal"), key=lambda s: (entry["candidates"][s]["twin"]["total_tardiness"], entry["candidates"][s]["twin"]["makespan"]))
    entry["status"] = "ready"
    mock._log("info", "plan", f"Kandidaten {entry['id']} berechnet, empfohlen: {SOLVER_LABEL[entry['recommended']]}")


def _window_counters(sim: Sim, machine: Machine, window: str) -> dict:
    current = sim.counters_of(machine)
    if window != "8h":
        return current
    cutoff = sim.t - 480
    baseline = None
    for sample in sim.samples:
        if sample["t"] <= cutoff:
            baseline = sample["counters"][machine.id]
        else:
            break
    if baseline is None:
        return current
    return {k: current[k] - baseline[k] for k in current}


def _oee_json(mock: Mock, window: str) -> dict:
    sim = mock.sim
    window = "8h" if window == "8h" else "start"
    rows = []
    plant_values: list[dict] = []
    for machine in sim.machines.values():
        c = _window_counters(sim, machine, window)
        f = oee_figures(c)
        plant_values.append(f)
        spec = spec_of(machine.id)
        faults = c["faults"]
        rows.append(dict(
            id=machine.id, name=spec.name, oee=_round(f["oee"]), oee_iso=_round(f["oee_iso"]), availability=_round(f["availability_net"]),
            availability_iso=_round(f["availability"]), performance=_round(f["performance"]), quality=_round(f["quality"]),
            minutes=dict(run=round(c["run"], 1), setup=round(c["setup"], 1), fault=round(c["fault"], 1), idle=round(c["idle"], 1),
                         no_demand=round(f["no_demand"], 1), starved=round(f["starved"], 1), planned=round(f["planned"], 1),
                         planned_net=round(f["planned_net"], 1), ideal=round(c["ideal"], 1)),
            counts=dict(total=c["total"], good=c["good"], scrap=c["scrap"], unit=spec.unit), fault_count=faults,
            mtbf_min=round(c["run"] / faults, 1) if faults else None, mttr_min=round(c["fault"] / faults, 1) if faults else None))

    def mean(key):
        values = [p[key] for p in plant_values if p[key] is not None]
        return _round(sum(values) / len(values)) if values else None

    times = []
    trend: dict[str, list] = {m: [] for m in sim.machines}
    for sample in sim.samples[-120:]:
        times.append(sample["t"])
        for machine_id, counters in sample["counters"].items():
            trend[machine_id].append(_round(oee_figures(counters)["oee"]))
    return dict(window=window, t=round(sim.t, 1),
                plant=dict(oee=mean("oee"), oee_iso=mean("oee_iso"), availability=mean("availability_net"), performance=mean("performance"), quality=mean("quality")),
                machines=rows, trend=dict(t=times, oee=trend))


def _energy_json(mock: Mock) -> dict:
    sim = mock.sim
    total = sum(m.energy for m in sim.machines.values())
    machines = []
    for machine in sim.machines.values():
        spec = spec_of(machine.id)
        machines.append(dict(id=machine.id, name=spec.name, energy_kwh=round(machine.energy, 1), power_kw=round(machine.power, 1),
                             share=round(machine.energy / total, 3) if total else 0.0,
                             kwh_per_unit=round(machine.energy / machine.total, 2) if machine.total else None, unit=spec.unit))
    times = []
    kw: dict[str, list] = {m: [] for m in sim.machines}
    kw["total"] = []
    for sample in sim.samples[-120:]:
        times.append(sample["t"])
        for machine_id, value in sample["kw"].items():
            kw[machine_id].append(value)
        kw["total"].append(round(sum(sample["kw"].values()), 1))
    by_order = []
    for order_id, kwh in sim.order_energy.items():
        order = sim.orders[order_id]
        product = catalog.PRODUCTS[order["product_id"]]
        kg = sum(sim.lots[l]["pieces"] for l in order["lots"]) * product.kg_per_bar or sum(sim.lots[l]["bars"] for l in order["lots"]) * product.kg_per_bar
        by_order.append(dict(order_id=order_id, energy_kwh=round(kwh, 1), kg=round(kg, 1), kwh_per_kg=round(kwh / kg, 2) if kg else None))
    kg_shipped = sim.ship["kg"]
    return dict(t=round(sim.t, 1), total_kwh=round(total, 1), power_kw=round(sum(m.power for m in sim.machines.values()), 1),
                kwh_per_kg=round(total / kg_shipped, 2) if kg_shipped else None, machines=machines, trend=dict(t=times, kw=kw), by_order=by_order)


def main() -> None:
    parser = argparse.ArgumentParser(description="Werkhalle Mock-MES (nur für die UI-Entwicklung)")
    parser.add_argument("--port", type=int, default=8399)
    parser.add_argument("--factor", type=int, default=120, choices=FACTORS)
    parser.add_argument("--seed", type=int, default=1)
    parser.add_argument("--warm-start", type=float, default=360.0, help="Simulationsminuten, die beim Start vorgespult werden")
    parser.add_argument("--no-random-failures", action="store_true")
    parser.add_argument("--no-auto-supply", action="store_true")
    args = parser.parse_args()
    mock = Mock(factor=args.factor, seed=args.seed, random_failures=not args.no_random_failures,
                auto_supply=not args.no_auto_supply, warm_start=args.warm_start)
    print(f"Mock-MES auf http://127.0.0.1:{args.port}/ (Lauf {mock.run_id}). Nur für die UI-Entwicklung.", flush=True)
    uvicorn.run(create_app(mock), host="127.0.0.1", port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
