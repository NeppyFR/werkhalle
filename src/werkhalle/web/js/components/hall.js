// The hall: an SVG floor plan fed by a pluggable provider, so the twin can replay frames through the same drawing.
//   const hall = createHall(el, { provider, onSelect });  provider() -> {t, machines, material}
// `machines` is the array from /api/state or the id->row map of a twin frame. A rAF loop reads the provider and only
// re-derives text and classes when the provider hands out new data; between updates it only interpolates geometry.
import { s, setText, setAttr, setClass, clear } from '../core/dom.js';
import { formatDuration, formatLotId, formatNumber, formatPercent, formatRepairLeft, formatSimTime, formatWaiting, waitingTip, DASH, machineName, plural, statusLabel, statusTag, statusTone } from '../core/format.js';
import { SHAPES } from './ui.js';
import { ALLOY_ORDER, ANCHORS, AREAS, BAYS, BUFFERS, CAPACITY, COLUMNS, KIND_OF, LEGEND, PICKUP, PLATE, RUNS, STATIONS, UPSTREAM, VIEW } from './hall-layout.js';

let instances = 0;
const reducedMotion = window.matchMedia?.('(prefers-reduced-motion: reduce)');

const clamp = (v, lo = 0, hi = 1) => Math.max(lo, Math.min(hi, v));
const lerp = (a, b, f) => a + (b - a) * f;

function lotTone(id) {
  if (!id) return 'var(--metal-3)';
  let hash = 0;
  for (const ch of id) hash = (hash * 31 + ch.charCodeAt(0)) >>> 0;
  return `var(--lot-${hash % 6})`;
}

function rect(cls, x, y, width, height, extra = {}) {
  return s('rect', { class: cls, x, y, width, height, ...extra });
}

function text(cls, x, y, value = '', extra = {}) {
  return s('text', { class: cls, x, y, ...extra }, value);
}

function polyPoints(points) {
  return points.map(([x, y]) => `${x},${y}`).join(' ');
}

function measure(points) {
  const lengths = [0];
  for (let i = 1; i < points.length; i += 1) {
    lengths.push(lengths[i - 1] + Math.hypot(points[i][0] - points[i - 1][0], points[i][1] - points[i - 1][1]));
  }
  return { points, lengths, total: lengths[lengths.length - 1] || 1 };
}

function pointAt(run, fraction) {
  const distance = clamp(fraction) * run.total;
  const { points, lengths } = run;
  for (let i = 1; i < points.length; i += 1) {
    if (distance <= lengths[i]) {
      const span = lengths[i] - lengths[i - 1] || 1;
      const f = (distance - lengths[i - 1]) / span;
      return [lerp(points[i - 1][0], points[i][0], f), lerp(points[i - 1][1], points[i][1], f)];
    }
  }
  return points[points.length - 1];
}

const RUN_TABLE = new Map(Object.entries(RUNS).map(([key, points]) => [key, measure(points)]));

function runFor(from, to) {
  const known = RUN_TABLE.get(`${from}>${to}`);
  if (known) return known;
  if (ANCHORS[from] && ANCHORS[to]) return measure([ANCHORS[from], ANCHORS[to]]);
  return null;
}

// ------------------------------------------------------------------------------------------- item glyphs
function defineItems(uid) {
  const billet = s('g', { id: `${uid}-billet` },
    rect('it-metal', -10, -4.5, 19, 9, { rx: 2 }),
    s('ellipse', { class: 'it-face', cx: 9, cy: 0, rx: 2.4, ry: 4.5 }));
  const basket = s('g', { id: `${uid}-basket` },
    rect('it-frame', -11, -8, 22, 16),
    s('path', { class: 'it-bars', d: 'M-9-4.5H9M-9-1.5H9M-9 1.5H9M-9 4.5H9' }));
  const bundle = s('g', { id: `${uid}-bundle` },
    s('path', { class: 'it-bars it-bars--thick', d: 'M-17-5H17M-17-2H17M-17 1H17M-17 4H17' }),
    s('path', { class: 'it-band', d: 'M-9-7V6M9-7V6' }));
  const pack = s('g', { id: `${uid}-package` },
    rect('it-block', -14, -8, 28, 16, { rx: 1 }),
    s('path', { class: 'it-strap', d: 'M-7-8V8M7-8V8' }));
  return [billet, basket, bundle, pack];
}

const GLYPH = { billets: 'billet', baskets: 'basket', pieces: 'bundle', packages: 'package' };
const GLYPH_STEP = { billets: [0, -9], baskets: [4, -4], pieces: [0, -5], packages: [3, -5] };

function makeItem(uid, transport) {
  const glyph = GLYPH[transport.kind] ?? 'package';
  const g = s('g.item');
  const shown = Math.min(3, Math.max(1, transport.kind === 'pieces' ? 2 : transport.count ?? 1));
  const [dx, dy] = GLYPH_STEP[transport.kind] ?? [0, -4];
  for (let i = shown - 1; i >= 0; i -= 1) {
    g.appendChild(s('use', { href: `#${uid}-${glyph}`, x: dx * i, y: dy * i }));
  }
  g.appendChild(rect('item__tag', -2, 8, 7, 3, { style: `fill:${lotTone(transport.lot_id)}` }));
  if ((transport.count ?? 1) > shown) {
    g.appendChild(text('item__count', 14, 4, formatNumber(transport.count)));
  }
  const count = transport.count ?? 1;
  const amount = {
    billets: plural(count, 'Bolzen', 'Bolzen'),
    baskets: plural(count, 'Korb', 'Körbe'),
    pieces: plural(count, 'Stange', 'Stangen'),
    packages: plural(count, 'Paket', 'Pakete'),
  }[transport.kind] ?? formatNumber(count);
  g.setAttribute('data-tip', transport.lot_id ? `${amount} · ${formatLotId(transport.lot_id)}` : amount);
  return g;
}

// ------------------------------------------------------------------------------------------- nameplate
function buildPlate(x, y, id) {
  const w = PLATE.w;
  const iconPath = s('path', { class: 'plate__shape', d: SHAPES.IDLE.d });
  const parts = {
    bg: rect('plate__bg', 0, 0, w, PLATE.h, { rx: 2 }),
    stripe: rect('plate__stripe', 0, 0, 4, PLATE.h),
    status: text('plate__status', 25, 33),
    since: text('plate__since', w - 8, 33, '', { 'text-anchor': 'end' }),
    warn: text('plate__warn', w - 8, 15, '', { 'text-anchor': 'end' }),
    temp: text('plate__val', 12, 49),
    power: text('plate__val', 60, 49),
    oeeBar: rect('plate__oee', 138, 42, 0, 6),
    oee: text('plate__val', w - 8, 49, '', { 'text-anchor': 'end' }),
    lot: text('plate__val', 12, 64),
    pct: text('plate__val plate__val--dim', w - 8, 64, '', { 'text-anchor': 'end' }),
    bar: rect('plate__bar', 0, PLATE.h - 3, 0, 3),
    name: text('plate__name', 12, 15),
  };
  const group = s('g.plate', { transform: `translate(${x} ${y})` },
    parts.bg, parts.stripe,
    text('plate__id', 12, 15, id),
    parts.name, parts.warn,
    s('g', { transform: 'translate(11 23.5) scale(.92)' }, iconPath), parts.status, parts.since,
    parts.temp, parts.power,
    text('plate__label', 112, 49, 'OEE'),
    rect('plate__oee-track', 138, 42, 34, 6), parts.oeeBar, parts.oee,
    parts.lot, parts.pct,
    rect('plate__bar-track', 0, PLATE.h - 3, w, 3), parts.bar);
  parts.name.setAttribute('x', id.length > 3 ? 48 : 40);

  return {
    group,
    update(m, t) {
      const tone = statusTone(m.status);
      setClass(group, `plate plate--${tone}${m.starved ? ' plate--starved' : ''}`);
      setText(parts.name, m.name ?? machineName(id));
      const shape = SHAPES[m.status] ?? SHAPES.IDLE;
      setAttr(iconPath, 'd', shape.d);
      setClass(iconPath, `plate__shape${shape.hollow ? ' plate__shape--hollow' : ''}`);
      setText(parts.status, m.status === 'FAULT' && m.alarm?.code ? `${statusTag(m.status)} ${m.alarm.code}` : statusTag(m.status));
      setText(parts.since, typeof m.since === 'number' ? formatDuration(Math.max(0, t - m.since)) : '');
      setText(parts.temp, typeof m.temperature === 'number' ? `${formatNumber(m.temperature)} °C` : `${DASH} °C`);
      setText(parts.power, typeof m.power === 'number' ? `${formatNumber(m.power)} kW` : `${DASH} kW`);
      setAttr(parts.oeeBar, 'width', typeof m.oee === 'number' ? (clamp(m.oee) * 34).toFixed(1) : 0);
      setText(parts.oee, formatPercent(m.oee));
      setText(parts.lot, plateLotLine(m, t));
      const showPct = m.lots.length > 0 && m.status !== 'FAULT' && m.status !== 'SETUP';
      setText(parts.pct, showPct ? formatPercent(m.progress) : '');
      setText(parts.warn, m.alarm?.active && m.alarm.severity < 900 ? `Warnung ${m.alarm.code}` : '');
    },
    setProgress(p) {
      setAttr(parts.bar, 'width', (clamp(p) * w).toFixed(1));
    },
  };
}

