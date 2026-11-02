# Werkhalle: build plan and contracts

Mini smart factory for aluminium extrusion: simulated plant behind a real OPC UA server, an MES that only talks to it
over OPC UA, two production schedulers, and a digital shadow/twin. Windows 11, one command to start, browser UI in
German (standard German, real umlauts, `de-DE` number formats).

This file is the source of truth for module boundaries; `docs/API.md` for the HTTP/WebSocket contract. Where either
conflicts with an implementer's taste, they win. Existing code in `domain/` and `scheduling/model.py` is normative for
the shapes it defines. If a contract is truly unworkable, change it minimally, and record the change in the section it
belongs to, so the others see it.

## 1. Architecture

```
 start.cmd -> python -m werkhalle (launcher: free ports, spawns, polls /api/health, opens browser, Ctrl+C stops all)
                   |                                 |
                   v                                 v
   Process 1: python -m werkhalle.plant    Process 2: python -m werkhalle.mes
   +--------------------------------+      +---------------------------------------------+
   | simcore.SimEngine (the "real"  |      | opcua client -> shadow (live mirror)        |
   |   plant, stochastic)           | OPC  | Tracker + Dispatcher (executes active plan) |
   | plant.server: asyncua server   |<---->| OEE, energy, alarms, trace store (SQLite)   |
   |   variables, methods           | UA   | scheduling: CP-SAT + annealing (proc pool)  |
   +--------------------------------+      | twin: simcore fork, runs faster than real   |
                                           | FastAPI: REST + WebSocket + static web UI   |
                                           +---------------------------------------------+
```

Package layout (`src/werkhalle/`, run with absolute `PYTHONPATH`):

| Package | Owns | May import |
|---|---|---|
| `domain` | catalogue, orders, lots, setup rules, `PlanningState` | stdlib only |
| `simcore` | discrete-event plant model, used by plant and twin | `domain` |
| `plant` | OPC UA server around `simcore` | `domain`, `simcore`, asyncua |
| `scheduling` | problem model, builder, CP-SAT, annealing, retime, validator, metrics | `domain`, ortools |
| `execution` | `Tracker`, `Dispatcher`, `rebase`, stall check, shared by live MES and twin | `domain`, `scheduling.model`, `scheduling.retime`, `scheduling.validate` (`rebase` carries a plan over with `keep_schedule` and validates it; neither loads ortools) |
| `mes` | OPC UA client, shadow, OEE, trace, twin runner, API | everything except `plant` |
| `web` | static UI, ES modules, no build step, no CDN, works offline | |

**Hard rule:** `werkhalle.plant` must be unreachable from `mes`, `execution`, `scheduling` and `simcore`. The test builds
the full import graph (relative, `from x import y`, function-local imports) and bans `importlib`/`__import__` in those
packages. `/api/health` reports `plant_module_loaded` from `sys.modules` for a runtime assert. At runtime the MES reaches
the plant only over OPC UA. It does not mark a machine as faulted when the UI asks for a fault; only the DataChange does
that. `GetStateSnapshot` is used for forks and planning only, never to refresh the shadow; the plant exposes a call
counter (`SnapshotCalls`) so a test can prove it.

**Twin and `simcore`:** the twin runs the same `simcore` code as the plant, but starts from the state read over OPC UA.
It never shares memory with the plant process.

## 2. Units, time, clock

- All times are simulated minutes (`float` in sim, `int` in planning), `t=0` is Tag 1, 06:00. UI format: `Tag 2 · 14:30`.
- Time factor default 120 (1 real second = 2 sim minutes), allowed {0, 60, 120, 240, 480}; 0 = paused. Invalid writes revert.
- **Clock object in the plant:** `sim = anchor_sim + (wall - anchor_wall) * factor / 60`, re-anchored on every factor change.
  Wall step clamped to about 2 s; longer gaps (laptop sleep) count as pause.
- **Driver:** short tick loop (about 50 ms) calling `engine.run_until(clock.now())`, Heartbeat written in the same task. If
  that task raises, the process exits non-zero. Never sleep until the next event.
- The MES writes floats as `ua.Variant(float(x), ua.VariantType.Double)`.
- Always use the literal `127.0.0.1`, never `localhost` (costs 2 s per connect on Windows).

## 3. Plant model (`simcore`)

Flow: `GUSS` -> `BILLET_STORE` -> `PR1`/`PR2` -> `AGE_IN` -> `OF1`/`OF2` -> `SAW_IN` -> `SAE` -> `PACK_IN` -> `VP` -> `SHIP`.
Parameters live in `domain/catalog.py`, including scrap rates, initial stock, warnings and fault setpoints.

- **Stage buffers, not machine routing:** output goes to the next stage's shared buffer, so a re-plan never re-routes.
  Transport is a fixed `TRANSPORT_MIN` lag with no capacity.
- **Initial stock:** at t=0 the engine emits real `melt_cast`/`melt_arrived` events for `INITIAL_BILLET_STOCK` (negative
  `t_start`, `plan_id="BESTAND"`), so every billet has a melt node.
