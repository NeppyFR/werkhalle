// Zwilling: what the shadow is, what the twin adds, and what a scenario would do to the plan. The hall is the same
// component as in the Leitstand, fed live as a shadow and from recorded frames as a twin. Every number comes from the run;
// the page only computes differences. Leaving the view keeps builder and run in `session`, so coming back restores them.
import { h, s, setText, setAttr, clear, loadCss } from '../core/dom.js';
import { createHall, liveHallProvider } from '../components/hall.js';
import { renderGantt } from '../components/gantt.js';
import { panel, statusBadge, updateStatus, statusIcon, paintIcon, meter, updateMeter } from '../components/ui.js';

const VARIANTS = ['A', 'B', 'C'];
const VARIANT_HEAD = { A: 'aktuell ungestört', B: 'aktuell mit Szenario', C: 'neuer Plan' };
const VARIANT_SEG = { A: 'A ungestört', B: 'B mit Szenario', C: 'C neu geplant' };
const VARIANT_FORK = { A: 'A ungestört', B: 'B mit Szenario', C: 'C neuer Plan' };
const MACHINES = ['GUSS', 'PR1', 'PR2', 'OF1', 'OF2', 'SAE', 'VP'];
const MONTE_OPTIONS = [{ value: 0, label: 'aus' }, { value: 10, label: '10' }, { value: 30, label: '30 Läufe' }];
const LIMIT_OPTIONS = [3, 6, 12, 30].map((value) => ({ value, label: `${value} s` }));
const SPEED_OPTIONS = [10, 30, 60, 120].map((value) => ({ value, label: String(value) }));
const POLL_MS = 400;
const GANTT_NOW_MS = 100;
const AGE_NOTE_MIN = 30;
const AGE_WARN_MIN = 120;

// The shapes that tell the variants apart wherever they appear together: never colour alone.
const MARKS = {
  A: 'M2 2h8v8H2Z',
  B: 'M6 .8 11.2 6 6 11.2 .8 6Z',
  C: 'M6 1.2a4.8 4.8 0 1 0 0 9.6 4.8 4.8 0 0 0 0-9.6Z',
};

const down = (machine) => ({ type: 'machine_down', machine, at_offset_min: 0, duration_min: 240 });
const rush = () => ({ type: 'rush_order', product_id: 'KK-80', qty_bars: 200, due_in_min: 600 });
const PRESETS = [
  { key: 'pr2', label: 'Presse 2 fällt aus', items: () => [down('PR2')] },
  { key: 'rush', label: 'Eilauftrag trifft ein', items: () => [rush()] },
  { key: 'both', label: 'Beides', items: () => [down('PR2'), rush()] },
  { key: 'of2', label: 'Ofen 2 fällt aus', items: () => [down('OF2')] },
];
const ITEM_DEFAULTS = {
  machine_down: () => down('PR2'),
  slowdown: () => ({ type: 'slowdown', machine: 'PR1', factor: 1.5, at_offset_min: 0, duration_min: 240 }),
  rush_order: rush,
};
const ITEM_TYPES = [
  { value: 'machine_down', label: 'Maschinenausfall' },
  { value: 'slowdown', label: 'Verlangsamung' },
  { value: 'rush_order', label: 'Eilauftrag' },
];

const session = {
  plantRun: null,
  scenario: PRESETS[2].items(),
  monte: 0,
  limit: 6,
  waiting: false,
  startedAt: 0,
  runId: null,
  pending: null,
  run: null,
  failed: null,
  editing: false,
  orderInfo: new Map(),
  rushDue: new Map(),
  startRevision: null,
  requested: null,
  applied: null,
  applyError: null,
  solver: null,
  source: 'live',
  t: null,
  speed: 30,
  pollProblem: null,
  kick: null,
};

function forgetRun() {
  session.waiting = false;
  session.editing = false;
  session.runId = null;
  session.pending = null;
  session.run = null;
  session.failed = null;
  session.applied = null;
  session.applyError = null;
  session.solver = null;
  session.source = 'live';
  session.t = null;
  session.pollProblem = null;
}

function dueOf(orderId) {
  return session.rushDue.get(orderId) ?? session.orderInfo.get(orderId)?.due ?? null;
}

// ------------------------------------------------------------------------------------------- small pieces
function variantMark(key, size = 12) {
  return s(`svg.zwilling-mark.zwilling-mark--${key}`, { viewBox: '0 0 12 12', width: size, height: size, 'aria-hidden': 'true' }, s('path', { d: MARKS[key] }));
}

function segmented(options, value, onChange, label, extraClass = '') {
  const buttons = new Map();
  const node = h(`div.seg${extraClass}`, { role: 'group', 'aria-label': label });
  for (const option of options) {
    const button = h('button.seg__btn', {
      type: 'button',
      'aria-pressed': String(option.value === value),
      'data-tip': option.tip,
      onclick: () => onChange(option.value),
    }, option.label);
    buttons.set(option.value, button);
    node.appendChild(button);
  }
  return {
    node,
    set(next) {
      for (const [key, button] of buttons) setAttr(button, 'aria-pressed', String(key === next));
    },
    enable(fn) {
      for (const [key, button] of buttons) button.disabled = !fn(key);
    },
  };
}

function field(label, control) {
  return h('label.field', h('span.field__label', label), control);
}

const sum = (values) => values.reduce((total, value) => total + value, 0);

function roundSignificant(value, digits) {
  if (!value) return 0;
  const magnitude = 10 ** (Math.ceil(Math.log10(Math.abs(value))) - digits);
  return Math.round(value / magnitude) * magnitude;
}

function spanWords(minutes, fmt) {
  if (minutes >= 1440) {
    const days = minutes / 1440;
    const text = fmt.number(days, days < 10 && Math.abs(days - Math.round(days)) >= 0.05 ? 1 : 0);
    return `${text} ${text === '1' ? 'Tag' : 'Tage'}`;
  }
  const hours = minutes / 60;
  const text = fmt.number(hours, hours < 10 && Math.abs(hours - Math.round(hours)) >= 0.05 ? 1 : 0);
  return `${text} ${text === '1' ? 'Stunde' : 'Stunden'}`;
}

function wallWords(seconds, fmt) {
  if (seconds >= 0.1) return fmt.seconds(seconds, 1);
  return fmt.seconds(seconds, seconds >= 0.01 ? 2 : 3);
}

// A press that is already down cannot get worse by being "broken" again, so the deltas stay at ±0. This says why, before anyone doubts the twin.
export function alreadyDownNotes(items, store, fmt) {
  const now = store.clock.now();
  const notes = [];
  const seen = new Set();
  for (const item of items ?? []) {
    if (item.type !== 'machine_down' || seen.has(item.machine)) continue;
    const machine = store.machine(item.machine);
    if (machine?.status !== 'FAULT') continue;
    seen.add(item.machine);
    const estimate = machine.est_repair_min;
    const until = typeof estimate === 'number' && estimate > 0 && typeof machine.status_since === 'number' ? machine.status_since + estimate : null;
    const repair = until === null ? '' : until > now ? `, Reparatur bis ca. ${fmt.simTime(until)}` : `, ${fmt.REPAIR_OVERDUE}`;
    notes.push(`${fmt.machineShort(item.machine)} steht bereits${repair}.`);
  }
  return notes;
}

function describeItem(item, fmt) {
  const from = item.at_offset_min > 0 ? `ab ${fmt.duration(item.at_offset_min)} nach dem Abzweig` : 'ab sofort';
  if (item.type === 'machine_down') return `${fmt.machineName(item.machine)} steht ${fmt.duration(item.duration_min)}, ${from}`;
  if (item.type === 'slowdown') return `${fmt.machineName(item.machine)} braucht ${fmt.duration(item.duration_min)} lang das ${fmt.number(item.factor, 1)}-Fache, ${from}`;
  if (item.type === 'rush_order') return `Eilauftrag ${item.product_id}: ${fmt.number(item.qty_bars)} Stangen, Termin in ${fmt.duration(item.due_in_min)}`;
  return String(item.type);
}

function checkItem(item, catalog, fmt) {
  const positive = (v) => typeof v === 'number' && Number.isFinite(v) && v > 0;
  const notNegative = (v) => typeof v === 'number' && Number.isFinite(v) && v >= 0;
  if (item.type === 'rush_order') {
    if (!positive(item.qty_bars)) return { error: 'Die Menge muss über 0 liegen.' };
    if (!positive(item.due_in_min)) return { error: 'Der Termin muss in der Zukunft liegen.' };
    const lead = catalog?.products?.find((p) => p.id === item.product_id)?.min_lead_min;
    if (lead && item.due_in_min < lead) {
      return { warn: `Unter der Mindestdurchlaufzeit von ${fmt.duration(lead)}: der Zwilling rechnet es, ein echter Auftrag würde abgelehnt.` };
    }
    return {};
  }
  if (!notNegative(item.at_offset_min)) return { error: 'Der Start darf nicht negativ sein.' };
  if (!positive(item.duration_min)) return { error: 'Die Dauer muss über 0 liegen.' };
  if (item.type === 'slowdown' && !(typeof item.factor === 'number' && item.factor > 1)) {
    return { error: 'Der Faktor multipliziert die Dauer und muss über 1 liegen.' };
  }
  return {};
}

function variantEnd(run, key) {
  const variant = run.variants?.[key];
  if (!variant) return null;
  return run.created_min + (variant.sim_minutes ?? variant.kpis?.makespan ?? 0);
}

function prepareRun(run) {
  // Transport ids are only unique within one fork. The hall keeps items by id, so an id shared between variants would carry its old route along.
  for (const key of VARIANTS) {
    const variant = run.variants?.[key];
    if (!variant) continue;
    variant.frames = variant.frames ?? [];
    for (const frame of variant.frames) {
      for (const transport of frame.material?.transports ?? []) transport.id = `${key}:${transport.id}`;
    }
  }
  return run;
}

function frameIndex(frames, t) {
  let lo = 0;
  let hi = frames.length - 1;
  while (lo < hi) {
    const mid = Math.ceil((lo + hi) / 2);
    if (frames[mid].t <= t) lo = mid;
    else hi = mid - 1;
  }
  return lo;
}

function frameAt(frames, t) {
  return frames.length ? frames[frameIndex(frames, t)] : null;
}