function plateLotLine(m, t) {
  if (m.status === 'FAULT') return formatRepairLeft(m.estRepair, m.since, t) ?? 'Reparatur läuft';
  if (m.lots.length) {
    const lots = m.lots.length > 1 ? `${formatLotId(m.lots[0])} +${m.lots.length - 1}` : formatLotId(m.lots[0]);
    return m.product ? `${lots} · ${m.product}` : lots;
  }
  if (m.opLabel) return formatLotId(m.opLabel);
  return m.waiting ? formatWaiting(m.waiting) : DASH;
}

function buildFlag(x, y) {
  const code = text('flag__code', 92, 15);
  const group = s('g.flag', { transform: `translate(${x} ${y})`, visibility: 'hidden' },
    rect('flag__halo', -3, -3, 148, 28, { rx: 3 }),
    rect('flag__bg', 0, 0, 142, 22, { rx: 2 }),
    s('path', { class: 'flag__icon', transform: 'translate(5 4) scale(1.15)', d: SHAPES.FAULT.d }),
    s('path', { class: 'flag__mark', transform: 'translate(5 4) scale(1.15)', d: 'M6 4.4v3.2M6 8.8v.8' }),
    text('flag__text', 22, 15, 'STÖRUNG'),
    code);
  return {
    group,
    update(m) {
      const on = m.status === 'FAULT';
      setAttr(group, 'visibility', on ? 'visible' : 'hidden');
      if (on) setText(code, m.alarm?.code ?? '');
    },
  };
}

function stackLight(x, y) {
  const lamps = {
    fault: s('circle.lamp', { cx: x, cy: y + 3.5, r: 2.6 }),
    setup: s('circle.lamp', { cx: x, cy: y + 9.5, r: 2.6 }),
    run: s('circle.lamp', { cx: x, cy: y + 15.5, r: 2.6 }),
  };
  const group = s('g', rect('light__body', x - 3.5, y, 7, 19, { rx: 1.5 }), lamps.fault, lamps.setup, lamps.run, rect('eq3', x - 1, y + 19, 2, 6));
  return {
    group,
    update(status) {
      const tone = statusTone(status);
      for (const [key, lamp] of Object.entries(lamps)) setClass(lamp, key === tone ? `lamp lamp--${key}` : 'lamp');
    },
  };
}

// ------------------------------------------------------------------------------------------- stations
function makeStation(ctx, id) {
  const cfg = STATIONS[id];
  const [hx, hy, hw, hh] = cfg.hit;
  const art = s('g.st__art');
  const plate = buildPlate(cfg.plate[0], cfg.plate[1], id);
  const hit = rect('st__hit', hx, hy, hw, hh, { rx: 3 });
  const outline = rect('st__outline', hx, hy, hw, hh, { rx: 3 });
  const [wx, wy, ww, wh] = cfg.work;
  const hatch = rect('st__hatch', wx, wy, ww, wh, { rx: 3, fill: `url(#${ctx.uid}-hatch)`, visibility: 'hidden' });
  const group = s('g.st', {
    tabindex: ctx.interactive ? 0 : undefined,
    role: ctx.interactive ? 'button' : 'img',
    'data-machine': id,
  }, hit, art, hatch, plate.group, outline);
  return { id, cfg, art, plate, group, hatch, outline };
}

function drawCast(ctx, st) {
  const { art } = st;
  const light = stackLight(136, 118);
  art.append(
    // Holding furnace, cut open so the melt is visible
    rect('eq4 edge', 36, 154, 94, 96, { rx: 3 }),
    rect('eq3', 30, 142, 106, 14, { rx: 2 }),
    rect('eq3 edge', 74, 114, 16, 28),
    rect('eq5', 46, 170, 74, 70, { rx: 2 }),
    light.group,
  );
  const glow = s('ellipse', { class: 'melt-glow', cx: 83, cy: 186, rx: 44, ry: 22, fill: `url(#${ctx.uid}-glow)` });
  const pool = s('path', { class: 'melt', d: 'M50 188q8-3 16 0t16 0 16 0 18 0V236H50Z' });
  const skin = s('path', { class: 'melt-skin', d: 'M50 188q8-3 16 0t16 0 16 0 18 0' });
  art.append(glow, pool, skin);
  // Launder down to the casting table
  art.append(s('path', { class: 'eq3 edge', d: 'M128 176 L162 188 L162 196 L128 184Z' }));
  const stream = s('path', { class: 'melt-stream', d: 'M130 180 L160 191' });
  art.append(stream);
  // Casting table and pit
  art.append(
    rect('pit', 150, 204, 92, 96),
    rect('eq3 edge', 148, 192, 96, 12, { rx: 1 }),
    rect('eq2', 160, 184, 14, 8),
    rect('eq2', 188, 184, 14, 8),
    rect('eq2', 216, 184, 14, 8),
  );
  const logs = [161, 189, 217].map((x) => rect('log', x, 204, 12, 2));
  const platen = rect('eq2 edge', 154, 206, 84, 5);
  art.append(...logs, platen);
  const flag = buildFlag(142, 118);
  ctx.flags.appendChild(flag.group);
  const count = text('art__note', 34, 268, '');
  art.append(count);

  return {
    flag,
    sync(m) {
      light.update(m.status);
      const running = m.status === 'RUN';
      setClass(pool, `melt${running ? ' melt--on' : m.status === 'FAULT' ? ' melt--cold' : ''}`);
      setClass(skin, `melt-skin${running ? ' melt-skin--on' : ''}`);
      setAttr(glow, 'visibility', running ? 'visible' : 'hidden');
      pool.style.fill = running ? `url(#${ctx.uid}-melt)` : '';
      setAttr(stream, 'visibility', running ? 'visible' : 'hidden');
      setText(count, m.opLabel ?? '');
    },
    animate(p) {
      const length = 2 + p * 84;
      for (const log of logs) log.setAttribute('height', length.toFixed(1));
      platen.setAttribute('y', (204 + length).toFixed(1));
    },
  };
}

