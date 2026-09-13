import { interpolate, spring, useCurrentFrame, useVideoConfig } from 'remotion';
import { theme } from '../theme';
import { honestyBadge } from '../brand';
import { fontMono, fontSans } from '../lib/fonts';

/** Deterministic entrance: fade + a short rise. No bounce. */
export const useEnter = (delay = 0, duration = 22) => {
  const frame = useCurrentFrame();
  const { fps } = useVideoConfig();
  const p = spring({ frame: frame - delay, fps, config: { damping: 200, mass: 0.6 }, durationInFrames: duration });
  return { opacity: p, transform: `translateY(${(1 - p) * 18}px)` };
};

/**
 * The honesty badge. Burned into every frame so a viewer can always tell
 * reconstructed motion from measured values. See video/README.md.
 */
export const HonestyBadge: React.FC<{ measured?: boolean }> = ({ measured = false }) => (
  <div
    style={{
      position: 'absolute',
      top: 44,
      right: 56,
      display: 'flex',
      alignItems: 'center',
      gap: 10,
      fontFamily: fontMono,
      fontSize: 12,
      letterSpacing: '0.16em',
      color: theme.muted,
      textTransform: 'uppercase',
    }}
  >
    <span
      style={{
        width: 7,
        height: 7,
        borderRadius: '50%',
        background: theme.cyan,
        boxShadow: `0 0 12px ${theme.cyan}`,
      }}
    />
    <span>{honestyBadge.simulated}</span>
    <span style={{ opacity: 0.4 }}>·</span>
    <span style={{ color: measured ? theme.green : theme.muted }}>{honestyBadge.measured}</span>
  </div>
);

/** Provenance stamp: names the artifact file a number came from. */
export const SourceTag: React.FC<{ children: React.ReactNode }> = ({ children }) => (
  <span
    style={{
      fontFamily: fontMono,
      fontSize: 11,
      letterSpacing: '0.12em',
      color: theme.muted,
      opacity: 0.75,
      textTransform: 'uppercase',
    }}
  >
    {children}
  </span>
);

/** Burned-in caption. The film must read fully muted. */
export const Caption: React.FC<{ text: string; delay?: number; accent?: string; bottom?: number }> = ({
  text,
  delay = 0,
  accent,
  bottom = 92,
}) => {
  const enter = useEnter(delay);
  return (
    <div
      style={{
        position: 'absolute',
        left: 0,
        right: 0,
        bottom,
        display: 'flex',
        justifyContent: 'center',
        ...enter,
      }}
    >
      <div
        style={{
          fontFamily: fontSans,
          fontSize: 30,
          fontWeight: 400,
          color: theme.text,
          letterSpacing: '-0.01em',
          // Opaque for the same reason as Stat: where a caption crosses the lit
          // lattice, a translucent background leaves a half-dimmed cube showing
          // through the edge of the text block.
          background: theme.bg,
          border: `1px solid ${theme.line}`,
          borderLeft: `3px solid ${accent ?? theme.cyan}`,
          padding: '16px 28px',
          maxWidth: 1180,
          textAlign: 'center',
          textWrap: 'balance',
        }}
      >
        {text}
      </div>
    </div>
  );
};

/** Section kicker + title, top-left. */
export const SceneTitle: React.FC<{ kicker: string; title: string; delay?: number }> = ({
  kicker,
  title,
  delay = 0,
}) => {
  const enter = useEnter(delay);
  return (
    <div style={{ position: 'absolute', top: 92, left: 96, maxWidth: 760, ...enter }}>
      <div
        style={{
          fontFamily: fontMono,
          fontSize: 13,
          letterSpacing: '0.22em',
          color: theme.cyan,
          textTransform: 'uppercase',
          marginBottom: 14,
        }}
      >
        {kicker}
      </div>
      <div
        style={{
          fontFamily: fontSans,
          fontSize: 52,
          fontWeight: 800,
          color: theme.text,
          letterSpacing: '-0.035em',
          lineHeight: 1.04,
        }}
      >
        {title}
      </div>
    </div>
  );
};

