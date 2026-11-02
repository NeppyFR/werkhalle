// Machine drawer. Commands go out and the drawer then waits for the state to change: the MES only learns a fault from the
// OPC UA DataChange, so nothing here paints a machine red on its own.
import { h, setText, setAttr, clear } from '../core/dom.js';
import { fmt } from '../core/format.js';
import { icon, meter, sparkline, statusBadge, statusIcon, updateMeter, updateStatus } from './ui.js';

const REPAIR_CHOICES = [30, 60, 120, 240, 480, 960];
const DEFAULT_REPAIR = 240;
const CONFIRM_TIMEOUT_MS = 8000;
const OEE_REFRESH_MS = 5000;

export function createDrawer(root, ctx) {
  const { store, api, toast } = ctx;
  let currentId = null;
  let opener = null;
  let offState = null;
  let oeeTimer = 0;
  let oeeRow = null;
  const listeners = new Set();

  // One command at a time per machine, kept across closing and reopening the drawer.
  const commands = new Map();

  const title = h('h1.drawer__title');
  const idTag = h('span.tag.tag--mono');
  const badge = statusBadge('IDLE');
  const since = h('span.faint.mono');
  const closeButton = h('button.btn.btn--ghost.btn--icon', { type: 'button', 'aria-label': 'Details schließen', 'data-offline-ok': '', onclick: () => close() }, icon('close'));

  const opLines = {
    op: h('dd'), lot: h('dd'), order: h('dd'), product: h('dd'), phase: h('dd'), batch: h('dd'), next: h('dd'), waiting: h('dd'),
  };
  const progressBar = meter({ label: 'Fortschritt' });
  const progressText = h('span.mono');

  const tempValue = h('span.drawer__big');
  const tempSet = h('span.faint.mono');
  const tempSpark = sparkline({ label: 'Temperaturverlauf der letzten zwei Minuten', minSpan: 4 });
  const powerValue = h('span.drawer__big');
  const powerSpark = sparkline({ label: 'Leistungsverlauf der letzten zwei Minuten', minSpan: 20 });

  const counters = { total: h('dd'), good: h('dd'), scrap: h('dd'), energy: h('dd'), setup: h('dd') };
  const unitLabel = h('dt', 'Gesamt');

  const oee = {
    value: h('span.drawer__big'),
    iso: h('dd'),
    a: h('dd'), p: h('dd'), q: h('dd'),
    aBar: meter({ label: 'Verfügbarkeit' }), pBar: meter({ label: 'Leistung' }), qBar: meter({ label: 'Qualität' }),
  };

  const reliability = { faults: h('dd'), mtbf: h('dd'), mttr: h('dd'), catalog: h('p.faint.drawer__note') };

  const alarmBox = h('div.drawer__alarm');

  const codeSelect = h('select.input', { 'aria-label': 'Störcode' });
  const repairSelect = h('select.input', { 'aria-label': 'Reparaturdauer' });
  for (const minutes of REPAIR_CHOICES) {
    repairSelect.appendChild(h('option', { value: String(minutes), selected: minutes === DEFAULT_REPAIR }, minutes < 60 ? fmt.duration(minutes) : `${fmt.duration(minutes)} (${minutes} min)`));
  }
  const faultButton = h('button.btn.btn--danger', { type: 'button', onclick: () => sendFault() }, 'Störung auslösen');
  const repairButton = h('button.btn', { type: 'button', onclick: () => sendRepair() }, 'Instandsetzung abschließen');
  const commandNote = h('p.drawer__command', { role: 'status', tabindex: '-1' });

  const body = h('div.drawer__body',
    section('Aktuelle Operation',
      h('div.drawer__progress', progressBar, progressText),
      h('dl.kv',
        h('dt', 'Operation'), opLines.op,
        h('dt', 'Phase'), opLines.phase,
        h('dt', 'Los'), opLines.lot,
        h('dt', 'Auftrag'), opLines.order,
        h('dt', 'Produkt'), opLines.product,
        h('dt', 'Ladung'), opLines.batch,
        h('dt', 'Nächste'), opLines.next,
        h('dt', 'Wartet auf'), opLines.waiting)),
    alarmBox,
    section('Temperatur',
      h('div.drawer__valueRow', tempValue, tempSet),
      h('div.drawer__spark', tempSpark.node)),
    section('Leistung',
      h('div.drawer__valueRow', powerValue),
      h('div.drawer__spark', powerSpark.node)),
    section('Zähler',
      h('dl.kv',
        unitLabel, counters.total,
        h('dt', 'Gut'), counters.good,
        h('dt', 'Ausschuss'), counters.scrap,
        h('dt', 'Energie'), counters.energy,
        h('dt', 'Rüstklasse'), counters.setup)),
    section('OEE',
      h('div.drawer__valueRow', oee.value, h('span.faint', 'netto')),
      h('dl.drawer__factors',
        h('dt', 'Verfügbarkeit'), oee.a, oee.aBar,
        h('dt', 'Leistung'), oee.p, oee.pBar,
        h('dt', 'Qualität'), oee.q, oee.qBar),
      h('dl.kv', h('dt', 'OEE nach ISO 22400'), oee.iso)),
    section('Zuverlässigkeit',
      h('dl.kv',
        h('dt', 'Störungen'), reliability.faults,
        h('dt', 'MTBF'), reliability.mtbf,
        h('dt', 'MTTR'), reliability.mttr),
      reliability.catalog));

  // The command area stays in view however far the details scroll.
  const footer = h('footer.drawer__foot',
    h('div.drawer__fields',
      h('label.field', h('span.field__label', 'Störcode'), codeSelect),
      h('label.field', h('span.field__label', 'Reparaturdauer'), repairSelect)),
    h('div.drawer__actions', faultButton, repairButton),
    commandNote);

  root.append(
    h('header.drawer__head',
      h('div.drawer__headRow', title, closeButton),
      h('div.drawer__headRow', idTag, badge, since)),
    body,
    footer);

  // On the document, because a command disables the button that has the focus and the key would otherwise go nowhere.
  document.addEventListener('keydown', (event) => {
    if (event.key === 'Escape' && currentId) close();
  });

  function section(heading, ...children) {
    return h('section.drawer__section', h('h2.drawer__heading', heading), children);
  }

  function fillCatalog(id) {
    clear(codeSelect);
    const machine = store.catalog?.machines?.find((m) => m.id === id);
    const faults = machine?.faults ?? [];
    for (const fault of faults) {
      codeSelect.appendChild(h('option', { value: fault.code }, `${fault.code} · ${fault.text}`));
    }
    if (!faults.length) codeSelect.appendChild(h('option', { value: '' }, 'Standardstörung'));
    setText(reliability.catalog, faults.length
      ? `Mittlere Reparaturzeit laut Katalog: ${faults.map((f) => `${f.code} ${fmt.duration(f.repair_mean_min)}`).join(', ')}.`
      : '');
  }

  async function refreshReliability(id) {
    try {
      const data = await api.get('/api/oee?window=start', { quiet: true });
      if (id !== currentId) return;
      oeeRow = data.machines?.find((m) => m.id === id) ?? null;
      paintReliability();
    } catch {
      /* the state row still shows the fault count */
    }
  }

  function paintReliability() {
    const machine = store.machine(currentId);
    setText(reliability.faults, fmt.number(machine?.fault_count ?? oeeRow?.fault_count));
    setText(reliability.mtbf, fmt.duration(oeeRow?.mtbf_min));
    setText(reliability.mttr, fmt.duration(oeeRow?.mttr_min));
  }

  async function sendFault() {
    const id = currentId;
    const machine = store.machine(id);
    if (!machine) return;
    commands.set(id, { type: 'fault', phase: 'sending', since: performance.now() });
    paintCommand(machine);
    commandNote.focus({ preventScroll: true });
    try {
      const result = await api.post(`/api/machines/${encodeURIComponent(id)}/fault`, {
        code: codeSelect.value,
        repair_min: Number(repairSelect.value),
      }, { quiet: [409] });
      const command = commands.get(id);
      if (command?.type === 'fault' && command.phase === 'sending') {
        command.phase = 'sent';
        command.sentWall = result?.sent_wall_ts;
      }
    } catch (error) {
      commands.set(id, { type: 'fault', phase: 'refused', since: performance.now(), message: error.message });
    }
    if (id === currentId) paintCommand(store.machine(id));
  }

  async function sendRepair() {
    const id = currentId;
    const machine = store.machine(id);
    if (!machine) return;
    commands.set(id, { type: 'repair', phase: 'sending', since: performance.now() });
    paintCommand(machine);
    commandNote.focus({ preventScroll: true });
    try {
      await api.post(`/api/machines/${encodeURIComponent(id)}/repair`, {}, { quiet: [409] });
      const command = commands.get(id);
      if (command?.type === 'repair' && command.phase === 'sending') command.phase = 'sent';
    } catch (error) {
      commands.set(id, { type: 'repair', phase: 'refused', since: performance.now(), message: error.message });
    }
    if (id === currentId) paintCommand(store.machine(id));
  }

  // Advances commands from what the state reports. Runs for every machine, so a confirmation also lands with the drawer closed.
  function trackCommands(state) {
    for (const machine of state.machines ?? []) {
      const command = commands.get(machine.id);
      if (!command) continue;
      if (command.type === 'fault' && (command.phase === 'sent' || command.phase === 'sending') && machine.status === 'FAULT') {
        commands.set(machine.id, { type: 'fault', phase: 'confirmed', since: performance.now(), code: machine.alarm?.code });
      } else if (command.type === 'repair' && (command.phase === 'sent' || command.phase === 'sending') && machine.status !== 'FAULT') {
        commands.set(machine.id, { type: 'repair', phase: 'confirmed', since: performance.now() });
      } else if (command.phase === 'sent' && performance.now() - command.since > CONFIRM_TIMEOUT_MS) {
        commands.set(machine.id, { type: command.type, phase: 'timeout', since: performance.now() });
      } else if ((command.phase === 'confirmed' || command.phase === 'refused' || command.phase === 'timeout') && performance.now() - command.since > 15000) {
        commands.delete(machine.id);
      }
    }
  }

  function paintCommand(machine) {
    const command = commands.get(currentId);
    const faulted = machine?.status === 'FAULT';
    const waiting = command && (command.phase === 'sending' || command.phase === 'sent');
    faultButton.disabled = faulted || waiting;
    repairButton.disabled = !faulted || waiting;
    codeSelect.disabled = faulted || waiting;
    repairSelect.disabled = faulted || waiting;
    faultButton.setAttribute('data-tip', faulted ? 'Die Maschine ist bereits gestört.' : 'Ruft InjectFault per OPC UA auf. Die Maschine wird erst rot, wenn der DataChange zurückkommt.');
    repairButton.setAttribute('data-tip', faulted ? 'Ruft Repair per OPC UA auf.' : 'Nur bei einer gestörten Maschine möglich.');

    let message = '';
    let tone = '';
    if (command) {
      if (command.type === 'fault') {
        if (command.phase === 'sending') message = 'Befehl wird gesendet …';
        else if (command.phase === 'sent') message = 'Befehl per OPC UA gesendet, warte auf DataChange.';
        else if (command.phase === 'confirmed') { message = `DataChange empfangen: Status FAULT${command.code ? `, ${command.code}` : ''}.`; tone = 'fault'; }
        else if (command.phase === 'timeout') { message = 'Kein DataChange innerhalb von 8 Sekunden. Status prüfen, die Verbindung kann gestört sein.'; tone = 'warn'; }
        else if (command.phase === 'refused') { message = `Abgelehnt: ${command.message}`; tone = 'warn'; }
      } else {
        if (command.phase === 'sending') message = 'Befehl wird gesendet …';
        else if (command.phase === 'sent') message = 'Instandsetzung per OPC UA gesendet, warte auf DataChange.';
        else if (command.phase === 'confirmed') { message = 'DataChange empfangen: Störung behoben.'; tone = 'ok'; }
        else if (command.phase === 'timeout') { message = 'Kein DataChange innerhalb von 8 Sekunden.'; tone = 'warn'; }
        else if (command.phase === 'refused') { message = `Abgelehnt: ${command.message}`; tone = 'warn'; }
      }
    }
    setText(commandNote, message);
    commandNote.className = `drawer__command${tone ? ` drawer__command--${tone}` : ''}`;
  }

  function paint(state) {
    trackCommands(state);
    if (!currentId) return;
    const machine = state.machines?.find((m) => m.id === currentId);
    if (!machine) return;
    const t = store.clock.now();

    setText(title, machine.name);
    setText(idTag, machine.id);
    updateStatus(badge, machine.status);
    setText(since, typeof machine.status_since === 'number' ? `seit ${fmt.duration(Math.max(0, t - machine.status_since))}` : '');
    root.dataset.tone = fmt.statusTone(machine.status);

    const op = machine.op;
    updateMeter(progressBar, op ? machine.progress : 0, fmt.statusTone(machine.status));
    setText(progressText, op ? fmt.percent(machine.progress) : fmt.DASH);
    setText(opLines.op, op ? fmt.ops(op.op_ids, op.kind) : 'keine');
    setText(opLines.phase, op ? fmt.statusLabel(op.phase) : fmt.DASH);
    setText(opLines.lot, op?.lot_ids?.length ? op.lot_ids.map(fmt.lotId).join(', ') : fmt.DASH);
    setText(opLines.order, op?.order_ids?.length ? op.order_ids.map(fmt.orderId).join(', ') : fmt.DASH);
    setText(opLines.product, op?.product_id ? `${fmt.productName(op.product_id)}` : fmt.DASH);
    setText(opLines.batch, fmt.lotId(op?.batch_id));
    setText(opLines.next, fmt.opId(machine.next_op));
    setText(opLines.waiting, machine.waiting_on ?? fmt.DASH);
    const waitingWhy = fmt.waitingTip(machine.waiting_on);
    if (waitingWhy) setAttr(opLines.waiting, 'data-tip', waitingWhy);
    else opLines.waiting.removeAttribute('data-tip');

    clear(alarmBox);
    if (machine.alarm?.active) {
      const severe = machine.alarm.severity >= 900;
      alarmBox.className = `drawer__alarm drawer__alarm--${severe ? 'fault' : 'warn'}`;
      alarmBox.append(
        statusIcon(severe ? 'FAULT' : 'warn', { size: 14 }),
        h('div',
          h('strong.mono', machine.alarm.code),
          h('span', ` ${machine.alarm.text}`),
          h('div.mono.drawer__alarmSub', severe
            ? `${fmt.repairLeft(machine.est_repair_min, machine.status_since, t, { long: true }) ?? 'Reparatur läuft'} · Schwere ${machine.alarm.severity}`
            : `Warnung · Schwere ${machine.alarm.severity}`)));
    } else {
      alarmBox.className = 'drawer__alarm';
    }

    setText(tempValue, fmt.temp(machine.temperature));
    setText(tempSet, typeof machine.setpoint === 'number' ? `Soll ${fmt.temp(machine.setpoint)}` : '');
    tempSpark.update(machine.trend?.temperature, { reference: machine.setpoint });
    setText(powerValue, fmt.kw(machine.power_kw));
    powerSpark.update(machine.trend?.power_kw);

    setText(unitLabel, `Gesamt (${machine.unit})`);
    setText(counters.total, fmt.number(machine.total));
    setText(counters.good, fmt.number(machine.good));
    setText(counters.scrap, fmt.number(machine.scrap));
    setText(counters.energy, fmt.kwh(machine.energy_kwh));
    setText(counters.setup, fmt.setupClass(machine.setup_class));

    const figures = machine.oee ?? {};
    setText(oee.value, fmt.percent(figures.oee));
    setText(oee.iso, fmt.percent(figures.oee_iso));
    setText(oee.a, fmt.percent(figures.availability));
    setText(oee.p, fmt.percent(figures.performance));
    setText(oee.q, fmt.percent(figures.quality));
    updateMeter(oee.aBar, figures.availability ?? null);
    updateMeter(oee.pBar, figures.performance ?? null);
    updateMeter(oee.qBar, figures.quality ?? null);

    paintReliability();
    paintCommand(machine);
  }

  function open(id) {
    if (currentId !== id) {
      commandNote.textContent = '';
      oeeRow = null;
    }
    opener = document.activeElement;
    currentId = id;
    root.hidden = false;
    fillCatalog(id);
    refreshReliability(id);
    clearInterval(oeeTimer);
    oeeTimer = setInterval(() => refreshReliability(id), OEE_REFRESH_MS);
    if (store.state) paint(store.state);
    closeButton.focus({ preventScroll: true });
    for (const listener of listeners) listener(id);
  }

  function close() {
    if (!currentId) return;
    currentId = null;
    root.hidden = true;
    clearInterval(oeeTimer);
    for (const listener of listeners) listener(null);
    if (opener?.isConnected) opener.focus({ preventScroll: true });
    opener = null;
  }

  offState = store.subscribe(paint);
  store.on('catalog', () => {
    if (currentId) fillCatalog(currentId);
  });
  void offState;

  return {
    open,
    close,
    get currentId() { return currentId; },
    /** fn(machineId | null) when the drawer opens or closes. Returns unsubscribe. */
    onChange(fn) {
      listeners.add(fn);
      return () => listeners.delete(fn);
    },
  };
}
