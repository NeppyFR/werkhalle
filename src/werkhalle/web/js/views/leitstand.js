// Leitstand: plant KPIs, the hall, a machine strip, active alarms and the live log.
import { h, setText, setAttr, clear } from '../core/dom.js';
import { createHall, liveHallProvider } from '../components/hall.js';
import { panel, statusBadge, updateStatus, statusIcon, empty, meter, updateMeter } from '../components/ui.js';

const LOG_ROWS = 60;

function kpiCell(label, tip) {
  const value = h('span.kpi__value');
  const unit = h('span.kpi__unit');
  const sub = h('span.kpi__sub');
  const marker = statusIcon('warn', { size: 10 });
  marker.style.display = 'none';
  const node = h('div.kpi', { 'data-tip': tip }, h('span.kpi__label', label), h('span.kpi__main', value, unit, marker), sub);
  return {
    node,
    set(main, unitText, subText, { warn = false } = {}) {
      setText(value, main);
      setText(unit, unitText ?? '');
      setText(sub, subText ?? '');
      // The sub line can be ellipsized at 1280. Empty, the cell's own tip shows through.
      if (subText) setAttr(sub, 'data-tip', subText);
      else sub.removeAttribute('data-tip');
      marker.style.display = warn ? '' : 'none';
    },
  };
}

function buildKpis({ fmt }) {
  const cells = {
    oee: kpiCell('OEE Anlage', 'Mittel der Maschinen, netto: ohne Zeit, in der eine Maschine auf Material wartet'),
    power: kpiCell('Leistung', 'Aktuelle Gesamtleistung der Anlage'),
    energy: kpiCell('Energie', 'Verbrauch seit Laufbeginn'),
    specific: kpiCell('Spezifisch', 'Energie je versandtem Kilogramm'),
    shipped: kpiCell('Versendet', 'Versandte Pakete und Gewicht'),
    orders: kpiCell('Aufträge', 'Offene Aufträge und davon verspätet. „Gesamt“ zählt auch fertige Aufträge, die zu spät geliefert wurden.'),
    lots: kpiCell('Lose', 'Offene und fertige Fertigungslose'),
    plan: kpiCell('Plan', 'Aktive Planrevision und der Solver, der sie erzeugt hat'),
  };
  const node = h('section.kpis', { 'aria-label': 'Kennzahlen der Anlage' }, Object.values(cells).map((c) => c.node));
  return {
    node,
    update(state) {
      const k = state.kpi ?? {};
      const plan = state.plan;
      cells.oee.set(fmt.number((k.oee_plant ?? NaN) * 100), '%', null);
      cells.power.set(fmt.number(k.power_kw), 'kW');
      cells.energy.set(fmt.number(k.energy_kwh), 'kWh');
      cells.specific.set(fmt.number(k.kwh_per_kg, 2), 'kWh/kg');
      cells.shipped.set(fmt.number(k.packages), 'Pakete', fmt.kg(k.kg_shipped));
      const late = k.orders_late ?? 0;
      const lateText = late ? `${fmt.number(late)} verspätet` : 'keine verspätet';
      const total = k.orders_late_total;
      cells.orders.set(fmt.number(k.orders_open), 'offen', typeof total === 'number' && total > late ? `${lateText} · ${fmt.number(total)} gesamt` : lateText, { warn: late > 0 });
      cells.lots.set(fmt.number(k.lots_open), 'offen', `${fmt.number(k.lots_done)} fertig`);
      cells.plan.set(plan ? `Rev. ${plan.revision}` : fmt.DASH, null,
        plan ? `${fmt.solverLabel(plan.solver)} · ${fmt.planSourceLabel(plan.source)}` : '');
    },
  };
}

