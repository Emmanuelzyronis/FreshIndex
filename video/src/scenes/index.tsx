import { AbsoluteFill, interpolate, useCurrentFrame, useVideoConfig } from 'remotion';
import artifact from '../data/artifact.json';
import { brand } from '../brand';
import { theme } from '../theme';
import { fontMono, fontSans } from '../lib/fonts';
import { HOPS, WAYPOINTS } from '../lib/rail';
import type { Director, StageParams } from '../lib/stage';
import { PipelineStage } from '../components/PipelineStage';
import { Caption, HonestyBadge, HudFrame, LowerThird, NumberTicker, SceneTitle, SourceTag, Stat, useEnter } from '../components/ui';

type SceneProps = { duration: number };

const clamp01 = (v: number) => (v < 0 ? 0 : v > 1 ? 1 : v);
/** Normalised scene progress — the clock every scene's narrative runs on. */
const p = (frame: number, duration: number) => clamp01(frame / Math.max(1, duration - 1));
const lerp = (a: number, b: number, t: number) => a + (b - a) * t;

/** Fade the whole frame in and out. Scenes never cut hard. */
function useSceneFade(duration: number, inF = 18, outF = 20) {
  const frame = useCurrentFrame();
  return Math.min(
    interpolate(frame, [0, inF], [0, 1], { extrapolateRight: 'clamp' }),
    interpolate(frame, [duration - outF, duration - 1], [1, 0], { extrapolateLeft: 'clamp' }),
  );
}

/* ------------------------------------------------------------------ camera */

type CamKey = { at: number; pos: [number, number, number]; look: [number, number, number] };

/** A 3/4 view of a hop, offset laterally so the rail reads in depth. */
function hopCam(i: number, dist = 7.2, up = 2.1, side = -2.4): Omit<CamKey, 'at'> {
  const wp = WAYPOINTS[i];
  return { pos: [wp[0] + side, wp[1] + up, wp[2] + dist], look: [wp[0], wp[1], wp[2]] };
}

/** Catmull-free linear camera track over normalised progress. */
function camTrack(keys: CamKey[], t: number): { camPos: [number, number, number]; camLook: [number, number, number] } {
  const x = clamp01(t);
  let a = keys[0];
  let b = keys[keys.length - 1];
  for (let i = 0; i < keys.length - 1; i += 1) {
    if (x >= keys[i].at && x <= keys[i + 1].at) {
      a = keys[i];
      b = keys[i + 1];
      break;
    }
  }
  const span = b.at - a.at || 1;
  const u = clamp01((x - a.at) / span);
  const e = u * u * (3 - 2 * u); // smoothstep — no camera snap
  return {
    camPos: [lerp(a.pos[0], b.pos[0], e), lerp(a.pos[1], b.pos[1], e), lerp(a.pos[2], b.pos[2], e)],
    camLook: [lerp(a.look[0], b.look[0], e), lerp(a.look[1], b.look[1], e), lerp(a.look[2], b.look[2], e)],
  };
}

/* -------------------------------------------------------------- hop rail UI */

/** Screen-space diagram of the seven hops; the active node tracks the camera. */
const HopRail: React.FC<{ active: number; delay?: number; compact?: boolean; bottom?: number }> = ({
  active,
  delay = 0,
  compact,
  bottom = 78,
}) => {
  const enter = useEnter(delay, 26);
  return (
    <div
      style={{
        position: 'absolute',
        left: 96,
        right: 96,
        bottom,
        display: 'flex',
        alignItems: 'flex-start',
        ...enter,
      }}
    >
      {HOPS.map((hop, i) => {
        const on = i <= active;
        const now = i === active;
        return (
          <div key={hop.key} style={{ flex: 1, display: 'flex', flexDirection: 'column', alignItems: 'center' }}>
            <div style={{ display: 'flex', alignItems: 'center', width: '100%' }}>
              <div
                style={{
                  flex: 1,
                  height: 1,
                  background: i === 0 ? 'transparent' : on ? theme.cyan : theme.line,
                  opacity: i === 0 ? 0 : 0.7,
                }}
              />
              <div
                style={{
                  width: now ? 11 : 7,
                  height: now ? 11 : 7,
                  borderRadius: '50%',
                  background: on ? theme.cyan : theme.line,
                  boxShadow: now ? `0 0 16px ${theme.cyan}` : 'none',
                }}
              />
              <div
                style={{
                  flex: 1,
                  height: 1,
                  background: i === HOPS.length - 1 ? 'transparent' : on ? theme.cyan : theme.line,
                  opacity: i === HOPS.length - 1 ? 0 : 0.7,
                }}
              />
            </div>
            <div
              style={{
                marginTop: 10,
                fontFamily: fontMono,
                fontSize: compact ? 10 : 11,
                letterSpacing: '0.10em',
                textAlign: 'center',
                color: now ? theme.text : on ? theme.muted : theme.line,
                textTransform: 'uppercase',
                whiteSpace: 'nowrap',
              }}
            >
              {hop.label}
            </div>
            <div
              style={{
                fontFamily: fontMono,
                fontSize: 9,
                letterSpacing: '0.06em',
                color: theme.muted,
                opacity: now ? 0.85 : 0.4,
                textAlign: 'center',
                whiteSpace: 'nowrap',
              }}
            >
              {hop.sub}
            </div>
          </div>
        );
      })}
    </div>
  );
};

