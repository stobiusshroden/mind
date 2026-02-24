Latest release: https://github.com/stobiusshroden/mind/releases/tag/v0.1.0

# mind

Hybrid conversational system + OpenClaw-style bridge + Dynasty v19 live reservoir (SSE metrics + bounded plasticity).

## What this is
- **Hybrid** (`hybrid_reservoir_service.py`): FastAPI chat backend with tool-broker + SSE relay.
- **UI** (`/ui/chat.html`): lightweight HTML chat UI with a live Dynasty status bar.
- **OpenClaw Bridge** (`openclaw_bridge/bridge.py`): explicit allowlisted Skill RPC layer with job model + SSE events.
- **Dynasty v19**: clean bounded nonlinear reservoir (Duffing-style oscillator + slow memory `S4`) with quadratic readout + online LMS.
- **Plasticity**: optional, slow, bounded, *bidirectional* drift of oscillator damping `gamma` driven by EMA(RMS) trend.

## Quickstart (Windows)
Prereqs:
- Python on PATH (`python --version` works)
- Git (optional for running, required for publishing)
- `OPENAI_API_KEY` set in your environment (Hybrid uses OpenAI for the conversational layer)

### Setup (one command)
```bat
setup.cmd
```

### Start everything
```bat
start_all.cmd
```

This starts:
- Bridge on `http://127.0.0.1:17171`
- Hybrid on `http://127.0.0.1:8000`
- UI at `http://127.0.0.1:8000/ui/chat.html`

### Stop everything
```bat
stop_all.cmd
```

## Smoke test (in the UI chat box)
Presets:
```text
dynasty_preset {"name":"baseline"}
dynasty_preset {"name":"bidirectional-default"}
```

Run continuously (streams SSE metrics to the top bar):
```text
dynasty_step {"n":0,"emitEvery":500}
```

Stop:
```text
dynasty_cancel
```

Markers for annotation:
```text
dynasty_mark {"label":"run-start","note":"baseline"}
```

## Notes
- The UI is served by Hybrid at `/ui` to avoid CORS issues.
- Skills are explicit allowlist entries in `openclaw_bridge/skills_manifest.json`.
- If you edit the bridge manifest or bridge code, restart the bridge.

## License
MIT (see `LICENSE`).

mind combines two parallel projects into one modular architecture:

1) Dynasty v19 — a mathematically clean, bounded nonlinear reservoir (Duffing‑style oscillator + slow memory S4) with quadratic readout and online LMS.
2) Hybrid — a FastAPI conversational system that governs capability use via an explicit, allowlisted skill bridge (jobs + SSE).

You can run Dynasty continuously (n=0) and stream RMS/eRMS and γ/Δγ live in the UI, including optional slow bounded bidirectional plasticity over damping.

start_all.cmd
# open http://127.0.0.1:8000/ui/chat.html
dynasty_preset {"name":"bidirectional-default"}
dynasty_step {"n":0,"emitEvery":500}

dynasty_cancel
