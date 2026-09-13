#!/usr/bin/env node
/**
 * The FreshIndex score.
 *
 * Synthesised from scratch rather than licensed, for three reasons:
 *   1. no attribution or licence file to carry into a portfolio
 *   2. every cue is placed against a real film timecode, so the picture and the
 *      score cannot drift apart
 *   3. it is reproducible — same script, same waveform, forever
 *
 * Section boundaries are read from src/timeline.json — the same numbers the
 * picture cuts to — so the score cannot drift from the film at either length.
 * Cues are placed at fractions of their section, which is why one script
 * scores both the 2:32 film and the 0:60 cut.
 *
 *   node scripts/score.mjs out/score.wav [film|short]
 */
import { writeFileSync, readFileSync } from 'node:fs';

const SR = 48000;
const CUT = process.argv[3] === 'short' ? 'short' : 'film';
const timeline = JSON.parse(readFileSync(new URL('../src/timeline.json', import.meta.url), 'utf8'));
const slices = timeline[CUT];
const FRAMES = slices[slices.length - 1].from + slices[slices.length - 1].duration;
const FPS = timeline.fps;
const DUR = FRAMES / FPS;
const N = Math.round(SR * DUR);

const L = new Float32Array(N);
const R = new Float32Array(N);
const WL = new Float32Array(N);
const WR = new Float32Array(N);

/* ------------------------------------------------------------ primitives */

const mulberry32 = (a) => () => {
  a |= 0;
  a = (a + 0x6d2b79f5) | 0;
  let t = Math.imul(a ^ (a >>> 15), 1 | a);
  t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
  return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
};
const rnd = mulberry32(20260913);

/** MIDI note -> Hz. A1 = 33, A2 = 45, A4 = 69. */
const hz = (n) => 440 * Math.pow(2, (n - 69) / 12);

/** Pluck: exponential decay from the attack. Pad: plateau with slow release. */
function shape(t, dur, kind, attack, rate) {
  if (t < 0 || t > dur) return 0;
  if (kind === 'pad') {
    const rel = Math.min(2.5, dur * 0.45);
    if (t < attack) return t / attack;
    if (t > dur - rel) return Math.max(0, (dur - t) / rel);
    return 1;
  }
  if (t < attack) return t / attack;
  return Math.exp(-((t - attack) / dur) * rate);
}

/**
 * Add a tone into the dry bed and the reverb send.
 * `wave` is deliberately crude — this is a low-passed bed, not a lead synth.
 */
function tone(t0, dur, freq, gain, o = {}) {
  const { kind = 'pluck', attack = 0.008, rate = 6, pan = 0, wet = 0.3, wave = 'sine', detune = 0 } = o;
  const i0 = Math.max(0, Math.floor(t0 * SR));
  const i1 = Math.min(N, Math.ceil((t0 + dur) * SR));
  const gl = (gain * Math.cos(((pan + 1) * Math.PI) / 4)) / (detune ? 2 : 1);
  const gr = (gain * Math.sin(((pan + 1) * Math.PI) / 4)) / (detune ? 2 : 1);
  for (let i = i0; i < i1; i += 1) {
    const t = (i - i0) / SR;
    const e = shape(t, dur, kind, attack, rate);
    if (e <= 0) continue;
    let s = wave === 'saw' ? (((t * freq) % 1) * 2 - 1) : wave === 'tri' ? 4 * Math.abs(((t * freq) % 1) - 0.5) - 1 : Math.sin(2 * Math.PI * freq * t);
    if (detune) s += wave === 'sine' ? Math.sin(2 * Math.PI * freq * (1 + detune) * t) : 0;
    const v = s * e;
    L[i] += v * gl;
    R[i] += v * gr;
    WL[i] += v * gl * wet;
    WR[i] += v * gr * wet;
  }
}

/** Filtered noise. `sweep` moves the cutoff from `cut0` to `cut1` across the event. */
function noise(t0, dur, gain, o = {}) {
  const { cut0 = 400, cut1 = 4000, pan = 0, wet = 0.4 } = o;
  const i0 = Math.max(0, Math.floor(t0 * SR));
  const i1 = Math.min(N, Math.ceil((t0 + dur) * SR));
  const gl = gain * Math.cos(((pan + 1) * Math.PI) / 4);
  const gr = gain * Math.sin(((pan + 1) * Math.PI) / 4);
  let lp = 0;
  for (let i = i0; i < i1; i += 1) {
    const u = (i - i0) / (i1 - i0);
    const cut = cut0 * Math.pow(cut1 / cut0, u);
    const a = Math.min(0.99, (2 * Math.PI * cut) / SR);
    lp += a * ((rnd() * 2 - 1) - lp);
    // Bell-shaped amplitude so the sweep breathes instead of gating.
    const e = Math.sin(Math.PI * u);
    const v = lp * e;
    L[i] += v * gl;
    R[i] += v * gr;
    WL[i] += v * gl * wet;
    WR[i] += v * gr * wet;
  }
}

