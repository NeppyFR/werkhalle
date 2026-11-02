// Gantt chart for a plan or a twin variant. Rows are machines in process order, bars carry the order's colour, furnace loads are one block
// with a sub-band per lot. Used by Planung and Zwilling. Geometry is recomputed per view from a flat list of marks, so 150 assignments
// pan and zoom at frame rate: only attributes of what is visible get written, and hit testing is arithmetic instead of 150 DOM listeners.
//
//   const chart = renderGantt(container, {assignments, downtimes, orders, now, rows, rowLabels, domain?, compact?, onSelect?});
//   chart.update({now}) or chart.update({assignments, downtimes, orders, now}); chart.destroy();
//
// Further options: selected (order id), legend (bool, default !compact), legendKey (bool, default true: the state key beside the order chips),
// orderCompletion ({orderId: minute}, otherwise derived from the last end), onViewChange(view, {fit}) for charts sharing a zoom,
// tableToggle (bool, default !compact), legendHost and keyHost (elements that take the order chips or the state key instead of the chart itself,
// so a key can sit in a panel head and charts can share one legend). faults ([{machine, start, end, reason, live}]): real outages from the plant, apart
// from the plan's own downtimes and drawn over the bars, because a bar in there did not run as drawn.
// Further methods: setSelected(id), setView([t0, t1]), getView(), fit(), toggleTable(), element. ganttDomain(datasets) gives the union domain of
// several charts.
import { h, s, clear, loadCss } from '../core/dom.js';
import { fmt } from '../core/format.js';
import { statusIcon } from './ui.js';

const DEFAULT_ROWS = ['GUSS', 'PR1', 'PR2', 'OF1', 'OF2', 'SAE', 'VP'];
const STATE_LABEL = { pending: 'geplant', running: 'läuft', done: 'fertig' };
const TICK_STEPS = [15, 30, 60, 120, 180, 360, 720, 1440];
const HIT_TOLERANCE_PX = 5;
const MIN_BAR_PX = 2;
const MIN_SPAN_MIN = 30;
const HUE_COUNT = 5;
const SLOT_COUNT = HUE_COUNT * 2;
// Label colour per slot, from the contrast of each hue against dark and white ink. Hollow slots sit on a dark ground.
const SLOT_INK = ['d', 'd', 'l', 'l', 'l', 't', 't', 't', 't', 't'];

let chartCounter = 0;
let charWidth = 0;

// Colour follows the order, not its rank: a slot stays with its order until the order has nothing left to do on any open chart.
const slotOf = new Map();
const liveCharts = new Set();

function syncSlots() {
  const live = new Map();
  for (const chart of liveCharts) {
    for (const [id, due] of chart.liveOrders) {
      const known = live.get(id);
      live.set(id, known === undefined ? due : Math.min(known, due));
    }
  }
  for (const id of [...slotOf.keys()]) if (!live.has(id)) slotOf.delete(id);
  const used = new Set(slotOf.values());
  const waiting = [...live.entries()].filter(([id]) => !slotOf.has(id)).sort((a, b) => a[1] - b[1] || (a[0] < b[0] ? -1 : 1));
  for (const [id] of waiting) {
    let slot = 0;
    while (slot < SLOT_COUNT && used.has(slot)) slot += 1;
    if (slot === SLOT_COUNT) break;
    slotOf.set(id, slot);
    used.add(slot);
  }
}

function slotClass(orderId) {
  if (!orderId) return 'gsc';
  const slot = slotOf.get(orderId);
  return slot === undefined ? 'gsn' : `gs${slot}`;
}

function slotInk(orderId) {
  const slot = slotOf.get(orderId);
  return slot === undefined ? 'l' : SLOT_INK[slot];
}

function measureCharWidth() {
  if (charWidth) return charWidth;
  const context = document.createElement('canvas').getContext('2d');
  context.font = '10px "Cascadia Mono", Consolas, monospace';
  charWidth = (context.measureText('0000000000').width / 10) || 6.1;
  return charWidth;
}

function fitLabel(options, available) {
  const per = measureCharWidth();
  for (const option of options) {
    if (option.length * per + 6 <= available) return option;
  }
  return '';
}

/** Smallest window that shows all ops and now. A repair estimate far beyond the plan does not squeeze the bars: it runs off the edge instead. */
export function ganttDomain(datasets) {
  let lo = Infinity;
  let hi = -Infinity;
  const list = Array.isArray(datasets) ? datasets : [datasets];
  for (const data of list) {
    for (const a of data?.assignments ?? []) {
      lo = Math.min(lo, a.start - (a.setup_before ?? 0));
      hi = Math.max(hi, a.end);
    }
    if (typeof data?.now === 'number') {
      lo = Math.min(lo, data.now);
      hi = Math.max(hi, data.now);
    }
  }
  if (!Number.isFinite(lo)) return [0, 480];
  if (hi - lo < 120) hi = lo + 120;
  const reach = Math.max(240, (hi - lo) * 0.25);
  for (const data of list) {
    for (const d of [...(data?.downtimes ?? []), ...(data?.faults ?? [])]) {
      if (d.start >= hi || d.end <= lo) continue;
      if (d.end <= hi + reach) hi = Math.max(hi, d.end);
      lo = Math.min(lo, d.start);
    }
  }
  const pad = Math.max(8, (hi - lo) * 0.012);
  return [lo - pad, hi + pad];
}

function buildMarks(assignments) {
  const marks = [];
  const batches = new Map();
  for (const a of assignments) {
    if (a.batch_id) {
      const key = `${a.machine}|${a.batch_id}`;
      let mark = batches.get(key);
      if (!mark) {
        mark = { row: a.machine, ops: [], batchId: a.batch_id, kind: a.kind };
        batches.set(key, mark);
        marks.push(mark);
      }
      mark.ops.push(a);
    } else {
      marks.push({ row: a.machine, ops: [a], batchId: null, kind: a.kind });
    }
  }
  for (const mark of marks) {
    mark.ops.sort((x, y) => (x.order_id ?? '').localeCompare(y.order_id ?? '') || (x.lot_id ?? '').localeCompare(y.lot_id ?? ''));
    mark.start = Math.min(...mark.ops.map((o) => o.start));
    mark.end = Math.max(...mark.ops.map((o) => o.end));
    mark.setup = Math.max(...mark.ops.map((o) => o.setup_before ?? 0));
    const states = mark.ops.map((o) => o.state ?? 'pending');
    mark.state = states.includes('running') ? 'running' : states.every((v) => v === 'done') ? 'done' : 'pending';
    mark.frozen = mark.state !== 'done' && mark.ops.some((o) => o.frozen);
    mark.orderIds = [...new Set(mark.ops.map((o) => o.order_id).filter(Boolean))];
  }
  return marks;
}

