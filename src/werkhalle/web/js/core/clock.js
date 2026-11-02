// Local sim clock: extrapolates from the last state so material moves smoothly between the 4 Hz updates.
const MAX_SAMPLE_AGE_S = 2;   // beyond this a wall-clock difference is skew or a stall, not transport delay
const MAX_COAST_S = 3;        // without new state the clock stops instead of inventing plant time
const SLEW_S = 0.3;

export class SimClock {
  constructor() {
    this.base = 0;
    this.baseWall = 0;
    this.factor = 0;
    this.error = 0;
    this.last = 0;
    this.synced = false;
  }

  sync(sim, nowMs = Date.now()) {
    const perf = performance.now();
    const sampleAge = Math.min(MAX_SAMPLE_AGE_S, Math.max(0, nowMs / 1000 - sim.wall_ts));
    const target = sim.t + (sampleAge * sim.factor) / 60;
    const current = this.synced ? this.value(perf) : target;
    const drift = target - current;
    const jumped = Math.abs(drift) > Math.max(3, (sim.factor / 60) * 1.5);
    this.base = jumped ? target : current;
    this.baseWall = perf;
    this.factor = sim.factor;
    this.error = jumped ? 0 : drift;
    if (jumped) this.last = target;
    this.synced = true;
  }

  value(perf) {
    const elapsed = Math.min(MAX_COAST_S, Math.max(0, (perf - this.baseWall) / 1000));
    return this.base + (elapsed * this.factor) / 60 + this.error * Math.min(1, elapsed / SLEW_S);
  }

  /** Current sim minutes. Monotonic between syncs, apart from a deliberate jump after a large correction. */
  now() {
    if (!this.synced) return 0;
    const value = this.value(performance.now());
    if (value > this.last || this.factor === 0) this.last = value;
    return this.last;
  }
}