/** A short filtered click — used for hop markers and state changes. */
function tick(t0, gain, freq, o = {}) {
  const { dur = 0.09, wet = 0.35 } = o;
  const i0 = Math.max(0, Math.floor(t0 * SR));
  const i1 = Math.min(N, Math.ceil((t0 + dur) * SR));
  for (let i = i0; i < i1; i += 1) {
    const t = (i - i0) / SR;
    const e = Math.exp(-(t / dur) * 9);
    const v = Math.sin(2 * Math.PI * freq * t) * e * gain;
    L[i] += v;
    R[i] += v;
    WL[i] += v * wet;
    WR[i] += v * wet;
  }
}

/* --------------------------------------------------------------- sections */

/* Scene key -> score section. The cut drops `title`, so its cue is dropped
 * with it rather than smeared across a scene that is not there. */
const SECTION = {
  coldOpen: 'cold',
  title: 'title',
  thePath: 'path',
  lateAndSuperseded: 'order',
  pileUp: 'stress',
  recovery: 'recov',
  theWitness: 'wit',
  evidence: 'evid',
  close: 'close',
};

/** Section name -> [start, end] in seconds, off a real timeline. */
const sectionsOf = (sl) => {
  const m = {};
  for (const s of sl) m[SECTION[s.key]] = [s.from / FPS, (s.from + s.duration) / FPS];
  return m;
};

const S = sectionsOf(slices);
const FILM_S = sectionsOf(timeline.film);

/**
 * Cues are written as *fractions* of their section, not absolute seconds, so
 * the same cue lands at the same musical moment in a 12 s section and a 5 s
 * one. `scale` is how much longer or shorter this section is than it was in
 * the film — exactly 1.0 for the film itself, so the 2:32 score is unchanged.
 */
const at = (n, f) => S[n][0] + (S[n][1] - S[n][0]) * f;
const len = (n, f) => (S[n][1] - S[n][0]) * f;
const scale = (n) => (S[n][1] - S[n][0]) / (FILM_S[n][1] - FILM_S[n][0]);

// --- cold open: a low room tone that does not yet commit to a key ----------
tone(at('cold', 0.05), len('cold', 0.95), hz(33), 0.15, { kind: 'pad', attack: 3.2 * scale('cold'), wave: 'saw', detune: 0.004, wet: 0.5 });
tone(at('cold', 0.2917), len('cold', 0.625), hz(45), 0.05, { kind: 'pad', attack: 2.5 * scale('cold'), wet: 0.6 });
[0.2667, 0.5333, 0.8].forEach((f) => tick(at('cold', f), 0.05, 1800, { dur: 0.07 }));

// --- title: the key arrives, a fifth opens above it ------------------------
// The cut has no title scene, so it gets no title cue.
if (S.title) {
  tone(at('title', -0.0286), len('title', 1.0286), hz(33), 0.16, { kind: 'pad', attack: 1.2 * scale('title'), wave: 'saw', detune: 0.004, wet: 0.45 });
  tone(at('title', 0.0714), len('title', 0.9286), hz(40), 0.08, { kind: 'pad', attack: 2.6 * scale('title'), wet: 0.5 });
  tone(at('title', 0.0214), len('title', 0.2143), hz(57), 0.14, { kind: 'pluck', rate: 3.2, wet: 0.5 });
  tone(at('title', 0.1571), len('title', 0.2857), hz(52), 0.05, { kind: 'pad', attack: 1.8 * scale('title'), wet: 0.6 });
  tone(at('title', 0.7143), len('title', 0.25), hz(60), 0.05, { kind: 'pad', attack: 1.4 * scale('title'), wet: 0.6 });
}

// --- the path: a data clock at 96 BPM, A minor pentatonic ------------------
// Eighth notes. The pulse is the pipeline moving; it must feel metronomic.
const BPM = 96;
const EIGHTH = 60 / BPM / 2;
const PENTA = [57, 60, 62, 64, 67, 69, 72]; // A3 C4 D4 E4 G4 A4 C5
{
  const [t0, t1] = S.path;
  let k = 0;
  for (let t = t0 + 0.25; t < t1; t += EIGHTH, k += 1) {
    if (k % 2 === 0) tone(t, 0.22, hz(PENTA[(k / 2) % PENTA.length]), 0.05, { rate: 9, wet: 0.35, pan: ((k % 4) - 1.5) * 0.25 });
    if (k % 4 === 0) tone(t, 0.20, hz(33), 0.16, { rate: 14, wet: 0.12 });
  }
  // Drone holds under the clock.
  tone(t0, t1 - t0, hz(33), 0.14, { kind: 'pad', attack: 0.6, wave: 'saw', detune: 0.004, wet: 0.4 });
  tone(t0, t1 - t0, hz(40), 0.06, { kind: 'pad', attack: 0.8, wet: 0.5 });
  // A tick as the camera reaches each hop.
  [0, 0.1607, 0.3286, 0.5, 0.7, 0.8786].forEach((f, i) => tick(at('path', f), 0.075 - i * 0.004, 2400 + i * 260, { dur: 0.10 }));
}