/** Small value chip that rides above the hop rail. */
const Chip: React.FC<{ label: string; value: string; accent?: string; delay?: number }> = ({
  label,
  value,
  accent = theme.cyan,
  delay = 0,
}) => {
  const enter = useEnter(delay, 18);
  return (
    <div
      style={{
        background: 'rgba(9,22,29,0.86)',
        border: `1px solid ${theme.line}`,
        borderLeft: `2px solid ${accent}`,
        padding: '12px 18px',
        ...enter,
      }}
    >
      <div style={{ fontFamily: fontMono, fontSize: 10, letterSpacing: '0.14em', color: theme.muted, textTransform: 'uppercase' }}>
        {label}
      </div>
      <div style={{ fontFamily: fontMono, fontSize: 22, fontWeight: 700, color: accent, marginTop: 6, letterSpacing: '-0.01em' }}>
        {value}
      </div>
    </div>
  );
};

/* --------------------------------------------------------------- 0 coldOpen */

export const ColdOpen: React.FC<SceneProps> = ({ duration }) => {
  const frame = useCurrentFrame();
  const fade = useSceneFade(duration, 24, 24);
  const t = p(frame, duration);

  const beats = [
    { at: 0.05, text: 'A database changes.' },
    { at: 0.30, text: 'Those changes have to reach search.' },
    { at: 0.58, text: 'Not eventually. Provably.' },
  ];
  const active = beats.filter((b) => t >= b.at).at(-1);
  const beat = active ?? beats[0];

  const director: Director = (): Partial<StageParams> => ({
    gateOpen: 1,
    flowSpeed: 0.55,
    camPos: [lerp(0, 0, t), lerp(7.5, 4.2, t), lerp(38, 27, t)],
    camLook: [0, 0, 0],
  });

  return (
    <AbsoluteFill style={{ opacity: fade, backgroundColor: theme.bg }}>
      <PipelineStage director={director} camera={{ fov: 40, position: [0, 7.5, 38] }}>
        <HudFrame />
        <HonestyBadge />
        <div style={{ position: 'absolute', inset: 0, display: 'flex', alignItems: 'center', justifyContent: 'center' }}>
          <KeyedText keyText={beat.text} startP={beat.at} t={t} />
        </div>
      </PipelineStage>
    </AbsoluteFill>
  );
};

/** Cross-fades the cold-open lines without a hard DOM swap. */
const KeyedText: React.FC<{ keyText: string; startP: number; t: number }> = ({ keyText, startP, t }) => {
  const local = clamp01((t - startP) / 0.18);
  const out = clamp01((t - 0.9) / 0.1);
  return (
    <div
      style={{
        fontFamily: fontSans,
        fontSize: 62,
        fontWeight: 600,
        color: theme.text,
        letterSpacing: '-0.035em',
        opacity: local * (1 - out),
        transform: `translateY(${(1 - local) * 14}px)`,
        textAlign: 'center',
      }}
    >
      {keyText}
    </div>
  );
};

/* ------------------------------------------------------------------ 1 title */

