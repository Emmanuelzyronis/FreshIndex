import * as THREE from 'three';
import { useCurrentFrame } from 'remotion';
import { EffectComposer } from 'three/examples/jsm/postprocessing/EffectComposer.js';
import { RenderPass } from 'three/examples/jsm/postprocessing/RenderPass.js';
import { UnrealBloomPass } from 'three/examples/jsm/postprocessing/UnrealBloomPass.js';
import { useScene, type SceneCtx, type SceneHandle } from '../lib/useScene';
import { mulberry32 } from '../lib/rng';

// The seven hops of the pipeline, as points in space.
const WAYPOINTS: [number, number, number][] = [
  [-11, 0.6, 0],
  [-7, -0.9, 1.4],
  [-3.2, 1.1, -1.2],
  [0, -0.4, 0.8],
  [3.2, 1.1, -1.2],
  [7, -0.9, 1.4],
  [11, 0.6, 0],
];

const PARTICLE_COUNT = 5000;
const LUT_SIZE = 1024;

/**
 * SPIKE SCENE — not part of the film.
 *
 * Deliberately carries the full production cost profile (5k GPU particles, a
 * ribbon mesh, and a real bloom pass at 1080p) so that the render timing
 * measured against it is an honest upper bound for the hero WebGL path.
 */
