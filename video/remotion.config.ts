import { Config } from '@remotion/cli/config';

// 30fps keeps total frame count (and therefore software-GL render cost) at
// roughly half of 60fps while remaining smooth for slow, deliberate motion.
Config.setVideoImageFormat('jpeg');
Config.setJpegQuality(92);
Config.setCodec('h264');
Config.setPixelFormat('yuv420p');
Config.setConcurrency(2);
Config.setChromiumOpenGlRenderer('angle');
Config.overrideWebpackConfig((config) => config);
