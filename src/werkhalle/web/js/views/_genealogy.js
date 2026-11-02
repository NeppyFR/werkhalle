// Genealogy graph as data: columns, grouping of big columns, barycentre ordering, row placement. No DOM, so it can run in Node.
//
// The flow is melt -> billet -> strand -> basket -> package -> order. Loads and lots stand beside it: they group material without
// being a step of it, so they never take part in ordering or in the ancestor and descendant walks.

export const COLUMN_TYPES = ['melt', 'billet', 'strand', 'basket', 'package', 'order'];
export const COLUMN_LIMIT = 16;
const SIDE_TYPES = new Set(['load', 'lot']);
const SIDE_LIMIT = 8;
const SWEEPS = 10;

const NAMES = {
  melt: ['Schmelze', 'Schmelzen'],
  billet: ['Bolzen', 'Bolzen'],
  strand: ['Strang', 'Stränge'],
  basket: ['Korb', 'Körbe'],
  package: ['Paket', 'Pakete'],
  order: ['Auftrag', 'Aufträge'],
  load: ['Ofenlast', 'Ofenlasten'],
  lot: ['Los', 'Lose'],
};

export function typeName(type) {
  return NAMES[type]?.[0] ?? type;
}

export function typePlural(type) {
  return NAMES[type]?.[1] ?? type;
}

function byTimeThenId(a, b) {
  return (a.t ?? 0) - (b.t ?? 0) || String(a.id).localeCompare(String(b.id));
}

/**
 * Splits the API's edges into flow and side links. A side link is recognised by touching a load or lot, in either direction,
 * because a load root points at its baskets while a side load is pointed at by them. Edges to nodes cut by the cap are dropped.
 */
export function buildModel(graph) {
  const nodes = new Map();
  for (const node of graph.nodes ?? []) nodes.set(node.id, node);
  const flow = [];
  const side = [];
  for (const edge of graph.edges ?? []) {
    const from = nodes.get(edge.from);
    const to = nodes.get(edge.to);
    if (!from || !to) continue;
    const fromSide = SIDE_TYPES.has(from.type);
    const toSide = SIDE_TYPES.has(to.type);
    if (fromSide && toSide) continue;
    if (fromSide || toSide) {
      side.push({ side: fromSide ? from.id : to.id, member: fromSide ? to.id : from.id, qty: edge.qty, unit: edge.unit });
    } else {
      flow.push(edge);
    }
  }
  return { root: graph.root, rootType: graph.root_type, truncated: Boolean(graph.truncated), nodes, flow, side };
}

function groupName(node, parentGroup) {
  const attrs = node.attrs ?? {};
  switch (node.type) {
    case 'melt': return attrs.Legierung ? `Legierung ${attrs.Legierung}` : 'Schmelzen';
    case 'billet': return attrs.Schmelze ?? parentGroup ?? 'Bolzen';
    case 'order': return attrs.Produkt ?? 'Aufträge';
    default: return attrs.Los ?? parentGroup ?? typePlural(node.type);
  }
}

/**
 * The graph as shown: every column holds single nodes, or groups once it has more than `limit` nodes and is not in `expanded`.
 * Groups follow the material (billets by melt, the rest by lot), so a group's edges still say something. The root is never grouped.
 */
export function collapse(model, { limit = COLUMN_LIMIT, expanded = new Set() } = {}) {
  const parents = new Map();
  for (const edge of model.flow) {
    if (!parents.has(edge.to)) parents.set(edge.to, edge.from);
  }
  const keyOf = new Map();
  const lineageName = new Map();
  const items = new Map();

  const single = (node, col) => {
    const item = {
      key: node.id, type: node.type, col, ids: [node.id], node, count: 1, qty: node.qty ?? 0, unit: node.unit ?? '',
      relation: node.relation, isRoot: node.id === model.root, group: null,
    };
    items.set(item.key, item);
    keyOf.set(node.id, item.key);
    lineageName.set(node.id, node.id);
    return item;
  };

  const columns = COLUMN_TYPES.map((type, col) => {
    const members = [...model.nodes.values()].filter((n) => n.type === type).sort(byTimeThenId);
    const folded = members.length > limit && !expanded.has(type);
    const list = [];
    if (!folded) {
      for (const node of members) list.push(single(node, col));
    } else {
      const groups = new Map();
      for (const node of members) {
        if (node.id === model.root) {
          list.push(single(node, col));
          continue;
        }
        const name = groupName(node, lineageName.get(parents.get(node.id)));
        lineageName.set(node.id, name);
        if (!groups.has(name)) groups.set(name, []);
        groups.get(name).push(node);
      }
      for (const [name, group] of groups) {
        if (group.length === 1) {
          list.push(single(group[0], col));
          lineageName.set(group[0].id, name);
          continue;
        }
        const item = {
          key: `gruppe:${type}:${name}`, type, col, ids: group.map((n) => n.id), node: null, count: group.length,
          qty: group.reduce((sum, n) => sum + (n.qty ?? 0), 0), unit: group[0].unit ?? '',
          relation: group[0].relation, isRoot: false, group: name,
        };
        items.set(item.key, item);
        for (const node of group) keyOf.set(node.id, item.key);
        list.push(item);
      }
    }
    return { type, col, total: members.length, folded, items: list };
  });

  const merged = new Map();
  for (const edge of model.flow) {
    const from = keyOf.get(edge.from);
    const to = keyOf.get(edge.to);
    if (!from || !to || from === to) continue;
    const key = `${from}\n${to}`;
    const row = merged.get(key);
    if (row) {
      row.qty += edge.qty ?? 0;
      row.count += 1;
    } else {
      merged.set(key, { from, to, qty: edge.qty ?? 0, unit: edge.unit ?? '', count: 1 });
    }
  }

  return { root: model.root, columns, items, keyOf, edges: [...merged.values()], sides: buildSides(model, items, keyOf) };
}

