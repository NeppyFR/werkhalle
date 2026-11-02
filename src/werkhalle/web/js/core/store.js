// Live data. The WebSocket handler only stores the newest state; one timer hands it to subscribers. Nothing queues
// per message, so a slow view or a hidden tab can never build up a backlog.
import { SimClock } from './clock.js';
import { setCatalog } from './format.js';

const PUMP_MS = 100;
const BACKOFF_MS = [500, 1000, 2000, 4000, 8000];
const SILENCE_LIMIT_MS = 6000;
const LOG_LIMIT = 300;
// About 15 s of failed attempts. Long enough for a blip, short enough that nobody waits on a plant that has stopped.
const GONE_AFTER_ATTEMPTS = 5;

export function createStore({ api, toast }) {
  let state = null;
  let version = 0;
  let delivered = 0;
  let plan = null;
  let catalog = null;
  let proposals = { open: null, history: [] };
  let lastProposal = null;
  let log = [];
  let uiMode = 'schatten';

  let socket = null;
  let socketState = 'connecting';
  let attempt = 0;
  let retryAt = 0;
  let retryTimer = 0;
  let lastMessage = 0;
  let everOpen = false;
  let proposalCheck = 0;
  let lastConnTick = 0;

  const clock = new SimClock();
  const subscribers = new Set();
  const handlers = new Map();

  function emit(type, payload) {
    for (const handler of handlers.get(type) ?? []) {
      try {
        handler(payload);
      } catch (error) {
        console.error(`Handler für "${type}" fehlgeschlagen`, error);
      }
    }
  }

  function setSocketState(next) {
    if (socketState === next) return;
    socketState = next;
    emit('conn', link());
  }

  function deliver() {
    if (version === delivered) return;
    delivered = version;
    clock.sync(state.sim);
    checkProposal(state.proposal);
    for (const subscriber of subscribers) {
      try {
        subscriber(state);
      } catch (error) {
        console.error('Abonnent fehlgeschlagen', error);
      }
    }
    emit('conn', link());
  }

  function acceptState(next) {
    if (state && next.run_id !== state.run_id) {
      const previous = state.run_id;
      state = next;
      version += 1;
      log = [];
      plan = null;
      proposals = { open: null, history: [] };
      lastProposal = null;
      toast(`Die Anlage wurde neu gestartet (Lauf ${next.run_id} statt ${previous}). Anzeige neu geladen.`, { kind: 'warn' });
      emit('run-change', next.run_id);
      refetchAll();
      return;
    }
    state = next;
    version += 1;
  }

  // The state carries the open proposal too. If a discrete message was lost, this notices and refetches.
  function checkProposal(summary) {
    const held = proposals.open;
    const same = (!summary && !held) || (summary && held && summary.id === held.id && summary.status === held.status);
    if (same) return;
    const now = performance.now();
    if (now - proposalCheck < 1000) return;
    proposalCheck = now;
    fetchProposals();
  }

  function applyProposal(data) {
    lastProposal = data;
    if (data.status === 'computing' || data.status === 'open') {
      proposals = { ...proposals, open: data };
    } else if (proposals.open?.id === data.id) {
      proposals = { open: null, history: [data, ...proposals.history].slice(0, 20) };
    }
    emit('proposal', data);
  }

  async function fetchProposals() {
    try {
      const data = await api.get('/api/proposals', { quiet: true });
      const held = proposals.open;
      proposals = { open: data.open ?? null, history: data.history ?? [] };
      if (data.open) {
        applyProposal(data.open);
      } else if (held) {
        // It closed while we were not listening: the newest history entry says how.
        const closed = data.history?.find((p) => p.id === held.id);
        applyProposal(closed ?? { ...held, status: 'expired', status_reason: 'Der Vorschlag ist nicht mehr offen.' });
      }
    } catch {
      /* the connection indicator already tells the story */
    }
  }

  async function fetchPlan() {
    try {
      plan = await api.get('/api/plan', { quiet: true });
      emit('plan', plan);
    } catch {
      /* retried on the next plan message or reconnect */
    }
  }

  async function fetchLog() {
    try {
      const data = await api.get('/api/log?limit=200', { quiet: true });
      log = (data.entries ?? []).slice(0, LOG_LIMIT);
      emit('log-reset', log);
    } catch {
      /* the live stream still fills it */
    }
  }

  async function fetchCatalog() {
    if (catalog) return;
    try {
      catalog = await api.get('/api/catalog', { quiet: true });
      setCatalog(catalog);
      emit('catalog', catalog);
    } catch {
      /* retried on reconnect */
    }
  }

  async function fetchState() {
    try {
      acceptState(await api.get('/api/state', { quiet: true }));
    } catch {
      /* the socket delivers it as soon as it can */
    }
  }

  function refetchAll() {
    fetchCatalog();
    fetchState();
    fetchPlan();
    fetchProposals();
    fetchLog();
  }

  function onDiscrete(type, data) {
    switch (type) {
      case 'proposal':
        applyProposal(data);
        break;
      case 'plan':
        emit('plan-changed', data);
        fetchPlan();
        break;
      case 'log':
        log = [data, ...log].slice(0, LOG_LIMIT);
        emit('log', data);
        break;
      case 'alarm':
      case 'order':
        emit(type, data);
        break;
      default:
        break;
    }
  }

  function scheduleReconnect() {
    const delay = BACKOFF_MS[Math.min(attempt, BACKOFF_MS.length - 1)] * (0.8 + Math.random() * 0.4);
    attempt += 1;
    retryAt = performance.now() + delay;
    clearTimeout(retryTimer);
    retryTimer = setTimeout(connect, delay);
  }

  function connect() {
    const scheme = location.protocol === 'https:' ? 'wss' : 'ws';
    setSocketState('connecting');
    try {
      socket = new WebSocket(`${scheme}://${location.host}/ws`);
    } catch {
      setSocketState('closed');
      scheduleReconnect();
      return;
    }
    socket.onopen = () => {
      attempt = 0;
      lastMessage = performance.now();
      setSocketState('open');
      refetchAll();
      if (everOpen) toast('Verbindung zum MES wiederhergestellt.', { kind: 'ok' });
      everOpen = true;
    };
    socket.onmessage = (event) => {
      lastMessage = performance.now();
      const message = JSON.parse(event.data);
      if (message.type === 'state') {
        acceptState(message.data);
      } else {
        onDiscrete(message.type, message.data);
      }
    };
    socket.onclose = () => {
      socket = null;
      setSocketState('closed');
      scheduleReconnect();
    };
    socket.onerror = () => {
      socket?.close();
    };
  }

  function pump() {
    deliver();
    const now = performance.now();
    if (socketState === 'open' && now - lastMessage > SILENCE_LIMIT_MS && !document.hidden) {
      socket?.close();
    }
    // While disconnected nothing else ticks, and the indicator shows a countdown.
    if (socketState !== 'open' && now - lastConnTick > 1000) {
      lastConnTick = now;
      emit('conn', link());
    }
  }

  /** What the connection indicator shows. level: ok | stale | down | wait. */
  function link() {
    if (socketState !== 'open') {
      const inSeconds = Math.max(0, Math.ceil((retryAt - performance.now()) / 1000));
      if (attempt >= GONE_AFTER_ATTEMPTS) {
        return {
          level: 'down',
          gone: true,
          label: 'MES getrennt',
          detail: 'Die Werkhalle läuft nicht mehr. Bitte mit start.cmd neu starten.',
        };
      }
      return {
        level: socketState === 'connecting' && !everOpen ? 'wait' : 'down',
        label: socketState === 'connecting' ? 'Verbinde …' : 'MES getrennt',
        detail: socketState === 'connecting'
          ? 'Verbindung zum MES wird aufgebaut.'
          : `Die Verbindung zum MES ist unterbrochen. Neuer Versuch in ${inSeconds} s.`,
      };
    }
    if (!state) return { level: 'wait', label: 'Verbinde …', detail: 'Warte auf den ersten Zustand vom MES.' };
    if (!state.connected) return { level: 'down', label: 'Getrennt', detail: 'Das MES hat keine Verbindung zum OPC-UA-Server der Anlage.' };
    if (state.stale) return { level: 'stale', label: 'Veraltet', detail: 'Der Heartbeat der Anlage bleibt aus. Die Anzeige kann veraltet sein.' };
    return {
      level: 'ok',
      label: 'Verbunden',
      detail: `Live über OPC UA, ${(state.counters?.datachanges_per_s ?? 0).toLocaleString('de-DE')} DataChanges pro Sekunde.`,
    };
  }

  return {
    get state() { return state; },
    get plan() { return plan; },
    get catalog() { return catalog; },
    get proposals() { return proposals; },
    get lastProposal() { return lastProposal; },
    get log() { return log; },
    get uiMode() { return uiMode; },
    clock,
    link,

    start() {
      fetchCatalog();
      connect();
      setInterval(pump, PUMP_MS);
    },

    /** fn(state) on every new state, at most 10 times a second. Calls back at once if a state exists. Returns unsubscribe. */
    subscribe(fn, { immediate = true } = {}) {
      subscribers.add(fn);
      if (immediate && state) fn(state);
      return () => subscribers.delete(fn);
    },

    /**
     * Discrete events: alarm, proposal, order, log, plan (full plan after a refetch), plan-changed (the raw message),
     * log-reset, catalog, conn, run-change, ui-mode. Returns unsubscribe.
     */
    on(type, fn) {
      if (!handlers.has(type)) handlers.set(type, new Set());
      handlers.get(type).add(fn);
      return () => handlers.get(type).delete(fn);
    },

    /** Current state of one machine, or undefined. */
    machine(id) {
      return state?.machines?.find((m) => m.id === id);
    },

    /** The mode badge: 'schatten' (live mirror) or 'zwilling' (fork). The router resets it on every view change. */
    setUiMode(mode) {
      if (mode === uiMode) return;
      uiMode = mode;
      emit('ui-mode', mode);
    },

    refetchPlan: fetchPlan,
    refetchProposals: fetchProposals,
  };
}
