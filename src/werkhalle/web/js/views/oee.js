// OEE & Energie: plant headline, OEE as A × P × Q per machine, where the time went, OEE trend per machine, power and energy.
// Grey is normal data. Colour appears only where a bar means a machine state (Rüsten, Störung).
import { h, s, setText, clear, loadCss } from '../core/dom.js';
import { panel, statusIcon, paintIcon } from '../components/ui.js';
import { poll } from './_poll.js';

const WINDOWS = [
  { id: 'start', label: 'Seit Start' },
  { id: '8h', label: 'Letzte 8 h' },
];

const WINDOW_WORDS = { start: 'seit Start', '8h': 'letzte 8 h' };
const WINDOW_MINUTES = { '8h': 480 };

/** Keeps the samples inside the window. `series` maps a name to values parallel to `t`. */
export function clipToWindow(t, series, windowId, now) {
  const span = WINDOW_MINUTES[windowId];
  if (!span || !t?.length || typeof now !== 'number') return { t: t ?? [], series: series ?? {} };
  const from = now - span;
  const first = t.findIndex((value) => value >= from);
  if (first <= 0) return first === 0 ? { t, series } : { t: [], series: Object.fromEntries(Object.keys(series ?? {}).map((key) => [key, []])) };
  return { t: t.slice(first), series: Object.fromEntries(Object.entries(series ?? {}).map(([key, values]) => [key, values.slice(first)])) };
}

const SEGMENTS = [
  { key: 'run', label: 'Lauf', head: 'Lauf' },
  { key: 'setup', label: 'Rüsten', head: 'Rüsten' },
  { key: 'fault', label: 'Störung', head: 'Störung' },
  { key: 'stop', label: 'Materialmangel/Stillstand', head: 'Mangel/Stillstand' },
  { key: 'none', label: 'Kein Bedarf', head: 'Kein Bedarf' },
];

const UNIT_ONE = { Stangen: 'Stange', 'Körbe': 'Korb', Pakete: 'Paket', Bolzen: 'Bolzen' };

function isNumber(value) {
  return typeof value === 'number' && !Number.isNaN(value);
}

function whyMissing(kind, row) {
  const minutes = row?.minutes ?? {};
  const counts = row?.counts ?? {};
  if (kind === 'availability') return (minutes.planned_net ?? 0) <= 0 ? 'keine Planzeit' : 'nicht definiert';
  if (kind === 'availability_iso') return (minutes.planned ?? 0) <= 0 ? 'keine Planzeit' : 'nicht definiert';
  if (kind === 'performance') return (minutes.run ?? 0) <= 0 ? 'keine Laufzeit' : 'nicht definiert';
  if (kind === 'quality') return (counts.total ?? 0) <= 0 ? 'keine Menge' : 'nicht definiert';
  return 'nicht definiert';
}

function oeeReason(row, availabilityKey) {
  for (const kind of [availabilityKey, 'performance', 'quality']) {
    if (!isNumber(row[kind])) return whyMissing(kind, row);
  }
  return 'nicht definiert';
}

/** Round axis step for a range: 1, 2 or 5 times a power of ten. */
function niceStep(span, count) {
  const raw = Math.max(span, 1e-9) / count;
  const power = 10 ** Math.floor(Math.log10(raw));
  const unit = raw / power;
  return (unit <= 1 ? 1 : unit <= 2 ? 2 : unit <= 5 ? 5 : 10) * power;
}

/** Ticks on whole clock times of the sim day, because t=0 is 06:00 and the labels show the clock. */
function timeTicks(t0, t1, count) {
  const steps = [5, 10, 15, 30, 60, 120, 180, 240, 360, 480, 720, 1440, 2880];
  const step = steps.find((candidate) => (t1 - t0) / candidate <= count) ?? steps[steps.length - 1];
  const first = Math.ceil((t0 + 360) / step) * step - 360;
  const ticks = [];
  for (let t = first; t <= t1 + 1e-6; t += step) ticks.push(t);
  return ticks;
}

function linePath(points) {
  let d = '';
  let pen = false;
  for (const point of points) {
    if (!point) {
      pen = false;
      continue;
    }
    d += `${pen ? 'L' : 'M'}${point[0].toFixed(1)} ${point[1].toFixed(1)}`;
    pen = true;
  }
  return d;
}

function nearestIndex(times, t) {
  let best = 0;
  let distance = Infinity;
  times.forEach((value, i) => {
    const gap = Math.abs(value - t);
    if (gap < distance) {
      distance = gap;
      best = i;
    }
  });
  return best;
}