// One row per order that differs between the variants. A rush order only exists in B and C; its due date comes from the scenario.
function orderRows(run, info) {
  const completion = Object.fromEntries(VARIANTS.map((v) => [v, run.variants?.[v]?.order_completion ?? {}]));
  const unfinished = Object.fromEntries(VARIANTS.map((v) => [v, new Set(run.variants?.[v]?.kpis?.unfinished_orders ?? [])]));
  const ids = [...new Set(VARIANTS.flatMap((v) => Object.keys(completion[v])))];
  const rushItems = (run.scenario ?? []).filter((item) => item.type === 'rush_order');
  const rushIds = ids.filter((id) => !(id in completion.A)).sort();
  const rows = ids.map((id) => {
    const rushIndex = rushIds.indexOf(id);
    const rushItem = rushIndex >= 0 ? rushItems[rushIndex] : null;
    const known = info.get(id);
    const due = rushItem ? run.created_min + rushItem.due_in_min : known?.due ?? null;
    const values = Object.fromEntries(VARIANTS.map((v) => [v, completion[v][id] ?? null]));
    const present = VARIANTS.map((v) => values[v]).filter((v) => v !== null);
    const flags = VARIANTS.map((v) => unfinished[v].has(id));
    const changed = rushItem !== null
      || (present.length > 0 && Math.max(...present) - Math.min(...present) >= 1)
      || flags.some((flag) => flag !== flags[0]);
    return { id, rush: rushItem !== null, due, values, unfinished: Object.fromEntries(VARIANTS.map((v, i) => [v, flags[i]])), product: rushItem?.product_id ?? known?.product_id ?? null, changed };
  });
  rows.sort((a, b) => (b.rush - a.rush) || ((a.due ?? Infinity) - (b.due ?? Infinity)) || a.id.localeCompare(b.id));
  return rows;
}

function variantDowntimes(run, key) {
  const seen = new Set();
  const out = [];
  const add = (d) => {
    const id = `${d.machine}:${Math.round(d.start)}`;
    if (seen.has(id)) return;
    seen.add(id);
    out.push(d);
  };
  for (const d of run.variants?.[key]?.downtimes ?? []) add(d);
  if (key === 'C') {
    const solver = run.variants.C?.solver ?? run.recommended;
    for (const d of run.candidates?.[solver]?.downtimes ?? []) add(d);
  }
  if (key !== 'A') {
    for (const item of run.scenario ?? []) {
      if (item.type !== 'machine_down') continue;
      const start = run.created_min + (item.at_offset_min ?? 0);
      add({ machine: item.machine, start: Math.round(start), end: Math.round(start + item.duration_min), reason: item.code || 'Szenario' });
    }
  }
  return out;
}

// ------------------------------------------------------------------------------------------- framing
function buildFraming(ctx) {
  const { fmt, store } = ctx;
  const liveIcon = statusIcon('link');
  const liveState = h('span.zwilling-frame__state');
  const liveFact = h('p.zwilling-frame__fact');
  const twinFact = h('p.zwilling-frame__fact');

  const width = { value: 640 };
  const HEIGHT = 92;
  const Y_LIVE = 13;
  const Y_BRANCH = { A: 32, B: 48, C: 64 };
  const LEFT = 8;
  const RIGHT = 196;

  const ticks = s('g');
  const future = s('line.zwilling-fork__future', { y1: Y_LIVE, y2: Y_LIVE });
  const live = s('line.zwilling-fork__live', { y1: Y_LIVE, y2: Y_LIVE });
  const branches = s('g');
  const forkDot = s('circle.zwilling-fork__fork', { r: 4, cy: Y_LIVE, visibility: 'hidden' });
  const forkLabel = s('text.zwilling-fork__label', { y: Y_LIVE + 15, 'text-anchor': 'end', visibility: 'hidden' });
  const nowDot = s('circle.zwilling-fork__now', { r: 4.5, cy: Y_LIVE });
  const nowLabel = s('text.zwilling-fork__label', { y: Y_LIVE + 4 });
  const hint = s('text.zwilling-fork__hint', { y: Y_BRANCH.B + 4 });
  const svg = s('svg.zwilling-fork', { role: 'img', 'aria-label': 'Zeitachse: Schatten bis jetzt und Abzweig des Zwillings', height: HEIGHT },
    ticks, future, live, branches, forkDot, forkLabel, nowDot, nowLabel, hint);

  let run = null;
  let selected = null;
  let domain = [0, 1];
  let drawnKey = '';

  const x = (t) => LEFT + ((t - domain[0]) / Math.max(1, domain[1] - domain[0])) * (width.value - LEFT - RIGHT);

  function pickDomain(now) {
    if (!run) {
      if (now >= domain[0] && now <= domain[0] + (domain[1] - domain[0]) * 0.55 && domain[1] - domain[0] === 2160) return;
      domain = [now - 720, now + 1440];
      return;
    }
    const ends = VARIANTS.map((v) => variantEnd(run, v)).filter((v) => v !== null);
    const end = Math.max(now, ...ends);
    const span = Math.max(240, end - run.created_min);
    domain = [run.created_min - Math.max(60, span * 0.12), end];
  }

  function rebuild() {
    clear(ticks);
    clear(branches);
    svg.setAttribute('viewBox', `0 0 ${width.value} ${HEIGHT}`);
    svg.setAttribute('width', width.value);
    // Day boundaries: Tag n starts at 06:00.
    const first = Math.ceil((domain[0] + 360) / 1440);
    const last = Math.floor((domain[1] + 360) / 1440);
    for (let day = first; day <= last; day += 1) {
      const t = day * 1440 - 360;
      ticks.append(
        s('line.zwilling-fork__tick', { x1: x(t), x2: x(t), y1: 0, y2: HEIGHT - 14 }),
        s('text.zwilling-fork__axis', { x: x(t), y: HEIGHT - 3, 'text-anchor': 'middle' }, `Tag ${day + 1}`));
    }
    if (run) {
      const xf = x(run.created_min);
      for (const key of VARIANTS) {
        const end = variantEnd(run, key);
        if (end === null) continue;
        const y = Y_BRANCH[key];
        const xe = Math.max(xf + 24, x(end));
        const span = fmt.duration(run.variants[key].sim_minutes ?? run.variants[key].kpis?.makespan);
        const current = key === selected;
        branches.append(s('g', { class: `zwilling-fork__branch zwilling-fork__branch--${key}${current ? ' is-current' : ''}`, 'data-tip': `${run.variants[key].label}: fertig ${fmt.simTime(end)}` },
          s('path.zwilling-fork__path', { d: `M${xf} ${Y_LIVE}C${xf + 12} ${Y_LIVE} ${xf + 6} ${y} ${xf + 20} ${y}H${xe}` }),
          s('circle.zwilling-fork__end', { cx: xe, cy: y, r: 4 }),
          s('text.zwilling-fork__label', { x: xe + 9, y: y + 4 }, `${VARIANT_FORK[key]} · ${span}`)));
      }
      setAttr(forkDot, 'cx', xf);
      setAttr(forkDot, 'visibility', 'visible');
      setAttr(forkLabel, 'x', xf - 7);
      setAttr(forkLabel, 'visibility', 'visible');
      setText(forkLabel, `Abzweig ${fmt.simClock(run.created_min)}`);
      setText(hint, '');
    } else {
      setAttr(forkDot, 'visibility', 'hidden');
      setAttr(forkLabel, 'visibility', 'hidden');
    }
    setAttr(future, 'x1', LEFT);
    setAttr(future, 'x2', width.value - RIGHT);
  }

  function draw() {
    const now = store.clock.now();
    pickDomain(now);
    const key = `${domain[0]}|${domain[1]}|${width.value}|${run?.id ?? ''}|${selected ?? ''}`;
    if (key !== drawnKey) {
      drawnKey = key;
      rebuild();
    }
    const xn = Math.max(LEFT, Math.min(width.value - RIGHT, x(now)));
    setAttr(live, 'x1', LEFT);
    setAttr(live, 'x2', xn.toFixed(1));
    setAttr(nowDot, 'cx', xn.toFixed(1));
    setAttr(nowLabel, 'x', (xn + 9).toFixed(1));
    setText(nowLabel, `Jetzt ${fmt.simClock(now)}`);
    if (!run) {
      setAttr(hint, 'x', (xn + 9).toFixed(1));
      setText(hint, session.waiting ? 'Abzweig läuft …' : 'Noch kein Abzweig');
    }
  }

  const observer = new ResizeObserver((entries) => {
    const next = Math.round(entries[0].contentRect.width);
    if (next > 40 && next !== width.value) {
      width.value = next;
      draw();
    }
  });
  const diagram = h('div.zwilling-frame__diagram', svg);
  observer.observe(diagram);

  const node = h('section.zwilling-frame', { 'aria-label': 'Schatten und Zwilling' },
    h('div.zwilling-frame__mode',
      h('h2.zwilling-frame__title', liveIcon, 'Schatten', liveState),
      h('p.zwilling-frame__text', 'Spiegelt die laufende Anlage über OPC UA, nur lesend'),
      liveFact),
    diagram,
    h('div.zwilling-frame__mode.zwilling-frame__mode--twin',
      h('h2.zwilling-frame__title', h('span.mode.mode--zwilling.zwilling-frame__badge', 'Zwilling')),
      h('p.zwilling-frame__text', 'Abzweig des aktuellen Zustands, läuft schneller als Echtzeit, berührt die Anlage nicht'),
      twinFact));

  return {
    node,
    update() {
      const link = store.link();
      paintIcon(liveIcon, link.level === 'ok' ? 'link' : link.level === 'stale' ? 'warn' : link.level === 'down' ? 'FAULT' : 'info');
      setText(liveState, link.level === 'ok' ? 'live' : link.label.toLowerCase());
      const state = store.state;
      const now = store.clock.now();
      if (state) {
        const factor = state.sim?.factor === 0 ? 'Pause' : `Faktor ${fmt.number(state.sim?.factor)}`;
        setText(liveFact, `${fmt.simTime(now)} · ${factor} · Rev. ${state.plan?.revision ?? fmt.DASH}`);
      }
      if (run) setText(twinFact, `Abzweig ${fmt.simTime(run.created_min)} · ${fmt.duration(Math.max(0, now - run.created_min))} alt`);
      else if (session.waiting) setText(twinFact, session.pending ? `Abzweig ${fmt.simTime(session.pending.created_min)} · rechnet` : 'Rechnet …');
      else setText(twinFact, 'Noch kein Abzweig.');
      draw();
    },
    setRun(next) {
      run = next;
      drawnKey = '';
    },
    setSelected(key) {
      selected = key;
    },
    destroy() {
      observer.disconnect();
    },
  };
}