/** Animated numeric readout. Waits for real evidence values. */
export const NumberTicker: React.FC<{
  value: number;
  decimals?: number;
  delay?: number;
  duration?: number;
  suffix?: string;
  color?: string;
  size?: number;
}> = ({ value, decimals = 1, delay = 0, duration = 34, suffix = '', color = theme.text, size = 44 }) => {
  const frame = useCurrentFrame();
  const shown = interpolate(frame - delay, [0, duration], [0, value], {
    extrapolateLeft: 'clamp',
    extrapolateRight: 'clamp',
  });
  return (
    <span style={{ fontFamily: fontMono, fontSize: size, fontWeight: 700, color, letterSpacing: '-0.02em' }}>
      {shown.toFixed(decimals)}
      {suffix}
    </span>
  );
};

/** Labelled stat used across the evidence wall and recovery readouts. */
export const Stat: React.FC<{
  label: string;
  value: React.ReactNode;
  accent?: string;
  source?: string;
  delay?: number;
  wide?: boolean;
}> = ({ label, value, accent = theme.text, source, delay = 0, wide = false }) => {
  const enter = useEnter(delay);
  return (
    <div
      style={{
        // Opaque, not translucent: these panels sit over the lit lattice and a
        // translucent background lets the 3D read through the numbers.
        background: theme.panel,
        border: `1px solid ${theme.line}`,
        borderLeft: `2px solid ${accent}`,
        padding: '18px 22px',
        minWidth: wide ? 300 : 220,
        ...enter,
      }}
    >
      <div
        style={{
          fontFamily: fontMono,
          fontSize: 11,
          letterSpacing: '0.14em',
          color: theme.muted,
          textTransform: 'uppercase',
          marginBottom: 10,
        }}
      >
        {label}
      </div>
      <div style={{ fontFamily: fontMono, fontSize: 30, fontWeight: 700, color: accent, letterSpacing: '-0.02em' }}>
        {value}
      </div>
      {source ? (
        <div style={{ marginTop: 9 }}>
          <SourceTag>{source}</SourceTag>
        </div>
      ) : null}
    </div>
  );
};

/** Persistent brand lower-third with the public handles. */
export const LowerThird: React.FC<{ delay?: number }> = ({ delay = 0 }) => {
  const enter = useEnter(delay, 30);
  return (
    <div
      style={{
        position: 'absolute',
        left: 96,
        bottom: 46,
        display: 'flex',
        alignItems: 'center',
        gap: 16,
        fontFamily: fontMono,
        fontSize: 13,
        letterSpacing: '0.14em',
        color: theme.muted,
        ...enter,
      }}
    >
      <span style={{ color: theme.text, fontWeight: 700 }}>FRESHINDEX</span>
      <span style={{ opacity: 0.35 }}>|</span>
      <span>EMMANUEL ZYRONIS</span>
      <span style={{ opacity: 0.35 }}>|</span>
      <span style={{ color: theme.cyan }}>@EMMANUELZYRONIS</span>
    </div>
  );
};

/** Thin corner framing that runs through the whole film. */
export const HudFrame: React.FC = () => (
  <div style={{ position: 'absolute', inset: 34, pointerEvents: 'none' }}>
    {(
      [
        ['top', 'left'],
        ['top', 'right'],
        ['bottom', 'left'],
        ['bottom', 'right'],
      ] as const
    ).map(([v, h]) => (
      <div
        key={`${v}-${h}`}
        style={{
          position: 'absolute',
          [v]: 0,
          [h]: 0,
          width: 26,
          height: 26,
          borderTop: v === 'top' ? `1px solid ${theme.line}` : undefined,
          borderBottom: v === 'bottom' ? `1px solid ${theme.line}` : undefined,
          borderLeft: h === 'left' ? `1px solid ${theme.line}` : undefined,
          borderRight: h === 'right' ? `1px solid ${theme.line}` : undefined,
        }}
      />
    ))}
  </div>
);