function drawPress(ctx, st) {
  const { art } = st;
  const { x0, cy } = st.cfg;
  const containerX = x0 + 160;
  const dieX = x0 + 214;
  const light = stackLight(x0 + 224, cy - 66);

  // Run-out table, cooling bed and basket station lie under the press drawing
  const table = s('polyline', { class: 'table', points: polyPoints([[dieX + 18, cy], [996, cy]]) });
  const tableRollers = s('polyline', { class: 'table__rollers', points: polyPoints([[dieX + 18, cy], [996, cy]]) });
  const bedX = 880;
  const bed = s('g',
    rect('bed', bedX, cy - 90, 116, 76),
    s('path', { class: 'bed__chain', d: `M${bedX + 14} ${cy - 90}V${cy - 10}M${bedX + 44} ${cy - 90}V${cy - 10}M${bedX + 74} ${cy - 90}V${cy - 10}M${bedX + 104} ${cy - 90}V${cy - 10}` }));
  const bedStrands = s('g');
  art.append(table, tableRollers, bed, bedStrands);

  const strand = rect('strand', dieX + 18, cy - 2.5, 0, 5, { fill: `url(#${ctx.uid}-strand)` });
  art.append(strand);

  // Basket station at the end of the line
  const stationRack = s('g', { transform: `translate(${bedX - 20} ${cy + 26})` });
  const stationCount = text('art__note', bedX - 48, cy + 29, '', { 'text-anchor': 'end' });
  art.append(rect('station', bedX - 42, cy + 12, 44, 26), stationRack);
  const readout = text('art__note', dieX + 26, cy + 30, '');
  art.append(readout, stationCount);

  // Billet tray above the container
  art.append(
    rect('tray', x0 + 112, cy - 54, 104, 10, { rx: 1 }),
    rect('eq3', x0 + 118, cy - 44, 4, 16),
    rect('eq3', x0 + 206, cy - 44, 4, 16),
  );
  const trayBillets = s('g');
  art.append(trayBillets);

  // Frame: foundation, tie rods, rear crosshead, main cylinder
  art.append(
    rect('eq5', x0 - 3, cy + 40, 240, 5),
    rect('eq2', x0, cy - 38, 232, 4),
    rect('eq2', x0, cy + 34, 232, 4),
    rect('eq3 edge', x0, cy - 40, 9, 80),
    rect('eq3 edge', x0 + 9, cy - 26, 31, 52, { rx: 2 }),
    s('path', { class: 'rib', d: `M${x0 + 16} ${cy - 26}V${cy + 26}M${x0 + 24} ${cy - 26}V${cy + 26}M${x0 + 32} ${cy - 26}V${cy + 26}` }),
  );
  const rod = rect('eq1', x0 + 40, cy - 5, 48, 10);
  const crosshead = rect('eq2 edge', x0 + 88, cy - 30, 8, 60, { rx: 1 });
  const stem = rect('eq1', x0 + 96, cy - 7, 64, 14);
  art.append(rod, crosshead, stem);

  // Container with the bore, and the billet inside
  art.append(
    rect('eq3 edge', containerX, cy - 28, 54, 56),
    rect('eq5', containerX, cy - 11, 54, 22),
  );
  const billet = rect('billet', containerX, cy - 9.5, 44, 19, { rx: 1 });
  const dropBillet = rect('billet', containerX, cy - 9.5, 44, 19, { rx: 1, visibility: 'hidden' });
  art.append(billet, dropBillet);

  // Front platen with the die
  art.append(
    rect('eq3 edge die', dieX, cy - 40, 18, 80),
    rect('eq1', dieX - 3, cy - 11, 5, 22),
  );
  art.append(light.group);
  art.append(text('art__floor', x0, cy - 60, st.id));

  const flag = buildFlag(x0 + 62, cy - 86);
  ctx.flags.appendChild(flag.group);

  let opKey = null;
  let nBillets = 1;
  let shownStrands = -1;
  let shownTray = -1;
  let shownBaskets = -1;

  function setBedStrands(count) {
    if (count === shownStrands) return;
    shownStrands = count;
    clear(bedStrands);
    for (let i = 0; i < Math.min(count, 9); i += 1) {
      bedStrands.appendChild(rect('strand--cold', bedX + 4, cy - 22 - i * 7.5, 108, 3));
    }
  }

  function setTray(count) {
    if (count === shownTray) return;
    shownTray = count;
    clear(trayBillets);
    for (let i = 0; i < Math.min(count, 7); i += 1) {
      trayBillets.appendChild(s('use', { href: `#${ctx.uid}-billet`, x: x0 + 202 - i * 13.5, y: cy - 58 }));
    }
  }

  function setBaskets(count) {
    if (count === shownBaskets) return;
    shownBaskets = count;
    clear(stationRack);
    if (count > 0) stationRack.appendChild(s('use', { href: `#${ctx.uid}-basket`, x: 0, y: 0 }));
    setText(stationCount, count > 0 ? plural(count, 'Korb', 'Körbe') : '');
  }

  return {
    flag,
    sync(m, inMachine) {
      light.update(m.status);
      if (m.opKey !== opKey) {
        opKey = m.opKey;
        const left = inMachine?.billets_left ?? 0;
        nBillets = Math.max(1, Math.round(left / Math.max(0.05, 1 - m.progress)));
      }
      // billets_left is only trusted for the first estimate of the lot size; after that progress says how far it is.
      const done = Math.min(nBillets, Math.floor(m.progress * nBillets + 1e-6));
      const active = m.lots.length > 0;
      setBedStrands(active ? done : 0);
      setTray(active ? Math.max(0, nBillets - done - 1) : 0);
      setBaskets(active ? inMachine?.baskets ?? 0 : 0);
      setText(readout, active ? `Bolzen ${Math.min(done + 1, nBillets)} von ${nBillets}` : '');
      st.running = m.status === 'RUN';
      st.active = active;
      st.setup = m.status === 'SETUP';
    },
    animate(p) {
      if (!st.active) {
        setStroke(-44, 0, false);
        billet.setAttribute('visibility', 'hidden');
        dropBillet.setAttribute('visibility', 'hidden');
        strand.setAttribute('width', 0);
        return;
      }
      if (st.setup) {
        setStroke(-44, 0, false);
        billet.setAttribute('visibility', 'hidden');
        dropBillet.setAttribute('visibility', 'hidden');
        strand.setAttribute('width', 0);
        return;
      }
      const cycle = (p * nBillets) % 1;
      let extension;
      if (cycle < 0.1) {
        extension = lerp(50, -44, cycle / 0.1);
      } else {
        extension = lerp(-44, 50, (cycle - 0.1) / 0.9);
      }
      const inLoad = cycle < 0.1;
      setStroke(extension, cycle, inLoad);
    },
  };

  function setStroke(extension, cycle, inLoad) {
    rod.setAttribute('width', (48 + extension).toFixed(1));
    crosshead.setAttribute('x', (x0 + 88 + extension).toFixed(1));
    stem.setAttribute('x', (x0 + 96 + extension).toFixed(1));
    const tip = x0 + 160 + extension;
    if (inLoad) {
      billet.setAttribute('visibility', 'hidden');
      strand.setAttribute('width', 0);
      const drop = clamp((cycle - 0.04) / 0.06);
      if (drop > 0) {
        dropBillet.setAttribute('visibility', 'visible');
        dropBillet.setAttribute('x', (x0 + 116).toFixed(1));
        dropBillet.setAttribute('y', (cy - 49 - 4.5 + drop * (49 + 4.5 - 9.5)).toFixed(1));
      } else {
        dropBillet.setAttribute('visibility', 'hidden');
      }
      return;
    }
    dropBillet.setAttribute('visibility', 'hidden');
    const right = Math.min(tip + 44, dieX);
    billet.setAttribute('visibility', 'visible');
    billet.setAttribute('x', tip.toFixed(1));
    billet.setAttribute('width', Math.max(0, right - tip).toFixed(1));
    strand.setAttribute('width', (clamp((extension - 10) / 40) * (996 - dieX - 18)).toFixed(1));
  }
}