export const TitleCard: React.FC<SceneProps> = ({ duration }) => {
  const fade = useSceneFade(duration, 20, 22);
  return (
    <AbsoluteFill style={{ opacity: fade, backgroundColor: theme.bg }}>
      <GridBackdrop />
      <HudFrame />
      <HonestyBadge />
      <div
        style={{
          position: 'absolute',
          inset: 0,
          display: 'flex',
          flexDirection: 'column',
          alignItems: 'center',
          justifyContent: 'center',
          gap: 26,
        }}
      >
        <div
          style={{
            fontFamily: fontSans,
            fontSize: 118,
            fontWeight: 800,
            color: theme.text,
            letterSpacing: '-0.055em',
            lineHeight: 1,
          }}
        >
          Fresh<span style={{ color: theme.cyan }}>Index</span>
        </div>
        <div
          style={{
            fontFamily: fontMono,
            fontSize: 17,
            letterSpacing: '0.20em',
            color: theme.muted,
            textTransform: 'uppercase',
          }}
        >
          PostgreSQL · Redis Streams · Meilisearch
        </div>
        <div style={{ height: 1, width: 520, background: theme.line, marginTop: 12 }} />
        <div
          style={{
            fontFamily: fontMono,
            fontSize: 21,
            color: theme.text,
            letterSpacing: '-0.01em',
          }}
        >
          p99(commit → <span style={{ color: theme.green }}>confirmed visible</span>) ≤ 1000 ms
        </div>
        <div style={{ marginTop: 10 }}>
          <SourceTag>target 500 ms detection delay · {artifact.meta.indexerWorkers} indexer workers</SourceTag>
        </div>
      </div>
      <LowerThird delay={30} />
    </AbsoluteFill>
  );
};

/** Faint technical grid — used behind DOM-only scenes so they still feel built. */
const GridBackdrop: React.FC = () => (
  <div
    style={{
      position: 'absolute',
      inset: 0,
      backgroundImage: `linear-gradient(${theme.line} 1px, transparent 1px), linear-gradient(90deg, ${theme.line} 1px, transparent 1px)`,
      backgroundSize: '68px 68px',
      opacity: 0.16,
      maskImage: 'radial-gradient(circle at 50% 45%, black 10%, transparent 78%)',
      WebkitMaskImage: 'radial-gradient(circle at 50% 45%, black 10%, transparent 78%)',
    }}
  />
);

/* --------------------------------------------------------------- 2 thePath */

export const ThePath: React.FC<SceneProps> = ({ duration }) => {
  const frame = useCurrentFrame();
  const fade = useSceneFade(duration);
  const t = p(frame, duration);

  // Travel the whole rail, then settle on the indexer hop.
  const keys: CamKey[] = [
    { at: 0, ...hopCam(0, 14, 4.4, -5.2) },
    { at: 0.16, ...hopCam(1, 13, 4.2, -5.0) },
    { at: 0.33, ...hopCam(2, 13, 4.2, -5.0) },
    { at: 0.5, ...hopCam(3, 12.5, 4.0, -4.8) },
    { at: 0.7, ...hopCam(4, 12, 3.9, -4.6) },
    { at: 0.88, ...hopCam(5, 13.5, 4.2, -4.6) },
    { at: 1, pos: [0, 8.5, 31], look: [0, 0, 0] },
  ];
  const cam = camTrack(keys, t);
  const activeHop = Math.min(HOPS.length - 1, Math.floor(t * 6.4));

  const director: Director = (): Partial<StageParams> => ({
    gateOpen: 1,
    flowSpeed: lerp(1.0, 1.6, t),
    latticeLit: interpolate(t, [0.6, 0.95], [0.15, 1], { extrapolateLeft: 'clamp', extrapolateRight: 'clamp' }),
    camPos: cam.camPos,
    camLook: cam.camLook,
  });

  const chips = [
    { label: 'commit LSN', value: artifact.happyPath.commitLsn, accent: theme.cyan, show: 0.14 },
    { label: 'commit → xadd', value: `${artifact.happyPath.commitToXaddMs.toFixed(3)} ms`, accent: theme.cyan, show: 0.4 },
    { label: 'queue latency', value: `${artifact.happyPath.queueLatencyMs.toFixed(3)} ms`, accent: theme.amber, show: 0.58 },
    { label: 'batch processing', value: `${artifact.happyPath.batchProcessingMs.toFixed(3)} ms`, accent: theme.amber, show: 0.76 },
    { label: 'commit → visible', value: `${artifact.happyPath.stalenessMs.toFixed(3)} ms`, accent: theme.green, show: 0.92 },
  ];
  const visible = chips.filter((c) => t >= c.show);

  return (
    <AbsoluteFill style={{ opacity: fade, backgroundColor: theme.bg }}>
      <PipelineStage director={director} camera={{ fov: 40, position: [-18.7, 4.9, 14] }}>
        <HudFrame />
        <HonestyBadge />
        <SceneTitle kicker="The path" title="Committed state, carried as its own meaning" />
        <div
          style={{
            position: 'absolute',
            right: 96,
            bottom: 320,
            display: 'flex',
            flexWrap: 'wrap',
            justifyContent: 'flex-end',
            gap: 12,
            maxWidth: 660,
          }}
        >
          {visible.map((c) => (
            <Chip key={c.label} label={c.label} value={c.value} accent={c.accent} delay={0} />
          ))}
        </div>
        <HopRail active={activeHop} delay={10} bottom={54} />
        {t < 0.34 ? (
          <Caption bottom={216} text="Every committed row becomes an event whose identity is derived, not invented." delay={8} />
        ) : t < 0.72 ? (
          <Caption bottom={216} text="It travels with the LSN it came from — so order is a property of the data, not of the network." delay={8} />
        ) : (
          <Caption bottom={216} text="Nothing is trusted to arrive; everything is proven to have arrived." delay={8} accent={theme.green} />
        )}
      </PipelineStage>
    </AbsoluteFill>
  );
};

