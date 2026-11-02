// After several failed reconnects the MES is gone, not just slow. The banner says what to do, and every control that would only fail goes
// disabled until the connection is back. The rail stays, and so does anything marked data-offline-ok.
import { h, setText } from '../core/dom.js';
import { statusIcon } from './ui.js';

const CONTROLS = ['#topbar', '#view', '#failure', '#drawer']
  .map((scope) => ['button', 'select', 'input', 'textarea'].map((tag) => `${scope} ${tag}:not([data-offline-ok])`).join(','))
  .join(',');

export function mountOfflineGuard(root, { store }) {
  const sub = h('span.offline__sub');
  root.append(statusIcon('FAULT', { size: 14 }), h('span', 'Die Werkhalle läuft nicht mehr. Bitte mit start.cmd neu starten.'), sub);
  setText(sub, 'Befehle sind gesperrt. Danach verbindet sich diese Seite von selbst.');

  let offline = false;
  const locked = new Set();

  function lockControls() {
    for (const control of document.querySelectorAll(CONTROLS)) {
      if (control.disabled) continue;
      control.disabled = true;
      locked.add(control);
    }
  }

  function release() {
    for (const control of locked) control.disabled = false;
    locked.clear();
  }

  store.on('conn', (link) => {
    const gone = Boolean(link.gone);
    if (gone) lockControls(); // on every tick, because a remount from the rail builds new controls
    if (gone === offline) return;
    offline = gone;
    root.hidden = !gone;
    document.getElementById('shell').classList.toggle('is-offline', gone);
    if (!gone) release();
  });
}
