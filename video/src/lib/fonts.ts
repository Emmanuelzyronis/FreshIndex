import { loadFont as loadInter } from '@remotion/google-fonts/Inter';
import { loadFont as loadPlexMono } from '@remotion/google-fonts/IBMPlexMono';

/**
 * Fonts are downloaded and bundled at build time so renders are reproducible
 * offline and identical across parallel render workers.
 */
const inter = loadInter('normal', { weights: ['400', '600', '800'] });
const mono = loadPlexMono('normal', { weights: ['400', '500', '600', '700'] });

export const fontSans = inter.fontFamily;
export const fontMono = mono.fontFamily;
