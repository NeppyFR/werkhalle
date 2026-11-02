# Werkhalle web UI

Static ES modules, no build step, no CDN, no external requests. German with real umlauts, `de-DE` numbers. Develop against
`python tools/mock_mes.py --port N`, which serves this folder and fakes all of `docs/API.md`.

```
index.html, app.js        page frame and wiring
css/tokens.css            design tokens, read this first
css/base.css              shared components, see below
js/core/                  store, api, format, router, dom, toast, tooltip, clock
js/components/            ui.js (shared pieces), hall.js, hall-layout.js, gantt.js, drawer.js, failure-panel.js, offline.js, topbar.js, rail.js
js/views/<name>.js        one module per view
```

## How a view mounts

The router imports `js/views/<name>.js` for `#/<name>` and calls `mount(el, ctx)`. `el` is an empty flex column that scrolls. Return
an unmount function that undoes every subscription, timer and rAF.

```js
import { h, loadCss } from '../core/dom.js';
import { panel } from '../components/ui.js';

export function mount(el, ctx) {
  loadCss('css/views/planung.css');
  const { store, api, fmt, router, toast, drawer } = ctx;
  const box = panel('Solververgleich');
  el.append(box.node);
  const off = store.subscribe((state) => { /* update from state */ });
  return () => off();
}
```

Name your CSS file `css/views/<name>.css` and prefix its classes with the view name. Query parameters are in `router.params`;
`router.go('planung', {proposal: 'pr-3'})` and `router.href(...)` build links. Changing the hash remounts the view.

## `ctx.store`

| Member | Meaning |
|---|---|
| `store.state` | latest `/ws` state or `null`, never queued, always the newest |
| `store.subscribe(fn)` | `fn(state)` on a new state, at most 10 Hz, at once if a state exists; returns unsubscribe |
| `store.on(type, fn)` | discrete events, returns unsubscribe. `alarm`, `order`, `log`, `proposal`, `plan` (full plan after refetch), `plan-changed` (raw message), `log-reset`, `catalog`, `conn`, `run-change`, `ui-mode` |
| `store.plan` | latest `GET /api/plan`, refetched on plan messages and reconnect |
| `store.proposals` | `{open, history}`, kept current by messages and refetches |
| `store.catalog`, `store.log` | `GET /api/catalog`; newest-first log ring |
| `store.machine(id)` | one machine row from the current state |
| `store.clock.now()` | smooth sim time extrapolated from `sim.t`, `sim.factor`, `sim.wall_ts`. Stops after 3 s without new state |
| `store.link()` | `{level: ok\|stale\|down\|wait, label, detail}` for connection displays; `gone: true` once several reconnects failed, then the banner asks for a restart and action controls are disabled |
| `store.setUiMode('zwilling')` | mode badge. The router resets it to `schatten` on every view change |
| `store.refetchPlan()`, `store.refetchProposals()` | fetch now |

The handler of `/ws` only stores. Never do per-message work for rendering; read `store.state` when you render.

## `ctx.api`

