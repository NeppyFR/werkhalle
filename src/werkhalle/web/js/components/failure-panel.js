// The failure chain, docked under whichever view is open while a proposal is computing or open:
// Störung ausgelöst -> OPC-UA-DataChange erkannt -> Zustand abgezweigt -> Neuplanung -> Vorschlag -> übernehmen.
// All times shown come from proposal.chain. The panel never invents a step, and it says so when a proposal went stale.
import { h, s, setText, setAttr, clear } from '../core/dom.js';
import { fmt } from '../core/format.js';
import { icon, statusIcon } from './ui.js';

const TRIGGER_FIRST_STEP = {
  fault: 'Störung ausgelöst',
  stall: 'Stillstand erkannt',
  no_material: 'Materialmangel erkannt',
  scenario: 'Szenario übernommen',
};

// Below this window height the panel starts collapsed: its 42 px head still carries the accept button, and the plant stays in view.
const COLLAPSE_BELOW_PX = 800;

const ROWS = [
  { key: 'makespan', label: 'Makespan', format: (v) => fmt.duration(v), delta: (v) => fmt.duration(v, { signed: true }) },
  { key: 'total_tardiness', label: 'Verspätung', format: (v) => fmt.duration(v), delta: (v) => fmt.duration(v, { signed: true }) },
  { key: 'late_orders', label: 'Verspätete Aufträge', format: (v) => fmt.number(v), delta: (v) => fmt.signed(v) },
  { key: 'setups', label: 'Rüstvorgänge', format: (v) => fmt.number(v), delta: (v) => fmt.signed(v) },
];

function solverShort(key, f) {
  return key === 'cpsat' ? 'CP-SAT' : key === 'anneal' ? 'Annealing' : f.solverLabel(key);
}

function figures(candidate) {
  return candidate?.twin ?? candidate?.metrics ?? null;
}

function stepShape(state) {
  // Shape carries the state: filled = done, ring with dot = running, hollow = still to come, cross = no longer valid.
  const svg = s('svg.step__mark', { viewBox: '0 0 16 16', width: 16, height: 16, 'aria-hidden': 'true' });
  if (state === 'done') {
    svg.append(s('circle', { cx: 8, cy: 8, r: 7, fill: 'var(--metal)' }),
      s('path', { d: 'M4.8 8.2 7 10.4 11.2 5.8', fill: 'none', stroke: 'var(--surface-1)', 'stroke-width': 1.8, 'stroke-linecap': 'round', 'stroke-linejoin': 'round' }));
  } else if (state === 'fault') {
    svg.append(s('path', { d: 'M8 1 15.4 14.4H.6Z', fill: 'var(--state-fault)' }),
      s('path', { d: 'M8 6v4.2M8 11.8v.8', stroke: '#fff', 'stroke-width': 1.7, 'stroke-linecap': 'round' }));
  } else if (state === 'active') {
    svg.append(s('circle', { cx: 8, cy: 8, r: 6.5, fill: 'none', stroke: 'var(--accent)', 'stroke-width': 1.6 }),
      s('circle.step__pulse', { cx: 8, cy: 8, r: 3, fill: 'var(--accent)' }));
  } else if (state === 'void') {
    svg.append(s('circle', { cx: 8, cy: 8, r: 6.5, fill: 'none', stroke: 'var(--state-setup)', 'stroke-width': 1.5 }),
      s('path', { d: 'M5.5 5.5l5 5M10.5 5.5l-5 5', stroke: 'var(--state-setup)', 'stroke-width': 1.5, 'stroke-linecap': 'round' }));
  } else {
    svg.append(s('circle', { cx: 8, cy: 8, r: 6.5, fill: 'none', stroke: 'var(--line-3)', 'stroke-width': 1.5 }));
  }
  return svg;
}