- **Billets are reserved atomically:** `StartOperation(press)` moves all `n_billets` from the store to the machine (FIFO by
  melt) or refuses with `NO_MATERIAL`. The store is always net of running lots.
- **Whole-strand rule:** a basket takes at most `product.strands_per_basket` strands; strands never straddle baskets.
- **State table:** IDLE→SETUP (or RUN if no setup is needed)→RUN→IDLE. `InjectFault`: IDLE→FAULT (back to IDLE after repair),
  SETUP/RUN→FAULT (work pauses, resumes after repair), FAULT→refused `ALREADY_FAULTED`. `Repair` on a healthy machine:
  refused `NOT_FAULTED`. Both return `{ok, reason}`.
- **Failures:** MTBF on operating minutes (exponential), true repair lognormal around the mean (or exact if `repairMin` was
  injected). `RandomFailures` writable. `EstimatedRepairMin` publishes the estimate; the sampled end stays hidden.
- **Warnings:** `WarningSpec` per machine. A warning cannot raise while Status=FAULT; a fault replaces a warning and cancels
  its clear; a clear only acts if its own `AlarmId` is still current. An excursion term in T(t) makes the temperature
  really show the deviation.
- **Durations:** `stochastic=True`: ideal × noise with mean `PLANNING_ALLOWANCE[kind]`, plus occasional micro-stops. AGE has
  no noise. `stochastic=False`: exactly `planned_minutes(kind, ideal)` (spread evenly across billets on a press), no scrap,
  no random faults, so twin completion ≤ plan completion within rounding.
- **Scrap:** press `PRESS_SCRAP_RATE`, saw `SAW_SCRAP_RATE`, per bar. 1 piece = 1 bar.
- **Temperature/energy** analytic between events. Furnace setpoint in SETUP/RUN = recipe temperature, tau about 12 min. FAULT
  setpoint = `temp_fault_c`. `soak_temp_avg` counts time in band only; report time-in-band and `fault_min`. Tests: no
  `billet_extruded.temp_c` below 400 °C, no `melt_cast.temp_c` below 650 °C, healthy load within 2 K of recipe.
- **OEE counters, continuous:** `IdealMinutes` accrues continuously with `RunMinutes` at the op's ideal/actual rate and pauses
  in FAULT. Press: minutes per billet, whole billet regardless of scrap. Furnace: `soak × baskets / capacity`, so 2/10
  baskets gives performance 0.2. Test: uncapped ideal/run ≤ 1 + ε on every window.
- **IDs from the plant:** melt `S-00001`, billet `B-00001-07`, strand `ST-00001`, basket `K-00001`, load `O-00001`, package
  `P-00001`.
- **Duplicate protection:** the engine keeps started op ids; a repeat is refused with `ALREADY_STARTED` before any material
  moves. Commands carry `run_id`; a mismatch is refused with `STALE_RUN`.

### Engine API (`simcore/__init__.py` exports `SimEngine`, `planning_state_from_snapshot`)

```python
SimEngine(seed=1, stochastic=True, random_failures=True, run_id="", initial_stock=True)
engine.t
engine.run_until(t: float) -> None                  # processes all events up to t
engine.next_event_time() -> float | None
engine.start_operation(cmd: dict) -> dict           # {ok, reason}; cmd = start command of section 4 plus "machine": "<id>"
engine.inject_fault(machine_id, code="", repair_min=0.0) -> dict
engine.repair(machine_id) -> dict
engine.ack_alarm(machine_id, alarm_id) -> dict
engine.machine_view(machine_id) -> dict             # keys = OPC UA variable names of section 4
engine.material_view() -> dict                      # section 4, bounded
engine.events_after(seq, max_count=500) -> dict     # {oldest_seq, newest_seq, events}
engine.export_snapshot() -> dict                    # JSON-serialisable, atomic
SimEngine.from_snapshot(snapshot, seed=1, stochastic=False, random_failures=False) -> SimEngine
engine.planning_state() -> domain.state.PlanningState
planning_state_from_snapshot(snapshot) -> PlanningState
# Scenario hooks for the twin
engine.schedule_fault(machine_id, at_min, code="", repair_min=0.0)
engine.schedule_slowdown(machine_id, factor, at_min, duration_min)
engine.set_timeline_recording(True); engine.timeline -> list[dict]   # status changes and ops, for twin Gantt and replay
# Added by the implementation
engine.set_random_failures(enabled); engine.random_failures; engine.stochastic; engine.run_id; engine.seq
engine.raise_warning(machine_id) -> dict            # force the self-clearing warning; refused MACHINE_FAULT or ALARM_ACTIVE
engine.total_power_kw() -> float; engine.total_energy_kwh() -> float
```

`start_operation` needs the machine, which the section 4 JSON does not carry because there the node addresses it: the OPC UA server
sets `cmd["machine"]` from the called node (a value inside the JSON cannot redirect it), and direct callers such as the twin pass it. A fork
keeps the snapshot's `run_id`, so its commands must carry that id.