function drawFurnace(ctx, st) {
  const { art, id } = st;
  const { x0, y0 } = st.cfg;
  const capacity = CAPACITY[id];
  const cols = capacity === 6 ? 3 : 5;
  const inX = x0 + 11;
  const inY = y0 + 24;
  const inW = 84;
  const inH = 78;
  const light = stackLight(x0 + 112, y0 - 4);

  art.append(
    rect('eq5', x0 + 2, y0 + 112, 112, 5),
    rect('eq3 edge', x0 + 2, y0 + 14, 112, 98, { rx: 2 }),
    rect('eq5', inX, inY, inW, inH),
  );
  const heat = rect('heat', inX, inY, inW, inH);
  art.append(heat);

  const slots = [];
  const rows = 2;
  const cellW = inW / cols;
  const cellH = inH / rows;
  for (let i = 0; i < capacity; i += 1) {
    const cx = inX + (i % cols) * cellW;
    const cy = inY + Math.floor(i / cols) * cellH;
    const pad = 2.5;
    const w = cellW - 2 * pad;
    const h = cellH - 2 * pad;
    const bars = s('path', { class: 'slot__bars', d: `M${cx + pad + 2} ${cy + pad + h * 0.25}h${w - 4}M${cx + pad + 2} ${cy + pad + h * 0.5}h${w - 4}M${cx + pad + 2} ${cy + pad + h * 0.75}h${w - 4}` });
    const tag = rect('slot__tag', cx + pad, cy + pad + h - 2.5, w, 2.5);
    const group = s('g.slot', rect('slot__frame', cx + pad, cy + pad, w, h), bars, tag);
    art.append(group);
    slots.push({ group, tag });
  }

  // Heat level: a thermometer bar on the casing, scale 0 to 250 °C
  const barX = x0 + 99;
  const barH = 78;
  art.append(rect('eq5', barX, inY, 9, barH));
  const heatBar = rect('heat-bar', barX + 1, inY + barH, 7, 0);
  const setTick = s('path', { class: 'heat-tick', d: '' });
  art.append(heatBar, setTick);

  // Circulation fan and flue on the roof
  art.append(rect('eq4 edge', x0 + 76, y0, 14, 14));
  const fanX = x0 + 44;
  const fanY = y0 + 8;
  art.append(s('circle', { class: 'eq4 edge', cx: fanX, cy: fanY, r: 8 }));
  const fan = s('path', { class: 'fan', d: 'M0 0C-3-1-3-6 0-6.5 1.5-5 1.5-2 0 0ZM0 0C1-3 6-3 6.5 0 5 1.5 2 1.5 0 0ZM0 0C3 1 3 6 0 6.5-1.5 5-1.5 2 0 0ZM0 0C-1 3-6 3-6.5 0-5-1.5-2-1.5 0 0Z', transform: `translate(${fanX} ${fanY})` });
  art.append(fan);

  // Door on the car side. It lifts while the furnace is being loaded.
  art.append(rect('eq5', x0 - 5, y0 + 2, 2, 112));
  const door = rect('eq2 edge door', x0 - 3, y0 + 16, 8, 94, { rx: 1 });
  art.append(door);
  art.append(light.group);

  const recipe = text('art__note', st.cfg.plate[0] + 2, y0 + 92, '');
  const fill = text('art__note', st.cfg.plate[0] + PLATE.w - 2, y0 + 92, '', { 'text-anchor': 'end' });
  const fillTrack = rect('plate__oee-track', st.cfg.plate[0] + 2, y0 + 100, PLATE.w - 4, 6);
  const fillBar = rect('fill-bar', st.cfg.plate[0] + 2, y0 + 100, 0, 6);
  art.append(recipe, fill, fillTrack, fillBar);

  const flag = buildFlag(x0 - 6, y0 - 2);
  ctx.flags.appendChild(flag.group);

  let doorOffset = 0;
  let doorTarget = 0;
  let fanAngle = 0;

  return {
    flag,
    sync(m, inMachine) {
      light.update(m.status);
      const baskets = m.lots.length ? inMachine?.baskets ?? 0 : 0;
      const lotCount = m.lots.length || 1;
      for (let i = 0; i < capacity; i += 1) {
        const on = i < baskets;
        setClass(slots[i].group, on ? 'slot slot--full' : 'slot');
        if (on) {
          const lot = m.lots[Math.floor((i / Math.max(1, baskets)) * lotCount)];
          slots[i].tag.style.fill = lotTone(lot);
        }
      }
      const temperature = typeof m.temperature === 'number' ? m.temperature : m.status === 'RUN' || m.status === 'SETUP' ? 185 : 120;
      setAttr(heat, 'opacity', (clamp((temperature - 40) / 240) * 0.5).toFixed(3));
      const h = clamp(temperature / 250) * barH;
      setAttr(heatBar, 'y', (inY + barH - h).toFixed(1));
      setAttr(heatBar, 'height', h.toFixed(1));
      if (typeof m.setpoint === 'number') {
        const yt = inY + barH - clamp(m.setpoint / 250) * barH;
        setAttr(setTick, 'd', `M${barX - 2} ${yt.toFixed(1)}h13`);
      }
      const recipeName = m.setupClass ?? '';
      setText(recipe, m.lots.length && recipeName
        ? `Rezept ${recipeName}${typeof m.setpoint === 'number' ? ` · ${formatNumber(m.setpoint)} °C` : ''}`
        : recipeName ? `Gerüstet ${recipeName}` : '');
      setText(fill, `Körbe ${baskets}/${capacity}`);
      setAttr(fillBar, 'width', ((baskets / capacity) * (PLATE.w - 4)).toFixed(1));
      doorTarget = m.status === 'SETUP' ? -46 : 0;
      st.running = m.status === 'RUN';
    },
    animate(p, dt, motion) {
      doorOffset += (doorTarget - doorOffset) * Math.min(1, dt * 3);
      door.setAttribute('transform', `translate(0 ${doorOffset.toFixed(1)})`);
      if (st.running && motion) {
        fanAngle = (fanAngle + dt * 400) % 360;
        fan.setAttribute('transform', `translate(${fanX} ${fanY}) rotate(${fanAngle.toFixed(1)})`);
      }
    },
  };
}

function bladePath(cx, cy, r, teeth) {
  const parts = [];
  for (let i = 0; i < teeth; i += 1) {
    const a0 = (i / teeth) * Math.PI * 2;
    const a1 = ((i + 0.55) / teeth) * Math.PI * 2;
    const a2 = ((i + 1) / teeth) * Math.PI * 2;
    parts.push(`${i ? 'L' : 'M'}${(cx + Math.cos(a0) * (r - 3)).toFixed(1)} ${(cy + Math.sin(a0) * (r - 3)).toFixed(1)}`);
    parts.push(`L${(cx + Math.cos(a1) * r).toFixed(1)} ${(cy + Math.sin(a1) * r).toFixed(1)}`);
    parts.push(`L${(cx + Math.cos(a2) * (r - 3)).toFixed(1)} ${(cy + Math.sin(a2) * (r - 3)).toFixed(1)}`);
  }
  return `${parts.join('')}Z`;
}

