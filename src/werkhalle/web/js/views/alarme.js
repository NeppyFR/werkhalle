// Alarme: active alarms, history, and the event log, each kept live over the WebSocket.
import { h, setText, clear, loadCss } from '../core/dom.js';
import { panel, empty, statusIcon } from '../components/ui.js';
import { poll } from './_poll.js';

const HISTORY_ROWS = 300;
const LOG_ROWS = 200;
const FLASH_MS = 1600;
const CATCH_UP_MS = 2000;

const SEVERITIES = [
  { id: 'all', label: 'Alle' },
  { id: 'fault', label: 'Störung (900)' },
  { id: 'warn', label: 'Warnung (500)' },
];
const LOG_LEVELS = [
  { id: 'all', label: 'Alle' },
  { id: 'error', label: 'Fehler' },
  { id: 'warn', label: 'Warnung' },
  { id: 'info', label: 'Info' },
];
const LOG_SOURCES = ['all', 'opcua', 'mes', 'plan', 'twin'];
const LEVEL_LABEL = { error: 'Fehler', warn: 'Warnung', info: 'Info' };

function isFault(row) {
  return (row.severity ?? 0) >= 900;
}

function isActive(row) {
  if (row.cleared_t !== null && row.cleared_t !== undefined) return false;
  return row.active !== false;
}

function select(label, options, value, onChange) {
  const node = h('select.input.alarme-select', { 'aria-label': label, onchange: (event) => onChange(event.target.value) },
    options.map((option) => h('option', { value: option.id, selected: option.id === value }, option.label)));
  return { node, field: h('label.alarme-field', h('span.field__label', label), node) };
}

