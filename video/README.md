# FreshIndex — portfolio film

A 2:32 technical film and a 0:60 cut, rendered frame-by-frame with Remotion over
a raw three.js (WebGL) stage. It tells one story:

> A database changes. Those changes have to reach search. Changes can arrive
> late, be superseded, or pile up. So FreshIndex doesn't merely move data — it
> preserves the meaning of committed state and measures whether the search index
> actually catches up. Then we prove it under stress.

---

## The honesty architecture

This is the part that matters. The film is two different kinds of claim shown at
the same time, and it never lets you confuse them.

| | |
|---|---|
| **The motion** is a *simulation* | Particle flow, pile-up, the backlog release, camera moves — all a deterministic model of the pipeline's behaviour. It is not a screen recording. |
| **Every number** is *measured* | Latencies, LSNs, violation counts, SLO percentiles come verbatim out of a real evidence run. Nothing is typed in by hand. |

Both facts are burned into every frame: the badge reads
`SIMULATED PIPELINE · MEASURED RESULTS`, and each stat panel carries a
provenance stamp naming the file it came from (`evidence.json · scenario_a`).

The design rule that follows from this: **the film may not invent a number, and
it may not present modelled motion as a recording.** If a value is not in
`artifact.json`, it does not appear.

### Where the numbers live

`src/data/artifact.json` is generated — never edit it.

```
demo/artifacts/<timestamp>/{evidence.json,scenario_c.json}
        │
        │  node scripts/sync-evidence.mjs
        ▼
video/src/data/artifact.json
        │
        │  scenes import via src/data/fromEvidence.ts
        ▼
      the film
```

Re-run the sync after any fresh evidence run and re-render; the film rebuilds
against the new real numbers. The scene components never change.

### The live-API seam

`src/data/fromEvidence.ts` is the only module that touches data. When the film
is switched to pull from a live API, a `fromLive.ts` produces the *same shape*
and only that import changes. No scene, no timing, and no component moves.
Nothing in `services/`, `demo/`, or `tests/` is read or written by the film.

---

## Rendering

```bash
./scripts/render.sh film        # -> out/freshindex-film.mp4  (2:32, 4560 frames)
./scripts/render.sh short       # -> out/freshindex-60.mp4    (0:60, 1800 frames)
./scripts/render.sh still 2700  # -> out/still-2700.png       (one frame, for checking)
```

This box renders with software GL (SwiftShader), so it is CPU-bound at roughly
**0.15 s per frame** — a measured ~5 minutes for the 4560-frame film at
concurrency 4, and ~2 minutes for the cut. The script renders in two segments
and concatenates, so a failure halfway does not throw away the whole run.
**Completed segments are skipped on re-run**, so if you only change the audio or
the concat step, re-running is nearly free. Segment filenames are per-cut, so
rendering the short cut after the film does not silently reuse the film's
segments. Delete `out/segments/` to force a genuine clean render.

### Both cuts come from one source

`src/timeline.json` is the single source of scene timing: both the film and the
60-second cut, as lists of `{key, from, duration}` slices. `src/timeline.ts`
types it for the renderer, and `scripts/score.mjs` reads the *same file* —
which is why the timing lives in JSON, since the score is plain Node and cannot
import TypeScript. One file, so the picture and the score cannot disagree about
when a scene starts.

Every scene derives its narrative from *normalised* progress —
`p = frame / (duration-1)` — so the identical component works at either length.
The short cut is not a re-edit of a different render; it is the same components
on a faster clock.

The short cut deliberately **keeps `theWitness`** (the independent-measurement
scene, which is the actual differentiator) and drops only `title`.

---

## The score

`scripts/score.mjs` synthesises the soundtrack from scratch and writes
`out/score.wav`. It is generated rather than licensed, for three reasons:

1. no attribution or licence file to carry into a portfolio
2. every cue is placed against a real film timecode, so score and picture cannot
   drift apart
3. it is reproducible — same script, same waveform, forever

Section boundaries are read from `src/timeline.json`, and every cue sits at a
*fraction* of its section rather than an absolute second — so the same cue lands
at the same musical moment in a 12-second section and a 5-second one. That is
what lets one script score both cuts: the 0:60 cut is the same shape on a faster
clock, and the `title` cue is dropped along with the scene it belonged to.

The cue carries the story: a metronomic data clock under "the path"; jittered timing and
a tritone as events arrive out of order; the key dropping a semitone with an
accelerating warning pulse under stress; the clock re-locking on recovery. In
`theWitness` a clean **E major** triad enters and holds *against* the bed's A
minor — the measurement deliberately sits apart from the pipeline it measures.
The film resolves to A major on the evidence wall.

Nothing here is sampled, so there is no third-party audio to clear. If you later
prefer a licensed track, drop it in and delete the `score` step in `render.sh`.

---

## Determinism

Remotion renders frames across parallel workers, so any frame-to-frame
nondeterminism shows up as flicker along a chunk seam. The contract:

- **No** `Date.now()`, `performance.now()`, or `Math.random()` during draw.
- Randomness comes only from the seeded `mulberry32` PRNG (`src/lib/rng.ts`).

A scene that needs randomness derives it from a fixed seed, so frame 2713 is
byte-identical no matter which worker drew it.

---

## Branding

`src/brand.ts` holds the name, handle, and links. **WhatsApp is off by default
(`showWhatsApp: false`)** — a phone number burned into a published video gets
scraped and mirrored, and that cannot be walked back. Flip the flag if you want
it in.

---

## Layout

```
src/
  timeline.json        scene timing for both cuts — the single source
  timeline.ts          types it for the renderer
  Root.tsx             registers FreshIndexFilm, FreshIndexShort, Spike
  brand.ts             name, handle, links, honesty badge text
  theme.ts             colour and type tokens
  data/
    artifact.json      GENERATED — do not edit
    fromEvidence.ts    the only module that touches data
  lib/
    stage.ts           the WebGL stage: particles, rails, lattice, camera
    useScene.ts        renderer setup and per-frame loop
    rail.ts            the pipeline path the camera and particles follow
    rng.ts             seeded mulberry32
  components/
    ui.tsx             captions, stat panels, number tickers, lower third
    PipelineStage.tsx  mounts the stage into a Remotion canvas
  scenes/index.tsx     all nine scenes
scripts/
  render.sh            render either cut
  score.mjs            synthesise the soundtrack
  sync-evidence.mjs    evidence run -> artifact.json
```