// --- Plant headline ---
function buildHeadline(fmt) {
  const cell = (label, sub, { lead = false, tip } = {}) => {
    const value = h('span.oee-kpi__value');
    const unit = h('span.oee-kpi__unit');
    const subNode = h('span.oee-kpi__sub', sub);
    const node = h(`div.oee-kpi${lead ? '.oee-kpi--lead' : ''}`, tip ? { 'data-tip': tip } : {}, h('span.oee-kpi__label', label), h('span.oee-kpi__main', value, unit), subNode);
    return {
      node,
      set(main, unitText, subText) {
        setText(value, main);
        setText(unit, unitText ?? '');
        if (subText !== undefined) setText(subNode, subText);
      },
    };
  };
  const cells = {
    net: cell('OEE Anlage, netto', 'ohne Wartezeit auf Material', { lead: true, tip: 'Mittel der Maschinen: Verfügbarkeit ohne Materialmangel × Leistung × Qualität' }),
    iso: cell('OEE Anlage, ISO', 'Materialmangel als Verlust', { tip: 'ISO 22400: Materialmangel senkt die Verfügbarkeit' }),
    a: cell('Verfügbarkeit A', 'Lauf ÷ Planzeit, netto'),
    p: cell('Leistung P', 'Ideal- ÷ Laufzeit'),
    q: cell('Qualität Q', 'Gut ÷ Gesamt'),
    power: cell('Leistung jetzt', 'live über OPC UA'),
    energy: cell('Energie', 'seit Laufbeginn'),
    specific: cell('Spezifisch', 'je versandtem kg'),
  };
  const node = h('section.oee-kpis', { 'aria-label': 'Kennzahlen der Anlage' }, Object.values(cells).map((c) => c.node));
  const percent = (value) => (isNumber(value) ? fmt.number(value * 100) : fmt.DASH);
  const sign = (value) => (isNumber(value) ? '%' : '');
  return {
    node,
    plant(plant) {
      cells.net.set(percent(plant?.oee), sign(plant?.oee));
      cells.iso.set(percent(plant?.oee_iso), sign(plant?.oee_iso));
      cells.a.set(percent(plant?.availability), sign(plant?.availability));
      cells.p.set(percent(plant?.performance), sign(plant?.performance));
      cells.q.set(percent(plant?.quality), sign(plant?.quality));
    },
    live(kpi) {
      cells.power.set(fmt.number(kpi?.power_kw), 'kW');
      cells.energy.set(fmt.number(kpi?.energy_kwh), 'kWh');
      cells.specific.set(fmt.number(kpi?.kwh_per_kg, 2), isNumber(kpi?.kwh_per_kg) ? 'kWh/kg' : '', isNumber(kpi?.kwh_per_kg) ? 'je versandtem kg' : 'noch nichts versandt');
    },
  };
}

// --- OEE per machine: A × P × Q ---
function factorCell() {
  const fill = h('span.oee-bar__fill');
  const tick = h('span.oee-bar__tick', { hidden: true });
  const bar = h('span.oee-bar', fill, tick);
  const value = h('span.oee-factor__value');
  const why = h('span.oee-factor__why', { hidden: true });
  const node = h('td.oee-factor', h('span.oee-factor__inner', bar, value, why));
  return {
    node,
    set(fraction, reason, { mark, tip, fmt }) {
      const known = isNumber(fraction);
      bar.hidden = !known;
      why.hidden = known;
      setText(value, known ? fmt.percent(fraction) : fmt.DASH);
      setText(why, known ? '' : reason);
      if (known) fill.style.width = `${(Math.max(0, Math.min(1, fraction)) * 100).toFixed(1)}%`;
      tick.hidden = !isNumber(mark);
      if (isNumber(mark)) tick.style.left = `${(Math.max(0, Math.min(1, mark)) * 100).toFixed(1)}%`;
      node.setAttribute('data-tip', known ? tip ?? '' : `Nicht definiert: ${reason}`);
    },
  };
}