export function mount(el, ctx) {
  loadCss('css/views/alarme.css');
  const { api, fmt, store, router, toast } = ctx;
  el.classList.add('alarme');

  let alive = true;
  const rows = new Map();
  const pending = new Set();
  const flashes = new Map();
  let machineFilter = router.params.machine ?? 'all';
  let severityFilter = 'all';
  let logLevel = 'all';
  let logSource = 'all';
  let renderedKey = '';
  let loaded = false;

  // --- filters ---
  const machineOptions = () => [{ id: 'all', label: 'Alle' }, ...(store.catalog?.machines ?? store.state?.machines ?? []).map((m) => ({ id: m.id, label: `${m.id} · ${m.name}` }))];
  const machineSelect = select('Maschine', machineOptions(), machineFilter, (value) => {
    machineFilter = value;
    renderAlarms(true);
  });
  const severitySelect = select('Schwere', SEVERITIES, severityFilter, (value) => {
    severityFilter = value;
    renderAlarms(true);
  });
  const counts = h('span.alarme-counts');
  const toolbar = h('div.toolbar.alarme-toolbar', machineSelect.field, severitySelect.field, counts);

  function fillMachines() {
    const options = machineOptions();
    if (machineSelect.node.options.length === options.length) return;
    clear(machineSelect.node);
    machineSelect.node.append(...options.map((option) => h('option', { value: option.id, selected: option.id === machineFilter }, option.label)));
  }

  // --- active and history share their columns ---
  const COLUMNS = ['Zeit', 'Maschine', 'Code', 'Meldung', 'Schwere', 'Dauer', 'Latenz', 'Quittiert'];
  const headRow = () => h('thead', h('tr', COLUMNS.map((name, i) => h(i === 5 || i === 6 ? 'th.num' : 'th', {
    'data-tip': name === 'Latenz' ? 'Zeit vom Quellzeitstempel der Anlage bis zum Eingang des DataChange im MES' : null,
  }, name))));

  const activeBody = h('tbody');
  const activeCount = h('span.tag.tag--mono', '0');
  const activeBox = panel('Aktive Alarme', { dense: true, actions: [activeCount] });
  const activeEmpty = empty('Keine aktiven Alarme.');
  const activeTable = h('table.tbl.alarme-table', headRow(), activeBody);
  activeBox.body.append(activeTable, activeEmpty);
  activeBox.node.classList.add('alarme-active');

  const historyBody = h('tbody');
  const historyCount = h('span.tag.tag--mono', '0');
  const historyBox = panel('Verlauf', { dense: true, actions: [historyCount] });
  const historyEmpty = empty('Noch keine beendeten Alarme.');
  const historyTable = h('table.tbl.alarme-table', headRow(), historyBody);
  historyBox.body.append(historyTable, historyEmpty);
  historyBox.node.classList.add('alarme-history');

  const durationCells = new Map();

  async function ack(row, button) {
    pending.add(row.id);
    button.disabled = true;
    try {
      await api.post(`/api/alarms/${encodeURIComponent(row.id)}/ack`, {});
      if (!alive) return;
      const held = rows.get(row.id);
      if (held) held.acked = true;
      toast(`Alarm ${row.code} an ${row.machine} quittiert.`, { kind: 'ok' });
      renderAlarms(true);
    } catch {
      if (alive) button.disabled = false;
    } finally {
      pending.delete(row.id);
    }
  }

  // The plant accepts an ack only for the machine's current AlarmId, which outlives the clear until the next alarm replaces it.
  function isCurrentAlarm(row) {
    for (const other of rows.values()) {
      if (other.machine === row.machine && (other.alarm_id ?? 0) > (row.alarm_id ?? 0)) return false;
    }
    return true;
  }

  function ackCell(row) {
    if (row.acked) return h('td.alarme-acked', statusIcon('ok', { size: 11 }), 'Ja');
    if (!isCurrentAlarm(row)) return h('td.faint', { 'data-tip': 'Nicht quittiert. Ein neuerer Alarm dieser Maschine hat ihn abgelöst, die Anlage nimmt nur für den aktuellen Alarm eine Quittierung an.' }, 'Nein');
    const button = h('button.btn.btn--sm', {
      type: 'button',
      disabled: pending.has(row.id),
      'aria-label': `Alarm ${row.code} an ${row.machine} quittieren`,
      onclick: () => ack(row, button),
    }, 'Quittieren');
    return h('td', button);
  }

  function duration(row) {
    const end = isActive(row) ? store.clock.now() : row.cleared_t;
    if (typeof end !== 'number' || typeof row.t !== 'number') return fmt.DASH;
    return fmt.duration(Math.max(0, end - row.t));
  }

  function alarmRow(row) {
    const fault = isFault(row);
    const live = h('td.num', duration(row));
    if (isActive(row)) durationCells.set(row.id, live);
    const latency = row.latency_ms === null || row.latency_ms === undefined
      ? h('td.num', { 'data-tip': 'Beim (Wieder-)Verbinden schon vorhanden, daher keine Messung' }, fmt.DASH)
      : h('td.num', { 'data-tip': row.source_ts ? `Quellzeitstempel ${fmt.wallTime(row.source_ts, { ms: true })}, erkannt ${fmt.wallTime(row.detected_wall_ts, { ms: true })}` : null }, `${fmt.number(row.latency_ms)} ms`);
    const classes = ['alarme-row'];
    if (fault && isActive(row)) classes.push(row.acked ? 'alarme-row--fault-acked' : 'alarme-row--fault');
    if (flashes.has(row.id)) classes.push('is-new');
    return h('tr', { class: classes.join(' ') },
      h('td.mono.nowrap', { 'data-tip': row.source_ts ? `Wanduhr ${fmt.wallTime(row.source_ts, { ms: true })}` : null }, fmt.simTime(row.t)),
      h('td.mono', { 'data-tip': fmt.machineName(row.machine) }, row.machine),
      h('td.mono', row.code),
      h('td.alarme-text', row.text),
      h('td.alarme-severity', statusIcon(fault ? 'FAULT' : 'warn', { size: 11 }), fault ? 'Störung' : 'Warnung', h('span.faint.mono', ` ${row.severity ?? ''}`)),
      live,
      latency,
      ackCell(row));
  }

  function visible(row) {
    if (machineFilter !== 'all' && row.machine !== machineFilter) return false;
    if (severityFilter === 'fault' && !isFault(row)) return false;
    if (severityFilter === 'warn' && isFault(row)) return false;
    return true;
  }

  function newestFirst(a, b) {
    return (b.t ?? 0) - (a.t ?? 0) || String(b.id).localeCompare(String(a.id));
  }

  function renderAlarms(force = false) {
    const all = [...rows.values()];
    const active = all.filter(isActive).sort(newestFirst);
    const past = all.filter((row) => !isActive(row)).sort(newestFirst);
    const shownActive = active.filter(visible);
    const shownPast = past.filter(visible).slice(0, HISTORY_ROWS);
    const key = [...shownActive, ...shownPast].map((r) => `${r.id}:${r.acked}:${isActive(r)}:${flashes.has(r.id)}`).join('|');
    setText(activeCount, shownActive.length === active.length ? active.length : `${shownActive.length}/${active.length}`);
    setText(historyCount, shownPast.length === past.length ? past.length : `${shownPast.length}/${past.length}`);
    const filtered = machineFilter !== 'all' || severityFilter !== 'all';
    setText(counts, filtered ? `${fmt.number(shownActive.length + shownPast.length)} von ${fmt.number(all.length)} Alarmen` : `${fmt.number(all.length)} Alarme in diesem Lauf`);
    activeTable.hidden = !shownActive.length;
    activeEmpty.hidden = Boolean(shownActive.length);
    setText(activeEmpty, filtered && active.length ? 'Keine aktiven Alarme für diesen Filter.' : 'Keine aktiven Alarme.');
    historyTable.hidden = !shownPast.length;
    historyEmpty.hidden = Boolean(shownPast.length);
    setText(historyEmpty, filtered && past.length ? 'Keine beendeten Alarme für diesen Filter.' : 'Noch keine beendeten Alarme.');
    if (!force && key === renderedKey) return;
    renderedKey = key;
    durationCells.clear();
    clear(activeBody);
    clear(historyBody);
    activeBody.append(...shownActive.map(alarmRow));
    historyBody.append(...shownPast.map(alarmRow));
  }

  function upsert(row, { fresh = false } = {}) {
    const held = rows.get(row.id);
    if (!held && fresh && loaded) {
      flashes.set(row.id, setTimeout(() => {
        flashes.delete(row.id);
        if (alive) renderAlarms();
      }, FLASH_MS));
    }
    // An ack we already saw must not be undone by an older copy of the row.
    rows.set(row.id, { ...held, ...row, acked: Boolean(held?.acked || row.acked) });
  }

  async function fetchAll() {
    const data = await api.get('/api/alarms?limit=500', { quiet: true });
    if (!alive) return;
    for (const row of data.alarms ?? []) upsert(row);
    loaded = true;
    renderAlarms();
  }

  // --- event log ---
  const logList = h('tbody');
  const logCount = h('span.tag.tag--mono', '0');
  const levelSelect = select('Stufe', LOG_LEVELS, logLevel, (value) => {
    logLevel = value;
    renderLog();
  });
  const sourceSelect = select('Quelle', LOG_SOURCES.map((id) => ({ id, label: id === 'all' ? 'Alle' : fmt.logSourceLabel(id) })), logSource, (value) => {
    logSource = value;
    renderLog();
  });
  const logBox = panel('Ereignisprotokoll', { dense: true, actions: [levelSelect.field, sourceSelect.field, logCount] });
  const logEmpty = empty('Keine Einträge für diesen Filter.');
  const logTable = h('table.tbl.alarme-log',
    h('thead', h('tr', h('th', 'Simulationszeit'), h('th', 'Uhrzeit'), h('th', 'Stufe'), h('th', 'Quelle'), h('th', 'Ereignis'))),
    logList);
  logBox.body.append(logTable, logEmpty);
  logBox.node.classList.add('alarme-logbox');

  function logMatches(entry) {
    if (logLevel !== 'all' && (entry.level ?? 'info') !== logLevel) return false;
    if (logSource !== 'all' && entry.source !== logSource) return false;
    return true;
  }

  function logRow(entry) {
    const level = entry.level ?? 'info';
    return h(`tr.alarme-log__row--${level}`,
      h('td.mono.nowrap', fmt.simTime(entry.t)),
      h('td.mono.nowrap.faint', fmt.wallTime(entry.wall_ts)),
      h('td.alarme-log__level', level === 'error' ? statusIcon('FAULT', { size: 10 }) : level === 'warn' ? statusIcon('warn', { size: 10 }) : statusIcon('info', { size: 10 }), LEVEL_LABEL[level] ?? level),
      h('td.nowrap', fmt.logSourceLabel(entry.source)),
      h('td', entry.text));
  }

  function renderLog() {
    const shown = store.log.filter(logMatches).slice(0, LOG_ROWS);
    clear(logList);
    logList.append(...shown.map(logRow));
    setText(logCount, shown.length);
    logTable.hidden = !shown.length;
    logEmpty.hidden = Boolean(shown.length);
  }

  el.append(toolbar, activeBox.node, historyBox.node, logBox.node);
  renderAlarms();
  renderLog();

  const refresh = poll(fetchAll, 15000);

  const offAlarm = store.on('alarm', (row) => {
    upsert(row, { fresh: true });
    renderAlarms();
  });
  const offLog = store.on('log', (entry) => {
    if (!logMatches(entry)) return;
    logList.insertBefore(logRow(entry), logList.firstChild);
    while (logList.children.length > LOG_ROWS) logList.lastChild.remove();
    setText(logCount, logList.children.length);
    logTable.hidden = false;
    logEmpty.hidden = true;
  });
  const offReset = store.on('log-reset', renderLog);
  const offRun = store.on('run-change', () => {
    rows.clear();
    refresh.now();
  });
  const offCatalog = store.on('catalog', fillMachines);

  // The state carries the active ones four times a second. That covers a lost message, and the durations run on with the clock.
  let lastTick = 0;
  let lastCatchUp = 0;
  const offState = store.subscribe((state) => {
    fillMachines();
    const activeNow = state.alarms_active ?? [];
    let missed = false;
    for (const row of activeNow) {
      if (!rows.has(row.id)) missed = true;
      upsert({ ...row, active: true }, { fresh: true });
    }
    // A row we hold as active that the state no longer lists has ended, and the row with its end time is one fetch away.
    const ids = new Set(activeNow.map((r) => r.id));
    for (const row of rows.values()) {
      if (isActive(row) && !ids.has(row.id)) missed = true;
    }
    const now = performance.now();
    if (missed && now - lastCatchUp > CATCH_UP_MS) {
      lastCatchUp = now;
      refresh.now();
    }
    renderAlarms();
    if (now - lastTick > 1000) {
      lastTick = now;
      for (const [id, cell] of durationCells) {
        const row = rows.get(id);
        if (row) setText(cell, duration(row));
      }
    }
  });

  return () => {
    alive = false;
    refresh.stop();
    for (const timer of flashes.values()) clearTimeout(timer);
    offAlarm();
    offLog();
    offReset();
    offRun();
    offCatalog();
    offState();
  };
}