// ------------------------------------------------------------------------------------------- scenario builder
function buildBuilder(ctx, { onStart }) {
  const { fmt, store } = ctx;
  const box = panel('Szenario');
  const presetButtons = PRESETS.map((preset) => ({
    preset,
    node: h('button.btn.zwilling-preset', {
      type: 'button',
      'aria-pressed': 'false',
      onclick: () => {
        session.scenario = preset.items();
        session.editing = true;
        paintList();
      },
    }, preset.label),
  }));
  const list = h('ol.zwilling-items', { 'aria-label': 'Störungen dieses Szenarios' });
  const summaryList = h('ul.zwilling-summary', { 'aria-label': 'Störungen dieses Szenarios' });
  const editButton = h('button.btn.btn--sm.btn--ghost.zwilling-summary__edit', {
    type: 'button',
    onclick: () => {
      session.editing = true;
      paintList();
    },
  }, 'Bearbeiten');
  const emptyNote = h('p.empty', 'Keine Störung gewählt. Nimm eine Vorlage oder füge eine Zeile hinzu.');
  const addButtons = ITEM_TYPES.map((type) => h('button.btn.btn--sm', {
    type: 'button',
    onclick: () => {
      session.scenario.push(ITEM_DEFAULTS[type.value]());
      session.editing = true;
      paintList();
    },
  }, `+ ${type.label}`));

  const monte = segmented(MONTE_OPTIONS, session.monte, (value) => {
    session.monte = value;
    monte.set(value);
  }, 'Monte Carlo');
  const limit = segmented(LIMIT_OPTIONS, session.limit, (value) => {
    session.limit = value;
    limit.set(value);
  }, 'Zeitlimit je Solver');
  const addRow = h('div.toolbar.zwilling-add', addButtons);
  const start = h('button.btn.btn--primary.zwilling-start', { type: 'button', onclick: onStart }, 'Zwilling starten');
  const reason = h('p.zwilling-start__why');
  const alreadyDown = h('p.zwilling-already', { role: 'status', hidden: true }, statusIcon('info', { size: 10 }), h('span'));

  box.body.append(
    h('div.zwilling-presets', presetButtons.map((p) => p.node)),
    list,
    summaryList,
    alreadyDown,
    editButton,
    emptyNote,
    addRow,
    h('div.zwilling-options',
      h('div.field', { 'data-tip': 'Wiederholt B und C mit zufälligen Dauern, Ausschuss und Störungen und zeigt die Streuung. Dauert länger.' }, h('span.field__label', 'Monte Carlo'), monte.node),
      h('div.field', { 'data-tip': 'So lange rechnen CP-SAT und Simulated Annealing höchstens an Variante C.' }, h('span.field__label', 'Zeitlimit je Solver'), limit.node)),
    start,
    reason);

  function numberField(label, item, key, attrs, onChange) {
    return field(label, h('input.input.mono', {
      type: 'number',
      ...attrs,
      value: item[key],
      oninput: (event) => {
        const raw = event.target.value;
        item[key] = raw === '' ? null : Number(raw);
        onChange();
      },
    }));
  }

  function selectOptions(options, current) {
    return options.map((o) => h('option', { value: o.value, selected: o.value === current }, o.label));
  }

  function itemRow(item, index) {
    const catalog = store.catalog;
    const machines = (catalog?.machines ?? MACHINES.map((id) => ({ id, name: fmt.machineName(id) }))).map((m) => ({ value: m.id, label: `${m.id} · ${m.name}` }));
    const products = (catalog?.products ?? [{ id: item.product_id, name: item.product_id }]).map((p) => ({ value: p.id, label: `${p.id} · ${p.name}` }));
    const summary = h('p.zwilling-item__summary');
    const row = h('li.zwilling-item');

    const refresh = () => {
      const { error, warn } = checkItem(item, store.catalog, fmt);
      row.classList.toggle('is-invalid', Boolean(error));
      clear(summary);
      if (error) summary.append(statusIcon('FAULT', { size: 10 }), ` ${error}`);
      else if (warn) summary.append(statusIcon('warn', { size: 10 }), ` ${warn}`);
      else summary.append(describeItem(item, fmt));
      paintStart();
      paintPresets();
    };

    const type = h('select.input', {
      'aria-label': 'Art der Störung',
      onchange: (event) => {
        session.scenario[index] = ITEM_DEFAULTS[event.target.value]();
        paintList();
      },
    }, selectOptions(ITEM_TYPES, item.type));
    const remove = h('button.btn.btn--ghost.btn--icon.zwilling-item__remove', {
      type: 'button',
      'aria-label': 'Störung entfernen',
      'data-tip': 'Zeile entfernen',
      onclick: () => {
        session.scenario.splice(index, 1);
        paintList();
      },
    }, '×');

    let what;
    let numbers;
    if (item.type === 'rush_order') {
      what = h('select.input', {
        'aria-label': 'Produkt',
        onchange: (event) => {
          item.product_id = event.target.value;
          refresh();
        },
      }, selectOptions(products, item.product_id));
      numbers = [
        numberField('Menge (Stangen)', item, 'qty_bars', { min: 1, step: 10 }, refresh),
        numberField('Termin in (min)', item, 'due_in_min', { min: 1, step: 30 }, refresh),
      ];
    } else {
      what = h('select.input', {
        'aria-label': 'Maschine',
        onchange: (event) => {
          item.machine = event.target.value;
          refresh();
        },
      }, selectOptions(machines, item.machine));
      numbers = [
        numberField('Start nach (min)', item, 'at_offset_min', { min: 0, step: 30 }, refresh),
        numberField('Dauer (min)', item, 'duration_min', { min: 1, step: 30 }, refresh),
      ];
      if (item.type === 'slowdown') numbers.push(numberField('Faktor (× Dauer)', item, 'factor', { min: 1.1, step: 0.1 }, refresh));
    }
    row.append(
      h('div.zwilling-item__head', type, what, remove),
      h('div.zwilling-item__numbers', numbers),
      summary);
    refresh();
    return row;
  }

  function paintPresets() {
    const current = JSON.stringify(session.scenario);
    for (const { preset, node } of presetButtons) setAttr(node, 'aria-pressed', String(JSON.stringify(preset.items()) === current));
  }

  function paintStart() {
    const errors = session.scenario.filter((item) => checkItem(item, store.catalog, fmt).error);
    let why = '';
    if (session.waiting) why = 'Ein Lauf rechnet gerade.';
    else if (!session.scenario.length) why = 'Mindestens eine Störung auswählen.';
    else if (errors.length) why = 'Eine Zeile ist noch nicht gültig.';
    start.disabled = Boolean(why);
    setText(reason, why || 'Abzweig vom jetzigen Zustand. Die Anlage läuft unberührt weiter.');
  }

  // With a result on screen the rows fold to one line each, so the decision below stays in view.
  function paintList() {
    const folded = Boolean(session.run || session.waiting) && !session.editing && session.scenario.length > 0;
    clear(list);
    clear(summaryList);
    if (folded) {
      for (const item of session.scenario) summaryList.appendChild(h('li', describeItem(item, fmt)));
    } else {
      session.scenario.forEach((item, index) => list.appendChild(itemRow(item, index)));
    }
    summaryList.hidden = !folded;
    editButton.hidden = !folded;
    addRow.hidden = folded;
    emptyNote.hidden = session.scenario.length > 0;
    list.hidden = folded || session.scenario.length === 0;
    paintPresets();
    paintStart();
  }

  function paintAlreadyDown() {
    const notes = alreadyDownNotes(session.scenario, store, fmt);
    alreadyDown.hidden = !notes.length;
    setText(alreadyDown.lastChild, notes.join(' '));
  }

  const offCatalog = store.on('catalog', paintList);
  paintList();
  paintAlreadyDown();

  return {
    node: box.node,
    paint: paintList,
    paintAlreadyDown,
    paintStart,
    destroy: offCatalog,
  };
}

// ------------------------------------------------------------------------------------------- progress, intro, failure
function buildIntro() {
  const box = panel('Wirkung');
  const card = (key, title, text) => h('div.zwilling-intro__card',
    h('h3.zwilling-intro__title', variantMark(key), `${key} ${title}`),
    h('p.zwilling-intro__text', text));
  box.body.append(
    h('p.zwilling-intro__lead', 'Noch kein Zwillingslauf. Ein Lauf zweigt den Zustand der Anlage ab und fährt ihn drei Mal bis zum Ende, ohne die Anlage zu berühren.'),
    h('div.zwilling-intro__cards',
      card('A', VARIANT_HEAD.A, 'Der aktive Plan, ohne Szenario. Der Bezugswert.'),
      card('B', VARIANT_HEAD.B, 'Der aktive Plan bleibt, das Szenario trifft ihn. Neue Lose hängen hinten an. Das ist der Schaden.'),
      card('C', VARIANT_HEAD.C, 'CP-SAT und Simulated Annealing planen in Kenntnis des Szenarios neu, der Zwilling fährt beide. Das ist der Nutzen.')));
  return box.node;
}

function buildProgress(ctx, { onAbandon }) {
  const { fmt, store } = ctx;
  const box = panel('Zwilling rechnet');
  const elapsed = h('span.zwilling-progress__elapsed', '0,0 s');
  const budget = meter({ label: 'Verstrichene Zeit gegen das Zeitlimit' });
  const forkLine = h('p.zwilling-progress__line');
  const plantLine = h('p.zwilling-progress__line');
  const problem = h('p.zwilling-progress__problem', { hidden: true });
  const requested = session.requested ?? { limit: session.limit, monte: session.monte };
  const tasks = [
    'A: aktueller Plan, ungestört',
    'B: aktueller Plan mit Szenario',
    `C: CP-SAT und Simulated Annealing planen neu, je höchstens ${requested.limit} s, der Zwilling fährt beide`,
  ];
  if (requested.monte) tasks.push(`Monte Carlo: bis zu ${requested.monte} Läufe mit Zufall für B und C`);
  box.actions.append(h('button.btn.btn--sm.btn--ghost', { type: 'button', onclick: onAbandon }, 'Nicht mehr warten'));
  box.body.append(
    h('div.zwilling-progress',
      h('div.zwilling-progress__time', elapsed, h('span.zwilling-progress__label', `Zeitlimit Solver ${requested.limit} s`), budget),
      h('div',
        forkLine,
        h('ul.zwilling-progress__tasks', tasks.map((task) => h('li', task))),
        h('p.zwilling-progress__honest', 'Das MES meldet nur „rechnet“ oder „fertig“, deshalb gibt es hier keine Prozentzahl. Die Leiste zeigt nur die verstrichene Zeit gegen das Solver-Limit.'),
        plantLine,
        problem)));
  return {
    node: box.node,
    paint() {
      const seconds = (performance.now() - session.startedAt) / 1000;
      setText(elapsed, fmt.seconds(seconds, 1));
      const over = seconds > requested.limit;
      updateMeter(budget, Math.min(1, seconds / requested.limit), over ? 'setup' : null);
      const pending = session.pending;
      setText(forkLine, pending ? `Abzweig von ${fmt.simTime(pending.created_min)}, Ereignis ${fmt.number(pending.forked_seq)}.` : 'Abzweig wird angefordert.');
      const now = store.clock.now();
      const ahead = pending ? Math.max(0, now - pending.created_min) : 0;
      setText(plantLine, ahead >= 1 ? `Die Anlage läuft währenddessen unberührt weiter, inzwischen ${fmt.duration(ahead)} Sim-Zeit über den Abzweig hinaus.` : 'Die Anlage läuft währenddessen unberührt weiter.');
      problem.hidden = !session.pollProblem;
      setText(problem, session.pollProblem ?? '');
    },
  };
}

function buildFailure(note, onRetry) {
  const box = panel('Zwilling fehlgeschlagen');
  box.body.append(
    h('p.zwilling-failure', statusIcon('FAULT'), note || 'Der Lauf ist ohne Ergebnis abgebrochen.'),
    h('button.btn', { type: 'button', onclick: onRetry }, 'Erneut starten'));
  return box.node;
}