function buildOeeTable(ctx) {
  const { fmt, store } = ctx;
  const box = panel('OEE je Maschine: OEE = A × P × Q', { dense: true });
  box.node.classList.add('oee-machines');
  const tbody = h('tbody');
  box.body.appendChild(h('table.tbl.oee-table',
    h('thead', h('tr',
      h('th', 'Maschine'),
      h('th.num', { 'data-tip': 'Netto: Zeit, in der eine Maschine auf Material wartet, zählt nicht gegen sie' }, 'OEE netto'),
      h('th.num', { 'data-tip': 'ISO 22400: Materialmangel zählt als Verfügbarkeitsverlust' }, 'OEE ISO'),
      h('th.oee-op', '='),
      h('th', { 'data-tip': 'Lauf ÷ geplante Zeit, netto. Der Strich im Balken zeigt den ISO-Wert.' }, 'Verfügbarkeit A'),
      h('th.oee-op', '×'),
      h('th', 'Leistung P'),
      h('th.oee-op', '×'),
      h('th', 'Qualität Q'))),
    tbody));

  const rows = new Map();
  function row(id) {
    const a = factorCell();
    const p = factorCell();
    const q = factorCell();
    const icon = statusIcon('IDLE');
    const name = h('span.oee-name__text');
    const oee = h('span.oee-big');
    const oeeCell = h('td.num', oee);
    const iso = h('td.num.muted');
    const tr = h('tr', { dataset: { id } },
      h('td.oee-name', icon, h('span.oee-name__id', id === 'plant' ? 'Anlage' : id), name),
      oeeCell,
      iso,
      h('td.oee-op', '='),
      a.node,
      h('td.oee-op', '×'),
      p.node,
      h('td.oee-op', '×'),
      q.node);
    if (id === 'plant') {
      tr.classList.add('oee-plant-row');
      icon.style.visibility = 'hidden';
    }
    tbody.appendChild(tr);
    const entry = { tr, a, p, q, icon, name, oee, oeeCell, iso };
    rows.set(id, entry);
    return entry;
  }

  function paint(id, data, nameText) {
    const entry = rows.get(id) ?? row(id);
    setText(entry.name, nameText);
    setText(entry.oee, fmt.percent(data.oee));
    entry.oeeCell.setAttribute('data-tip', isNumber(data.oee) ? 'Verfügbarkeit netto × Leistung × Qualität' : `Nicht definiert: ${oeeReason(data, 'availability')}`);
    setText(entry.iso, fmt.percent(data.oee_iso));
    entry.iso.setAttribute('data-tip', isNumber(data.oee_iso) ? 'ISO 22400: Materialmangel zählt als Verfügbarkeitsverlust' : `Nicht definiert: ${oeeReason(data, 'availability_iso')}`);
    entry.a.set(data.availability, whyMissing('availability', data), {
      fmt,
      mark: data.availability_iso,
      tip: `Netto ${fmt.percent(data.availability)}${isNumber(data.availability_iso) ? `, ISO ${fmt.percent(data.availability_iso)} (Strich)` : ''}`,
    });
    entry.p.set(data.performance, whyMissing('performance', data), { fmt, tip: `Leistung ${fmt.percent(data.performance, 1)}` });
    entry.q.set(data.quality, whyMissing('quality', data), {
      fmt,
      tip: data.counts ? `${fmt.number(data.counts.good)} gut von ${fmt.number(data.counts.total)} ${data.counts.unit ?? ''}` : `Qualität ${fmt.percent(data.quality, 1)}`,
    });
  }

  return {
    node: box.node,
    update(data) {
      // The plant row has no minutes of its own: a null there means no machine has a value.
      paint('plant', { ...data.plant, minutes: { planned: 0, planned_net: 0, run: 0 }, counts: { total: 0 } }, 'Mittel der Maschinen');
      for (const machine of data.machines ?? []) paint(machine.id, machine, machine.name ?? fmt.machineName(machine.id));
    },
    live() {
      for (const [id, entry] of rows) {
        if (id === 'plant') continue;
        const status = store.machine(id)?.status;
        if (status) {
          paintIcon(entry.icon, status);
          entry.icon.setAttribute('aria-label', fmt.statusLabel(status));
        }
      }
    },
  };
}

// --- Where the time went ---
function minutesOf(row) {
  const m = row.minutes ?? {};
  const idle = m.idle ?? 0;
  const none = Math.min(m.no_demand ?? 0, idle);
  return { run: m.run ?? 0, setup: m.setup ?? 0, fault: m.fault ?? 0, stop: Math.max(0, idle - none), none };
}

// The count only knows faults that began in the window. A fault reaching in from before it still cost these minutes, so 0 would be wrong.
export function carriedFault(row) {
  return (row.fault_count ?? 0) === 0 && (row.minutes?.fault ?? 0) > 0;
}