function drawSaw(ctx, st) {
  const { art } = st;
  const cy = 500;
  const cx = 850;
  const by = 482;
  const light = stackLight(946, 446);
  art.append(
    s('polyline', { class: 'table', points: polyPoints([[1060, cy], [652, cy]]) }),
    s('polyline', { class: 'table__rollers', points: polyPoints([[1060, cy], [652, cy]]) }),
    rect('eq4 edge', 806, cy + 12, 88, 34, { rx: 2 }),
    rect('eq5', 816, cy + 46, 12, 8),
    rect('eq5', 872, cy + 46, 12, 8),
    s('path', { class: 'chute', d: `M826 ${cy + 16}h48l-10 22h-28Z` }),
    rect('eq3 edge', 892, by - 30, 42, 24, { rx: 2 }),
    s('path', { class: 'rib', d: `M902 ${by - 30}v24M912 ${by - 30}v24M922 ${by - 30}v24` }),
    s('path', { class: 'belt', d: `M892 ${by - 18}L${cx} ${by}` }),
  );
  const inBars = s('g');
  const outBars = s('g');
  for (let i = 0; i < 4; i += 1) {
    inBars.appendChild(rect('bar', 880, cy - 7 + i * 4, 0, 2.4));
    outBars.appendChild(rect('bar', 820, cy - 7 + i * 4, 0, 2.4));
  }
  art.append(inBars, outBars);
  const blade = s('path', { class: 'blade', d: bladePath(0, 0, 31, 28), transform: `translate(${cx} ${by})` });
  art.append(
    blade,
    s('circle', { class: 'eq2 edge', cx, cy: by, r: 6 }),
    s('path', { class: 'guard edge', d: `M${cx - 40} ${by + 4}A40 40 0 0 1 ${cx + 40} ${by + 4}L${cx + 33} ${by + 4}A33 33 0 0 0 ${cx - 33} ${by + 4}Z` }),
    light.group,
  );
  const note = text('art__note', 660, cy + 28, '');
  art.append(note);
  art.append(text('art__floor', 660, 448, st.id));
  const flag = buildFlag(662, 456);
  ctx.flags.appendChild(flag.group);
  let angle = 0;
  const inRects = [...inBars.children];
  const outRects = [...outBars.children];

  return {
    flag,
    sync(m) {
      light.update(m.status);
      st.running = m.status === 'RUN';
      st.active = m.lots.length > 0 && m.status !== 'SETUP';
      setText(note, m.setupClass ? `Zuschnitt ${m.setupClass.replace(/^L/, 'L ')} mm` : '');
    },
    animate(p, dt, motion) {
      if (st.running && motion) {
        angle = (angle + dt * 720) % 360;
        blade.setAttribute('transform', `translate(${cx} ${by}) rotate(${angle.toFixed(1)})`);
      }
      const span = 170;
      const left = st.active ? (1 - p) * span : 0;
      const right = st.active ? p * span : 0;
      for (const bar of inRects) bar.setAttribute('width', left.toFixed(1));
      for (const bar of outRects) {
        bar.setAttribute('x', (820 - right).toFixed(1));
        bar.setAttribute('width', right.toFixed(1));
      }
    },
  };
}

function drawPack(ctx, st) {
  const { art } = st;
  const cy = 500;
  const light = stackLight(420, 438);
  const left = 296;
  const right = 404;
  art.append(
    s('polyline', { class: 'table', points: polyPoints([[500, cy], [200, cy]]) }),
    s('polyline', { class: 'table__rollers', points: polyPoints([[500, cy], [200, cy]]) }),
  );
  const bundle = s('g.pack-bundle', { visibility: 'hidden' },
    rect('it-block', left + 4, cy - 19, right - left - 8, 28, { rx: 1 }),
    s('path', { class: 'bundle__lines', d: `M${left + 6} ${cy - 13}H${right - 6}M${left + 6} ${cy - 7}H${right - 6}M${left + 6} ${cy - 1}H${right - 6}M${left + 6} ${cy + 5}H${right - 6}` }));
  const straps = [];
  const nStraps = 6;
  for (let i = 0; i < nStraps; i += 1) {
    const x = left + 14 + i * ((right - left - 28) / (nStraps - 1));
    const strap = rect('strap', x - 1.5, cy - 21, 3, 32, { visibility: 'hidden' });
    straps.push(strap);
    bundle.appendChild(strap);
  }
  art.append(bundle);
  art.append(
    rect('eq3 edge', left - 12, 436, 12, 90),
    rect('eq3 edge', right, 436, 12, 90),
    rect('eq2 edge', left - 16, 430, right - left + 32, 12, { rx: 1 }),
    rect('eq5', left - 18, 526, 20, 6),
    rect('eq5', right - 2, 526, 20, 6),
  );
  const headGroup = s('g');
  const lance = rect('strap-lance', -1, 12, 2, 46);
  headGroup.append(rect('eq1', -7, 0, 14, 12, { rx: 1 }), lance);
  headGroup.setAttribute('transform', `translate(${left + 14} 438)`);
  art.append(headGroup, light.group);
  const note = text('art__note', 212, cy + 28, '');
  art.append(note);
  art.append(text('art__floor', 212, 448, st.id));
  const flag = buildFlag(212, 456);
  ctx.flags.appendChild(flag.group);

  return {
    flag,
    sync(m) {
      light.update(m.status);
      st.active = m.lots.length > 0;
      setAttr(bundle, 'visibility', st.active ? 'visible' : 'hidden');
      setText(note, m.setupClass ? `Bund ${m.setupClass.replace(/^B-/, '')} Stangen` : '');
    },
    animate(p) {
      const span = right - left - 28;
      headGroup.setAttribute('transform', `translate(${(left + 14 + p * span).toFixed(1)} 438)`);
      for (let i = 0; i < nStraps; i += 1) {
        straps[i].setAttribute('visibility', st.active && i / (nStraps - 1) <= p ? 'visible' : 'hidden');
      }
    },
  };
}

const DRAWERS = { cast: drawCast, press: drawPress, age: drawFurnace, saw: drawSaw, pack: drawPack };

// ------------------------------------------------------------------------------------------- buffers
function zoneFrame(cfg, title) {
  const count = text('zone__count', cfg.x + 8, cfg.y + 30);
  const group = s('g.zone',
    rect('zone__floor', cfg.x, cfg.y, cfg.w, cfg.h, { rx: 2 }),
    text('zone__title', cfg.x + 8, cfg.y + 15, title),
    count);
  return { group, count };
}

function buildBilletStore() {
  const cfg = BUFFERS.BILLET_STORE;
  const { group, count } = zoneFrame(cfg, 'Bolzenlager');
  const rows = new Map();
  ALLOY_ORDER.forEach((alloy, index) => {
    const y = cfg.y + 40 + index * 76;
    const label = text('rack__alloy', cfg.x + 8, y + 12, alloy);
    const value = text('rack__count', cfg.x + cfg.w - 8, y + 12, '0', { 'text-anchor': 'end' });
    const dots = s('g');
    group.append(rect('rack', cfg.x + 6, y + 17, cfg.w - 12, 48, { rx: 1 }), label, value, dots);
    rows.set(alloy, { value, dots, y: y + 17, shown: -1 });
  });
  return {
    group,
    update(store) {
      let total = 0;
      for (const [alloy, row] of rows) {
        const n = store?.[alloy] ?? 0;
        total += n;
        setText(row.value, formatNumber(n));
        setClass(row.value, n === 0 ? 'rack__count rack__count--empty' : 'rack__count');
        if (row.shown === n) continue;
        row.shown = n;
        clear(row.dots);
        // Billets end on, in rows of twelve. Beyond 36 the count takes over.
        for (let i = 0; i < Math.min(n, 36); i += 1) {
          const col = i % 12;
          const line = Math.floor(i / 12);
          row.dots.appendChild(s('circle', {
            class: 'billet-end',
            cx: cfg.x + 15 + col * 10.2,
            cy: row.y + 41 - line * 15,
            r: 4.4,
          }));
        }
      }
      setText(count, plural(total, 'Bolzen', 'Bolzen'));
      group.setAttribute('data-tip', `Bolzenlager: ${ALLOY_ORDER.map((a) => `${a}: ${store?.[a] ?? 0}`).join(', ')}`);
    },
  };
}

