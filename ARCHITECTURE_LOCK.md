# Hybrid ↔ OpenClaw Bridge ↔ Dynasty (Lock-in v1)

## Components
- **Hybrid** (FastAPI): conversational layer + tool broker + SSE relay.
- **OpenClaw Bridge** (FastAPI): explicit allowlisted Skill RPC + job runner + SSE events.
- **Dynasty v19 internal sim**: implemented inside bridge as internal skills.

## Network (localhost)
- Hybrid: `http://127.0.0.1:8000`
  - UI: `/ui/chat.html`
  - Chat API: `/v1/chat/completions`
  - SSE: `/v1/events?sessionId=...`
- Bridge: `http://127.0.0.1:17171`
  - Health: `/health`
  - Skills: `/skills`
  - Invoke: `/skills/invoke`
  - Jobs: `/jobs/{jobId}` and `/jobs/{jobId}/events`

## Locked skills (Bridge)
### dynasty_status (internal)
Args:
- `includeWeights: bool` (default true)
Returns:
- `state`: `{t,q,p,S4,E,rms,ema_rms,dt,omega,beta,gamma,eta,delay,learning,plasticity{...},w?}`

### dynasty_step (internal)
Args:
- `n: int` (0 = continuous until canceled)
- `emitEvery: int` (default 50)
- `includeWeights: bool` (default false)
- Optional param overrides: `learning, dt, omega, beta, gamma, eta, delay`
- Reset: `reset: bool`, `seed: int?`
- Plasticity block:
  - `enabled: bool`
  - `mu: float`
  - `emaAlpha: float`
  - `eps: float`
  - `relax: float`
  - `gammaMin: float`, `gammaMax: float`
  - `EHigh: float`

SSE events (Bridge → Hybrid → UI):
- `metrics`: `{sessionId, kind, i, n, metrics{t,E,rms,ema_rms,gamma,dgamma,q,p,S4,u,target,y,err}, w?}`

### dynasty_cancel (internal)
Args:
- `jobId?: string` (optional; if omitted cancels last continuous dynasty_step for session)

### dynasty_preset (internal)
Args:
- `name: "baseline"|"bidirectional-default"`
- `apply: bool` (default true)
Returns:
- preset parameters + current state snapshot
Emits:
- `mark` event: `preset:<name>`

### dynasty_export_run (internal)
Args:
- `steps: int` (default 200000)
- `sampleEvery: int` (default 200)
- `includeWeights: bool` (default true)
- optional overrides: same params as `dynasty_step` including `plasticity` block
Returns:
- paths to `dynasty_export.csv` and `dynasty_export.json` in the job artifacts folder

## Operational convention
- Prefer starting everything via `start_all.cmd`.
- Prefer stopping via `stop_all.cmd`.
- Avoid separate `http.server` for UI; use Hybrid `/ui` to eliminate CORS.
