// German formatters. Every function takes null or undefined and answers with DASH, never "NaN" or "null".
const LOCALE = 'de-DE';
export const DASH = '–';
const NBSP = ' ';
const MINUS = '−';

const numberFormats = new Map();
function numberFormat(digits) {
  let format = numberFormats.get(digits);
  if (!format) {
    format = new Intl.NumberFormat(LOCALE, { minimumFractionDigits: digits, maximumFractionDigits: digits });
    numberFormats.set(digits, format);
  }
  return format;
}

function missing(value) {
  return value === null || value === undefined || Number.isNaN(value);
}

/** 1234.5 -> "1.234,5" with `digits` decimals. */
export function formatNumber(value, digits = 0) {
  if (missing(value)) return DASH;
  return numberFormat(digits).format(value).replace('-', MINUS);
}

/** 0.712 -> "71 %". The input is a fraction. */
export function formatPercent(value, digits = 0) {
  if (missing(value)) return DASH;
  return `${formatNumber(value * 100, digits)}${NBSP}%`;
}

/** Sim minutes -> day number, with t=0 as Tag 1, 06:00. */
export function simDay(t) {
  return Math.floor((t + 360) / 1440) + 1;
}

/** Sim minutes -> "14:30". */
export function formatSimClock(t) {
  if (missing(t)) return DASH;
  const minuteOfDay = ((Math.floor(t) + 360) % 1440 + 1440) % 1440;
  const hours = Math.floor(minuteOfDay / 60);
  const minutes = minuteOfDay % 60;
  return `${String(hours).padStart(2, '0')}:${String(minutes).padStart(2, '0')}`;
}

/** Sim minutes -> "Tag 2 · 14:30". */
export function formatSimTime(t) {
  if (missing(t)) return DASH;
  return `Tag${NBSP}${simDay(Math.floor(t))} · ${formatSimClock(t)}`;
}

/**
 * Minutes as a span: "35 min", "4 h 20 min", "24 h 35 min", "2 T 3 h". Up to 48 h the minutes stay, above that the next non-zero unit
 * does ("2 T 35 min"), so a plan of 1475 min never reads as "1 T". `signed` adds + or −, so deltas read correctly.
 */
export function formatDuration(minutes, { signed = false } = {}) {
  if (missing(minutes)) return DASH;
  const rounded = Math.round(Math.abs(minutes));
  const sign = signed ? (minutes > 0 ? '+' : minutes < 0 && rounded > 0 ? MINUS : '') : minutes < 0 && rounded > 0 ? MINUS : '';
  if (rounded < 60) return `${sign}${rounded}${NBSP}min`;
  const hours = Math.floor(rounded / 60);
  const rest = rounded % 60;
  if (hours < 48) return rest ? `${sign}${hours}${NBSP}h ${rest}${NBSP}min` : `${sign}${hours}${NBSP}h`;
  const days = Math.floor(hours / 24);
  const restHours = hours % 24;
  if (restHours) return `${sign}${days}${NBSP}T ${restHours}${NBSP}h`;
  return rest ? `${sign}${days}${NBSP}T ${rest}${NBSP}min` : `${sign}${days}${NBSP}T`;
}

// An estimate does not deserve minutes once it is hours away, and at 480x an exact figure would flicker.
function roundEstimate(minutes) {
  if (minutes >= 600) return Math.round(minutes / 60) * 60;
  if (minutes >= 60) return Math.round(minutes / 5) * 5;
  return Math.ceil(minutes);
}

export const REPAIR_OVERDUE = 'Reparatur überfällig';

/**
 * What is left of a repair estimate: "noch ca. 15 h", or "Reparatur überfällig" once the estimate has run out. `estimate` is the total, in minutes,
 * so the time since `since` comes off it. Null without an estimate. `long` writes "Reparatur noch ca. 15 h", for where the word is not implied.
 */
export function formatRepairLeft(estimate, since, now, { long = false } = {}) {
  if (missing(estimate) || estimate <= 0) return null;
  const elapsed = typeof since === 'number' && typeof now === 'number' ? Math.max(0, now - since) : 0;
  const left = Math.max(estimate - elapsed, 0);
  if (left < 0.5) return REPAIR_OVERDUE;
  return `${long ? 'Reparatur noch' : 'noch'} ca.${NBSP}${formatDuration(roundEstimate(left))}`;
}

/** Seconds, for solver times: "8,8 s". */
export function formatSeconds(seconds, digits = 1) {
  if (missing(seconds)) return DASH;
  return `${formatNumber(seconds, digits)}${NBSP}s`;
}