function buildLossTable(ctx) {
  const { fmt } = ctx;
  const legend = h('div.oee-legend', SEGMENTS.map((seg) => h('span.oee-legend__item', h(`span.oee-seg.oee-seg--${seg.key}.oee-legend__swatch`), seg.label)));
  const box = panel('Zeit je Maschine, Minuten', { dense: true, actions: [legend] });
  const title = box.head.querySelector('.panel__title');
  box.node.classList.add('oee-losses');
  const tbody = h('tbody');
  box.body.appendChild(h('table.tbl.oee-table',
    h('thead', h('tr',
      h('th', 'Maschine'),
      h('th.oee-stack-head', 'Verteilung'),
      SEGMENTS.map((seg) => h('th.num', { 'data-tip': seg.label }, seg.head)),
      h('th.num', { 'data-tip': 'Störungen im Fenster, mit MTBF und MTTR im Hinweis der Zeile. „≥ 1“: die Störung begann vor dem Fenster und reicht hinein.' }, 'Stör.'))),
    tbody));

  const rows = new Map();
  function row(id) {
    const segs = new Map(SEGMENTS.map((seg) => [seg.key, h(`span.oee-seg.oee-seg--${seg.key}`)]));
    const cells = new Map(SEGMENTS.map((seg) => [seg.key, h('td.num')]));
    const faults = h('td.num');
    const idCell = h('td.oee-name', h('span.oee-name__id', id));
    const tr = h('tr', idCell, h('td.oee-stack-cell', h('span.oee-stack', [...segs.values()])), [...cells.values()], faults);
    tbody.appendChild(tr);
    const entry = { tr, segs, cells, faults };
    rows.set(id, entry);
    return entry;
  }

  function plantRow(machines) {
    const sum = { minutes: { run: 0, setup: 0, fault: 0, idle: 0, no_demand: 0, starved: 0 }, fault_count: 0, carried: false, id: 'Anlage', mtbf_min: null, mttr_min: null };
    for (const machine of machines) {
      for (const key of Object.keys(sum.minutes)) sum.minutes[key] += machine.minutes?.[key] ?? 0;
      if (carriedFault(machine)) {
        sum.fault_count += 1;
        sum.carried = true;
      } else {
        sum.fault_count += machine.fault_count ?? 0;
      }
    }
    return sum;
  }

  return {
    node: box.node,
    update(data, windowId) {
      setText(title, `Zeit je Maschine, Minuten, ${WINDOW_WORDS[windowId]}`);
      const machines = data.machines ?? [];
      for (const machine of [plantRow(machines), ...machines]) {
        const isPlant = machine.id === 'Anlage';
        let entry = rows.get(machine.id);
        if (!entry) {
          entry = row(machine.id);
          if (isPlant) entry.tr.classList.add('oee-plant-row');
        }
        const minutes = minutesOf(machine);
        const total = Object.values(minutes).reduce((a, b) => a + b, 0);
        for (const seg of SEGMENTS) {
          const value = minutes[seg.key];
          const node = entry.segs.get(seg.key);
          node.hidden = value <= 0;
          node.style.flexGrow = String(value);
          const share = total > 0 ? ` (${fmt.percent(value / total)})` : '';
          const starved = seg.key === 'stop' && isNumber(machine.minutes?.starved) ? `, davon Materialmangel ${fmt.number(machine.minutes.starved)} min` : '';
          node.setAttribute('data-tip', `${machine.id} · ${seg.label}: ${fmt.number(value)} min${share}${starved}`);
          setText(entry.cells.get(seg.key), fmt.number(value));
        }
        const carried = isPlant ? machine.carried : carriedFault(machine);
        setText(entry.faults, carried ? `≥ ${fmt.number(isPlant ? machine.fault_count : 1)}` : fmt.number(machine.fault_count));
        const noneYet = carried ? 'keine Störung im Fenster begonnen' : 'keine Störung';
        const stats = isPlant ? ['Summe der Maschinen'] : [
          ...(carried ? ['Die Störung begann vor dem Fenster und reicht hinein'] : []),
          `MTBF ${isNumber(machine.mtbf_min) ? fmt.duration(machine.mtbf_min) : `${fmt.DASH} (${noneYet})`}`,
          `MTTR ${isNumber(machine.mttr_min) ? fmt.duration(machine.mttr_min) : `${fmt.DASH} (${noneYet})`}`,
        ];
        entry.faults.setAttribute('data-tip', stats.join(' · '));
      }
    },
  };
}