**Snapshot contents:** `run_id, seq, t`; per machine status, setup class, current op with remaining fraction, fault code +
estimate + elapsed, temperature, counters; billet store with ids; buffers with full payload; transports with full payload
(basket ids, alloy, melt id, `t1`); started and finished op ids; id counters. **Excluded on purpose:** sampled repair ends,
next-failure times, RNG state, so a fork cannot peek at the hidden future. `from_snapshot(stochastic=False)` uses
`max(estimate - elapsed, FAULT_REMAINING_FLOOR_MIN)` as the remaining repair; `stochastic=True` samples afresh. Test: fork at
every event of a closed-loop run, every fork completes with no horizon hit.

## 4. OPC UA address space (namespace `urn:werkhalle:plant`, string NodeIds, security None on 127.0.0.1)

```
Objects/Werkhalle/
  Plant/
    RunId (String), SimTimeMin (Double), TimeFactor (Double, writable), RandomFailures (Boolean, writable),
    Heartbeat (UInt64, ~2 Hz), EventSeq (UInt64), Material (String JSON),
    TotalPowerKW (Double), TotalEnergyKWh (Double), SnapshotCalls (UInt64)
    methods: GetEvents(afterSeq: UInt64, maxCount: UInt32) -> String JSON {oldest_seq, newest_seq, events}
             GetStateSnapshot() -> String JSON
  Machines/<ID>/                                  NodeIds like "Werkhalle.PR2.Status"
    Name, Kind, Unit (String)
    Status (String: IDLE|SETUP|RUN|FAULT)
    Temperature, TemperatureSetpoint, PowerKW, EnergyKWh (Double)
    TotalCount, GoodCount, ScrapCount (UInt64)
    RunMinutes, SetupMinutes, FaultMinutes, IdleMinutes, IdealMinutes (Double, cumulative)
    SetupClass (String), CurrentOperation (String JSON or ""), Progress (Double 0..1)
    AlarmId (UInt64), AlarmActive (Boolean), AlarmCode, AlarmText (String), AlarmSeverity (UInt16: 500 warning, 900 fault)
    EstimatedRepairMin (Double), FaultCount (UInt64)
    methods: StartOperation(json: String) -> String JSON {ok, reason}
             InjectFault(code: String, repairMin: Double) -> String JSON      ("" and 0 = default)
             Repair() -> String JSON
             AckAlarm(alarmId: UInt64) -> String JSON
```

Server rules:
- Every method handler is `async def` (sync ones run on an asyncua worker thread and race the engine). It first calls
  `engine.run_until(clock.now())` and has no suspending await between first engine access and return. A test checks it.
- Fast block, written with no suspension, in this order: `Alarm*`, `EstimatedRepairMin`, `FaultCount`, `CurrentOperation`,
  `SetupClass`, `Progress`, then **`Status` last**, with a wall-clock `SourceTimestamp`. Slow signals (temperature, power, counters) at about 2 Hz.
- Inside `Alarm*` the order is `AlarmCode, AlarmText, AlarmSeverity, AlarmId, AlarmActive`. A clear only sets `AlarmActive` false: id, code, text and severity stay, so `AlarmActive` alone says whether the alarm is on.
- asyncua groups one publish by monitored item, in order of first trigger. If `Status` already changed earlier in the same publishing interval, `Status=FAULT` sits ahead of the alarm values inside that publish, never in a later one. A consumer must apply a whole publish before reacting to `Status`.
- Reliable trace transport is pull-on-notify over a bounded ring (keep the last 20 000 events; `oldest_seq` lets the client
  detect a gap). `Material`: `SHIP` has counts plus the last 20 lots, never an ever-growing list.
- Refusal reasons: `MACHINE_BUSY`, `MACHINE_FAULT`, `NO_MATERIAL`, `NOT_ELIGIBLE`, `BAD_COMMAND`, `ALREADY_STARTED`, `STALE_RUN`,
  `ALREADY_FAULTED`, `NOT_FAULTED`, `STALE_ALARM`.

### Start command (`StartOperation`), each also carries `"run_id"`

```json
{"op_ids":["M-6060-00003:cast"], "kind":"cast", "melt_plan_id":"M-6060-00003", "alloy":"6060"}
{"op_ids":["L-00012:press"], "kind":"press", "lot_id":"L-00012", "order_id":"A-1001", "product_id":"FR-60", "n_billets":8}
{"op_ids":["L-00012:age","L-00013:age"], "kind":"age", "batch_id":"B-r3-7", "recipe":"T66", "lot_ids":["L-00012","L-00013"]}
{"op_ids":["L-00012:saw"], "kind":"saw", "lot_id":"L-00012"}
{"op_ids":["L-00012:pack"], "kind":"pack", "lot_id":"L-00012"}
```

### Trace events (`GetEvents`), each has `seq`, `t`, `type`. Quantities are on the edges, so balances are testable.

