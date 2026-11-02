# Werkhalle HTTP and WebSocket API

Base `http://127.0.0.1:<port>`. JSON everywhere. Times are sim minutes unless the key ends in `_ms` or `_s` or `_ts`
(`wall_ts` = Unix seconds). Machine ids come from the OPC UA browse: `GUSS PR1 PR2 OF1 OF2 SAE VP`. Errors:
`{"error":"code","message":"deutscher Text"}` with 4xx. This is the contract the UI builds against.

Local guard on every request: a `Host` other than `127.0.0.1:<port>` or `localhost:<port>` gets `403 forbidden_host`. A present `Origin` that does not match it gets `403 forbidden_origin` on anything but GET and HEAD, and the WebSocket is closed before it opens. A non-GET request with a body must say `Content-Type: application/json`, else `415 bad_content_type`. Bodies over 1 MB get `413 too_large`, malformed bodies `400 bad_request`. JSON answers are strict: a non-finite number goes out as `null`.

## State

### `GET /api/state`, and WebSocket `/ws` `{type:"state", data:<same>}` at about 4 Hz. Stays under 12 KB.

```json
{
 "run_id":"r-ab12", "connected":true, "stale":false,
 "sim":{"t":1234.5,"factor":120,"wall_ts":1793000000.1,"random_failures":true,"auto_supply":true},
 "machines":[{
   "id":"PR2","name":"Presse 2 (35 MN)","kind":"press","unit":"Stangen","capacity":1,
   "status":"RUN","status_since":1200.0,
   "temperature":488.2,"setpoint":490.0,"power_kw":451.0,"energy_kwh":1234.5,
   "total":320,"good":316,"scrap":4,"setup_class":"6082|M-3001",
   "op":{"op_ids":["L-00001:press"],"kind":"press","lot_ids":["L-00001"],"order_ids":["A-1001"],"product_id":"ST-200","phase":"RUN","batch_id":null},
   "progress":0.42,
   "alarm":{"active":false,"id":0,"code":"","text":"","severity":0},
   "est_repair_min":0.0,"fault_count":1,
   "oee":{"oee":0.71,"oee_iso":0.42,"availability":0.81,"performance":0.92,"quality":0.98},
   "next_op":"L-00002:press","waiting_on":null,
   "trend":{"temperature":[488.1,488.2],"power_kw":[450,451]}
 }],
 "material":{ "...":"Material JSON, PLAN section 4" },
 "kpi":{"oee_plant":0.62,"power_kw":1200.5,"energy_kwh":9000.1,"kwh_per_kg":0.91,"packages":42,"kg_shipped":5400.0,
        "orders_open":8,"orders_late":1,"orders_late_total":3,"lots_open":14,"lots_done":6},
 "plan":{"revision":3,"solver":"cpsat","source":"startup","created_min":0.0,"metrics":{"makespan":2100,"total_tardiness":45,"late_orders":1,"setups":14,"setup_minutes":420,"objective":2345}},
 "proposal":null,
 "alarms_active":[{"id":"PR2:3","machine":"PR2","alarm_id":3,"code":"P-204","text":"Hauptzylinder undicht","severity":900,"t":1230.0,"detected_wall_ts":1793000000.0,"latency_ms":62,"acked":false}],
 "stalled":false,
 "counters":{"datachanges_total":12345,"datachanges_per_s":180,"events_total":900,"last_event_seq":900}
}
```

`trend` holds the last 120 samples at 1 Hz (wall clock), as whole numbers to fit the size limit. Alarm rows also carry `source_ts`, `active` and, once cleared, `cleared_t` and `cleared_ts`; `latency_ms` is `null` for an alarm first seen in the baseline after a (re)connect. `waiting_on` is one of `null`, `"Material"`, `"Vorgänger"`, `"Störung"`, `"Kein Auftrag"`, `"Kein Plan"`, `"Entscheidung offen"`. `"Entscheidung offen"` means open work exists but the active plan holds it on another machine until the open proposal is decided; like `"Kein Plan"` it counts as starved in OEE. `"Kein Plan"` means idle while open lots are in no plan yet; OEE counts it as `starved`, while `"Kein Auftrag"` counts as `no_demand`. `kpi.orders_late` counts open orders by the effective finish below, `kpi.orders_late_total` adds the orders that finished late. `op` is `null` when idle.
`status` values: `IDLE RUN SETUP FAULT`, UI labels: Bereit, Produktion, Rüsten, Störung.
`plan` is `null` until the first plan is active. `proposal` is the open proposal's summary, `{id,status,trigger,recommended,age_min,pending_update}`, or `null`. The size limit wins over the trend length: when the rest of the state leaves too little room, the trends shrink below 120 samples and grow back later. An alarm row with `machine:"MES"` comes from the MES itself (`MES-STALL`), not from an `Alarm*` DataChange; its ack stays in the MES.

