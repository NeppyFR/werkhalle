// Planung: the active plan as a Gantt with its metrics, a re-solve with both solvers, and their comparison against the current plan.
// The same comparison opens for a proposal (#/planung?proposal=pr-3), and #/planung?order=A-1001 highlights an order.
import { h, setText, clear, loadCss } from '../core/dom.js';
import { panel, statusIcon, meter, updateMeter } from '../components/ui.js';
import { renderGantt, ganttDomain } from '../components/gantt.js';

const TIME_LIMITS = [3, 5, 8, 15, 30];
const POLL_MS = 600;
const NOW_REFRESH_MS = 1000;
const PLAN_REFETCH_GAP_MS = 2500;
const ROWS = ['GUSS', 'PR1', 'PR2', 'OF1', 'OF2', 'SAE', 'VP'];
const SOLVERS = ['cpsat', 'anneal'];
const COLUMNS = ['keep', 'cpsat', 'anneal'];
const STATUS_LABEL = { optimal: 'optimal', feasible: 'zulässig', no_solution: 'keine Lösung' };
const TRIGGER_LABEL = { fault: 'Störung', stall: 'Stillstand', no_material: 'Materialmangel', scenario: 'Szenario' };
const PROPOSAL_STATUS = { accepted: 'übernommen', superseded: 'überholt', expired: 'abgelaufen', rejected: 'verworfen' };

// Survives a view change, so coming back to Planung finds the last comparison again. Cleared when the plant restarts.
const memory = { limit: 8, setId: null, seed: 0 };

const metric = (name) => (c) => c?.metrics?.[name];
const twin = (name) => (c) => c?.twin?.[name];

/** One row per figure. `get` reads the value, `digits` and `unit` format it, `delta` shows the difference to Behalten. */
const TABLE = [
  { group: 'Plan laut Validator' },
  { label: 'Makespan', unit: 'min', get: metric('makespan'), delta: true },
  { label: 'Gesamtverspätung', unit: 'min', get: metric('total_tardiness'), delta: true },
  { label: 'Gewichtete Verspätung', unit: 'min', get: metric('weighted_tardiness'), delta: true },
  { label: 'Verspätete Aufträge', get: metric('late_orders'), delta: true },
  { label: 'Max. Verspätung', unit: 'min', get: metric('max_tardiness'), delta: true },
  { label: 'Rüstvorgänge', get: metric('setups'), delta: true },
  { label: 'Rüstzeit', unit: 'min', get: metric('setup_minutes'), delta: true },
  { label: 'Zielfunktion', get: metric('objective'), delta: true, strong: true },
  { group: 'Solver' },
  { label: 'Rechenzeit', unit: 's', digits: 1, get: (c) => c?.solve_seconds, solverOnly: true },
  { label: 'Status', status: true },
  { label: 'Untere Schranke', get: (c) => c?.best_bound, solverOnly: true, noBest: true, digits: 0 },
  { label: 'Iterationen', get: (c) => c?.iterations, solverOnly: true, noBest: true },
  { group: 'Prüfung' },
  { label: 'Gültigkeit laut Validator', valid: true },
  { group: 'Twin-Bestätigung, Lauf des Plans im Zwilling' },
  { label: 'Makespan', unit: 'min', get: twin('makespan'), delta: true },
  { label: 'Gesamtverspätung', unit: 'min', get: twin('total_tardiness'), delta: true },
  { label: 'Verspätete Aufträge', get: twin('late_orders'), delta: true },
  { label: 'Rüstvorgänge', get: twin('setups'), delta: true },
  { label: 'Energie', unit: 'kWh', get: twin('energy_kwh'), delta: true },
  { label: 'Ergebnis', outcome: true },
];

function kpiCell(label, { text = false } = {}) {
  const value = h('span.planung-kpi__value');
  const sub = h('span.planung-kpi__sub');
  const marker = statusIcon('warn', { size: 10 });
  marker.style.display = 'none';
  const node = h(`div.planung-kpi${text ? '.planung-kpi--text' : ''}`, h('span.planung-kpi__label', label), h('span.planung-kpi__main', value, marker), sub);
  return {
    node,
    set(main, subText, { warn = false } = {}) {
      setText(value, main);
      setText(sub, subText ?? '');
      marker.style.display = warn ? '' : 'none';
    },
  };
}

function buildHeader(fmt) {
  const cells = {
    revision: kpiCell('Revision'),
    solver: kpiCell('Solver', { text: true }),
    source: kpiCell('Quelle', { text: true }),
    makespan: kpiCell('Makespan'),
    tardiness: kpiCell('Gesamtverspätung'),
    late: kpiCell('Verspätete Aufträge'),
    setups: kpiCell('Rüstvorgänge'),
    setupTime: kpiCell('Rüstzeit'),
  };
  const node = h('section.planung-kpis', { 'aria-label': 'Kennzahlen des aktiven Plans' }, Object.values(cells).map((c) => c.node));
  return {
    node,
    update(plan) {
      const m = plan?.metrics;
      cells.revision.set(plan ? fmt.number(plan.revision) : fmt.DASH, plan ? `erstellt ${fmt.simTime(plan.created_min)}` : '');
      cells.solver.set(plan ? fmt.solverLabel(plan.solver) : fmt.DASH);
      cells.source.set(plan ? fmt.planSourceLabel(plan.source) : fmt.DASH, plan?.source_proposal_id ? `aus ${plan.source_proposal_id}` : '');
      const end = plan && typeof m?.makespan === 'number' ? plan.created_min + m.makespan : null;
      cells.makespan.set(fmt.duration(m?.makespan), end === null ? '' : `bis ${fmt.simTime(end)}`);
      cells.tardiness.set(fmt.duration(m?.total_tardiness), m?.max_tardiness ? `höchstens ${fmt.duration(m.max_tardiness)}` : '', { warn: (m?.total_tardiness ?? 0) > 0 });
      cells.late.set(fmt.number(m?.late_orders), plan?.orders ? `von ${fmt.number(plan.orders.length)} offenen` : '', { warn: (m?.late_orders ?? 0) > 0 });
      cells.setups.set(fmt.number(m?.setups));
      cells.setupTime.set(fmt.duration(m?.setup_minutes));
    },
  };
}

