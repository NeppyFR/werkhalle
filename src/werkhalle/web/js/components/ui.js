// Shared UI pieces. State is always shape plus text plus colour, never colour alone (ISA-101).
import { h, s, setText, clear } from '../core/dom.js';
import { statusLabel, statusTone } from '../core/format.js';

/** Shape path per state on a 12x12 grid, shared with the hall. */
export const SHAPES = {
  RUN: { d: 'M3 1.5 10.5 6 3 10.5Z', tone: 'run' },
  SETUP: { d: 'M6 .8 11.2 6 6 11.2 .8 6Z', tone: 'setup' },
  FAULT: { d: 'M6 .6 11.6 11H.4Z', tone: 'fault', mark: true },
  IDLE: { d: 'M2 2h8v8H2Z', tone: 'idle', hollow: true },
  info: { d: 'M6 1a5 5 0 1 0 0 10A5 5 0 0 0 6 1Z', tone: 'info', hollow: true },
  ok: { d: 'M2 6.5 5 9.5 10.5 3', tone: 'run', line: true },
  warn: { d: 'M6 .8 11.2 6 6 11.2 .8 6Z', tone: 'setup' },
  link: { d: 'M6 1a5 5 0 1 0 0 10A5 5 0 0 0 6 1Z', tone: 'run' },
};

/** Small SVG icon for a state. `key` is a machine status or a key of SHAPES. */
export function statusIcon(key, { size = 12 } = {}) {
  const svg = s('svg.status-icon', { viewBox: '0 0 12 12', width: size, height: size, 'aria-hidden': 'true' });
  paintIcon(svg, key);
  return svg;
}

export function paintIcon(svg, key) {
  const shape = SHAPES[key] ?? SHAPES.IDLE;
  if (svg.dataset.key === key) return;
  svg.dataset.key = key;
  clear(svg);
  svg.setAttribute('class', `status-icon tone-${shape.tone}`);
  const hollow = shape.hollow || shape.line;
  svg.appendChild(s('path', {
    d: shape.d,
    fill: hollow ? 'none' : 'currentColor',
    stroke: hollow ? 'currentColor' : 'none',
    'stroke-width': hollow ? 1.5 : 0,
    'stroke-linecap': 'round',
    'stroke-linejoin': 'round',
  }));
  if (shape.mark) {
    svg.appendChild(s('path', { d: 'M6 4.4v3.2M6 8.8v.8', stroke: 'var(--surface-0)', 'stroke-width': 1.4, 'stroke-linecap': 'round' }));
  }
}

/** Status badge: shape and label. Use updateStatus(node, status) on a later state. */
export function statusBadge(status) {
  const label = h('span.status-badge__label');
  const icon = statusIcon(status);
  const node = h('span.status-badge', icon, label);
  node._icon = icon;
  node._label = label;
  updateStatus(node, status);
  return node;
}

export function updateStatus(node, status) {
  node.className = `status-badge tone-${statusTone(status)}`;
  paintIcon(node._icon, status);
  setText(node._label, statusLabel(status));
}

/** A horizontal fill bar. `value` is 0..1 or null. */
export function meter({ value = null, label, tone } = {}) {
  const fill = h('span.meter__fill');
  const node = h('span.meter', { role: 'img', 'aria-label': label ?? '' }, fill);
  node._fill = fill;
  updateMeter(node, value, tone);
  return node;
}

export function updateMeter(node, value, tone) {
  const pct = value === null || value === undefined ? 0 : Math.max(0, Math.min(1, value)) * 100;
  node._fill.style.width = `${pct.toFixed(1)}%`;
  node.className = tone ? `meter meter--${tone}` : 'meter';
}

/**
 * Sparkline. `update(values, {reference})` redraws. A thin 1.5 px line with an end dot and an optional dashed
 * reference line (setpoint). Height stays free so it fills its row. The y range is padded and never collapses to nothing.
 */