// --- OEE trend: one small plot per machine, same scale, one shared time cursor ---
function buildMultiples(ctx) {
  const { fmt, store } = ctx;
  const stamp = h('span.oee-multiples__stamp.mono');
  const box = panel('OEE-Verlauf je Maschine, netto, Skala 0 bis 100 %', { actions: [stamp] });
  const title = box.head.querySelector('.panel__title');
  box.node.classList.add('oee-multiples');
  const grid = h('div.oee-multiples__grid');
  box.body.appendChild(grid);

  const cells = new Map();
  let data = null;
  let hover = null;

  function cell(id) {
    const value = h('span.oee-mini__value');
    const svg = s('svg.oee-mini__plot', { role: 'img', tabindex: 0, 'aria-label': `OEE-Verlauf ${id}. Pfeiltasten bewegen den Zeitpunkt.` });
    const node = h('div.oee-mini', h('div.oee-mini__head', h('span.oee-name__id', id), value), svg);
    grid.appendChild(node);
    const entry = { node, svg, value, cursor: null };
    cells.set(id, entry);
    svg.addEventListener('pointermove', (event) => {
      if (!data?.t?.length) return;
      const box2 = svg.getBoundingClientRect();
      const x = Math.max(0, Math.min(1, (event.clientX - box2.left) / box2.width));
      const t = data.t[0] + x * (data.t[data.t.length - 1] - data.t[0]);
      setHover(nearestIndex(data.t, t));
    });
    svg.addEventListener('pointerleave', () => setHover(null));
    svg.addEventListener('blur', () => setHover(null));
    svg.addEventListener('keydown', (event) => {
      if (!data?.t?.length) return;
      if (event.key !== 'ArrowLeft' && event.key !== 'ArrowRight') return;
      event.preventDefault();
      const start = hover ?? data.t.length - 1;
      setHover(Math.max(0, Math.min(data.t.length - 1, start + (event.key === 'ArrowLeft' ? -1 : 1))));
    });
    return entry;
  }

  function setHover(index) {
    hover = index;
    paintValues();
  }

  function paintValues() {
    if (!data) return;
    const index = hover ?? data.t.length - 1;
    setText(stamp, data.t.length ? `${hover === null ? 'zuletzt' : 'Zeitpunkt'} ${fmt.simTime(data.t[index])}` : '');
    for (const [id, entry] of cells) {
      const series = data.oee?.[id] ?? [];
      const current = series[index];
      setText(entry.value, fmt.percent(current));
      entry.value.setAttribute('data-tip', isNumber(current) ? '' : 'Nicht definiert: zu diesem Zeitpunkt noch keine Laufzeit oder Menge');
      if (entry.cursor) {
        const x = entry.xOf(data.t[index]);
        entry.cursor.setAttribute('x1', x);
        entry.cursor.setAttribute('x2', x);
        entry.cursor.setAttribute('visibility', hover === null ? 'hidden' : 'visible');
        const y = isNumber(current) ? entry.yOf(current) : null;
        entry.dot.setAttribute('cx', x);
        entry.dot.setAttribute('cy', y ?? 0);
        entry.dot.setAttribute('visibility', y === null ? 'hidden' : 'visible');
      }
    }
  }

  function draw() {
    if (!data) return;
    const times = data.t ?? [];
    for (const [id, entry] of cells) {
      const { svg } = entry;
      const width = svg.clientWidth || 180;
      const height = svg.clientHeight || 62;
      svg.setAttribute('viewBox', `0 0 ${width} ${height}`);
      clear(svg);
      const pad = 3;
      const xOf = (t) => (times.length > 1 ? pad + ((t - times[0]) / (times[times.length - 1] - times[0] || 1)) * (width - 2 * pad - 4) : width / 2);
      const yOf = (v) => pad + (1 - v) * (height - 2 * pad);
      entry.xOf = xOf;
      entry.yOf = yOf;
      for (const level of [0, 0.5, 1]) {
        svg.appendChild(s('line.oee-grid', { x1: 0, x2: width, y1: yOf(level), y2: yOf(level) }));
      }
      const series = data.oee?.[id] ?? [];
      const points = series.map((v, i) => (isNumber(v) ? [xOf(times[i]), yOf(Math.max(0, Math.min(1, v)))] : null));
      svg.appendChild(s('path.oee-line', { d: linePath(points) }));
      entry.cursor = s('line.oee-cursor', { y1: 0, y2: height, visibility: 'hidden' });
      entry.dot = s('circle.oee-dot', { r: 3.5, visibility: 'hidden' });
      svg.append(entry.cursor, entry.dot);
      const lastIndex = series.length - 1;
      if (isNumber(series[lastIndex]) && hover === null) {
        entry.dot.setAttribute('cx', xOf(times[lastIndex]));
        entry.dot.setAttribute('cy', yOf(series[lastIndex]));
        entry.dot.setAttribute('visibility', 'visible');
      }
    }
    paintValues();
  }

  const resize = new ResizeObserver(draw);
  resize.observe(grid);

  return {
    node: box.node,
    update(oee, windowId) {
      setText(title, `OEE-Verlauf je Maschine, netto, ${WINDOW_WORDS[windowId]}, Skala 0 bis 100 %`);
      const clipped = clipToWindow(oee.trend?.t, oee.trend?.oee, windowId, store.clock.now());
      data = { t: clipped.t, oee: clipped.series };
      if (hover !== null && hover >= (data.t?.length ?? 0)) hover = null;
      for (const machine of oee.machines ?? []) {
        if (!cells.has(machine.id)) cell(machine.id);
      }
      draw();
    },
    destroy() {
      resize.disconnect();
    },
  };
}