/* ------------------------------------------------------ 3 lateAndSuperseded */

export const LateAndSuperseded: React.FC<SceneProps> = ({ duration }) => {
  const frame = useCurrentFrame();
  const fade = useSceneFade(duration);
  const t = p(frame, duration);
  const o = artifact.ordering;

  const director: Director = (): Partial<StageParams> => ({
    gateOpen: 1,
    flowSpeed: 0.95,
    staleRatio: interpolate(t, [0.2, 0.6], [0, 1], { extrapolateLeft: 'clamp', extrapolateRight: 'clamp' }),
    latticeLit: 1,
    camPos: [lerp(-6, 1.5, t), lerp(6.0, 7.4, t), lerp(22, 21, t)],
    camLook: [lerp(2, 6, t), lerp(0, 0.2, t), 0],
  });

  const stats = [
    { label: 'versions committed', value: o.committedVersions, accent: theme.cyan },
    { label: 'versions applied', value: o.appliedCount, accent: theme.green },
    { label: 'superseded before visible', value: o.supersededCount, accent: theme.violet },
  ];

  return (
    <AbsoluteFill style={{ opacity: fade, backgroundColor: theme.bg }}>
      <PipelineStage director={director} camera={{ fov: 40, position: [-6, 6, 22] }}>
        <HudFrame />
        <HonestyBadge measured />
        <SceneTitle kicker="Out of order" title="Arrival order is not truth order" />
        <div style={{ position: 'absolute', right: 96, top: 150, display: 'flex', flexDirection: 'column', gap: 12, width: 300 }}>
          {stats.map((s, i) => (
            <Stat
              key={s.label}
              label={s.label}
              accent={s.accent}
              delay={14 + i * 8}
              value={<NumberTicker value={s.value} decimals={0} delay={14 + i * 8} size={30} color={s.accent} />}
            />
          ))}
        </div>
        <div style={{ position: 'absolute', left: 96, bottom: 118, display: 'flex', flexDirection: 'column', gap: 8 }}>
          <div style={{ fontFamily: fontMono, fontSize: 20, color: theme.text }}>
            newest commit <span style={{ color: theme.cyan }}>{o.newestCommitLsn}</span>
            <span style={{ color: theme.muted }}> · final doc </span>
            <span style={{ color: theme.cyan }}>{o.finalDocumentLsn}</span>
          </div>
          <SourceTag>artifacts/portfolio-live · scenario_b_ordering · evidence.json</SourceTag>
        </div>
        {t < 0.42 ? (
          <Caption bottom={248} text="Changes can arrive late. A version superseded before it is indexed is not a lost update — it is a decided one." delay={8} />
        ) : (
          <Caption bottom={248} text="LSN decides which version wins. The index converges on what the database committed to." delay={8} accent={theme.green} />
        )}
      </PipelineStage>
    </AbsoluteFill>
  );
};

/* ---------------------------------------------------------------- 4 pileUp */