// ------------------------------------------------------------------------------------------- impact
const METRICS = [
  { key: 'makespan', label: 'Makespan', tip: 'Zeit vom Abzweig, bis der letzte Auftrag fertig ist', read: (k) => k.makespan, kind: 'minutes' },
  { key: 'total_tardiness', label: 'Gesamtverspätung', tip: 'Summe der Verspätung über alle Aufträge', read: (k) => k.total_tardiness, kind: 'minutes' },
  { key: 'late_orders', label: 'Verspätete Aufträge', tip: 'Anzahl der Aufträge, die nach ihrem Termin fertig werden', read: (k) => k.late_orders, kind: 'count' },
  { key: 'setups', label: 'Rüstvorgänge', tip: 'Anzahl der Rüstvorgänge bis zum Ende', read: (k) => k.setups, kind: 'count' },
  { key: 'energy_kwh', label: 'Energie', tip: 'Energie vom Abzweig bis zum Ende. Ein Eilauftrag kostet zusätzliche Energie, das ist kein Fehler des Plans.', read: (k) => k.energy_kwh, kind: 'kwh' },
  { key: 'unfinished', label: 'Unvollständige Aufträge', tip: 'Bis zum Horizont nicht fertig. Sie zählen mit dem Horizont als Fertigstellung, damit nichts besser aussieht, weil es nicht liefert.', read: (k) => k.unfinished_orders?.length ?? 0, kind: 'count' },
];

function metricValue(metric, value, fmt) {
  if (value === null || value === undefined) return fmt.DASH;
  if (metric.kind === 'minutes') return fmt.duration(value);
  if (metric.kind === 'kwh') return fmt.kwh(value);
  return fmt.number(value);
}

function deltaBadge(metric, delta, fmt) {
  if (delta === null || delta === undefined) return h('span.faint', fmt.DASH);
  const settled = metric.kind === 'kwh' ? Math.abs(delta) < 0.5 : Math.round(delta) === 0;
  if (settled) return h('span.tag.tag--mono.zwilling-delta', '±0');
  const text = metric.kind === 'minutes' ? fmt.duration(delta, { signed: true })
    : metric.kind === 'kwh' ? `${fmt.signed(delta)} kWh`
      : fmt.signed(delta);
  // Lower is better for every metric on this table, in both comparisons.
  const worse = delta > 0;
  return h(`span.tag.tag--mono.zwilling-delta.${worse ? 'tag--setup' : 'tag--run'}`, statusIcon(worse ? 'warn' : 'ok', { size: 10 }), text);
}

function verdict(run, fmt, store) {
  const { A, B, C } = run.variants;
  const parts = [];
  const a = A?.kpis ?? {};
  const b = B?.kpis ?? {};
  const damage = (b.total_tardiness ?? 0) - (a.total_tardiness ?? 0);
  const extraLate = (b.late_orders ?? 0) - (a.late_orders ?? 0);
  const longer = (b.makespan ?? 0) - (a.makespan ?? 0);
  if (damage > 0) {
    parts.push(`Das Szenario kostet ${fmt.duration(damage)} Verspätung${extraLate > 0 ? ` und ${fmt.plural(extraLate, 'weiteren verspäteten Auftrag', 'weitere verspätete Aufträge')}` : ''}.`);
  } else if (longer > 0) {
    parts.push(`Das Szenario erhöht die Verspätung nicht, verlängert aber den Makespan um ${fmt.duration(longer)}.`);
  } else {
    parts.push('Das Szenario schadet dem aktuellen Plan laut Zwilling nicht.');
    parts.push(...alreadyDownNotes(run.scenario, store, fmt));
  }
  if (b.stalled) parts.push('Variante B läuft sogar fest.');
  if (b.unfinished_orders?.length) parts.push(`In Variante B ${b.unfinished_orders.length === 1 ? 'bleibt 1 Auftrag' : `bleiben ${b.unfinished_orders.length} Aufträge`} unvollständig.`);

  if (!C?.kpis) {
    parts.push('Eine Neuplanung liegt nicht vor.');
    return parts.join(' ');
  }
  const c = C.kpis;
  const gain = (b.total_tardiness ?? 0) - (c.total_tardiness ?? 0);
  const shorter = (b.makespan ?? 0) - (c.makespan ?? 0);
  const moreLate = (c.late_orders ?? 0) - (b.late_orders ?? 0);
  if (gain > 0) {
    parts.push(damage > 0 && gain <= damage ? `Die Neuplanung holt davon ${fmt.duration(gain)} zurück.` : `Die Neuplanung spart ${fmt.duration(gain)} Verspätung gegenüber B.`);
    if (moreLate > 0) parts.push(`Dafür ${moreLate === 1 ? 'wird 1 Auftrag' : `werden ${moreLate} Aufträge`} mehr verspätet.`);
  } else if (gain < 0) {
    parts.push(`Die Neuplanung ist bei der Verspätung um ${fmt.duration(-gain)} schlechter als der aktuelle Plan.`);
  } else if (shorter > 0) {
    parts.push(`Die Neuplanung ändert die Verspätung nicht, verkürzt aber den Makespan um ${fmt.duration(shorter)}.`);
  } else {
    parts.push('Die Neuplanung bringt hier keinen Vorteil gegenüber dem aktuellen Plan.');
  }
  if (c.stalled) parts.push('Vorsicht: auch Variante C läuft fest.');
  if (c.unfinished_orders?.length) parts.push(`In Variante C ${c.unfinished_orders.length === 1 ? 'bleibt 1 Auftrag' : `bleiben ${c.unfinished_orders.length} Aufträge`} unvollständig.`);
  return parts.join(' ');
}

function buildHero(run, ctx) {
  const { fmt, store } = ctx;
  const rows = VARIANTS.map((v) => run.variants?.[v]).filter(Boolean);
  const simMinutes = sum(rows.map((r) => r.sim_minutes ?? r.kpis?.makespan ?? 0));
  const wall = sum(rows.map((r) => r.wall_seconds ?? 0));
  const factor = store.state?.sim?.factor ?? 0;
  const speedup = wall > 0 ? (simMinutes * 60) / wall : Math.max(...rows.map((r) => r.speedup ?? 0));
  const realMinutes = factor > 0 ? simMinutes / factor : null;
  const tip = `Simulationszeit der drei Varianten zusammen, ohne die Rechenzeit der Solver: ${fmt.number(simMinutes)} Sim-Minuten in ${wallWords(wall, fmt)}.`;
  return h('div.zwilling-hero', { 'data-tip': tip },
    h('p.zwilling-hero__pre', `${spanWords(simMinutes, fmt)} in ${wallWords(wall, fmt)}`),
    h('p.zwilling-hero__figure', `${fmt.number(roundSignificant(speedup, 2))}-fach`),
    h('p.zwilling-hero__post', 'schneller als Echtzeit'),
    realMinutes !== null
      ? h('p.zwilling-hero__compare', `A, B und C zusammen, ohne Solver. Die Anlage braucht dafür bei Faktor ${fmt.number(factor)} rund ${fmt.duration(realMinutes)}.`)
      : h('p.zwilling-hero__compare', 'A, B und C zusammen, ohne Solver.'));
}

function buildCompare(run, ctx) {
  const { fmt } = ctx;
  const head = (key) => {
    const variant = run.variants?.[key];
    return h('th.zwilling-compare__variant', { scope: 'col', 'data-tip': variant?.label ?? '' },
      h('span.zwilling-compare__letter', variantMark(key), key),
      h('span.zwilling-compare__name', VARIANT_HEAD[key]),
      key === 'C' && variant?.solver ? h('span.zwilling-compare__sub.faint', fmt.solverLabel(variant.solver)) : null,
      variant?.kpis?.stalled ? h('span.zwilling-compare__sub', h('span.tag.tag--fault', 'läuft fest')) : null);
  };
  const body = h('tbody');
  for (const metric of METRICS) {
    const value = (key) => {
      const kpis = run.variants?.[key]?.kpis;
      return kpis ? metric.read(kpis) : null;
    };
    const diff = (x, y) => (x === null || y === null ? null : x - y);
    const a = value('A');
    const b = value('B');
    const c = value('C');
    const unfinishedTip = metric.key === 'unfinished'
      ? VARIANTS.map((key) => `${key}: ${run.variants?.[key]?.kpis?.unfinished_orders?.join(', ') || 'keine'}`).join(' · ')
      : undefined;
    body.appendChild(h('tr', { 'data-tip': unfinishedTip },
      h('th.zwilling-compare__metric', { scope: 'row', 'data-tip': metric.tip }, metric.label),
      h('td.num.zwilling-compare__value', metricValue(metric, a, fmt)),
      h('td.num.zwilling-compare__value', metricValue(metric, b, fmt)),
      h('td.zwilling-compare__delta', deltaBadge(metric, diff(b, a), fmt)),
      h('td.num.zwilling-compare__value', metricValue(metric, c, fmt)),
      h('td.zwilling-compare__delta', deltaBadge(metric, diff(c, b), fmt))));
  }
  return h('table.zwilling-compare',
    h('thead',
      h('tr',
        h('th', { scope: 'col' }, h('span.faint', 'Kennzahl')),
        head('A'),
        head('B'),
        h('th.zwilling-compare__deltahead', { scope: 'col' }, h('span.zwilling-compare__name', 'Schaden'), h('span.faint', 'B gegen A')),
        head('C'),
        h('th.zwilling-compare__deltahead', { scope: 'col' }, h('span.zwilling-compare__name', 'Nutzen der Neuplanung'), h('span.faint', 'C gegen B')))),
    body);
}

function buildImpact(run, ctx, { onWatch }) {
  const { fmt } = ctx;
  const watch = h('button.btn.btn--sm', { type: 'button', onclick: onWatch, disabled: !VARIANTS.some((v) => run.variants?.[v]?.frames?.length) }, 'Wiedergabe ansehen');
  const box = panel('Wirkung', { dense: true, actions: [watch] });
  const recommended = run.recommended ? run.candidates?.[run.recommended] : null;
  const notes = [];
  if (run.note) notes.push(h('p.zwilling-impact__note', run.note));
  box.body.append(
    h('div.zwilling-impact__top',
      buildHero(run, ctx),
      h('div.zwilling-impact__verdict',
        h('p.zwilling-scenario', (run.scenario ?? []).map((item) => describeItem(item, fmt)).join(' · ')),
        h('p.zwilling-verdict', verdict(run, fmt, ctx.store)),
        recommended
          ? h('p.zwilling-recommend', statusIcon('ok', { size: 11 }), 'Empfohlener Solver: ', h('strong', recommended.label ?? fmt.solverLabel(run.recommended)),
            ` (bestes Ergebnis im Zwilling, ${fmt.seconds(recommended.solve_seconds)} Rechenzeit)`)
          : h('p.zwilling-recommend', 'Kein Solver empfohlen: kein gültiger Kandidat.'),
        notes)),
    buildCompare(run, ctx));
  return box.node;
}

