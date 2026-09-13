import * as THREE from 'three';

/** The seven hops of the FreshIndex path, as spatial waypoints. */
export const WAYPOINTS: [number, number, number][] = [
  [-13.5, 0.5, 0],
  [-9.0, -0.7, 1.3],
  [-4.5, 1.0, -1.1],
  [0.0, -0.35, 0.7],
  [4.5, 1.0, -1.1],
  [9.0, -0.7, 1.3],
  [13.5, 0.5, 0],
];

/** Node metadata, authored to match the real architecture. */
export const HOPS = [
  { key: 'source', label: 'PostgreSQL', sub: 'committed state' },
  { key: 'capture', label: 'Logical CDC', sub: 'pgoutput / WAL' },
  { key: 'reader', label: 'CDC Reader', sub: 'deterministic event' },
  { key: 'delivery', label: 'Redis Streams', sub: 'durable queue' },
  { key: 'projection', label: 'Indexer', sub: 'LSN-gated apply' },
  { key: 'search', label: 'Meilisearch', sub: 'materialized view' },
  { key: 'measure', label: 'Independent SLO', sub: 'commit → visible' },
] as const;

export const LUT_SIZE = 1024;

export function buildRailCurve() {
  return new THREE.CatmullRomCurve3(
    WAYPOINTS.map((p) => new THREE.Vector3(...p)),
    false,
    'catmullrom',
    0.5,
  );
}

/** Bake a curve into a float texture so shaders can sample position by `t`. */
export function curveToLut(curve: THREE.CatmullRomCurve3): THREE.DataTexture {
  const data = new Float32Array(LUT_SIZE * 4);
  for (let i = 0; i < LUT_SIZE; i += 1) {
    const p = curve.getPointAt(i / (LUT_SIZE - 1));
    data[i * 4 + 0] = p.x;
    data[i * 4 + 1] = p.y;
    data[i * 4 + 2] = p.z;
    data[i * 4 + 3] = 1;
  }
  const tex = new THREE.DataTexture(data, LUT_SIZE, 1, THREE.RGBAFormat, THREE.FloatType);
  tex.needsUpdate = true;
  return tex;
}

/**
 * The witness rail — the monitor's own replication slot. Deliberately drawn
 * BELOW and apart from the main rail: the visual argument that measurement is
 * independent of the pipeline it measures.
 */
export function buildWitnessCurve() {
  return new THREE.CatmullRomCurve3(
    WAYPOINTS.map(([x, y, z]) => new THREE.Vector3(x * 0.94, y - 4.6, z * 0.94)),
    false,
    'catmullrom',
    0.5,
  );
}