/** Signed count, for deltas: "+2", "−1", "0". */
export function formatSigned(value, digits = 0) {
  if (missing(value)) return DASH;
  const text = numberFormat(digits).format(Math.abs(value));
  if (value > 0) return `+${text}`;
  if (value < 0 && Number(text.replace(',', '.')) !== 0) return `${MINUS}${text}`;
  return text;
}

export function formatKw(value) {
  return missing(value) ? DASH : `${formatNumber(value, 0)}${NBSP}kW`;
}

export function formatKwh(value) {
  return missing(value) ? DASH : `${formatNumber(value, 0)}${NBSP}kWh`;
}

export function formatKg(value) {
  return missing(value) ? DASH : `${formatNumber(value, 0)}${NBSP}kg`;
}

export function formatTemp(value) {
  return missing(value) ? DASH : `${formatNumber(value, 0)}${NBSP}°C`;
}

/** Unix seconds -> "14:30:12", or "14:30:12,345" with `ms`. Wall clock, for the chain and the OPC UA feed. */
export function formatWallTime(ts, { ms = false } = {}) {
  if (missing(ts)) return DASH;
  const date = new Date(ts * 1000);
  const base = date.toLocaleTimeString(LOCALE, { hour12: false });
  return ms ? `${base},${String(date.getMilliseconds()).padStart(3, '0')}` : base;
}

export const STATUS = {
  RUN: { label: 'Produktion', tag: 'PRODUKTION', tone: 'run' },
  SETUP: { label: 'Rüsten', tag: 'RÜSTEN', tone: 'setup' },
  FAULT: { label: 'Störung', tag: 'STÖRUNG', tone: 'fault' },
  IDLE: { label: 'Bereit', tag: 'BEREIT', tone: 'idle' },
};

export function statusLabel(status) {
  return STATUS[status]?.label ?? status ?? DASH;
}

export function statusTag(status) {
  return STATUS[status]?.tag ?? status ?? DASH;
}

export function statusTone(status) {
  return STATUS[status]?.tone ?? 'idle';
}

const MACHINE_NAMES = {
  GUSS: 'Gießanlage',
  PR1: 'Presse 1 (25 MN)',
  PR2: 'Presse 2 (35 MN)',
  OF1: 'Auslagerungsofen 1',
  OF2: 'Auslagerungsofen 2',
  SAE: 'Fertigsäge',
  VP: 'Verpackung',
};

const SOLVER_LABELS = { cpsat: 'CP-SAT', anneal: 'Simulated Annealing', keep: 'Aktueller Plan', edd: 'EDD' };
const SOURCE_LABELS = { startup: 'Startplan', manual: 'Manuell', proposal: 'Vorschlag', twin: 'Zwilling', rolling: 'Rollierend' };
const LOG_SOURCES = { opcua: 'OPC UA', mes: 'MES', plan: 'Plan', twin: 'Zwilling' };
const KIND_LABELS = { cast: 'Gießen', press: 'Pressen', age: 'Auslagern', saw: 'Sägen', pack: 'Verpacken' };

let catalog = null;

/** Called by the store once /api/catalog arrived. Names then come from the backend, not from the table above. */
export function setCatalog(next) {
  catalog = next;
  for (const machine of next?.machines ?? []) MACHINE_NAMES[machine.id] = machine.name;
}

export function machineName(id) {
  return MACHINE_NAMES[id] ?? id ?? DASH;
}

export function productName(id) {
  return catalog?.products?.find((p) => p.id === id)?.name ?? id ?? DASH;
}

/** "Presse 2 (35 MN)" -> "Presse 2", for running text. */
export function machineShortName(id) {
  return machineName(id).replace(/\s*\(.*\)$/, '');
}

export function solverLabel(solver) {
  return SOLVER_LABELS[solver] ?? solver ?? DASH;
}

export function planSourceLabel(source) {
  return SOURCE_LABELS[source] ?? source ?? DASH;
}

export function logSourceLabel(source) {
  return LOG_SOURCES[source] ?? source ?? DASH;
}

export function opKindLabel(kind) {
  return KIND_LABELS[kind] ?? kind ?? DASH;
}

/** "L-00041:press" -> "L-00041 · Pressen". An id without a known step comes back as it is. */
export function formatOpId(id) {
  if (id === null || id === undefined || id === '') return DASH;
  const match = /^(.+):(cast|press|age|saw|pack)$/.exec(id);
  return match ? `${formatLotId(match[1])} · ${opKindLabel(match[2])}` : formatLotId(id);
}