// trigger.also is frozen when the proposal is made, but the machines move on, so what it says about them comes from the live row.
export function alsoLine(others, machineOf, leftOf) {
  const down = [];
  const back = [];
  for (const fault of others ?? []) {
    const live = machineOf(fault.machine);
    if (live && live.status !== 'FAULT') {
      back.push(fault.machine);
      continue;
    }
    const left = leftOf(fault);
    down.push(`${fault.machine ?? ''} ${live?.alarm?.code ?? fault.code ?? ''}`.trim() + (left ? `, ${left}` : ''));
  }
  return [down.length ? `auch: ${down.join('; ')}` : '', back.length ? `wieder bereit: ${back.join(', ')}` : ''].filter(Boolean).join(' · ');
}

// A new order changes the proposal as well, and it has no machine (API.md).
export function pendingLine(pending) {
  const what = [pending.machine, pending.code].filter(Boolean).join(' ');
  const text = pending.text ? pending.text[0].toUpperCase() + pending.text.slice(1) : 'Neue Aufträge';
  const subject = pending.machine ? `Neue Störung ${what}` : text;
  if (pending.accept_blocked) return `${subject}: dieser Stand lässt sich nicht übernehmen. ${pending.accept_blocked} Der Vorschlag wird im Hintergrund aktualisiert, danach geht es wieder.`;
  return `${subject}: der Vorschlag wird im Hintergrund aktualisiert. Dieser Stand bleibt übernehmbar.`;
}