export const PileUp: React.FC<SceneProps> = ({ duration }) => {
  const frame = useCurrentFrame();
  const fade = useSceneFade(duration);
  const t = p(frame, duration);
  const r = artifact.recovery;

  // Outage begins at 18% and holds; the gate slams shut over 6%.
  const gateOpen = interpolate(t, [0.18, 0.26], [1, 0], { extrapolateLeft: 'clamp', extrapolateRight: 'clamp' });

  const director: Director = (): Partial<StageParams> => ({
    gateOpen,
    flowSpeed: 1,
    camPos: [lerp(-1.5, 2.2, t), lerp(5.0, 6.4, t), lerp(19, 21.5, t)],
    // Panned a little further right than the lattice strictly needs, so the
    // lit lattice sits to the left of the violation-chart panel rather than
    // underneath it.
    camLook: [lerp(5.6, 7.2, t), lerp(0.6, 0.9, t), 0],
    latticeLit: gateOpen,
  });

  const peak = Math.max(...r.growth.map((g) => g.violationCount));

  return (
    <AbsoluteFill style={{ opacity: fade, backgroundColor: theme.bg }}>
      <PipelineStage director={director} camera={{ fov: 40, position: [-1.5, 5.0, 19] }}>
        <HudFrame />
        <HonestyBadge measured />
        <SceneTitle kicker="Under stress" title="Stop the indexer. Nothing is lost — the SLO says so." />
        <ViolationChart t={t} delay={30} />
        <div style={{ position: 'absolute', left: 96, top: 300, display: 'flex', flexDirection: 'column', gap: 12, width: 268 }}>
          <Stat
            label="indexer outage"
            value={`${r.outageSeconds} s`}
            accent={theme.red}
            delay={14}
            source="scenario_c_recovery"
          />
          <Stat
            label="peak pending violations"
            accent={theme.amber}
            delay={22}
            value={<NumberTicker value={peak} decimals={0} delay={22} size={30} color={theme.amber} />}
          />
        </div>
        <div style={{ position: 'absolute', right: 96, top: 620, width: 320 }}>
          <Stat
            label="peak p99 during outage"
            accent={theme.red}
            delay={40}
            wide
            source="measured, not modelled"
            value={<NumberTicker value={r.peakP99Ms} decimals={0} delay={40} size={30} color={theme.red} suffix=" ms" />}
          />
        </div>
        {t < 0.5 ? (
          <Caption text="Kill the indexer mid-stream. Writes keep committing. The queue keeps them." delay={8} accent={theme.amber} />
        ) : (
          <Caption text="Nothing is dropped — but the index is now behind, and the measurement refuses to hide it." delay={8} accent={theme.red} />
        )}
      </PipelineStage>
    </AbsoluteFill>
  );
};

/** Violation growth, drawn straight from the real scenario C samples. */
const ViolationChart: React.FC<{ t: number; delay: number }> = ({ t, delay }) => {
  const frame = useCurrentFrame();
  const enter = useEnter(delay, 26);
  const growth = artifact.recovery.growth;
  const W = 440;
  const H = 150;
  const maxV = Math.max(...growth.map((g) => g.violationCount));
  const maxT = growth[growth.length - 1].secondsSinceStop;
  const pts = growth.map((g) => [
    (g.secondsSinceStop / maxT) * W,
    H - (g.violationCount / maxV) * H,
  ]);
  const reveal = clamp01((frame - delay) / 70);
  const path = pts.map(([x, y], i) => `${i === 0 ? 'M' : 'L'}${x.toFixed(1)},${y.toFixed(1)}`).join(' ');

  return (
    <div
      style={{
        position: 'absolute',
        right: 96,
        top: 300,
        // Opaque: this panel sits over the lit Meilisearch lattice, and a
        // translucent background lets the cubes read through the axis labels.
        background: theme.panel,
        border: `1px solid ${theme.line}`,
        padding: '20px 22px 14px',
        ...enter,
      }}
    >
      <div style={{ fontFamily: fontMono, fontSize: 11, letterSpacing: '0.14em', color: theme.muted, textTransform: 'uppercase' }}>
        pending violations vs. seconds since outage
      </div>
      <svg width={W} height={H} style={{ display: 'block', marginTop: 14, overflow: 'visible' }}>
        <line x1={0} y1={H} x2={W} y2={H} stroke={theme.line} strokeWidth={1} />
        <line x1={0} y1={0} x2={0} y2={H} stroke={theme.line} strokeWidth={1} />
        <path
          d={`${path} L${W},${H} L0,${H} Z`}
          fill={theme.amber}
          opacity={0.10 * reveal}
        />
        <path
          d={path}
          fill="none"
          stroke={theme.amber}
          strokeWidth={2.5}
          strokeDasharray={4000}
          strokeDashoffset={4000 * (1 - reveal)}
        />
        {pts.map(([x, y], i) => (
          <circle key={i} cx={x} cy={y} r={reveal > i / pts.length ? 3.5 : 0} fill={theme.amber} />
        ))}
      </svg>
      <div style={{ display: 'flex', justifyContent: 'space-between', fontFamily: fontMono, fontSize: 10, color: theme.muted, marginTop: 8, letterSpacing: '0.08em' }}>
        <span>0 s</span>
        <span>{maxV} violations</span>
        <span>{maxT.toFixed(1)} s</span>
      </div>
    </div>
  );
};