/** Several ops of one running step: "L-00041, L-00042 · Auslagern". */
export function formatOps(ids, kind) {
  const list = ids ?? [];
  if (!list.length) return DASH;
  const owners = list.map((id) => formatLotId(String(id).replace(/:(cast|press|age|saw|pack)$/, '')));
  return `${owners.join(', ')} · ${opKindLabel(kind ?? /:([a-z]+)$/.exec(list[0])?.[1])}`;
}

/** Setup class: "6082|M-3001" -> "Legierung 6082 · Matrize M-3001". The raw form stays in the OPC UA view. */
export function formatSetupClass(value) {
  if (value === null || value === undefined || value === '') return DASH;
  return String(value).split('|').map((part) => {
    if (/^\d{4}/.test(part)) return `Legierung ${part}`;
    if (/^M-/.test(part)) return `Matrize ${part}`;
    if (/^L\d+$/.test(part)) return `Länge ${formatNumber(Number(part.slice(1)))} mm`;
    if (/^B-\d+$/.test(part)) return `${part.slice(2)} Stangen je Paket`;
    if (/^T\d+$/.test(part)) return `Zustand ${part}`;
    return part;
  }).join(' · ');
}

/** A scenario order only exists in the twin, so its id means nothing: "Z-A-0001" -> "Eilauftrag (neu)". */
export function formatOrderId(id) {
  if (id === null || id === undefined || id === '') return DASH;
  if (!String(id).startsWith('Z-')) return String(id);
  const number = Number(/(\d+)$/.exec(id)?.[1] ?? 1);
  return number > 1 ? `Eilauftrag ${number} (neu)` : 'Eilauftrag (neu)';
}

/** A scenario's lots, melts and batches only exist in the twin too: "Z-L-00001" -> "Eil-Los 1", "Z-M-6082-00014" -> "M-6082 (neu)". Real ids pass through. */
export function formatLotId(id) {
  if (id === null || id === undefined || id === '') return DASH;
  const text = String(id);
  if (!text.startsWith('Z-')) return text;
  const lot = /^Z-L-0*(\d+)$/.exec(text);
  if (lot) return `Eil-Los ${lot[1]}`;
  const melt = /^Z-M-([^-]+)-\d+$/.exec(text);
  return `${melt ? `M-${melt[1]}` : text.slice(2)} (neu)`;
}

const WAITING = {
  'Kein Auftrag': { line: 'kein Auftrag', tip: 'Für diese Maschine liegt keine offene Arbeit vor.' },
  'Kein Plan': { line: 'wartet auf Plan', tip: 'Es liegt Arbeit an, aber der aktive Plan sieht hier nichts vor: der Startplan wird berechnet oder ein Vorschlag wartet auf die Entscheidung.' },
  'Entscheidung offen': { line: 'wartet: Entscheidung', tip: 'Es liegt Arbeit an, aber der aktive Plan hält sie an einer anderen Maschine fest, bis über den offenen Vorschlag entschieden ist.' },
  Material: { line: 'wartet: Material', tip: 'Das Material für den nächsten Schritt fehlt.' },
  'Vorgänger': { line: 'wartet: Vorgänger', tip: 'Der vorherige Arbeitsschritt ist noch nicht fertig.' },
  'Störung': { line: 'wartet: Störung', tip: 'Eine gestörte Maschine hält diesen Schritt auf.' },
};

/** The one-line reason an idle machine shows. */
export function formatWaiting(value) {
  if (value === null || value === undefined || value === '') return DASH;
  return WAITING[value]?.line ?? `wartet: ${value}`;
}

export function waitingTip(value) {
  return WAITING[value]?.tip ?? '';
}

/** "Stange" and friends do not matter here, but "1 Paket" against "2 Pakete" does. */
export function plural(count, one, many) {
  return `${formatNumber(count)}${NBSP}${count === 1 ? one : many}`;
}

export const fmt = {
  DASH,
  number: formatNumber,
  percent: formatPercent,
  simTime: formatSimTime,
  formatSimTime,
  simClock: formatSimClock,
  simDay,
  duration: formatDuration,
  repairLeft: formatRepairLeft,
  REPAIR_OVERDUE,
  seconds: formatSeconds,
  signed: formatSigned,
  kw: formatKw,
  kwh: formatKwh,
  kg: formatKg,
  temp: formatTemp,
  wallTime: formatWallTime,
  statusLabel,
  statusTag,
  statusTone,
  machineName,
  machineShort: machineShortName,
  opId: formatOpId,
  ops: formatOps,
  setupClass: formatSetupClass,
  orderId: formatOrderId,
  lotId: formatLotId,
  waiting: formatWaiting,
  waitingTip,
  productName,
  solverLabel,
  planSourceLabel,
  logSourceLabel,
  opKindLabel,
  plural,
};
