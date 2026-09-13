import * as THREE from 'three';
import type { SceneCtx, SceneHandle } from './useScene';
import { mulberry32 } from './rng';
import { WAYPOINTS, buildRailCurve, buildWitnessCurve, curveToLut } from './rail';

export type StageParams = {
  /** 1 = indexer healthy (free flow), 0 = indexer down (particles pile at the gate). */
  gateOpen: number;
  /** Travel speed multiplier. */
  flowSpeed: number;
  /** 0..1 visibility of the independent witness rail. */
  showWitness: number;
  /** 0..1 fraction of Meilisearch lattice nodes lit. */
  latticeLit: number;
  /** Fraction of events that are older/superseded and dissolve at the lattice. */
  staleRatio: number;
  camPos: [number, number, number];
  camLook: [number, number, number];
};

export type Director = (frame: number) => Partial<StageParams>;

const DEFAULTS: StageParams = {
  gateOpen: 1,
  flowSpeed: 1,
  showWitness: 0,
  latticeLit: 1,
  staleRatio: 0,
  camPos: [0, 1.4, 17],
  camLook: [0, 0, 0],
};

const MAIN_PARTICLES = 5000;
const WITNESS_PARTICLES = 1200;
const GATE_POS = 0.62; // between the Indexer hop and Meilisearch
const LATTICE_DIM = 10;

const PARTICLE_FRAG = /* glsl */ `
  varying vec3 vTint;
  varying float vFade;
  void main() {
    vec2 d = gl_PointCoord - vec2(0.5);
    float r = length(d);
    if (r > 0.5) discard;
    // Tight core plus a wide, faint halo. The halo replaces what a bloom pass
    // would do, but stays local to each particle: a full-frame bloom sums its
    // widest mips and lifts the entire background off black.
    float k = 1.0 - r * 2.0;
    float g = pow(k, 2.2) + 0.30 * pow(k, 0.9);
    float a = g * vFade;
    if (a < 0.005) discard;
    gl_FragColor = vec4(vTint * g, a);
  }
`;

function makeParticleGeometry(count: number, seed: number, staleRatio: number) {
  const rand = mulberry32(seed);
  const aT = new Float32Array(count);
  const aLane = new Float32Array(count * 3);
  const aSpeed = new Float32Array(count);
  const aSize = new Float32Array(count);
  const aTint = new Float32Array(count * 3);
  const aStale = new Float32Array(count);

  const cyan = new THREE.Color('#67e8f9');
  const amber = new THREE.Color('#f6c96b');
  const violet = new THREE.Color('#b39ddb');

  for (let i = 0; i < count; i += 1) {
    aT[i] = rand();
    // Bias lanes toward the rail centre so the ribbon reads as a stream, but
    // spread enough that additive blending does not saturate to white.
    aLane[i * 3 + 0] = (rand() - 0.5) * 2.0;
    aLane[i * 3 + 1] = (rand() - 0.5) * 2.0;
    aLane[i * 3 + 2] = (rand() - 0.5) * 2.0;
    aSpeed[i] = 0.00016 + rand() * 0.00040;
    aSize[i] = 1.1 + rand() * 2.0;
    const roll = rand();
    const c = roll < 0.1 ? violet : roll < 0.4 ? amber : cyan;
    aTint[i * 3 + 0] = c.r;
    aTint[i * 3 + 1] = c.g;
    aTint[i * 3 + 2] = c.b;
    aStale[i] = rand() < staleRatio ? 1 : 0;
  }

  const geo = new THREE.BufferGeometry();
  geo.setAttribute('position', new THREE.BufferAttribute(new Float32Array(count * 3), 3));
  geo.setAttribute('aT', new THREE.BufferAttribute(aT, 1));
  geo.setAttribute('aLane', new THREE.BufferAttribute(aLane, 3));
  geo.setAttribute('aSpeed', new THREE.BufferAttribute(aSpeed, 1));
  geo.setAttribute('aSize', new THREE.BufferAttribute(aSize, 1));
  geo.setAttribute('aTint', new THREE.BufferAttribute(aTint, 3));
  geo.setAttribute('aStale', new THREE.BufferAttribute(aStale, 1));
  geo.boundingSphere = new THREE.Sphere(new THREE.Vector3(), 60);
  return geo;
}