// ------------------------------------------------------------------------------------------- orders
function completionCell(row, key, fmt) {
  const value = row.values[key];
  if (value === null) return h('td.zwilling-orders__when.faint', row.rush && key === 'A' ? 'nicht enthalten' : fmt.DASH);
  const late = row.due === null ? null : value - row.due;
  return h('td.zwilling-orders__when',
    h('span.mono', fmt.simTime(value)),
    row.unfinished[key]
      ? h('span.tag.tag--fault', 'unvollständig')
      : late === null ? null
        : h(`span.mono.zwilling-orders__late${late > 0 ? '.is-late' : ''}`, fmt.duration(late, { signed: true })));
}

function orderChart(row, scale) {
  const svg = s('svg.zwilling-orders__chart', { viewBox: `0 0 ${scale.width} 20`, width: scale.width, height: 20, 'aria-hidden': 'true' },
    s('line.zwilling-orders__rule', { x1: 0, x2: scale.width, y1: 10, y2: 10 }));
  if (row.due !== null) svg.appendChild(s('line.zwilling-orders__due', { x1: scale.x(row.due), x2: scale.x(row.due), y1: 2, y2: 18 }));
  for (const key of VARIANTS) {
    const value = row.values[key];
    if (value === null) continue;
    svg.appendChild(s(`path.zwilling-orders__mark.zwilling-orders__mark--${key}`, { d: MARKS[key], transform: `translate(${(scale.x(value) - 6).toFixed(1)} 4)` }));
  }
  return svg;
}

function buildOrders(run, ctx) {
  const { fmt } = ctx;
  const rows = orderRows(run, session.orderInfo);
  const changed = rows.filter((row) => row.changed);
  const legend = h('span.zwilling-legend',
    VARIANTS.map((key) => h('span.zwilling-legend__item', variantMark(key, 10), key)),
    h('span.zwilling-legend__item', s('svg.zwilling-legend__due', { viewBox: '0 0 4 12', width: 4, height: 12, 'aria-hidden': 'true' }, s('line', { x1: 2, x2: 2, y1: 0, y2: 12 })), 'Termin'));
  const box = panel('Aufträge, die sich ändern', { dense: true, actions: [
    h('span.tag.tag--mono', { 'data-tip': 'Geänderte Aufträge von allen Aufträgen dieses Laufs' }, `${changed.length} von ${rows.length}`),
    legend,
  ] });
  if (!changed.length) {
    box.body.appendChild(h('p.empty', 'Kein Auftrag ändert seine Fertigstellung: das Szenario trifft keinen offenen Auftrag spürbar.'));
    return box.node;
  }
  const values = changed.flatMap((row) => [row.due, ...VARIANTS.map((key) => row.values[key])]).filter((value) => value !== null);
  const lo = Math.min(...values);
  const hi = Math.max(...values);
  const width = 128;
  const scale = { width, x: (t) => 7 + ((t - lo) / Math.max(1, hi - lo)) * (width - 14) };
  const body = h('tbody');
  for (const row of changed) {
    const tip = VARIANTS.map((key) => {
      const value = row.values[key];
      if (value === null) return `${key}: ${fmt.DASH}`;
      const late = row.due === null ? '' : ` (${fmt.duration(value - row.due, { signed: true })})`;
      return `${key}: ${fmt.simTime(value)}${late}${row.unfinished[key] ? ', unvollständig' : ''}`;
    }).join(' · ');
    body.appendChild(h('tr',
      h('td.nowrap', { 'data-tip': row.product ? fmt.productName(row.product) : undefined },
        row.rush ? h('span', { 'data-tip': 'Nur im Zwilling, die echte Nummer entsteht beim Übernehmen' }, fmt.orderId(row.id)) : h('span.mono', row.id),
        row.product ? h('span.mono.zwilling-orders__product', row.product) : null,
        row.rush ? h('span.tag.zwilling-orders__rush', { 'data-tip': 'Nur im Szenario, noch kein echter Auftrag' }, 'Eil') : null),
      h('td.mono.nowrap', row.due === null ? fmt.DASH : fmt.simTime(row.due)),
      VARIANTS.map((key) => completionCell(row, key, fmt)),
      h('td.zwilling-orders__plot', { 'data-tip': tip }, orderChart(row, scale))));
  }
  box.body.appendChild(h('div.zwilling-orders__scroll',
    h('table.tbl.zwilling-orders',
      h('thead', h('tr', h('th', 'Auftrag'), h('th', 'Termin'),
        VARIANTS.map((key) => h('th', h('span.zwilling-orders__head', variantMark(key, 10), `${key} fertig`))),
        h('th', { 'data-tip': 'Fertigstellung je Variante gegen den Termin, alle Zeilen auf derselben Zeitachse' }, 'Fertig gegen Termin'))),
      body)));
  return box.node;
}

// ------------------------------------------------------------------------------------------- Monte Carlo
function rangeRow(key, range, scale, deterministic, format) {
  const left = scale(range.p10);
  const right = scale(range.p90);
  const tip = `${key}: P10 ${format(range.p10)} · P50 ${format(range.p50)} · P90 ${format(range.p90)}${deterministic === null ? '' : ` · ohne Zufall ${format(deterministic)}`}`;
  return h('div.zwilling-range__row', { 'data-tip': tip },
    h('span.zwilling-range__key', variantMark(key, 10), key),
    h('span.zwilling-range__track',
      deterministic === null ? null : h('span.zwilling-range__det', { style: { left: `${scale(deterministic)}%` } }),
      h(`span.zwilling-range__bar.zwilling-range__bar--${key}`, { style: { left: `${left}%`, width: `${Math.max(0, right - left)}%` } }),
      h('span.zwilling-range__median', { style: { left: `${scale(range.p50)}%` } })),
    h('span.zwilling-range__values.mono', `${format(range.p10)} · ${format(range.p50)} · ${format(range.p90)}`));
}

function monteInsight(mc) {
  const b = mc.B?.total_tardiness;
  const c = mc.C?.total_tardiness;
  if (!b || !c) return '';
  if (c.p90 < b.p10) return 'Selbst der ungünstige Fall von C (P90) liegt unter dem günstigen Fall von B (P10): die Neuplanung gewinnt auch mit Streuung.';
  if (c.p50 < b.p50) return 'Im Median ist C besser, die Bereiche überlappen aber: ein wahrscheinlicher, kein sicherer Gewinn.';
  if (c.p50 === b.p50) return 'Im Median gleich: mit Streuung zeigt sich kein Vorteil für C.';
  return 'Mit Streuung liegt C im Median schlechter als B: die Neuplanung ist hier nicht robust.';
}

function buildMonteCarlo(run, ctx) {
  const { fmt } = ctx;
  const mc = run.monte_carlo;
  const box = panel('Streuung (Monte Carlo)');
  if (!mc || !mc.n) {
    box.body.appendChild(h('p.zwilling-mc__off', 'Monte Carlo war aus. Die Zahlen oben sind eine Fahrt ohne Zufall: geplante Dauern, kein Ausschuss, keine zufälligen Störungen. Für die Streuung 10 oder 30 Läufe wählen und neu starten.'));
    return box.node;
  }
  const requested = session.requested?.monte;
  box.actions.append(h('span.tag.tag--mono', { 'data-tip': requested && requested !== mc.n ? `Angefragt waren ${requested} Läufe.` : 'Läufe je Variante mit zufälligen Dauern, Ausschuss und Störungen' }, `n = ${fmt.number(mc.n)}`));
  const metrics = [
    { key: 'total_tardiness', label: 'Gesamtverspätung' },
    { key: 'makespan', label: 'Makespan' },
  ];
  const groups = metrics.map((metric) => {
    const ranges = [mc.B?.[metric.key], mc.C?.[metric.key]].filter(Boolean);
    const dets = ['B', 'C'].map((key) => run.variants?.[key]?.kpis?.[metric.key]).filter((v) => v !== null && v !== undefined);
    const values = [...ranges.flatMap((r) => [r.p10, r.p90]), ...dets];
    const lo = Math.min(...values);
    const hi = Math.max(...values);
    const pad = Math.max(1, (hi - lo) * 0.08);
    const min = lo - pad;
    const max = hi + pad;
    const scale = (v) => ((v - min) / (max - min)) * 100;
    const format = (v) => fmt.duration(v);
    return h('div.zwilling-range',
      h('div.zwilling-range__head', h('span', metric.label), h('span.zwilling-range__legend.faint', 'P10 · P50 · P90')),
      ['B', 'C'].map((key) => (mc[key]?.[metric.key] ? rangeRow(key, mc[key][metric.key], scale, run.variants?.[key]?.kpis?.[metric.key] ?? null, format) : null)),
      h('div.zwilling-range__axis', h('span.mono', format(Math.max(0, min))), h('span.mono', format(max)), h('span.zwilling-range__axispad')));
  });
  box.body.append(
    h('div.zwilling-mc', groups),
    h('p.zwilling-mc__insight', monteInsight(mc)),
    h('p.zwilling-mc__legend', h('span.zwilling-mc__swatch'), 'P10 bis P90, Strich: P50, Ring: Fahrt ohne Zufall'));
  return box.node;
}