`await api.get(path, options)`, `await api.post(path, body, options)`. Returns parsed JSON. Failures throw `ApiError` with `.status`, `.code`,
`.message` (the backend's German text) and `.body`, and show that message as a toast. If you show the problem in place, silence the toast:
`{quiet: true}` or `{quiet: [409, 422]}`.

## `ctx.fmt`

Everything takes `null` and returns `–`, never `NaN`. `formatSimTime`/`simTime(t)` gives `Tag 2 · 14:30` with `t=0` = Tag 1, 06:00.
`simClock`, `duration(min, {signed})`, `seconds(s, digits)`, `number(v, digits)`, `percent(fraction, digits)`, `signed(v)`, `kw`, `kwh`, `kg`, `temp`,
`wallTime(ts, {ms})`, `statusLabel` (Bereit, Produktion, Rüsten, Störung), `statusTag`, `statusTone` (`run setup fault idle`), `machineName(id)`, `productName(id)`,
`solverLabel`, `planSourceLabel`, `logSourceLabel`, `opKindLabel`, `plural(n, 'Los', 'Lose')`. For labels people read, never the raw form: `opId('L-1:press')`, `ops(ids, kind)`, `setupClass('6082|M-3001')`, `orderId('Z-A-0001')`, `machineShort(id)`, `waiting(value)` + `waitingTip(value)`, `repairLeft(estimate, since, now, {long})`.

## `ctx.toast`, `ctx.drawer`, tooltips

`toast('Text', {kind: 'info'|'ok'|'warn'|'error'})`. `drawer.open('PR2')`, `drawer.close()`, `drawer.onChange(fn)`. Any element, SVG included, gets a tooltip with `data-tip="Text"`.

## Design tokens and rules

Restrained HMI after ISA-101. Use the tokens in `css/tokens.css`, no new colours:
surfaces `--surface-0..3`, lines `--line-1..3`, text `--text-1..3`, state `--state-run|setup|fault|idle` with `-dim` variants, `--accent` for selection, `--font-ui`, `--font-mono`, `--fs-*`, `--sp-*`.

- Colour means state or abnormal condition. Normal data is grey. No decorative gradients, no glass, no emoji, no shadows on panels.
- State is never colour alone: use `statusBadge`/`statusIcon`, shape plus label.
- Values use `.mono` or `.num`, which set tabular numerals. Numbers right-aligned, in `de-DE` via `fmt`.
- Controls are real `<button>`, `<select>`, `<a>`, with `aria-label` where the text is not enough. Focus stays visible; do not remove it.
- `@media (prefers-reduced-motion: reduce)`: drop pulses, keep moving material.
- Must work at 1440x900 and 1920x1080, and be usable at 1280x720. The failure panel docks under your view and takes about 210 px, 42 px when collapsed, and it starts collapsed below 800 px window height.

## Components

`js/components/gantt.js`: `renderGantt(host, {assignments, downtimes, faults, orders, now, rows, ...})`, see the head of the file. `faults` are real outages from the plant, drawn over the bars, apart from the plan's own `downtimes`. `js/components/hall-layout.js`: positions and routes of the hall.

`css/base.css`: `.panel` (`__head __title __actions __body`, `.panel--dense`), `.btn` (`--primary --danger --ghost --sm --icon`), `.field` + `.input`, `.seg` + `.seg__btn` (`aria-pressed`), `.switch`, `.tbl` (dense table, sticky head),
`.kv` (definition grid), `.tag` (`--mono --run --setup --fault`), `.meter`, `.toolbar`, `.empty`, `.muted`, `.faint`, `.nowrap`.

`js/core/dom.js`: `h('div.a.b', {attrs}, ...children)` and `s(...)` for SVG build elements and always insert text safely. `setText`, `setAttr`, `setClass` write only on change. `clear(node)`, `loadCss(href)`. Use `appendAll(node, ...)` when a child may be `null`: the native `append` prints it.

`js/components/ui.js`: `panel(title, {dense, actions})` returns `{node, head, body, actions}`. `statusBadge(status)` + `updateStatus(node, status)`. `statusIcon(key, {size})` + `paintIcon(svg, key)`, keys are machine statuses plus `info ok warn link`. `meter({value, label, tone})` + `updateMeter(node, value, tone)`.
`sparkline({width, height, label, minSpan})` returns `{node, update(values, {reference})}`. `icon(name, size)`, `empty(text)`.

### The hall

```js
import { createHall, liveHallProvider } from '../components/hall.js';
const hall = createHall(hostElement, { provider: liveHallProvider(store), onSelect: (id) => drawer.open(id), showClock: false });
hall.setProvider(fn); hall.setSelected(id); hall.setBanner('Text', 'warn'|'fault'); hall.destroy();
```

A provider is a function returning `{t, machines, material}` or `null`. `machines` is the array from `/api/state` or the `id -> row` map of a twin `Frame`; frame rows only carry `status`, `progress`, `lot_ids`, and the hall shows `–` for the rest.
`t` drives all motion. To replay a twin variant, return the frame's `machines` and `material` objects and an interpolated `t`, and hand out the same objects until the frame changes: the hall re-derives text only when those references change. Leave out `onSelect` for a read-only hall. `showClock: true` adds the frame time. Several halls can coexist. Call `destroy()` on unmount.

### Mock

`python tools/mock_mes.py --port N [--factor 120] [--seed 1] [--warm-start 360] [--no-random-failures] [--no-auto-supply]`. Dev only, and nothing under `src/` may import it. `POST /api/_mock/force_stale` makes the next accept or apply answer `409 veraltet`; `POST /api/_mock/link {"connected":false,"stale":false}` simulates a dropped or stale OPC UA link.
