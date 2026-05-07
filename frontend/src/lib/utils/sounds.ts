/**
 * Cortex sound system — Web Audio API (no Tone.js dependency).
 * Lazy-initializes AudioContext on first user interaction.
 */

let ctx: AudioContext | null = null;
let _volume = 0.3;

function getCtx(): AudioContext | null {
  if (!ctx) {
    try {
      ctx = new AudioContext();
    } catch {
      return null;
    }
  }
  if (ctx.state === 'suspended') ctx.resume();
  return ctx;
}

function playNote(freq: number, duration: number, delay = 0) {
  const ac = getCtx();
  if (!ac) return;
  const osc = ac.createOscillator();
  const gain = ac.createGain();
  osc.type = 'sine';
  osc.frequency.value = freq;
  gain.gain.value = _volume;
  gain.gain.exponentialRampToValueAtTime(0.001, ac.currentTime + delay + duration);
  osc.connect(gain);
  gain.connect(ac.destination);
  osc.start(ac.currentTime + delay);
  osc.stop(ac.currentTime + delay + duration);
}

/** Idea created — ascending two-note chime */
export function bloop() {
  playNote(523, 0.12);       // C5
  playNote(659, 0.15, 0.08); // E5
}

/** Resolve/archive — ascending three-note chime */
export function popChime() {
  playNote(392, 0.1);        // G4
  playNote(523, 0.1, 0.08);  // C5
  playNote(659, 0.12, 0.16); // E5
}

/** Budget approval needed — single bright note */
export function ding() {
  playNote(880, 0.15); // A5
}

/** Status change chimes by status */
const STATUS_NOTES: Record<string, number[]> = {
  active:      [440, 554],       // A4 + C#5
  working:     [494, 587],       // B4 + D5
  needs_input: [523, 659],       // C5 + E5
  done:        [523, 659, 784],  // C5 + E5 + G5
  resolved:    [392, 494, 587],  // G4 + B4 + D5
  failed:      [330, 277],       // E4 + C#4 (descending)
};

export function statusChime(status: string) {
  const notes = STATUS_NOTES[status];
  if (!notes) return;
  notes.forEach((freq, i) => playNote(freq, 0.1, i * 0.08));
}

/** Set volume (0..1) */
export function setVolume(v: number) {
  _volume = Math.max(0, Math.min(1, v));
}

/** Get current volume */
export function getVolume(): number {
  return _volume;
}