/* -------------------------------------------------------------- 5 recovery */

export const Recovery: React.FC<SceneProps> = ({ duration }) => {
  const frame = useCurrentFrame();
  const fade = useSceneFade(duration);
  const t = p(frame, duration);
  const r = artifact.recovery;

  // The indexer returns and the whole backlog is released in one visible rush.
  const gateOpen = interpolate(t, [0.08, 0.3], [0, 1], { extrapolateLeft: 'clamp', extrapolateRight: 'clamp' });

  const director: Director = (): Partial<StageParams> => ({
    gateOpen,
    flowSpeed: interpolate(t, [0.08, 0.42], [0.5, 3.1], { extrapolateLeft: 'clamp', extrapolateRight: 'clamp' }),
    latticeLit: interpolate(t, [0.15, 0.85], [0, 1], { extrapolateLeft: 'clamp', extrapolateRight: 'clamp' }),
    camPos: [lerp(1.0, 0, t), lerp(10, 6.4, t), lerp(20, 26, t)],
    camLook: [lerp(4, 0, t), lerp(1, 0, t), 0],
  });

  const walLag = Math.round(lerp(r.walLagBefore.cdc_products_slot, r.walLagAfter.cdc_products_slot, interpolate(t, [0.3, 0.8], [0, 1], { extrapolateLeft: 'clamp', extrapolateRight: 'clamp' })));

  return (
    <AbsoluteFill style={{ opacity: fade, backgroundColor: theme.bg }}>
      <PipelineStage director={director} camera={{ fov: 40, position: [1, 10, 20] }}>
        <HudFrame />
        <HonestyBadge measured />
        <SceneTitle kicker="Recovery" title="The backlog drains, and the lag goes to zero" />
        <div style={{ position: 'absolute', right: 96, top: 150, display: 'flex', flexDirection: 'column', gap: 12, width: 300 }}>
          <Stat label="WAL lag (cdc slot)" value={`${walLag}`} accent={walLag === 0 ? theme.green : theme.amber} delay={18} />
          <Stat label="pending after recovery" value={`${r.pendingAfter}`} accent={theme.green} delay={34} />
          <Stat label="dead letters" value={`${r.dlqDelta}`} accent={theme.green} delay={46} source="nothing dropped" />
        </div>
        <div
          style={{
            position: 'absolute',
            left: 96,
            bottom: 150,
            fontFamily: fontMono,
            fontSize: 19,
            color: theme.text,
            opacity: interpolate(t, [0.75, 0.92], [0, 1], { extrapolateLeft: 'clamp', extrapolateRight: 'clamp' }),
          }}
        >
          slot lag <span style={{ color: theme.muted }}>{r.walLagBefore.cdc_products_slot}</span> →{' '}
          <span style={{ color: theme.green }}>{r.walLagAfter.cdc_products_slot}</span> · counts consistent{' '}
          <span style={{ color: theme.green }}>{String(r.countsConsistent)}</span>
        </div>
        {t < 0.45 ? (
          <Caption bottom={248} text="Bring it back. The queue was never a buffer that forgets — it is the backlog, preserved." delay={8} accent={theme.green} />
        ) : (
          <Caption bottom={248} text="Replication lag returns to zero. The index converges on committed state." delay={8} accent={theme.green} />
        )}
      </PipelineStage>
    </AbsoluteFill>
  );
};

/* ------------------------------------------------------------- 6 theWitness */