// ------------------------------------------------------------------------------------------- apply
function buildApply(ctx, { onRerun }) {
  const { fmt, store, api } = ctx;
  const box = panel('Übernehmen');
  const content = h('div.zwilling-apply');
  box.body.appendChild(content);
  box.node.hidden = true;
  let run = null;
  const ageText = h('p.zwilling-apply__age');
  const ageMeter = meter({ label: 'Anteil des vorhergesagten Zeitraums, der schon vergangen ist' });
  const ageNote = h('p.zwilling-apply__agenote');
  const rerun = h('button.btn.btn--sm', { type: 'button', hidden: true, onclick: () => onRerun(run) }, 'Neu rechnen');
  const applyButton = h('button.btn.btn--primary.zwilling-apply__go', { type: 'button' }, 'Diesen Plan übernehmen');
  const result = h('div.zwilling-apply__result', { role: 'status' });
  let busy = false;

  applyButton.addEventListener('click', async () => {
    if (!run || busy) return;
    busy = true;
    session.applyError = null;
    paintResult();
    try {
      const solver = session.solver ?? run.recommended;
      const response = await api.post(`/api/twin/runs/${encodeURIComponent(run.id)}/apply`, solver ? { solver } : {}, { quiet: true });
      session.applied = { runId: run.id, proposalId: response.proposal_id, solver };
    } catch (error) {
      session.applyError = { status: error.status, message: error.message, newProposal: error.body?.new_proposal_id ?? null };
    } finally {
      busy = false;
      paintResult();
    }
  });

  function solverRows() {
    const candidates = run.candidates ?? {};
    const names = Object.keys(candidates).filter((name) => name !== 'keep');
    if (!names.length) return null;
    const group = `zwilling-solver-${run.id}`;
    return h('fieldset.zwilling-solvers',
      h('legend.field__label', 'Plan von'),
      names.map((name) => {
        const candidate = candidates[name];
        const usable = candidate.valid !== false && candidate.status !== 'no_solution';
        const twin = candidate.twin ?? candidate.metrics ?? {};
        const facts = usable
          ? `Verspätung ${fmt.duration(twin.total_tardiness)} · Makespan ${fmt.duration(twin.makespan)}`
          : candidate.note || (candidate.violations?.length ? `${fmt.plural(candidate.violations.length, 'Regelverstoß', 'Regelverstöße')}` : 'keine Lösung');
        return h(`label.zwilling-solver${usable ? '' : '.is-disabled'}`,
          h('input', {
            type: 'radio',
            name: group,
            value: name,
            checked: name === (session.solver ?? run.recommended),
            disabled: !usable,
            onchange: () => { session.solver = name; },
          }),
          h('span.zwilling-solver__name', candidate.label ?? fmt.solverLabel(name)),
          name === run.recommended ? h('span.tag.tag--run', 'Empfohlen') : null,
          !usable ? h('span.tag.tag--setup', 'ungültig') : null,
          h('span.zwilling-solver__time.mono', { 'data-tip': 'Rechenzeit des Solvers' }, fmt.seconds(candidate.solve_seconds)),
          h('span.zwilling-solver__facts.mono', facts));
      }));
  }

  function whatHappens() {
    const rushItems = (run.scenario ?? []).filter((item) => item.type === 'rush_order');
    const hypothetical = (run.scenario ?? []).filter((item) => item.type !== 'rush_order');
    const steps = [];
    steps.push(rushItems.length
      ? `Legt ${rushItems.map((item) => `den Eilauftrag ${item.product_id} (${fmt.number(item.qty_bars)} Stangen, Termin in ${fmt.duration(item.due_in_min)})`).join(' und ')} wirklich an.`
      : 'Legt keinen Auftrag an: dieses Szenario enthält keinen Eilauftrag.');
    steps.push(hypothetical.length
      ? `Plant vom jetzigen Zustand der Anlage neu, ohne ${[...new Set(hypothetical.map((item) => fmt.machineName(item.machine)))].join(' und ')} als gestört anzunehmen: die Störung ist nur eine Annahme.`
      : 'Plant vom jetzigen Zustand der Anlage neu.');
    steps.push('Startet dabei aus der Reihenfolge dieses Zwillingsplans und prüft das Ergebnis wie jeden anderen Plan.');
    steps.push('Das Ergebnis kommt als Vorschlag in die Kette unten. Erst dort wird angenommen oder verworfen, nichts läuft automatisch.');
    return h('ol.zwilling-apply__steps', steps.map((step) => h('li', step)));
  }

  function paintResult() {
    clear(result);
    const applied = session.applied?.runId === run?.id ? session.applied : null;
    if (!run?.applicable) {
      applyButton.disabled = true;
      result.append(h('p.zwilling-apply__blocked', statusIcon('warn'), ` Dieser Lauf lässt sich nicht übernehmen. ${run?.note || 'Das MES hat ihn als nicht anwendbar gekennzeichnet.'} Die Zahlen oben bleiben als Prognose gültig; zum Übernehmen neu rechnen.`));
      rerun.hidden = false;
      return;
    }
    applyButton.disabled = busy || Boolean(applied);
    if (busy) result.append(h('p.zwilling-apply__state', 'Wird an das MES gesendet …'));
    if (session.applyError) {
      const error = session.applyError;
      result.append(h('p.zwilling-apply__blocked', statusIcon(error.status === 409 ? 'warn' : 'FAULT'), ` ${error.message}`));
      if (error.newProposal) result.append(h('p.zwilling-apply__state', `Das MES rechnet neu: Vorschlag ${error.newProposal}.`));
      rerun.hidden = false;
    }
    if (applied) {
      const proposal = store.proposals.open?.id === applied.proposalId
        ? store.proposals.open
        : store.proposals.history?.find((p) => p.id === applied.proposalId) ?? (store.lastProposal?.id === applied.proposalId ? store.lastProposal : null);
      const status = proposal?.status ?? 'computing';
      const texts = {
        computing: `Vorschlag ${applied.proposalId} wird berechnet. Die Kette unten zeigt den Fortschritt.`,
        open: `Vorschlag ${applied.proposalId} liegt bereit: unten in der Kette annehmen oder verwerfen.`,
        accepted: `Vorschlag ${applied.proposalId} ist angenommen und läuft als aktiver Plan.`,
        rejected: `Vorschlag ${applied.proposalId} wurde verworfen. Der aktive Plan bleibt.`,
        superseded: `Vorschlag ${applied.proposalId} ist überholt${proposal?.status_reason ? `: ${proposal.status_reason}` : '.'}`,
        expired: `Vorschlag ${applied.proposalId} ist abgelaufen${proposal?.status_reason ? `: ${proposal.status_reason}` : '.'}`,
      };
      result.append(h('p.zwilling-apply__state', statusIcon(status === 'accepted' ? 'ok' : 'info', { size: 11 }), ` ${texts[status] ?? texts.computing}`));
      if ((run.scenario ?? []).some((item) => item.type === 'rush_order')) {
        result.append(h('p.zwilling-apply__hint', 'Der Eilauftrag ist damit angelegt. Dieser Lauf lässt sich deshalb nicht ein zweites Mal übernehmen.'));
      } else {
        result.append(h('p.zwilling-apply__hint', 'Für einen weiteren Vorschlag den Zwilling neu rechnen.'));
      }
    } else if (!session.applyError && store.proposals.open) {
      result.append(h('p.zwilling-apply__hint', `Offen ist schon Vorschlag ${store.proposals.open.id}. Es gibt immer nur einen offenen Vorschlag: der neue tritt an seine Stelle.`));
    }
  }

  const offProposal = store.on('proposal', () => {
    if (run) paintResult();
  });

  return {
    node: box.node,
    setRun(next) {
      run = next;
      box.node.hidden = !run;
      clear(content);
      if (!run) return;
      session.solver = session.solver ?? run.recommended ?? null;
      const b = run.variants?.B?.kpis;
      const c = run.variants?.C?.kpis;
      const noGain = b && c && c.total_tardiness >= b.total_tardiness && c.makespan >= b.makespan;
      // Native append() would print a null as text, so the optional parts are filtered out.
      content.append(...[
        whatHappens(),
        noGain ? h('p.zwilling-apply__blocked', statusIcon('warn'), ' Laut Zwilling ist der neue Plan nicht besser als der aktuelle. Übernehmen geht trotzdem, die Zahlen sprechen aber nicht dafür.') : null,
        solverRows(),
        h('div.zwilling-apply__aging', ageText, ageMeter, ageNote),
        applyButton,
        result,
        rerun,
      ].filter(Boolean));
      rerun.hidden = true;
      paintResult();
      this.update();
    },
    update() {
      if (!run) return;
      const now = store.clock.now();
      const age = Math.max(0, now - run.created_min);
      const horizon = Math.max(1, ...VARIANTS.map((v) => run.variants?.[v]?.sim_minutes ?? 0));
      setText(ageText, `Alter des Abzweigs: ${fmt.duration(age)} (Sim-Zeit), ${fmt.percent(Math.min(1, age / horizon))} des vorhergesagten Zeitraums sind vorbei.`);
      const revision = store.state?.plan?.revision;
      const planMoved = session.startRevision !== null && revision !== undefined && revision !== session.startRevision;
      const aged = age >= AGE_WARN_MIN || planMoved;
      updateMeter(ageMeter, age / horizon, aged ? 'setup' : null);
      let note;
      if (planMoved) note = `Der aktive Plan hat sich seit dem Abzweig geändert (Rev. ${session.startRevision} auf ${revision}). Diese Prognose gilt für den alten Plan.`;
      else if (age >= AGE_WARN_MIN) note = 'Die Anlage ist weit weitergelaufen. Die Prognose kennt nichts, was seither passiert ist: neu rechnen.';
      else if (age >= AGE_NOTE_MIN) note = 'Was seit dem Abzweig passiert ist, steckt nicht in diesen Zahlen.';
      else note = 'Der Stand ist frisch.';
      setText(ageNote, `${note} Übernehmen plant ohnehin vom dann aktuellen Zustand.`);
      ageNote.classList.toggle('is-aged', aged);
      if (run.applicable && !session.applyError) rerun.hidden = !aged;
    },
    destroy: offProposal,
  };
}