// --- Power trend ---
function buildPower(ctx) {
  const { fmt, store } = ctx;
  const now = h('span.oee-power__now');
  const box = panel('Gesamtleistung, Verlauf', { actions: [h('span.oee-power__label', 'jetzt'), now] });
  const title = box.head.querySelector('.panel__title');
  box.node.classList.add('oee-power');
  const svg = s('svg.oee-power__plot', { role: 'img', tabindex: 0, 'aria-label': 'Gesamtleistung über der Zeit. Pfeiltasten bewegen den Zeitpunkt.' });
  const tip = h('div.oee-tip', { hidden: true });
  const host = h('div.oee-power__host', svg, tip);
  box.body.appendChild(host);

  let data = null;
  let hover = null;
  let scale = null;

  function draw() {
    clear(svg);
    scale = null;
    const times = data?.t ?? [];
    const total = data?.kw?.total ?? [];
    const width = host.clientWidth || 400;
    const height = host.clientHeight || 170;
    svg.setAttribute('viewBox', `0 0 ${width} ${height}`);
    if (times.length < 2) {
      svg.appendChild(s('text.oee-axis', { x: 8, y: 20 }, 'Noch zu wenige Messpunkte.'));
      return;
    }
    const margin = { left: 44, right: 50, top: 8, bottom: 20 };
    const plotW = width - margin.left - margin.right;
    const plotH = height - margin.top - margin.bottom;
    const peak = Math.max(1, ...total.filter(isNumber));
    const step = niceStep(peak, 4);
    const top = Math.ceil(peak / step) * step;
    const t0 = times[0];
    const t1 = times[times.length - 1];
    const xOf = (t) => margin.left + ((t - t0) / (t1 - t0 || 1)) * plotW;
    const yOf = (v) => margin.top + (1 - v / top) * plotH;
    scale = { xOf, yOf, margin, plotW, height };

    for (let v = 0; v <= top + step / 2; v += step) {
      svg.appendChild(s('line.oee-grid', { x1: margin.left, x2: margin.left + plotW, y1: yOf(v), y2: yOf(v) }));
      svg.appendChild(s('text.oee-axis', { x: margin.left - 6, y: yOf(v) + 4, 'text-anchor': 'end' }, fmt.number(v)));
    }
    for (const t of timeTicks(t0, t1, 6)) {
      svg.appendChild(s('text.oee-axis', { x: xOf(t), y: height - 5, 'text-anchor': 'middle' }, fmt.simClock(t)));
    }
    const points = total.map((v, i) => (isNumber(v) ? [xOf(times[i]), yOf(v)] : null));
    const known = points.filter(Boolean);
    if (known.length > 1) {
      const area = `${linePath(known)}L${known[known.length - 1][0].toFixed(1)} ${yOf(0).toFixed(1)}L${known[0][0].toFixed(1)} ${yOf(0).toFixed(1)}Z`;
      svg.appendChild(s('path.oee-area', { d: area }));
    }
    svg.appendChild(s('path.oee-line', { d: linePath(points) }));
    const last = known[known.length - 1];
    if (last) {
      svg.appendChild(s('circle.oee-dot', { cx: last[0], cy: last[1], r: 4 }));
      svg.appendChild(s('text.oee-endlabel', { x: last[0] + 8, y: last[1] + 4 }, fmt.number(total[total.length - 1])));
    }
    paintHover();
  }

  let cursor = null;
  let hoverDot = null;
  function paintHover() {
    cursor?.remove();
    hoverDot?.remove();
    cursor = null;
    hoverDot = null;
    const times = data?.t ?? [];
    if (hover === null || !scale || !times.length) {
      tip.hidden = true;
      return;
    }
    const x = scale.xOf(times[hover]);
    cursor = s('line.oee-cursor', { x1: x, x2: x, y1: scale.margin.top, y2: scale.height - scale.margin.bottom });
    svg.appendChild(cursor);
    const value = data.kw.total[hover];
    if (isNumber(value)) {
      hoverDot = s('circle.oee-dot', { cx: x, cy: scale.yOf(value), r: 4 });
      svg.appendChild(hoverDot);
    }
    clear(tip);
    tip.append(
      h('div.oee-tip__time.mono', fmt.simTime(times[hover])),
      h('div.oee-tip__row.oee-tip__row--total', h('span.oee-tip__value', fmt.kw(value)), h('span', 'Gesamt')),
      ...Object.keys(data.kw).filter((id) => id !== 'total').map((id) => h('div.oee-tip__row', h('span.oee-tip__value', fmt.kw(data.kw[id][hover])), h('span', id))),
    );
    tip.hidden = false;
    const own = tip.getBoundingClientRect();
    const hostWidth = host.clientWidth;
    tip.style.left = `${x + 12 + own.width > hostWidth ? x - own.width - 12 : x + 12}px`;
  }

  svg.addEventListener('pointermove', (event) => {
    if (!scale || !data?.t?.length) return;
    const box2 = svg.getBoundingClientRect();
    const px = event.clientX - box2.left;
    const t = data.t[0] + ((px - scale.margin.left) / scale.plotW) * (data.t[data.t.length - 1] - data.t[0]);
    hover = nearestIndex(data.t, t);
    paintHover();
  });
  svg.addEventListener('pointerleave', () => {
    hover = null;
    paintHover();
  });
  svg.addEventListener('blur', () => {
    hover = null;
    paintHover();
  });
  svg.addEventListener('keydown', (event) => {
    if (!data?.t?.length) return;
    if (event.key !== 'ArrowLeft' && event.key !== 'ArrowRight') return;
    event.preventDefault();
    const start = hover ?? data.t.length - 1;
    hover = Math.max(0, Math.min(data.t.length - 1, start + (event.key === 'ArrowLeft' ? -1 : 1)));
    paintHover();
  });

  const resize = new ResizeObserver(draw);
  resize.observe(host);

  return {
    node: box.node,
    update(energy, windowId) {
      setText(title, `Gesamtleistung, Verlauf ${WINDOW_WORDS[windowId]}`);
      // /api/energy has no window of its own.
      const clipped = clipToWindow(energy.trend?.t, energy.trend?.kw, windowId, store.clock.now());
      data = energy.trend ? { t: clipped.t, kw: clipped.series } : null;
      if (hover !== null && hover >= (data?.t?.length ?? 0)) hover = null;
      draw();
    },
    live(kpi) {
      setText(now, fmt.kw(kpi?.power_kw));
    },
    destroy() {
      resize.disconnect();
    },
  };
}