export const TheWitness: React.FC<SceneProps> = ({ duration }) => {
  const frame = useCurrentFrame();
  const fade = useSceneFade(duration);
  const t = p(frame, duration);

  const show = interpolate(t, [0.16, 0.5], [0, 1], { extrapolateLeft: 'clamp', extrapolateRight: 'clamp' });

  // Tilt down and back so the witness rail rises into frame beneath the main rail.
  const director: Director = (): Partial<StageParams> => ({
    gateOpen: 1,
    flowSpeed: 1.1,
    showWitness: show,
    camPos: [lerp(0, 0, t), lerp(2.6, -1.6, t), lerp(19, 23, t)],
    // Pan right as the scene settles so the lit lattice moves in from under the
    // stats column on the right edge.
    camLook: [lerp(0, 2.8, t), lerp(0.4, -3.0, t), 0],
  });

  return (
    <AbsoluteFill style={{ opacity: fade, backgroundColor: theme.bg }}>
      <PipelineStage director={director} camera={{ fov: 40, position: [0, 2.6, 19] }}>
        <HudFrame />
        <HonestyBadge measured />
        <SceneTitle kicker="The difference" title="It does not trust the pipeline it measures" />
        <div
          style={{
            position: 'absolute',
            left: 96,
            bottom: 150,
            width: 640,
            fontFamily: fontMono,
            fontSize: 17,
            lineHeight: 1.7,
            color: theme.muted,
            opacity: show,
          }}
        >
          A second, independent replication slot watches for itself.
          <br />
          <span style={{ color: theme.green }}>
            staleness(E) = first_successful_marker_probe(E) − postgres_commit(E)
          </span>
        </div>
        <div style={{ position: 'absolute', right: 96, top: 150, display: 'flex', flexDirection: 'column', gap: 12, width: 312 }}>
          <Stat
            label="monitored slot"
            value="staleness_monitor"
            accent={theme.green}
            delay={26}
            source="separate from cdc_products_slot"
          />
          <Stat
            label="observed staleness"
            accent={theme.green}
            delay={38}
            value={<NumberTicker value={artifact.happyPath.stalenessMs} decimals={3} delay={38} size={28} color={theme.green} suffix=" ms" />}
          />
          <Stat
            label="reader / indexer trusted"
            value="neither"
            accent={theme.cyan}
            delay={50}
            source="probe reads search directly"
          />
        </div>
        {t < 0.5 ? (
          <Caption bottom={248} text="Every other pipeline asks the writer and the indexer whether they are keeping up." delay={8} />
        ) : (
          <Caption bottom={248} text="FreshIndex asks search itself — from a position neither of them can influence." delay={8} accent={theme.green} />
        )}
      </PipelineStage>
    </AbsoluteFill>
  );
};

/* -------------------------------------------------------------- 7 evidence */

