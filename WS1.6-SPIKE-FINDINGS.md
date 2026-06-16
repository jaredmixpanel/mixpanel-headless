# WS1.6 Spike — Interruptible / wall-clock-bounded Mixpanel query in Pyodide

**Date:** 2026-06-16
**Repo / branch:** mixpanel-headless @ `pyodide`
**Runtime under test:** Pyodide **0.29.4** (the version pinned by `tests/pyodide/package.json`)
**Question:** Can the synchronous-XHR Mixpanel query (`_internal/pyodide_transport.py`) be made
**interruptible** (KeyboardInterrupt mid-query) or at least **wall-clock-bounded**, via an
**additive, emscripten-gated** change *confined to the headless transport*, without breaking the
proven synchronous Workspace API or any gate?

**VERDICT: NOT cleanly feasible additively in the headless transport.** The terminate+respawn
backstop remains the fallback for query-bound cells — exactly as SANDBOX-STATE-PLAN.md already
documents (Decision #2, Risks: "Interrupt can't preempt a blocked query. True without WS1.6").
The mechanism that *would* make a query interruptible is off-thread I/O orchestrated by the **host
(mixpanel-lab sandbox window)** via SharedArrayBuffer + Atomics; it cannot live inside, nor be
proven by, the headless transport alone. Evidence below.

---

## The core constraint (restated, then proven)

`PyfetchTransport.handle_request` issues a **synchronous** `XMLHttpRequest`:
`xhr.open(method, url, False)` then `xhr.send(body)` (lines 227/232). A sync `send()` parks the
worker's single JS thread in native code. Pyodide checks the interrupt buffer only at **Python
bytecode boundaries / explicit `pyodide.checkInterrupt()`** — never while the thread is in native
`send()`. And `xhr.timeout` is **ignored in synchronous mode** in browsers, so the request cannot
even be bounded from the same thread. Both facts are the premise of the spike; the approaches
below are the candidate escapes.

---

## Approach 1 — Async XHR + same-thread `Atomics.wait` poll, `checkInterrupt()` each iteration

**Idea:** swap the sync XHR for an async one; on the Pyodide thread, loop on `Atomics.wait(flag, 0,
0, timeout)` calling `pyodide.checkInterrupt()` between waits (Pyodide's documented
interruptible-sleep shape), and have the async `onload` flip the flag to deliver the response.

**Raw evidence** (`/tmp/ws16-spike/atomics-blocks-eventloop.mjs`, plain Node — this is a
thread-semantics fact, runtime-independent):

```
before wait: asyncFired = false
gave up after 5 waits, 519 ms
after loop: asyncFired = false flag = 0 iterations = 5
```

A `setTimeout` callback (standing in for an async-XHR `onload`) scheduled on the same thread
**never ran** across 5 timed `Atomics.wait` iterations / 519 ms, because the event loop is never
pumped while the thread is parked in `Atomics.wait`. **The response can never be delivered to the
polling thread.** Approach 1 is structurally impossible on a single thread.

**Why the cross-worker variant works — and why that disqualifies a transport-only fix**
(`/tmp/ws16-spike/cross-worker-sab.mjs`):

```
main: result delivered by worker = 42 after 3 polls
```

A **second** worker doing the I/O and `Atomics.notify`-ing the SAB *can* hand a result to a thread
parked in `Atomics.wait` — and the gap between polls is exactly where `checkInterrupt()` would fire,
making it genuinely interruptible. **But this requires a second thread doing the network I/O.** In
mixpanel-lab the only thread that could play that role is the **sandbox window's main thread**
(separate from the Pyodide worker thread); Decision #1's COI on the sandbox window is precisely what
makes the SAB usable. That bridge — request marshaling, the Atomics protocol, the fetch on the other
thread — is **host machinery in mixpanel-lab, not the headless transport.** The transport could at
most expose an injected seam for it, but the seam has no counterpart to talk to yet and can't be
exercised in the gate (see "What the node gate cannot prove").

**Verdict: infeasible on one thread; the working variant lives in the host, not headless.**

---

## Approach 2 — Wall-clock deadline enforced outside the sync `send()`

**Idea:** let a SAB the page signals abort a hung request after a deadline.

**Evidence / reasoning:** there is **no same-thread abort of a synchronous XHR** — once in
`send()`, the thread is gone until the request completes; nothing on that thread runs to read a
deadline. A *different* thread cannot abort another thread's in-flight XHR object either (XHR is
thread-local). Bounding therefore also requires moving the request to async + off-thread — i.e. it
collapses into Approach 1's cross-worker shape. `xhr.timeout` is the obvious in-transport knob and it
is **specified to be ignored in synchronous mode**. **Verdict: not achievable in-transport.**

---

## Approach 3 — `pyodide.ffi.run_sync` (JSPI): async `pyfetch` behind a sync surface

**Idea:** keep httpx's sync `handle_request`, but inside it call `run_sync(pyfetch(...))` so an async
fetch presents a synchronous result — and an async fetch *is* abortable / interruptible.

