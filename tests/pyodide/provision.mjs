// Build-time provisioning of the base-dependency wheels into the local Pyodide
// dir (node_modules/pyodide), mirroring the app's scripts/provision-pyodide.mjs.
//
// The npm `pyodide` package ships only the core runtime (no package wheels), so
// loadPackage() has nothing local to load. We fetch the pinned-version wheel
// closure for the slim wheel's *bundled* base deps HERE and sha256-verify every
// wheel against pyodide-lock.json. Idempotent: a wheel already present with the
// right hash is left untouched. `anytree` is intentionally absent — it is not a
// bundled Pyodide package and is resolved from PyPI by micropip at install time.
import { createHash } from 'node:crypto';
import { createRequire } from 'node:module';
import { dirname, join } from 'node:path';
import { existsSync, readFileSync, writeFileSync } from 'node:fs';

// The slim wheel's bundled base deps (everything except anytree, which comes
// from PyPI). Their transitive closure (numpy, pydantic-core, dateutil, anyio,
// certifi, …) is resolved from the lock.
const TARGETS = [
  'micropip',
  'pandas',
  'pyarrow',
  'networkx',
  'pydantic',
  'httpx',
  'tomli-w',
];

const require = createRequire(import.meta.url);
const pyodideDir = dirname(require.resolve('pyodide/package.json'));
const pyodideVersion = JSON.parse(
  readFileSync(join(pyodideDir, 'package.json'), 'utf8'),
).version;
const lock = JSON.parse(readFileSync(join(pyodideDir, 'pyodide-lock.json'), 'utf8'));
const packages = lock.packages;
const cdnBase = `https://cdn.jsdelivr.net/pyodide/v${pyodideVersion}/full/`;

const normalize = (name) => name.toLowerCase().replace(/[_.]+/g, '-');
const byName = new Map(Object.values(packages).map((p) => [normalize(p.name), p]));

function closure(targets) {
  const seen = new Set();
  const stack = [...targets.map(normalize)];
  while (stack.length > 0) {
    const name = stack.pop();
    if (seen.has(name)) continue;
    const pkg = byName.get(name);
    if (!pkg) throw new Error(`package not in pyodide-lock.json: ${name}`);
    seen.add(name);
    for (const dep of pkg.depends ?? []) stack.push(normalize(dep));
  }
  return [...seen].map((n) => byName.get(n));
}

const sha256Hex = (bytes) => createHash('sha256').update(bytes).digest('hex');

async function ensureWheel(pkg) {
  const dest = join(pyodideDir, pkg.file_name);
  if (existsSync(dest) && sha256Hex(readFileSync(dest)) === pkg.sha256) {
    return 'present';
  }
  const res = await fetch(cdnBase + pkg.file_name);
  if (!res.ok) throw new Error(`download failed ${res.status} for ${pkg.file_name}`);
  const bytes = new Uint8Array(await res.arrayBuffer());
  const got = sha256Hex(bytes);
  if (got !== pkg.sha256) {
    throw new Error(
      `sha256 mismatch for ${pkg.file_name}: expected ${pkg.sha256}, got ${got}`,
    );
  }
  writeFileSync(dest, bytes);
  return 'downloaded';
}

async function main() {
  const wheels = closure(TARGETS);
  let downloaded = 0;
  for (const pkg of wheels) {
    if ((await ensureWheel(pkg)) === 'downloaded') downloaded += 1;
  }
  console.log(
    `provision: ${String(wheels.length)} wheels ready in ${pyodideDir} ` +
      `(pyodide ${pyodideVersion}, ${String(downloaded)} downloaded, ` +
      `${String(wheels.length - downloaded)} cached)`,
  );
}

main().catch((err) => {
  console.error(`provision failed: ${err instanceof Error ? err.message : String(err)}`);
  process.exit(1);
});
