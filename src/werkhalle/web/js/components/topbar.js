// Top bar: product mark, sim time, speed, plant switches, OPC UA connection and mode badge.
import { h, s, setText } from '../core/dom.js';
import { formatSimTime, formatNumber } from '../core/format.js';
import { statusIcon, paintIcon } from './ui.js';

const FALLBACK_FACTORS = [0, 60, 120, 240, 480];
const CONFIRM_TIMEOUT_MS = 4000;

function productMark() {
  // The mark is an extruded H profile, end on.
  return h('a.topbar__mark', { href: '#/leitstand', 'aria-label': 'Werkhalle, zum Leitstand' },
    s('svg', { viewBox: '0 0 16 16', width: 18, height: 18, 'aria-hidden': 'true' },
      s('path', { fill: 'currentColor', d: 'M2 2h4v5h4V2h4v12h-4V9H6v5H2z' })),
    h('span.topbar__name', 'Werkhalle'));
}

function speedSelector({ api, store, toast }) {
  const buttons = new Map();
  const group = h('div.seg', { role: 'group', 'aria-label': 'Simulationsgeschwindigkeit' });
  let wanted = null;
  let wantedSince = 0;

  const paint = () => {
    const actual = store.state?.sim?.factor;
    const shown = wanted ?? actual;
    for (const [factor, button] of buttons) {
      const on = factor === shown;
      button.setAttribute('aria-pressed', String(on));
      button.classList.toggle('is-pending', on && wanted !== null && wanted !== actual);
    }
  };

  const select = async (factor) => {
    wanted = factor;
    wantedSince = performance.now();
    paint();
    try {
      await api.post('/api/plant/settings', { time_factor: factor });
    } catch {
      wanted = null;
      paint();
    }
  };

  const build = (factors) => {
    group.textContent = '';
    buttons.clear();
    for (const factor of factors) {
      const label = factor === 0 ? 'Pause' : `${factor}×`;
      const button = h('button.seg__btn', {
        type: 'button',
        'aria-pressed': 'false',
        dataset: { tip: factor === 0 ? 'Anlage anhalten' : `1 Sekunde = ${formatNumber(factor / 60, 0)} Simulationsminuten` },
        onclick: () => select(factor),
      }, label);
      buttons.set(factor, button);
      group.appendChild(button);
    }
    paint();
  };

  build(FALLBACK_FACTORS);
  store.on('catalog', (catalog) => {
    if (catalog.time_factors?.length) build(catalog.time_factors);
  });
  store.subscribe((state) => {
    // The factor only counts as set once the state reports it, since the write goes through OPC UA and can revert.
    if (wanted !== null) {
      if (state.sim.factor === wanted) {
        wanted = null;
      } else if (performance.now() - wantedSince > CONFIRM_TIMEOUT_MS) {
        wanted = null;
        toast('Die Anlage hat die neue Geschwindigkeit nicht übernommen.', { kind: 'warn' });
      }
    }
    paint();
  });
  return group;
}

function plantSwitch({ api, store, toast }, { key, label, tip }) {
  let wanted = null;
  let wantedSince = 0;
  const button = h('button.switch', {
    type: 'button',
    role: 'switch',
    'aria-checked': 'false',
    dataset: { tip },
    onclick: async () => {
      const next = !(wanted ?? store.state?.sim?.[key] ?? false);
      wanted = next;
      wantedSince = performance.now();
      paint();
      try {
        await api.post('/api/plant/settings', { [key]: next });
      } catch {
        wanted = null;
        paint();
      }
    },
  }, h('span.switch__track', h('span.switch__thumb')), h('span.switch__label', label));

  function paint() {
    const actual = store.state?.sim?.[key] ?? false;
    const shown = wanted ?? actual;
    button.setAttribute('aria-checked', String(shown));
    button.classList.toggle('is-pending', wanted !== null && wanted !== actual);
  }

  store.subscribe((state) => {
    if (wanted !== null) {
      if (state.sim[key] === wanted) {
        wanted = null;
      } else if (performance.now() - wantedSince > CONFIRM_TIMEOUT_MS) {
        wanted = null;
        toast(`„${label}“ wurde nicht übernommen.`, { kind: 'warn' });
      }
    }
    paint();
  });
  return button;
}

function connectionIndicator({ store }) {
  const LEVEL_ICON = { ok: 'link', stale: 'warn', down: 'FAULT', wait: 'IDLE' };
  const iconNode = statusIcon('IDLE');
  const text = h('span.conn__state');
  const rate = h('span.conn__rate');
  const node = h('div.conn', { role: 'status', tabindex: '0' }, iconNode, h('span.conn__name', 'OPC UA'), text, rate);
  const paint = () => {
    const link = store.link();
    paintIcon(iconNode, LEVEL_ICON[link.level]);
    node.className = `conn conn--${link.level}`;
    setText(text, link.label);
    const perSecond = store.state?.counters?.datachanges_per_s;
    setText(rate, link.level === 'ok' && perSecond !== undefined ? `${formatNumber(perSecond)}/s` : '');
    node.setAttribute('data-tip', link.detail);
    node.setAttribute('aria-label', `OPC UA: ${link.label}. ${link.detail}`);
  };
  store.on('conn', paint);
  paint();
  return node;
}

function modeBadge({ store }) {
  const LABELS = {
    schatten: { label: 'Schatten', tip: 'Schatten: Live-Spiegel der Anlage. Alle Werte kommen über OPC UA.' },
    zwilling: { label: 'Zwilling', tip: 'Zwilling: Abzweig des Anlagenzustands. Diese Werte sind nicht die laufende Anlage.' },
  };
  const node = h('span.mode');
  const paint = (mode) => {
    node.className = `mode mode--${mode}`;
    setText(node, LABELS[mode].label);
    node.setAttribute('data-tip', LABELS[mode].tip);
  };
  store.on('ui-mode', paint);
  paint(store.uiMode);
  return node;
}

export function mountTopbar(root, ctx) {
  const { store } = ctx;
  const time = h('output.topbar__time', { 'aria-label': 'Simulationszeit' }, '–');
  const stateNote = h('span.topbar__note');

  root.append(
    productMark(),
    h('div.topbar__clock', time, stateNote),
    speedSelector(ctx),
    h('div.topbar__switches',
      plantSwitch(ctx, { key: 'random_failures', label: 'Zufallsstörungen', tip: 'Zufällige Störungen nach MTBF der Maschinen' }),
      plantSwitch(ctx, { key: 'auto_supply', label: 'Demo-Nachschub', tip: 'Erzeugt automatisch neue Aufträge, damit die Anlage weiterläuft. Nur für die Demo.' })),
    h('div.topbar__spacer'),
    connectionIndicator(ctx),
    modeBadge(ctx),
  );

  // The clock text only changes once a sim minute, so a 4 Hz timer is plenty and keeps working in a hidden tab.
  setInterval(() => {
    const sim = store.state?.sim;
    if (!sim) return;
    setText(time, formatSimTime(store.clock.now()));
    setText(stateNote, sim.factor === 0 ? 'Pause' : '');
  }, 250);
}
