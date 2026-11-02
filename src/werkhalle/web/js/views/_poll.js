// Polling for a view: calls never overlap, a hidden tab skips the work, and stop() ends it with the view.
export function poll(fn, intervalMs, { immediate = true } = {}) {
  let stopped = false;
  let running = false;
  let again = false;
  let timer = 0;

  async function tick() {
    if (stopped) return;
    if (running) {
      again = true;
      return;
    }
    clearTimeout(timer);
    running = true;
    try {
      if (!document.hidden) await fn();
    } catch (error) {
      if (error?.name !== 'ApiError') console.error('Abfrage fehlgeschlagen', error);
    }
    running = false;
    if (stopped) return;
    if (again) {
      again = false;
      tick();
    } else {
      timer = setTimeout(tick, intervalMs);
    }
  }

  if (immediate) tick();
  else timer = setTimeout(tick, intervalMs);

  return {
    /** Fetch right away, for a changed filter. If a call is running, one more follows it. */
    now: tick,
    stop() {
      stopped = true;
      clearTimeout(timer);
    },
  };
}