### Discrete WebSocket messages

`{type:"alarm",data:<alarm row>}`, `{type:"proposal",data:<proposal>}`, `{type:"plan",data:{revision,...}}` (refetch `GET /api/plan`), `{type:"order",data:<order row>}`,
`{type:"log",data:{t,wall_ts,level:"info|warn|error",source:"opcua|mes|plan|twin",text}}`. On reconnect the client refetches `GET /api/state`, `/api/plan`, `/api/proposals`.
A `proposal` message leaves out `assignments`; `GET /api/proposals` has them for the open proposal.

Before the MES has read the plant's first snapshot, routes that need the order book answer `503 not_ready`; with the OPC UA line down, those that must reach the plant answer `503 not_connected`.

## Catalogue, orders

`GET /api/catalog`

```json
{"machines":[{"id":"PR1","name":"...","kind":"press","unit":"Stangen","capacity":1,"faults":[{"code":"P-201","text":"...","repair_mean_min":90}]}],
 "products":[{"id":"FR-60","name":"Fensterrahmen FR-60","alloy":"6060","recipe":"T66","die":"M-1001","presses":["PR1","PR2"],"kg_per_bar":7.2,"bars_per_billet":8,"min_lead_min":428}],
 "recipes":[{"id":"T66","temp_c":185,"soak_min":300}],
 "time_factors":[0,60,120,240,480]}
```

`GET /api/orders` returns `{"orders":[Order]}`: open orders newest first, then the 200 most recently finished.

```json
Order = {"id":"A-1001","product_id":"FR-60","product_name":"Fensterrahmen FR-60","qty_bars":200,"due_min":1500,"weight":1,"created_min":0,
 "status":"offen|in_arbeit|fertig|unterlieferung","delivered_pieces":0,"lots_total":4,"lots_done":1,
 "planned_completion":1450,"lateness_min":-50,"late":false,"completed_min":null,
 "lots":[{"id":"L-00001","n_billets":8,"stage":"press|age|saw|pack|fertig","state":"wartend|laeuft|fertig","machine":"PR1"}]}
```

`late` and `lateness_min` follow the real clock. For an open order the effective finish is `max(planned_completion, now)`, or `now` while it has no planned completion; for a finished one it is `completed_min`. `late` is effective finish > `due_min`, so an overdue open order never reads as on time. `planned_completion` stays the plan's own figure.

`POST /api/orders` `{"product_id":"KK-80","qty_bars":200,"due_in_min":600,"rush":true}` returns `{"order":Order,"replan":"proposal_id or null"}` and rolls the plan: an auto-applied rolling replan, or, while a proposal is computing or open, its lots and melts appended to the active plan without a solver (a new revision with `source:"rolling"`, validated as always), and `replan` names that proposal, which then gets a `pending_update`. `400 due_too_soon` includes `min_lead_min`.

Rules: `product_id` a known string, `qty_bars` a whole number from 1 (`20.0` passes, `20.5` does not) up to 1000000 and at most 16 lots, `due_in_min` finite, from `min_lead_min` up to 100000, `rush` a boolean. Everything else gets `400 bad_order`. Auto-supply is held while a proposal is computing or open.

With the OPC UA line down an order is still accepted and kept in the run's database. If the plant comes back as the same run, the order goes on; if it comes back as a new run, the MES starts over and the order is gone.

## Plan and solver comparison

`GET /api/plan`

```json
{"revision":3,"solver":"cpsat","source":"startup","source_proposal_id":null,"created_min":0,"now":1234.5,
 "metrics":{"makespan":2100,"total_tardiness":45,"weighted_tardiness":45,"late_orders":1,"max_tardiness":45,"setups":14,"setup_minutes":420,"objective":2345,"order_completion":{"A-1001":1450}},
 "assignments":[{"op_id":"L-00001:press","kind":"press","machine":"PR2","start":10,"end":106,"setup_before":0,"batch_id":null,"lot_id":"L-00001","order_id":"A-1001","product_id":"ST-200","state":"pending|running|done","frozen":false}],
 "downtimes":[{"machine":"PR2","start":1230,"end":1470,"reason":"P-204"}],
 "orders":[{"id":"A-1001","due":1500,"weight":1}]}
```

