// Small DOM helpers. Text always goes through textContent or createTextNode, so plant and order strings cannot inject markup.
const SVG_NS = 'http://www.w3.org/2000/svg';

function applyAttrs(node, attrs, isSvg) {
  for (const [key, value] of Object.entries(attrs)) {
    if (value === undefined || value === null || value === false) continue;
    if (key === 'class') {
      node.setAttribute('class', value);
    } else if (key === 'style' && typeof value === 'object') {
      Object.assign(node.style, value);
    } else if (key === 'dataset') {
      Object.assign(node.dataset, value);
    } else if (key.startsWith('on') && typeof value === 'function') {
      node.addEventListener(key.slice(2).toLowerCase(), value);
    } else if (!isSvg && key in node && key !== 'list' && key !== 'form') {
      node[key] = value;
    } else {
      node.setAttribute(key, value === true ? '' : String(value));
    }
  }
}

function append(node, children) {
  for (const child of children) {
    if (child === undefined || child === null || child === false) continue;
    if (Array.isArray(child)) append(node, child);
    else if (child instanceof Node) node.appendChild(child);
    else node.appendChild(document.createTextNode(String(child)));
  }
}

function parseTag(spec) {
  const parts = spec.split('.');
  return { tag: parts[0] || 'div', classes: parts.slice(1).join(' ') };
}

/** HTML element. `h('div.panel.panel--dense', {onclick}, 'Text', child, [more])`. Attrs may be left out. */
export function h(spec, attrs, ...children) {
  const { tag, classes } = parseTag(spec);
  const node = document.createElement(tag);
  if (classes) node.className = classes;
  if (attrs instanceof Node || typeof attrs !== 'object' || attrs === null || Array.isArray(attrs)) {
    append(node, [attrs]);
  } else {
    if (attrs.class && classes) attrs = { ...attrs, class: `${classes} ${attrs.class}` };
    applyAttrs(node, attrs, false);
  }
  append(node, children);
  return node;
}

/** SVG element, same call shape as h(). All attributes are set with setAttribute. */
export function s(spec, attrs, ...children) {
  const { tag, classes } = parseTag(spec);
  const node = document.createElementNS(SVG_NS, tag);
  if (classes) node.setAttribute('class', classes);
  if (attrs instanceof Node || typeof attrs !== 'object' || attrs === null || Array.isArray(attrs)) {
    append(node, [attrs]);
  } else {
    if (attrs.class && classes) attrs = { ...attrs, class: `${classes} ${attrs.class}` };
    applyAttrs(node, attrs, true);
  }
  append(node, children);
  return node;
}

/** Like h() for an existing node: null, undefined and false are skipped. The native Element.append() would print them as text. */
export function appendAll(node, ...children) {
  append(node, children);
  return node;
}

/** Writes only when the value changed, so a 4 Hz update of many cells does not touch the DOM needlessly. */
export function setText(node, value) {
  const text = value === undefined || value === null ? '' : String(value);
  if (node.textContent !== text) node.textContent = text;
}

export function setAttr(node, name, value) {
  const text = String(value);
  if (node.getAttribute(name) !== text) node.setAttribute(name, text);
}

export function setClass(node, value) {
  if (node.getAttribute('class') !== value) node.setAttribute('class', value);
}

export function clear(node) {
  while (node.firstChild) node.removeChild(node.firstChild);
}

const loaded = new Map();

/** Adds a stylesheet once. Paths are relative to index.html, e.g. loadCss('css/views/planung.css'). */
export function loadCss(href) {
  if (loaded.has(href)) return loaded.get(href);
  const promise = new Promise((resolve) => {
    const link = document.createElement('link');
    link.rel = 'stylesheet';
    link.href = href;
    link.onload = () => resolve();
    link.onerror = () => resolve();
    document.head.appendChild(link);
  });
  loaded.set(href, promise);
  return promise;
}