// ------------------------------------------------------------------------------------------- replay
function buildReplay(ctx, { onSourceChange }) {
  const { store, fmt } = ctx;
  let run = null;
  let source = 'live';
  let t = 0;
  let t0 = 0;
  let t1 = 1;
  let playing = false;
  let raf = 0;
  let lastWall = 0;
  let lastGanttNow = 0;
  let shownFrame = null;
  let liveTimer = 0;
  let truncated = false;

  // Fork bar over the hall
  const barTag = h('span.zwilling-bar__tag');
  const barVariant = h('span.zwilling-bar__variant');
  const barTime = h('span.zwilling-bar__time');
  const barNote = h('span.zwilling-bar__note');
  const bar = h('div.zwilling-bar', barTag, barVariant, h('span.zwilling-bar__spacer'), barNote, barTime);

  const hallHost = h('div.zwilling-replay__hall');
  const noFrames = h('p.zwilling-replay__noframes', { hidden: true }, 'Für diese Variante liegen keine Bilder vor: das MES behält sie nur für die letzten 5 Läufe. Der Plan darunter und die Zahlen oben gelten weiter.');
  const hall = createHall(hallHost, {
    provider: liveHallProvider(store),
    ariaLabel: 'Werkhalle, Wiedergabe von Schatten oder Zwilling',
  });

  // Transport
  const playIcon = s('path', { d: 'M4 2.5 13 8l-9 5.5Z', fill: 'currentColor' });
  const playButton = h('button.btn.btn--icon.zwilling-transport__play', { type: 'button', 'aria-label': 'Abspielen', 'data-tip': 'Abspielen oder anhalten', onclick: () => setPlaying(!playing) },
    s('svg', { viewBox: '0 0 16 16', width: 14, height: 14, 'aria-hidden': 'true' }, playIcon));
  const restartButton = h('button.btn.btn--icon', { type: 'button', 'aria-label': 'Zurück zum Abzweig', 'data-tip': 'Zurück zum Abzweig', onclick: () => seek(t0) },
    s('svg', { viewBox: '0 0 16 16', width: 14, height: 14, 'aria-hidden': 'true' }, s('path', { d: 'M3 2.5v11h2v-11ZM13.5 2.5 6 8l7.5 5.5Z', fill: 'currentColor' })));
  const sourceSeg = segmented([
    { value: 'live', label: 'Schatten', tip: 'Die laufende Anlage, live über OPC UA' },
    ...VARIANTS.map((key) => ({ value: key, label: VARIANT_SEG[key], tip: 'Beim Wechsel bleibt die Zeitposition gleich' })),
  ], source, (value) => setSource(value), 'Was die Halle zeigt', '.zwilling-transport__source');
  const scrubMarks = h('div.zwilling-scrub__marks', { 'aria-hidden': 'true' });
  const scrub = h('input.zwilling-scrub__input', {
    type: 'range',
    min: 0,
    max: 1,
    step: 1,
    value: 0,
    'aria-label': 'Zeitposition im Abzweig',
    oninput: (event) => {
      setPlaying(false);
      seek(Number(event.target.value));
    },
  });
  const timeOffset = h('span.zwilling-transport__offset');
  const speedSeg = segmented(SPEED_OPTIONS, session.speed, (value) => {
    session.speed = value;
    speedSeg.set(value);
  }, 'Wiedergabe in Sim-Minuten je Sekunde');
  const scrubBox = h('div.zwilling-scrub', scrubMarks, scrub);
  const transport = h('div.zwilling-transport',
    sourceSeg.node,
    playButton,
    restartButton,
    scrubBox,
    timeOffset,
    h('span.zwilling-transport__speed', speedSeg.node, h('span.faint', 'min/s')));

  // Status strip and comparison at the same moment
  const cells = new Map();
  const strip = h('div.zwilling-strip', { 'aria-label': 'Maschinen in der Wiedergabe' });
  for (const id of MACHINES) {
    const badge = statusBadge('IDLE');
    const lot = h('span.zwilling-strip__lot');
    const cell = h('div.zwilling-strip__cell', h('span.zwilling-strip__head', h('span.zwilling-strip__id', id), badge), lot);
    cells.set(id, { cell, badge, lot });
    strip.appendChild(cell);
  }
  const atRows = new Map();
  const atBody = h('tbody');
  for (const key of VARIANTS) {
    const packages = h('td.num');
    const finished = h('td.num');
    const late = h('td.num');
    const row = h('tr', h('th', { scope: 'row' }, h('span.zwilling-at__key', variantMark(key, 10), key)), packages, finished, late);
    atRows.set(key, { row, packages, finished, late });
    atBody.appendChild(row);
  }
  const atTable = h('table.zwilling-at', { 'aria-label': 'Stand aller Varianten zur selben Zeit' },
    h('thead', h('tr', h('th', { scope: 'col', 'data-tip': 'Stand aller Varianten zur gleichen Zeitposition' }, 'Stand'), h('th.num', { scope: 'col' }, 'Pakete'), h('th.num', { scope: 'col' }, 'Fertig'), h('th.num', { scope: 'col' }, 'Verspätet'))),
    atBody);
  const statusRow = h('div.zwilling-replay__status', strip, atTable);

  const ganttHost = h('div.zwilling-replay__gantt');
  const ganttNote = h('p.zwilling-replay__ganttnote');
  const gantt = renderGantt(ganttHost, { assignments: [], downtimes: [], orders: [], now: null, rows: MACHINES, compact: true, legend: true, tableToggle: true });

  // Until a run exists the hall stays folded to its bar, so the scenario and its result are what the view opens on.
  const node = h('section.zwilling-replay.zwilling-replay--idle', { 'aria-label': 'Wiedergabe in der Halle' }, bar, hallHost, noFrames, transport, statusRow, ganttHost, ganttNote);

  node.addEventListener('keydown', (event) => {
    if (event.target !== scrub) return;
    if (event.key === ' ') {
      event.preventDefault();
      setPlaying(!playing);
    } else if (event.key === 'ArrowLeft' || event.key === 'ArrowRight') {
      event.preventDefault();
      setPlaying(false);
      const step = event.shiftKey ? 60 : 15;
      seek(t + (event.key === 'ArrowLeft' ? -step : step));
    }
  });

  function hasFrames(key) {
    return Boolean(run?.variants?.[key]?.frames?.length);
  }

  function forkProvider() {
    if (!run || source === 'live') return null;
    const frame = frameAt(run.variants[source]?.frames ?? [], t);
    return frame ? { t, machines: frame.machines, material: frame.material } : null;
  }

  function variantLastFrame(key) {
    const frames = run?.variants?.[key]?.frames ?? [];
    return frames.length ? frames[frames.length - 1].t : null;
  }

  function paintMarks() {
    clear(scrubMarks);
    if (!run) return;
    const span = Math.max(1, t1 - t0);
    const pct = (value) => `${(((value - t0) / span) * 100).toFixed(2)}%`;
    for (const item of run.scenario ?? []) {
      if (item.type === 'rush_order') {
        const due = run.created_min + item.due_in_min;
        if (due <= t1) scrubMarks.appendChild(h('span.zwilling-scrub__due', { style: { left: pct(due) } }, 'Eil-Termin'));
        continue;
      }
      const start = run.created_min + (item.at_offset_min ?? 0);
      const end = Math.min(t1, start + item.duration_min);
      scrubMarks.appendChild(h(`span.zwilling-scrub__band.zwilling-scrub__band--${item.type}`, {
        style: { left: pct(start), width: pct(t0 + (end - start)) },
      }));
    }
    // Variants that end within a few pixels share one mark, so their letters do not run into each other.
    const ends = VARIANTS.map((key) => ({ key, end: variantLastFrame(key) })).filter((e) => e.end !== null).sort((a, b) => a.end - b.end);
    const near = span * 0.012;
    let group = null;
    const groups = [];
    for (const entry of ends) {
      if (group && entry.end - group.end <= near) {
        group.keys.push(entry.key);
      } else {
        group = { end: entry.end, keys: [entry.key] };
        groups.push(group);
      }
    }
    for (const item of groups) scrubMarks.appendChild(h('span.zwilling-scrub__end', { style: { left: pct(item.end) } }, item.keys.join(' ')));
  }

  function atMoment() {
    if (!run) return;
    for (const key of VARIANTS) {
      const variant = run.variants?.[key];
      const cell = atRows.get(key);
      if (!variant) {
        setText(cell.packages, fmt.DASH);
        setText(cell.finished, fmt.DASH);
        setText(cell.late, fmt.DASH);
        continue;
      }
      const frame = frameAt(variant.frames ?? [], t);
      setText(cell.packages, fmt.number(frame?.material?.buffers?.SHIP?.packages ?? null));
      const completion = variant.order_completion ?? {};
      const unfinished = new Set(variant.kpis?.unfinished_orders ?? []);
      let finished = 0;
      let late = 0;
      const ids = Object.keys(completion);
      for (const id of ids) {
        const done = !unfinished.has(id) && completion[id] <= t;
        if (done) finished += 1;
        const due = dueOf(id);
        if (due !== null && (done ? completion[id] > due : t > due)) late += 1;
      }
      setText(cell.finished, `${fmt.number(finished)}/${fmt.number(ids.length)}`);
      setText(cell.late, fmt.number(late));
      cell.row.classList.toggle('is-current', key === source);
    }
  }

  function paintStrip(machines) {
    const lookup = Array.isArray(machines) ? new Map(machines.map((m) => [m.id, m])) : new Map(Object.entries(machines ?? {}));
    for (const [id, cell] of cells) {
      const m = lookup.get(id);
      const status = m?.status ?? 'IDLE';
      updateStatus(cell.badge, status);
      cell.cell.className = `zwilling-strip__cell zwilling-strip__cell--${fmt.statusTone(status)}`;
      const lots = m?.op?.lot_ids ?? m?.lot_ids ?? [];
      let line = fmt.DASH;
      if (status === 'FAULT') line = 'Störung';
      else if (lots.length) line = lots.length > 1 ? `${fmt.lotId(lots[0])} +${lots.length - 1}` : fmt.lotId(lots[0]);
      setText(cell.lot, line);
    }
  }

  function paintTime() {
    if (source === 'live') {
      setText(barTime, fmt.simTime(store.clock.now()));
      return;
    }
    const value = Math.round(t);
    if (Number(scrub.value) !== value) scrub.value = String(value);
    const fill = ((t - t0) / Math.max(1, t1 - t0)) * 100;
    scrub.style.setProperty('--fill', `${fill.toFixed(2)}%`);
    const label = fmt.simTime(t);
    setText(barTime, label);
    setAttr(scrub, 'aria-valuetext', `${label}, ${fmt.duration(t - t0, { signed: true })} nach dem Abzweig`);
    const end = variantLastFrame(source);
    setText(timeOffset, end !== null && t >= end && end < t1
      ? `Variante ${source} fertig, ${fmt.simTime(end)}`
      : `${fmt.duration(t - t0, { signed: true })} nach Abzweig`);
    const now = performance.now();
    if (now - lastGanttNow >= GANTT_NOW_MS) {
      lastGanttNow = now;
      gantt.update({ now: t });
    }
    const frame = frameAt(run.variants[source]?.frames ?? [], t);
    if (frame !== shownFrame) {
      shownFrame = frame;
      if (frame) paintStrip(frame.machines);
      atMoment();
    }
  }

  function seek(value) {
    t = Math.max(t0, Math.min(t1, value));
    session.t = t;
    paintTime();
    gantt.update({ now: t });
  }

  function setPlaying(next) {
    const can = next && source !== 'live' && hasFrames(source);
    playing = can;
    setAttr(playIcon, 'd', playing ? 'M3.5 2.5h3.2v11H3.5ZM9.3 2.5h3.2v11H9.3Z' : 'M4 2.5 13 8l-9 5.5Z');
    playButton.setAttribute('aria-label', playing ? 'Anhalten' : 'Abspielen');
    cancelAnimationFrame(raf);
    if (playing) {
      const end = variantLastFrame(source);
      if (end !== null && t >= end - 0.01) seek(t0);
      lastWall = performance.now();
      raf = requestAnimationFrame(loop);
    }
  }

  function loop(wall) {
    if (!playing) return;
    raf = requestAnimationFrame(loop);
    const dt = Math.min(0.1, Math.max(0, (wall - lastWall) / 1000));
    lastWall = wall;
    t += dt * session.speed;
    const end = variantLastFrame(source) ?? t1;
    if (t >= end) {
      t = end;
      session.t = t;
      paintTime();
      gantt.update({ now: t });
      setPlaying(false);
      return;
    }
    session.t = t;
    paintTime();
  }

  function paintBar() {
    const fork = source !== 'live';
    node.classList.toggle('zwilling-replay--fork', fork);
    if (fork) {
      setText(barTag, `Zwilling · Abzweig von ${fmt.simTime(run.created_min)}`);
      const variant = run.variants[source];
      const solver = source === 'C' && variant?.solver ? ` · ${fmt.solverLabel(variant.solver)}` : '';
      setText(barVariant, `Variante ${source}: ${variant?.label ?? VARIANT_HEAD[source]}${solver}`);
      setText(barNote, 'Berührt die Anlage nicht. Keine Live-Werte.');
      hall.setBanner(null);
    } else {
      setText(barTag, 'Schatten live');
      setText(barVariant, 'Die laufende Anlage über OPC UA, nur lesend');
      setText(barNote, run ? 'Zurück zum Zwilling: A, B oder C wählen' : 'Die Halle erscheint mit der Wiedergabe eines Zwillingslaufs');
      paintLink();
    }
  }

  function paintLink() {
    if (source !== 'live') return;
    const link = store.link();
    if (link.level === 'down') hall.setBanner(`${link.label}: Anzeige eingefroren, Werte sind nicht aktuell.`, 'fault');
    else if (link.level === 'stale') hall.setBanner('Daten veraltet: der Heartbeat der Anlage bleibt aus.', 'warn');
    else hall.setBanner(null);
  }

  function liveGantt() {
    const plan = store.plan;
    if (!plan) return;
    gantt.update({
      assignments: plan.assignments ?? [],
      downtimes: plan.downtimes ?? [],
      orders: plan.orders ?? [],
      now: store.clock.now(),
      domain: null,
      orderCompletion: plan.metrics?.order_completion ?? null,
    });
    setText(ganttNote, `Aktiver Plan, Revision ${plan.revision}`);
  }

  function variantGantt() {
    const variant = run.variants[source];
    const end = Math.max(t1, ...VARIANTS.map((key) => variantEnd(run, key) ?? t1));
    gantt.update({
      assignments: (variant?.assignments ?? []).filter((a) => a.end >= t0),
      downtimes: variantDowntimes(run, source),
      orders: Object.keys(variant?.order_completion ?? {}).map((id) => ({ id, due: dueOf(id), weight: session.orderInfo.get(id)?.weight ?? 1 })).filter((o) => o.due !== null),
      now: t,
      domain: [t0, end],
      orderCompletion: variant?.order_completion ?? null,
    });
    setText(ganttNote, `Plan der Variante ${source}, gleiche Zeitachse für alle Varianten. Der Strich folgt der Wiedergabe.${truncated ? ' Die Bilder enden vor dem Ende der Variante: das MES zeichnet höchstens 600 je Variante auf.' : ''}`);
  }

  function setSource(next) {
    if (next !== 'live' && !run?.variants?.[next]) return;
    const wasPlaying = playing;
    if (next === 'live') setPlaying(false);
    source = next;
    session.source = next;
    sourceSeg.set(next);
    shownFrame = null;
    if (next === 'live') {
      hall.setProvider(liveHallProvider(store));
      liveGantt();
      statusRow.classList.add('zwilling-replay__status--live');
      if (store.state) paintStrip(store.state.machines);
    } else {
      hall.setProvider(forkProvider);
      variantGantt();
      statusRow.classList.remove('zwilling-replay__status--live');
      paintTime();
      atMoment();
      if (wasPlaying) setPlaying(true);
    }
    const pictures = next === 'live' || hasFrames(next);
    hallHost.hidden = !pictures;
    noFrames.hidden = pictures;
    statusRow.hidden = !pictures;
    for (const button of [playButton, restartButton, scrub]) button.disabled = next === 'live' || !pictures;
    if (next === 'live') setText(timeOffset, '');
    paintBar();
    onSourceChange(next);
  }

  liveTimer = setInterval(() => {
    if (source === 'live') {
      setText(barTime, fmt.simTime(store.clock.now()));
      gantt.update({ now: store.clock.now() });
    }
  }, 500);

  const offPlan = store.on('plan', () => {
    if (source === 'live') liveGantt();
  });
  const offConn = store.on('conn', paintLink);

  return {
    node,
    get source() { return source; },
    setRun(next) {
      setPlaying(false);
      run = next;
      node.classList.toggle('zwilling-replay--idle', !run);
      sourceSeg.enable((key) => key === 'live' || Boolean(run?.variants?.[key]));
      if (!run) {
        setSource('live');
        // Folded away, so it has nothing to draw until a run brings it back.
        hall.setProvider(() => null);
        speedSeg.enable(() => false);
        return;
      }
      speedSeg.enable(() => true);
      const lasts = VARIANTS.map(variantLastFrame).filter((v) => v !== null);
      t0 = run.created_min;
      if (lasts.length) {
        const firsts = VARIANTS.map((key) => run.variants?.[key]?.frames?.[0]?.t).filter((v) => v !== undefined);
        t0 = Math.min(...firsts);
      }
      t1 = lasts.length ? Math.max(...lasts) : t0 + 1;
      scrub.min = String(Math.floor(t0));
      scrub.max = String(Math.ceil(t1));
      t = Math.max(t0, Math.min(t1, session.t ?? t0));
      session.t = t;
      paintMarks();
      truncated = VARIANTS.some((key) => {
        const end = variantEnd(run, key);
        const last = variantLastFrame(key);
        return end !== null && last !== null && end - last > 10;
      });
      setSource(session.source === 'live' || run.variants?.[session.source] ? session.source : (run.variants?.C ? 'C' : 'B'));
    },
    update(state) {
      if (source === 'live') paintStrip(state.machines);
    },
    seekAndPlay() {
      if (source === 'live') setSource(run?.variants?.C ? 'C' : 'B');
      setPlaying(true);
    },
    scrollIntoView() {
      const smooth = !window.matchMedia?.('(prefers-reduced-motion: reduce)').matches;
      node.scrollIntoView({ behavior: smooth ? 'smooth' : 'auto', block: 'start' });
    },
    destroy() {
      setPlaying(false);
      clearInterval(liveTimer);
      offPlan();
      offConn();
      gantt.destroy();
      hall.destroy();
    },
  };
}