`POST /api/plan/solve` `{"time_limit_s":8,"seed":1}` returns `{"id":"cs-7","status":"computing"}`, or `503 not_connected` with the line down, or `400 bad_request` for a `seed` that is not an integer (the same on `POST /api/twin/run`). Manual solves and explicit twin runs have a low-priority pool of their own, so a fault proposal never queues behind them. A set still computing is never evicted, so an id just returned stays valid. `GET /api/plan/candidates/{id}`:

```json
{"id":"cs-7","status":"computing|ready|failed","created_min":1234.5,"time_limit_s":8,"lots":14,"ops":62,
 "candidates":{"cpsat":Candidate,"anneal":Candidate,"keep":Candidate},"recommended":"cpsat","stale":false}
```

```json
Candidate = {"solver":"cpsat","label":"CP-SAT","status":"optimal|feasible|no_solution","valid":true,"violations":[],"solve_seconds":8.02,"best_bound":1200,"iterations":null,"note":"",
 "metrics":{...as in plan...},"assignments":[...as in plan...],"downtimes":[...],
 "twin":{"makespan":2090,"total_tardiness":30,"late_orders":1,"setups":14,"energy_kwh":12000,"unfinished_orders":[],"stalled":false,"speedup":54000}}
```

`POST /api/plan/apply` `{"candidates_id":"cs-7","solver":"anneal"}` returns `{"revision":4}`, or `409 veraltet`, `422 invalid`. Both ids must be strings, else `400 bad_request`.

## Proposals

`GET /api/proposals` returns `{"open":Proposal|null,"history":[Proposal, last 20, without assignments]}`.

```json
Proposal = {"id":"pr-3","status":"computing|open|superseded|expired|accepted|rejected","status_reason":"",
 "pending_update":null,
 "trigger":{"kind":"fault|stall|no_material|scenario","machine":"PR2","code":"P-204","text":"Hauptzylinder undicht","est_repair_min":240,"t":1230.0,
            "also":[{"machine":"OF1","code":"O-301","text":"Umluftgebläse defekt","remaining_min":180.5}]},
 "chain":{"injected_wall_ts":1793000000.0,"source_ts":1793000000.05,"detected_wall_ts":1793000000.11,"latency_ms":62,
          "forked_wall_ts":1793000000.2,"solved_wall_ts":1793000009.0,"twin_wall_ts":1793000009.6,"ready_wall_ts":1793000009.7,"replan_seconds":8.8,"snapshot_seq":900},
 "based_on_revision":3,"age_min":12.0,
 "keep":Candidate,"candidates":{"cpsat":Candidate,"anneal":Candidate},"recommended":"cpsat","beats_keep":true,
 "summary":"Neuplanung spart 180 min Verspätung gegenüber dem aktuellen Plan.",
 "delta":{"makespan":-60,"total_tardiness":-180,"late_orders":-1,"setups":2}}
```

`injected_wall_ts` is null when the fault did not come through this MES (random or external). A re-solve keeps the first four links of the machine's real chain; its `replan_seconds` counts from when the re-solve was asked for.

`trigger.also` lists every other fault the proposal assumes, with the repair time left at its fork. A machine leaves the list as soon as it recovers.

A new fault no longer closes an open proposal. The proposal stays acceptable and gets `pending_update:{"machine":"OF1","code":"O-301","text":"..."}` (`machine` null and `text` naming the order for a new order, or naming new lots or the current situation) while the pipeline re-solves in the background with everything that is down. When that is ready, the old proposal becomes `superseded` and the new one is `open` in the same step, so the accept button is never gone. When the main trigger's machine recovers, the proposal is superseded as before, with a re-solve for remaining faults. When only a secondary machine recovers, the proposal stays. New lots, from an order or from auto-supply, can need melts the proposal's result does not hold. Then `pending_update` also carries `accept_blocked`, the reason an accept would answer `409` until the re-solve lands; the key is absent otherwise. A proposal left undecided for 480 sim minutes expires, unless it is for a fault still running: that one is re-solved in the background instead and stays open. A fault injected through this MES always gets a proposal; a random or external one shorter than the replan takes is skipped with a log line.

`POST /api/proposals/{id}/accept` `{"solver":"cpsat"}` (optional; default recommended, `keep` not allowed) returns `{"revision":4}`, or `409 veraltet` with a `new_proposal_id`, the re-solve starting automatically; its `message` carries the real reason, also kept in `status_reason`. Accepting during a pending update is allowed and goes through the rebase as usual. The background result is then discarded, never applied; a fault it was for gets a fresh proposal, also after a reject.
A proposal still computing answers `409 noch_nicht_fertig`, with its own id as `new_proposal_id`.
`POST /api/proposals/{id}/reject` returns `{"ok":true}`.

