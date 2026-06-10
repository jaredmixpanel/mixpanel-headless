// Node + Pyodide harness for `just test-pyodide-node`.
//
// Proves, in the real Emscripten runtime, that the SLIM wheel:
//   (a) micropip-installs with its declared deps resolvable (bundled base deps
//       loaded locally; the pure-Python `anytree` resolved from PyPI);
//   (b) `import mixpanel_headless` works and the Emscripten gate is live;
//   (c) PyfetchTransport self-registers in MixpanelAPIClient and round-trips a
//       request via a stubbed synchronous XHR;
//   (d) the ThreadPoolExecutor sites fall back to a sequential loop (Pyodide
//       has no OS threads — the threaded path would raise);
//   (e) the chmod / MP_OAUTH_CLIENT_DIR paths run on MEMFS without raising.
//
// Run `node provision.mjs` first (the just recipe does) to stage the bundled
// wheels into node_modules/pyodide.
import { loadPyodide } from 'pyodide';
import { createRequire } from 'node:module';
import { dirname, join } from 'node:path';
import { readFileSync, readdirSync, existsSync } from 'node:fs';

const require = createRequire(import.meta.url);
const pyodideDir = dirname(require.resolve('pyodide/package.json'));

const slimDir = join(import.meta.dirname, '..', '..', 'dist', 'slim');
if (!existsSync(slimDir)) {
  console.error(`no dist/slim dir — run \`just build-wheel-slim\` first (${slimDir})`);
  process.exit(1);
}
const wheelFile = readdirSync(slimDir).find((f) => f.endsWith('.whl'));
if (!wheelFile) {
  console.error(`no slim wheel in ${slimDir} — run \`just build-wheel-slim\` first`);
  process.exit(1);
}

// Bundled base deps loaded from the locally-provisioned wheels. anytree is the
// one pure-Python base dep not bundled by Pyodide; micropip resolves it from
// PyPI when the slim wheel is installed below.
const BASE_PACKAGES = [
  'micropip',
  'pandas',
  'pyarrow',
  'networkx',
  'pydantic',
  'httpx',
  'tomli-w',
];

// Inline Python (kept backslash-free and free of ${} so it embeds verbatim).
// Returns a JSON string of the checks it proved.
const PY_CHECKS = `
import json, os, sys, pathlib
from datetime import datetime, timezone

result = {}

# (b) import + Emscripten gate is live.
import mixpanel_headless as mp
from mixpanel_headless._internal.runtime import is_emscripten
assert is_emscripten() is True, "is_emscripten() must be True under Pyodide"
result["b_import_and_gate"] = {"platform": sys.platform}

# (c) PyfetchTransport self-registers + stubbed XHR round-trip.
import httpx
from pydantic import SecretStr
from mixpanel_headless._internal.auth.account import ServiceAccount
from mixpanel_headless._internal.auth.session import Project, Session
from mixpanel_headless._internal.api_client import MixpanelAPIClient
from mixpanel_headless._internal.pyodide_transport import PyfetchTransport

session = Session(
    account=ServiceAccount(name="t", region="us", username="u", secret=SecretStr("s")),
    project=Project(id="12345"),
)
client = MixpanelAPIClient(session=session)
assert isinstance(client._transport, PyfetchTransport), "transport must auto-register"
client.close()

class _FakeXHR:
    def __init__(self):
        self.status = 200
        self.responseText = '{"ok": true}'
    def open(self, method, url, async_flag):
        pass
    def setRequestHeader(self, name, value):
        pass
    def send(self, body):
        pass
    def getAllResponseHeaders(self):
        return "content-type: application/json"

transport = PyfetchTransport(xhr_factory=lambda: _FakeXHR())
with httpx.Client(transport=transport) as c:
    resp = c.get("https://mixpanel.com/api/query", headers={"Authorization": "Bearer t"})
assert resp.status_code == 200 and resp.json() == {"ok": True}
result["c_pyfetch_transport"] = "auto-registered + stub round-trip 200"

# (d) ThreadPoolExecutor fallback runs sequentially (threads would raise here).
from unittest.mock import MagicMock
from mixpanel_headless import Workspace
from mixpanel_headless.types import Replay

def _replay(rid):
    return Replay(
        replay_id=rid, distinct_id=None, project_id=12345,
        start_time=1716810000000, end_time=1716810005000, retention_days=30,
    )

ws = Workspace(session=session, _api_client=MagicMock())
ws.fetch_replay = lambda rid, **kwargs: _replay(rid)
bundle = ws.fetch_replays(["r-1", "r-2", "r-3"])
assert [r.replay_id for r in bundle.replays] == ["r-1", "r-2", "r-3"]
result["d_sequential_fallback"] = "fetch_replays ran sequentially under Emscripten"

# (e) chmod / MP_OAUTH_CLIENT_DIR paths run on MEMFS without raising.
os.environ["MP_OAUTH_STORAGE_DIR"] = "/tmp/mp_store"
os.environ["MP_OAUTH_CLIENT_DIR"] = "/tmp/mp_client"
from mixpanel_headless._internal.auth import storage as storage_mod
from mixpanel_headless._internal.auth.storage import OAuthStorage
from mixpanel_headless._internal.auth.token import OAuthClientInfo
from mixpanel_headless._internal.me import MeCache, MeResponse

storage_mod.ensure_account_dir("personal")
st = OAuthStorage(storage_dir=pathlib.Path("/tmp/mp_store/oauth"))
st.save_client_info(
    OAuthClientInfo(
        client_id="c", region="us", redirect_uri="http://x/cb", scope="s",
        created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )
)
assert st.load_client_info("us").client_id == "c"
assert pathlib.Path("/tmp/mp_client/client_us.json").is_file()
MeCache(account_name="personal", storage_dir=pathlib.Path("/tmp/mp_me")).put(
    MeResponse(user_id=1, user_email="a@example.com")
)
result["e_memfs_chmod_and_client_dir"] = "ensure_account_dir + save_client_info + MeCache.put OK"

json.dumps(result)
`;

async function main() {
  console.log(`test-pyodide-node: loading Pyodide from ${pyodideDir}`);
  const pyodide = await loadPyodide({ indexURL: pyodideDir });
  await pyodide.loadPackage(BASE_PACKAGES);

  const micropip = pyodide.pyimport('micropip');
  const wheelBytes = readFileSync(join(slimDir, wheelFile));
  pyodide.FS.mkdirTree('/wheels');
  pyodide.FS.writeFile(`/wheels/${wheelFile}`, wheelBytes);

  // (a) install the slim wheel; deps=true → micropip resolves anytree from PyPI.
  await micropip.install(`emfs:/wheels/${wheelFile}`);
  console.log(`[a] slim wheel installed with deps resolvable: ${wheelFile}`);

  const raw = await pyodide.runPythonAsync(PY_CHECKS);
  const checks = JSON.parse(String(raw));
  for (const [key, value] of Object.entries(checks)) {
    console.log(`[${key}] ${JSON.stringify(value)}`);
  }
  const expected = [
    'b_import_and_gate',
    'c_pyfetch_transport',
    'd_sequential_fallback',
    'e_memfs_chmod_and_client_dir',
  ];
  for (const key of expected) {
    if (!(key in checks)) throw new Error(`missing check: ${key}`);
  }
  console.log('test-pyodide-node: ALL CHECKS PASSED');
}

main().catch((err) => {
  console.error('test-pyodide-node FAILED:', err);
  process.exit(1);
});