function buildStrip(ctx) {
  const { fmt } = ctx;
  const cells = new Map();
  const node = h('section.strip', { 'aria-label': 'Maschinenübersicht' });

  function cell(machine) {
    const badge = statusBadge(machine.status);
    const oeeMeter = meter({ label: 'OEE' });
    const oee = h('span.strip__num');
    const temp = h('span.strip__num');
    const power = h('span.strip__num');
    const code = h('span.strip__code');
    const lineText = h('span');
    const line = h('span.strip__line', lineText, code);
    const button = h('button.strip__cell', {
      type: 'button',
      onclick: () => ctx.drawer.open(machine.id),
    },
    h('span.strip__head', h('span.strip__id', machine.id), badge),
    h('span.strip__values', temp, power),
    h('span.strip__oee', oeeMeter, oee),
    line);
    node.appendChild(button);
    return { button, badge, oeeMeter, oee, temp, power, code, lineText };
  }

  return {
    node,
    update(state) {
      for (const machine of state.machines ?? []) {
        let c = cells.get(machine.id);
        if (!c) {
          c = cell(machine);
          cells.set(machine.id, c);
        }
        updateStatus(c.badge, machine.status);
        c.button.className = `strip__cell strip__cell--${fmt.statusTone(machine.status)}`;
        c.button.setAttribute('aria-label', `${machine.name}, ${fmt.statusLabel(machine.status)}, Details öffnen`);
        setText(c.temp, fmt.temp(machine.temperature));
        setText(c.power, fmt.kw(machine.power_kw));
        updateMeter(c.oeeMeter, machine.oee?.oee ?? null);
        setText(c.oee, fmt.percent(machine.oee?.oee));
        let detail;
        let tip = '';
        let faultCode = '';
        if (machine.status === 'FAULT') {
          detail = fmt.repairLeft(machine.est_repair_min, machine.status_since, ctx.store.clock.now()) ?? 'Reparatur läuft';
          // The overdue text is long enough on its own, and the hall still shows the code.
          if (machine.alarm?.code && detail !== fmt.REPAIR_OVERDUE) faultCode = `${machine.alarm.code} · `;
          tip = `${machine.name}: ${machine.alarm?.code ?? ''} ${machine.alarm?.text ?? ''}, ${detail}`;
        } else if (machine.op?.lot_ids?.length) detail = machine.op.lot_ids.length > 1 ? `${machine.op.lot_ids[0]} +${machine.op.lot_ids.length - 1}` : machine.op.lot_ids[0];
        else if (machine.op?.op_ids?.length) detail = machine.op.op_ids[0].replace(/:cast$/, '');
        else if (machine.waiting_on) {
          detail = fmt.waiting(machine.waiting_on);
          tip = fmt.waitingTip(machine.waiting_on);
        } else detail = fmt.DASH;
        setText(c.code, faultCode);
        setText(c.lineText, detail);
        setAttr(c.button, 'data-tip', tip || `${machine.name}: ${detail}`);
      }
    },
  };
}

function buildAlarms(ctx) {
  const { fmt, api, toast } = ctx;
  const box = panel('Aktive Alarme', { dense: true });
  const count = h('span.tag.tag--mono', '0');
  box.actions.appendChild(count);
  const tbody = h('tbody');
  const table = h('table.tbl',
    h('thead', h('tr', h('th', { 'aria-label': 'Schwere' }), h('th', 'Zeit'), h('th', 'Maschine'), h('th', 'Code'), h('th', 'Meldung'), h('th.num', 'Latenz'), h('th'))),
    tbody);
  const none = empty('Keine aktiven Alarme.');
  box.body.append(table, none);
  let key = '';
  const pending = new Set();

  async function ack(row, button) {
    pending.add(row.id);
    button.disabled = true;
    try {
      await api.post(`/api/alarms/${encodeURIComponent(row.id)}/ack`);
      toast(`Alarm ${row.code} quittiert.`, { kind: 'ok' });
    } catch {
      button.disabled = false;
    } finally {
      pending.delete(row.id);
    }
  }

  return {
    node: box.node,
    update(state) {
      const rows = state.alarms_active ?? [];
      const nextKey = rows.map((r) => `${r.id}:${r.acked}`).join('|');
      setText(count, rows.length);
      table.style.display = rows.length ? '' : 'none';
      none.style.display = rows.length ? 'none' : '';
      if (nextKey === key) return;
      key = nextKey;
      clear(tbody);
      for (const row of rows) {
        const button = h('button.btn.btn--sm', {
          type: 'button',
          disabled: row.acked || pending.has(row.id),
          'aria-label': `Alarm ${row.code} an ${row.machine} quittieren`,
          onclick: () => ack(row, button),
        }, row.acked ? 'Quittiert' : 'Quittieren');
        tbody.appendChild(h(`tr${row.severity >= 900 ? '.row--fault' : ''}`,
          h('td', statusIcon(row.severity >= 900 ? 'FAULT' : 'warn')),
          h('td.mono.nowrap', fmt.simTime(row.t)),
          h('td.mono', row.machine),
          h('td.mono', row.code),
          h('td', row.text),
          h('td.num', row.latency_ms === null || row.latency_ms === undefined ? fmt.DASH : `${fmt.number(row.latency_ms)} ms`),
          h('td.num', button)));
      }
    },
  };
}

