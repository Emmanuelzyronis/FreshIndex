import { Composition, Sequence } from 'remotion';
import { Spike } from './spike/Spike';
import { SCENES } from './scenes';
import { CUT, CUT_FRAMES, FILM, FILM_FRAMES, type Slice } from './timeline';

/**
 * Both cuts are assembled from the same scene components. The film and the cut
 * differ only in how long each scene is allowed to breathe — see timeline.ts.
 */
const Cut: React.FC<{ slices: Slice[] }> = ({ slices }) => (
  <>
    {slices.map((s) => {
      const Scene = SCENES[s.key];
      return (
        <Sequence key={s.key} from={s.from} durationInFrames={s.duration} name={s.key}>
          <Scene duration={s.duration} />
        </Sequence>
      );
    })}
  </>
);

export const RemotionRoot: React.FC = () => {
  return (
    <>
      <Composition
        id="FreshIndexFilm"
        component={Cut}
        durationInFrames={FILM_FRAMES}
        fps={30}
        width={1920}
        height={1080}
        defaultProps={{ slices: FILM }}
      />
      <Composition
        id="FreshIndexShort"
        component={Cut}
        durationInFrames={CUT_FRAMES}
        fps={30}
        width={1920}
        height={1080}
        defaultProps={{ slices: CUT }}
      />

      {/* Go/no-go render-timing spike. Not part of the film. */}
      <Composition
        id="Spike"
        component={Spike}
        durationInFrames={90}
        fps={30}
        width={1920}
        height={1080}
        defaultProps={{ width: 1920, height: 1080 }}
      />
    </>
  );
};
