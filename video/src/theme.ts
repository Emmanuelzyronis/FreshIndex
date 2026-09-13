/** Shared visual language. Every scene pulls colour and type from here. */
export const theme = {
  bg: '#071016',
  panel: '#0c1820',
  line: '#23404a',
  text: '#e9f2ef',
  muted: '#8ca5a6',
  cyan: '#67e8f9',
  green: '#7de2a8',
  amber: '#f6c96b',
  red: '#ff8c8c',
  violet: '#b39ddb',
  mono: '"IBM Plex Mono", ui-monospace, SFMono-Regular, Menlo, monospace',
  sans: 'Inter, ui-sans-serif, system-ui, sans-serif',
  fps: 30,
} as const;

/** Motion constants, in frames at 30fps. */
export const motion = {
  beat: 8,
  enter: 22,
  exit: 16,
} as const;