function buildRackBuffer(uid, cfg, title) {
  const { group, count } = zoneFrame(cfg, title);
  const content = s('g');
  group.appendChild(content);
  let key = '';
  const cellW = 28;
  const cellH = 21;
  return {
    group,
    update(entries) {
      const list = entries ?? [];
      const nextKey = JSON.stringify(list);
      if (nextKey === key) return;
      key = nextKey;
      clear(content);
      const total = list.reduce((sum, e) => sum + (e.baskets ?? 0), 0);
      const lots = cfg.w >= 120 ? ` · ${plural(list.length, 'Los', 'Lose')}` : '';
      setText(count, total ? `${plural(total, 'Korb', 'Körbe')}${lots}` : 'leer');
      const startX = cfg.x + 8 + 11;
      const startY = cfg.y + 48;
      const maxRows = Math.floor((cfg.h - 44) / cellH);
      const capacity = maxRows * cfg.cols;
      let i = 0;
      for (const entry of list) {
        for (let b = 0; b < (entry.baskets ?? 0) && i < capacity; b += 1, i += 1) {
          const x = startX + (i % cfg.cols) * cellW;
          const y = startY + Math.floor(i / cfg.cols) * cellH;
          content.appendChild(s('use', { href: `#${uid}-basket`, x, y }));
          content.appendChild(rect('item__tag', x - 11, y + 8, 22, 2.5, { style: `fill:${lotTone(entry.lot_id)}` }));
        }
      }
      if (total > capacity) content.appendChild(text('zone__more', cfg.x + cfg.w - 8, cfg.y + cfg.h - 6, `+${total - capacity}`, { 'text-anchor': 'end' }));
      group.setAttribute('data-tip', list.length ? `${title}: ${list.map((e) => `${formatLotId(e.lot_id)} (${e.baskets})`).join(', ')}` : `${title}: leer`);
    },
  };
}

function buildPieceBuffer(uid, cfg, title) {
  const { group, count } = zoneFrame(cfg, title);
  const content = s('g');
  group.appendChild(content);
  let key = '';
  return {
    group,
    update(entries) {
      const list = entries ?? [];
      const nextKey = JSON.stringify(list);
      if (nextKey === key) return;
      key = nextKey;
      clear(content);
      const total = list.reduce((sum, e) => sum + (e.pieces ?? 0), 0);
      setText(count, total ? `${formatNumber(total)} Stück · ${plural(list.length, 'Los', 'Lose')}` : 'leer');
      list.slice(0, 6).forEach((entry, i) => {
        const y = cfg.y + 52 + i * 24;
        content.append(
          s('use', { href: `#${uid}-bundle`, x: cfg.x + 28, y }),
          rect('item__tag', cfg.x + 11, y + 8, 34, 2.5, { style: `fill:${lotTone(entry.lot_id)}` }),
          text('zone__line', cfg.x + 54, y + 4, formatLotId(entry.lot_id)),
        );
      });
      if (list.length > 6) content.appendChild(text('zone__more', cfg.x + cfg.w - 8, cfg.y + cfg.h - 6, `+${list.length - 6} Lose`, { 'text-anchor': 'end' }));
      group.setAttribute('data-tip', list.length ? `${title}: ${list.map((e) => `${formatLotId(e.lot_id)} (${e.pieces} Stück)`).join(', ')}` : `${title}: leer`);
    },
  };
}

function buildShip(uid) {
  const cfg = BUFFERS.SHIP;
  const { group } = zoneFrame(cfg, 'Versand');
  const big = text('ship__big', cfg.x + 8, cfg.y + 38);
  const sub = text('zone__count', cfg.x + 8, cfg.y + 54);
  const content = s('g');
  group.append(big, sub, content);
  // Roller door in the outer wall
  group.append(
    rect('door-slat', 8, cfg.y + 62, 8, 96),
    s('path', { class: 'door-lines', d: `M8 ${cfg.y + 74}h8M8 ${cfg.y + 86}h8M8 ${cfg.y + 98}h8M8 ${cfg.y + 110}h8M8 ${cfg.y + 122}h8M8 ${cfg.y + 134}h8M8 ${cfg.y + 146}h8` }),
  );
  let shown = -1;
  return {
    group,
    update(ship) {
      const packages = ship?.packages ?? 0;
      setText(big, plural(packages, 'Paket', 'Pakete'));
      setText(sub, ship ? `${formatNumber(ship.kg)} kg · ${plural(ship.lots_total, 'Los', 'Lose')}` : '');
      group.setAttribute('data-tip', ship?.last_lots?.length ? `Zuletzt versandt: ${ship.last_lots.slice(-6).map(formatLotId).join(', ')}` : 'Versand');
      const drawn = Math.min(packages, 28);
      if (drawn === shown) return;
      shown = drawn;
      clear(content);
      // Stacks grow from the wall outward, the way a loading bay fills.
      for (let i = 0; i < drawn; i += 1) {
        const col = i % 5;
        const row = Math.floor(i / 5);
        content.appendChild(s('use', { href: `#${uid}-package`, x: cfg.x + 24 + col * 31, y: cfg.y + cfg.h - 16 - row * 19 }));
      }
    },
  };
}

function buildLegend() {
  const { x, y, w, h } = LEGEND;
  const group = s('g.legend', rect('legend__bg', x, y, w, h, { rx: 2 }), text('zone__title', x + 10, y + 16, 'Kennzeichnung'));
  const states = [['RUN', 'Produktion'], ['SETUP', 'Rüsten'], ['FAULT', 'Störung'], ['IDLE', 'Bereit']];
  states.forEach(([key, label], i) => {
    const shape = SHAPES[key];
    const cy = y + 34 + i * 18;
    group.append(
      s('path', { class: `legend__shape legend__shape--${shape.tone}${shape.hollow ? ' legend__shape--hollow' : ''}`, d: shape.d, transform: `translate(${x + 10} ${cy - 9})` }),
      text('legend__text', x + 28, cy + 1, label));
  });
  group.append(
    rect('legend__hatch', x + 112, y + 28, 22, 12),
    text('legend__text', x + 140, y + 38, 'wartet'),
    s('path', { class: 'legend__blocked', d: `M${x + 112} ${y + 52}h22` }),
    text('legend__text', x + 140, y + 56, 'blockiert'),
    rect('legend__tag', x + 112, y + 68, 22, 3),
    text('legend__text', x + 140, y + 74, 'Los-Markierung'),
  );
  return { group };
}

function legendItems(group, uid) {
  const { x, y } = LEGEND;
  const items = [['billet', 'Bolzen'], ['basket', 'Korb'], ['bundle', 'Stangenbund'], ['package', 'Paket']];
  group.appendChild(text('zone__title', x + 10, y + 124, 'Material'));
  items.forEach(([glyph, label], i) => {
    const col = i % 2;
    const row = Math.floor(i / 2);
    const px = x + 28 + col * 118;
    const py = y + 148 + row * 30;
    group.append(s('use', { href: `#${uid}-${glyph}`, x: px, y: py }), text('legend__text', px + 24, py + 4, label));
  });
}

// ------------------------------------------------------------------------------------------- normalisation
function normaliseMachine(id, raw) {
  const op = raw.op ?? null;
  const lots = op?.lot_ids ?? raw.lot_ids ?? [];
  const opIds = op?.op_ids ?? [];
  return {
    id,
    name: raw.name,
    status: raw.status ?? 'IDLE',
    progress: raw.progress ?? 0,
    lots,
    opKey: opIds.length ? opIds.join('|') : lots.join('|'),
    opKind: op?.kind ?? KIND_OF[id],
    opLabel: !lots.length && opIds.length ? opIds[0].replace(/:cast$/, '') : null,
    product: op?.product_id ?? null,
    phase: op?.phase ?? null,
    temperature: raw.temperature,
    setpoint: raw.setpoint,
    power: raw.power_kw,
    oee: raw.oee?.oee ?? null,
    alarm: raw.alarm ?? null,
    estRepair: raw.est_repair_min,
    waiting: raw.waiting_on,
    since: raw.status_since,
    setupClass: raw.setup_class,
    starved: false,
    hasOp: opIds.length > 0 || lots.length > 0,
  };
}