function buildLog(ctx) {
  const { fmt, store } = ctx;
  const box = panel('Protokoll', { dense: true });
  const list = h('ol.log', { 'aria-live': 'off' });
  box.body.appendChild(list);

  function row(entry) {
    return h(`li.log__row.log__row--${entry.level ?? 'info'}`,
      entry.level === 'error' ? statusIcon('FAULT', { size: 10 }) : entry.level === 'warn' ? statusIcon('warn', { size: 10 }) : h('span.log__dot'),
      h('span.log__time.mono', fmt.simClock(entry.t)),
      h('span.log__source', fmt.logSourceLabel(entry.source)),
      h('span.log__text', { 'data-tip': entry.text }, entry.text));
  }

  function reset(entries) {
    clear(list);
    for (const entry of entries.slice(0, LOG_ROWS)) list.appendChild(row(entry));
  }

  reset(store.log);
  const offLog = store.on('log', (entry) => {
    list.insertBefore(row(entry), list.firstChild);
    while (list.children.length > LOG_ROWS) list.lastChild.remove();
  });
  const offReset = store.on('log-reset', reset);
  return { node: box.node, destroy() { offLog(); offReset(); } };
}

export function mount(el, ctx) {
  const { store } = ctx;
  el.classList.add('leitstand');

  const kpis = buildKpis(ctx);
  const hallHost = h('div.leitstand__hall');
  const strip = buildStrip(ctx);
  const alarms = buildAlarms(ctx);
  const log = buildLog(ctx);
  const stalled = h('div.leitstand__stalled', { role: 'alert', hidden: true },
    statusIcon('FAULT'), 'Die Anlage steht: keine laufende Operation, kein Transport, keine Störung, und der Plan ist nicht fertig.');

  el.append(kpis.node, stalled, hallHost, strip.node, h('div.leitstand__lower', alarms.node, log.node));

  const hall = createHall(hallHost, {
    provider: liveHallProvider(store),
    onSelect: (id) => ctx.drawer.open(id),
  });
  const offDrawer = ctx.drawer.onChange((id) => hall.setSelected(id));
  hall.setSelected(ctx.drawer.currentId);

  function paintLink() {
    const link = store.link();
    if (link.level === 'down') hall.setBanner(`${link.label}: Anzeige eingefroren, Werte sind nicht aktuell.`, 'fault');
    else if (link.level === 'stale') hall.setBanner('Daten veraltet: der Heartbeat der Anlage bleibt aus.', 'warn');
    else if (store.state && !store.state.plan) hall.setBanner('Startplan wird berechnet …', 'info');
    else hall.setBanner(null);
  }

  const offState = store.subscribe((state) => {
    kpis.update(state);
    strip.update(state);
    alarms.update(state);
    stalled.hidden = !state.stalled;
    paintLink();
  });
  const offConn = store.on('conn', paintLink);
  paintLink();

  return () => {
    offState();
    offConn();
    offDrawer();
    log.destroy();
    hall.destroy();
  };
}