export function sparkline({ width = 240, height = 44, label = '', minSpan = 1 } = {}) {
  const line = s('polyline.spark__line', { fill: 'none', 'vector-effect': 'non-scaling-stroke' });
  const reference = s('line.spark__ref', { 'vector-effect': 'non-scaling-stroke', visibility: 'hidden' });
  const dot = s('circle.spark__dot', { r: 2.5, visibility: 'hidden' });
  const svg = s('svg.spark', { viewBox: `0 0 ${width} ${height}`, role: 'img', 'aria-label': label, preserveAspectRatio: 'none' }, reference, line, dot);
  return {
    node: svg,
    update(values, { reference: ref } = {}) {
      const clean = (values ?? []).filter((v) => typeof v === 'number');
      if (clean.length < 2) {
        line.setAttribute('points', '');
        dot.setAttribute('visibility', 'hidden');
        reference.setAttribute('visibility', 'hidden');
        return;
      }
      let min = Math.min(...clean);
      let max = Math.max(...clean);
      if (typeof ref === 'number') {
        min = Math.min(min, ref);
        max = Math.max(max, ref);
      }
      if (max - min < minSpan) {
        const mid = (max + min) / 2;
        min = mid - minSpan / 2;
        max = mid + minSpan / 2;
      }
      const pad = 4;
      const x = (i) => (i / (clean.length - 1)) * (width - 6);
      const y = (v) => pad + (1 - (v - min) / (max - min)) * (height - 2 * pad);
      line.setAttribute('points', clean.map((v, i) => `${x(i).toFixed(1)},${y(v).toFixed(1)}`).join(' '));
      dot.setAttribute('cx', x(clean.length - 1).toFixed(1));
      dot.setAttribute('cy', y(clean[clean.length - 1]).toFixed(1));
      dot.setAttribute('visibility', 'visible');
      if (typeof ref === 'number') {
        reference.setAttribute('x1', 0);
        reference.setAttribute('x2', width);
        reference.setAttribute('y1', y(ref).toFixed(1));
        reference.setAttribute('y2', y(ref).toFixed(1));
        reference.setAttribute('visibility', 'visible');
      } else {
        reference.setAttribute('visibility', 'hidden');
      }
    },
  };
}

/** Standard panel with a head row. Returns {node, body, head, actions}. */
export function panel(title, { dense = false, actions = [] } = {}) {
  const actionsNode = h('div.panel__actions', actions);
  const head = h('header.panel__head', h('h2.panel__title', title), actionsNode);
  const body = h('div.panel__body');
  const node = h(`section.panel${dense ? '.panel--dense' : ''}`, head, body);
  return { node, head, body, actions: actionsNode };
}

/** Shape-and-label empty state. */
export function empty(text) {
  return h('p.empty', text);
}

const ICONS = {
  hall: 'M2 14V6l6-3 6 3v8M5 14V9h6v5M2 14h12',
  list: 'M5 4h9M5 8h9M5 12h9M2 4h.8M2 8h.8M2 12h.8',
  gantt: 'M2 3h6M5 7h7M3 11h5M9 13h5',
  trace: 'M3 3.5a1.5 1.5 0 1 0 0 .01M13 8a1.5 1.5 0 1 0 0 .01M3 12.5a1.5 1.5 0 1 0 0 .01M4.4 4 11.6 7.4M4.4 12l7.2-3.4',
  twin: 'M2 5h8v8H2zM6 5V2.5h8v8h-4',
  gauge: 'M2.5 12a6 6 0 1 1 11 0M8 11l3-5',
  alarm: 'M8 2 14.5 13.5h-13ZM8 6.5v3.4M8 11.6v.4',
  opcua: 'M8 2v3.5M3 13v-3.5h10V13M8 5.5v4M6.5 2h3v3h-3zM1.5 13h3v2h-3zM11.5 13h3v2h-3z',
  close: 'M3.5 3.5l9 9M12.5 3.5l-9 9',
  chevron: 'M4 6l4 4 4-4',
};

/** Line icon on a 16x16 grid. */
export function icon(name, size = 16) {
  return s('svg.icon', { viewBox: '0 0 16 16', width: size, height: size, 'aria-hidden': 'true', fill: 'none', stroke: 'currentColor', 'stroke-width': 1.3, 'stroke-linecap': 'round', 'stroke-linejoin': 'round' },
    s('path', { d: ICONS[name] ?? '' }));
}