// --- out of order: the clock slips, and a tritone sits under it ------------
{
  const [t0, t1] = S.order;
  tone(t0, t1 - t0, hz(33), 0.14, { kind: 'pad', attack: 1.0, wave: 'saw', detune: 0.005, wet: 0.4 });
  tone(t0, t1 - t0, hz(51), 0.032, { kind: 'pad', attack: 4.0, wet: 0.7 }); // Eb — the tritone
  let k = 0;
  for (let t = t0 + 0.25; t < t1; t += EIGHTH, k += 1) {
    const jitter = (rnd() - 0.5) * 0.11; // arrival order is not truth order
    const idx = PENTA[Math.floor(rnd() * PENTA.length)];
    const cut = rnd() < 0.13 ? 0.34 : 1; // a superseded note is cut short
    tone(t + jitter, 0.24 * cut, hz(idx), 0.045, { rate: 8, wet: 0.4, pan: (rnd() - 0.5) * 0.7 });
    if (k % 4 === 0) tone(t, 0.20, hz(33), 0.15, { rate: 14, wet: 0.12 });
  }
  tick(at('order', 0.1615), 0.09, 1500, { dur: 0.18 }); // supersede
  tick(at('order', 0.3077), 0.10, 3200, { dur: 0.13 }); // lock
}

// --- under stress: the key sags, the clock drags, a warning pulses --------
{
  const [t0, t1] = S.stress;
  tone(t0, t1 - t0, hz(32), 0.16, { kind: 'pad', attack: 1.4, wave: 'saw', detune: 0.006, wet: 0.4 }); // down a semitone
  tone(t0, t1 - t0, hz(34), 0.07, { kind: 'pad', attack: 3.0, wet: 0.5 }); // beats against it
  let k = 0;
  for (let t = t0, gap = EIGHTH; t < t1; t += gap, k += 1) {
    gap = EIGHTH * (1 + Math.min(0.9, (t - t0) / (t1 - t0)) * 1.1); // tempo drags
    if (k % 2 === 0) tone(t, 0.22, hz(PENTA[(k / 2) % PENTA.length]), 0.03, { rate: 8, wet: 0.35 });
  }
  // Warning pulse, accelerating from 1.0 s to 0.34 s apart.
  {
    let t = t0 + 2;
    let gap = 1.0;
    while (t < t1 - 1.5) {
      tone(t, 0.14, 220, 0.055, { rate: 7, wet: 0.2, wave: 'tri' });
      gap = Math.max(0.34, gap * 0.955);
      t += gap;
    }
  }
  noise(at('stress', 0.6154), len('stress', 0.3692), 0.10, { cut0: 200, cut1: 5200, wet: 0.5 }); // riser into recovery
}

// --- recovery: the clock re-locks and the filter opens ---------------------
{
  const [t0, t1] = S.recov;
  noise(t0, len('recov', 0.0938), 0.14, { cut0: 6000, cut1: 200, wet: 0.6 }); // release
  tone(t0, t1 - t0, hz(33), 0.16, { kind: 'pad', attack: 0.4, wave: 'saw', detune: 0.004, wet: 0.4 });
  tone(t0, t1 - t0, hz(45), 0.08, { kind: 'pad', attack: 0.6, wet: 0.5 });
  const fast = EIGHTH * 0.8; // 120 BPM — the backlog drains quickly
  let k = 0;
  for (let t = t0 + 0.3; t < t1; t += fast, k += 1) {
    tone(t, 0.20, hz(PENTA[k % PENTA.length] + 12), 0.045, { rate: 10, wet: 0.4, pan: ((k % 4) - 1.5) * 0.3 });
    if (k % 4 === 0) tone(t, 0.20, hz(33), 0.17, { rate: 14, wet: 0.12 });
  }
}

