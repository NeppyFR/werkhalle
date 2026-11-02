// Hash router: #/name or #/name?a=1&b=2. Each view module exports mount(el, ctx) and returns an unmount function.
import { h, clear } from './dom.js';

export function createRouter({ views, fallback, outlet, focusTarget }) {
  let ctx = null;
  let unmount = null;
  let currentKey = null;
  let token = 0;
  let current = { name: fallback, params: {} };
  const listeners = new Set();

  function parse() {
    const raw = location.hash.replace(/^#\/?/, '');
    const [name, query] = raw.split('?');
    return { name: name || fallback, params: Object.fromEntries(new URLSearchParams(query ?? '')) };
  }

  function teardown() {
    if (unmount) {
      try {
        unmount();
      } catch (error) {
        console.error('Fehler beim Verlassen der Ansicht', error);
      }
    }
    unmount = null;
    clear(outlet);
  }

  async function navigate() {
    const key = location.hash;
    if (key === currentKey) return;
    currentKey = key;
    const next = parse();
    const view = views.find((v) => v.name === next.name);
    if (!view) {
      location.hash = `#/${fallback}`;
      return;
    }
    const mine = ++token;
    const switched = current.name !== next.name || unmount === null;
    current = next;
    document.title = `${view.label} · Werkhalle`;
    for (const listener of listeners) listener(current);
    teardown();
    ctx.store.setUiMode('schatten');

    try {
      const module = await import(`../views/${view.name}.js`);
      if (mine !== token) return;
      const host = h('div.view', { dataset: { view: view.name } });
      outlet.appendChild(host);
      const result = module.mount(host, ctx);
      unmount = typeof result === 'function' ? result : () => {};
      if (switched) focusTarget?.focus({ preventScroll: true });
    } catch (error) {
      if (mine !== token) return;
      console.error(error);
      outlet.appendChild(h('div.view-error', { role: 'alert' },
        h('h1', `Die Ansicht „${view.label}“ konnte nicht geladen werden.`),
        h('p', error.message)));
    }
  }

  function href(name, params) {
    const query = params ? new URLSearchParams(params).toString() : '';
    return `#/${name}${query ? `?${query}` : ''}`;
  }

  return {
    setContext(value) { ctx = value; },
    start() {
      window.addEventListener('hashchange', navigate);
      if (!location.hash) location.hash = `#/${fallback}`;
      navigate();
    },
    /** router.go('planung', {proposal: 'pr-3'}) */
    go(name, params) {
      location.hash = href(name, params);
    },
    /** router.href('planung', {proposal: 'pr-3'}) -> "#/planung?proposal=pr-3" */
    href,
    get current() { return current; },
    get params() { return current.params; },
    onChange(fn) {
      listeners.add(fn);
      return () => listeners.delete(fn);
    },
  };
}