// ------------------------------------------------------------------------------------------- the view
export function mount(el, ctx) {
  loadCss('css/zwilling.css');
  const { store, api, router } = ctx;
  el.classList.add('zwilling');

  let alive = true;
  let pollTimer = 0;
  let ticker = 0;
  let progress = null;

  const framing = buildFraming(ctx);
  const builder = buildBuilder(ctx, { onStart: () => start() });
  const results = h('div.zwilling-results');
  const apply = buildApply(ctx, {
    onRerun: (run) => {
      session.scenario = JSON.parse(JSON.stringify(run.scenario ?? []));
      start();
    },
  });
  const replay = buildReplay(ctx, {
    onSourceChange: (source) => {
      framing.setSelected(source === 'live' ? null : source);
      syncMode();
    },
  });

  el.append(
    framing.node,
    h('div.zwilling-work', h('div.zwilling-left', builder.node, apply.node), results),
    replay.node);

  function syncMode() {
    store.setUiMode(session.run && replay.source !== 'live' ? 'zwilling' : 'schatten');
  }

  function stopTicker() {
    clearInterval(ticker);
    ticker = 0;
  }

  function paintResults() {
    clear(results);
    progress = null;
    stopTicker();
    if (session.waiting) {
      progress = buildProgress(ctx, {
        onAbandon: () => {
          // The server keeps computing; only this page stops waiting for it.
          clearTimeout(pollTimer);
          forgetRun();
          refresh();
        },
      });
      results.appendChild(progress.node);
      progress.paint();
      ticker = setInterval(() => progress?.paint(), 200);
    } else if (session.run) {
      results.append(
        buildImpact(session.run, ctx, {
          onWatch: () => {
            replay.seekAndPlay();
            replay.scrollIntoView();
          },
        }),
        buildOrders(session.run, ctx),
        buildMonteCarlo(session.run, ctx));
    } else if (session.failed) {
      results.appendChild(buildFailure(session.failed, () => start()));
    } else {
      results.appendChild(buildIntro());
    }
  }

  function refresh() {
    builder.paint();
    paintResults();
    framing.setRun(session.run);
    apply.setRun(session.run);
    replay.setRun(session.run);
    framing.update();
    syncMode();
  }

  async function loadOrderInfo(run) {
    const info = new Map();
    for (const order of store.plan?.orders ?? []) info.set(order.id, { due: order.due, weight: order.weight });
    try {
      const data = await api.get('/api/orders', { quiet: true });
      for (const order of data.orders ?? []) info.set(order.id, { due: order.due_min, weight: order.weight, product_id: order.product_id });
    } catch {
      /* the plan's list still gives most due dates, the rest show a dash */
    }
    session.orderInfo = info;
    const rushDue = new Map();
    for (const row of orderRows(run, info)) if (row.rush && row.due !== null) rushDue.set(row.id, row.due);
    session.rushDue = rushDue;
  }

  async function finish(data) {
    if (data.status === 'failed') {
      session.waiting = false;
      session.failed = data.note || 'Der Zwillingslauf ist fehlgeschlagen.';
      refresh();
      return;
    }
    prepareRun(data);
    await loadOrderInfo(data);
    if (!alive && session.runId !== data.id) return;
    session.run = data;
    session.pending = null;
    session.waiting = false;
    session.t = data.created_min;
    session.source = data.variants?.C ? 'C' : 'B';
    if (alive) refresh();
  }

  async function poll() {
    const id = session.runId;
    if (!alive || !session.waiting || !id) return;
    try {
      const data = await api.get(`/api/twin/runs/${encodeURIComponent(id)}`, { quiet: true });
      if (!alive || session.runId !== id || !session.waiting) return;
      session.pollProblem = null;
      if (data.status === 'computing') {
        session.pending = data;
        progress?.paint();
        pollTimer = setTimeout(poll, POLL_MS);
        return;
      }
      await finish(data);
    } catch (error) {
      if (!alive || session.runId !== id || !session.waiting) return;
      if (error.status === 404) {
        session.waiting = false;
        session.failed = 'Das MES kennt diesen Lauf nicht mehr. Vermutlich wurde es neu gestartet.';
        refresh();
        return;
      }
      session.pollProblem = `Das MES antwortet gerade nicht (${error.message}). Es wird weiter gefragt.`;
      progress?.paint();
      pollTimer = setTimeout(poll, POLL_MS * 3);
    }
  }

  async function start() {
    const previous = { run: session.run, source: session.source, t: session.t, applied: session.applied, solver: session.solver };
    forgetRun();
    session.waiting = true;
    session.startedAt = performance.now();
    session.startRevision = store.state?.plan?.revision ?? null;
    session.requested = { monte: session.monte, limit: session.limit };
    refresh();
    try {
      const response = await api.post('/api/twin/run', {
        scenario: session.scenario.map((item) => ({ ...item })),
        monte_carlo: session.monte,
        seed: 1,
        time_limit_s: session.limit,
      }, { quiet: true });
      session.runId = response.id;
      session.kick?.();
    } catch (error) {
      session.waiting = false;
      if (previous.run) {
        Object.assign(session, previous);
        ctx.toast(`Zwilling nicht gestartet: ${error.message}`, { kind: 'error' });
      } else {
        session.failed = error.message;
      }
      session.kick?.();
      if (alive) refresh();
    }
  }

  session.kick = () => {
    if (alive) poll();
  };

  const offState = store.subscribe((state) => {
    if (session.plantRun && session.plantRun !== state.run_id) {
      const hadWork = Boolean(session.run || session.waiting);
      clearTimeout(pollTimer);
      forgetRun();
      session.failed = hadWork ? 'Die Anlage wurde neu gestartet. Der Zwillingslauf gehörte zum alten Lauf.' : null;
      session.plantRun = state.run_id;
      refresh();
    }
    session.plantRun = state.run_id;
    framing.update();
    apply.update();
    builder.paintAlreadyDown();
    replay.update(state);
  });

  refresh();
  const linked = router.params?.run;
  if (linked && linked !== session.run?.id && !session.waiting) {
    session.waiting = true;
    session.startedAt = performance.now();
    session.runId = linked;
    session.requested = { monte: 0, limit: session.limit };
    refresh();
  }
  if (session.waiting) poll();

  return () => {
    alive = false;
    session.kick = null;
    clearTimeout(pollTimer);
    stopTicker();
    offState();
    framing.destroy();
    builder.destroy();
    apply.destroy();
    replay.destroy();
  };
}