function makeParticleMaterial(pathTex: THREE.DataTexture, height: number, tint: string) {
  return new THREE.ShaderMaterial({
    uniforms: {
      uPath: { value: pathTex },
      uFrame: { value: 0 },
      uScale: { value: height / 2 },
      uGateOpen: { value: 1 },
      uGatePos: { value: GATE_POS },
      uSpeedMul: { value: 1 },
      uOpacity: { value: 1 },
      uIntensity: { value: 0.34 },
      uStaleFade: { value: 0 },
      uTintShift: { value: new THREE.Color(tint) },
    },
    vertexShader: /* glsl */ `
      attribute float aT;
      attribute vec3 aLane;
      attribute float aSpeed;
      attribute float aSize;
      attribute vec3 aTint;
      attribute float aStale;
      uniform sampler2D uPath;
      uniform float uFrame;
      uniform float uScale;
      uniform float uGateOpen;
      uniform float uGatePos;
      uniform float uSpeedMul;
      uniform float uOpacity;
      uniform float uIntensity;
      uniform float uStaleFade;
      uniform vec3 uTintShift;
      varying vec3 vTint;
      varying float vFade;

      void main() {
        float raw = fract(aT + uFrame * aSpeed * uSpeedMul);

        // Gate: when the indexer is down, travel stops at the gate and events
        // bunch there. uGateOpen animates 0 -> 1 on recovery, which releases
        // the whole backlog in one visible rush.
        float jam = min(raw, uGatePos + aLane.x * 0.02 * (1.0 - uGateOpen));
        float t = mix(jam, raw, uGateOpen);

        vec3 base = texture2D(uPath, vec2(clamp(t, 0.0, 1.0), 0.5)).xyz;
        vec3 pos = base + aLane * 0.42;
        vec4 mv = modelViewMatrix * vec4(pos, 1.0);
        gl_Position = projectionMatrix * mv;

        float head = smoothstep(0.0, 0.07, t);
        float tail = 1.0 - smoothstep(0.90, 1.0, t);

        // Superseded events dissolve on approach to the lattice.
        float stale = aStale * smoothstep(0.80, 0.98, t) * uStaleFade;

        // Pile-up glow: events at a closed gate brighten as they accumulate.
        float jamGlow = (1.0 - uGateOpen) * (1.0 - smoothstep(uGatePos - 0.06, uGatePos, t));

        vTint = mix(aTint, uTintShift, 0.0) * (1.0 + jamGlow * 1.5);
        vFade = head * tail * (1.0 - stale) * uOpacity * uIntensity;

        gl_PointSize = aSize * (uScale / max(0.001, -mv.z)) * 0.12 * (1.0 + jamGlow * 0.6);
      }
    `,
    fragmentShader: PARTICLE_FRAG,
    transparent: true,
    blending: THREE.AdditiveBlending,
    depthWrite: false,
  });
}

