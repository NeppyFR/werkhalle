// Rückverfolgung: search, genealogy graph with ancestors and descendants, recall figures, node details.
// Layout logic lives in _genealogy.js. This module draws it and answers the two questions the view exists for:
// "von welchem Bolzen stammt dieses Paket" and "welche Pakete betrifft diese Schmelze".
import { h, s, setText, clear, loadCss, appendAll } from '../core/dom.js';
import { panel, empty, statusIcon } from '../components/ui.js';
import { poll } from './_poll.js';
import { COLUMN_LIMIT, COLUMN_TYPES, buildModel, collapse, orderColumns, placeRows, lineage, typeName, typePlural } from './_genealogy.js';

const NODE_W = 88;
const RIBBON_ROOMY_PX = 210;
const ROW_H = 22;
const GROUP_H = 34;
const ROW_GAP = 5;
const CHIP_H = 20;
const CHIP_GAP = 4;
const RAIL_STEP = 6;
const PAD_X = 10;
const MIN_COL_GAP = 26;
const MAX_COL_GAP = 130;
const RECENT_ROWS = 8;
const PACKAGE_LINKS = 16;

// Edge width grows with the square root of the quantity. The floors stop a graph of equal edges from looking heavy.
const EDGE_FLOOR = { Bolzen: 24, Stangen: 64, 'Stück': 64 };
const LINE_TAGS = 6;

const QUESTIONS = {
  melt: 'Welche Pakete betrifft diese Schmelze?',
  billet: 'Was ist aus diesem Bolzen geworden?',
  strand: 'Woher kommt dieser Strang, und wohin ging er?',
  basket: 'Woher kommt dieser Korb, und wohin ging er?',
  load: 'Was lag gemeinsam in diesem Ofen, und wohin ging es?',
  package: 'Von welchem Bolzen stammt dieses Paket?',
  order: 'Woraus besteht dieser Auftrag?',
  lot: 'Woraus besteht dieses Los, und wohin ging es?',
};

function links(ids, router, max = 4) {
  const shown = ids.length > max ? ids.slice(0, max - 1) : ids;
  const parts = [];
  shown.forEach((id, i) => {
    if (i) parts.push(', ');
    parts.push(h('a.mono', { href: router.href('rueckverfolgung', { q: id }) }, id));
  });
  if (ids.length > shown.length) parts.push(` +${ids.length - shown.length}`);
  return parts;
}

function originLine(graph, model, router, fmt) {
  const root = model.nodes.get(graph.root);
  const others = (type) => [...model.nodes.values()].filter((n) => n.type === type && n.id !== graph.root).map((n) => n.id).sort();
  if (root?.type === 'melt') {
    const alloy = root.attrs?.Legierung;
    return [`Anfang der Kette: ${fmt.plural(root.qty ?? 0, 'Bolzen', 'Bolzen')} gegossen`, alloy ? `, Legierung ${alloy}.` : '.'];
  }
  const melts = others('melt');
  const billets = others('billet');
  const mark = graph.truncated ? 'mindestens ' : '';
  const parts = [];
  if (billets.length) {
    parts.push(`Aus ${mark}${fmt.plural(billets.length, 'Bolzen', 'Bolzen')} (`, ...links(billets, router), ')');
  }
  if (melts.length === 1) parts.push(billets.length ? ' der Schmelze ' : 'Aus Schmelze ', ...links(melts, router), '.');
  else if (melts.length) parts.push(billets.length ? ' von ' : 'Aus ', fmt.plural(melts.length, 'Schmelze', 'Schmelzen'), ': ', ...links(melts, router), '.');
  else parts.push('Keine Herkunft erfasst.');
  return parts;
}

function targetLine(graph, model, router, fmt) {
  const root = model.nodes.get(graph.root);
  const summary = graph.summary ?? {};
  const orders = summary.orders ?? [];
  const amounts = `${fmt.plural(summary.packages ?? 0, 'Paket', 'Pakete')}, ${fmt.number(summary.pieces)} Stück, ${fmt.kg(summary.kg)}`;
  if (root?.type === 'order') {
    return summary.packages ? [`Ende der Kette: ${amounts} geliefert.`] : ['Ende der Kette: noch nichts verpackt.'];
  }
  if (root?.type === 'package') return ['Geliefert an Auftrag ', ...links(orders, router), '.'];
  if (!summary.packages) return ['Noch kein Paket: das Material ist noch im Lager oder in der Fertigung.'];
  const label = graph.recall_label ?? 'möglich betroffen';
  const parts = [`${label[0].toUpperCase()}${label.slice(1)}: ${amounts} in ${fmt.plural(orders.length, 'Auftrag', 'Aufträgen')}: `, ...links(orders, router), '.'];
  if (root?.type === 'melt' && !graph.truncated) {
    const used = new Set(model.flow.map((e) => e.from));
    const billets = [...model.nodes.values()].filter((n) => n.type === 'billet');
    const unused = billets.filter((n) => !used.has(n.id)).length;
    if (unused) parts.push(` ${fmt.number(unused)} von ${fmt.number(billets.length)} Bolzen noch nicht verpresst.`);
  }
  return parts;
}