// --- the difference: a clean triad, deliberately NOT in the bed's key ------
// E major, held and steady while everything underneath stays in A minor. The
// measurement sits apart from the pipeline it measures.
{
  const [t0, t1] = S.wit;
  const hold = t1 - t0 + len('wit', 0.125); // rings on into the evidence wall
  tone(t0, hold, hz(52), 0.055, { kind: 'pad', attack: 2.2 * scale('wit'), wet: 0.75 });
  tone(t0, hold, hz(56), 0.045, { kind: 'pad', attack: 2.6 * scale('wit'), wet: 0.75 });
  tone(t0, hold, hz(59), 0.042, { kind: 'pad', attack: 3.0 * scale('wit'), wet: 0.75 });
  tone(t0, t1 - t0, hz(33), 0.07, { kind: 'pad', attack: 1.5 * scale('wit'), wet: 0.4 });
  [0.125, 0.375, 0.625].forEach((f, i) => tone(at('wit', f), len('wit', 0.15), hz(76 + i * 2), 0.035, { rate: 3.5, wet: 0.8 }));
}

// --- evidence: resolve to A major ------------------------------------------
{
  const [t0, t1] = S.evid;
  [45, 61, 64, 69].forEach((n, i) => tone(t0 + i * 0.16, t1 - t0, hz(n), 0.075, { kind: 'pad', attack: 0.9, wet: 0.6 }));
  tone(at('evid', 0.035), len('evid', 0.3), hz(81), 0.07, { rate: 3.0, wet: 0.8 }); // arrival chime
  const slow = EIGHTH * 2;
  for (let i = 0, t = at('evid', 0.08); t < t1; t += slow, i += 1) {
    tone(t, 0.3, hz(PENTA[i % PENTA.length] + 12), 0.035, { rate: 7, wet: 0.5 });
  }
}

// --- close ------------------------------------------------------------------
{
  const [t0, t1] = S.close;
  tone(t0, t1 - t0, hz(45), 0.09, { kind: 'pad', attack: 0.4, wet: 0.6 });
  tone(t0, t1 - t0, hz(52), 0.06, { kind: 'pad', attack: 0.5, wet: 0.6 });
  tone(t0, t1 - t0, hz(33), 0.12, { kind: 'pad', attack: 0.6, wet: 0.5 });
}

/* ------------------------------------------------------ space and master */

// A short feedback delay network stands in for a room. Three taps at
// mutually prime-ish delays keep it from ringing on a note.
const TAPS = [
  [0.037, 0.42],
  [0.053, 0.36],
  [0.079, 0.30],
];
for (const [d, fb] of TAPS) {
  const k = Math.round(d * SR);
  for (let i = k; i < N; i += 1) {
    WL[i] += WL[i - k] * fb;
    WR[i] += WR[i - k] * fb * 0.97;
  }
}

for (let i = 0; i < N; i += 1) {
  L[i] += WL[i] * 0.20;
  R[i] += WR[i] * 0.20;
}

// Master: gentle soft clip for glue, then normalise to -1.5 dBFS.
let peak = 1e-9;
for (let i = 0; i < N; i += 1) {
  L[i] = Math.tanh(L[i] * 1.1);
  R[i] = Math.tanh(R[i] * 1.1);
  peak = Math.max(peak, Math.abs(L[i]), Math.abs(R[i]));
}
const g = 0.84 / peak;
const fadeIn = 0.6 * SR;
const fadeOut = 3.0 * SR;
for (let i = 0; i < N; i += 1) {
  const f = Math.min(1, i / fadeIn) * Math.min(1, (N - i) / fadeOut);
  L[i] *= g * f;
  R[i] *= g * f;
}

/* -------------------------------------------------------------- wav out */

const HEADER = 44;
const buf = Buffer.alloc(HEADER + N * 4);
buf.write('RIFF', 0);
buf.writeUInt32LE(36 + N * 4, 4);
buf.write('WAVE', 8);
buf.write('fmt ', 12);
buf.writeUInt32LE(16, 16);
buf.writeUInt16LE(1, 20); // PCM
buf.writeUInt16LE(2, 22); // stereo
buf.writeUInt32LE(SR, 24);
buf.writeUInt32LE(SR * 4, 28);
buf.writeUInt16LE(4, 32);
buf.writeUInt16LE(16, 34);
buf.write('data', 36);
buf.writeUInt32LE(N * 4, 40);

for (let i = 0; i < N; i += 1) {
  buf.writeInt16LE(Math.max(-32768, Math.min(32767, Math.round(L[i] * 32767))), HEADER + i * 4);
  buf.writeInt16LE(Math.max(-32768, Math.min(32767, Math.round(R[i] * 32767))), HEADER + i * 4 + 2);
}

const out = process.argv[2] ?? 'out/score.wav';
writeFileSync(out, buf);
console.log(`${out}  ${DUR.toFixed(1)}s  ${(buf.length / 1048576).toFixed(1)} MB  peak ${(g * peak).toFixed(3)}`);