export function mountFailurePanel(root, ctx) {
  const { store, api, fmt: f, router, toast } = ctx;
  let shown = null;
  let solver = null;
  let chosenCollapse = null;  // what the operator clicked; until then the window height decides
  let busy = false;
  let notice = null;          // {tone, text}
  let acceptedRevision = null;
  let hideTimer = 0;
  let rendered = '';

  // The machine drawer ends where this panel begins, so the chain stays readable while the drawer is open.
  new ResizeObserver(() => {
    document.documentElement.style.setProperty('--failure-h', `${root.hidden ? 0 : root.offsetHeight}px`);
  }).observe(root);

  const isCollapsed = () => chosenCollapse ?? window.innerHeight < COLLAPSE_BELOW_PX;

  const head = h('header.failure__head');
  // These tick with the sim clock and a refresh arrives in the background. Rebuilding the head for them could swallow a click on its accept button.
  const metaNode = h('span.failure__meta.mono');
  const alsoNode = h('span.failure__also.mono');
  const pendingNode = h('p.failure__pending', { role: 'status', hidden: true });
  const stepper = h('ol.chain', { 'aria-label': 'Ablauf von der Störung bis zur Planübernahme' });
  const detail = h('div.failure__detail');
  root.append(head, pendingNode, stepper, detail);

  function acceptedRevisionOf(p) {
    if (acceptedRevision) return acceptedRevision;
    // Accepted from Planung: the panel's own accept() never ran, but the active plan says where it came from.
    return store.plan?.source_proposal_id === p.id ? store.plan.revision : null;
  }

  function leftOf(fault, options) {
    const live = store.machine(fault.machine);
    const now = store.clock.now();
    if (live?.status === 'FAULT') return f.repairLeft(live.est_repair_min, live.status_since, now, options);
    if (live) return null;
    // Without a live row all that is known is what was left when the proposal was made.
    if (typeof fault.remaining_min === 'number') return fault.remaining_min > 0 ? f.repairLeft(fault.remaining_min, null, null, options) : f.REPAIR_OVERDUE;
    return f.repairLeft(fault.est_repair_min, fault.t, now, options);
  }

  function metaText(p) {
    const trigger = p.trigger ?? {};
    const meta = [];
    const live = trigger.kind === 'fault' ? store.machine(trigger.machine) : null;
    const back = Boolean(live) && live.status !== 'FAULT';
    if (trigger.kind === 'fault') {
      const left = back ? 'wieder bereit' : leftOf(trigger, { long: true });
      if (left) meta.push(left);
    }
    // A re-solve carries the time of its own trigger. The machine knows when it really went down.
    const since = live?.status === 'FAULT' && typeof live.status_since === 'number' ? live.status_since : trigger.t;
    if (typeof since === 'number') meta.push(`${trigger.kind === 'fault' ? (back ? 'Störung ab ' : 'seit ') : ''}${f.simTime(since)}`);
    if (typeof p.based_on_revision === 'number') meta.push(`auf Basis von Rev. ${p.based_on_revision}`);
    return meta.join(' · ');
  }

  function alsoText(p) {
    return alsoLine(p.trigger?.also, (id) => store.machine(id), leftOf);
  }

  function paintLive(p) {
    const meta = metaText(p);
    const also = alsoText(p);
    setText(metaNode, meta);
    setAttr(metaNode, 'data-tip', meta);
    setText(alsoNode, also);
    setAttr(alsoNode, 'data-tip', also);
    alsoNode.hidden = !also;
    const pending = p.status === 'open' ? p.pending_update : null;
    pendingNode.hidden = !pending;
    if (pending) setText(pendingNode, pendingLine(pending));
  }

  function steps(p) {
    const c = p.chain ?? {};
    const kind = p.trigger?.kind ?? 'fault';
    const closed = !['computing', 'open'].includes(p.status) && p.status !== 'accepted';
    const list = [];

    const firstTime = c.injected_wall_ts ?? c.source_ts ?? c.detected_wall_ts;
    // Only a fault that did not come through this MES has no injection time. Then the plant's own timestamp is the first real value.
    const external = kind === 'fault' && !c.injected_wall_ts;
    list.push({
      label: TRIGGER_FIRST_STEP[kind] ?? 'Auslöser',
      value: firstTime ? f.wallTime(firstTime, { ms: true }) : external ? 'zufällig oder extern' : f.DASH,
      sub: external && firstTime ? 'zufällig/extern' : '',
      state: kind === 'fault' ? 'fault' : 'done',
    });

    if (kind !== 'scenario') {
      const measured = typeof c.latency_ms === 'number';
      list.push({
        label: 'OPC-UA-DataChange erkannt',
        value: !c.detected_wall_ts ? '…' : measured ? `${f.number(c.latency_ms)} ms` : f.wallTime(c.detected_wall_ts, { ms: true }),
        sub: c.detected_wall_ts && measured ? f.wallTime(c.detected_wall_ts, { ms: true }) : '',
        state: c.detected_wall_ts ? 'done' : 'active',
      });
    }

    list.push({
      label: 'Zustand abgezweigt',
      value: c.forked_wall_ts && c.detected_wall_ts ? `+${f.seconds(c.forked_wall_ts - c.detected_wall_ts, 2)}` : c.forked_wall_ts ? f.wallTime(c.forked_wall_ts) : '…',
      sub: typeof c.snapshot_seq === 'number' && c.forked_wall_ts ? `Snapshot #${f.number(c.snapshot_seq)}` : '',
      state: c.forked_wall_ts ? 'done' : closed ? 'void' : 'active',
    });

    const replanDone = Boolean(c.ready_wall_ts);
    const running = !replanDone && Boolean(c.forked_wall_ts);
    list.push({
      label: 'Neuplanung',
      value: replanDone ? f.seconds(c.replan_seconds) : running && !closed ? `läuft ${f.seconds(Date.now() / 1000 - c.forked_wall_ts)}` : '–',
      sub: replanDone ? 'inkl. Zwilling' : '',
      state: replanDone ? 'done' : closed ? 'void' : running ? 'active' : 'pending',
    });

    list.push({
      label: 'Vorschlag',
      value: replanDone && p.delta ? `Verspätung ${f.duration(p.delta.total_tardiness, { signed: true })}` : replanDone ? f.wallTime(c.ready_wall_ts) : '–',
      state: replanDone ? (closed ? 'void' : 'done') : closed ? 'void' : 'pending',
    });

    list.push({
      label: 'übernehmen',
      value: p.status === 'accepted' ? `Revision ${acceptedRevisionOf(p) ?? f.DASH}` : p.status === 'open' ? 'wartet auf Bediener' : closed ? 'nicht mehr möglich' : '–',
      state: p.status === 'accepted' ? 'done' : p.status === 'open' ? 'active' : closed ? 'void' : 'pending',
    });
    return list;
  }

  function renderSteps(p) {
    const list = steps(p);
    const key = JSON.stringify(list.map(({ label, state }) => [label, state]));
    if (stepper.dataset.key !== key) {
      stepper.dataset.key = key;
      clear(stepper);
      for (const step of list) {
        const value = h('span.step__value.mono');
        const sub = h('span.step__sub.mono');
        const node = h(`li.step.step--${step.state}`,
          stepShape(step.state),
          h('span.step__text', h('span.step__label', step.label), value, sub));
        node._value = value;
        node._sub = sub;
        stepper.appendChild(node);
      }
    }
    list.forEach((step, i) => {
      const node = stepper.children[i];
      setText(node._value, step.value);
      setText(node._sub, step.sub ?? '');
    });
  }

  function renderHead(p) {
    clear(head);
    const trigger = p.trigger ?? {};
    const isFault = trigger.kind === 'fault';
    const collapsed = isCollapsed();
    const titleText = isFault
      ? `Störung ${trigger.machine ?? ''} · ${trigger.code ?? ''} ${trigger.text ?? ''}`.trim()
      : trigger.kind === 'scenario' ? 'Szenario aus dem Zwilling'
        : trigger.kind === 'stall' ? 'Stillstand der Anlage'
          : trigger.kind === 'no_material' ? `Materialmangel${trigger.machine ? ` an ${trigger.machine}` : ''}`
            : 'Neuplanung';

    const stateTag = {
      computing: h('span.tag', 'Berechnung läuft'),
      open: h('span.tag.tag--run', 'Vorschlag offen'),
      superseded: h('span.tag.tag--setup', 'Überholt'),
      expired: h('span.tag.tag--setup', 'Abgelaufen'),
      accepted: h('span.tag.tag--run', 'Übernommen'),
      rejected: h('span.tag', 'Verworfen'),
    }[p.status] ?? h('span.tag', p.status);

    const collapseButton = h('button.btn.btn--ghost.btn--sm', {
      type: 'button',
      'aria-expanded': String(!collapsed),
      onclick: () => {
        chosenCollapse = !isCollapsed();
        rendered = '';
        render();
      },
    }, collapsed ? 'Details zeigen' : 'Einklappen');

    const closed = !['computing', 'open'].includes(p.status);
    head.append(
      isFault ? statusIcon('FAULT', { size: 14 }) : statusIcon('warn', { size: 14 }),
      h('h2.failure__title', { 'data-tip': titleText }, titleText),
      metaNode,
      alsoNode,
      stateTag,
      h('span.failure__spacer'),
    );
    if (collapsed && p.status === 'open') head.append(acceptButton(p));
    head.append(collapseButton);
    if (closed) head.append(h('button.btn.btn--ghost.btn--icon', { type: 'button', 'aria-label': 'Hinweis schließen', onclick: dismiss }, icon('close', 14)));
  }

  function acceptButton(p) {
    const label = 'Plan übernehmen';
    const blocked = p.pending_update?.accept_blocked;
    const button = h('button.btn.btn--primary', { type: 'button', disabled: busy || Boolean(blocked), onclick: () => accept(p) }, label);
    if (blocked) button.setAttribute('data-tip', `Erst nach der Aktualisierung möglich: ${blocked}`);
    else if (solver) button.setAttribute('data-tip', `Übernimmt den Plan von ${f.solverLabel(solver)}`);
    return button;
  }

  function validSolvers(p) {
    return Object.entries(p.candidates ?? {}).filter(([, c]) => c).map(([key, c]) => ({ key, candidate: c }));
  }

  function renderDetail(p) {
    clear(detail);
    if (notice) {
      detail.appendChild(h(`p.failure__notice.failure__notice--${notice.tone}`, { role: 'status' }, notice.text));
    }
    if (p.status === 'computing') {
      detail.appendChild(h('p.failure__summary', 'CP-SAT und Simulated Annealing rechnen auf dem abgezweigten Zustand. Der Zwilling prüft danach jeden Kandidaten gegen den aktuellen Plan.'));
      return;
    }
    if (p.status === 'superseded' || p.status === 'expired') {
      detail.appendChild(h('p.failure__summary', p.status_reason || 'Dieser Vorschlag gilt nicht mehr. Er lässt sich nicht mehr übernehmen.'));
      return;
    }
    if (p.status === 'rejected') {
      detail.appendChild(h('p.failure__summary', 'Der Vorschlag wurde verworfen, der aktuelle Plan läuft weiter.'));
      return;
    }
    if (p.status === 'accepted') {
      const revision = acceptedRevisionOf(p);
      detail.appendChild(h('p.failure__summary', `Der Plan ist aktiv${revision ? ` als Revision ${revision}` : ''}. Die Sequenzen gelten ab jetzt.`));
      return;
    }

    const solvers = validSolvers(p);
    if (!solver || !p.candidates?.[solver]) solver = p.recommended ?? solvers[0]?.key ?? null;
    const chosen = p.candidates?.[solver];
    const keep = figures(p.keep);
    const next = figures(chosen);

    const rows = ROWS.map((row) => {
      const a = keep?.[row.key];
      const b = next?.[row.key];
      const delta = typeof a === 'number' && typeof b === 'number' ? b - a : null;
      const tone = delta === null || delta === 0 ? 'same' : delta < 0 ? 'better' : 'worse';
      const arrow = delta === null || delta === 0 ? null
        : s('svg.delta__arrow', { viewBox: '0 0 10 10', width: 9, height: 9, 'aria-hidden': 'true' },
          s('path', { d: delta < 0 ? 'M5 9 1 2h8Z' : 'M5 1l4 7H1Z', fill: 'currentColor' }));
      return h('tr',
        h('th', { scope: 'row' }, row.label),
        h('td.num', typeof a === 'number' ? row.format(a) : f.DASH),
        h('td.num', typeof b === 'number' ? row.format(b) : f.DASH),
        h(`td.num.delta.delta--${tone}`, arrow, delta === null ? f.DASH : row.delta(delta)));
    });

    const table = h('table.tbl.failure__table',
      h('thead', h('tr',
        h('th', p.keep?.twin ? 'laut Zwilling' : 'laut Plan'),
        h('th.num', 'Plan behalten'),
        h('th.num', chosen ? solverShort(solver, f) : 'Empfehlung'),
        h('th.num', 'Differenz'))),
      h('tbody', rows));

    const solverPicker = h('div.seg.failure__solvers', { role: 'radiogroup', 'aria-label': 'Solver auswählen' },
      solvers.map(({ key, candidate }) => {
        const invalid = candidate.valid === false || candidate.status === 'no_solution';
        const tip = invalid
          ? `Nicht übernehmbar: ${candidate.violations?.map((v) => v.message ?? v.rule ?? v).join('; ') || candidate.status}`
          : `${f.solverLabel(key)}: ${candidate.status}, ${f.seconds(candidate.solve_seconds)}`;
        return h('button.seg__btn.failure__solver', {
          type: 'button',
          role: 'radio',
          'aria-checked': String(key === solver),
          'aria-pressed': String(key === solver),
          disabled: invalid,
          'data-tip': tip,
          onclick: () => {
            solver = key;
            rendered = '';
            render();
          },
        }, solverShort(key, f), key === p.recommended ? h('span.failure__reco', ' empfohlen') : null);
      }));

    const summary = h('div.failure__text',
      h(`p.failure__summary${p.beats_keep === false ? '.failure__summary--caveat' : ''}`,
        p.beats_keep === false ? statusIcon('warn', { size: 10 }) : null,
        p.beats_keep === false ? ' ' : null,
        p.summary || 'Vorschlag bereit.'),
      solverPicker,
      chosen?.note ? h('p.faint.failure__solverNote', chosen.note) : null);

    const actions = h('div.failure__actions',
      acceptButton(p),
      h('button.btn', { type: 'button', disabled: busy, onclick: () => reject(p) }, 'Verwerfen'),
      h('a.btn.btn--ghost', { href: router.href('planung', { proposal: p.id }) }, 'Gantt in Planung prüfen'));

    detail.append(summary, table, actions);
  }

  async function accept(p) {
    busy = true;
    notice = null;
    rendered = '';
    render();
    try {
      const result = await api.post(`/api/proposals/${encodeURIComponent(p.id)}/accept`, solver ? { solver } : {}, { quiet: [409, 422] });
      acceptedRevision = result?.revision ?? null;
      toast(`Plan übernommen: Revision ${acceptedRevision}.`, { kind: 'ok' });
      shown = { ...p, status: 'accepted' };
      scheduleHide(6000);
    } catch (error) {
      if (error.status === 409) {
        const fresh = error.body?.new_proposal_id;
        notice = { tone: 'warn', text: fresh
          ? `Veraltet: ${error.message} Neue Berechnung läuft (${fresh}).`
          : `Veraltet: ${error.message}` };
        shown = { ...p, status: 'superseded', status_reason: error.message };
        store.refetchProposals();
      } else if (error.status === 422) {
        notice = { tone: 'warn', text: `Dieser Kandidat wurde nicht übernommen: ${error.message}` };
      } else {
        notice = { tone: 'warn', text: `Nicht übernommen: ${error.message}` };
      }
    } finally {
      busy = false;
      rendered = '';
      render();
    }
  }

  async function reject(p) {
    busy = true;
    rendered = '';
    render();
    try {
      await api.post(`/api/proposals/${encodeURIComponent(p.id)}/reject`);
      toast('Vorschlag verworfen, der aktuelle Plan läuft weiter.');
      hide();
    } catch {
      /* the api helper showed the toast */
    } finally {
      busy = false;
      rendered = '';
      render();
    }
  }

  function dismiss() {
    hide();
  }

  function hide() {
    shown = null;
    notice = null;
    chosenCollapse = null;
    acceptedRevision = null;
    rendered = '';
    render();
  }

  function scheduleHide(ms) {
    clearTimeout(hideTimer);
    hideTimer = setTimeout(hide, ms);
  }

  function render() {
    root.hidden = !shown;
    if (!shown) return;
    const p = shown;
    const collapsed = isCollapsed();
    root.className = `failure failure--${p.status}${collapsed ? ' failure--collapsed' : ''}`;
    stepper.hidden = collapsed;
    detail.hidden = collapsed;
    const key = JSON.stringify([p.id, p.status, p.summary, p.recommended, solver, busy, collapsed, notice, acceptedRevisionOf(p), p.delta,
      p.candidates ? Object.keys(p.candidates) : null, p.trigger?.machine, p.trigger?.code, p.pending_update?.accept_blocked]);
    if (key !== rendered) {
      rendered = key;
      renderHead(p);
      if (!collapsed) renderDetail(p);
    }
    paintLive(p);
    if (!collapsed) renderSteps(p);
  }

  function onProposal(data) {
    clearTimeout(hideTimer);
    if (data.status === 'computing' || data.status === 'open') {
      if (shown?.id !== data.id) {
        solver = null;
        // A notice about the proposal this one replaces stays visible until the new one is there.
        if (shown?.status !== 'superseded') notice = null;
        if (data.status === 'open') notice = null;
      } else if (data.status === 'open') {
        notice = null;
      }
      acceptedRevision = null;
      shown = data;
    } else if (data.status === 'accepted') {
      if (shown?.id === data.id && shown.status !== 'accepted') shown = { ...shown, ...data };
      else if (!shown) shown = null;
      if (shown) scheduleHide(6000);
    } else if (data.status === 'rejected') {
      if (shown?.id === data.id) hide();
    } else if (shown?.id === data.id) {
      // superseded or expired: stay on screen and say so, until the operator closes it or a new proposal arrives.
      shown = { ...shown, ...data };
    }
    render();
  }

  store.on('proposal', onProposal);
  store.on('run-change', hide);
  store.on('plan', () => render());
  window.addEventListener('resize', () => render());
  if (store.proposals.open) onProposal(store.proposals.open);

  // The running solver time and the age tick on their own.
  setInterval(() => {
    if (shown && shown.status === 'computing' && !isCollapsed()) renderSteps(shown);
  }, 250);
  store.subscribe(() => {
    if (shown) paintLive(shown);
  });

  return { render };
}
