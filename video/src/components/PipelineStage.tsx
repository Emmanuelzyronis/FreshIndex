import { AbsoluteFill, useCurrentFrame, useVideoConfig } from 'remotion';
import { useScene, type CameraOptions } from '../lib/useScene';
import { buildStage, type Director } from '../lib/stage';

/**
 * Full-frame WebGL stage. Every scene is this component plus DOM overlays,
 * so the visual language stays identical across the whole film.
 */
export const PipelineStage: React.FC<{
  director: Director;
  camera?: CameraOptions;
  children?: React.ReactNode;
}> = ({ director, camera, children }) => {
  const frame = useCurrentFrame();
  const { width, height } = useVideoConfig();

  const ref = useScene(
    (ctx) => buildStage(ctx, director),
    { width, height, camera: camera ?? { fov: 40, position: [0, 1.4, 17] } },
    frame,
  );

  return (
    <AbsoluteFill style={{ backgroundColor: '#071016' }}>
      <canvas ref={ref} style={{ width, height, display: 'block' }} />
      {children}
    </AbsoluteFill>
  );
};
