// Left navigation rail. VIEWS is the one list of views; the router and the rail both read it.
import { h, setText } from '../core/dom.js';
import { icon } from './ui.js';

export const VIEWS = [
  { name: 'leitstand', label: 'Leitstand', icon: 'hall' },
  { name: 'auftraege', label: 'Aufträge', icon: 'list' },
  { name: 'planung', label: 'Planung', icon: 'gantt' },
  { name: 'rueckverfolgung', label: 'Rückverfolgung', icon: 'trace' },
  { name: 'zwilling', label: 'Zwilling', icon: 'twin' },
  { name: 'oee', label: 'OEE & Energie', icon: 'gauge' },
  { name: 'alarme', label: 'Alarme', icon: 'alarm' },
  { name: 'opcua', label: 'OPC UA', icon: 'opcua' },
];

export function mountRail(root, { router, store }) {
  const links = new Map();
  const badge = h('span.rail__badge', { hidden: true });
  const list = h('ul.rail__list');
  for (const view of VIEWS) {
    const link = h('a.rail__link', { href: router.href(view.name) },
      icon(view.icon, 18),
      h('span.rail__label', view.label),
      view.name === 'alarme' ? badge : null);
    links.set(view.name, link);
    list.appendChild(h('li', link));
  }
  root.appendChild(list);

  const markCurrent = ({ name }) => {
    for (const [viewName, link] of links) {
      if (viewName === name) link.setAttribute('aria-current', 'page');
      else link.removeAttribute('aria-current');
    }
  };
  router.onChange(markCurrent);
  markCurrent(router.current);

  store.subscribe((state) => {
    const open = state.alarms_active?.filter((a) => !a.acked).length ?? 0;
    badge.hidden = open === 0;
    setText(badge, open);
    badge.setAttribute('aria-label', `${open} offene Alarme`);
  });
}