function normaliseMachines(machines) {
  const out = new Map();
  if (Array.isArray(machines)) {
    for (const raw of machines) out.set(raw.id, normaliseMachine(raw.id, raw));
  } else if (machines) {
    for (const [id, raw] of Object.entries(machines)) out.set(id, normaliseMachine(id, raw));
  }
  for (const m of out.values()) {
    if (m.status !== 'IDLE') continue;
    let reason = m.waiting;
    if (reason === undefined) {
      // A twin frame has no waiting_on. Idle with every direct feeder down reads as blocked by the fault.
      const feeders = UPSTREAM[m.id] ?? [];
      const down = feeders.length && feeders.every((f) => out.get(f)?.status === 'FAULT');
      reason = down ? 'Störung' : null;
      m.waiting = reason;
    }
    m.starved = reason === 'Material' || reason === 'Vorgänger' || reason === 'Störung';
  }
  return out;
}

class ProgressSmoother {
  constructor() {
    this.key = null;
    this.p = 0;
    this.rate = 0;
    this.tSeen = 0;
    this.shown = 0;
  }

  observe(key, p, t) {
    if (key !== this.key) {
      this.key = key;
      this.p = p;
      this.rate = 0;
      this.tSeen = t;
      this.shown = p;
      return;
    }
    const dt = t - this.tSeen;
    if (dt > 0.02) {
      const measured = Math.max(0, (p - this.p) / dt);
      this.rate = this.rate ? this.rate * 0.6 + measured * 0.4 : measured;
      this.p = p;
      this.tSeen = t;
    } else if (dt < 0) {
      // The clock moved back, which only a twin replay does.
      this.p = p;
      this.tSeen = t;
      this.rate = 0;
      this.shown = p;
    }
  }

  value(t, running, dtWall) {
    const target = running ? Math.min(1, this.p + this.rate * Math.max(0, t - this.tSeen)) : this.p;
    this.shown += (target - this.shown) * Math.min(1, dtWall * 8);
    return this.shown;
  }
}