function buildSides(model, items, keyOf) {
  const byHolder = new Map();
  for (const node of model.nodes.values()) {
    if (SIDE_TYPES.has(node.type)) byHolder.set(node.id, []);
  }
  for (const link of model.side) byHolder.get(link.side)?.push(link);

  const sides = [];
  for (const [id, links] of byHolder) {
    const memberKeys = [...new Set(links.map((l) => keyOf.get(l.member)).filter(Boolean))];
    const col = memberKeys.length ? items.get(memberKeys[0]).col : null;
    const node = model.nodes.get(id);
    sides.push({ key: id, type: node.type, node, ids: [id], memberKeys, memberCount: links.length, col, isRoot: id === model.root, more: false });
  }
  sides.sort((a, b) => Number(b.isRoot) - Number(a.isRoot) || b.memberCount - a.memberCount || a.key.localeCompare(b.key));
  if (sides.length <= SIDE_LIMIT) return sides;

  const shown = sides.slice(0, SIDE_LIMIT - 1);
  const rest = sides.slice(SIDE_LIMIT - 1);
  shown.push({
    key: 'gruppe:load:weitere', type: rest[0].type, node: null, ids: rest.map((r) => r.key), memberKeys: [], memberCount: 0,
    col: rest[0].col, isRoot: false, more: true,
  });
  return shown;
}

function neighbours(view) {
  const into = new Map();
  const out = new Map();
  for (const edge of view.edges) {
    if (!out.has(edge.from)) out.set(edge.from, []);
    if (!into.has(edge.to)) into.set(edge.to, []);
    out.get(edge.from).push(edge.to);
    into.get(edge.to).push(edge.from);
  }
  return { into, out };
}

/** Number of edge crossings for the current order of `view.columns[*].items`. */
export function countCrossings(view) {
  const pos = new Map();
  for (const column of view.columns) column.items.forEach((item, i) => pos.set(item.key, i));
  let total = 0;
  for (let c = 0; c + 1 < view.columns.length; c += 1) {
    const between = view.edges
      .filter((e) => view.items.get(e.from).col === c && view.items.get(e.to).col === c + 1)
      .map((e) => [pos.get(e.from), pos.get(e.to)])
      .sort((a, b) => a[0] - b[0] || a[1] - b[1]);
    for (let i = 0; i < between.length; i += 1) {
      for (let j = i + 1; j < between.length; j += 1) {
        if (between[i][0] < between[j][0] && between[i][1] > between[j][1]) total += 1;
      }
    }
  }
  return total;
}

/** Barycentre sweeps, left to right and back. Keeps the order with the fewest crossings it saw. Reorders `view.columns[*].items`. */
export function orderColumns(view, sweeps = SWEEPS) {
  const { into, out } = neighbours(view);
  // Baskets of one load sit together where the flow leaves the choice open, which keeps the load's rail short.
  const holder = new Map();
  for (const side of view.sides) {
    for (const key of side.memberKeys) {
      if (!holder.has(key)) holder.set(key, side.key);
    }
  }

  const pos = new Map();
  for (const column of view.columns) column.items.forEach((item, i) => pos.set(item.key, i));
  const reorder = (column, table) => {
    const score = new Map();
    for (const item of column.items) {
      const around = table.get(item.key) ?? [];
      score.set(item.key, around.length ? around.reduce((sum, k) => sum + pos.get(k), 0) / around.length : pos.get(item.key));
    }
    column.items.sort((a, b) => score.get(a.key) - score.get(b.key)
      || String(holder.get(a.key) ?? '').localeCompare(String(holder.get(b.key) ?? ''))
      || pos.get(a.key) - pos.get(b.key));
    column.items.forEach((item, i) => pos.set(item.key, i));
  };

  let best = countCrossings(view);
  let bestOrder = view.columns.map((c) => [...c.items]);
  for (let sweep = 0; sweep < sweeps && best > 0; sweep += 1) {
    for (let c = 1; c < view.columns.length; c += 1) reorder(view.columns[c], into);
    for (let c = view.columns.length - 2; c >= 0; c -= 1) reorder(view.columns[c], out);
    const crossings = countCrossings(view);
    if (crossings < best) {
      best = crossings;
      bestOrder = view.columns.map((c) => [...c.items]);
    }
  }
  view.columns.forEach((column, c) => { column.items = bestOrder[c]; });
  return best;
}

