import { useLayoutEffect, useRef } from 'react';
import * as THREE from 'three';

export type SceneCtx = {
  scene: THREE.Scene;
  camera: THREE.PerspectiveCamera;
  renderer: THREE.WebGLRenderer;
  width: number;
  height: number;
};

export type SceneHandle = {
  /** Advance the scene to an absolute frame. Must be a pure function of `frame`. */
  update: (frame: number) => void;
  /** Draw the current state. */
  render: () => void;
  dispose: () => void;
};

export type SceneFactory = (ctx: SceneCtx) => SceneHandle;

export type CameraOptions = {
  fov?: number;
  near?: number;
  far?: number;
  position: [number, number, number];
  rotation?: [number, number, number];
};

/**
 * Mount a three.js scene into a canvas whose every frame is a pure function of
 * Remotion's frame counter.
 *
 * Determinism contract for anything built on this hook:
 *   - never read performance.now(), Date.now(), or Math.random() while drawing
 *   - seed all variation from lib/rng.ts
 *   - derive all animation from the `frame` argument of update()
 * Violating this makes Remotion's parallel renderers disagree between chunks,
 * which shows up as flicker at chunk seams in the final output.
 */
export function useScene(
  factory: SceneFactory,
  options: { width: number; height: number; camera: CameraOptions },
  frame: number,
) {
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const handleRef = useRef<SceneHandle | null>(null);

  const { width, height } = options;
  const cam = options.camera;
  const factoryRef = useRef(factory);
  factoryRef.current = factory;

  useLayoutEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;

    const renderer = new THREE.WebGLRenderer({ canvas, antialias: true, alpha: false });
    renderer.setPixelRatio(1);
    renderer.setSize(width, height, false);
    renderer.setClearColor(0x071016, 1);

    const scene = new THREE.Scene();
    const camera = new THREE.PerspectiveCamera(cam.fov ?? 42, width / height, cam.near ?? 0.1, cam.far ?? 200);
    camera.position.set(...cam.position);
    if (cam.rotation) camera.rotation.set(cam.rotation[0], cam.rotation[1], cam.rotation[2]);

    const handle = factoryRef.current({ scene, camera, renderer, width, height });
    handleRef.current = handle;

    return () => {
      handle.dispose();
      handleRef.current = null;
      renderer.dispose();
    };
  }, [width, height]);

  useLayoutEffect(() => {
    const handle = handleRef.current;
    if (!handle) return;
    handle.update(frame);
    handle.render();
  }, [frame]);

  return canvasRef;
}
