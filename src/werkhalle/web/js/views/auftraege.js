// Aufträge: all orders with status, dates and progress, an expanding row per order with its lots, and the form for a new order.
import { h, setText, clear, loadCss } from '../core/dom.js';
import { panel, statusIcon, paintIcon, meter, updateMeter, icon } from '../components/ui.js';
import { formatNumber } from '../core/format.js';

const REFRESH_MS = 3000;
const PLAN_WAIT_MS = 45000;
const PLAN_PATIENCE_MS = 1200;
const HIGHLIGHT_MS = 8000;
const MAX_DUE_MIN = 100000;  // the API's limit for due_in_min
const STATUS = {
  offen: { label: 'Offen', icon: 'IDLE', tone: 'idle' },
  in_arbeit: { label: 'In Arbeit', icon: 'RUN', tone: 'run' },
  fertig: { label: 'Fertig', icon: 'ok', tone: 'idle' },
  unterlieferung: { label: 'Unterlieferung', icon: 'warn', tone: 'setup' },
};
const STAGES = ['press', 'age', 'saw', 'pack'];
const FILTERS = [
  { key: 'open', label: 'Offen', test: (o) => o.status === 'offen' || o.status === 'in_arbeit' },
  { key: 'late', label: 'Verspätet', test: (o) => o.late },
  { key: 'done', label: 'Fertig', test: (o) => o.status === 'fertig' || o.status === 'unterlieferung' },
  { key: 'all', label: 'Alle', test: () => true },
];

// A filter choice survives a view change.
const memory = { filter: 'open' };

function buildStageCell(fmt) {
  const shape = statusIcon('IDLE', { size: 10 });
  const label = h('span.auftraege-stage__label');
  const node = h('span.auftraege-stage', shape, label);
  return {
    node,
    set(stage, mode, machine) {
      node.className = `auftraege-stage auftraege-stage--${mode}`;
      paintIcon(shape, mode === 'done' ? 'ok' : mode === 'running' ? 'RUN' : 'IDLE');
      setText(label, mode === 'running' && machine ? `${fmt.opKindLabel(stage)} · ${machine}` : fmt.opKindLabel(stage));
    },
  };
}

function lotSummary(lot, fmt) {
  if (lot.stage === 'fertig' || lot.state === 'fertig') return 'fertig';
  const stage = fmt.opKindLabel(lot.stage);
  if (lot.state === 'laeuft') return lot.machine ? `${stage} läuft auf ${lot.machine}` : `${stage} läuft`;
  return lot.machine ? `wartet auf ${stage}, ${lot.machine}` : `wartet auf ${stage}`;
}

function stageMode(lot, stage) {
  if (lot.stage === 'fertig' || lot.state === 'fertig') return 'done';
  const current = STAGES.indexOf(lot.stage);
  const index = STAGES.indexOf(stage);
  if (index < current) return 'done';
  if (index > current) return 'future';
  return lot.state === 'laeuft' ? 'running' : 'waiting';
}

/** What is wrong with the typed quantity, or null. A fraction is refused, not rounded: the operator would book another order than the one typed. */
export function quantityProblem(raw) {
  const qty = Number(raw);
  if (raw === '' || !Number.isFinite(qty) || qty <= 0) return 'Die Menge muss größer als null sein.';
  if (!Number.isInteger(qty)) return 'Die Menge muss eine ganze Zahl von Stangen sein.';
  return null;
}

/** What is wrong with the typed due date, in the hours the field asks for, or null. */
export function dueProblem(raw) {
  const hours = Number(raw);
  if (raw === '' || !Number.isFinite(hours) || hours <= 0) return 'Der Termin muss in der Zukunft liegen.';
  if (Math.round(hours * 60) > MAX_DUE_MIN) return `Der Termin darf höchstens ${formatNumber(Math.floor(MAX_DUE_MIN / 60))} Stunden ab jetzt liegen.`;
  return null;
}

