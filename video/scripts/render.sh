#!/usr/bin/env bash
# Render the FreshIndex film and/or the 60s cut.
#
#   ./scripts/render.sh film     -> out/freshindex-film.mp4   (2:32, 4560 frames)
#   ./scripts/render.sh short    -> out/freshindex-60.mp4     (0:60, 1800 frames)
#   ./scripts/render.sh still F  -> out/still-<F>.png         (one verification frame)
#
# Rendering is software-GL on this box, so it is CPU-bound and slow (~0.15 s per
# frame with concurrency 1). Segments are rendered separately and concatenated
# so a failure halfway does not throw away the whole run, and so progress is
# visible. Delete out/segments between full runs to force a clean render.
set -euo pipefail
cd "$(dirname "$0")/.."

case "${1:-film}" in
  film)  ID=FreshIndexFilm;  CUT=film;  TOTAL=4560; OUT=out/freshindex-film.mp4 ;;
  short) ID=FreshIndexShort; CUT=short; TOTAL=1800; OUT=out/freshindex-60.mp4 ;;
  still)
    F="${2:?usage: render.sh still <frame>}"
    ID=FreshIndexFilm
    npx remotion still src/index.ts "$ID" "out/still-$F.png" --frame="$F" --log=error
    echo "wrote out/still-$F.png"
    exit 0 ;;
  *) echo "usage: render.sh [film|short|still <frame>]" >&2; exit 2 ;;
esac

# Two segments keeps peak memory and blast radius down without paying the
# bundle cost more than twice.
SEG=$((TOTAL / 2))
mkdir -p out/segments

render_segment () {
  local from="$1" to="$2" name="$3"
  if [[ -f "out/segments/$name.mp4" ]]; then
    echo "== $name already rendered, skipping"
    return
  fi
  echo "== rendering $name (frames $from-$to)"
  npx remotion render src/index.ts "$ID" "out/segments/$name.mp4" \
    --frames="$from-$to" --concurrency=4 --log=error
}

# Segment names are per-cut: both cuts are two segments, and a shared name let
# a second cut silently reuse the first cut's video and ship the wrong film
# under the right filename.
render_segment 0 $((SEG - 1)) "$ID-a"
render_segment $SEG $((TOTAL - 1)) "$ID-b"

echo "== concatenating"
printf 'file %s\n' "$(pwd)/out/segments/$ID-a.mp4" "$(pwd)/out/segments/$ID-b.mp4" > "out/segments/$ID-list.txt"
SILENT="out/segments/$ID-silent.mp4"
ffmpeg -y -loglevel error -f concat -safe 0 -i "out/segments/$ID-list.txt" -c copy "$SILENT"

# The score is synthesised, not licensed, and every cue is placed against the
# film's own timecode — see scripts/score.mjs. It reads the same timeline.json
# the picture cuts to, so each cut gets its own score.
echo "== scoring"
SCORE="out/$ID-score.wav"
node scripts/score.mjs "$SCORE" "$CUT"
ffmpeg -y -loglevel error -i "$SILENT" -i "$SCORE" \
  -c:v copy -c:a aac -b:a 192k -ar 48000 -shortest "$OUT"

echo "wrote $OUT"
ffprobe -v error -show_entries format=duration,size -of default=nw=1 "$OUT"