export const Evidence: React.FC<SceneProps> = ({ duration }) => {
  const frame = useCurrentFrame();
  const fade = useSceneFade(duration, 16, 18);
  const t = p(frame, duration);
  const s = artifact.slo;
  const b = artifact.benchmark;

  const rows: { name: string; verdict: string }[] = [
    { name: 'Happy path', verdict: artifact.verdicts.happy_path },
    { name: 'Ordering', verdict: artifact.verdicts.ordering },
    { name: 'Failure & recovery', verdict: artifact.verdicts.recovery },
    { name: 'SLO', verdict: artifact.verdicts.slo },
  ];

  return (
    <AbsoluteFill style={{ opacity: fade, backgroundColor: theme.bg }}>
      <GridBackdrop />
      <HudFrame />
      <HonestyBadge measured />
      <SceneTitle kicker="Evidence" title="Measured, not asserted" delay={4} />
      <div
        style={{
          position: 'absolute',
          left: 96,
          right: 96,
          top: 320,
          display: 'flex',
          gap: 28,
          alignItems: 'flex-start',
        }}
      >
        <div style={{ display: 'flex', flexDirection: 'column', gap: 10, width: 340 }}>
          {rows.map((r, i) => {
            const enter = useEnter(18 + i * 9, 20);
            return (
              <div
                key={r.name}
                style={{
                  display: 'flex',
                  justifyContent: 'space-between',
                  alignItems: 'center',
                  padding: '14px 20px',
                  background: 'rgba(9,22,29,0.78)',
                  border: `1px solid ${theme.line}`,
                  ...enter,
                }}
              >
                <span style={{ fontFamily: fontSans, fontSize: 21, color: theme.text }}>{r.name}</span>
                <span style={{ fontFamily: fontMono, fontSize: 15, letterSpacing: '0.14em', color: theme.green }}>{r.verdict}</span>
              </div>
            );
          })}
          <div style={{ marginTop: 6 }}>
            <SourceTag>run {artifact.meta.runTimestampUtc} · git {artifact.meta.gitCommit} · 2026-09-01</SourceTag>
          </div>
        </div>

        <div style={{ display: 'flex', gap: 12, flexWrap: 'wrap', flex: 1 }}>
          <Stat label="p50" accent={theme.cyan} delay={26} value={<NumberTicker value={s.p50} delay={26} color={theme.cyan} />} source="slo.json · 60 samples" />
          <Stat label="p95" accent={theme.cyan} delay={34} value={<NumberTicker value={s.p95} delay={34} color={theme.cyan} />} source="ms" />
          <Stat label="p99" accent={theme.green} delay={42} value={<NumberTicker value={s.p99} delay={42} color={theme.green} />} source={`budget ${artifact.meta.sloMs} ms`} />
          <Stat label="violations" accent={theme.green} delay={50} value={`${s.violations}`} source="60 samples" />
          <Stat label="max" accent={theme.cyan} delay={58} value={<NumberTicker value={s.max} delay={58} color={theme.cyan} />} source="ms" />
          <Stat label="sustained load p99" accent={theme.violet} delay={66} value={<NumberTicker value={b.p99} delay={66} color={theme.violet} />} source={`${b.samples} samples @ ${b.throughputPerSecond}/s`} />
          <Stat label="violations under load" accent={theme.green} delay={74} value={`${b.violations}`} source="README validation, 2026-09-01" wide />
        </div>
      </div>
      <div
        style={{
          position: 'absolute',
          left: 96,
          bottom: 74,
          fontFamily: fontSans,
          fontSize: 27,
          color: theme.text,
          opacity: interpolate(t, [0.3, 0.6], [0, 1], { extrapolateLeft: 'clamp', extrapolateRight: 'clamp' }),
        }}
      >
        Four scenarios. Four passes. One number the pipeline cannot fake.
      </div>
    </AbsoluteFill>
  );
};

/* ----------------------------------------------------------------- 8 close */

export const Close: React.FC<SceneProps> = ({ duration }) => {
  const fade = useSceneFade(duration, 14, 26);
  const enter = useEnter(8, 30);
  return (
    <AbsoluteFill style={{ opacity: fade, backgroundColor: theme.bg }}>
      <GridBackdrop />
      <HudFrame />
      <HonestyBadge measured />
      <div
        style={{
          position: 'absolute',
          inset: 0,
          display: 'flex',
          flexDirection: 'column',
          alignItems: 'center',
          justifyContent: 'center',
          gap: 22,
          ...enter,
        }}
      >
        <div style={{ fontFamily: fontSans, fontSize: 96, fontWeight: 800, color: theme.text, letterSpacing: '-0.055em' }}>
          Fresh<span style={{ color: theme.cyan }}>Index</span>
        </div>
        <div style={{ fontFamily: fontSans, fontSize: 30, color: theme.muted, letterSpacing: '-0.01em' }}>
          {brand.builtBy} · <span style={{ color: theme.cyan }}>{brand.handle}</span>
        </div>
        <div style={{ display: 'flex', gap: 26, marginTop: 8, fontFamily: fontMono, fontSize: 15, color: theme.muted, letterSpacing: '0.10em' }}>
          <span>{brand.links.x}</span>
          <span style={{ opacity: 0.35 }}>·</span>
          <span>{brand.links.telegram}</span>
          {brand.showWhatsApp ? (
            <>
              <span style={{ opacity: 0.35 }}>·</span>
              <span>{brand.links.whatsapp}</span>
            </>
          ) : null}
        </div>
        <div style={{ height: 1, width: 420, background: theme.line, marginTop: 18 }} />
        <div style={{ fontFamily: fontMono, fontSize: 13, color: theme.muted, letterSpacing: '0.10em' }}>
          p99 commit → confirmed visible ~{artifact.slo.p99.toFixed(1)} ms · SLO {artifact.meta.sloMs} ms · 0 violations
        </div>
      </div>
    </AbsoluteFill>
  );
};

export const SCENES = {
  coldOpen: ColdOpen,
  title: TitleCard,
  thePath: ThePath,
  lateAndSuperseded: LateAndSuperseded,
  pileUp: PileUp,
  recovery: Recovery,
  theWitness: TheWitness,
  evidence: Evidence,
  close: Close,
} as const;