export function mount(el, ctx) {
  loadCss('css/auftraege.css');
  const { store, api, fmt, router, toast } = ctx;
  el.classList.add('auftraege');

  const rows = new Map();
  const expanded = new Set();
  let orders = [];
  let highlightId = router.params.order ?? null;
  let highlightTimer = 0;
  let refreshTimer = 0;
  let inFlight = false;
  let unmounted = false;
  let shownKey = '';
  let loaded = false;
  let scrollPending = Boolean(highlightId);
  if (highlightId) expanded.add(highlightId);

  // ------------------------------------------------------------------ table
  const tbody = h('tbody');
  const none = h('p.empty', 'Lade Aufträge …');
  const table = h('table.tbl.auftraege-tbl',
    h('thead', h('tr',
      h('th', 'Auftrag'),
      h('th', 'Status'),
      h('th', 'Produkt'),
      h('th.num', { 'data-tip': 'Bestellte Menge in Stangen' }, 'Menge'),
      h('th.num', { 'data-tip': 'Gelieferte Stück, 1 Stück = 1 Stange' }, 'Geliefert'),
      h('th', 'Termin'),
      h('th', { 'data-tip': 'Tatsächlich, sobald der Auftrag fertig ist, sonst laut aktivem Plan' }, 'Fertigstellung'),
      h('th.num', { 'data-tip': 'Fertigstellung minus Termin. Negativ heißt Reserve.' }, 'Abweichung'),
      h('th', 'Fortschritt'),
      h('th', { 'aria-label': 'Verweise' }))),
    tbody);
  const filterSeg = h('div.seg.auftraege__seg', { role: 'group', 'aria-label': 'Aufträge filtern' });
  const filterButtons = new Map();
  for (const filter of FILTERS) {
    const count = h('span.auftraege__count');
    const button = h('button.seg__btn', {
      type: 'button',
      'aria-pressed': String(filter.key === memory.filter),
      onclick: () => {
        memory.filter = filter.key;
        for (const [key, node] of filterButtons) node.setAttribute('aria-pressed', String(key === filter.key));
        paintRows();
      },
    }, `${filter.label} `, count);
    button._count = count;
    filterButtons.set(filter.key, button);
    filterSeg.appendChild(button);
  }
  const tableBox = panel('Aufträge', { dense: true, actions: [filterSeg] });
  tableBox.body.append(table, none);

  function buildRow(order) {
    const toggle = h('button.auftraege__toggle', { type: 'button', 'aria-expanded': 'false', onclick: () => toggleRow(order.id) }, icon('chevron', 14));
    const idText = h('span.mono.auftraege__id', order.id);
    const rush = h('span.tag', { 'data-tip': 'Eilauftrag: Verspätung zählt mit Gewicht 3' }, 'Eil');
    const statusShape = statusIcon('IDLE', { size: 11 });
    const statusText = h('span');
    const statusCell = h('span.auftraege__status', statusShape, statusText);
    const product = h('td.auftraege__product');
    const qty = h('td.num');
    const delivered = h('td.num');
    const due = h('td.mono.nowrap');
    const completion = h('td.mono.nowrap');
    const lateMark = statusIcon('warn', { size: 10 });
    const lateText = h('span');
    const lateCell = h('td.num.nowrap', lateMark, lateText);
    const progressMeter = meter({ label: 'Lose fertig' });
    const progressText = h('span.mono.auftraege__lots');
    const main = h('tr.auftraege__row', {
      dataset: { order: order.id },
    },
    h('td.nowrap', h('span.auftraege__idcell', toggle, idText, rush)),
    h('td.nowrap', statusCell),
    product, qty, delivered, due, completion, lateCell,
    h('td', h('span.auftraege__progress', progressMeter, progressText)),
    h('td.auftraege__links',
      h('a.btn.btn--ghost.btn--sm.btn--icon', { href: router.href('planung', { order: order.id }), 'aria-label': `${order.id} in der Planung zeigen`, 'data-tip': 'In der Planung zeigen' }, icon('gantt', 14)),
      h('a.btn.btn--ghost.btn--sm.btn--icon', { href: router.href('rueckverfolgung', { q: order.id }), 'aria-label': `${order.id} rückverfolgen`, 'data-tip': 'Rückverfolgung' }, icon('trace', 14))));
    const lotsHost = h('div.auftraege__lotsBody');
    const detailCell = h('td', { colSpan: 10 }, lotsHost);
    const detail = h('tr.auftraege__detail', { hidden: true }, detailCell);
    const refs = { main, detail, toggle, rush, statusShape, statusText, product, qty, delivered, due, completion, lateMark, lateText, lateCell, progressMeter, progressText, lotsHost, lotRows: new Map() };
    main.addEventListener('click', (event) => {
      if (event.target.closest('a, button')) return;
      toggleRow(order.id);
    });
    return refs;
  }

  function updateLots(refs, order) {
    const seen = new Set();
    for (const lot of order.lots ?? []) {
      seen.add(lot.id);
      let row = refs.lotRows.get(lot.id);
      if (!row) {
        const cells = STAGES.map(() => buildStageCell(fmt));
        const summary = h('span.auftraege-lot__summary');
        row = {
          cells,
          summary,
          billets: h('span.auftraege-lot__billets.mono'),
          node: h('div.auftraege-lot', h('span.mono.auftraege-lot__id', lot.id)),
        };
        row.node.append(row.billets, h('span.auftraege-lot__stages', cells.map((c) => c.node)), summary,
          h('a.btn.btn--ghost.btn--sm', { href: router.href('rueckverfolgung', { q: lot.id }) }, 'Rückverfolgung'));
        refs.lotRows.set(lot.id, row);
        refs.lotsHost.appendChild(row.node);
      }
      setText(row.billets, fmt.plural(lot.n_billets, 'Bolzen', 'Bolzen'));
      STAGES.forEach((stage, i) => row.cells[i].set(stage, stageMode(lot, stage), lot.machine));
      setText(row.summary, lotSummary(lot, fmt));
    }
    for (const [id, row] of refs.lotRows) {
      if (!seen.has(id)) {
        row.node.remove();
        refs.lotRows.delete(id);
      }
    }
  }

  function updateRow(refs, order) {
    const status = STATUS[order.status] ?? { label: order.status, icon: 'IDLE', tone: 'idle' };
    refs.rush.hidden = !(order.weight > 1);
    paintIcon(refs.statusShape, status.icon);
    setText(refs.statusText, status.label);
    setText(refs.product, order.product_name ?? fmt.productName(order.product_id));
    refs.product.dataset.tip = order.product_name ?? fmt.productName(order.product_id);
    setText(refs.qty, fmt.number(order.qty_bars));
    setText(refs.delivered, fmt.number(order.delivered_pieces));
    setText(refs.due, fmt.simTime(order.due_min));
    const finished = order.completed_min ?? order.planned_completion;
    setText(refs.completion, fmt.simTime(finished));
    refs.completion.classList.toggle('faint', order.completed_min === null || order.completed_min === undefined);
    const lateness = order.lateness_min;
    setText(refs.lateText, lateness === null || lateness === undefined ? fmt.DASH : fmt.duration(lateness, { signed: true }));
    refs.lateMark.style.display = order.late ? '' : 'none';
    refs.lateCell.classList.toggle('auftraege__late', Boolean(order.late));
    updateMeter(refs.progressMeter, order.lots_total ? order.lots_done / order.lots_total : 0);
    setText(refs.progressText, `${fmt.number(order.lots_done)}/${fmt.number(order.lots_total)}`);
    refs.main.classList.toggle('is-selected', order.id === highlightId);
    if (expanded.has(order.id)) updateLots(refs, order);
  }

  function toggleRow(id) {
    if (expanded.has(id)) expanded.delete(id);
    else expanded.add(id);
    const refs = rows.get(id);
    const order = orders.find((o) => o.id === id);
    if (!refs || !order) return;
    applyExpansion(refs, order);
  }

  function applyExpansion(refs, order) {
    const open = expanded.has(order.id);
    refs.detail.hidden = !open;
    refs.toggle.setAttribute('aria-expanded', String(open));
    refs.toggle.setAttribute('aria-label', `Lose von ${order.id} ${open ? 'ausblenden' : 'anzeigen'}`);
    refs.main.classList.toggle('is-open', open);
    if (open) updateLots(refs, order);
  }

  function paintRows() {
    const filter = FILTERS.find((f) => f.key === memory.filter) ?? FILTERS[0];
    for (const candidate of FILTERS) {
      setText(filterButtons.get(candidate.key)._count, fmt.number(orders.filter(candidate.test).length));
    }
    // A highlighted order always stays in view, also if the filter would hide it.
    const shown = orders.filter((o) => filter.test(o) || o.id === highlightId);
    for (const order of shown) {
      let refs = rows.get(order.id);
      if (!refs) {
        refs = buildRow(order);
        rows.set(order.id, refs);
      }
      updateRow(refs, order);
      applyExpansion(refs, order);
    }
    const key = shown.map((o) => o.id).join('|');
    if (key !== shownKey) {
      shownKey = key;
      clear(tbody);
      for (const order of shown) {
        const refs = rows.get(order.id);
        tbody.append(refs.main, refs.detail);
      }
    }
    table.hidden = shown.length === 0;
    none.hidden = shown.length > 0;
    if (loaded) setText(none, orders.length ? `Keine Aufträge im Filter „${filter.label}“.` : 'Noch keine Aufträge.');
    if (scrollPending && highlightId && rows.get(highlightId)?.main.isConnected) {
      scrollPending = false;
      // Scrolls the table only. scrollIntoView would also move the page frame, whose overflow:hidden does not stop programmatic scrolling.
      const row = rows.get(highlightId).main.getBoundingClientRect();
      const scroller = tableBox.body.getBoundingClientRect();
      tableBox.body.scrollTop += row.top - scroller.top - (scroller.height - row.height) / 2;
    }
  }

  async function refresh() {
    if (inFlight || unmounted) return;
    inFlight = true;
    try {
      const data = await api.get('/api/orders', { quiet: true });
      if (unmounted) return;
      orders = data.orders ?? [];
      if (!loaded && highlightId) showHighlighted();
      loaded = true;
      paintRows();
    } catch {
      /* the connection indicator tells the story, and the next tick tries again */
    } finally {
      inFlight = false;
    }
  }

  // A deep link to a finished order must not lose it when the highlight fades: the filter widens first.
  function showHighlighted() {
    const order = orders.find((o) => o.id === highlightId);
    const filter = FILTERS.find((f) => f.key === memory.filter);
    if (!order || filter?.test(order)) return;
    memory.filter = 'all';
    for (const [key, node] of filterButtons) node.setAttribute('aria-pressed', String(key === 'all'));
  }

  function setHighlight(id) {
    highlightId = id;
    clearTimeout(highlightTimer);
    highlightTimer = setTimeout(() => {
      highlightId = null;
      paintRows();
    }, HIGHLIGHT_MS);
  }

  // ------------------------------------------------------------------ new order form
  const productSelect = h('select.input', { id: 'auftraege-product', required: true });
  const qtyInput = h('input.input', { id: 'auftraege-qty', type: 'number', min: 1, step: 1, value: 200, required: true, inputMode: 'numeric' });
  const dueInput = h('input.input', { id: 'auftraege-due', type: 'number', min: 0, step: 0.5, value: 10, required: true, inputMode: 'decimal' });
  const rushInput = h('input', { id: 'auftraege-rush', type: 'checkbox' });
  const productInfo = h('p.auftraege-form__info');
  const dueInfo = h('p.auftraege-form__info', { id: 'auftraege-due-info' });
  const leadNote = h('div.auftraege-form__note', { hidden: true, role: 'status' });
  const errorBox = h('div.auftraege-form__error', { hidden: true, role: 'alert' });
  const submit = h('button.btn.btn--primary', { type: 'submit' }, 'Auftrag anlegen');
  const form = h('form.auftraege-form', { onsubmit: onSubmit, noValidate: true },
    h('label.field', h('span.field__label', 'Produkt'), productSelect),
    productInfo,
    h('label.field', h('span.field__label', 'Menge in Stangen'), qtyInput),
    h('label.field', h('span.field__label', 'Termin in Stunden ab jetzt'), dueInput),
    dueInfo,
    leadNote,
    h('label.auftraege-form__check', rushInput, h('span', 'Eilauftrag', h('span.faint', ' zählt in der Verspätung dreifach'))),
    errorBox,
    submit);
  dueInput.setAttribute('aria-describedby', 'auftraege-due-info');
  const formBox = panel('Neuer Auftrag');
  formBox.body.appendChild(form);

  el.append(tableBox.node, h('div.auftraege__side', formBox.node));

  function currentProduct() {
    return store.catalog?.products?.find((p) => p.id === productSelect.value) ?? null;
  }

  function fillProducts() {
    const products = store.catalog?.products ?? [];
    if (!products.length || productSelect.options.length) return;
    for (const product of products) {
      productSelect.appendChild(h('option', { value: product.id }, product.name));
    }
    if (products.some((p) => p.id === 'KK-80')) productSelect.value = 'KK-80';
    paintForm();
  }

  function setDueToLead(lead) {
    dueInput.value = String(Math.ceil(lead / 30) / 2);
    errorBox.hidden = true;
    paintForm();
  }

  function paintForm() {
    const product = currentProduct();
    const qty = Number(qtyInput.value);
    const hours = Number(dueInput.value);
    const dueMin = Math.round(hours * 60);
    if (product) {
      const kg = Number.isFinite(qty) && qty > 0 ? `${fmt.kg(qty * product.kg_per_bar)} · ` : '';
      const onlyPress = product.presses?.length === 1 ? `nur ${product.presses[0]}` : product.presses?.join(', ');
      setText(productInfo, `${kg}Legierung ${product.alloy} · Zustand ${product.recipe} · ${onlyPress}`);
    } else {
      setText(productInfo, '');
    }
    const lead = product?.min_lead_min ?? null;
    const now = store.clock.now() ?? store.state?.sim?.t ?? null;
    const tooFar = Number.isFinite(hours) && dueMin > MAX_DUE_MIN;
    setText(dueInfo, tooFar ? dueProblem(dueInput.value) : Number.isFinite(hours) && now !== null ? `Termin ${fmt.simTime(now + dueMin)}${lead !== null ? ` · Mindestvorlauf ${fmt.duration(lead)}` : ''}` : '');
    const tooSoon = lead !== null && Number.isFinite(hours) && dueMin < lead;
    // The API's own refusal says the same thing, more firmly, and then stands alone.
    leadNote.hidden = !tooSoon || !errorBox.hidden;
    if (tooSoon) {
      clear(leadNote);
      leadNote.append(statusIcon('warn', { size: 11 }),
        h('span', `Der Termin liegt ${fmt.duration(lead - dueMin)} unter dem Mindestvorlauf für ${product.id}, auch in einer leeren Anlage nicht zu schaffen.`),
        h('button.btn.btn--sm', { type: 'button', onclick: () => setDueToLead(lead) }, 'Frühestmöglich'));
    }
  }

  function showError(message, lead) {
    clear(errorBox);
    errorBox.hidden = false;
    leadNote.hidden = true;
    errorBox.append(statusIcon('FAULT', { size: 11 }), h('span', message));
    if (typeof lead === 'number') {
      errorBox.append(h('button.btn.btn--sm', { type: 'button', onclick: () => setDueToLead(lead) }, `Auf ${fmt.duration(lead)} setzen`));
    }
  }

  // Tells what the new order did to the plan. The POST answers first, the rolling re-plan is applied seconds later. `replan` names the
  // proposal that holds it back.
  function announce(order, replan, revisionBefore) {
    const holding = replan ?? store.proposals.open?.id;
    if (holding) {
      toast(`Auftrag ${order.id} angelegt. Vorschlag ${holding} ist offen, die Lose warten auf den nächsten Plan.`, { kind: 'warn' });
      return;
    }
    const includes = (plan) => plan.revision > revisionBefore
      && (plan.metrics?.order_completion?.[order.id] !== undefined || plan.assignments?.some((a) => a.order_id === order.id));
    const finish = (plan) => {
      const completion = plan.metrics?.order_completion?.[order.id] ?? order.planned_completion;
      const due = order.due_min;
      const reserve = typeof completion === 'number' ? completion - due : null;
      const outcome = reserve === null
        ? ''
        : reserve > 0
          ? `, fertig ${fmt.simTime(completion)}, verspätet ${fmt.duration(reserve, { signed: true })}`
          : `, fertig ${fmt.simTime(completion)}, ${fmt.duration(-reserve)} vor Termin`;
      toast(`Auftrag ${order.id} angelegt. Plan Revision ${fmt.number(plan.revision)} (${fmt.solverLabel(plan.solver)})${outcome}.`, { kind: reserve > 0 ? 'warn' : 'ok' });
    };
    if (store.plan && includes(store.plan)) {
      finish(store.plan);
      return;
    }
    // Keeps listening after the view is left: the toast is global, and the answer is still worth having.
    const patience = setTimeout(() => toast(`Auftrag ${order.id} angelegt. Der Plan wird fortgeschrieben …`, { kind: 'info' }), PLAN_PATIENCE_MS);
    let off = () => {};
    const giveUp = setTimeout(() => {
      off();
      toast(`Auftrag ${order.id} ist angelegt, aber noch in keinem Plan. Das Protokoll im Leitstand sagt, warum.`, { kind: 'warn' });
    }, PLAN_WAIT_MS);
    off = store.on('plan', (plan) => {
      if (!includes(plan)) return;
      clearTimeout(patience);
      clearTimeout(giveUp);
      off();
      finish(plan);
    });
  }

  async function onSubmit(event) {
    event.preventDefault();
    errorBox.hidden = true;
    const qty = Number(qtyInput.value);
    const hours = Number(dueInput.value);
    if (!productSelect.value) {
      showError('Bitte ein Produkt wählen.');
      return;
    }
    const qtyProblem = quantityProblem(qtyInput.value);
    if (qtyProblem) {
      showError(qtyProblem);
      qtyInput.focus();
      return;
    }
    const dueIssue = dueProblem(dueInput.value);
    if (dueIssue) {
      showError(dueIssue);
      dueInput.focus();
      return;
    }
    submit.disabled = true;
    const revisionBefore = store.plan?.revision ?? store.state?.plan?.revision ?? 0;
    try {
      const response = await api.post('/api/orders', {
        product_id: productSelect.value,
        qty_bars: qty,
        due_in_min: Math.round(hours * 60),
        rush: rushInput.checked,
      }, { quiet: [400] });
      const order = response.order;
      rushInput.checked = false;
      if (!unmounted) {
        expanded.add(order.id);
        setHighlight(order.id);
        scrollPending = true;
        const index = orders.findIndex((o) => o.id === order.id);
        if (index >= 0) orders[index] = order;
        else orders = [order, ...orders];
        paintRows();
        refresh();
      }
      announce(order, response.replan, revisionBefore);
    } catch (error) {
      if (error.status === 400) {
        showError(error.message, error.body?.min_lead_min);
      }
    } finally {
      submit.disabled = false;
    }
  }

  productSelect.addEventListener('change', () => {
    errorBox.hidden = true;
    paintForm();
  });
  qtyInput.addEventListener('input', () => {
    errorBox.hidden = true;
    paintForm();
  });
  dueInput.addEventListener('input', () => {
    errorBox.hidden = true;
    paintForm();
  });

  // ------------------------------------------------------------------ live wiring
  const offOrder = store.on('order', (order) => {
    const index = orders.findIndex((o) => o.id === order.id);
    if (index >= 0) orders[index] = order;
    else orders = [order, ...orders];
    paintRows();
  });
  const offPlan = store.on('plan', () => refresh());
  const offCatalog = store.on('catalog', fillProducts);
  const offRun = store.on('run-change', () => {
    rows.clear();
    expanded.clear();
    shownKey = '';
    orders = [];
    clear(tbody);
    refresh();
  });
  let lastPaint = 0;
  const offState = store.subscribe(() => {
    const stamp = performance.now();
    if (stamp - lastPaint < 1000) return;
    lastPaint = stamp;
    paintForm();
  });
  refreshTimer = setInterval(refresh, REFRESH_MS);

  fillProducts();
  paintForm();
  refresh();
  if (highlightId) setHighlight(highlightId);

  return () => {
    unmounted = true;
    clearInterval(refreshTimer);
    clearTimeout(highlightTimer);
    offOrder();
    offPlan();
    offCatalog();
    offRun();
    offState();
  };
}