function buildSpike(ctx: SceneCtx): SceneHandle {
  const { scene, camera, renderer, width, height } = ctx;
  const disposables: { dispose: () => void }[] = [];

  const curve = new THREE.CatmullRomCurve3(
    WAYPOINTS.map((p) => new THREE.Vector3(...p)),
    false,
    'catmullrom',
    0.5,
  );

  // Bake the curve into a texture so the vertex shader can sample position directly.
  const lut = new Float32Array(LUT_SIZE * 4);
  for (let i = 0; i < LUT_SIZE; i += 1) {
    const p = curve.getPointAt(i / (LUT_SIZE - 1));
    lut[i * 4 + 0] = p.x;
    lut[i * 4 + 1] = p.y;
    lut[i * 4 + 2] = p.z;
    lut[i * 4 + 3] = 1;
  }
  const pathTex = new THREE.DataTexture(lut, LUT_SIZE, 1, THREE.RGBAFormat, THREE.FloatType);
  pathTex.needsUpdate = true;
  disposables.push(pathTex);

  // --- particles ---------------------------------------------------------
  const rand = mulberry32(20260913);
  const aT = new Float32Array(PARTICLE_COUNT);
  const aLane = new Float32Array(PARTICLE_COUNT * 3);
  const aSpeed = new Float32Array(PARTICLE_COUNT);
  const aSize = new Float32Array(PARTICLE_COUNT);
  const aTint = new Float32Array(PARTICLE_COUNT * 3);

  const palette = [
    new THREE.Color('#67e8f9'), // insert
    new THREE.Color('#f6c96b'), // update
    new THREE.Color('#b39ddb'), // delete
  ];

  for (let i = 0; i < PARTICLE_COUNT; i += 1) {
    aT[i] = rand();
    aLane[i * 3 + 0] = (rand() - 0.5) * 1.5;
    aLane[i * 3 + 1] = (rand() - 0.5) * 1.5;
    aLane[i * 3 + 2] = (rand() - 0.5) * 1.5;
    aSpeed[i] = 0.00018 + rand() * 0.00042;
    aSize[i] = 2.2 + rand() * 5.2;
    const c = palette[i % 7 === 0 ? 2 : i % 3 === 0 ? 1 : 0];
    aTint[i * 3 + 0] = c.r;
    aTint[i * 3 + 1] = c.g;
    aTint[i * 3 + 2] = c.b;
  }

  const geo = new THREE.BufferGeometry();
  // `position` is unused numerically but required by three for a draw range.
  geo.setAttribute('position', new THREE.BufferAttribute(new Float32Array(PARTICLE_COUNT * 3), 3));
  geo.setAttribute('aT', new THREE.BufferAttribute(aT, 1));
  geo.setAttribute('aLane', new THREE.BufferAttribute(aLane, 3));
  geo.setAttribute('aSpeed', new THREE.BufferAttribute(aSpeed, 1));
  geo.setAttribute('aSize', new THREE.BufferAttribute(aSize, 1));
  geo.setAttribute('aTint', new THREE.BufferAttribute(aTint, 3));
  geo.boundingSphere = new THREE.Sphere(new THREE.Vector3(), 40);
  disposables.push(geo);

  const mat = new THREE.ShaderMaterial({
    uniforms: {
      uPath: { value: pathTex },
      uFrame: { value: 0 },
      uScale: { value: height / 2 },
    },
    vertexShader: /* glsl */ `
      attribute float aT;
      attribute vec3 aLane;
      attribute float aSpeed;
      attribute float aSize;
      attribute vec3 aTint;
      uniform sampler2D uPath;
      uniform float uFrame;
      uniform float uScale;
      varying vec3 vTint;
      varying float vFade;
      void main() {
        float t = fract(aT + uFrame * aSpeed);
        vec3 base = texture2D(uPath, vec2(t, 0.5)).xyz;
        vec3 pos = base + aLane * 0.35;
        vec4 mv = modelViewMatrix * vec4(pos, 1.0);
        gl_Position = projectionMatrix * mv;
        gl_PointSize = aSize * (uScale / max(0.001, -mv.z)) * 0.35;
        vTint = aTint;
        // fade in at the head, out at the tail, so particles emerge and resolve
        vFade = smoothstep(0.0, 0.08, t) * (1.0 - smoothstep(0.9, 1.0, t));
      }
    `,
    fragmentShader: /* glsl */ `
      varying vec3 vTint;
      varying float vFade;
      void main() {
        vec2 d = gl_PointCoord - vec2(0.5);
        float r = length(d);
        if (r > 0.5) discard;
        float glow = pow(1.0 - r * 2.0, 2.2);
        gl_FragColor = vec4(vTint * glow * 1.6, glow * vFade);
      }
    `,
    transparent: true,
    blending: THREE.AdditiveBlending,
    depthWrite: false,
  });
  disposables.push(mat);

  const points = new THREE.Points(geo, mat);
  scene.add(points);

  // --- ribbon ------------------------------------------------------------
  const tubeGeo = new THREE.TubeGeometry(curve, 512, 0.035, 8, false);
  const tubeMat = new THREE.MeshBasicMaterial({
    color: new THREE.Color('#2b6f78'),
    transparent: true,
    opacity: 0.55,
  });
  disposables.push(tubeGeo, tubeMat);
  scene.add(new THREE.Mesh(tubeGeo, tubeMat));

  // --- post --------------------------------------------------------------
  const composer = new EffectComposer(renderer);
  composer.setSize(width, height);
  const renderPass = new RenderPass(scene, camera);
  const bloom = new UnrealBloomPass(new THREE.Vector2(width, height), 0.85, 0.5, 0.22);
  composer.addPass(renderPass);
  composer.addPass(bloom);
  disposables.push(composer);

  return {
    update(frame: number) {
      mat.uniforms.uFrame.value = frame;
      // Slow camera drift, frame-derived only.
      const a = frame * 0.004;
      camera.position.set(Math.sin(a * 0.6) * 2.4, 1.2 + Math.cos(a * 0.45) * 0.5, 16);
      camera.lookAt(0, 0, 0);
    },
    render() {
      composer.render();
    },
    dispose() {
      for (const d of disposables) d.dispose();
    },
  };
}

export const Spike: React.FC<{ width: number; height: number }> = ({ width, height }) => {
  const frame = useCurrentFrame();
  const ref = useScene(
    buildSpike,
    { width, height, camera: { fov: 42, position: [0, 1.2, 16] } },
    frame,
  );
  return <canvas ref={ref} style={{ width, height, display: 'block' }} />;
};
