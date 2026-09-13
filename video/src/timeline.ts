/**
 * Scene timing, in frames at 30fps.
 *
 * The film (4560 fr = 2:32) and the cut (1800 fr = 0:60) share the same scene
 * components. Scenes drive their narrative from normalised progress
 * (localFrame / duration), so the same scene reads correctly at either length —
 * the cut simply gives each beat less dwell.
 *
 * The slices live in timeline.json so that scripts/score.mjs — which is plain
 * Node and cannot import TypeScript — reads the *same* numbers and places its
 * cues against the same timecode. That is the whole point: the score cannot
 * drift from the picture, at either length.
 */
import timeline from './timeline.json';

export const FPS = timeline.fps;

export type Slice = { key: SceneKey; from: number; duration: number };

export type SceneKey =
  | 'coldOpen'
  | 'title'
  | 'thePath'
  | 'lateAndSuperseded'
  | 'pileUp'
  | 'recovery'
  | 'theWitness'
  | 'evidence'
  | 'close';

const KEYS: SceneKey[] = [
  'coldOpen',
  'title',
  'thePath',
  'lateAndSuperseded',
  'pileUp',
  'recovery',
  'theWitness',
  'evidence',
  'close',
];

/** A mistyped key in timeline.json would otherwise surface as a blank scene. */
const asSlices = (raw: typeof timeline.film, label: string): Slice[] =>
  raw.map((s) => {
    if (!KEYS.includes(s.key as SceneKey)) {
      throw new Error(`timeline.json: "${s.key}" in ${label} is not a known scene`);
    }
    return { key: s.key as SceneKey, from: s.from, duration: s.duration };
  });

const end = (slices: Slice[]) => slices[slices.length - 1].from + slices[slices.length - 1].duration;

/** 2:32 film. */
export const FILM: Slice[] = asSlices(timeline.film, 'film');
export const FILM_FRAMES = end(FILM);

/** 0:60 cut — same story, no wasted frame. */
export const CUT: Slice[] = asSlices(timeline.short, 'short');
export const CUT_FRAMES = end(CUT);

const durations = (slices: Slice[]) =>
  slices.reduce((acc, s) => ({ ...acc, [s.key]: s.duration }), {} as Record<SceneKey, number>);

export const FILM_DURATION: Record<SceneKey, number> = durations(FILM);
export const CUT_DURATION: Record<SceneKey, number> = durations(CUT);