function bestSet(values) {
  // Marks the lowest value. A tie among all competitors, or a single competitor, says nothing.
  const numbers = values.filter((v) => typeof v.value === 'number');
  if (numbers.length < 2) return new Set();
  const best = Math.min(...numbers.map((v) => v.value));
  const winners = numbers.filter((v) => v.value === best);
  if (winners.length === numbers.length) return new Set();
  return new Set(winners.map((v) => v.key));
}

function violationText(v) {
  if (typeof v === 'string') return v;
  const ops = v.op_ids?.length ? ` (${v.op_ids.slice(0, 3).join(', ')}${v.op_ids.length > 3 ? ' …' : ''})` : '';
  return `${v.rule ? `${v.rule}: ` : ''}${v.message ?? ''}${ops}`;
}

export function mount(el, ctx) {
  loadCss('css/planung.css');
  const { store, api, fmt, router, toast } = ctx;
  el.classList.add('planung');

  let selected = router.params.order ?? null;
  let unmounted = false;
  let comparison = null; // {kind: 'set'|'proposal', id, data, computing, startedAt, limit, seed, notice, applied, invalid, busy}
  let pollTimer = 0;
  let tickTimer = 0;
  let lastNow = 0;
  let lastOpKey = null;
  let lastRefetch = 0;
  let renderedKey = '';
  let charts = [];
  let lastRevision = store.plan?.revision ?? null;

  // ------------------------------------------------------------------ active plan
  const header = buildHeader(fmt);
  const activeKey = h('div.planung__key');
  const activeBox = panel('Aktiver Plan', { actions: [activeKey] });
  const chartHost = h('div.planung__chart');
  const noPlan = h('p.empty', 'Noch kein aktiver Plan vom MES.');
  const selectionLine = h('div.planung__selection', { hidden: true });
  activeBox.body.append(noPlan, chartHost, selectionLine);

  const active = renderGantt(chartHost, {
    rows: ROWS,
    selected,
    keyHost: activeKey,
    faults: [],
    onSelect: (id) => select(id, active),
  });

  function select(id, source) {
    selected = id;
    if (source !== active) active.setSelected(id);
    for (const entry of charts) {
      if (entry.chart !== source) entry.chart.setSelected(id);
    }
    renderSelection();
  }

  function renderSelection() {
    clear(selectionLine);
    selectionLine.hidden = !selected;
    if (!selected) return;
    const plan = store.plan;
    const order = plan?.orders?.find((o) => o.id === selected);
    const ops = plan?.assignments?.filter((a) => a.order_id === selected) ?? [];
    const completion = plan?.metrics?.order_completion?.[selected] ?? null;
    const late = order && completion !== null ? completion - order.due : null;
    const parts = [h('strong.mono', selected)];
    if (ops.length) {
      parts.push(h('span', fmt.productName(ops[0].product_id)));
      if (order) parts.push(h('span', `Termin ${fmt.simTime(order.due)}`));
      if (completion !== null) parts.push(h('span', `Fertigstellung ${fmt.simTime(completion)}`));
      if (late !== null) {
        parts.push(late > 0
          ? h('span.tone-setup.planung__late', statusIcon('warn', { size: 10 }), `verspätet ${fmt.duration(late, { signed: true })}`)
          : h('span', `Reserve ${fmt.duration(-late)}`));
      }
    } else {
      parts.push(h('span.muted', 'ist im aktiven Plan nicht enthalten, er ist fertig oder noch nicht eingeplant.'));
    }
    selectionLine.append(
      h('span.planung__selectionText', parts),
      h('a.btn.btn--ghost.btn--sm', { href: router.href('auftraege', { order: selected }) }, 'Zum Auftrag'),
      h('a.btn.btn--ghost.btn--sm', { href: router.href('rueckverfolgung', { q: selected }) }, 'Rückverfolgung'),
      h('button.btn.btn--ghost.btn--sm', { type: 'button', onclick: () => select(null, null) }, 'Auswahl aufheben'));
  }

  // Real outages, kept apart from plan.downtimes: a plan accepted before a fault, or a proposal thrown away, knows nothing of it.
  let pastFaults = [];
  let faultKey = null;
  let faultLoad = 0;

  function liveFaults() {
    const now = store.clock.now();
    return (store.state?.machines ?? [])
      .filter((m) => m.status === 'FAULT' && typeof m.status_since === 'number')
      .map((m) => ({ machine: m.id, start: m.status_since, end: Math.max(m.status_since + (m.est_repair_min ?? 0), now ?? m.status_since), reason: m.alarm?.code ?? null, live: true }));
  }

  function planDowntimes(plan, faults) {
    // A plan's downtime that only repeats a real outage would stack a second band and label on it.
    return (plan.downtimes ?? []).filter((d) => !faults.some((f) => f.machine === d.machine && d.start >= f.start - 1 && d.end <= f.end + 1));
  }

  function paintFaults() {
    const plan = store.plan;
    if (!(plan?.revision > 0)) return;
    const faults = [...pastFaults, ...liveFaults()];
    active.update({ faults, downtimes: planDowntimes(plan, faults) });
  }

  async function loadPastFaults() {
    const mine = ++faultLoad;
    try {
      const data = await api.get('/api/alarms?limit=200', { quiet: true });
      if (unmounted || mine !== faultLoad) return;
      pastFaults = (data.alarms ?? [])
        .filter((a) => a.severity >= 900 && typeof a.cleared_t === 'number' && a.machine !== 'MES')
        .map((a) => ({ machine: a.machine, start: a.t, end: a.cleared_t, reason: a.code, live: false }));
      paintFaults();
    } catch {
      /* the live faults still show, and the next change tries again */
    }
  }

  function showPlan(next) {
    // The MES answers revision 0 and empty lists until its start-up plan is applied.
    const plan = next?.revision > 0 ? next : null;
    header.update(plan);
    noPlan.hidden = Boolean(plan);
    chartHost.hidden = !plan;
    if (!plan) return;
    const faults = [...pastFaults, ...liveFaults()];
    active.update({
      assignments: plan.assignments ?? [],
      downtimes: planDowntimes(plan, faults),
      faults,
      orders: plan.orders ?? [],
      orderCompletion: plan.metrics?.order_completion ?? null,
      now: store.clock.now() ?? plan.now,
    });
    renderSelection();
    renderHistory();
  }

  // ------------------------------------------------------------------ revision history, from the log
  const historyBox = panel('Planrevisionen', { dense: true });
  const historyList = h('ol.planung__history');
  historyBox.body.appendChild(historyList);
  let historyKey = '';

  function renderHistory() {
    const entries = store.log.filter((e) => e.source === 'plan' && /\bplan revision\s+\d+/i.test(e.text ?? ''));
    historyBox.node.hidden = entries.length === 0;
    const current = store.plan?.revision;
    const key = `${current}|${entries.map((e) => `${e.t}:${e.text}`).join('|')}`;
    if (key === historyKey) return;
    historyKey = key;
    clear(historyList);
    for (const entry of entries.slice(0, 30)) {
      const revision = Number(/revision\s+(\d+)/i.exec(entry.text)[1]);
      historyList.appendChild(h(`li.planung__historyRow${revision === current ? '.is-current' : ''}`,
        h('span.mono.faint', fmt.simTime(entry.t)),
        h('span', entry.text),
        revision === current ? h('span.tag', 'aktiv') : null));
    }
  }

  // ------------------------------------------------------------------ solver panel
  const limitSeg = h('div.seg', { role: 'group', 'aria-label': 'Zeitlimit je Solver' }, TIME_LIMITS.map((value) => h('button.seg__btn', {
    type: 'button',
    'aria-pressed': String(value === memory.limit),
    onclick: (event) => {
      memory.limit = value;
      for (const button of limitSeg.children) button.setAttribute('aria-pressed', String(button === event.currentTarget));
    },
  }, `${value} s`)));
  const solveButton = h('button.btn.btn--primary', { type: 'button', onclick: () => solve() }, 'Neu berechnen');
  const openProposalButton = h('button.btn', { type: 'button', hidden: true, onclick: () => openProposal(store.proposals.open) });
  const compareBox = panel('Solververgleich', {
    actions: [openProposalButton, h('span.planung__limitLabel', 'Zeitlimit je Solver'), limitSeg, solveButton],
  });
  const objectiveLine = h('p.planung__objective', 'Beide Solver minimieren dieselbe Zielfunktion: 10 × gewichtete Verspätung + Makespan + 2 × Rüstzeit, kleiner ist besser.');
  const statusRow = h('div.planung__status', { hidden: true });
  const statusText = h('span.planung__statusText');
  const statusMeter = meter({ label: 'Rechendauer' });
  statusRow.append(statusText, statusMeter);
  const noticeHost = h('div.planung__notices');
  const metaLine = h('p.planung__meta', { hidden: true });
  const compareBody = h('div.planung-cmp', { hidden: true });
  const tableHost = h('div.planung-cmp__table');
  const compareLegend = h('div.planung-cmp__legend');
  const chartStack = h('div.planung-cmp__stack');
  const chartsHost = h('div.planung-cmp__charts', compareLegend, chartStack);
  compareBody.append(tableHost, chartsHost);
  const emptyLine = h('p.empty', 'Noch kein Vergleich. „Neu berechnen“ lässt CP-SAT und Simulated Annealing auf dem aktuellen Zustand rechnen und prüft beide Ergebnisse im Zwilling.');
  compareBox.body.append(objectiveLine, statusRow, noticeHost, emptyLine, metaLine, compareBody);

  el.append(header.node, activeBox.node, compareBox.node, historyBox.node);

  function destroyCharts() {
    for (const entry of charts) entry.chart.destroy();
    charts = [];
  }

  function candidatesOf(c) {
    const data = c?.data;
    if (!data) return {};
    if (c.kind === 'proposal') return { keep: data.keep, cpsat: data.candidates?.cpsat, anneal: data.candidates?.anneal };
    return { keep: data.candidates?.keep, cpsat: data.candidates?.cpsat, anneal: data.candidates?.anneal };
  }

  function usable(candidate) {
    return Boolean(candidate) && candidate.valid !== false && candidate.status !== 'no_solution';
  }

  // Why a candidate cannot be applied right now, or null. Each reason is said out loud at the button.
  function blockedReason(key, candidate) {
    if (!comparison || comparison.busy) return 'wird übernommen …';
    if (comparison.computing) return 'wird neu berechnet …';
    if (comparison.applied) return comparison.applied.solver === key ? `übernommen, Revision ${fmt.number(comparison.applied.revision)}` : 'nicht übernommen';
    if (comparison.invalid.has(key) || candidate?.valid === false) return 'ungültig, nicht übernehmbar';
    if (candidate?.status === 'no_solution') return 'keine Lösung';
    if (comparison.kind === 'set' && (comparison.data?.stale || comparison.rejected)) return 'veraltet, neu berechnen';
    if (comparison.kind === 'proposal' && comparison.data?.status !== 'open') return 'Vorschlag nicht mehr offen';
    return null;
  }

  function cellGrid(mark, text, delta, hasDelta) {
    return h(`span.planung-cell${hasDelta ? '.planung-cell--delta' : ''}`,
      h('span.planung-cell__mark', mark ? [h('span.planung-best'), h('span.gantt__sr', 'bester Wert')] : null),
      h('span.planung-cell__value', text),
      hasDelta ? h('span.planung-cell__delta', delta) : null);
  }

  function renderTable(cands) {
    clear(tableHost);
    const recommended = comparison.data?.recommended;
    const head = h('tr',
      h('th', { scope: 'col' }, 'Kennzahl'),
      COLUMNS.map((key) => h('th.planung-col', { scope: 'col' },
        h('span.planung-col__name', key === 'keep' ? 'Behalten' : fmt.solverLabel(key)),
        key === 'keep' ? h('span.tag', 'Referenz') : key === recommended ? h('span.tag', 'empfohlen') : null)));
    const body = h('tbody');

    for (const row of TABLE) {
      if (row.group) {
        body.appendChild(h('tr.planung-group', h('th', { colSpan: 4, scope: 'colgroup' }, row.group)));
        continue;
      }
      const cells = COLUMNS.map((key) => ({ key, candidate: cands[key] }));
      if (row.status) {
        const optimal = new Set(cells.filter((c) => c.key !== 'keep' && c.candidate?.status === 'optimal').map((c) => c.key));
        const others = cells.some((c) => c.key !== 'keep' && c.candidate && c.candidate.status !== 'optimal');
        body.appendChild(h('tr', h('th', { scope: 'row' }, row.label), cells.map(({ key, candidate }) => h(`td.num${optimal.has(key) && others ? '.is-best' : ''}`,
          cellGrid(optimal.has(key) && others, key === 'keep' ? 'Referenz' : candidate ? (STATUS_LABEL[candidate.status] ?? candidate.status) : fmt.DASH, null, key !== 'keep')))));
        continue;
      }
      if (row.valid) {
        body.appendChild(h('tr', h('th', { scope: 'row' }, row.label), cells.map(({ key, candidate }) => {
          const bad = candidate?.valid === false || comparison.invalid.has(key);
          const count = candidate?.violations?.length ?? 0;
          return h(`td.num${bad ? '.planung-td--fault' : ''}`,
            h('span.planung-valid', candidate ? statusIcon(bad ? 'FAULT' : 'ok', { size: 11 }) : null,
              candidate ? (bad ? `ungültig${count ? `, ${fmt.plural(count, 'Verstoß', 'Verstöße')}` : ''}` : 'gültig') : fmt.DASH));
        })));
        const withViolations = cells.filter((c) => c.candidate?.violations?.length);
        if (withViolations.length) {
          body.appendChild(h('tr.planung-violations', h('th', { scope: 'row' }, 'Verstöße'), cells.map(({ candidate }) => h('td',
            candidate?.violations?.length
              ? h('ul', candidate.violations.slice(0, 8).map((v) => h('li', violationText(v))), candidate.violations.length > 8 ? h('li.faint', `und ${fmt.number(candidate.violations.length - 8)} weitere`) : null)
              : null))));
        }
        continue;
      }
      if (row.outcome) {
        body.appendChild(h('tr', h('th', { scope: 'row' }, row.label), cells.map(({ candidate }) => {
          const t = candidate?.twin;
          if (!t) return h('td.num.faint', fmt.DASH);
          const unfinished = t.unfinished_orders?.length ?? 0;
          const text = t.stalled ? 'Stillstand' : unfinished ? `${fmt.number(unfinished)} unvollständig` : 'vollständig';
          const bad = t.stalled || unfinished > 0;
          const tip = unfinished ? t.unfinished_orders.join(', ') : undefined;
          return h(`td.num${bad ? '.planung-td--warn' : ''}`, { 'data-tip': tip },
            h('span.planung-valid', statusIcon(bad ? 'warn' : 'ok', { size: 11 }), text));
        })));
        continue;
      }
      const competing = cells.filter((c) => usable(c.candidate) && !(row.solverOnly && c.key === 'keep'));
      const winners = row.noBest ? new Set() : bestSet(competing.map((c) => ({ key: c.key, value: row.get(c.candidate) })));
      const reference = row.delta ? row.get(cands.keep) : null;
      body.appendChild(h(`tr${row.strong ? '.planung-row--strong' : ''}`,
        h('th', { scope: 'row', 'data-tip': row.tip }, row.label, row.unit ? h('span.planung-unit', ` ${row.unit}`) : null),
        cells.map(({ key, candidate }) => {
          const hide = row.solverOnly && key === 'keep';
          const value = hide ? null : row.get(candidate);
          const best = winners.has(key);
          const showDelta = row.delta && key !== 'keep';
          const delta = showDelta && typeof value === 'number' && typeof reference === 'number' && value !== reference ? fmt.signed(value - reference) : '';
          return h(`td.num${best ? '.is-best' : ''}`, cellGrid(best, fmt.number(value, row.digits ?? 0), delta, key !== 'keep'));
        })));
    }

    const foot = h('tr.planung-apply',
      h('th', { scope: 'row' }, 'Übernahme'),
      COLUMNS.map((key) => {
        if (key === 'keep') return h('td.planung-apply__keep', 'bleibt aktiv');
        const candidate = cands[key];
        if (!candidate) return h('td.num.faint', fmt.DASH);
        const reason = blockedReason(key, candidate);
        return h('td.num',
          h(`button.btn.btn--sm${key === comparison.data?.recommended ? '.btn--primary' : ''}`, {
            type: 'button',
            disabled: Boolean(reason),
            onclick: () => apply(key),
          }, 'Diesen Plan übernehmen'),
          reason ? h('span.planung-apply__why', reason) : null);
      }));

    tableHost.appendChild(h('table.tbl.planung-tbl', h('thead', head), body, h('tfoot', foot)));
    const notes = COLUMNS.map((key) => (cands[key]?.note ? h('li', h('span.muted', `${key === 'keep' ? 'Behalten' : fmt.solverLabel(key)}: `), cands[key].note) : null)).filter(Boolean);
    tableHost.appendChild(h('div.planung-legend', h('span.planung-best'), 'bester Wert der Zeile, die kleine Zahl daneben ist der Abstand zu „Behalten“'));
    if (notes.length) tableHost.appendChild(h('ul.planung-notes', notes));
  }

  function renderCharts(cands) {
    destroyCharts();
    clear(chartStack);
    const orders = store.plan?.orders ?? [];
    const now = store.clock.now() ?? store.plan?.now ?? comparison.data?.created_min ?? null;
    const present = SOLVERS.filter((key) => cands[key]?.assignments?.length);
    if (!present.length) {
      chartStack.appendChild(h('p.empty', comparison.kind === 'proposal' ? 'Für diesen Vorschlag liegen keine Planzeilen mehr vor: abgeschlossene Vorschläge werden ohne Zuordnungen aufbewahrt.' : 'Kein Solver hat einen Plan geliefert.'));
      return;
    }
    const domain = ganttDomain(present.map((key) => ({ assignments: cands[key].assignments, downtimes: cands[key].downtimes, now })));
    present.forEach((key, index) => {
      const candidate = cands[key];
      const host = h('div.planung-cmp__chart');
      const tableToggle = h('button.btn.btn--ghost.btn--sm', {
        type: 'button',
        'aria-pressed': 'false',
        onclick: () => {
          const open = chart.toggleTable();
          tableToggle.setAttribute('aria-pressed', String(open));
          tableToggle.textContent = open ? 'Tabelle ausblenden' : 'Als Tabelle';
        },
      }, 'Als Tabelle');
      chartStack.append(h('h3.planung-cmp__title', fmt.solverLabel(key), key === comparison.data?.recommended ? h('span.tag', 'empfohlen') : null,
        h('span.planung-cmp__fig.mono', `Ziel ${fmt.number(candidate.metrics?.objective)} · Verspätung ${fmt.duration(candidate.metrics?.total_tardiness)} · ${fmt.plural(candidate.metrics?.setups ?? 0, 'Rüstvorgang', 'Rüstvorgänge')}`),
        tableToggle), host);
      const chart = renderGantt(host, {
        assignments: candidate.assignments,
        downtimes: candidate.downtimes ?? [],
        orders,
        orderCompletion: candidate.metrics?.order_completion ?? null,
        now,
        rows: ROWS,
        domain,
        compact: true,
        legend: index === 0,
        legendKey: false,
        legendHost: compareLegend,
        selected,
        onSelect: (id) => select(id, chart),
        onViewChange: (view, { fit }) => {
          for (const other of charts) if (other.chart !== chart) other.chart.setView(view, { fit });
        },
      });
      charts.push({ key, chart });
    });
  }

  function renderNotice() {
    clear(noticeHost);
    const c = comparison;
    const stale = c?.kind === 'set' && c.data?.stale && !c.applied && !c.rejected;
    if (stale) {
      noticeHost.appendChild(h('div.planung-notice.planung-notice--warn', { role: 'status' }, statusIcon('warn'),
        h('span', 'Dieser Vergleich ist veraltet: der aktive Plan hat sich seit der Berechnung geändert. Übernehmen ist gesperrt.'),
        h('button.btn.btn--sm', { type: 'button', onclick: () => solve() }, 'Neu berechnen')));
    }
    if (c?.kind === 'proposal' && c.data && !c.applied && !['computing', 'open'].includes(c.data.status)) {
      const accepted = c.data.status === 'accepted';
      noticeHost.appendChild(h(`div.planung-notice.planung-notice--${accepted ? 'ok' : 'warn'}`, { role: 'status' }, statusIcon(accepted ? 'ok' : 'warn'),
        h('span', `Vorschlag ${c.id} ist ${PROPOSAL_STATUS[c.data.status] ?? c.data.status}. ${c.data.status_reason ?? ''}`)));
    }
    if (c?.notice) {
      const { tone, text, list, action } = c.notice;
      noticeHost.appendChild(h(`div.planung-notice.planung-notice--${tone}`, { role: tone === 'ok' ? 'status' : 'alert' },
        statusIcon(tone === 'ok' ? 'ok' : tone === 'fault' ? 'FAULT' : 'warn'),
        h('span', text, list?.length ? h('ul.planung-notice__list', list.slice(0, 8).map((v) => h('li', violationText(v)))) : null),
        action));
    }
  }

  function renderComparison() {
    const c = comparison;
    const data = c?.data;
    const hasResult = Boolean(data) && (c.kind === 'proposal' ? data.status !== 'computing' : data.status === 'ready');
    emptyLine.hidden = Boolean(c);
    renderNotice();
    solveButton.disabled = Boolean(c?.computing);
    solveButton.textContent = c?.computing ? 'Rechnet …' : 'Neu berechnen';

    statusRow.hidden = !c?.computing;

    // While a new result is computing, the previous comparison stays in place at reduced strength instead of vanishing.
    compareBody.classList.toggle('is-refreshing', Boolean(c?.computing));
    if (!hasResult) {
      if (!c?.computing || !charts.length) {
        compareBody.hidden = true;
        metaLine.hidden = true;
      }
      if (c?.failed) {
        noticeHost.appendChild(h('div.planung-notice.planung-notice--fault', { role: 'alert' }, statusIcon('FAULT'),
          h('span', c.failed), h('button.btn.btn--sm', { type: 'button', onclick: () => solve() }, 'Erneut versuchen')));
      }
      return;
    }

    const cands = candidatesOf(c);
    const key = JSON.stringify([c.kind, c.id, data.status, data.stale, c.rejected, c.applied, c.busy, c.computing, [...c.invalid], SOLVERS.map((s2) => cands[s2]?.valid)]);
    compareBody.hidden = false;
    metaLine.hidden = false;
    clear(metaLine);
    if (c.kind === 'set') {
      metaLine.append(`${c.id} · berechnet ab ${fmt.simTime(data.created_min)} · ${fmt.plural(data.lots ?? 0, 'Los', 'Lose')}, ${fmt.plural(data.ops ?? 0, 'Operation', 'Operationen')} · Limit ${fmt.number(data.time_limit_s)} s je Solver${c.seed ? ` · Startwert ${c.seed}` : ''}`);
    } else {
      const t = data.trigger ?? {};
      metaLine.append(`Vorschlag ${c.id} · ${TRIGGER_LABEL[t.kind] ?? 'Auslöser'}${t.machine ? ` an ${t.machine}` : ''}${t.code ? ` (${t.code})` : ''} · auf Basis von Revision ${fmt.number(data.based_on_revision)}${data.summary ? ` · ${data.summary}` : ''}`);
    }
    if (key === renderedKey) return;
    const dataChanged = !renderedKey.startsWith(JSON.stringify([c.kind, c.id, data.status]).slice(0, -1));
    renderedKey = key;
    renderTable(cands);
    // A proposal closing arrives without its assignments: the charts stay as they were.
    const hasRows = SOLVERS.some((s2) => cands[s2]?.assignments?.length);
    if (!charts.length || (dataChanged && hasRows)) renderCharts(cands);
  }

  // ------------------------------------------------------------------ actions
  // Scrolls the view only. scrollIntoView would also move the page frame, whose overflow:hidden does not stop programmatic scrolling, and push the top bar out.
  function showCompare() {
    const top = compareBox.node.getBoundingClientRect().top - el.getBoundingClientRect().top + el.scrollTop - 6;
    el.scrollTo({ top, behavior: matchMedia('(prefers-reduced-motion: reduce)').matches ? 'auto' : 'smooth' });
  }

  function startComparison(partial) {
    comparison = { invalid: new Set(), busy: false, notice: null, applied: null, computing: false, rejected: false, ...partial };
    return comparison;
  }

  function openProposal(proposal) {
    clearTimeout(pollTimer);
    stopTicker();
    renderedKey = '';
    startComparison({ kind: 'proposal', id: proposal.id, data: proposal, computing: proposal.status === 'computing', limit: null, startedAt: performance.now() });
    if (comparison.computing) startTicker();
    paintOpenProposal();
    renderComparison();
    showCompare();
    loadProposalRows(comparison);
  }

  // Proposal messages leave out the assignments to stay small. Only GET /api/proposals carries them, and only for the open one.
  async function loadProposalRows(entry) {
    try {
      const list = await api.get('/api/proposals', { quiet: true });
      if (unmounted || comparison !== entry || list.open?.id !== entry.id) return;
      entry.data = list.open;
      entry.computing = list.open.status === 'computing';
      if (!entry.computing) stopTicker();
      renderedKey = '';
      renderComparison();
    } catch {
      /* the table still shows what the message carried */
    }
  }

  async function solve() {
    if (comparison?.computing) return;
    const previous = comparison?.kind === 'set' ? comparison : null;
    memory.seed += 1;
    const seed = memory.seed;
    const limit = memory.limit;
    const running = startComparison({ kind: 'set', id: previous?.id ?? null, data: previous?.data ?? null, computing: true, limit, seed, startedAt: performance.now() });
    startTicker();
    renderComparison();
    try {
      const response = await api.post('/api/plan/solve', { time_limit_s: limit, seed }, { quiet: true });
      if (unmounted || comparison !== running) return;
      running.pollId = response.id;
      memory.setId = response.id;
      poll(running);
    } catch (error) {
      if (comparison !== running) return;
      running.computing = false;
      running.data = null;
      running.failed = `Berechnung konnte nicht gestartet werden: ${error.message}`;
      stopTicker();
      renderComparison();
    }
  }

  function poll(entry) {
    clearTimeout(pollTimer);
    pollTimer = setTimeout(async () => {
      if (unmounted || comparison !== entry) return;
      try {
        const data = await api.get(`/api/plan/candidates/${encodeURIComponent(entry.pollId)}`, { quiet: true });
        if (unmounted || comparison !== entry) return;
        entry.networkTrouble = false;
        if (data.status === 'computing') {
          poll(entry);
          return;
        }
        entry.computing = false;
        stopTicker();
        if (data.status === 'failed') {
          entry.data = null;
          entry.failed = `Die Berechnung ist fehlgeschlagen.${data.note ? ` ${data.note}` : ''}`;
        } else {
          entry.id = data.id;
          entry.data = data;
          renderedKey = '';
          requestAnimationFrame(() => {
            if (!unmounted) showCompare();
          });
        }
        renderComparison();
      } catch (error) {
        if (unmounted || comparison !== entry) return;
        if (error.status === 404) {
          entry.computing = false;
          entry.data = null;
          entry.failed = 'Das MES kennt diese Berechnung nicht mehr, vermutlich wurde es neu gestartet.';
          stopTicker();
          renderComparison();
          return;
        }
        // A short network hiccup does not end a running calculation.
        entry.networkTrouble = true;
        poll(entry);
      }
    }, POLL_MS);
  }

  function startTicker() {
    stopTicker();
    updateTicker();
    tickTimer = setInterval(updateTicker, 200);
  }

  function stopTicker() {
    clearInterval(tickTimer);
    tickTimer = 0;
  }

  function updateTicker() {
    const c = comparison;
    if (!c?.computing) return;
    const elapsed = (performance.now() - c.startedAt) / 1000;
    const limit = c.limit;
    if (!limit) {
      setText(statusText, `Vorschlag ${c.id} wird berechnet …`);
      updateMeter(statusMeter, null);
      return;
    }
    const overrun = elapsed > limit;
    const text = c.networkTrouble
      ? `MES nicht erreichbar, frage weiter nach … ${fmt.seconds(elapsed)}`
      : overrun
        ? `Solver am Limit, der Zwilling prüft die Ergebnisse … ${fmt.seconds(elapsed)}`
        : `CP-SAT und Simulated Annealing rechnen auf einem Abzweig des aktuellen Zustands … ${fmt.seconds(elapsed)} von ${fmt.number(limit)} s`;
    setText(statusText, text);
    updateMeter(statusMeter, Math.min(1, elapsed / limit));
  }

  async function apply(key) {
    const c = comparison;
    if (!c || c.busy) return;
    c.busy = true;
    c.notice = null;
    renderComparison();
    try {
      const result = c.kind === 'set'
        ? await api.post('/api/plan/apply', { candidates_id: c.id, solver: key }, { quiet: true })
        : await api.post(`/api/proposals/${encodeURIComponent(c.id)}/accept`, { solver: key }, { quiet: true });
      c.applied = { solver: key, revision: result?.revision ?? null };
      c.notice = { tone: 'ok', text: `${fmt.solverLabel(key)} ist jetzt aktiv: Plan Revision ${fmt.number(result?.revision)}.` };
      toast(`Plan übernommen: Revision ${fmt.number(result?.revision)} (${fmt.solverLabel(key)}).`, { kind: 'ok' });
      store.refetchPlan();
    } catch (error) {
      if (error.status === 409) {
        c.rejected = true;
        const next = error.body?.new_proposal_id;
        c.notice = {
          tone: 'warn',
          text: next
            ? `Abgelehnt, veraltet: ${error.message} Vorschlag ${next} wird automatisch neu berechnet.`
            : `Abgelehnt, veraltet: ${error.message}`,
          action: c.kind === 'set' ? h('button.btn.btn--sm', { type: 'button', onclick: () => solve() }, 'Neu berechnen') : null,
        };
        if (next) store.refetchProposals();
      } else if (error.status === 422) {
        c.invalid.add(key);
        c.notice = { tone: 'fault', text: `Abgelehnt, ungültig: ${error.message}`, list: error.body?.violations ?? [] };
      } else {
        c.notice = { tone: 'fault', text: `Nicht übernommen: ${error.message}` };
      }
    } finally {
      c.busy = false;
      if (comparison === c) renderComparison();
    }
  }

  async function restore() {
    const wanted = router.params.proposal;
    if (wanted) {
      try {
        const list = await api.get('/api/proposals', { quiet: true });
        if (unmounted) return;
        const found = list.open?.id === wanted ? list.open : list.history?.find((p) => p.id === wanted);
        if (found) {
          openProposal(found);
        } else {
          startComparison({ kind: 'proposal', id: wanted, data: null, failed: `Vorschlag ${wanted} ist nicht bekannt.` });
          renderComparison();
        }
      } catch {
        /* the connection indicator tells the story */
      }
      return;
    }
    if (!memory.setId) return;
    try {
      const data = await api.get(`/api/plan/candidates/${encodeURIComponent(memory.setId)}`, { quiet: true });
      if (unmounted || comparison) return;
      const entry = startComparison({ kind: 'set', id: data.id, data: data.status === 'ready' ? data : null, computing: data.status === 'computing', pollId: data.id, limit: data.time_limit_s, startedAt: performance.now() });
      if (entry.computing) {
        startTicker();
        poll(entry);
      } else if (data.status === 'failed') {
        entry.failed = 'Die letzte Berechnung ist fehlgeschlagen.';
      }
      renderComparison();
    } catch {
      memory.setId = null;
    }
  }

  async function refreshStale() {
    const c = comparison;
    if (c?.kind !== 'set' || !c.id || c.computing || c.applied) return;
    try {
      const data = await api.get(`/api/plan/candidates/${encodeURIComponent(c.id)}`, { quiet: true });
      if (comparison !== c || unmounted) return;
      if (data.stale !== c.data?.stale) {
        c.data = { ...c.data, stale: data.stale };
        renderComparison();
      }
    } catch {
      /* next plan message tries again */
    }
  }

  // ------------------------------------------------------------------ live wiring
  function paintOpenProposal() {
    const open = store.proposals.open;
    const offer = open && open.status === 'open' && !(comparison?.kind === 'proposal' && comparison.id === open.id);
    openProposalButton.hidden = !offer;
    if (offer) setText(openProposalButton, `Vorschlag ${open.id} vergleichen`);
  }

  const offState = store.subscribe((state) => {
    const stamp = performance.now();
    // The band only needs redrawing when a fault starts, ends or runs past its estimate; not on every state.
    const nextFaultKey = liveFaults().map((f) => `${f.machine}:${f.start}:${f.reason}:${Math.round(f.end / 5)}`).join('|');
    if (nextFaultKey !== faultKey) {
      const faultEnded = faultKey !== null && faultKey.split('|').length > nextFaultKey.split('|').length;
      faultKey = nextFaultKey;
      paintFaults();
      if (faultEnded) loadPastFaults();
    }
    if (stamp - lastNow >= NOW_REFRESH_MS) {
      lastNow = stamp;
      const now = store.clock.now();
      if (store.plan) active.update({ now });
      for (const entry of charts) entry.chart.update({ now });
    }
    // Op states in the plan only change when an op starts or ends, and the state says exactly when that is.
    const opKey = (state.machines ?? []).map((m) => (m.op?.op_ids ?? []).join(',')).join('|');
    if (opKey !== lastOpKey) {
      const first = lastOpKey === null;
      lastOpKey = opKey;
      if (!first && stamp - lastRefetch >= PLAN_REFETCH_GAP_MS) {
        lastRefetch = stamp;
        store.refetchPlan();
      }
    }
  });
  const offPlan = store.on('plan', (plan) => {
    showPlan(plan);
    if (plan.revision !== lastRevision) {
      lastRevision = plan.revision;
      refreshStale();
      loadPastFaults();
    }
  });
  const offProposal = store.on('proposal', (proposal) => {
    paintOpenProposal();
    if (comparison?.kind === 'proposal' && comparison.id === proposal.id) {
      // A slim message must not wipe assignments already loaded.
      const held = comparison.data;
      const hasRows = (c) => Boolean(c?.candidates?.cpsat?.assignments?.length || c?.candidates?.anneal?.assignments?.length);
      comparison.data = hasRows(proposal) || !hasRows(held) ? proposal : { ...held, status: proposal.status, status_reason: proposal.status_reason };
      const wasComputing = comparison.computing;
      comparison.computing = proposal.status === 'computing';
      if (!comparison.computing) stopTicker();
      renderComparison();
      if (wasComputing && proposal.status === 'open') loadProposalRows(comparison);
    }
  });
  const offLog = store.on('log', renderHistory);
  const offLogReset = store.on('log-reset', renderHistory);
  const offRun = store.on('run-change', () => {
    memory.setId = null;
    clearTimeout(pollTimer);
    stopTicker();
    comparison = null;
    renderedKey = '';
    pastFaults = [];
    loadPastFaults();
    destroyCharts();
    clear(chartStack);
    clear(tableHost);
    showPlan(null);
    renderComparison();
  });

  showPlan(store.plan ?? null);
  if (!store.plan && store.state?.plan) header.update(store.state.plan);
  renderHistory();
  paintOpenProposal();
  renderComparison();
  store.refetchPlan();
  loadPastFaults();
  restore();

  return () => {
    unmounted = true;
    clearTimeout(pollTimer);
    stopTicker();
    offState();
    offPlan();
    offProposal();
    offLog();
    offLogReset();
    offRun();
    destroyCharts();
    active.destroy();
  };
}