- `op_started{machine,op_ids,kind,lot_ids,batch_id,setup_min,setup_class}`
- `melt_cast{machine,melt_id,plan_id,alloy,billets:[id],temp_c,t_start}`
- `melt_arrived{melt_id,plan_id,alloy,n_billets}`
- `billet_extruded{machine,lot_id,order_id,billet_id,melt_id,strand_id,die,bars_total,bars_good,bars_scrap,temp_c,t_start}`
- `baskets_filled{machine,lot_id,baskets:[{id,bars,strands:[{id,bars}]}]}`
- `furnace_load_done{machine,load_id,batch_id,recipe,lot_ids,baskets:[id],soak_temp_avg,soak_min,in_band_min,fault_min,t_start}`
- `sawn{machine,lot_id,baskets:[{id,bars_in,pieces_good,pieces_scrap}],bars_in,pieces_good,pieces_scrap,length_mm,t_start}`
- `packed{machine,lot_id,packages:[{id,pieces,baskets:[{id,pieces}]}],t_start}`
- `op_finished{machine,op_ids,kind,lot_ids,energy_kwh,run_min,setup_min,fault_min}`
- `lot_arrived{lot_id,stage}` (stage = `age|saw|pack|ship`)

Fault events are deliberately NOT in this stream. Fault detection comes from the `Status`/`Alarm*` DataChange path.

### Material JSON (`Plant/Material`)

```json
{"t":123.4,
 "transports":[{"id":"T-17","kind":"billets|baskets|pieces|packages","lot_id":"L-1","count":3,"from":"PR1","to":"AGE_IN","t0":120.0,"t1":128.0}],
 "buffers":{"BILLET_STORE":{"6060":12,"6063":0,"6082":24},
            "AGE_IN":[{"lot_id":"L-1","baskets":3}],"SAW_IN":[{"lot_id":"L-2","baskets":2}],"PACK_IN":[{"lot_id":"L-3","pieces":60}],
            "SHIP":{"packages":42,"pieces":800,"kg":5400.0,"lots_total":7,"last_lots":["L-1"]}},
 "in_machine":{"PR1":{"lot_ids":["L-2"],"kind":"press","billets_left":5,"baskets":0}}}
```

## 5. Order -> lots -> ops, and melts

`domain.orders.split_order` splits an order (bars) into lots of at most `MAX_BILLETS_PER_LOT` billets. Each lot has four ops
`press -> age -> saw -> pack`. Lags of `TRANSPORT_MIN` between ops.

**Melt allocation (the rule that keeps the billet store from ever deadlocking a press):**
1. Demand = only lots whose press has not started. Started lots carry no melt predecessors.
2. Within an alloy, sort lots by (due, id). Consume per-alloy supply in this order: free store; in-transit melts (expressed
   as `Op.release = arrival`); running cast (frozen op, `Pred` with `TRANSPORT_MIN`); pending melts of the active plan, reused
   in order with their ids; new melts from a never-resetting counter, id `M-<alloy>-<5 digits>` (alloy in the id, so a label
   can never change meaning). Surplus pending melts are dropped from the tail of the chain only.
3. A lot depends on every supply tier up to the one covering its cumulative demand. Melts of one alloy are chained.
4. Store, op state and lot state come from the same snapshot.

Why it is safe: chained melts and cumulative coverage mean that any set of started lots has demand ≤ the supply already
arrived for its highest-ranked member. Property-test it on random mid-run states, not just t=0.

## 6. Scheduling (`scheduling/`, public API in `__init__.py`)

```python
build_problem(state: PlanningState, open_lots: Sequence[Lot], orders: Sequence[OrderInfo], *,
              pending_melts: Sequence[str] = (),   # melt plan ids of the active plan not yet started, chain order
              melt_counter: int = 1, downtimes: Mapping[str, Sequence[Downtime]] = {}, slowdowns: Sequence[dict] = (),
              batch_prefix: str = "B", weights: Weights = Weights()) -> BuildResult  # .problem, .melt_counter, .notes
decode_edd(problem) -> Schedule                    # single decode with EDD priorities
solve_cpsat(problem, time_limit_s=10, seed=1, workers=8, hint: Schedule | None = None) -> Schedule
solve_anneal(problem, time_limit_s=10, seed=1) -> Schedule
retime(problem, schedule) -> Schedule              # same sequences and loads, left-justified forward pass
keep_schedule(problem, previous: Schedule) -> Schedule  # previous sequence retimed; missing ops appended at machine tails
validate_schedule(problem, schedule) -> list[Violation]
compute_metrics(problem, schedule) -> Metrics
```

Model:
- Alternative machines (`ST-200`/`CB-90` are `PR2` only; two furnaces). Sequence-dependent setups with initial class.
- **CP-SAT uses `add_circuit` on every machine.** Furnaces use parallel arcs: for same-recipe i<j a *join* arc (same start, cost 0)
  and a *next* arc (`start_j >= end_i + setup`, priced); `add_cumulative` for baskets. Per op, `setup_j = sum(arc literal × setup)`,
  so setups are priced on every machine and the objective equals `compute_metrics`. Downtime: `end_j <= d.start` or
  `start_j - setup_j >= d.end`. Depot arcs carry the initial class.