## Machines, alarms, plant

`POST /api/machines/{id}/fault` `{"code":"P-204","repair_min":240}` calls OPC UA `InjectFault`. `repair_min` must be finite and between 0 and 100000, else `400 BAD_COMMAND`, which the plant method answers as well. Returns `{"ok":true,"sent_wall_ts":...}` and does **not** change MES state; the machine turns FAULT only through the DataChange. `409 ALREADY_FAULTED`.
`POST /api/machines/{id}/repair` calls OPC UA `Repair`. `409 NOT_FAULTED`.
`POST /api/alarms/{id}/ack` calls `AckAlarm`. `GET /api/alarms?limit=200` returns `{"alarms":[row with "cleared_t":number|null,"acked":bool]}`, newest first.
`POST /api/plant/settings` `{"time_factor":240,"random_failures":false,"auto_supply":true}`, every key optional, the two switches real booleans (`400 bad_request`). `GET /api/log?limit=200` returns `{"entries":[...]}`.

## OEE and energy

`GET /api/oee?window=start|8h`:

```json
{"window":"8h","t":1234.5,"plant":{"oee":0.6,"oee_iso":0.4,"availability":0.8,"performance":0.9,"quality":0.98},
 "machines":[{"id":"PR2","name":"...","oee":0.7,"oee_iso":0.4,"availability":0.81,"availability_iso":0.55,"performance":0.92,"quality":0.98,
   "minutes":{"run":700,"setup":100,"fault":60,"idle":300,"no_demand":40,"starved":120,"planned":1120,"planned_net":1000,"ideal":640},
   "counts":{"total":320,"good":316,"scrap":4,"unit":"Stangen"},"fault_count":2,"mtbf_min":350,"mttr_min":60}],
 "trend":{"t":[60,120],"oee":{"PR2":[0.7,0.71]}}}
```
OEE values may be `null` when undefined. `trend` covers the requested window only, so `8h` has points from the last 480 sim minutes. `fault_count`, and with it `mtbf_min` and `mttr_min`, counts the faults overlapping the window, including one already running when the window opens.

`GET /api/energy`:

```json
{"t":1234.5,"total_kwh":9000,"power_kw":1200,"kwh_per_kg":0.91,
 "machines":[{"id":"PR2","name":"...","energy_kwh":1234,"power_kw":450,"share":0.14,"kwh_per_unit":0.8,"unit":"Stangen"}],
 "trend":{"t":[60,120],"kw":{"PR2":[450,120],"total":[1200,1100]}},
 "by_order":[{"order_id":"A-1001","energy_kwh":1200,"kg":800,"kwh_per_kg":1.5}]}
```

`by_order` covers open orders and the last 200 finished, sorted by id. `kwh_per_kg` is `null` until all lots of the order are packed.

## Traceability

`GET /api/trace/search?q=S-000` returns `{"results":[{"id":"S-00001","type":"melt","label":"Schmelze S-00001 · 6060","t":90}]}`. Matches ids and order ids, max 30; `q` is cut at 100 characters. Types: `melt billet strand basket load package order lot`.
`GET /api/trace/recent?limit=20` returns the same shape, newest packages and melts first.

`GET /api/trace/graph/{id}`:

```json
{"root":"S-00001","root_type":"melt","truncated":false,"direction_note":"Vorfahren und Nachkommen des gewählten Knotens",
 "nodes":[{"id":"B-00001-01","type":"billet","label":"Bolzen B-00001-01","t":90,"qty":1,"unit":"Bolzen",
   "attrs":{"Legierung":"6060","Schmelze":"S-00001","Temperatur":"721 °C"},"relation":"descendant|ancestor|root|side"}],
 "edges":[{"from":"S-00001","to":"B-00001-01","qty":1,"unit":"Bolzen"}],
 "summary":{"packages":6,"orders":["A-1001","A-1002"],"pieces":320,"melts":["S-00001"],"loads":["O-00001"],"kg":2304.0},
 "recall_label":"möglich betroffen"}
```

Column order for layout: `melt, billet, strand, basket, package, order`; `load` is a side node attached to its baskets, `relation:"side"`, except when it is the root.
`lot` appears only as a root (a lot can span melts, so it is not a step of the flow): it attaches to its strands the way a load root attaches to its baskets. Edges to or from a load or lot carry `"side":true`. `summary` covers the whole traversal even when `truncated` is true; `truncated` means only the node and edge lists stopped at the cap of 400.