function attrValue(value, fmt) {
  return typeof value === 'number' ? fmt.number(value, Number.isInteger(value) ? 0 : 1) : String(value);
}

function relationLabel(relation) {
  return { root: 'Ausgangspunkt', ancestor: 'Vorfahre', descendant: 'Nachkomme', side: 'Seitenknoten' }[relation] ?? '';
}

export function mount(el, ctx) {
  loadCss('css/views/rueckverfolgung.css');
  const { api, fmt, router } = ctx;
  el.classList.add('rueckv');

  let alive = true;
  let token = 0;
  let graph = null;
  let model = null;
  let view = null;
  let rootKey = null;
  let selected = null;
  let mode = 'graph';
  let drawnWidth = 0;
  let parts = null;
  const expanded = new Set();

  // --- sidebar ---
  const input = h('input.input.rueckv-search__input', {
    type: 'search',
    placeholder: 'S-00001, P-00012, A-1001 …',
    'aria-label': 'Schmelze, Bolzen, Korb, Ofenlast, Paket oder Auftrag suchen',
    autocomplete: 'off',
    spellcheck: false,
  });
  const form = h('form.rueckv-search', { role: 'search' },
    h('label.field__label', 'Schmelze, Bolzen, Korb, Ofenlast, Paket, Auftrag'), input);
  const hits = h('div.rueckv-hits', { hidden: true });
  const recent = h('div.rueckv-recent');
  const side = h('aside.rueckv-side.panel', form, hits, recent);

  function hitRow(row) {
    const current = graph && row.id === graph.root;
    return h('a.rueckv-hit', {
      href: router.href('rueckverfolgung', { q: row.id }),
      'data-tip': row.label,
      'aria-current': current ? 'true' : null,
    },
    h('span.rueckv-hit__id.mono', row.id),
    h('span.rueckv-hit__time.mono', fmt.simTime(row.t)));
  }

  function showHits(rows, query) {
    clear(hits);
    const typed = query.trim();
    hits.hidden = !typed;
    recent.hidden = Boolean(typed);
    if (!typed) return;
    if (!rows.length) {
      hits.appendChild(empty(`Kein Treffer für „${typed}“.`));
      return;
    }
    const byType = new Map();
    for (const row of rows) {
      if (!byType.has(row.type)) byType.set(row.type, []);
      byType.get(row.type).push(row);
    }
    for (const [type, group] of byType) {
      hits.append(h('h3.rueckv-side__head', typePlural(type)), ...group.map(hitRow));
    }
  }

  let recentKey = '';
  let recentRows = [];
  function showRecent(rows) {
    recentRows = rows;
    const packages = rows.filter((r) => r.type === 'package').slice(0, RECENT_ROWS);
    const melts = rows.filter((r) => r.type === 'melt').slice(0, RECENT_ROWS);
    const key = [...packages, ...melts].map((r) => r.id).join();
    if (key === recentKey) return;
    recentKey = key;
    clear(recent);
    recent.append(
      h('h3.rueckv-side__head', 'Neueste Pakete'),
      h('p.rueckv-side__ask', 'Von welchem Bolzen stammt dieses Paket?'),
      ...(packages.length ? packages.map(hitRow) : [empty('Noch keine Pakete.')]),
      h('h3.rueckv-side__head', 'Neueste Schmelzen'),
      h('p.rueckv-side__ask', 'Welche Pakete betrifft diese Schmelze?'),
      ...(melts.length ? melts.map(hitRow) : [empty('Noch keine Schmelzen.')]),
    );
  }

  async function fetchRecent() {
    const data = await api.get('/api/trace/recent?limit=60', { quiet: true });
    if (!alive) return data.results;
    showRecent(data.results ?? []);
    return data.results ?? [];
  }

  let searchTimer = 0;
  let searchToken = 0;
  async function runSearch(query) {
    const mine = ++searchToken;
    try {
      const data = await api.get(`/api/trace/search?q=${encodeURIComponent(query.trim())}`, { quiet: true });
      if (alive && mine === searchToken) showHits(data.results ?? [], query);
      return data.results ?? [];
    } catch {
      return [];
    }
  }

  input.addEventListener('input', () => {
    clearTimeout(searchTimer);
    const query = input.value;
    if (!query.trim()) {
      searchToken += 1;
      showHits([], '');
      return;
    }
    searchTimer = setTimeout(() => runSearch(query), 160);
  });
  input.addEventListener('keydown', (event) => {
    if (event.key === 'ArrowDown') {
      const first = hits.querySelector('a');
      if (first) {
        event.preventDefault();
        first.focus();
      }
    }
  });
  form.addEventListener('submit', async (event) => {
    event.preventDefault();
    const query = input.value.trim();
    if (!query) return;
    clearTimeout(searchTimer);
    const rows = await runSearch(query);
    const exact = rows.find((r) => r.id.toLowerCase() === query.toLowerCase());
    if (exact || rows.length === 1) router.go('rueckverfolgung', { q: (exact ?? rows[0]).id });
  });

  // --- answer band ---
  const asked = h('p.rueckv-answer__ask');
  const title = h('h1.rueckv-answer__title');
  const titleMeta = h('span.rueckv-answer__meta.mono');
  const originText = h('p.rueckv-answer__text');
  const targetText = h('p.rueckv-answer__text');
  const answer = h('section.rueckv-answer.panel', { 'aria-live': 'polite', hidden: true },
    h('div.rueckv-answer__top', title, titleMeta, asked),
    h('div.rueckv-answer__row', h('span.rueckv-answer__key', 'Woher', h('span.faint', ' ← Vorfahren')), originText),
    h('div.rueckv-answer__row', h('span.rueckv-answer__key', 'Wohin', h('span.faint', ' → Nachkommen')), targetText));

  // --- graph panel ---
  const refresh = h('button.btn.btn--sm', { type: 'button', hidden: true }, 'Neue Daten: aktualisieren');
  const graphBtn = h('button.seg__btn', { type: 'button', 'aria-pressed': 'true' }, 'Graph');
  const listBtn = h('button.seg__btn', { type: 'button', 'aria-pressed': 'false' }, 'Liste');
  const graphPanel = panel('Stammbaum', { dense: true, actions: [refresh, h('div.seg', { role: 'group', 'aria-label': 'Darstellung' }, graphBtn, listBtn)] });
  const note = h('p.rueckv-note', { hidden: true }, statusIcon('warn', { size: 11 }), h('span'));
  const scroller = h('div.rueckv-scroll', { tabindex: 0, 'aria-label': 'Stammbaum, scrollbar' });
  const legend = h('p.rueckv-legend',
    h('span.rueckv-legend__item', s('svg', { width: 26, height: 8, 'aria-hidden': 'true' }, s('line', { x1: 1, y1: 4, x2: 25, y2: 4, class: 'rueckv-edge', 'stroke-width': 2 })), 'Materialfluss, Dicke = Menge'),
    h('span.rueckv-legend__item', s('svg', { width: 26, height: 8, 'aria-hidden': 'true' }, s('line', { x1: 1, y1: 4, x2: 25, y2: 4, class: 'rueckv-rail' })), 'Ofenlast: Seitenknoten, kein Materialfluss'),
    h('span.rueckv-legend__item.rueckv-legend__hint', 'Klick: nur diese Linie. Doppelklick: neuer Ausgangspunkt.'));
  graphPanel.body.append(note, scroller, legend);
  graphPanel.node.classList.add('rueckv-graph');

  const placeholder = h('div.rueckv-placeholder', { hidden: true });

  // --- right column ---
  const recall = panel('Rückruf');
  recall.node.classList.add('rueckv-recall');
  const detail = panel('Details');
  detail.node.classList.add('rueckv-detail');
  const right = h('div.rueckv-right', recall.node, detail.node);

  const centre = h('div.rueckv-centre', answer, graphPanel.node, placeholder);
  el.append(side, centre, right);

  // --- drawing ---
  function edgeWidth(edge, maxByUnit) {
    const top = Math.max(maxByUnit.get(edge.unit) ?? 1, EDGE_FLOOR[edge.unit] ?? 1);
    return 1 + 3 * Math.sqrt((edge.qty || 0) / top);
  }

  function shortGroup(name) {
    return name.length > 13 ? `${name.slice(0, 12)}…` : name;
  }

  function itemName(item) {
    if (item.count > 1) return `${fmt.number(item.count)} ${typePlural(item.type)}`;
    return item.node?.id ?? item.key;
  }

  function drawItem(item, x, top) {
    const y = top + item.y - item.h / 2;
    const classes = ['rueckv-node', `rueckv-node--${item.type}`];
    if (item.isRoot) classes.push('is-root');
    if (item.count > 1) classes.push('is-group');
    const tip = item.count > 1
      ? `${fmt.number(item.count)} ${typePlural(item.type)} · ${item.group}: ${fmt.number(item.qty)} ${item.unit}`
      : `${item.node.label}${item.node.t !== null && item.node.t !== undefined ? ` · ${fmt.simTime(item.node.t)}` : ''}${item.node.qty ? ` · ${fmt.number(item.node.qty)} ${item.node.unit ?? ''}` : ''}`;
    const group = s('g', {
      class: classes.join(' '),
      tabindex: 0,
      role: 'button',
      'aria-label': `${typeName(item.type)} ${itemName(item)}${item.relation ? `, ${relationLabel(item.relation)}` : ''}`,
      'data-tip': tip,
      'data-key': item.key,
    });
    if (item.count > 1) {
      group.appendChild(s('rect.rueckv-node__back', { x: x + 3, y: y - 3, width: NODE_W, height: item.h, rx: 2 }));
    }
    group.appendChild(s('rect.rueckv-node__box', { x, y, width: NODE_W, height: item.h, rx: 2 }));
    if (item.count > 1) {
      group.appendChild(s('text.rueckv-node__count', { x: x + 7, y: y + 14 }, itemName(item)));
      group.appendChild(s('text.rueckv-node__sub', { x: x + 7, y: y + 27 }, shortGroup(item.group)));
    } else {
      group.appendChild(s('text.rueckv-node__id', { x: x + 7, y: y + 15 }, item.node.id));
      if (item.type !== 'billet' && item.node.qty) {
        group.appendChild(s('text.rueckv-node__qty', { x: x + NODE_W - 6, y: y + 15, 'text-anchor': 'end' }, fmt.number(item.node.qty)));
      }
    }
    return group;
  }

  function drawChip(entry, x, y) {
    const w = NODE_W;
    const notch = 7;
    const d = `M${x + notch} ${y}H${x + w - notch}L${x + w} ${y + CHIP_H / 2}L${x + w - notch} ${y + CHIP_H}H${x + notch}L${x} ${y + CHIP_H / 2}Z`;
    const label = entry.more ? `+${fmt.number(entry.ids.length)} weitere` : entry.key;
    const nameOfType = typeName(entry.type);
    const group = s('g', {
      class: `rueckv-chip${entry.isRoot ? ' is-root' : ''}`,
      tabindex: 0,
      role: 'button',
      'aria-label': entry.more ? `${label} ${typePlural(entry.type)}` : `${nameOfType} ${entry.key}, Seitenknoten`,
      'data-tip': entry.more ? `${label} ${typePlural(entry.type)}` : `${nameOfType} ${entry.key}: ${entry.type === 'load' ? fmt.plural(entry.memberCount, 'Korb', 'Körbe') : fmt.plural(entry.memberCount, 'Strang', 'Stränge')} in diesem Stammbaum, Seitenknoten ohne Materialfluss`,
      'data-key': entry.key,
    },
    s('path.rueckv-chip__shape', { d }),
    s('text.rueckv-chip__text', { x: x + w / 2, y: y + 14, 'text-anchor': 'middle' }, label));
    return group;
  }

  function headerFor(xs, rootCol) {
    const head = h('div.rueckv-head', { style: { width: `${drawnWidth}px` } });
    const lastCol = COLUMN_TYPES.length - 1;
    if (rootCol !== null) {
      const sideRoot = view.sides.some((entry) => entry.isRoot);
      const leftEnd = rootCol - 1;
      const rightStart = sideRoot ? rootCol : rootCol + 1;
      // Over a single column the arrow is all that fits; the explanation would squeeze it against the clip.
      const roomy = (width) => width >= RIBBON_ROOMY_PX;
      if (leftEnd >= 0) {
        const width = xs[leftEnd] + NODE_W - xs[0];
        head.appendChild(h('div.rueckv-ribbon', { style: { left: `${xs[0]}px`, width: `${width}px` } },
          h('span', '← Vorfahren'), roomy(width) ? h('span.rueckv-ribbon__why', 'woraus es entstand') : null));
      }
      if (rightStart <= lastCol) {
        const width = xs[lastCol] + NODE_W - xs[rightStart];
        head.appendChild(h('div.rueckv-ribbon.rueckv-ribbon--down', { style: { left: `${xs[rightStart]}px`, width: `${width}px` } },
          roomy(width) ? h('span.rueckv-ribbon__why', 'was daraus entstand') : null, h('span', 'Nachkommen →')));
      }
    }
    for (const column of view.columns) {
      const isRootColumn = !view.sides.some((e) => e.isRoot) && column.col === rootCol;
      const canToggle = column.total > COLUMN_LIMIT;
      const label = h(`div.rueckv-col${isRootColumn ? '.is-root' : ''}`, { style: { left: `${xs[column.col]}px` } },
        h('span.rueckv-col__name', typeName(column.type)),
        h('span.rueckv-col__count.mono', `${fmt.number(column.total)}${model.truncated ? '+' : ''}`),
        canToggle ? h('button.rueckv-col__toggle', {
          type: 'button',
          'aria-label': `${typePlural(column.type)} ${column.folded ? 'einzeln zeigen' : 'zusammenfassen'}`,
          'data-tip': column.folded ? 'Alle einzeln zeigen' : 'Wieder zu Gruppen zusammenfassen',
          onclick: () => {
            if (column.folded) expanded.add(column.type);
            else expanded.delete(column.type);
            rebuild();
          },
        }, column.folded ? 'aufklappen' : 'einklappen') : null);
      head.appendChild(label);
    }
    return head;
  }

  function draw() {
    if (!view) return;
    clear(scroller);
    drawnWidth = scroller.clientWidth;
    const sidesByCol = new Map();
    for (const entry of view.sides) {
      const col = entry.col ?? COLUMN_TYPES.indexOf('basket');
      if (!sidesByCol.has(col)) sidesByCol.set(col, []);
      sidesByCol.get(col).push(entry);
    }
    const extra = COLUMN_TYPES.map((_, c) => {
      const rails = (sidesByCol.get(c) ?? []).filter((e) => e.memberKeys.length).length;
      return rails ? rails * RAIL_STEP + 10 : 0;
    });
    const count = COLUMN_TYPES.length;
    const free = drawnWidth - 2 * PAD_X - count * NODE_W - extra.reduce((a, b) => a + b, 0);
    const gap = Math.min(MAX_COL_GAP, Math.max(MIN_COL_GAP, free / (count - 1)));
    const xs = [];
    let cursor = PAD_X;
    COLUMN_TYPES.forEach((_, c) => {
      xs[c] = cursor;
      cursor += NODE_W + extra[c] + gap;
    });
    const contentW = cursor - gap + PAD_X;
    drawnWidth = Math.max(drawnWidth, contentW);

    const chipRows = Math.max(0, ...[...sidesByCol.values()].map((list) => list.length));
    const top = (chipRows ? chipRows * (CHIP_H + CHIP_GAP) + 8 : 0) + 8;
    const rowsH = placeRows(view, { rowH: ROW_H, groupH: GROUP_H, gap: ROW_GAP });
    const height = Math.max(top + rowsH + 12, 120);

    const maxByUnit = new Map();
    for (const edge of view.edges) maxByUnit.set(edge.unit, Math.max(maxByUnit.get(edge.unit) ?? 0, edge.qty || 0));

    const edgesLayer = s('g');
    const railsLayer = s('g');
    const nodesLayer = s('g');
    const svg = s('svg.rueckv-svg', { width: drawnWidth, height, role: 'group', 'aria-label': `Stammbaum von ${graph.root}` }, edgesLayer, railsLayer, nodesLayer);

    const edgeEls = [];
    for (const edge of view.edges) {
      const a = view.items.get(edge.from);
      const b = view.items.get(edge.to);
      const x1 = xs[a.col] + NODE_W + (a.count > 1 ? 3 : 0);
      const x2 = xs[b.col];
      const y1 = top + a.y;
      const y2 = top + b.y;
      const dx = (x2 - x1) / 2;
      const d = `M${x1} ${y1}C${x1 + dx} ${y1} ${x2 - dx} ${y2} ${x2} ${y2}`;
      const text = `${itemName(a)} → ${itemName(b)}: ${fmt.number(edge.qty)} ${edge.unit}`;
      const hit = s('path.rueckv-edge-hit', { d, 'data-tip': text });
      const line = s('path.rueckv-edge', { d, 'stroke-width': edgeWidth(edge, maxByUnit).toFixed(2) });
      edgesLayer.append(hit, line);
      edgeEls.push({ line, from: edge.from, to: edge.to });
    }

    const railEls = [];
    const chipEls = new Map();
    for (const [col, list] of sidesByCol) {
      const withRails = list.filter((e) => e.memberKeys.length).length;
      let railIndex = 0;
      list.forEach((entry, i) => {
        const chipY = 4 + i * (CHIP_H + CHIP_GAP);
        const chipX = xs[col];
        if (entry.memberKeys.length) {
          const railX = xs[col] + NODE_W + 8 + (withRails - 1 - railIndex) * RAIL_STEP;
          railIndex += 1;
          const members = entry.memberKeys.map((key) => view.items.get(key));
          const bottom = Math.max(...members.map((m) => top + m.y));
          const spine = s('path.rueckv-rail', { d: `M${chipX + NODE_W} ${chipY + CHIP_H / 2}H${railX}V${bottom}` });
          railsLayer.appendChild(spine);
          railEls.push({ line: spine, chip: entry.key, member: null });
          for (const member of members) {
            const tick = s('path.rueckv-rail', { d: `M${railX} ${top + member.y}H${xs[col] + NODE_W + (member.count > 1 ? 3 : 0)}` });
            railsLayer.appendChild(tick);
            railEls.push({ line: tick, chip: entry.key, member: member.key });
          }
        }
        if (i === 0 && chipX > 60) {
          railsLayer.appendChild(s('text.rueckv-chip__caption', { x: chipX - 6, y: chipY + 14, 'text-anchor': 'end' }, typeName(entry.type)));
        }
        const chip = drawChip(entry, chipX, chipY);
        nodesLayer.appendChild(chip);
        chipEls.set(entry.key, chip);
      });
    }

    const nodeEls = new Map();
    for (const column of view.columns) {
      for (const item of column.items) {
        const node = drawItem(item, xs[column.col], top);
        nodesLayer.appendChild(node);
        nodeEls.set(item.key, node);
      }
    }

    const rootSide = view.sides.find((entry) => entry.isRoot);
    const rootCol = rootSide ? rootSide.col : view.items.get(rootKey)?.col ?? null;
    scroller.append(headerFor(xs, rootCol), svg);
    parts = { svg, edgeEls, railEls, nodeEls, chipEls };
    paintSelection();
  }

  function paintSelection() {
    if (!parts) return;
    const line = lineage(view, model, selected);
    const focused = selected !== rootKey;
    for (const { line: el, from, to } of parts.edgeEls) {
      const on = line.edges.has(`${from}\n${to}`);
      el.classList.toggle('is-dim', !on);
      el.classList.toggle('is-path', on && focused);
    }
    for (const { line: el, chip, member } of parts.railEls) {
      const on = line.keys.has(chip) && (member === null || line.keys.has(member));
      el.classList.toggle('is-dim', !on);
      el.classList.toggle('is-path', on && focused);
    }
    for (const [key, node] of [...parts.nodeEls, ...parts.chipEls]) {
      node.classList.toggle('is-dim', !line.keys.has(key));
      node.classList.toggle('is-selected', key === selected);
      node.setAttribute('aria-pressed', key === selected ? 'true' : 'false');
    }
    // A group on the line says how many of its members really are on it.
    for (const [key, node] of parts.nodeEls) {
      const item = view.items.get(key);
      if (item.count > 1) {
        const lit = line.inGroup.get(key) ?? 0;
        setText(node.querySelector('.rueckv-node__sub'), focused && lit && lit < item.count ? `davon ${fmt.number(lit)}` : shortGroup(item.group));
      }
    }
    paintDetail(line);
  }

  function select(key) {
    selected = key;
    paintSelection();
  }

  function entryFor(key) {
    return view.items.get(key) ?? view.sides.find((entry) => entry.key === key);
  }

  function reRoot(id) {
    router.go('rueckverfolgung', { q: id });
  }

  scroller.addEventListener('click', (event) => {
    const target = event.target.closest?.('[data-key]');
    if (target) select(target.getAttribute('data-key'));
    else if (event.target.closest?.('svg')) select(rootKey);
  });
  scroller.addEventListener('dblclick', (event) => {
    const key = event.target.closest?.('[data-key]')?.getAttribute('data-key');
    const entry = key ? entryFor(key) : null;
    if (entry?.node && entry.node.id !== graph.root) reRoot(entry.node.id);
  });
  scroller.addEventListener('keydown', (event) => {
    if (event.key === 'Escape') {
      select(rootKey);
      return;
    }
    if (event.key !== 'Enter' && event.key !== ' ') return;
    const key = event.target.closest?.('[data-key]')?.getAttribute('data-key');
    if (key) {
      event.preventDefault();
      select(key);
    }
  });

  function lineRows(label, ids) {
    const byType = new Map();
    for (const id of ids) {
      const node = model.nodes.get(id);
      if (!node) continue;
      if (!byType.has(node.type)) byType.set(node.type, []);
      byType.get(node.type).push(id);
    }
    if (!byType.size) return null;
    const rows = COLUMN_TYPES.filter((type) => byType.has(type)).map((type) => {
      const list = byType.get(type).sort();
      const shown = list.slice(0, LINE_TAGS);
      return h('div.rueckv-line__row',
        h('span.rueckv-line__type', list.length === 1 ? typeName(type) : `${fmt.number(list.length)} ${typePlural(type)}`),
        h('div.rueckv-tags',
          shown.map((id) => h('a.tag.tag--mono', { href: router.href('rueckverfolgung', { q: id }) }, id)),
          list.length > shown.length ? h('span.faint', `+${fmt.number(list.length - shown.length)}`) : null));
    });
    return h('div.rueckv-line', h('h3.rueckv-side__head', label), rows);
  }

  function paintDetail(line = lineage(view, model, selected)) {
    const entry = entryFor(selected);
    clear(detail.body);
    if (!entry) return;
    const lineParts = [lineRows('Vorfahren, woraus es entstand', line.ancestors), lineRows('Nachkommen, was daraus entstand', line.descendants)];
    if (entry.node) {
      const node = entry.node;
      const relation = node.id === graph.root ? 'root' : node.relation;
      const pairs = [];
      if (node.t !== null && node.t !== undefined) pairs.push(['Zeit', fmt.simTime(node.t)]);
      if (node.qty) pairs.push(['Menge', `${fmt.number(node.qty)} ${node.unit ?? ''}`]);
      for (const [key, value] of Object.entries(node.attrs ?? {})) {
        if (value !== null && value !== undefined && value !== '') pairs.push([key, attrValue(value, fmt)]);
      }
      appendAll(detail.body,
        h('div.rueckv-detail__head',
          h('span.rueckv-detail__type', typeName(node.type)),
          h('span.rueckv-detail__id.mono', node.id),
          relationLabel(relation) ? h('span.tag', relationLabel(relation)) : null),
        h('dl.kv', pairs.flatMap(([key, value]) => [h('dt', key), h('dd', value)])));
      if (node.id !== graph.root) {
        detail.body.appendChild(h('a.btn.btn--sm.rueckv-detail__action', { href: router.href('rueckverfolgung', { q: node.id }) }, 'Als Ausgangspunkt wählen'));
      }
      if (node.type === 'load' && node.id !== graph.root) {
        detail.body.appendChild(h('p.rueckv-detail__hint', 'Eine Ofenlast teilt nur den Ofen, nicht das Material. Sie verbindet hier keine Lose. Als Ausgangspunkt zeigt sie alle Körbe in diesem Ofen.'));
      }
      for (const part of lineParts) {
        if (part) detail.body.appendChild(part);
      }
      return;
    }
    const heading = entry.more ? `${fmt.number(entry.ids.length)} weitere ${typePlural(entry.type)}` : `${fmt.number(entry.count)} ${typePlural(entry.type)}`;
    appendAll(detail.body,
      h('div.rueckv-detail__head', h('span.rueckv-detail__id', heading), entry.group ? h('span.tag.tag--mono', entry.group) : null),
      entry.qty ? h('dl.kv', h('dt', 'Summe'), h('dd', `${fmt.number(entry.qty)} ${entry.unit}`)) : null,
      h('div.rueckv-tags', entry.ids.map((id) => h('a.tag.tag--mono', { href: router.href('rueckverfolgung', { q: id }) }, id))),
      ...lineParts.filter(Boolean));
  }

  function statCell(label, value, tip) {
    return h('div.rueckv-stat', tip ? { 'data-tip': tip } : {}, h('span.rueckv-stat__value', value), h('span.rueckv-stat__label', label));
  }

  function tagRow(label, ids, max = Infinity) {
    if (!ids.length) return null;
    const shown = ids.slice(0, max);
    return h('div.rueckv-recall__row',
      h('h3.rueckv-side__head', label),
      h('div.rueckv-tags',
        shown.map((id) => h('a.tag.tag--mono', { href: router.href('rueckverfolgung', { q: id }) }, id)),
        ids.length > shown.length ? h('span.faint', `+${fmt.number(ids.length - shown.length)} weitere`) : null));
  }

  function paintRecall() {
    const summary = graph.summary ?? {};
    const label = graph.recall_label ?? 'möglich betroffen';
    setText(recall.head.querySelector('.panel__title'), `Rückruf: ${label}`);
    clear(recall.body);
    const packageIds = [...model.nodes.values()].filter((n) => n.type === 'package').map((n) => n.id).sort().reverse();
    appendAll(recall.body,
      h('div.rueckv-stats',
        statCell('Pakete', fmt.number(summary.packages)),
        statCell('Stück', fmt.number(summary.pieces)),
        statCell('kg', fmt.number(summary.kg)),
        statCell('Aufträge', fmt.number(summary.orders?.length ?? 0))),
      tagRow('Aufträge', summary.orders ?? []),
      tagRow('Pakete', packageIds, PACKAGE_LINKS),
      tagRow('Schmelzen, Herkunft', summary.melts ?? []),
      tagRow('Ofenlasten, nur zur Information', summary.loads ?? []),
      h('p.rueckv-recall__scope', 'Zählt nur Vorfahren und Nachkommen des Ausgangspunkts. Eine gemeinsame Ofenlast zieht fremde Lose nicht mit hinein, außer sie ist selbst der Ausgangspunkt.'));
  }

  function paintAnswer(autoPicked) {
    const root = model.nodes.get(graph.root);
    answer.hidden = false;
    setText(title, root?.label ?? graph.root);
    const qty = root?.qty ? `${fmt.number(root.qty)} ${root.unit ?? ''}` : '';
    setText(titleMeta, [root?.t !== null && root?.t !== undefined ? fmt.simTime(root.t) : '', qty, autoPicked ? 'neuestes Paket, automatisch gewählt' : ''].filter(Boolean).join(' · '));
    setText(asked, QUESTIONS[graph.root_type] ?? '');
    clear(originText);
    originText.append(...originLine(graph, model, router, fmt));
    clear(targetText);
    targetText.append(...targetLine(graph, model, router, fmt));
    const cut = graph.truncated;
    note.hidden = !cut;
    if (cut) {
      setText(note.lastChild, `Anzeige gekürzt auf ${fmt.number(graph.nodes.length)} Knoten, die nächsten um den Ausgangspunkt. Die Rückruf-Zahlen gelten für die gesamte Rückverfolgung.`);
    }
    setText(graphPanel.head.querySelector('.panel__title'), `Stammbaum: ${graph.direction_note ?? ''}`);
  }

  function paintList() {
    clear(scroller);
    parts = null;
    const rows = [...model.nodes.values()].sort((a, b) => {
      const ca = COLUMN_TYPES.indexOf(a.type);
      const cb = COLUMN_TYPES.indexOf(b.type);
      return (ca < 0 ? 99 : ca) - (cb < 0 ? 99 : cb) || (a.t ?? 0) - (b.t ?? 0) || a.id.localeCompare(b.id);
    });
    scroller.appendChild(h('table.tbl',
      h('thead', h('tr', h('th', 'Art'), h('th', 'Kennung'), h('th', 'Beziehung'), h('th.num', 'Menge'), h('th', 'Zeit'))),
      h('tbody', rows.map((node) => h(`tr${node.id === graph.root ? '.is-selected' : ''}`,
        h('td', typeName(node.type)),
        h('td', h('a.mono', { href: router.href('rueckverfolgung', { q: node.id }) }, node.id)),
        h('td.muted', relationLabel(node.id === graph.root ? 'root' : node.relation)),
        h('td.num', node.qty ? `${fmt.number(node.qty)} ${node.unit ?? ''}` : fmt.DASH),
        h('td.mono.nowrap', fmt.simTime(node.t)))))));
  }

  function rebuild() {
    view = collapse(model, { expanded });
    orderColumns(view);
    rootKey = view.keyOf.get(graph.root) ?? view.sides.find((entry) => entry.isRoot)?.key ?? null;
    if (selected === null || !entryFor(selected)) selected = rootKey;
    if (mode === 'graph') draw();
    else {
      paintList();
      paintDetail();
    }
  }

  function setMode(next) {
    mode = next;
    graphBtn.setAttribute('aria-pressed', String(next === 'graph'));
    listBtn.setAttribute('aria-pressed', String(next === 'list'));
    legend.hidden = next !== 'graph';
    if (next === 'graph') draw();
    else paintList();
  }
  graphBtn.addEventListener('click', () => setMode('graph'));
  listBtn.addEventListener('click', () => setMode('list'));

  function signature(data) {
    return `${data.nodes?.length}:${data.edges?.length}:${data.summary?.pieces}`;
  }

  function showGraph(data, autoPicked = false) {
    graph = data;
    model = buildModel(data);
    selected = null;
    placeholder.hidden = true;
    graphPanel.node.hidden = false;
    refresh.hidden = true;
    paintAnswer(autoPicked);
    paintRecall();
    rebuild();
    recentKey = '';
    showRecent(recentRows);
  }

  function showPlaceholder(text) {
    answer.hidden = true;
    graphPanel.node.hidden = true;
    placeholder.hidden = false;
    clear(placeholder);
    placeholder.append(
      h('h1', 'Woher kommt es, und was betrifft es?'),
      h('p', text),
      h('ul.rueckv-placeholder__list',
        h('li', h('strong', 'Vorfahren'), ': woraus ein Teil entstanden ist. Vom Paket zurück zum Korb, Strang, Bolzen und zur Schmelze.'),
        h('li', h('strong', 'Nachkommen'), ': was daraus entstanden ist. Von der Schmelze vorwärts bis zu Paket und Auftrag.')));
    clear(recall.body);
    recall.body.appendChild(empty('Wähle einen Ausgangspunkt.'));
    clear(detail.body);
    detail.body.appendChild(empty('Wähle einen Knoten.'));
  }

  async function loadGraph(id, autoPicked = false) {
    const mine = ++token;
    try {
      const data = await api.get(`/api/trace/graph/${encodeURIComponent(id)}`, { quiet: [404] });
      if (!alive || mine !== token) return;
      showGraph(data, autoPicked);
    } catch (error) {
      if (alive && mine === token && error.status === 404) showPlaceholder(`Zu „${id}“ gibt es keinen Knoten.`);
    }
  }

  async function openQuery(query) {
    input.value = query;
    const rows = await runSearch(query);
    if (!alive) return;
    const exact = rows.find((r) => r.id.toLowerCase() === query.toLowerCase());
    if (exact) {
      showHits([], '');
      loadGraph(exact.id);
    } else if (rows.length === 1) {
      loadGraph(rows[0].id);
    } else if (rows.length) {
      showPlaceholder(`${fmt.number(rows.length)} Treffer für „${query}“. Wähle links einen aus.`);
    } else {
      showPlaceholder(`Zu „${query}“ gibt es keinen Knoten.`);
    }
  }

  const resize = new ResizeObserver(() => {
    if (!view || mode !== 'graph') return;
    if (Math.abs(scroller.clientWidth - drawnWidth) > 8) draw();
  });
  resize.observe(scroller);

  refresh.addEventListener('click', () => {
    if (graph) loadGraph(graph.root);
  });

  // The graph does not redraw itself while someone reads it, it offers the newer data.
  const watcher = poll(async () => {
    if (!graph) return;
    const data = await api.get(`/api/trace/graph/${encodeURIComponent(graph.root)}`, { quiet: true });
    if (alive && graph && data.root === graph.root) refresh.hidden = signature(data) === signature(graph);
  }, 10000, { immediate: false });

  const query = router.params.q?.trim();

  function openNewest(rows) {
    const newest = rows.find((r) => r.type === 'package');
    if (newest) loadGraph(newest.id, true);
    else showPlaceholder('Noch ist kein Paket fertig. Wähle links eine Schmelze oder suche nach einer Kennung.');
  }

  const recents = poll(async () => {
    const rows = await fetchRecent();
    if (alive && !query && !graph && !input.value.trim()) openNewest(rows);
  }, 20000, { immediate: false });

  (async () => {
    let rows = null;
    try {
      rows = await fetchRecent();
    } catch {
      rows = null;
    }
    if (!alive) return;
    if (query) openQuery(query);
    else if (rows) openNewest(rows);
    else showPlaceholder('Das MES antwortet gerade nicht. Die Ansicht füllt sich, sobald es wieder da ist.');
  })();

  return () => {
    alive = false;
    clearTimeout(searchTimer);
    resize.disconnect();
    watcher.stop();
    recents.stop();
  };
}
