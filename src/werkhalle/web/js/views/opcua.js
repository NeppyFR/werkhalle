// OPC UA: the proof that the MES really sees the plant through OPC UA. Live node tree, DataChange feed with latency, method calls.
// Everything here shows what the MES's client received. Status comes from the node values, not from /api/state, on purpose.
import { h, setText, clear, loadCss } from '../core/dom.js';
import { panel, empty, statusIcon, paintIcon, sparkline, icon } from '../components/ui.js';
import { poll } from './_poll.js';

const FEED_KEEP = 6000;
const STATUS_KEEP = 800;
const FEED_ROWS = 120;
const RATE_POINTS = 60;
const PLANT = 'Plant';
const REDUCED_MOTION = window.matchMedia?.('(prefers-reduced-motion: reduce)');

function machineOfPath(path) {
  return /\/Machines\/([^/]+)\//.exec(path ?? '')?.[1] ?? null;
}

function nameOfPath(path) {
  return (path ?? '').split('/').pop() ?? '';
}

function shortPath(path) {
  return (path ?? '').replace(/^Werkhalle\/(Machines\/)?/, '').replaceAll('/', '.');
}

function showValue(value, type, fmt) {
  if (value === null || value === undefined) return fmt.DASH;
  if (typeof value === 'boolean') return value ? 'true' : 'false';
  if (typeof value === 'number') {
    if (type === 'Double') return fmt.number(value, Number.isInteger(value) ? 1 : Math.abs(value) < 10 ? 3 : 1);
    return fmt.number(value, Number.isInteger(value) ? 0 : 1);
  }
  return value === '' ? '(leer)' : String(value);
}

// Every command starts with run_id and machine. The row already names the machine, and what matters comes after them.
function compactArgs(text) {
  try {
    const { run_id: _run, machine: _machine, ...rest } = JSON.parse(text);
    return JSON.stringify(rest);
  } catch {
    return text;
  }
}

function quantile(sorted, q) {
  if (!sorted.length) return null;
  return sorted[Math.min(sorted.length - 1, Math.floor(q * sorted.length))];
}