function downLabel(reason, isFault) {
  if (!reason) return isFault ? { long: 'Störung', short: 'Störung' } : { long: 'Stillstand', short: 'Stillstand' };
  if (/^[A-Z]+-[A-Z]?\d+/.test(reason)) return { long: `Störung ${reason}`, short: reason };
  return { long: reason, short: reason };
}

function keyGlyph(kind, pid) {
  const svg = s('svg', { width: 22, height: 10, 'aria-hidden': 'true' });
  if (kind === 'done') svg.append(s('rect.gantt-fill.gsn', { x: 0, y: 3, width: 22, height: 4, opacity: 0.5 }));
  else if (kind === 'running') svg.append(s('rect.gsn', { x: 0.5, y: 0.5, width: 21, height: 9, stroke: 'var(--text-1)', 'stroke-width': 1 }), s('rect.gantt-remaining', { x: 12, y: 1, width: 9, height: 8 }));
  else if (kind === 'pending') svg.append(s('rect.gsn', { x: 0, y: 0, width: 22, height: 10 }));
  else if (kind === 'frozen') svg.append(s('rect.gsn', { x: 0, y: 0, width: 22, height: 10 }), s('rect.gantt-frozen', { x: 0, y: 0, width: 22, height: 2 }));
  else if (kind === 'setup') svg.append(s('rect', { x: 0, y: 0, width: 22, height: 10, fill: `url(#${pid}-setup)` }));
  else if (kind === 'fault') svg.append(s('rect.gsn', { x: 0, y: 0, width: 22, height: 10 }), s('rect.gantt-faultveil', { x: 0, y: 0, width: 22, height: 10 }), s('rect', { x: 0, y: 0, width: 22, height: 10, fill: `url(#${pid}-down)` }));
  else if (kind === 'down') svg.append(s('rect', { x: 0, y: 0, width: 22, height: 10, fill: 'var(--state-fault-dim)' }), s('rect', { x: 0, y: 0, width: 22, height: 10, fill: `url(#${pid}-down)` }));
  return svg;
}