export function buildStage(ctx: SceneCtx, director: Director): SceneHandle {
  const { scene, camera, renderer, height } = ctx;
  const disposables: { dispose: () => void }[] = [];
  const params: StageParams = { ...DEFAULTS };

  const rail = buildRailCurve();
  const railLut = curveToLut(rail);
  disposables.push(railLut);

  const witness = buildWitnessCurve();
  const witnessLut = curveToLut(witness);
  disposables.push(witnessLut);

  // --- rails -------------------------------------------------------------
  const railGeo = new THREE.TubeGeometry(rail, 640, 0.030, 8, false);
  const railMat = new THREE.MeshBasicMaterial({
    color: new THREE.Color('#1d6570'),
    transparent: true,
    opacity: 0.85,
  });
  disposables.push(railGeo, railMat);
  scene.add(new THREE.Mesh(railGeo, railMat));

  const witGeo = new THREE.TubeGeometry(witness, 480, 0.018, 6, false);
  const witMat = new THREE.MeshBasicMaterial({
    color: new THREE.Color('#7de2a8'),
    transparent: true,
    opacity: 0,
  });
  disposables.push(witGeo, witMat);
  scene.add(new THREE.Mesh(witGeo, witMat));

  // --- hop markers -------------------------------------------------------
  const ringGeo = new THREE.RingGeometry(0.30, 0.37, 40);
  const rings: THREE.Mesh[] = [];
  const hopRands = mulberry32(77);
  const ringPhase: number[] = [];
  WAYPOINTS.forEach((wp, i) => {
    const mat = new THREE.MeshBasicMaterial({
      color: new THREE.Color(i === 4 ? '#f6c96b' : i === 6 ? '#7de2a8' : '#67e8f9'),
      transparent: true,
      opacity: 0.75,
      side: THREE.DoubleSide,
    });
    const ring = new THREE.Mesh(ringGeo, mat);
    ring.position.set(...wp);
    scene.add(ring);
    rings.push(ring);
    ringPhase.push(hopRands() * Math.PI * 2);
    disposables.push(mat);
  });
  disposables.push(ringGeo);

  // --- event particles ---------------------------------------------------
  const mainGeo = makeParticleGeometry(MAIN_PARTICLES, 20260913, 0.06);
  const mainMat = makeParticleMaterial(railLut, height, '#67e8f9');
  disposables.push(mainGeo, mainMat);
  scene.add(new THREE.Points(mainGeo, mainMat));

  const witPtsGeo = makeParticleGeometry(WITNESS_PARTICLES, 987654, 0);
  const witPtsMat = makeParticleMaterial(witnessLut, height, '#7de2a8');
  witPtsMat.uniforms.uOpacity.value = 0;
  disposables.push(witPtsGeo, witPtsMat);
  scene.add(new THREE.Points(witPtsGeo, witPtsMat));

  // --- Meilisearch lattice ----------------------------------------------
  const lattice = new THREE.InstancedMesh(
    new THREE.BoxGeometry(0.20, 0.20, 0.20),
    new THREE.MeshBasicMaterial({ transparent: true, opacity: 0.95 }),
    LATTICE_DIM * LATTICE_DIM,
  );
  const litColor = new THREE.Color('#7de2a8');
  const dimColor = new THREE.Color('#16323a');
  const seeds: number[] = [];
  const lrand = mulberry32(4242);
  let li = 0;
  const searchHop = WAYPOINTS[5];
  for (let r = 0; r < LATTICE_DIM; r += 1) {
    for (let c = 0; c < LATTICE_DIM; c += 1) {
      const x = searchHop[0] + (c - (LATTICE_DIM - 1) / 2) * 0.42;
      const y = searchHop[1] + (r - (LATTICE_DIM - 1) / 2) * 0.42;
      lattice.setMatrixAt(li, new THREE.Matrix4().makeTranslation(x, y, searchHop[2] - 0.6));
      seeds.push(lrand());
      li += 1;
    }
  }
  lattice.instanceMatrix.needsUpdate = true;
  scene.add(lattice);
  disposables.push(lattice.geometry, lattice.material);

  // --- post --------------------------------------------------------------
  // No EffectComposer: three renders straight to the canvas so the clear colour
  // (#071016) survives untouched and the output colour space is converted once.
  const lookTarget = new THREE.Vector3();

  return {
    update(frame: number) {
      Object.assign(params, director(frame));

      mainMat.uniforms.uFrame.value = frame;
      mainMat.uniforms.uGateOpen.value = params.gateOpen;
      mainMat.uniforms.uSpeedMul.value = params.flowSpeed;
      mainMat.uniforms.uStaleFade.value = params.staleRatio > 0 ? 1 : 0;

      witPtsMat.uniforms.uFrame.value = frame;
      witPtsMat.uniforms.uSpeedMul.value = params.flowSpeed;
      witPtsMat.uniforms.uOpacity.value = params.showWitness;
      witMat.opacity = params.showWitness * 0.42;
      witMat.color.set(params.showWitness > 0.4 ? '#7de2a8' : '#1f5f68');

      // Lattice lights up from the far edge inward as writes land.
      for (let i = 0; i < lattice.count; i += 1) {
        const on = seeds[i] < params.latticeLit;
        lattice.setColorAt(i, on ? litColor : dimColor);
      }
      if (lattice.instanceColor) lattice.instanceColor.needsUpdate = true;

      // Hop rings breathe, and the indexer ring flashes red during an outage.
      rings.forEach((ring, i) => {
        const breathe = 1 + Math.sin(frame * 0.06 + ringPhase[i]) * 0.10;
        ring.scale.setScalar(breathe);
        const mat = ring.material as THREE.MeshBasicMaterial;
        if (i === 4) {
          const down = 1 - params.gateOpen;
          mat.color.set(down > 0.5 ? '#ff8c8c' : '#f6c96b');
          mat.opacity = 0.55 + down * 0.45;
        }
      });

      camera.position.set(...params.camPos);
      lookTarget.set(...params.camLook);
      camera.lookAt(lookTarget);
    },
    render() {
      renderer.render(scene, camera);
    },
    dispose() {
      for (const d of disposables) d.dispose();
    },
  };
}

export { GATE_POS, MAIN_PARTICLES, WITNESS_PARTICLES };