// ------------------------------------------------------------------------------------------- the component
export function createHall(host, { provider, onSelect = null, showClock = false, ariaLabel = 'Werkhalle, Grundriss mit Maschinen und Materialfluss' } = {}) {
  instances += 1;
  const uid = `hall${instances}`;
  const interactive = Boolean(onSelect);
  let currentProvider = provider;
  let raf = 0;
  let destroyed = false;
  let lastMachines = null;
  let lastMaterial = null;
  let lastWall = performance.now();
  let selected = null;
  let firstData = true;

  const svg = s('svg.hall__svg', { viewBox: `0 0 ${VIEW.w} ${VIEW.h}`, role: 'group', 'aria-label': ariaLabel, preserveAspectRatio: 'xMidYMid meet' });
  const defs = s('defs',
    s('pattern', { id: `${uid}-grid`, width: 40, height: 40, patternUnits: 'userSpaceOnUse' },
      s('path', { class: 'floor__grid', d: 'M40 0H0V40' })),
    s('pattern', { id: `${uid}-hatch`, width: 7, height: 7, patternUnits: 'userSpaceOnUse', patternTransform: 'rotate(45)' },
      s('line', { class: 'hatch__line', x1: 0, y1: 0, x2: 0, y2: 7 })),
    s('linearGradient', { id: `${uid}-melt`, x1: 0, y1: 0, x2: 0, y2: 1 },
      s('stop', { offset: 0, 'stop-color': 'var(--melt-core)' }),
      s('stop', { offset: 0.35, 'stop-color': 'var(--melt)' }),
      s('stop', { offset: 1, 'stop-color': 'var(--melt-dim)' })),
    s('radialGradient', { id: `${uid}-glow` },
      s('stop', { offset: 0, 'stop-color': 'var(--melt)', 'stop-opacity': 0.55 }),
      s('stop', { offset: 1, 'stop-color': 'var(--melt)', 'stop-opacity': 0 })),
    s('linearGradient', { id: `${uid}-strand`, gradientUnits: 'userSpaceOnUse', x1: 656, y1: 0, x2: 996, y2: 0 },
      s('stop', { offset: 0, 'stop-color': 'var(--hot)' }),
      s('stop', { offset: 0.45, 'stop-color': 'var(--metal-2)' }),
      s('stop', { offset: 1, 'stop-color': 'var(--metal)' })),
    ...defineItems(uid));
  svg.appendChild(defs);

  const layers = {
    floor: s('g'),
    runs: s('g'),
    buffers: s('g'),
    machines: s('g'),
    items: s('g'),
    flags: s('g'),
  };

  // Floor and walls
  layers.floor.append(
    rect('floor', 14, 14, VIEW.w - 28, VIEW.h - 28),
    rect('floor--grid', 14, 14, VIEW.w - 28, VIEW.h - 28, { fill: `url(#${uid}-grid)` }),
    s('path', { class: 'aisle', d: `M14 ${BAYS.aisle.y}H${VIEW.w - 14}M14 ${BAYS.aisle.y + BAYS.aisle.h}H${VIEW.w - 14}` }),
    text('aisle__label', 660, BAYS.aisle.y + 15, 'Fahrweg'),
    s('path', { class: 'wall', d: `M8 8H${VIEW.w - 8}V${VIEW.h - 8}H8Z` }),
  );
  for (const x of COLUMNS) {
    layers.floor.appendChild(rect('column', x - 5, BAYS.aisle.y + 6, 10, 10));
    layers.floor.appendChild(rect('column', x - 5, 8, 10, 8));
    layers.floor.appendChild(rect('column', x - 5, VIEW.h - 16, 10, 8));
  }
  for (const area of AREAS) layers.floor.appendChild(text('area', area.x, area.y, area.label));

  // Conveyors and the transfer car rail
  const conveyors = new Map();
  for (const [key, points] of Object.entries(RUNS)) {
    const [from, to] = key.split('>');
    const line = polyPoints(points);
    // A chevron near the end says which way the run goes, also while nothing is on it.
    const [ax, ay] = points[points.length - 2];
    const [bx, by] = points[points.length - 1];
    const angle = (Math.atan2(by - ay, bx - ax) * 180) / Math.PI;
    const inset = Math.min(10, Math.hypot(bx - ax, by - ay) / 2);
    const cx = bx - Math.cos((angle * Math.PI) / 180) * inset;
    const cy = by - Math.sin((angle * Math.PI) / 180) * inset;
    const group = s('g.conv',
      s('polyline', { class: 'conv__bed', points: line }),
      s('polyline', { class: 'conv__rollers', points: line }),
      s('polyline', { class: 'conv__flow', points: line }),
      s('path', { class: 'conv__arrow', d: 'M-3-4 2 0-3 4', transform: `translate(${cx.toFixed(1)} ${cy.toFixed(1)}) rotate(${angle.toFixed(0)})` }));
    layers.runs.appendChild(group);
    conveyors.set(key, { group, from, to });
  }

  const flagsLayer = layers.flags;
  const ctx = { uid, flags: flagsLayer, interactive };

  // Stations
  const stations = new Map();
  for (const id of Object.keys(STATIONS)) {
    const st = makeStation(ctx, id);
    st.draw = DRAWERS[KIND_OF[id]](ctx, st);
    st.smoother = new ProgressSmoother();
    st.opKey = null;
    layers.machines.appendChild(st.group);
    stations.set(id, st);
    if (interactive) {
      st.group.addEventListener('click', () => onSelect(id));
      st.group.addEventListener('keydown', (event) => {
        if (event.key === 'Enter' || event.key === ' ') {
          event.preventDefault();
          onSelect(id);
        }
      });
    }
  }

  // Buffers
  const buffers = {
    store: buildBilletStore(),
    age: buildRackBuffer(uid, BUFFERS.AGE_IN, 'Ofen-Vorzone'),
    saw: buildRackBuffer(uid, BUFFERS.SAW_IN, 'Säge-Puffer'),
    pack: buildPieceBuffer(uid, BUFFERS.PACK_IN, 'Pack-Puffer'),
    ship: buildShip(uid),
  };
  Object.values(buffers).forEach((b) => layers.buffers.appendChild(b.group));
  const legend = buildLegend();
  legendItems(legend.group, uid);
  layers.buffers.appendChild(legend.group);

  svg.append(layers.floor, layers.runs, layers.buffers, layers.machines, layers.items, layers.flags);

  const banner = document.createElement('div');
  banner.className = 'hall__banner';
  banner.hidden = true;
  const clockNode = document.createElement('div');
  clockNode.className = 'hall__clock';
  clockNode.hidden = !showClock;
  const wrap = document.createElement('div');
  wrap.className = 'hall';
  wrap.append(svg, banner, clockNode);
  host.appendChild(wrap);

  const items = new Map();
  const localMoves = [];
  let localId = 0;

  function syncItems(material, t) {
    const seen = new Set();
    const active = new Set();
    const transports = material?.transports ?? [];
    for (const transport of transports) {
      seen.add(transport.id);
      if (!items.has(transport.id)) addItem(transport.id, transport);
      active.add(`${transport.from}>${transport.to}`);
    }
    for (let i = localMoves.length - 1; i >= 0; i -= 1) {
      const move = localMoves[i];
      if (t >= move.t1 || t < move.t0 - 1) {
        localMoves.splice(i, 1);
      } else {
        seen.add(move.id);
        active.add(`${move.from}>${move.to}`);
      }
    }
    for (const [id, entry] of items) {
      if (!seen.has(id)) {
        entry.g.remove();
        items.delete(id);
      }
    }
    return active;
  }

  function addItem(id, transport) {
    const run = runFor(transport.from, transport.to);
    if (!run) return;
    const g = makeItem(uid, transport);
    layers.items.appendChild(g);
    items.set(id, { g, run, t0: transport.t0, t1: transport.t1 });
  }

  function spawnPickup(machineId, kind, m, inMachine, t) {
    const rule = PICKUP[kind];
    if (!rule) return;
    const count = kind === 'press' ? inMachine?.billets_left ?? 1 : kind === 'age' ? inMachine?.baskets ?? 1 : 1;
    const move = {
      id: `local-${(localId += 1)}`,
      kind: rule.kind,
      lot_id: m.lots[0] ?? null,
      count,
      from: rule.from,
      to: machineId,
      t0: t,
      t1: t + 4,
    };
    localMoves.push(move);
    addItem(move.id, move);
  }

  function slowUpdate(state, t) {
    const machines = normaliseMachines(state.machines);
    const material = state.material ?? {};
    const inMachine = material.in_machine ?? {};
    for (const [id, st] of stations) {
      const m = machines.get(id);
      if (!m) continue;
      const mine = inMachine[id];
      const tone = statusTone(m.status);
      setClass(st.group, `st st--${tone}${m.starved ? ' st--starved' : ''}${id === selected ? ' is-selected' : ''}`);
      setAttr(st.hatch, 'visibility', m.starved ? 'visible' : 'hidden');
      st.plate.update(m, t);
      st.draw.sync(m, mine);
      st.draw.flag?.update(m);
      st.smoother.observe(m.opKey, m.progress, t);
      st.status = m.status;
      if (m.opKey !== st.opKey) {
        if (!firstData && m.hasOp && m.lots.length) spawnPickup(id, m.opKind, m, mine, t);
        st.opKey = m.opKey;
      }
      st.group.setAttribute('aria-label', `${machineName(id)}, ${statusLabel(m.status)}${m.alarm?.active ? `, Alarm ${m.alarm.code}` : ''}${m.lots.length ? `, Los ${m.lots.map(formatLotId).join(', ')}` : ''}`);
      if (interactive) st.group.setAttribute('data-tip', `${machineName(id)}: ${(m.status === 'IDLE' && waitingTip(m.waiting)) || 'Details öffnen'}`);
    }
    firstData = false;

    const b = material.buffers ?? {};
    buffers.store.update(b.BILLET_STORE);
    buffers.age.update(b.AGE_IN);
    buffers.saw.update(b.SAW_IN);
    buffers.pack.update(b.PACK_IN);
    buffers.ship.update(b.SHIP);

    const active = syncItems(material, t);
    for (const [key, conveyor] of conveyors) {
      const source = machines.get(conveyor.from);
      const target = machines.get(conveyor.to);
      let cls = 'conv';
      if (source?.status === 'FAULT' || target?.status === 'FAULT') cls += ' conv--blocked';
      else if (target?.starved) cls += ' conv--starved';
      if (active.has(key)) cls += ' conv--active';
      setClass(conveyor.group, cls);
    }
  }

  function fastUpdate(t, dt) {
    const motion = !reducedMotion?.matches;
    for (const entry of items.values()) {
      const span = entry.t1 - entry.t0;
      const fraction = span > 0 ? (t - entry.t0) / span : 1;
      const [x, y] = pointAt(entry.run, fraction);
      entry.g.setAttribute('transform', `translate(${x.toFixed(1)} ${y.toFixed(1)}) scale(1.15)`);
    }
    for (const st of stations.values()) {
      const p = st.smoother.value(t, st.status === 'RUN', dt);
      st.draw.animate(p, dt, motion);
      st.plate.setProgress(p);
    }
    if (showClock) {
      const label = formatSimTime(t);
      if (clockNode.textContent !== label) clockNode.textContent = label;
    }
  }

  function frame(now) {
    if (destroyed) return;
    raf = requestAnimationFrame(frame);
    const dt = Math.min(0.1, (now - lastWall) / 1000);
    lastWall = now;
    const state = currentProvider?.();
    if (!state) return;
    const t = state.t ?? 0;
    if (state.machines !== lastMachines || state.material !== lastMaterial) {
      lastMachines = state.machines;
      lastMaterial = state.material;
      slowUpdate(state, t);
    }
    fastUpdate(t, dt);
  }

  raf = requestAnimationFrame(frame);

  return {
    /** Replaces the data source, e.g. to replay a twin variant. */
    setProvider(next) {
      currentProvider = next;
      lastMachines = null;
      lastMaterial = null;
      firstData = true;
      localMoves.length = 0;
    },
    /** Outlines one machine, used while its drawer is open. */
    setSelected(id) {
      selected = id;
      for (const [machineId, st] of stations) st.group.classList.toggle('is-selected', machineId === id);
    },
    /** A strip across the top of the hall, or null. `level` is warn or fault. */
    setBanner(message, level = 'warn') {
      banner.hidden = !message;
      if (message) {
        banner.textContent = message;
        banner.className = `hall__banner hall__banner--${level}`;
      }
    },
    focusMachine(id) {
      stations.get(id)?.group.focus();
    },
    get element() { return wrap; },
    destroy() {
      destroyed = true;
      cancelAnimationFrame(raf);
      wrap.remove();
    },
  };
}

/** The usual provider: latest /ws state plus the smoothly extrapolated clock. */
export function liveHallProvider(store) {
  return () => {
    const state = store.state;
    if (!state || !store.clock.synced) return null;
    return { t: store.clock.now(), machines: state.machines, material: state.material };
  };
}