export function mount(el, ctx) {
  loadCss('css/views/opcua.css');
  const { api, fmt, store, router } = ctx;
  el.classList.add('opcua');

  let alive = true;
  let machineFilter = router.params.machine ?? 'all';
  let nodeFilter = (router.params.node ?? '').toLowerCase();
  let statusOnly = router.params.status === '1';
  let feedPaused = false;

  // --- band: the facts that prove the link ---
  const endpoint = h('dd.mono', fmt.DASH);
  const namespace = h('dd.mono', fmt.DASH);
  const linkIcon = statusIcon('info');
  const linkLabel = h('span');
  const rateValue = h('span.opcua-band__big');
  const rateSpark = sparkline({ width: 120, height: 22, label: 'DataChanges pro Sekunde, letzte Minute', minSpan: 10 });
  const totalValue = h('dd.mono', fmt.DASH);
  const latencyValue = h('dd.mono', fmt.DASH);
  const moduleValue = h('dd.mono', fmt.DASH);
  const snapshotValue = h('dd.mono', fmt.DASH);
  const runValue = h('dd.mono', fmt.DASH);
  const band = h('section.opcua-band', { 'aria-label': 'OPC-UA-Verbindung' },
    h('dl.opcua-band__cell.opcua-band__cell--wide', h('dt', 'Endpunkt'), endpoint, h('dt', 'Namensraum'), namespace),
    h('dl.opcua-band__cell', h('dt', 'Verbindung'), h('dd.opcua-band__link', linkIcon, linkLabel), h('dt', 'Lauf'), runValue),
    h('div.opcua-band__cell.opcua-band__cell--rate', h('span.opcua-band__label', 'DataChanges pro Sekunde'), h('div.opcua-band__rate', rateValue, h('div.opcua-band__spark', rateSpark.node))),
    h('dl.opcua-band__cell', h('dt', 'DataChanges gesamt'), totalValue, h('dt', { 'data-tip': 'Quellzeitstempel bis Eingang im MES, aus den letzten empfangenen Änderungen' }, 'Latenz p50 / p95'), latencyValue),
    h('dl.opcua-band__cell', { 'data-tip': 'Aus /api/health: das MES hat den Anlagencode nicht geladen, und GetStateSnapshot dient nur Fork und Planung' },
      h('dt', 'Anlagencode im MES geladen'), moduleValue, h('dt', 'GetStateSnapshot-Aufrufe'), snapshotValue));
  const claim = h('p.opcua-claim',
    'Alle Werte unten kamen als DataChange vom Anlagenprozess. Eine Störung erscheint erst, wenn ',
    h('code', 'Status'), ' per DataChange auf ', h('code', 'FAULT'), ' geht.');

  // --- filter row, scopes tree, feed and methods ---
  const machineSelect = h('select.input.opcua-select', { 'aria-label': 'Maschine', onchange: (event) => { machineFilter = event.target.value; applyFilters(); } });
  const nodeInput = h('input.input.opcua-node-input', {
    type: 'search',
    placeholder: 'z. B. Status, Temperature',
    value: nodeFilter,
    'aria-label': 'Knoten filtern',
    spellcheck: false,
    oninput: (event) => { nodeFilter = event.target.value.trim().toLowerCase(); applyFilters(); },
  });
  const statusSwitch = h('button.switch', {
    type: 'button',
    role: 'switch',
    'aria-checked': String(statusOnly),
    onclick: () => {
      statusOnly = !statusOnly;
      statusSwitch.setAttribute('aria-checked', String(statusOnly));
      applyFilters();
    },
  }, h('span.switch__track', h('span.switch__thumb')), 'Nur Status');
  const toolbar = h('div.toolbar.opcua-toolbar',
    h('label.opcua-field', h('span.field__label', 'Maschine'), machineSelect),
    h('label.opcua-field', h('span.field__label', 'Knoten'), nodeInput),
    statusSwitch);

  // --- faults: findable at once, whatever the filters say ---
  const alertRow = h('div.opcua-alerts', { role: 'status', hidden: true });

  // --- tree ---
  const treeCount = h('span.tag.tag--mono', '0');
  const treeBox = panel('Adressraum, live', { dense: true, actions: [treeCount] });
  treeBox.node.classList.add('opcua-tree');
  const table = h('table.tbl.opcua-table',
    h('thead', h('tr', h('th', 'Knoten'), h('th', 'Node-ID'), h('th', 'Typ'), h('th', 'Wert'), h('th', 'Quellzeit'), h('th.num', 'Änd.'))));
  const treeEmpty = empty('Kein Knoten passt zum Filter.');
  treeEmpty.hidden = true;
  treeBox.body.append(table, treeEmpty);

  const groups = new Map();
  const nodeRows = new Map();
  const open = new Set([PLANT]);

  function groupRow(id) {
    const chevron = icon('chevron', 12);
    const statusNode = statusIcon('IDLE', { size: 11 });
    const statusText = h('span.opcua-group__status');
    const count = h('span');
    const nodesCount = h('span.faint.opcua-group__nodes');
    const toggle = h('button.opcua-group__toggle', {
      type: 'button',
      'aria-expanded': String(open.has(id)),
      onclick: () => {
        if (open.has(id)) open.delete(id);
        else open.add(id);
        applyFilters();
      },
    }, chevron, h('span.opcua-group__id', id), id === PLANT ? h('span.faint', 'Werkhalle/Plant') : h('span.faint', fmt.machineName(id)), nodesCount);
    const head = h('tr.opcua-group',
      h('td', { colSpan: 3 }, toggle),
      h('td.opcua-group__value', id === PLANT ? null : [statusNode, statusText]),
      h('td'),
      h('td.num', count));
    statusNode.style.display = id === PLANT ? 'none' : '';
    const tbody = h('tbody', head);
    table.appendChild(tbody);
    const group = { id, tbody, head, toggle, chevron, count, nodesCount, statusNode, statusText, rows: [], status: null };
    groups.set(id, group);
    return group;
  }

  function nodeRow(node) {
    const groupId = node.machine ?? PLANT;
    const group = groups.get(groupId) ?? groupRow(groupId);
    const value = h('td.opcua-value.mono');
    const ts = h('td.mono.nowrap.opcua-ts');
    const changes = h('td.num');
    const tr = h('tr.opcua-node', { dataset: { name: node.name } },
      h('td.opcua-node__name', h('span.opcua-node__prefix', `${groupId}.`), node.name),
      h('td.mono.opcua-node__id', { 'data-tip': node.node_id }, node.node_id),
      h('td.opcua-node__type', node.type),
      value, ts, changes);
    group.tbody.appendChild(tr);
    const row = { tr, value, ts, changes, node, groupId, group, shown: null };
    nodeRows.set(node.node_id, row);
    group.rows.push(row);
    return row;
  }

  function flash(cell) {
    if (REDUCED_MOTION?.matches) return;
    cell.animate([{ backgroundColor: 'var(--accent-dim)', color: 'var(--text-1)' }, { backgroundColor: 'transparent' }], { duration: 800, easing: 'ease-out' });
  }

  function matchesNode(name, id) {
    if (statusOnly && name !== 'Status') return false;
    if (nodeFilter && !(name.toLowerCase().includes(nodeFilter) || String(id).toLowerCase().includes(nodeFilter))) return false;
    return true;
  }

  function applyFilters() {
    const flat = statusOnly || Boolean(nodeFilter);
    table.classList.toggle('is-flat', flat);
    let visibleCount = 0;
    for (const group of groups.values()) {
      const groupVisible = machineFilter === 'all' || group.id === machineFilter;
      let matches = 0;
      for (const row of group.rows) {
        const on = groupVisible && matchesNode(row.node.name, row.node.node_id);
        row.tr.hidden = !on || (!flat && !open.has(group.id) && machineFilter === 'all');
        if (on) matches += 1;
      }
      group.head.hidden = flat || !groupVisible;
      group.tbody.hidden = !groupVisible || (flat && matches === 0);
      group.toggle.setAttribute('aria-expanded', String(open.has(group.id) || machineFilter === group.id));
      group.chevron.classList.toggle('opcua-group__chevron--closed', !(open.has(group.id) || machineFilter === group.id));
      setText(group.nodesCount, fmt.plural(group.rows.length, 'Knoten', 'Knoten'));
      visibleCount += matches;
    }
    treeEmpty.hidden = visibleCount > 0;
    setText(treeEmpty, groups.size ? 'Kein Knoten passt zum Filter.' : 'Das MES meldet noch keine Knoten.');
    setText(treeCount, visibleCount);
    sortFlat();
    renderFeed();
    renderMethods();
  }

  // In a flat list a faulted machine moves to the top, so "Nur Status" answers "what is broken" at a glance.
  let flatOrder = '';
  function sortFlat() {
    const flat = table.classList.contains('is-flat');
    const ordered = [...groups.values()].sort((a, b) => Number(flat && b.status === 'FAULT') - Number(flat && a.status === 'FAULT'));
    const key = ordered.map((g) => g.id).join();
    if (key === flatOrder) return;
    flatOrder = key;
    for (const group of ordered) table.appendChild(group.tbody);
  }

  function reveal(machine, nodeId) {
    open.add(machine);
    machineSelect.value = 'all';
    machineFilter = 'all';
    applyFilters();
    const row = nodeRows.get(nodeId);
    if (row) {
      row.tr.scrollIntoView({ block: 'center' });
      flash(row.tr);
    }
  }

  const alertTimes = new Map();
  function paintAlerts(faulted) {
    clear(alertRow);
    alertTimes.clear();
    alertRow.hidden = !faulted.length;
    for (const row of faulted) {
      const time = h('span.mono.opcua-alert__ts', fmt.wallTime(row.node.source_ts, { ms: true }));
      alertTimes.set(row, time);
      alertRow.appendChild(h('button.opcua-alert', {
        type: 'button',
        'data-tip': `${row.node.node_id}: Zeile zeigen`,
        onclick: () => reveal(row.groupId, row.node.node_id),
      },
      statusIcon('FAULT', { size: 12 }),
      h('span.mono', `${row.groupId}.Status = FAULT`),
      time,
      h('span.opcua-alert__go', 'zeigen')));
    }
  }

  let alertKey = '';
  function paintNodes(data) {
    setText(endpoint, data.endpoint ?? fmt.DASH);
    setText(namespace, data.namespace ?? fmt.DASH);
    const faulted = [];
    let added = false;
    for (const node of data.nodes ?? []) {
      let row = nodeRows.get(node.node_id);
      if (!row) {
        row = nodeRow(node);
        added = true;
      }
      const previous = row.node;
      row.node = node;
      const text = `${showValue(node.value, node.type, fmt)}${node.truncated ? ' …' : ''}`;
      if (row.shown !== text) {
        const first = row.shown === null;
        row.shown = text;
        setText(row.value, text);
        row.value.setAttribute('data-tip', typeof node.value === 'string' && node.value.length > 24 ? `${node.value}${node.truncated ? ' … (gekürzt)' : ''}` : '');
        if (!first && !row.tr.hidden) flash(row.value);
      }
      if (previous.source_ts !== node.source_ts) setText(row.ts, fmt.wallTime(node.source_ts, { ms: true }));
      else if (!row.ts.textContent) setText(row.ts, fmt.wallTime(node.source_ts, { ms: true }));
      setText(row.changes, fmt.number(node.changes));

      if (node.name === 'Status') {
        const group = row.group;
        const isFault = node.value === 'FAULT';
        row.tr.classList.toggle('is-fault', isFault);
        group.head.classList.toggle('is-fault', isFault);
        if (group.status !== node.value) {
          group.status = node.value;
          paintIcon(group.statusNode, node.value);
          group.statusText.className = `opcua-group__status tone-${fmt.statusTone(node.value)}`;
          flash(group.statusText);
        }
        setText(group.statusText, `${node.value} · ${fmt.statusLabel(node.value)}`);
        if (isFault) faulted.push(row);
      }
      if (node.name === 'AlarmActive') row.tr.classList.toggle('is-alarm', node.value === true);
    }
    const key = faulted.map((r) => r.node.node_id).join();
    if (key !== alertKey) {
      alertKey = key;
      paintAlerts(faulted);
      flatOrder = '';
      sortFlat();
    }
    for (const [row, time] of alertTimes) setText(time, fmt.wallTime(row.node.source_ts, { ms: true }));
    for (const group of groups.values()) {
      setText(group.count, fmt.number(group.rows.reduce((sum, r) => sum + (r.node.changes ?? 0), 0)));
    }
    const machines = [...groups.keys()].filter((id) => id !== PLANT);
    if (machineSelect.options.length !== machines.length + 1) {
      clear(machineSelect);
      machineSelect.append(
        h('option', { value: 'all', selected: machineFilter === 'all' }, 'Alle'),
        ...machines.map((id) => h('option', { value: id, selected: machineFilter === id }, `${id} · ${fmt.machineName(id)}`)),
      );
    }
    if (added) applyFilters();
  }

  // --- DataChange feed. The server's ring is short at this rate, so rare changes such as Status are kept here. ---
  const feed = [];
  const statusFeed = [];
  const seen = new Set();
  let unseenWhilePaused = 0;

  const pauseBtn = h('button.btn.btn--sm', {
    type: 'button',
    'aria-pressed': 'false',
    onclick: () => {
      feedPaused = !feedPaused;
      pauseBtn.setAttribute('aria-pressed', String(feedPaused));
      setText(pauseBtn, feedPaused ? 'Weiter' : 'Anhalten');
      if (!feedPaused) {
        unseenWhilePaused = 0;
        renderFeed();
      }
    },
  }, 'Anhalten');
  const feedCount = h('span.opcua-feed__count');
  const feedBox = panel('DataChanges, neueste zuerst', { dense: true, actions: [feedCount, pauseBtn] });
  feedBox.node.classList.add('opcua-feed');
  const feedBody = h('tbody');
  const feedTable = h('table.tbl.opcua-table',
    h('thead', h('tr', h('th', 'Empfangen'), h('th', 'Knoten'), h('th', 'Wert'), h('th', 'Quellzeit'), h('th.num', { 'data-tip': 'Quellzeitstempel der Anlage bis Eingang im MES' }, 'Latenz'))),
    feedBody);
  const feedEmpty = empty('Keine Änderung passt zum Filter.');
  feedBox.body.append(feedTable, feedEmpty);
  const feedRows = [];

  function feedRow() {
    const when = h('td.mono.nowrap');
    const node = h('td.mono.nowrap');
    const value = h('td.mono.opcua-value');
    const source = h('td.mono.nowrap.opcua-ts');
    const latency = h('td.num');
    const tr = h('tr', when, node, value, source, latency);
    feedBody.appendChild(tr);
    const row = { tr, when, node, value, source, latency };
    feedRows.push(row);
    return row;
  }

  function changeMatches(change) {
    if (machineFilter !== 'all' && machineOfPath(change.path) !== machineFilter) return false;
    return matchesNode(nameOfPath(change.path), change.node_id);
  }

  function renderFeed() {
    if (feedPaused) return;
    const source = statusOnly ? statusFeed : feed;
    const out = [];
    for (let i = source.length - 1; i >= 0 && out.length < FEED_ROWS; i -= 1) {
      if (changeMatches(source[i])) out.push(source[i]);
    }
    feedTable.hidden = !out.length;
    feedEmpty.hidden = Boolean(out.length);
    setText(feedEmpty, source.length ? 'Keine Änderung passt zum Filter.' : 'Noch keine Änderung empfangen.');
    out.forEach((change, i) => {
      const row = feedRows[i] ?? feedRow();
      row.tr.hidden = false;
      const name = nameOfPath(change.path);
      setText(row.when, fmt.wallTime(change.received_wall_ts, { ms: true }));
      setText(row.node, shortPath(change.path));
      setText(row.value, showValue(change.value, nodeRows.get(change.node_id)?.node.type ?? '', fmt));
      setText(row.source, fmt.wallTime(change.source_ts, { ms: true }));
      setText(row.latency, change.latency_ms === null || change.latency_ms === undefined ? fmt.DASH : `${fmt.number(change.latency_ms, change.latency_ms < 10 ? 1 : 0)} ms`);
      const fault = name === 'Status' && change.value === 'FAULT';
      const status = name === 'Status';
      const cls = fault ? 'is-fault' : status ? 'is-status' : '';
      if (row.tr.className !== cls) row.tr.className = cls;
    });
    for (let i = out.length; i < feedRows.length; i += 1) feedRows[i].tr.hidden = true;
    setText(feedCount, `${fmt.number(out.length)} von ${fmt.number(statusOnly ? statusFeed.length : feed.length)} seit Öffnen`);
  }

  function takeChanges(changes) {
    // Newest first from the server, oldest first here.
    let fresh = 0;
    for (let i = changes.length - 1; i >= 0; i -= 1) {
      const change = changes[i];
      const key = `${change.node_id}|${change.source_ts}|${change.received_wall_ts}`;
      if (seen.has(key)) continue;
      seen.add(key);
      feed.push(change);
      if (nameOfPath(change.path) === 'Status') statusFeed.push(change);
      fresh += 1;
    }
    if (feed.length > FEED_KEEP) {
      const dropped = feed.splice(0, feed.length - FEED_KEEP);
      for (const change of dropped) seen.delete(`${change.node_id}|${change.source_ts}|${change.received_wall_ts}`);
    }
    if (statusFeed.length > STATUS_KEEP) statusFeed.splice(0, statusFeed.length - STATUS_KEEP);
    if (feedPaused) {
      unseenWhilePaused += fresh;
      setText(feedCount, `angehalten, ${fmt.number(unseenWhilePaused)} neue`);
    }
    return fresh;
  }

  // --- method calls ---
  const methodBody = h('tbody');
  const methodBox = panel('Methodenaufrufe des MES', { dense: true });
  methodBox.node.classList.add('opcua-methods');
  const methodTable = h('table.tbl.opcua-table',
    h('thead', h('tr', h('th', 'Zeit'), h('th', 'Methode'), h('th', 'Knoten'), h('th', 'Argumente'), h('th', 'Ergebnis'), h('th.num', 'Dauer'))),
    methodBody);
  const methodEmpty = empty('');
  methodBox.body.append(methodTable, methodEmpty);
  let methodCalls = [];
  let methodKey = '';

  function resultCell(text) {
    let parsed = null;
    try {
      parsed = JSON.parse(text);
    } catch {
      parsed = null;
    }
    if (parsed && parsed.ok === false) {
      return h('td', { 'data-tip': text }, h('span.tag.tag--setup', `abgelehnt: ${parsed.reason ?? ''}`));
    }
    if (parsed && parsed.ok === true) return h('td.muted', { 'data-tip': text }, 'ok');
    return h('td.mono.opcua-method__cut', { tabindex: 0, 'data-tip': text ?? '' }, text ?? fmt.DASH);
  }

  function renderMethods() {
    const rows = methodCalls.filter((call) => {
      if (machineFilter !== 'all' && call.node !== machineFilter) return false;
      if (nodeFilter && !`${call.method} ${call.node}`.toLowerCase().includes(nodeFilter)) return false;
      return true;
    });
    const key = `${machineFilter}|${nodeFilter}|${rows.length}|${rows[0]?.wall_ts ?? ''}`;
    methodTable.hidden = !rows.length;
    methodEmpty.hidden = Boolean(rows.length);
    setText(methodEmpty, methodCalls.length ? 'Kein Methodenaufruf passt zum Filter.' : 'Noch kein Methodenaufruf.');
    if (key === methodKey) return;
    methodKey = key;
    clear(methodBody);
    methodBody.append(...rows.slice(0, 100).map((call) => h('tr',
      h('td.mono.nowrap', fmt.wallTime(call.wall_ts, { ms: true })),
      h('td.mono', call.method),
      h('td.mono', call.node),
      h('td.mono.opcua-method__cut', { tabindex: 0, 'data-tip': call.args ?? '' }, call.args ? compactArgs(call.args) : fmt.DASH),
      resultCell(call.result),
      h('td.num', typeof call.ms === 'number' ? `${fmt.number(call.ms, 1)} ms` : fmt.DASH))));
  }

  // --- assemble ---
  const right = h('div.opcua-right', feedBox.node, methodBox.node);
  el.append(band, claim, toolbar, alertRow, h('div.opcua-body', treeBox.node, right));
  machineSelect.append(h('option', { value: 'all' }, 'Alle'));
  applyFilters();

  // --- live values ---
  function paintLink() {
    const link = store.link();
    paintIcon(linkIcon, { ok: 'ok', stale: 'warn', down: 'FAULT', wait: 'info' }[link.level] ?? 'info');
    setText(linkLabel, link.label);
    linkLabel.parentElement.setAttribute('data-tip', link.detail ?? '');
  }
  const offConn = store.on('conn', paintLink);
  paintLink();

  const rateHistory = [];
  let lastRatePush = 0;
  const offState = store.subscribe((state) => {
    const counters = state.counters ?? {};
    setText(rateValue, fmt.number(counters.datachanges_per_s));
    setText(totalValue, fmt.number(counters.datachanges_total));
    setText(runValue, state.run_id ?? fmt.DASH);
    const now = performance.now();
    if (now - lastRatePush >= 1000) {
      lastRatePush = now;
      rateHistory.push(counters.datachanges_per_s ?? 0);
      if (rateHistory.length > RATE_POINTS) rateHistory.shift();
      rateSpark.update(rateHistory);
    }
  });

  const nodesPoll = poll(async () => {
    const data = await api.get('/api/opcua/nodes', { quiet: true });
    if (alive) paintNodes(data);
  }, 1000);

  const changesPoll = poll(async () => {
    const data = await api.get('/api/opcua/changes?limit=500', { quiet: true });
    if (!alive) return;
    const changes = data.changes ?? [];
    takeChanges(changes);
    const latencies = changes.map((c) => c.latency_ms).filter((v) => typeof v === 'number').sort((a, b) => a - b);
    setText(latencyValue, latencies.length ? `${fmt.number(quantile(latencies, 0.5))} / ${fmt.number(quantile(latencies, 0.95))} ms` : fmt.DASH);
    renderFeed();
  }, 1000);

  const methodsPoll = poll(async () => {
    const data = await api.get('/api/opcua/methods', { quiet: true });
    if (!alive) return;
    methodCalls = [...(data.calls ?? [])].sort((a, b) => (b.wall_ts ?? 0) - (a.wall_ts ?? 0));
    renderMethods();
  }, 2000);

  const healthPoll = poll(async () => {
    const data = await api.get('/api/health', { quiet: true });
    if (!alive) return;
    const loaded = data.plant_module_loaded === true || data.pool_plant_module_loaded === true;
    setText(moduleValue, loaded ? 'ja' : 'nein');
    moduleValue.classList.toggle('tone-fault', loaded);
    setText(snapshotValue, fmt.number(data.snapshot_calls));
  }, 5000);

  return () => {
    alive = false;
    nodesPoll.stop();
    changesPoll.stop();
    methodsPoll.stop();
    healthPoll.stop();
    offConn();
    offState();
  };
}