- **Always pass the best start plan as a complete hint** (the best of the EDD decode, the best rule decode and the caller's plan; starts, machines, every arc literal). Without it CP-SAT is 80-190× worse at 32-48
  lots in a 10 s budget. Keep auto-supply at ≤ about 28 open lots.
- Frozen ops: fixed, copied to the result with batch id, excluded from downtime constraints; their end occupies the machine; a pending
  op never joins their load.
- Objective shared by both solvers: `w_tard × Σ weight × tardiness + w_mk × makespan + w_setup × setup_min`.
- Annealing: lot-priority list + machine choices + a per-lot *wait gene* in {0, 30, 60, 120, 180, 240} that lets an opener hold a furnace
  load to fill it. Serial decoder, left-justified, tries to join a compatible load. Runs in pure Python.
- **Every candidate goes through `retime` before validation and metrics.** The dispatcher left-shifts, so unretimed times would be
  fiction (drift measured up to 128 min).
- `validate_schedule` is the arbiter: independent of both solvers, includes `billet_balance` (replay per alloy by start time: supply at
  cast end + lag, demand at press start). Each rule has a negative test proving it can fail. No plan failing validation is applied.
  Validate against the problem it was built for, at creation.
- Batch ids from solvers are `f"{problem.batch_prefix}-{n}"`; the MES passes `batch_prefix=f"B-r{revision}"`.
- Slowdown scenarios scale durations in the builder (approximation; the report says `näherungsweise`). `factor` is a duration multiplier, as in `engine.schedule_slowdown`; a window with a factor of 1 or below is ignored. Windows use `at_offset_min` from `state.now`, or an absolute `at_min`.
- **A setup needs its material:** `start_j - setup_j >= pred end + lag` and `>= release`, for every member of a load, and billet demand counts at `start_j - setup_j`. `StartOperation` needs the material on site and only then enters SETUP, so a plan letting a setup run ahead of the material comes true up to all of a lot's setups too late (measured against the deterministic engine: every saw and pack run after an idle stretch). With this, a deterministic run finishes every op exactly when the plan says. The `ST-200` wait-gene case therefore no longer has the 759 of the earlier analysis: its optimum is now makespan 718 at objective 828 (a held load of two lots, the third on the other furnace), against 875 at objective 985 without holding, and both solvers reach it.
- Additions to the contract: `solve_anneal(..., *, max_iterations=None)` stops after that many decodes, so a seeded run repeats exactly; `Schedule.solver_objective` is what CP-SAT itself reported, which equals `compute_metrics` at the optimum. `solve_cpsat` never returns less than the better of the EDD decode and the given `hint`: with no solution of its own in time it returns that start plan as `feasible`, and `note` says so.

## 7. Execution (`execution/`, shared by live MES and twin)

```python
Tracker.from_state(state: PlanningState) -> Tracker
tracker.apply(event)            # ignores seq <= last; a gap sets tracker.resync_needed
tracker.op_state(op_id)         # "pending" | "running" | "done"
tracker.lot_at_stage(lot_id, stage) -> bool; tracker.melt_arrived(plan_id) -> bool
next_commands(plan, tracker, machines: Mapping[str, dict], commanded: AbstractSet[str] = frozenset(), run_id="") -> list[dict]
stalled(plan, tracker, machines, transports_active: bool) -> bool
rebase(plan, tracker, open_lot_ids, *, new_lots=(), new_orders=(), now=None) -> RebaseResult   # .plan | None, .reason, .dropped
# Added by the implementation
tracker.last_seq; tracker.t; tracker.resync_needed; tracker.lot_arrival(lot_id); tracker.melts_in_transit(); tracker.transports_pending()
tracker.running_on(machine) -> RunningOp | None; tracker.running(); tracker.setup_class(machine); tracker.free_billets(alloy, lots={})
machine_heads(plan, tracker, machines, commanded=frozenset()) -> dict[str, Head]   # Head(machine, next_op, waiting_on), the waiting_on of docs/API.md
plan_finished(plan, tracker) -> bool; machine_status(view) -> str | None; billet_shortfall(plan, tracker) -> dict[alloy, missing]
Dispatcher(run_id="", backoff=2.0)   # .poll(plan, tracker, machines, now) -> commands, recorded as commanded on the spot
                                     # .on_result(plan, tracker, command, result, now); .forget_unconfirmed(); .commanded; .replan_needed; .last_refusal
```

- `next_commands` is pure. For each free machine (Status IDLE, no command in flight) it takes the head unit of the sequence, skipping
  started/done/commanded ops. A partly started load is reduced to its pending members. It starts only if all predecessors are done
  and material has arrived. Strict head-of-line: sequence is binding. Sequence + preds are always a DAG, so only missing material or dropped ops can stall.
- A command is the JSON of section 4 plus `run_id` and `machine`. A free machine also has no op running as far as the tracker knows, because `Status` and the trace do not arrive together. Material "has arrived" for a press means: its melts have arrived (predecessor melts by `melt_arrived`, a melt in transit when the plan was built by its release) and the tracker's store holds its billets, net of commanded presses. So a press is not commanded just to be refused, and a plan short of billets shows up as `stalled`.
- Live MES: record op ids as commanded *before* the OPC call, clear only on a definite refusal, back off on refusals. At most one
  in-flight command per machine. Test with a double that delays state by 150 ms. `Dispatcher` does exactly this. `ALREADY_STARTED` is no definite refusal, and neither is a lost answer: after a reconnect, `forget_unconfirmed` lets the command go out again, and the plant's duplicate protection covers a repeat.
- `rebase` at accept: drops started and done ops, reduces loads, appends pending ops missing from the plan at eligible machine tails, then
  re-checks billet supply. Shortfall or still-missing lots: `.plan is None` -> `409 veraltet`, and the MES auto re-solves from a fresh snapshot.
  A lot the plan does not know can only be appended with its data: pass `Lot` objects in `open_lot_ids`, or in `new_lots`, and its `OrderInfo` in `new_orders` for its rank and its metrics. Without the data it counts as still missing. Supply is what the tracker knows now plus the plan's casts, no new melts are created, and melt predecessors are rebuilt by the rules of section 5. Running ops stay in the result as frozen ops with an estimated end; `.dropped` lists what the plan still wanted to start. The result has passed `validate_schedule`. `now` is the current sim time, as a tracker only knows the time of its last event.
- `stalled`: no op running, no transport, no FAULT, and the plan is unfinished. Twin ends with `stalled=true`; live MES raises an alarm
  and proposes a re-plan. A `NO_MATERIAL` refusal with no planned supply for the alloy is also a re-plan trigger.
  `stalled` is also false while `next_commands` could start something, or it would be true for the instant between an arrival and its dispatch, and at the very start. A live caller still has to hold it back while commands are in flight, since it takes no `commanded`.

## 8. Twin and re-planning (`mes/twin.py`, `mes/planning.py`, pure, run in process pools)

```python
# mes/planning.py
make_candidates(state, open_lots, orders, active_plan: Plan | None, *, revision, melt_counter, downtimes={}, slowdowns=(),
                solver_limit_s=8.0, seed=1, workers=8, source="manual") -> CandidateSet
# CandidateSet: .problem, .melt_counter, .candidates: dict["cpsat"|"anneal"|"keep", Candidate(plan, metrics, violations, status, solve_seconds, note)]

# mes/twin.py
run_closed_loop(snapshot, plan, *, scenario=(), seed=1, stochastic=False, horizon_min=None, record_frames=False, frame_step_min=5) -> TwinRun
# TwinRun: makespan, tardiness, late_orders, setups, energy_kwh, order_completion, unfinished_orders, stalled, wall_seconds, sim_minutes, speedup, timeline, frames
evaluate_scenario(snapshot, active_plan, open_lots, orders, scenario, *, revision, melt_counter, solver_limit_s, seed=1, monte_carlo=0) -> dict   # JSON per docs/API.md
evaluate_replan(snapshot, active_plan, open_lots, orders, *, revision, melt_counter, solver_limit_s, seed=1, trigger=None) -> dict   # JSON per docs/API.md
```

Additions to the contract, all top level and picklable:

```python
# mes/planning.py: the same work in pieces, for one worker per solver
prepare_problem(state, open_lots, orders, active_plan, *, revision, melt_counter, downtimes={}, slowdowns=(), extra_melts=(), batch_prefix=None, weights=Weights()) -> BuildResult
solve_candidate(problem, solver, limit=8.0, seed=1, hint=None, *, workers=8) -> Schedule   # cpsat|anneal|keep|edd; never raises, a failure is no_solution with a note
solve_all(problem, active_schedule, *, limit, seed=1, workers=8, hint=None) -> dict[str, Schedule]   # cpsat and anneal on two threads, then keep
finalize_candidates(build, schedules, *, revision, lots, created_min=None, source="manual", source_proposal_id=None) -> CandidateSet
pending_melts_of(plan, state) -> list[str]
candidate_json(candidate, twin=None); assignments_json(plan, op_state=None); downtimes_json(problem)
plan_from_result(result, solver, *, revision=None, source="proposal", source_proposal_id=None) -> Plan   # the plan behind a candidate of an evaluate_replan result
# mes/twin.py
evaluate_candidate_set(snapshot, candidate_set, *, trigger=None, based_on_revision=None, revision=None) -> dict   # the second half of evaluate_replan
monte_carlo_rows(snapshot, plan, scenario, seeds, horizon_min=None) -> list[dict]; monte_carlo_percentiles(rows) -> dict   # for chunks in the twin pool
warm_start_schedule(result, solver, lot_ids={}) -> Schedule   # step two of apply from the twin: fork-only lots mapped to the real ones, other Z- ops dropped
scenario_items(scenario) -> list[dict]; run_rank(run); default_horizon_min(plan, scenario=(), snapshot=None); run_assignments_json(plan, run); variant_json(label, plan, run, **extra)
```

`run_closed_loop` also takes `random_failures=None` (default: same as `stochastic`) and `record_timeline=True`. `TwinRun` also has `weighted_tardiness, setup_minutes, objective, horizon_hit, refusals, started_at, ended_at, finished_at, lots_packed, op_states`, and `kpis()`, `twin_json()`. Its `objective` is the plan's objective with the twin's figures, and `run_rank` (delivers everything first, then objective) is what "better" means for `recommended` and `beats_keep`. A stalled run or one that hit the horizon has makespan = horizon. `make_candidates` runs cpsat and anneal side by side on two threads inside its worker, because CP-SAT spends its time outside the GIL, so it takes one solver limit rather than two. cpsat starts from the active plan where there is one, and `solve_cpsat` takes the better of that and its EDD decode as its hint.

Variants, as built: A and B carry the active plan over to the fork with `keep_schedule`. For B a rush lot ranks last for the melts, whatever its due date, because by due date it would take billets from lots ahead of it in the sequence, and those would wait for a melt at the tail of the chain. Fork-only ids: order `Z-A-0001`, lot `Z-L-00001`, melt `Z-M-6063-00009`, load `Z-B-...`. Machine down at offset 0 acts at once, and on a machine already in FAULT it keeps the longer of both; at a later offset, `schedule_fault` is ignored if the machine is in FAULT right then.

Scenarios: `{type:"machine_down",machine,at_offset_min,duration_min,code?}`, `{type:"rush_order",product_id,qty_bars,due_in_min}`,
`{type:"slowdown",machine,factor,at_offset_min,duration_min}`.

Variants: **A** current plan undisturbed; **B** current plan + scenario (rush lots and needed melts appended naively at machine and chain tails);
**C** re-planned knowing the scenario, one per solver, best by twin result recommended. Unfinished orders get completion = horizon and an `unvollständig` flag, so a variant
cannot look better by not delivering. Monte Carlo (`stochastic=True`, N seeds) yields P10/P50/P90. Fork-only ids use a `Z-` namespace.

**Apply from the twin is a two-step path:** create scenario orders via the normal order path, rebuild the problem from live state *without* hypothetical
downtimes, re-solve warm-started from the twin plan's sequences, go through the same validate and accept path as a proposal.

**Live failure path:** `Status` DataChange to `FAULT` -> alarm with measured latency -> fork via `GetStateSnapshot` (downtime and code come from the
snapshot, for **every** faulted machine) -> both solvers -> twin evaluates `keep` vs each candidate -> proposal, recommendation, operator accepts or rejects.
Never auto-applied. If nothing beats the current plan, say so plainly; accepting the best validated candidate must still work.

**Proposal lifecycle:** one open proposal. States `computing, open, superseded, expired, accepted, rejected`. Superseded when the main machine leaves FAULT, or when a background re-solve lands. A new FAULT, a new order or new lots only set `pending_update`: the proposal stays open and acceptable while the single in-flight pipeline re-forks. Accept on a closed proposal, or one the rebase refuses, returns 409; `pending_update.accept_blocked` says in advance when new lots make that certain. Expired after 480 sim minutes undecided, except while its main fault is still running, when it is re-solved instead. Each candidate is stamped with plan revision, snapshot seq,
open lot ids, assumed machine states. Skip and log when the estimate is below `pipeline_seconds × factor / 60` ("zu kurz für Neuplanung"). The UI's break action
passes an explicit `repairMin` (default 240), so the chain is readable. Auto-supply does not stop for an open proposal: its lots go onto the active plan through the same append path as a manual order, so the plant never idles behind an undecided proposal.

**Pools:** two spawn executors: `replan` (2 workers) and `twin` (min(4, cores-2), Monte Carlo in small chunks). Each worker's initializer starts a daemon thread that
joins `multiprocessing.parent_process()` then `os._exit(3)`. On `BrokenProcessPool`, rebuild once and report the retry.
As built (`mes/pools.py`, `mes/jobs.py`): a proposal or a manual solve runs its solvers in `replan` and then its twin evaluation in `twin`, so the chain shows each step as it ends; `evaluate_scenario` runs whole in one `twin` worker, including its Monte Carlo seeds, capped at 200.

## 9. MES (`mes/`)

**OPC UA client:** `Client(url, timeout=10, watchdog_intervall=5)`, connect with `auto_reconnect=True`, `connection_lost_callback` pauses the dispatcher and raises
"Verbindung getrennt". `datachange_notification` is a plain `def` that only does `queue.put_nowait`; one consumer task. Never `queuesize=1` on `Status`. One single-flight
`GetEvents` loop runs until `lastSeq >= newest EventSeq seen`. Compare `RunId` on every (re)connect: changed = hard reset and banner; same = resume, and treat first
notifications as a baseline, keying fault handling on `FaultCount`. Browse the tree; do not hard-code machines. Mark the shadow stale when Heartbeat stops.

**SQLite:** per-run file `data/run-<RunId>/werkhalle.db`, older ones removed best effort. `--data-dir`. Writes batched off the event loop.

**OEE (`mes/oee.py`, pure, heavily tested):** from cumulative counters, windowed by differencing samples.
`no_demand = min(no_demand, idle)`; `planned = run + setup + fault + idle - no_demand`; `availability = run / planned`; `performance = min(1, ideal / run)`, plus an uncapped
value for tests; `quality = good / total`; `oee_iso = A×P×Q`. `starved` = idle while a head op exists but cannot start. `planned_net = planned - starved`, `availability_net = run / planned_net`,
`oee_net = A_net×P×Q`. The UI leads with `oee_net` and shows starvation as its own loss bar. Undefined inputs give `None`, not 0. Windows: since start, rolling 8 h.
Down-sample and prune samples, keep cumulative baselines.

**Alarms:** from `Alarm*` DataChange with source timestamp, keyed by (machine, AlarmId), ack via `AckAlarm(alarmId)`.

**Genealogy:** melt -> billet -> strand -> basket -> package -> order, edges with quantities. **The furnace load is a side node on baskets**, expanded only when the query root is the load,
because a shared load must not link unrelated lots. Queries are ancestors ∪ descendants of the root, never an undirected closure, with a node cap and a `truncated` flag.
Recall labelled "möglich betroffen". Test: two lots from different melts in one load: forward from melt 1 excludes lot 2's packages; a load-rooted recall includes both. Test: edges sum to node quantities at every level; every billet resolves to a melt node.

**Order status:** from the sum of packed pieces against `qty_bars`, with `Unterlieferung` if short once all lots are packed.

**Auto-supply:** when open lots drop below about 12, generate orders with due dates derived from a load estimate, never below `catalog.min_lead_min`, cap about 28 open lots, roll the plan forward (rolling replan, auto-applied unless a proposal is open), switchable.
Start-up plan computed and applied automatically. Rush scenario default: KK-80, 200 bars, `due_in_min=600`.

**WebSocket:** one sender task per client, latest-wins slot for state, bounded queue for discrete messages, `wait_for(send, 2 s)`, drop the client on timeout.
**Bounds:** ring of 500 DataChange records (store length only for large strings), twin frames only for explicit runs with LRU 5, metrics only for proposals.

**Static files:** `mimetypes.add_type('text/javascript', '.js')` and for `.mjs`, `.css`, `.svg`, `.json` before mounting; test that `/app.js` gets a JavaScript content type.

## 10. Launcher and start.cmd

`python -m werkhalle [--http-port 8321] [--plant-port 4840] [--time-factor 120] [--seed 42] [--solver-limit 8] [--data-dir data] [--no-browser] [--no-random-failures] [--no-auto-supply]`.
Ports checked by `bind()` without `SO_REUSEADDR`, next free port if taken, and say so. Child env: `PYTHONUTF8=1`, absolute `PYTHONPATH`. Child stdout/stderr to UTF-8 files in `logs/`, never an undrained pipe; errors reach the console.
If either child dies, stop the other and exit with a clear German message. Stop with `taskkill /PID <pid> /T /F` plus a Job Object for the direct children. Pid file, clear own leftovers at start. Open the browser only after `/api/health` is ok.

Additions: `--log-dir` (default `logs/` in the project), so a test can keep its logs apart. `tools/break_machine.py PR2 --code P-204 --repair 240 --port 4840` breaks a machine from outside the MES, over OPC UA, with no werkhalle code in it.

`start.cmd`: ASCII only, `.gitattributes` `*.cmd text eol=crlf`, `cd /d "%~dp0"`, `set "PYTHONPATH=%CD%\src"`, no parenthesised blocks (`goto`). Try `py -3.13`, then `python`, each verified by running a version check. Marker `.venv\.deps-ok` containing a requirements hash, written only after pip succeeds; re-run pip when missing or different. On failure print a plain message, `pause`, `exit /b 1`.

## 11. UI

Static ES modules. Views: **Leitstand** (2D hall with moving material, status, OEE, power, alarms, machine drawer with "Störung auslösen"), **Aufträge**, **Planung** (solver comparison + Gantt), **Rückverfolgung** (genealogy DAG, recall),
**Zwilling** (scenarios, impact, replay in the hall view), **OEE & Energie**, **Alarme**, **OPC UA** (live node table, DataChange feed: the proof that data really arrives over OPC UA). Mode badge: *Schatten* (live mirror) vs *Zwilling* (fork).
Visual language: restrained control-room HMI after ISA-101: muted neutrals, colour reserved for state and abnormal conditions. Windows 11 system fonts (Segoe UI Variable, Cascadia Mono), no external requests. `onmessage` only stores the latest state; rendering reads it; never queue per-message work for rAF.

The failure story must read as a chain on screen: *Störung ausgelöst -> OPC-UA-DataChange erkannt (x ms) -> Zustand geforkt -> Neuplanung (y s) -> Vorschlag mit Wirkung -> übernehmen.*

## 12. Definition of done

1. `start.cmd` from a fresh clone: venv, deps, plant + MES, browser opens, plant is running.
2. `pytest` green: scheduler constraints, OEE, simcore, tracker/dispatcher closed loop, twin, architecture rule.
3. E2E test with real processes, flags `--seed --no-random-failures --no-auto-supply --solver-limit 3 --no-browser`, temp data dir, own ports, children started *without* `PYTHONIOENCODING`: wait for plan revision ≥ 1 with a pending `ST-200` lot, call `InjectFault('P-204', 100000)` from an **independent OPC UA client**, assert `proposal.trigger.machine == 'PR2'`, alarm source timestamp equals the Status DataValue timestamp, downtime length correct, snapshot called only after detection, accept gives `plan.source_proposal_id` match and revision exactly +1, and `plant_module_loaded` is false.
4. Verified in a real browser: plant moves, a machine breaks from the UI, chain appears, re-plan applied. Use Playwright headless with `channel:'chrome'` (the preview pane's tabs are hidden, so rAF never fires there).
5. README explains the architecture with a diagram.