// --- Energy by machine ---
function buildShare(ctx) {
  const { fmt, store } = ctx;
  const box = panel('Energie je Maschine', { dense: true });
  box.node.classList.add('oee-share');
  const tbody = h('tbody');
  box.body.appendChild(h('table.tbl.oee-table',
    h('thead', h('tr',
      h('th', 'Maschine'),
      h('th', 'Anteil'),
      h('th.num', 'kWh'),
      h('th.num', { 'data-tip': 'Aktuelle Leistung, live' }, 'kW jetzt'),
      h('th.num', { 'data-tip': 'Energie je gefertigter Einheit dieser Maschine' }, 'Je Einheit'))),
    tbody));
  const rows = new Map();

  function row(id) {
    const fill = h('span.oee-bar__fill');
    const percent = h('span.oee-factor__value');
    const kwh = h('td.num');
    const kw = h('td.num');
    const perUnit = h('td.num');
    const tr = h('tr',
      h('td.oee-name', h('span.oee-name__id', { 'data-tip': fmt.machineName(id) }, id)),
      h('td.oee-factor', h('span.oee-factor__inner', h('span.oee-bar', fill), percent)),
      kwh, kw, perUnit);
    tbody.appendChild(tr);
    const entry = { tr, fill, percent, kwh, kw, perUnit };
    rows.set(id, entry);
    return entry;
  }

  return {
    node: box.node,
    update(energy) {
      const machines = energy.machines ?? [];
      const top = Math.max(1e-6, ...machines.map((m) => m.share ?? 0));
      for (const machine of machines) {
        const entry = rows.get(machine.id) ?? row(machine.id);
        entry.fill.style.width = `${(((machine.share ?? 0) / top) * 100).toFixed(1)}%`;
        setText(entry.percent, fmt.percent(machine.share));
        setText(entry.kwh, fmt.number(machine.energy_kwh));
        const unit = UNIT_ONE[machine.unit] ?? machine.unit ?? '';
        setText(entry.perUnit, isNumber(machine.kwh_per_unit) ? `${fmt.number(machine.kwh_per_unit, 2)} kWh/${unit}` : fmt.DASH);
        entry.perUnit.setAttribute('data-tip', isNumber(machine.kwh_per_unit) ? `Energie je ${unit}` : 'Noch keine gefertigte Einheit');
      }
    },
    live() {
      for (const [id, entry] of rows) setText(entry.kw, fmt.number(store.machine(id)?.power_kw));
    },
  };
}