// Closest positions to `wanted` that keep the order and the minimum spacing: pool adjacent violators on the spacing-free values.
function resolveRow(wanted, heights, gap) {
  const offset = [0];
  for (let i = 1; i < wanted.length; i += 1) offset[i] = offset[i - 1] + (heights[i - 1] + heights[i]) / 2 + gap;
  const blocks = [];
  wanted.forEach((value, i) => {
    blocks.push({ sum: value - offset[i], n: 1 });
    while (blocks.length > 1) {
      const last = blocks[blocks.length - 1];
      const before = blocks[blocks.length - 2];
      if (before.sum / before.n <= last.sum / last.n) break;
      before.sum += last.sum;
      before.n += last.n;
      blocks.pop();
    }
  });
  const result = [];
  let i = 0;
  for (const block of blocks) {
    const mean = block.sum / block.n;
    for (let k = 0; k < block.n; k += 1) {
      result.push(mean + offset[i]);
      i += 1;
    }
  }
  return result;
}

/**
 * Vertical centres so that edges run as straight as the order allows. Sets `item.y` and `item.h`; returns the total height.
 * Sizes are the caller's, because they belong to the drawing.
 */
export function placeRows(view, { rowH = 22, groupH = 34, gap = 5 } = {}) {
  const { into, out } = neighbours(view);
  for (const column of view.columns) {
    let y = 0;
    for (const item of column.items) {
      item.h = item.count > 1 ? groupH : rowH;
      item.y = y + item.h / 2;
      y += item.h + gap;
    }
  }
  const pass = (column, tables) => {
    if (!column.items.length) return;
    const wanted = column.items.map((item) => {
      const around = tables.flatMap((table) => table.get(item.key) ?? []);
      return around.length ? around.reduce((sum, k) => sum + view.items.get(k).y, 0) / around.length : item.y;
    });
    const placed = resolveRow(wanted, column.items.map((i) => i.h), gap);
    column.items.forEach((item, i) => { item.y = placed[i]; });
  };
  for (let round = 0; round < 8; round += 1) {
    for (let c = 1; c < view.columns.length; c += 1) pass(view.columns[c], [into]);
    for (let c = view.columns.length - 2; c >= 0; c -= 1) pass(view.columns[c], [out]);
  }
  for (const column of view.columns) pass(column, [into, out]);

  let top = Infinity;
  let bottom = -Infinity;
  for (const item of view.items.values()) {
    top = Math.min(top, item.y - item.h / 2);
    bottom = Math.max(bottom, item.y + item.h / 2);
  }
  if (!Number.isFinite(top)) return 0;
  for (const item of view.items.values()) item.y -= top;
  return bottom - top;
}

function walk(starts, table) {
  const seen = new Set(starts);
  const reached = new Set();
  const stack = [...starts];
  while (stack.length) {
    for (const next of table.get(stack.pop()) ?? []) {
      if (!seen.has(next)) {
        seen.add(next);
        reached.add(next);
        stack.push(next);
      }
    }
  }
  return reached;
}

/**
 * What belongs to one selected item: its ancestors and descendants, by edge direction only, so a selection never spreads sideways
 * across a shared load. The walk runs on the real nodes, not on the groups: a group on the way must not pull in members that have
 * no link to the selection. A selected load starts from its baskets. Loads light up when one of their baskets is lit.
 *
 * Returns `keys` and `edges` for the drawing (view keys, edges as "from\nto"), `inGroup` (view key -> members lit), `ancestors` and `descendants` (node ids).
 */
export function lineage(view, model, key) {
  const entry = view.items.get(key) ?? view.sides.find((side) => side.key === key);
  const into = new Map();
  const out = new Map();
  for (const edge of model.flow) {
    if (!out.has(edge.from)) out.set(edge.from, []);
    if (!into.has(edge.to)) into.set(edge.to, []);
    out.get(edge.from).push(edge.to);
    into.get(edge.to).push(edge.from);
  }
  const isSide = !view.items.has(key);
  const starts = isSide ? model.side.filter((link) => entry?.ids.includes(link.side)).map((link) => link.member) : entry?.ids ?? [];
  const ancestors = walk(starts, into);
  const descendants = walk(starts, out);
  const ids = new Set([...starts, ...ancestors, ...descendants]);

  const keys = new Set([key]);
  const inGroup = new Map();
  for (const id of ids) {
    const itemKey = view.keyOf.get(id);
    if (itemKey) {
      keys.add(itemKey);
      inGroup.set(itemKey, (inGroup.get(itemKey) ?? 0) + 1);
    }
  }
  const edges = new Set();
  for (const edge of model.flow) {
    if (ids.has(edge.from) && ids.has(edge.to)) edges.add(`${view.keyOf.get(edge.from)}\n${view.keyOf.get(edge.to)}`);
  }
  for (const side of view.sides) {
    if (side.memberKeys.some((k) => keys.has(k))) keys.add(side.key);
  }
  return { keys, edges, inGroup, ancestors, descendants };
}