**Raw evidence** (Pyodide 0.29.4, real Emscripten runtime via the harness's pyodide):

```
run_sync probe: {"has_run_sync": true, "has_can_run_sync": true, "can_run_sync_value": false}
```

`run_sync` exists but `can_run_sync()` is **false**. Calling it anyway:

```
File "<exec>", line 5, in <module>
RuntimeError: WebAssembly stack switching not supported in this JavaScript runtime
```

`run_sync` depends on **JSPI / WebAssembly stack switching**, which is an experimental,
flag-/browser-gated capability that is **not enabled** in this runtime (and is not enabled by default
in the Electron/Chromium the app ships on without explicit opt-in). Even if it were:
`run_sync` only works when reached from an Asyncify/JSPI-aware entry (`runPythonAsync`), and enabling
it is a **host runtime decision**, not something the transport can assert. The JS-side
`pyodide.ffi.run_sync` is also `undefined` in 0.29.4. **Verdict: not available; and gating on an
experimental runtime feature would be neither "clean" nor safely additive.**

---

## Approach 4 — A pyodide/emscripten-native mechanism that integrates sync XHR with the interrupt buffer

**Evidence** (same probe run):

```
typeof setInterruptBuffer: function
typeof checkInterrupt:     function
pure-Python loop interrupted: true       # buffer set to SIGINT, `while True: x+=1` raised KeyboardInterrupt
typeof globalThis.XMLHttpRequest: undefined
js.XMLHttpRequest in pyodide:     false
```

The interrupt buffer works perfectly for **Python-bytecode** work (the pure-Python loop was
interrupted) — which is why WS1 already ships value for CPU-bound cells. But there is **no Pyodide /
Emscripten primitive that checks the interrupt buffer during a native synchronous XHR `send()`**: by
construction the buffer is read only at Python boundaries, and a sync `send()` crosses none. Pyodide
offers no "interruptible sync XHR." **Verdict: no native integration exists.**

---

## What the node gate cannot prove (independent reason to not implement here)

`typeof globalThis.XMLHttpRequest` is **`undefined`** in the Node/Pyodide harness and
`js.XMLHttpRequest` is **false** — which is exactly why `tests/pyodide/run.mjs` exercises the
transport through a **fake XHR**, never the real one. Any real interruptible-request path (async
XHR/fetch, SAB bridge, second worker) is **not exercisable in `just test-pyodide-node`**. So even a
speculative in-transport seam could not be backed by a green gate proving the *actual* behavior — it
would add untested surface to a module whose current value is that it is a faithful, proven port of
the live-verified shim.

---

## Conclusion & recommendation

Making a Mixpanel query interruptible/bounded is **achievable** (Approach 1's cross-worker variant is
proven to work), but the mechanism is **off-thread I/O driven by the host**: the mixpanel-lab sandbox
window performs an async fetch on its main thread and delivers the result to the Pyodide worker over a
SharedArrayBuffer with `Atomics.notify`, while the worker does an interruptible
`Atomics.wait`-with-timeout loop calling `pyodide.checkInterrupt()`. Decision #1's sandbox-only COI is
the enabler. **That work belongs in mixpanel-lab WS1, not in an additive headless transport change.**

Implementing anything in `pyodide_transport.py` now would: (a) depend on host worker/bridge plumbing
the transport cannot assume; (b) risk the proven synchronous Workspace API contract; (c) be
unprovable in the headless gate (no real XHR there). Per the spike's stop rule, **STOP** — do not
force it.

**Backstop (already the documented plan):** query-bound cells continue to hit **terminate+respawn**
on timeout (SANDBOX-STATE-PLAN.md Decision #2 + Risks). WS1's interrupt path covers pure-Python /
CPU-bound cells today; query cells fall through to the terminate backstop until/unless the host SAB
fetch-bridge lands.

**Constructive follow-up (contingent, not this spike):** once mixpanel-lab builds the sandbox-window
SAB fetch-bridge, the headless transport can gain an **injected, emscripten-gated alternate backend**
that mirrors the existing `xhr_factory` seam (a `request_via_bridge` callable defaulting to the
current sync XHR). At that point the change is additive *and* testable — a fake bridge can drive it in
`run.mjs`, and the sync API contract is preserved. Until the host counterpart exists, adding the seam
is premature.

## Reproduction

- `tests/pyodide/package.json` pins `pyodide@0.29.4`; `node_modules` provisioned via `npm install`.
- Approach-1 thread semantics: `node atomics-blocks-eventloop.mjs` (plain Node).
- Cross-worker delivery: `node cross-worker-sab.mjs` (plain Node).
- Pyodide capability/interrupt/run_sync probes: run from `tests/pyodide/` so `pyodide` resolves; loads
  Pyodide 0.29.4, checks `setInterruptBuffer`/`checkInterrupt`/`run_sync`/`can_run_sync`, sets the
  interrupt buffer to SIGINT around a `while True` loop, and attempts `run_sync(asyncio.sleep)`.

**No production code changed. No gate touched.**
