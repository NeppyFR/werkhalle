// Entry point: wires the shell once, then the router mounts one view at a time. See README.md.
import { api } from './js/core/api.js';
import { createStore } from './js/core/store.js';
import { fmt } from './js/core/format.js';
import { toast, initToasts } from './js/core/toast.js';
import { initTooltips } from './js/core/tooltip.js';
import { createRouter } from './js/core/router.js';
import { mountTopbar } from './js/components/topbar.js';
import { mountRail, VIEWS } from './js/components/rail.js';
import { createDrawer } from './js/components/drawer.js';
import { mountFailurePanel } from './js/components/failure-panel.js';
import { mountOfflineGuard } from './js/components/offline.js';

initToasts(document.getElementById('toasts'));
initTooltips(document.getElementById('tooltip'));

const store = createStore({ api, toast });
const router = createRouter({
  views: VIEWS,
  fallback: 'leitstand',
  outlet: document.getElementById('view'),
  focusTarget: document.getElementById('main'),
});

const ctx = { store, api, fmt, router, toast };
ctx.drawer = createDrawer(document.getElementById('drawer'), ctx);
router.setContext(ctx);

mountTopbar(document.getElementById('topbar'), ctx);
mountRail(document.getElementById('rail'), ctx);
mountFailurePanel(document.getElementById('failure'), ctx);
mountOfflineGuard(document.getElementById('offline'), ctx);

// The drawer belongs to the machine picture of the view that opened it, and would cover the next view's controls.
router.onChange(() => ctx.drawer.close());

store.start();
router.start();