// --- Energy by order ---
function buildByOrder(ctx) {
  const { fmt, router } = ctx;
  const count = h('span.tag.tag--mono', '0');
  const box = panel('Energie je Auftrag', { dense: true, actions: [count] });
  box.node.classList.add('oee-orders');
  const tbody = h('tbody');
  box.body.appendChild(h('table.tbl.oee-table',
    h('thead', h('tr', h('th', 'Auftrag'), h('th.num', 'kWh'), h('th.num', 'kg'), h('th.num', 'kWh/kg'))),
    tbody));
  let key = '';
  return {
    node: box.node,
    update(energy) {
      const rows = [...(energy.by_order ?? [])].sort((a, b) => String(b.order_id).localeCompare(String(a.order_id), 'de', { numeric: true }));
      const next = JSON.stringify(rows);
      setText(count, rows.length);
      if (next === key) return;
      key = next;
      clear(tbody);
      for (const row of rows) {
        tbody.appendChild(h('tr',
          h('td', h('a.mono', { href: router.href('rueckverfolgung', { q: row.order_id }), 'data-tip': 'In der Rückverfolgung zeigen' }, row.order_id)),
          h('td.num', fmt.number(row.energy_kwh)),
          h('td.num', fmt.number(row.kg)),
          h('td.num', isNumber(row.kwh_per_kg) ? {} : { 'data-tip': 'Noch nichts versandt' }, fmt.number(row.kwh_per_kg, 2))));
      }
    },
  };
}

export function mount(el, ctx) {
  loadCss('css/views/oee.css');
  const { api, fmt, store, router } = ctx;
  el.classList.add('oee');

  let alive = true;
  let windowId = router.params.window === '8h' ? '8h' : 'start';
  let oeeToken = 0;

  const buttons = WINDOWS.map((entry) => h('button.seg__btn.oee-seg-btn', {
    type: 'button',
    'aria-pressed': String(entry.id === windowId),
    onclick: () => {
      if (entry.id === windowId) return;
      windowId = entry.id;
      for (const [i, button] of buttons.entries()) button.setAttribute('aria-pressed', String(WINDOWS[i].id === windowId));
      content.classList.add('is-loading');
      oeeToken += 1;
      oeePoll.now();
      energyPoll.now();
    },
  }, entry.label));
  const trouble = h('span.oee-trouble', { hidden: true }, statusIcon('warn', { size: 11 }), h('span'));
  const bar = h('div.toolbar.oee-toolbar',
    h('span.field__label', 'Fenster'),
    h('div.seg', { role: 'group', 'aria-label': 'Zeitfenster' }, buttons),
    h('p.oee-definition',
      h('strong', 'OEE netto'), ': Verfügbarkeit ohne Wartezeit auf Material × Leistung × Qualität. ',
      h('strong', 'OEE ISO'), ' (ISO 22400): Materialmangel zählt als Verfügbarkeitsverlust.'),
    trouble);

  const headline = buildHeadline(fmt);
  const oeeTable = buildOeeTable(ctx);
  const lossTable = buildLossTable(ctx);
  const multiples = buildMultiples(ctx);
  const power = buildPower(ctx);
  const share = buildShare(ctx);
  const byOrder = buildByOrder(ctx);

  const content = h('div.oee-content',
    headline.node,
    h('div.oee-row.oee-row--tables', oeeTable.node, lossTable.node),
    multiples.node,
    h('div.oee-row.oee-row--energy', power.node, share.node, byOrder.node));
  el.append(bar, content);

  function showTrouble(text) {
    trouble.hidden = !text;
    if (text) setText(trouble.lastChild, text);
  }

  const oeePoll = poll(async () => {
    const mine = oeeToken;
    try {
      const data = await api.get(`/api/oee?window=${windowId}`, { quiet: true });
      if (!alive || mine !== oeeToken) return;
      headline.plant(data.plant);
      oeeTable.update(data);
      lossTable.update(data, windowId);
      multiples.update(data, windowId);
      oeeTable.live();
      content.classList.remove('is-loading');
      showTrouble('');
    } catch (error) {
      if (alive) showTrouble(`Keine aktuellen OEE-Daten: ${error.message}`);
    }
  }, 4000);

  const energyPoll = poll(async () => {
    const data = await api.get('/api/energy', { quiet: true });
    if (!alive) return;
    power.update(data, windowId);
    share.update(data);
    byOrder.update(data);
    share.live();
  }, 4000);

  const offState = store.subscribe((state) => {
    headline.live(state.kpi);
    power.live(state.kpi);
    share.live();
    oeeTable.live();
  });
  const offRun = store.on('run-change', () => {
    oeePoll.now();
    energyPoll.now();
  });

  return () => {
    alive = false;
    oeePoll.stop();
    energyPoll.stop();
    offState();
    offRun();
    multiples.destroy();
    power.destroy();
  };
}