export function renderGantt(container, options = {}) {
  loadCss('css/gantt.css');
  const compact = Boolean(options.compact);
  const showLegend = options.legend ?? !compact;
  const showKey = options.legendKey ?? true;
  const allowTable = options.tableToggle ?? !compact;
  const pid = `gantt${++chartCounter}`;
  const size = compact
    ? { row: 17, batch: 30, axis: 30, foot: 15, gutter: 44, barPad: 2 }
    : { row: 24, batch: 44, axis: 32, foot: 17, gutter: 146, barPad: 3 };

  const data = {
    assignments: [],
    downtimes: [],
    orders: [],
    now: null,
    rows: DEFAULT_ROWS,
    rowLabels: null,
    domain: null,
    orderCompletion: null,
    selected: null,
    ...options,
  };
  const chartRef = { liveOrders: new Map() };
  liveCharts.add(chartRef);

  let marks = [];
  let marksByRow = new Map();
  let downs = [];
  let rowsLayout = [];
  let rowIndex = new Map();
  let orderInfo = new Map();
  let view = [0, 480];
  let isFit = true;
  let width = 0;
  let plotW = 0;
  let plotH = 0;
  let raf = 0;
  let destroyed = false;
  let hover = null;
  let tipKey = '';
  let cursor = null;
  let drag = null;
  let tableOpen = false;

  // ------------------------------------------------------------------ static DOM
  const defs = s('defs',
    s('clipPath', { id: `${pid}-clip` }, s('rect', { x: 0, y: 0, width: 10, height: 10 })),
    s('pattern', { id: `${pid}-setup`, patternUnits: 'userSpaceOnUse', width: 5, height: 5, patternTransform: 'rotate(45)' },
      s('rect', { width: 5, height: 5, fill: 'var(--state-setup-dim)' }),
      s('rect', { width: 1.1, height: 5, fill: 'var(--state-setup)', opacity: 0.8 })),
    s('pattern', { id: `${pid}-down`, patternUnits: 'userSpaceOnUse', width: 7, height: 7, patternTransform: 'rotate(-45)' },
      s('rect', { width: 1.8, height: 7, fill: 'var(--state-fault)' })));
  const clipRect = defs.firstChild.firstChild;

  const tickLayer = s('g');
  const dayLayer = s('g');
  const axisLine = s('line.gantt-axisline');
  const plotBg = s('rect.gantt-plotbg', { x: 0, y: 0 });
  const past = s('rect.gantt-past', { x: 0, y: 0 });
  const gridLayer = s('g');
  const rowLines = s('g');
  const downBodies = s('g');
  const markLayer = s('g');
  const downOver = s('g');
  const lateLayer = s('rect.gantt-latespan', { visibility: 'hidden' });
  const dueLine = s('line.gantt-due', { visibility: 'hidden' });
  const nowLine = s('line.gantt-now', { visibility: 'hidden' });
  const cursorRect = s('rect.gantt-cursor', { visibility: 'hidden', rx: 2 });
  const plot = s('g', { 'clip-path': `url(#${pid}-clip)` },
    plotBg, past, gridLayer, rowLines, downBodies, markLayer, downOver, lateLayer, dueLine, nowLine, cursorRect);
  const plotHost = s('g', {}, plot);
  const gutter = s('g');
  const nowFlagText = s('text.gantt-flag__text', { y: 0, 'text-anchor': 'middle' }, 'Jetzt');
  const nowFlagBg = s('rect.gantt-flag__bg', { rx: 2 });
  const nowFlag = s('g.gantt-flag', { visibility: 'hidden' }, nowFlagBg, nowFlagText);
  const dueFlagText = s('text.gantt-dueflag__text');
  const dueFlagBg = s('rect.gantt-dueflag__bg', { rx: 2 });
  const dueFlag = s('g.gantt-flag', { visibility: 'hidden' }, dueFlagBg, dueFlagText);

  const svg = s('svg.gantt__svg', {
    tabindex: 0,
    role: 'application',
    'aria-roledescription': 'Gantt-Diagramm',
    style: { cursor: 'grab' },
  }, defs, tickLayer, dayLayer, axisLine, plotHost, gutter, nowFlag, dueFlag);

  const tip = h('div.tooltip.gantt-tip', { role: 'tooltip', hidden: true });
  const live = h('div.gantt__sr', { 'aria-live': 'polite' });
  const tools = h('div.gantt__tools');
  const frame = h('div.gantt__frame', svg, tools, tip, live);
  const chips = h('div.gantt__chips', { role: 'group', 'aria-label': 'Aufträge im Plan' });
  const tableHost = h('div.gantt__table', { hidden: true });
  function toggleTable() {
    tableOpen = !tableOpen;
    tableHost.hidden = !tableOpen;
    if (tableOpen) renderTable();
    if (tableButton) {
      tableButton.setAttribute('aria-pressed', String(tableOpen));
      tableButton.textContent = tableOpen ? 'Tabelle ausblenden' : 'Als Tabelle';
    }
    return tableOpen;
  }
  const tableButton = allowTable ? h('button.btn.btn--sm.btn--ghost', { type: 'button', 'aria-pressed': 'false', onclick: toggleTable }, 'Als Tabelle') : null;
  const foldedKey = h('span.gantt-key', { hidden: true, 'data-tip': 'Mehr Aufträge als unterscheidbare Farben: die übrigen tragen ihre Nummer am Balken und im Tooltip.' }, keyGlyph('pending', pid), 'weitere Aufträge');
  const key = h('div.gantt__key',
    foldedKey,
    [['done', 'fertig'], ['running', 'läuft'], ['pending', 'geplant'], ['frozen', 'fixiert'], ['setup', 'Rüsten'], ['down', 'Stillstand'], ...(options.faults ? [['fault', 'Störung']] : [])]
      .map(([kind, label]) => h('span.gantt-key', keyGlyph(kind, pid), label)),
    h('span.gantt-key', statusIcon('warn', { size: 10 }), 'verspätet'),
    tableButton);
  const legend = showLegend ? h('div.gantt__legend', chips, showKey && !options.keyHost ? key : null) : null;
  const root = h(`div.gantt${compact ? '.gantt--compact' : ''}`, options.legendHost ? null : legend, frame, tableHost);
  container.appendChild(root);
  if (legend && options.legendHost) options.legendHost.appendChild(legend);
  if (showLegend && showKey && options.keyHost) options.keyHost.appendChild(key);

  const zoomButton = (label, aria, factor) => h('button.btn.btn--sm', {
    type: 'button', 'aria-label': aria, 'data-tip': aria,
    onclick: () => zoomAt(factor, plotW / 2),
  }, label);
  if (!compact) tools.append(zoomButton('−', 'Herauszoomen', 1.5), zoomButton('+', 'Hineinzoomen', 1 / 1.5));
  tools.append(h('button.btn.btn--sm', {
    type: 'button',
    'aria-label': 'Ganzen Zeitraum zeigen',
    'data-tip': 'Ganzen Zeitraum zeigen',
    onclick: () => { fit(); notifyView(); },
  }, 'Alles'));

  // ------------------------------------------------------------------ scale helpers
  function fullDomain() {
    return data.domain ?? ganttDomain(data);
  }

  function clampView(next) {
    const [lo, hi] = fullDomain();
    const full = hi - lo;
    const span = Math.max(Math.min(MIN_SPAN_MIN, full), Math.min(next[1] - next[0], full));
    let start = next[0];
    if (start < lo) start = lo;
    if (start + span > hi) start = hi - span;
    return [start, start + span];
  }

  function x(t) {
    return (t - view[0]) * (plotW / (view[1] - view[0]));
  }

  function timeAt(px) {
    return view[0] + (px / plotW) * (view[1] - view[0]);
  }

  // ------------------------------------------------------------------ build per data change
  function rowOrder() {
    const order = [...(data.rows ?? DEFAULT_ROWS)];
    const seen = new Set(order);
    for (const a of data.assignments) {
      if (!seen.has(a.machine)) {
        seen.add(a.machine);
        order.push(a.machine);
      }
    }
    return order;
  }

  function rowLabel(id) {
    const labels = data.rowLabels;
    if (typeof labels === 'function') return labels(id);
    if (labels && labels[id]) return labels[id];
    return fmt.machineName(id);
  }

  function collectOrders() {
    const info = new Map();
    const dues = new Map();
    for (const order of data.orders ?? []) dues.set(order.id, order);
    for (const a of data.assignments) {
      if (!a.order_id) continue;
      let row = info.get(a.order_id);
      if (!row) {
        const known = dues.get(a.order_id);
        row = { id: a.order_id, due: known?.due ?? null, weight: known?.weight ?? 1, product: a.product_id, lastEnd: -Infinity, openOps: 0, marks: [] };
        info.set(a.order_id, row);
      }
      row.lastEnd = Math.max(row.lastEnd, a.end);
      if ((a.state ?? 'pending') !== 'done') row.openOps += 1;
    }
    for (const row of info.values()) {
      row.completion = data.orderCompletion?.[row.id] ?? row.lastEnd;
      row.lateBy = row.due === null ? null : row.completion - row.due;
      row.late = row.due !== null && row.lateBy > 0;
    }
    return info;
  }

  function buildRows() {
    clear(gutter);
    clear(rowLines);
    rowsLayout = [];
    rowIndex = new Map();
    const order = rowOrder();
    let y = 0;
    order.forEach((id, index) => {
      const hasBatch = id.startsWith('OF') || (marksByRow.get(id) ?? []).some((m) => m.ops.length > 1);
      const height = hasBatch ? size.batch : size.row;
      rowsLayout.push({ id, y, height, index });
      rowIndex.set(id, index);
      const textY = size.axis + y + height / 2 + 4;
      gutter.appendChild(s('text.gantt-rowid', { x: 4, y: textY }, id));
      if (!compact) gutter.appendChild(s('text.gantt-rowname', { x: 40, y: textY }, rowLabel(id)));
      rowLines.appendChild(s('line.gantt-rowline', { x1: 0, x2: 8000, y1: y + height + 0.5, y2: y + height + 0.5 }));
      y += height;
    });
    plotH = y;
    const total = size.axis + plotH + size.foot;
    svg.setAttribute('height', total);
    plotHost.setAttribute('transform', `translate(${size.gutter} ${size.axis})`);
    plotBg.setAttribute('height', plotH);
    past.setAttribute('height', plotH);
    clipRect.setAttribute('height', plotH + 1);
    axisLine.setAttribute('y1', size.axis - 0.5);
    axisLine.setAttribute('y2', size.axis - 0.5);
    axisLine.setAttribute('x1', 0);
    nowLine.setAttribute('y1', 0);
    nowLine.setAttribute('y2', plotH);
    dueLine.setAttribute('y1', 0);
    dueLine.setAttribute('y2', plotH);
    lateLayer.setAttribute('y', plotH - 3);
    lateLayer.setAttribute('height', 3);
  }

  function buildMark(mark) {
    const row = rowsLayout[rowIndex.get(mark.row)];
    const thin = mark.state === 'done';
    const full = row.height - size.barPad * 2;
    const bar = thin ? Math.max(5, Math.round(full * 0.45)) : full;
    const topPad = Math.round((row.height - bar) / 2);
    mark.y = row.y + topPad;
    mark.h = bar;
    mark.g = s(`g.gantt-mark.gantt-mark--${mark.state}`);
    mark.visible = true;
    mark.labelText = '';
    if (mark.setup > 0) {
      mark.setupRect = s('rect.gantt-setup', { y: 0, height: bar, fill: `url(#${pid}-setup)` });
      mark.g.appendChild(mark.setupRect);
    }
    mark.fills = [];
    const count = mark.ops.length;
    const gap = count > 1 ? 1 : 0;
    const bandH = (bar - gap * (count - 1)) / count;
    mark.bandH = bandH;
    mark.labels = [];
    mark.ops.forEach((op, i) => {
      const fill = s(`rect.gantt-fill.${slotClass(op.order_id)}`, { y: (bandH + gap) * i, height: Math.max(1, bandH) });
      mark.g.appendChild(fill);
      mark.fills.push({ node: fill, order: op.order_id });
    });
    if (count > 1 || mark.batchId) {
      mark.block = s('rect.gantt-block', { y: -0.5, height: bar + 1 });
      mark.g.appendChild(mark.block);
    }
    if (mark.state === 'running') {
      mark.remaining = s('rect.gantt-remaining', { y: 0, height: bar });
      mark.g.appendChild(mark.remaining);
    }
    const canLabelBands = bandH >= (compact ? 9 : 10.5) && !thin;
    mark.ops.forEach((op, i) => {
      if (!canLabelBands) return;
      const ink = op.order_id ? slotInk(op.order_id) : 'l';
      const label = s(`text.gantt-lab.gantt-lab--${ink}`, { y: (bandH + gap) * i + bandH / 2 + 3.3, x: 3 });
      mark.g.appendChild(label);
      const lot = fmt.lotId(op.lot_id ?? op.op_id);
      const melt = op.op_id.split(':')[0];
      const options = op.kind === 'cast'
        ? [fmt.lotId(melt), /M-([^-]+)-/.exec(melt)?.[1] ?? '']
        : op.order_id ? [`${fmt.orderId(op.order_id)} · ${lot}`, fmt.orderId(op.order_id)] : [lot];
      mark.labels.push({ node: label, options, text: '' });
    });
    if (mark.frozen) {
      mark.frozenRect = s('rect.gantt-frozen', { y: 0, height: 2, x: 0 });
      mark.g.appendChild(mark.frozenRect);
    }
    return mark;
  }

  function buildDowntime(down, isFault = false) {
    const row = rowsLayout[rowIndex.get(down.machine)];
    if (!row) return null;
    const text = downLabel(down.reason, isFault);
    const body = s('rect.gantt-down', { y: row.y, height: row.height });
    const hatch = s('rect.gantt-downhatch', { y: row.y, height: row.height, fill: `url(#${pid}-down)` });
    const edgeA = s('rect.gantt-downedge', { y: row.y, height: row.height, width: 1.5 });
    const edgeB = s('rect.gantt-downedge', { y: row.y, height: row.height, width: 1.5 });
    const labelY = row.y + Math.min(row.height / 2 + 3.5, 13);
    const plate = s('rect.gantt-downplate', { y: labelY - 10, height: 13, visibility: 'hidden' });
    const label = s('text.gantt-downlab', { y: labelY });
    downBodies.appendChild(body);
    const veil = isFault ? s('rect.gantt-faultveil', { y: row.y, height: row.height }) : null;
    if (veil) downOver.appendChild(veil);
    downOver.append(hatch, edgeA, edgeB, plate, label);
    return { ...down, isFault, row, body, veil, hatch, edgeA, edgeB, plate, label, text, labelText: '' };
  }

  function rebuild() {
    marks = buildMarks(data.assignments);
    marksByRow = new Map();
    for (const mark of marks) {
      if (!marksByRow.has(mark.row)) marksByRow.set(mark.row, []);
      marksByRow.get(mark.row).push(mark);
    }
    for (const list of marksByRow.values()) list.sort((a, b) => a.start - b.start);

    orderInfo = collectOrders();
    chartRef.liveOrders = new Map([...orderInfo.values()].filter((o) => o.openOps > 0).map((o) => [o.id, o.due ?? Infinity]));
    syncSlots();

    buildRows();
    clear(markLayer);
    clear(downBodies);
    clear(downOver);
    downs = [];
    for (const down of data.downtimes ?? []) {
      const built = buildDowntime(down);
      if (built) downs.push(built);
    }
    for (const fault of data.faults ?? []) {
      const built = buildDowntime(fault, true);
      if (built) downs.push(built);
    }
    for (const mark of marks) markLayer.appendChild(buildMark(mark).g);
    addLateFlags();
    renderLegend();
    if (isFit) view = fullDomain();
    else view = clampView(view);
    hover = null;
    if (cursor) cursor = marks.find((m) => m.ops[0].op_id === cursor.ops[0].op_id) ?? null;
    layout();
    applySelection();
    if (tableOpen) renderTable();
    svg.setAttribute('aria-label', `${fmt.number(data.assignments.length)} Operationen auf ${fmt.number(rowsLayout.length)} Maschinen. Pfeiltasten wählen eine Operation, Eingabe markiert ihren Auftrag, Plus und Minus zoomen.`);
  }

  function addLateFlags() {
    for (const info of orderInfo.values()) {
      if (!info.late) continue;
      let last = null;
      for (const mark of marks) {
        if (mark.orderIds.includes(info.id) && (!last || mark.end > last.end)) last = mark;
      }
      if (last && !last.lateFlag) {
        last.lateFlag = s('path.gantt-late', { d: 'M0 -4.5 4.5 0 0 4.5 -4.5 0Z' });
        last.g.appendChild(last.lateFlag);
        // Drawn last, so the next bar on the row cannot cover the flag.
        markLayer.appendChild(last.g);
      }
    }
  }

  // ------------------------------------------------------------------ geometry per view
  function layout() {
    if (destroyed || width <= 0) return;
    plotW = Math.max(40, width - size.gutter);
    const scale = plotW / (view[1] - view[0]);
    for (const mark of marks) {
      const x0 = (mark.start - view[0]) * scale;
      const x1 = (mark.end - view[0]) * scale;
      const setupW = mark.setup * scale;
      const visible = x1 >= -8 && x0 - setupW <= plotW + 8;
      if (visible !== mark.visible) {
        mark.g.style.display = visible ? '' : 'none';
        mark.visible = visible;
      }
      if (!visible) continue;
      mark.g.setAttribute('transform', `translate(${x0.toFixed(1)} ${mark.y})`);
      const w = Math.max(MIN_BAR_PX, x1 - x0 - 1);
      for (const fill of mark.fills) fill.node.setAttribute('width', w.toFixed(1));
      if (mark.block) mark.block.setAttribute('width', w.toFixed(1));
      if (mark.setupRect) {
        const sw = Math.max(1, setupW - 1);
        mark.setupRect.setAttribute('x', (-setupW).toFixed(1));
        mark.setupRect.setAttribute('width', sw.toFixed(1));
      }
      if (mark.labels.length) {
        // The label stays inside the visible part of the bar, and is dropped rather than clipped when it does not fit.
        const left = Math.max(0, -x0);
        const right = Math.min(w, plotW - x0);
        const room = right - left;
        for (const label of mark.labels) {
          const text = fitLabel(label.options, room);
          if (text !== label.text) {
            label.node.textContent = text;
            label.text = text;
          }
          if (text) label.node.setAttribute('x', (left + 3).toFixed(1));
        }
      }
      if (mark.lateFlag) mark.lateFlag.setAttribute('transform', `translate(${(w + 5).toFixed(1)} ${(mark.h / 2).toFixed(1)})`);
      if (mark.frozenRect) mark.frozenRect.setAttribute('width', w.toFixed(1));
      if (mark.remaining) updateProgress(mark, w);
    }
    for (const d of downs) {
      const a = Math.max(-2, x(d.start));
      const b = Math.min(plotW + 2, x(d.end));
      const w = Math.max(0, b - a);
      d.body.setAttribute('x', a.toFixed(1));
      d.body.setAttribute('width', w.toFixed(1));
      d.hatch.setAttribute('x', a.toFixed(1));
      d.hatch.setAttribute('width', w.toFixed(1));
      if (d.veil) {
        d.veil.setAttribute('x', a.toFixed(1));
        d.veil.setAttribute('width', w.toFixed(1));
      }
      d.edgeA.setAttribute('x', a.toFixed(1));
      d.edgeB.setAttribute('x', (b - 1.5).toFixed(1));
      const text = w > d.text.long.length * 5.6 + 10 ? d.text.long : w > d.text.short.length * 5.6 + 10 ? d.text.short : '';
      if (text !== d.labelText) {
        d.label.textContent = text;
        d.labelText = text;
      }
      const labelX = Math.max(0, a) + 5;
      d.label.setAttribute('x', labelX.toFixed(1));
      d.plate.setAttribute('visibility', text ? 'visible' : 'hidden');
      if (text) {
        d.plate.setAttribute('x', (labelX - 3).toFixed(1));
        d.plate.setAttribute('width', (text.length * 5.6 + 6).toFixed(1));
      }
    }
    drawAxis(scale);
    layoutMarkers();
    if (cursor) drawCursor();
  }

  function updateProgress(mark, w) {
    const now = data.now;
    const fraction = typeof now === 'number' ? Math.max(0, Math.min(1, (now - mark.start) / Math.max(1, mark.end - mark.start))) : 0;
    mark.remaining.setAttribute('x', (w * fraction).toFixed(1));
    mark.remaining.setAttribute('width', (w * (1 - fraction)).toFixed(1));
  }

  const pool = { ticks: [], grids: [], days: [] };
  function pooled(list, parent, factory, index) {
    let node = list[index];
    if (!node) {
      node = factory();
      list.push(node);
      parent.appendChild(node);
    }
    if (node.style.display === 'none') node.style.display = '';
    return node;
  }
  function hideRest(list, from) {
    for (let i = from; i < list.length; i += 1) list[i].style.display = 'none';
  }

  function drawAxis(scale) {
    const minPx = compact ? 44 : 52;
    let step = TICK_STEPS.find((v) => v * scale >= minPx);
    if (!step) step = 1440 * Math.ceil(minPx / (1440 * scale));
    const first = Math.ceil((view[0] + 360) / step) * step - 360;
    let tickCount = 0;
    let gridCount = 0;
    const offset = size.gutter;
    for (let t = first; t <= view[1]; t += step) {
      const px = (t - view[0]) * scale;
      const clockMinute = ((t + 360) % 1440 + 1440) % 1440;
      const line = pooled(pool.grids, gridLayer, () => s('line.gantt-grid', { y1: 0 }), gridCount++);
      line.setAttribute('x1', px.toFixed(1));
      line.setAttribute('x2', px.toFixed(1));
      line.setAttribute('y2', plotH);
      line.setAttribute('class', clockMinute === 0 ? 'gantt-grid gantt-grid--day' : 'gantt-grid');
      const label = pooled(pool.ticks, tickLayer, () => s('text.gantt-tick', { 'text-anchor': 'middle' }), tickCount++);
      label.setAttribute('x', (px + offset).toFixed(1));
      label.setAttribute('y', size.axis - 5);
      const clock = fmt.simClock(t);
      if (label.textContent !== clock) label.textContent = clock;
    }
    hideRest(pool.grids, gridCount);
    hideRest(pool.ticks, tickCount);

    // Day labels stick to the left edge while their day is on screen.
    let dayCount = 0;
    const dayPx = 1440 * scale;
    const every = dayPx < 56 ? Math.ceil(56 / dayPx) : 1;
    let dayStart = Math.floor((view[0] + 360) / 1440) * 1440 - 360;
    let index = 0;
    while (dayStart <= view[1]) {
      const next = dayStart + 1440;
      const left = Math.max(0, (dayStart - view[0]) * scale);
      const nextLeft = (next - view[0]) * scale;
      if (index % every === 0 && nextLeft - left >= 40) {
        const label = pooled(pool.days, dayLayer, () => s('text.gantt-day'), dayCount++);
        let labelX = left + offset + 3;
        const nowX = typeof data.now === 'number' ? (data.now - view[0]) * scale + offset : -Infinity;
        if (labelX > nowX - 54 && labelX < nowX + 18) labelX = nowX + 20;
        label.setAttribute('x', labelX.toFixed(1));
        label.setAttribute('y', 12);
        const text = `Tag ${fmt.simDay(dayStart)}`;
        if (label.textContent !== text) label.textContent = text;
      }
      dayStart = next;
      index += 1;
    }
    hideRest(pool.days, dayCount);
    axisLine.setAttribute('x2', width);
    plotBg.setAttribute('width', plotW);
    clipRect.setAttribute('width', plotW);
    svg.setAttribute('width', width);
  }

  function layoutMarkers() {
    const now = data.now;
    const hasNow = typeof now === 'number';
    if (hasNow) {
      const px = x(now);
      const inside = px >= 0 && px <= plotW;
      past.setAttribute('width', Math.max(0, Math.min(plotW, px)).toFixed(1));
      nowLine.setAttribute('visibility', inside ? 'visible' : 'hidden');
      nowFlag.setAttribute('visibility', inside ? 'visible' : 'hidden');
      if (inside) {
        nowLine.setAttribute('x1', px.toFixed(1));
        nowLine.setAttribute('x2', px.toFixed(1));
        const flagW = 32;
        const flagX = Math.min(width - flagW / 2 - 1, px + size.gutter);
        nowFlagBg.setAttribute('x', (flagX - flagW / 2).toFixed(1));
        nowFlagBg.setAttribute('y', 1);
        nowFlagBg.setAttribute('width', flagW);
        nowFlagBg.setAttribute('height', 14);
        nowFlagText.setAttribute('x', flagX.toFixed(1));
        nowFlagText.setAttribute('y', 12);
      }
    } else {
      past.setAttribute('width', 0);
      nowLine.setAttribute('visibility', 'hidden');
      nowFlag.setAttribute('visibility', 'hidden');
    }
    layoutDue();
  }

  function layoutDue() {
    const info = data.selected ? orderInfo.get(data.selected) : null;
    const due = info?.due;
    if (due === null || due === undefined) {
      dueLine.setAttribute('visibility', 'hidden');
      dueFlag.setAttribute('visibility', 'hidden');
      lateLayer.setAttribute('visibility', 'hidden');
      return;
    }
    const px = x(due);
    const inside = px >= 0 && px <= plotW;
    dueLine.setAttribute('visibility', inside ? 'visible' : 'hidden');
    if (inside) {
      dueLine.setAttribute('x1', px.toFixed(1));
      dueLine.setAttribute('x2', px.toFixed(1));
    }
    if (info.late) {
      const a = Math.max(0, px);
      const b = Math.min(plotW, x(info.completion));
      lateLayer.setAttribute('visibility', b > a ? 'visible' : 'hidden');
      lateLayer.setAttribute('x', a.toFixed(1));
      lateLayer.setAttribute('width', Math.max(0, b - a).toFixed(1));
    } else {
      lateLayer.setAttribute('visibility', 'hidden');
    }
    // Off screen, the flag sits at the edge with an arrow, so the due date is never silently out of view.
    const arrow = px < 0 ? '◂ ' : px > plotW ? ' ▸' : '';
    const late = info.late ? ` · ${fmt.duration(info.lateBy, { signed: true })}` : '';
    const body = compact ? `Termin ${fmt.simTime(due)}${late}` : `Termin ${fmt.orderId(info.id)} · ${fmt.simTime(due)}${late}`;
    const text = px < 0 ? `${arrow}${body}` : `${body}${arrow}`;
    if (dueFlagText.textContent !== text) dueFlagText.textContent = text;
    const flagW = text.length * 5.5 + 10;
    const center = Math.max(flagW / 2, Math.min(plotW - flagW / 2, px)) + size.gutter;
    const y = size.axis + plotH + 1;
    dueFlagBg.setAttribute('x', (center - flagW / 2).toFixed(1));
    dueFlagBg.setAttribute('y', y);
    dueFlagBg.setAttribute('width', flagW.toFixed(1));
    dueFlagBg.setAttribute('height', size.foot - 2);
    dueFlagText.setAttribute('x', (center - flagW / 2 + 5).toFixed(1));
    dueFlagText.setAttribute('y', y + size.foot - 6);
    dueFlag.setAttribute('visibility', 'visible');
  }

  function requestLayout() {
    if (raf) return;
    raf = requestAnimationFrame(() => {
      raf = 0;
      layout();
    });
  }

  // ------------------------------------------------------------------ selection, legend
  function applySelection() {
    const selected = data.selected && orderInfo.has(data.selected) ? data.selected : null;
    root.classList.toggle('gantt--selection', Boolean(selected));
    for (const mark of marks) {
      const hit = Boolean(selected) && mark.orderIds.includes(selected);
      mark.g.classList.toggle('is-sel', hit);
      for (const fill of mark.fills) fill.node.classList.toggle('is-sel', hit && fill.order === selected);
    }
    for (const chip of chips.children) {
      chip.setAttribute('aria-pressed', String(chip.dataset.order === data.selected));
    }
    layoutDue();
  }

  function setSelected(id, { notify = false } = {}) {
    data.selected = id ?? null;
    applySelection();
    if (notify) data.onSelect?.(data.selected);
  }

  function renderLegend() {
    if (!legend) return;
    clear(chips);
    const rows = [...orderInfo.values()].sort((a, b) => (a.id < b.id ? -1 : 1));
    let folded = false;
    for (const info of rows) {
      const slot = slotClass(info.id);
      if (slot === 'gsn') folded = true;
      const lateText = info.late ? `, verspätet um ${fmt.duration(info.lateBy)}` : '';
      const due = info.due === null ? '' : `, Termin ${fmt.simTime(info.due)}`;
      chips.appendChild(h('button.gantt-chip', {
        type: 'button',
        dataset: { order: info.id },
        'aria-pressed': 'false',
        'data-tip': `${fmt.productName(info.product)}${due}${lateText}`,
        onclick: () => setSelected(data.selected === info.id ? null : info.id, { notify: true }),
      },
      s('svg.gantt-chip__sw', { viewBox: '0 0 10 10', 'aria-hidden': 'true' }, s(`rect.${slot}`, { x: 0.5, y: 0.5, width: 9, height: 9 })),
      fmt.orderId(info.id),
      info.late ? statusIcon('warn', { size: 9 }) : null));
    }
    foldedKey.hidden = !folded;
  }

  function renderTable() {
    clear(tableHost);
    const sorted = [...data.assignments].sort((a, b) => (rowIndex.get(a.machine) ?? 99) - (rowIndex.get(b.machine) ?? 99) || a.start - b.start);
    tableHost.appendChild(h('table.tbl',
      h('thead', h('tr', ['Maschine', 'Operation', 'Auftrag', 'Produkt', 'Start', 'Ende'].map((t) => h('th', t)), h('th.num', 'Rüsten'), h('th', 'Charge'), h('th', 'Status'))),
      h('tbody', sorted.map((a) => h('tr',
        h('td.mono', a.machine),
        h('td.mono', fmt.opId(a.op_id)),
        h('td.mono', fmt.orderId(a.order_id)),
        h('td', a.product_id ? fmt.productName(a.product_id) : fmt.DASH),
        h('td.mono', fmt.simTime(a.start)),
        h('td.mono', fmt.simTime(a.end)),
        h('td.num', a.setup_before ? fmt.duration(a.setup_before) : fmt.DASH),
        h('td.mono', a.batch_id ?? fmt.DASH),
        h('td', `${STATE_LABEL[a.state ?? 'pending']}${a.frozen ? ', fixiert' : ''}`))))));
  }

  // ------------------------------------------------------------------ hit testing and tooltip
  function localPoint(event) {
    const box = svg.getBoundingClientRect();
    return { px: event.clientX - box.left - size.gutter, py: event.clientY - box.top - size.axis };
  }

  function hitTest(px, py) {
    if (px < 0 || px > plotW || py < 0 || py >= plotH) return null;
    const row = rowsLayout.find((r) => py >= r.y && py < r.y + r.height);
    if (!row) return null;
    let best = null;
    let bestDistance = Infinity;
    for (const mark of marksByRow.get(row.id) ?? []) {
      if (!mark.visible) continue;
      const a = x(mark.start - mark.setup);
      const b = Math.max(x(mark.end), x(mark.start) + MIN_BAR_PX);
      const distance = px < a ? a - px : px > b ? px - b : 0;
      if (distance < bestDistance || (distance === 0 && bestDistance === 0)) {
        best = mark;
        bestDistance = distance;
      }
    }
    // A bar under a fault veil did not run as drawn, so the veil answers first.
    const fault = downs.find((d) => d.isFault && d.row === row && px >= x(d.start) && px <= x(d.end));
    if (fault) return { down: fault };
    if (best && bestDistance <= HIT_TOLERANCE_PX) {
      const inner = py - best.y;
      const band = best.ops.length > 1 ? Math.max(0, Math.min(best.ops.length - 1, Math.floor(inner / (best.bandH + 1)))) : 0;
      return { mark: best, op: best.ops[band] };
    }
    const down = downs.find((d) => d.row === row && px >= x(d.start) && px <= x(d.end));
    return down ? { down } : null;
  }

  function hitKey(hit) {
    if (!hit) return '';
    return hit.down ? `d:${hit.down.machine}:${hit.down.start}` : `o:${hit.op.op_id}`;
  }

  function tipRows(hit) {
    if (hit.down) {
      const d = hit.down;
      return {
        title: `${downLabel(d.reason, d.isFault).long} · ${d.machine}`,
        rows: [['Maschine', rowLabel(d.machine)], ['Beginn', fmt.simTime(d.start)], [d.live ? 'Ende, geschätzt' : 'Ende', fmt.simTime(d.end)], ['Dauer', fmt.duration(d.end - d.start)]],
      };
    }
    const { op, mark } = hit;
    const rows = [];
    const info = op.order_id ? orderInfo.get(op.order_id) : null;
    rows.push(['Operation', fmt.opId(op.op_id)]);
    if (op.lot_id) rows.push(['Los', fmt.lotId(op.lot_id)]);
    if (op.order_id) rows.push(['Auftrag', fmt.orderId(op.order_id)]);
    if (op.product_id) rows.push(['Produkt', fmt.productName(op.product_id)]);
    rows.push(['Maschine', rowLabel(op.machine)]);
    rows.push(['Start', fmt.simTime(op.start)]);
    rows.push(['Ende', fmt.simTime(op.end)]);
    rows.push(['Dauer', fmt.duration(op.end - op.start)]);
    const setupMin = op.setup_before || (mark.ops.length > 1 ? mark.setup : 0);
    rows.push(['Rüsten', setupMin ? `${fmt.duration(setupMin)} davor` : 'keines']);
    if (op.batch_id) rows.push(['Charge', mark.ops.length > 1 ? `${fmt.lotId(op.batch_id)}, ${fmt.plural(mark.ops.length, 'Los', 'Lose')}` : fmt.lotId(op.batch_id)]);
    rows.push(['Status', `${STATE_LABEL[op.state ?? 'pending']}${op.frozen ? ', fixiert' : ''}`]);
    if (info?.due !== null && info?.due !== undefined) rows.push(['Termin', fmt.simTime(info.due)]);
    const warn = info?.late ? `Auftrag verspätet um ${fmt.duration(info.lateBy)}` : null;
    const title = op.kind === 'cast' ? `Schmelze ${fmt.lotId(op.op_id.split(':')[0])}` : `${fmt.opKindLabel(op.kind)} · ${fmt.lotId(op.lot_id ?? op.op_id)}`;
    return { title, rows, warn };
  }

  function fillTip(hit) {
    const key2 = hitKey(hit);
    if (key2 === tipKey) return;
    tipKey = key2;
    const content = tipRows(hit);
    clear(tip);
    tip.append(
      h('div.gantt-tip__title', content.title),
      h('dl.gantt-tip__rows', content.rows.flatMap(([label, value]) => [h('dt', label), h('dd', value)])));
    if (content.warn) tip.appendChild(h('div.gantt-tip__warn', content.warn));
  }

  function placeTip(clientX, clientY) {
    tip.hidden = false;
    const own = tip.getBoundingClientRect();
    let left = clientX + 14;
    let top = clientY + 16;
    if (left + own.width > window.innerWidth - 8) left = clientX - own.width - 12;
    if (top + own.height > window.innerHeight - 8) top = clientY - own.height - 12;
    tip.style.left = `${Math.max(8, Math.round(left))}px`;
    tip.style.top = `${Math.max(8, Math.round(top))}px`;
  }

  function hideTip() {
    tip.hidden = true;
    tipKey = '';
  }

  function setHover(hit) {
    const mark = hit?.mark ?? null;
    if (mark === hover) return;
    hover?.g.classList.remove('is-hover');
    hover = mark;
    hover?.g.classList.add('is-hover');
  }

  // ------------------------------------------------------------------ interaction
  function notifyView() {
    data.onViewChange?.(view.slice(), { fit: isFit });
  }

  function setViewInternal(next, { fitted = false } = {}) {
    view = clampView(next);
    const [lo, hi] = fullDomain();
    isFit = fitted || (Math.abs(view[0] - lo) < 0.5 && Math.abs(view[1] - hi) < 0.5);
    requestLayout();
  }

  function zoomAt(factor, px) {
    const pivot = timeAt(px);
    const span = (view[1] - view[0]) * factor;
    const ratio = px / plotW;
    setViewInternal([pivot - span * ratio, pivot + span * (1 - ratio)]);
    notifyView();
  }

  function fit() {
    setViewInternal(fullDomain(), { fitted: true });
  }

  function onWheel(event) {
    const { px } = localPoint(event);
    if (px < 0) return;
    const unit = event.deltaMode === 1 ? 16 : event.deltaMode === 2 ? 300 : 1;
    const dx = event.deltaX * unit;
    const dy = event.deltaY * unit;
    // Zoomed all the way out there is nothing left to zoom: the wheel scrolls the page instead of trapping it.
    if (isFit && !event.shiftKey && Math.abs(dy) >= Math.abs(dx) && dy > 0) return;
    event.preventDefault();
    if (event.shiftKey || Math.abs(dx) > Math.abs(dy)) {
      const shift = ((event.shiftKey && !dx ? dy : dx) / plotW) * (view[1] - view[0]);
      setViewInternal([view[0] + shift, view[1] + shift]);
      notifyView();
    } else if (dy) {
      zoomAt(Math.exp(Math.max(-1, Math.min(1, dy * 0.0016))), Math.min(plotW, px));
    }
  }

  function onPointerDown(event) {
    if (event.button !== 0) return;
    const { px } = localPoint(event);
    if (px < 0) return;
    drag = { startX: event.clientX, startView: view.slice(), moved: false, pointer: event.pointerId };
    svg.setPointerCapture(event.pointerId);
  }

  function onPointerMove(event) {
    if (drag && event.pointerId === drag.pointer) {
      const dx = event.clientX - drag.startX;
      if (!drag.moved && Math.abs(dx) < 3) return;
      if (!drag.moved) {
        drag.moved = true;
        svg.classList.add('is-grabbing');
        hideTip();
        setHover(null);
      }
      const shift = (-dx / plotW) * (drag.startView[1] - drag.startView[0]);
      setViewInternal([drag.startView[0] + shift, drag.startView[1] + shift]);
      notifyView();
      return;
    }
    const { px, py } = localPoint(event);
    const hit = hitTest(px, py);
    setHover(hit);
    svg.style.cursor = hit?.mark ? 'pointer' : px >= 0 ? 'grab' : 'default';
    if (hit) {
      fillTip(hit);
      placeTip(event.clientX, event.clientY);
    } else {
      hideTip();
    }
  }

  function onPointerUp(event) {
    if (!drag || event.pointerId !== drag.pointer) return;
    const { moved } = drag;
    drag = null;
    svg.classList.remove('is-grabbing');
    if (moved) return;
    const { px, py } = localPoint(event);
    const hit = hitTest(px, py);
    const order = hit?.op?.order_id ?? null;
    setSelected(order && data.selected === order ? null : order, { notify: true });
  }

  function onPointerLeave() {
    if (drag) return;
    setHover(null);
    hideTip();
  }

  // Keyboard path to the same details as hover: arrows walk the marks, Enter marks the order.
  function drawCursor() {
    if (!cursor?.visible) {
      cursorRect.setAttribute('visibility', 'hidden');
      return;
    }
    const a = x(cursor.start) - 2;
    const b = Math.max(x(cursor.end), x(cursor.start) + MIN_BAR_PX) + 2;
    cursorRect.setAttribute('x', a.toFixed(1));
    cursorRect.setAttribute('y', cursor.y - 2);
    cursorRect.setAttribute('width', (b - a).toFixed(1));
    cursorRect.setAttribute('height', cursor.h + 4);
    cursorRect.setAttribute('visibility', 'visible');
  }

  function focusMark(mark) {
    if (!mark) return;
    cursor = mark;
    if (x(mark.end) < 0 || x(mark.start) > plotW) {
      const span = view[1] - view[0];
      setViewInternal([mark.start - span * 0.3, mark.start + span * 0.7]);
      notifyView();
      layout();
    }
    drawCursor();
    const hit = { mark, op: mark.ops[0] };
    fillTip(hit);
    const box = svg.getBoundingClientRect();
    placeTip(box.left + size.gutter + x(mark.end), box.top + size.axis + mark.y + mark.h);
    const content = tipRows(hit);
    live.textContent = `${content.title}: ${content.rows.map(([a, b]) => `${a} ${b}`).join(', ')}`;
  }

  function onKeyDown(event) {
    const current = cursor;
    const neighbour = (rowDelta) => {
      if (!current) return null;
      const from = rowIndex.get(current.row);
      for (let i = from + rowDelta; i >= 0 && i < rowsLayout.length; i += rowDelta) {
        const list = marksByRow.get(rowsLayout[i].id) ?? [];
        if (!list.length) continue;
        return list.reduce((best, m) => (Math.abs(m.start - current.start) < Math.abs(best.start - current.start) ? m : best));
      }
      return null;
    };
    const step = (delta) => {
      if (!current) return null;
      const list = marksByRow.get(current.row) ?? [];
      return list[list.indexOf(current) + delta] ?? null;
    };
    let target = null;
    switch (event.key) {
      case 'ArrowRight': target = current ? step(1) : firstMark(); break;
      case 'ArrowLeft': target = current ? step(-1) : firstMark(); break;
      case 'ArrowDown': target = current ? neighbour(1) : firstMark(); break;
      case 'ArrowUp': target = current ? neighbour(-1) : firstMark(); break;
      case 'Enter':
      case ' ':
        if (current) {
          const order = current.ops[0].order_id ?? null;
          setSelected(data.selected === order ? null : order, { notify: true });
          event.preventDefault();
        }
        return;
      case 'Escape':
        cursor = null;
        cursorRect.setAttribute('visibility', 'hidden');
        hideTip();
        setSelected(null, { notify: Boolean(data.selected) });
        return;
      case '+':
      case '=':
        zoomAt(1 / 1.5, plotW / 2);
        event.preventDefault();
        return;
      case '-':
        zoomAt(1.5, plotW / 2);
        event.preventDefault();
        return;
      case '0':
        fit();
        notifyView();
        event.preventDefault();
        return;
      default:
        return;
    }
    event.preventDefault();
    if (target) focusMark(target);
  }

  function firstMark() {
    const now = data.now ?? -Infinity;
    return marks.filter((m) => m.end >= now).sort((a, b) => a.start - b.start)[0] ?? marks[0] ?? null;
  }

  svg.addEventListener('wheel', onWheel, { passive: false });
  svg.addEventListener('pointerdown', onPointerDown);
  svg.addEventListener('pointermove', onPointerMove);
  svg.addEventListener('pointerup', onPointerUp);
  svg.addEventListener('pointercancel', onPointerUp);
  svg.addEventListener('pointerleave', onPointerLeave);
  svg.addEventListener('keydown', onKeyDown);
  svg.addEventListener('blur', () => {
    cursor = null;
    cursorRect.setAttribute('visibility', 'hidden');
    if (!hover) hideTip();
  });
  svg.addEventListener('dblclick', (event) => {
    if (localPoint(event).px < 0) return;
    fit();
    notifyView();
  });

  const hideOnScroll = () => hideTip();
  window.addEventListener('scroll', hideOnScroll, true);

  const observer = new ResizeObserver((entries) => {
    const next = Math.round(entries[entries.length - 1].contentRect.width);
    if (next === width || next <= 0) return;
    width = next;
    layout();
  });
  observer.observe(frame);
  width = Math.round(frame.getBoundingClientRect().width);
  rebuild();

  const structural = ['assignments', 'downtimes', 'faults', 'orders', 'rows', 'rowLabels', 'domain', 'orderCompletion'];

  return {
    element: root,

    /** Merges `next` into the chart's data. Only `now` or `selected` changing is cheap; new assignments rebuild the marks and keep view and selection. */
    update(next = {}) {
      if (destroyed) return;
      const needsRebuild = structural.some((name) => name in next && next[name] !== data[name]);
      const selectionChanged = 'selected' in next && next.selected !== data.selected;
      Object.assign(data, next);
      if (needsRebuild) {
        rebuild();
        return;
      }
      if (selectionChanged) applySelection();
      if ('now' in next) {
        if (isFit) {
          const [lo, hi] = fullDomain();
          if (Math.abs(lo - view[0]) > 0.5 || Math.abs(hi - view[1]) > 0.5) {
            view = [lo, hi];
            layout();
            return;
          }
        }
        const scale = plotW / (view[1] - view[0]);
        for (const mark of marks) {
          if (mark.remaining && mark.visible) updateProgress(mark, Math.max(MIN_BAR_PX, (mark.end - mark.start) * scale - 1));
        }
        layoutMarkers();
      }
    },

    setSelected(id) {
      setSelected(id);
    },

    /** Shows `[t0, t1]`. Never calls onViewChange, so two charts can mirror each other without a loop. */
    setView(next, { fit: fitted = false } = {}) {
      setViewInternal(next, { fitted });
    },

    getView() {
      return view.slice();
    },

    fit,

    /** Shows or hides the table of all assignments, the same data without hover. Returns whether it is open now. */
    toggleTable,

    destroy() {
      destroyed = true;
      cancelAnimationFrame(raf);
      observer.disconnect();
      window.removeEventListener('scroll', hideOnScroll, true);
      liveCharts.delete(chartRef);
      legend?.remove();
      key.remove();
      root.remove();
    },
  };
}
