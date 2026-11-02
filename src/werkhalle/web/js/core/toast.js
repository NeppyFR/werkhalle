// Transient messages. `kind` is info, ok, warn or error; errors stay longer and are announced assertively.
import { h } from './dom.js';
import { statusIcon } from '../components/ui.js';

let host = null;
const DURATION = { info: 4000, ok: 4000, warn: 7000, error: 9000 };
const ICON = { info: 'info', ok: 'ok', warn: 'warn', error: 'FAULT' };

export function initToasts(element) {
  host = element;
}

export function toast(message, { kind = 'info', ms } = {}) {
  if (!host) return;
  const close = () => node.remove();
  const node = h(`div.toast.toast--${kind}`, { role: kind === 'error' ? 'alert' : undefined },
    statusIcon(ICON[kind]),
    h('span.toast__text', message),
    h('button.toast__close', { type: 'button', 'aria-label': 'Meldung schließen', onclick: close }, '×'));
  host.appendChild(node);
  while (host.children.length > 4) host.firstChild.remove();
  setTimeout(close, ms ?? DURATION[kind]);
}