## Twin

`POST /api/twin/run` `{"scenario":[{"type":"machine_down","machine":"PR2","at_offset_min":0,"duration_min":240},{"type":"rush_order","product_id":"KK-80","qty_bars":200,"due_in_min":600}],"monte_carlo":0,"seed":1,"time_limit_s":6}` returns `{"id":"tw-2","status":"computing"}`, or `503 not_connected` with the line down, or `400 bad_scenario` (`min_lead_min` included for a rush order due too soon). At most 50 items. `at_offset_min` is 0 to 100000, `duration_min` above 0 up to 100000, `factor` above 1 up to 100 for a `slowdown`, `machine` and `product_id` known strings. A `rush_order` follows the rules of `POST /api/orders`. Unknown keys of an item are dropped.

`GET /api/twin/runs/{id}`:

```json
{"id":"tw-2","status":"computing|ready|failed","created_min":1234.5,"forked_seq":900,"scenario":[...],"note":"",
 "variants":{
   "A":{"label":"Aktueller Plan, ungestört","kpis":{"makespan":2000,"total_tardiness":0,"late_orders":0,"setups":12,"energy_kwh":11000,"unfinished_orders":[],"stalled":false},"order_completion":{"A-1001":1400},"speedup":54000,"wall_seconds":0.04,"sim_minutes":2000,"assignments":[...],"frames":[Frame]},
   "B":{"label":"Aktueller Plan mit Szenario","...":"same"},
   "C":{"label":"Neuer Plan mit Szenario","solver":"cpsat","...":"same"}},
 "candidates":{"cpsat":Candidate,"anneal":Candidate},"recommended":"cpsat",
 "delta_b_vs_a":{"makespan":240,"total_tardiness":400},"delta_c_vs_b":{"makespan":-100,"total_tardiness":-300},
 "monte_carlo":{"n":0,"B":{"makespan":{"p10":0,"p50":0,"p90":0},"total_tardiness":{"p10":0,"p50":0,"p90":0}},"C":{"...":"same"}},
 "applicable":true}
```

```json
Frame = {"t":1240,"machines":{"PR2":{"status":"FAULT","progress":0.4,"lot_ids":["L-1"]}},"material":{"...":"Material JSON"}}
```

In a variant, `assignments` are the rows as the twin ran them (`start`, `end` and `setup_before` are numbers with decimals, `start` is when the op's run began, and rows the run never finished keep their planned times). `kpis` and a Candidate's `twin` also carry `objective`, and the deltas too. `variants.C` is `null`, `delta_c_vs_b` empty and `applicable` false when no new plan is valid. A ready run also carries `rush_orders` and `timing`. Frames are spaced 5 sim minutes, or a doubling of that on long runs so that at most 600 per variant cover the whole run. Read each frame's `t`, do not assume the spacing. They are present only for the last 5 explicit runs. `POST /api/twin/runs/{id}/apply` `{"solver":"cpsat"}` runs the two-step path from PLAN section 8: returns `{"proposal_id":"pr-4"}`, and the result arrives as a proposal with `trigger.kind:"scenario"`. All rush items are checked before any is booked; a refused apply leaves the run applicable. `503 not_connected` with the line down.

## OPC UA monitor

`GET /api/opcua/nodes` returns `{"endpoint":"opc.tcp://127.0.0.1:4840/werkhalle/","namespace":"urn:werkhalle:plant","nodes":[{"node_id":"ns=2;s=Werkhalle.PR2.Status","path":"Werkhalle/Machines/PR2/Status","machine":"PR2","name":"Status","type":"String","value":"RUN","source_ts":1793000000.1,"changes":12}]}`. Large strings are shortened to 120 characters with `"truncated":true`.
`GET /api/opcua/changes?limit=200` returns `{"changes":[{"node_id":"...","path":"...","value":"FAULT","source_ts":1793000000.05,"received_wall_ts":1793000000.11,"latency_ms":62}],"per_second":180}`, newest first, from a ring of 500.
`GET /api/opcua/methods` returns `{"calls":[{"wall_ts":...,"method":"StartOperation","node":"PR1","args":"{...}","result":"{\"ok\":true}","ms":2.1}]}`, ring of 200.

## Health

`GET /api/health` returns `{"ok":true,"connected":true,"run_id":"r-ab12","plan_revision":1,"plant_module_loaded":false,"snapshot_calls":1,"pool_plant_module_loaded":false,"version":"1"}`. `ok` is true only once connected, which can be before the first plan: the launcher opens the browser once `plan_revision` is at least 1.
