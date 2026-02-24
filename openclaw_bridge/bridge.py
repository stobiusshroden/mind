import asyncio
import json
import os
import re
import signal
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional
from uuid import uuid4

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from jsonschema import validate as js_validate
from jsonschema.exceptions import ValidationError
from sse_starlette.sse import EventSourceResponse


APP_NAME = "openclaw-bridge"
DEFAULT_PORT = 17171

# Adjust if your OpenClaw workspace is elsewhere
DEFAULT_OPENCLAW_WORKSPACE = r"C:\\Users\\shrod\\.openclaw\\workspace"
DEFAULT_DYNASTY_FILE = "manifold-swarm-dynasty-v19-clean.html"

MANIFEST_PATH = Path(__file__).with_name("skills_manifest.json")
ARTIFACTS_DIR = Path(__file__).with_name("artifacts")


class SkillInvokeRequest(BaseModel):
    callId: str = Field(default_factory=lambda: str(uuid4()))
    skill: str
    args: Dict[str, Any] = Field(default_factory=dict)
    context: Dict[str, Any] = Field(default_factory=dict)


class SkillInvokeResponse(BaseModel):
    callId: str
    jobId: str
    ok: bool
    status: Literal["queued", "running", "succeeded", "failed", "canceled"]


@dataclass
class Job:
    jobId: str
    callId: str
    skill: str
    args: Dict[str, Any]
    context: Dict[str, Any] = field(default_factory=dict)
    status: str = "queued"
    createdAt: float = field(default_factory=time.time)
    startedAt: Optional[float] = None
    endedAt: Optional[float] = None
    returncode: Optional[int] = None
    error: Optional[str] = None
    result: Optional[Dict[str, Any]] = None
    artifactsDir: str = ""
    _proc: Optional[asyncio.subprocess.Process] = None
    _events: "asyncio.Queue[dict]" = field(default_factory=asyncio.Queue)


app = FastAPI(title=APP_NAME)

# If your Hybrid UI is served from elsewhere, add it here. Keep narrow.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://127.0.0.1", "http://localhost", "http://127.0.0.1:8000", "http://localhost:8000"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


def _load_manifest() -> dict:
    if not MANIFEST_PATH.exists():
        raise RuntimeError(f"Missing manifest: {MANIFEST_PATH}")
    return json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))


_manifest = _load_manifest()
_skill_index = {s["name"]: s for s in _manifest.get("skills", [])}
_jobs: Dict[str, Job] = {}

# dynasty v19 state is stored per sessionId (from invoke.context.sessionId)
_dynasty_sessions: Dict[str, "DynastyState"] = {}
# sessionId -> last started continuous dynasty_step jobId
_dynasty_last_job: Dict[str, str] = {}


def _public_skill_view(s: dict) -> dict:
    return {
        "name": s.get("name"),
        "description": s.get("description", ""),
        "argsSchema": s.get("argsSchema", {"type": "object"}),
        "kind": s.get("kind", "command"),
    }


def _validate_args(skill: dict, args: dict) -> None:
    schema = skill.get("argsSchema") or {"type": "object"}
    try:
        js_validate(instance=args, schema=schema)
    except ValidationError as e:
        raise HTTPException(status_code=400, detail=f"argsSchema validation failed: {e.message}")


def _resolve_dynasty_path(args: dict) -> str:
    file_base = args.get("file") or DEFAULT_DYNASTY_FILE
    # Basename only, to prevent path traversal.
    if ("/" in file_base) or ("\\" in file_base) or (":" in file_base):
        raise HTTPException(status_code=400, detail="'file' must be a basename only")
    p = Path(DEFAULT_OPENCLAW_WORKSPACE) / file_base
    return str(p)


# ============================================================
# Dynasty v19 internal simulator (matches manifold-swarm-dynasty-v19-clean.html)
# ============================================================

class DynastyState:
    def __init__(self):
        # params
        self.dt = 0.05
        self.omega = 0.35
        self.beta = 0.05
        self.gamma = 0.0  # damping
        self.eta = 5e-8
        self.delay = 10
        self.learning = True

        # very slow plasticity over gamma (optional)
        self.plastic_enabled = False
        self.plastic_mu = 1e-6
        self.plastic_ema_alpha = 0.002
        self.plastic_eps = 0.0
        self.plastic_relax = 0.5
        self.plastic_E_high = 1e9
        self.gamma_min = 0.0
        self.gamma_max = 0.2
        self._ema_rms: Optional[float] = None
        self._prev_ema_rms: Optional[float] = None
        self._prev_E: Optional[float] = None

        # state
        self.t = 0  # integer step index
        self.q = 0.1
        self.p = 0.0
        self.S4 = 0.0
        self.w = [0.0] * 9

        # rolling RMS
        self.rmsWindow = 2000
        self._errBuf: List[float] = []
        self._errSumSq = 0.0

    def _input(self, tt: int) -> float:
        return float(__import__("math").sin(self.omega * tt))

    def _energy(self) -> float:
        return 0.5 * self.p * self.p + 0.5 * (self.omega * self.omega) * (self.q * self.q)

    def _update_rms(self, e: float) -> float:
        ee = e if (e == e and abs(e) != float("inf")) else 0.0
        self._errBuf.append(ee)
        self._errSumSq += ee * ee
        if len(self._errBuf) > self.rmsWindow:
            old = self._errBuf.pop(0)
            self._errSumSq -= old * old
        n = len(self._errBuf)
        if n <= 0:
            return 0.0
        import math
        rms = math.sqrt(max(0.0, self._errSumSq / n))
        return float(rms) if (rms == rms and abs(rms) != float("inf")) else 0.0

    def reset(self, seed: Optional[int] = None):
        import random
        rng = random.Random(seed)
        self.t = 0
        self.q = 0.1
        self.p = 0.0
        self.S4 = 0.0
        self.w = [(rng.random() * 0.02 - 0.01) for _ in range(9)]
        self._errBuf = []
        self._errSumSq = 0.0
        self._ema_rms = None
        self._prev_ema_rms = None
        self._prev_E = None

        # prime RMS with initial error like JS does
        u0 = self._input(self.t)
        target0 = 0.0
        qq = self.q * self.q
        pp = self.p * self.p
        phi0 = [self.q, self.p, self.S4, qq, pp, self.S4 * self.S4, self.q * self.p, self.q * self.S4, self.p * self.S4]
        y0 = sum(self.w[i] * phi0[i] for i in range(9))
        err0 = target0 - y0
        rms0 = self._update_rms(err0)
        return {"u": u0, "target": target0, "y": y0, "err": err0, "rms": rms0}

    def step_once(self) -> Dict[str, Any]:
        # Mirrors JS stepOnce
        u = self._input(self.t)

        omega2 = self.omega * self.omega
        # Duffing-style oscillator with optional linear damping -gamma*p
        self.p = self.p + self.dt * (-(omega2 * self.q) - self.beta * self.q * self.q * self.q - self.gamma * self.p) + self.beta * u
        self.q = self.q + self.dt * self.p

        qq = self.q * self.q
        self.S4 = self.S4 + self.dt * (qq - 0.1 * self.S4)

        target = 0.0
        if self.t > self.delay:
            target = self._input(self.t - self.delay)

        pp = self.p * self.p
        phi = [self.q, self.p, self.S4, qq, pp, self.S4 * self.S4, self.q * self.p, self.q * self.S4, self.p * self.S4]
        y = sum(self.w[i] * phi[i] for i in range(9))
        err = target - y

        if self.learning and self.t > self.delay:
            for i in range(9):
                self.w[i] += self.eta * err * phi[i]

        rms = self._update_rms(err)
        E = self._energy()

        # ---- slow plasticity over damping gamma ----
        dgamma = 0.0
        ema_rms = self._ema_rms
        if ema_rms is None:
            ema_rms = rms
        prev_ema = self._ema_rms
        self._ema_rms = (1.0 - self.plastic_ema_alpha) * ema_rms + self.plastic_ema_alpha * rms
        self._prev_ema_rms = prev_ema

        if self.plastic_enabled:
            dR = (self._ema_rms - prev_ema) if (prev_ema is not None) else 0.0

            # Bidirectional rule primarily driven by RMS trend.
            # - if EMA(RMS) worsens -> increase damping
            # - if EMA(RMS) improves -> decrease damping (scaled by plastic_relax)
            # Energy is a safety override only.
            if abs(dR) <= self.plastic_eps:
                dgamma = 0.0
            elif dR > 0.0:
                dgamma = self.plastic_mu * dR
            else:
                dgamma = -self.plastic_relax * self.plastic_mu * (-dR)

            # Safety: if energy is too high, bias towards more damping
            if E > self.plastic_E_high:
                dgamma = abs(dgamma) if dgamma != 0.0 else self.plastic_mu

            self.gamma = max(self.gamma_min, min(self.gamma_max, self.gamma + dgamma))

        self._prev_E = E
        self.t += 1

        return {
            "t": self.t,
            "u": u,
            "target": target,
            "y": y,
            "err": err,
            "rms": rms,
            "ema_rms": self._ema_rms,
            "q": self.q,
            "p": self.p,
            "S4": self.S4,
            "E": E,
            "gamma": self.gamma,
            "dgamma": dgamma,
        }

    def snapshot(self, include_weights: bool = True) -> Dict[str, Any]:
        snap = {
            "t": self.t,
            "q": self.q,
            "p": self.p,
            "S4": self.S4,
            "E": self._energy(),
            "learning": self.learning,
            "dt": self.dt,
            "omega": self.omega,
            "beta": self.beta,
            "gamma": self.gamma,
            "eta": self.eta,
            "delay": self.delay,
            "rms": self._update_rms(0.0) if self._errBuf else 0.0,
            "ema_rms": self._ema_rms,
            "plasticity": {
                "enabled": self.plastic_enabled,
                "mu": self.plastic_mu,
                "emaAlpha": self.plastic_ema_alpha,
                "eps": self.plastic_eps,
                "relax": self.plastic_relax,
                "EHigh": self.plastic_E_high,
                "gammaMin": self.gamma_min,
                "gammaMax": self.gamma_max,
            }
        }
        if include_weights:
            snap["w"] = list(self.w)
        return snap


def _dynasty_get(session_id: str) -> DynastyState:
    if not session_id:
        session_id = "default"
    st = _dynasty_sessions.get(session_id)
    if not st:
        st = DynastyState()
        st.reset(seed=None)
        _dynasty_sessions[session_id] = st
    return st


TEMPLATE_RE = re.compile(r"\{([a-zA-Z0-9_]+)\}")


def _render_command(template: List[str], args: Dict[str, Any], extra: Dict[str, str]) -> List[str]:
    def render_piece(piece: str) -> str:
        def repl(m: re.Match) -> str:
            k = m.group(1)
            if k in extra:
                return str(extra[k])
            if k in args:
                return str(args[k])
            raise HTTPException(status_code=400, detail=f"Missing template arg: {k}")

        out = TEMPLATE_RE.sub(repl, piece)
        # If braces remain, it means unresolved templates.
        if "{" in out or "}" in out:
            raise HTTPException(status_code=400, detail="Unresolved template in runner command")
        return out

    return [render_piece(p) for p in template]


async def _emit(job: Job, event: str, data: Any) -> None:
    await job._events.put({"event": event, "data": data, "ts": time.time()})


async def _pump_stream(job: Job, stream: asyncio.StreamReader, which: str, log_path: Path) -> None:
    with log_path.open("ab") as f:
        while True:
            chunk = await stream.readline()
            if not chunk:
                break
            f.write(chunk)
            f.flush()
            try:
                text = chunk.decode("utf-8", errors="replace").rstrip("\n")
            except Exception:
                text = repr(chunk)
            await _emit(job, which, text)


async def _finalize_process(job: Job) -> None:
    assert job._proc is not None
    rc = await job._proc.wait()
    job.returncode = rc
    job.endedAt = time.time()

    if job.status == "canceled":
        await _emit(job, "lifecycle", {"status": job.status, "returncode": rc})
        return

    if rc == 0:
        job.status = "succeeded"
        job.result = job.result or {"message": "ok"}
    else:
        job.status = "failed"
        job.error = job.error or f"Process exited with code {rc}"

    await _emit(job, "lifecycle", {"status": job.status, "returncode": rc, "error": job.error})


async def _run_internal(job: Job, skill: dict) -> None:
    job.status = "running"
    job.startedAt = time.time()
    await _emit(job, "lifecycle", {"status": job.status})

    session_id = str((job.context or {}).get("sessionId") or "default")

    def apply_dynasty_overrides(st: DynastyState, args: Dict[str, Any]) -> None:
        # optional param overrides
        if "dt" in args: st.dt = float(args["dt"])
        if "omega" in args: st.omega = float(args["omega"])
        if "beta" in args: st.beta = float(args["beta"])
        if "gamma" in args: st.gamma = float(args["gamma"])
        if "eta" in args: st.eta = float(args["eta"])
        if "delay" in args: st.delay = int(args["delay"])
        if "learning" in args: st.learning = bool(args["learning"])

        plast = args.get("plasticity")
        if isinstance(plast, dict):
            if "enabled" in plast: st.plastic_enabled = bool(plast["enabled"])
            if "mu" in plast: st.plastic_mu = float(plast["mu"])
            if "emaAlpha" in plast: st.plastic_ema_alpha = float(plast["emaAlpha"])
            if "eps" in plast: st.plastic_eps = float(plast["eps"])
            if "relax" in plast: st.plastic_relax = float(plast["relax"])
            if "EHigh" in plast: st.plastic_E_high = float(plast["EHigh"])
            if "gammaMin" in plast: st.gamma_min = float(plast["gammaMin"])
            if "gammaMax" in plast: st.gamma_max = float(plast["gammaMax"])
            if st.gamma_max < st.gamma_min:
                st.gamma_max, st.gamma_min = st.gamma_min, st.gamma_max
            st.gamma = max(st.gamma_min, min(st.gamma_max, st.gamma))

    if job.skill == "dynasty_status":
        st = _dynasty_get(session_id)
        include_w = bool(job.args.get("includeWeights", True))
        job.result = {"sessionId": session_id, "state": st.snapshot(include_weights=include_w)}
        job.status = "succeeded"
        job.endedAt = time.time()
        await _emit(job, "lifecycle", {"status": job.status})
        return

    if job.skill == "dynasty_export_run":
        st = _dynasty_get(session_id)
        apply_dynasty_overrides(st, job.args)

        if bool(job.args.get("reset", False)):
            st.reset(seed=job.args.get("seed"))
            await _emit(job, "mark", {"sessionId": session_id, "label": "export:reset", "note": "reset before export", "t": st.t})

        steps = int(job.args.get("steps", 200000))
        sample_every = int(job.args.get("sampleEvery", 200))
        include_w = bool(job.args.get("includeWeights", True))

        artifacts_dir = Path(job.artifactsDir)
        csv_path = artifacts_dir / "dynasty_export.csv"
        json_path = artifacts_dir / "dynasty_export.json"

        rows: List[Dict[str, Any]] = []
        first = None
        last = None

        for i in range(steps):
            if job.status == "canceled":
                break
            last = st.step_once()
            if first is None:
                first = dict(last)
            if (i % sample_every) == 0 or i == (steps - 1):
                rec = {
                    "i": i,
                    "t": last.get("t"),
                    "E": last.get("E"),
                    "rms": last.get("rms"),
                    "ema_rms": last.get("ema_rms"),
                    "gamma": last.get("gamma"),
                    "dgamma": last.get("dgamma"),
                    "q": last.get("q"),
                    "p": last.get("p"),
                    "S4": last.get("S4"),
                    "u": last.get("u"),
                    "target": last.get("target"),
                    "y": last.get("y"),
                    "err": last.get("err"),
                }
                if include_w:
                    rec["w"] = list(st.w)
                rows.append(rec)

            if (i % 50000) == 0:
                await _emit(job, "metrics", {"sessionId": session_id, "kind": "export_progress", "i": i, "steps": steps, "metrics": last, "w": list(st.w) if include_w else None})
                await asyncio.sleep(0)

        # Write CSV
        import csv
        fieldnames = ["i","t","E","rms","ema_rms","gamma","dgamma","q","p","S4","u","target","y","err"]
        with csv_path.open("w", newline="", encoding="utf-8") as f:
            wcsv = csv.DictWriter(f, fieldnames=fieldnames)
            wcsv.writeheader()
            for r in rows:
                wcsv.writerow({k: r.get(k) for k in fieldnames})

        # Write JSON (includes weights)
        json_payload = {
            "sessionId": session_id,
            "export": {
                "steps": steps,
                "sampleEvery": sample_every,
                "includeWeights": include_w,
                "startedAt": job.startedAt,
                "endedAt": time.time(),
            },
            "params": {
                "dt": st.dt,
                "omega": st.omega,
                "beta": st.beta,
                "gamma": st.gamma,
                "eta": st.eta,
                "delay": st.delay,
                "learning": st.learning,
                "plasticity": {
                    "enabled": st.plastic_enabled,
                    "mu": st.plastic_mu,
                    "emaAlpha": st.plastic_ema_alpha,
                    "eps": st.plastic_eps,
                    "relax": st.plastic_relax,
                    "EHigh": st.plastic_E_high,
                    "gammaMin": st.gamma_min,
                    "gammaMax": st.gamma_max,
                },
            },
            "first": first,
            "last": last,
            "rows": rows,
        }
        json_path.write_text(json.dumps(json_payload), encoding="utf-8")

        job.result = {
            "sessionId": session_id,
            "ok": True,
            "csv": str(csv_path),
            "json": str(json_path),
            "samples": len(rows),
            "first": first,
            "last": last,
        }
        job.status = "succeeded" if job.status != "canceled" else "canceled"
        job.endedAt = time.time()
        await _emit(job, "lifecycle", {"status": job.status})
        return

    if job.skill == "dynasty_cancel":
        target = str(job.args.get("jobId") or "").strip()
        if not target:
            target = _dynasty_last_job.get(session_id, "")

        if not target:
            job.status = "failed"
            job.error = "No jobId provided and no prior continuous dynasty job recorded for this session"
            job.endedAt = time.time()
            await _emit(job, "lifecycle", {"status": job.status, "error": job.error})
            return

        tgt = _jobs.get(target)
        if not tgt:
            job.status = "failed"
            job.error = "Unknown jobId"
            job.endedAt = time.time()
            await _emit(job, "lifecycle", {"status": job.status, "error": job.error, "jobId": target})
            return

        # Only allow canceling dynasty_step jobs (defense-in-depth)
        if tgt.skill != "dynasty_step":
            job.status = "failed"
            job.error = "Refusing to cancel non-dynasty_step job"
            job.endedAt = time.time()
            await _emit(job, "lifecycle", {"status": job.status, "error": job.error, "jobId": target})
            return

        if tgt.status in ("succeeded", "failed", "canceled"):
            job.result = {"jobId": target, "status": tgt.status, "ok": True}
        else:
            tgt.status = "canceled"
            tgt.endedAt = time.time()
            await _emit(tgt, "lifecycle", {"status": tgt.status})
            job.result = {"jobId": target, "status": "canceled", "ok": True}

        job.status = "succeeded"
        job.endedAt = time.time()
        await _emit(job, "lifecycle", {"status": job.status})
        return

    if job.skill == "dynasty_preset":
        name = str(job.args.get("name") or "").strip()
        apply = bool(job.args.get("apply", True))
        st = _dynasty_get(session_id)

        presets = {
            "baseline": {
                "plasticity": {"enabled": False},
                "gamma": 0.0,
            },
            "bidirectional-default": {
                "plasticity": {
                    "enabled": True,
                    "mu": 1e-4,
                    "emaAlpha": 0.01,
                    "eps": 1e-5,
                    "relax": 1.2,
                    "gammaMin": 0.0,
                    "gammaMax": 0.05,
                    "EHigh": 1e9,
                }
            },
        }

        if name not in presets:
            job.status = "failed"
            job.error = f"Unknown preset: {name}"
            job.endedAt = time.time()
            await _emit(job, "lifecycle", {"status": job.status, "error": job.error})
            return

        preset = presets[name]

        if apply:
            if "gamma" in preset:
                st.gamma = float(preset["gamma"])

            plast = preset.get("plasticity")
            if isinstance(plast, dict):
                if "enabled" in plast: st.plastic_enabled = bool(plast["enabled"])
                if "mu" in plast: st.plastic_mu = float(plast["mu"])
                if "emaAlpha" in plast: st.plastic_ema_alpha = float(plast["emaAlpha"])
                if "eps" in plast: st.plastic_eps = float(plast["eps"])
                if "relax" in plast: st.plastic_relax = float(plast["relax"])
                if "EHigh" in plast: st.plastic_E_high = float(plast["EHigh"])
                if "gammaMin" in plast: st.gamma_min = float(plast["gammaMin"])
                if "gammaMax" in plast: st.gamma_max = float(plast["gammaMax"])
                if st.gamma_max < st.gamma_min:
                    st.gamma_max, st.gamma_min = st.gamma_min, st.gamma_max
                st.gamma = max(st.gamma_min, min(st.gamma_max, st.gamma))

        await _emit(job, "mark", {"sessionId": session_id, "label": f"preset:{name}", "note": "applied" if apply else "preview", "t": st.t})
        job.result = {"sessionId": session_id, "name": name, "apply": apply, "preset": preset, "state": st.snapshot(include_weights=False)}
        job.status = "succeeded"
        job.endedAt = time.time()
        await _emit(job, "lifecycle", {"status": job.status})
        return

    if job.skill == "dynasty_mark":
        label = str(job.args.get("label") or "").strip()
        note = str(job.args.get("note") or "").strip()
        if not label:
            job.status = "failed"
            job.error = "label required"
            job.endedAt = time.time()
            await _emit(job, "lifecycle", {"status": job.status, "error": job.error})
            return

        await _emit(job, "mark", {"sessionId": session_id, "label": label, "note": note, "t": _dynasty_get(session_id).t})
        job.result = {"sessionId": session_id, "label": label, "note": note}
        job.status = "succeeded"
        job.endedAt = time.time()
        await _emit(job, "lifecycle", {"status": job.status})
        return

    if job.skill == "dynasty_step":
        st = _dynasty_get(session_id)
        apply_dynasty_overrides(st, job.args)

        if bool(job.args.get("reset", False)):
            st.reset(seed=job.args.get("seed"))
            await _emit(job, "metrics", {"sessionId": session_id, "kind": "reset", "state": st.snapshot(include_weights=False)})

        n = int(job.args.get("n", 1))
        emit_every = int(job.args.get("emitEvery", 50))
        include_w = bool(job.args.get("includeWeights", False))

        # step loop (stream metrics)
        last = None
        i = 0

        async def emit_metrics(i_now: int, n_total: Optional[int], last_metrics: Dict[str, Any]):
            payload = {
                "sessionId": session_id,
                "kind": "step",
                "i": i_now,
                "n": n_total,
                "metrics": last_metrics,
            }
            if include_w:
                payload["w"] = list(st.w)
            await _emit(job, "metrics", payload)

        if n == 0:
            # Continuous mode: run until canceled
            while True:
                if job.status == "canceled":
                    job.endedAt = time.time()
                    job.result = {
                        "sessionId": session_id,
                        "final": last,
                        "state": st.snapshot(include_weights=include_w),
                        "continuous": True,
                    }
                    # lifecycle for cancel is emitted by cancel endpoint; don't override.
                    return

                last = st.step_once()
                if (i % emit_every) == 0:
                    await emit_metrics(i, None, last)

                i += 1
                if (i % 2000) == 0:
                    await asyncio.sleep(0)
        else:
            for i in range(n):
                if job.status == "canceled":
                    job.endedAt = time.time()
                    job.result = {
                        "sessionId": session_id,
                        "final": last,
                        "state": st.snapshot(include_weights=include_w),
                        "continuous": False,
                    }
                    return

                last = st.step_once()
                if (i % emit_every) == 0 or i == (n - 1):
                    await emit_metrics(i, n, last)
                if (i % 2000) == 0:
                    await asyncio.sleep(0)

        job.result = {
            "sessionId": session_id,
            "final": last,
            "state": st.snapshot(include_weights=include_w),
            "continuous": False,
        }
        job.status = "succeeded"
        job.endedAt = time.time()
        await _emit(job, "lifecycle", {"status": job.status})
        return

    job.status = "failed"
    job.error = f"Unknown internal skill: {job.skill}"
    job.endedAt = time.time()
    await _emit(job, "lifecycle", {"status": job.status, "error": job.error})


async def _run_command(job: Job, skill: dict) -> None:
    runner = skill.get("runner") or {}
    template = runner.get("command")
    if not isinstance(template, list) or not template:
        raise HTTPException(status_code=500, detail="Invalid runner.command")

    timeout_sec = int(runner.get("timeoutSec") or 0)

    artifacts_dir = Path(job.artifactsDir)
    stdout_log = artifacts_dir / "stdout.log"
    stderr_log = artifacts_dir / "stderr.log"

    extra = {}
    if job.skill in ("dynasty_run",):
        extra["path"] = _resolve_dynasty_path(job.args)

    cmd = _render_command(template, job.args, extra)

    job.status = "running"
    job.startedAt = time.time()
    await _emit(job, "lifecycle", {"status": job.status, "cmd": cmd})

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=runner.get("cwd") or None,
        env=None,
    )
    job._proc = proc

    # Start stream pumps
    assert proc.stdout is not None and proc.stderr is not None
    t1 = asyncio.create_task(_pump_stream(job, proc.stdout, "stdout", stdout_log))
    t2 = asyncio.create_task(_pump_stream(job, proc.stderr, "stderr", stderr_log))

    try:
        if timeout_sec > 0:
            await asyncio.wait_for(_finalize_process(job), timeout=timeout_sec)
        else:
            await _finalize_process(job)
    except asyncio.TimeoutError:
        job.status = "failed"
        job.error = f"Timeout after {timeout_sec}s"
        try:
            proc.terminate()
        except Exception:
            pass
        job.endedAt = time.time()
        await _emit(job, "lifecycle", {"status": job.status, "error": job.error})
    finally:
        t1.cancel()
        t2.cancel()


@app.get("/health")
def health() -> dict:
    return {"ok": True, "name": APP_NAME}


@app.get("/version")
def version() -> dict:
    return {
        "name": APP_NAME,
        "manifestVersion": _manifest.get("version", 1),
    }


@app.get("/skills")
def list_skills() -> dict:
    return {
        "version": _manifest.get("version", 1),
        "skills": [_public_skill_view(s) for s in _manifest.get("skills", [])],
    }


@app.post("/skills/invoke", response_model=SkillInvokeResponse)
async def invoke(req: SkillInvokeRequest) -> SkillInvokeResponse:
    skill = _skill_index.get(req.skill)
    if not skill:
        raise HTTPException(status_code=404, detail="Unknown skill")

    _validate_args(skill, req.args)

    jobId = str(uuid4())
    artifacts_dir = ARTIFACTS_DIR / jobId
    artifacts_dir.mkdir(parents=True, exist_ok=True)

    job = Job(
        jobId=jobId,
        callId=req.callId,
        skill=req.skill,
        args=req.args,
        context=req.context or {},
        artifactsDir=str(artifacts_dir),
    )
    _jobs[jobId] = job

    kind = skill.get("kind", "command")
    if kind == "internal":
        # Record last continuous dynasty job for this session (used by dynasty_cancel fallback)
        try:
            session_id = str((req.context or {}).get("sessionId") or "default")
            if req.skill == "dynasty_step" and int((req.args or {}).get("n", 1) or 0) == 0:
                _dynasty_last_job[session_id] = jobId
        except Exception:
            pass
        asyncio.create_task(_run_internal(job, skill))
    elif kind == "command":
        asyncio.create_task(_run_command(job, skill))
    else:
        job.status = "failed"
        job.error = f"Unsupported skill kind: {kind}"
        job.endedAt = time.time()
        await _emit(job, "lifecycle", {"status": job.status, "error": job.error})

    return SkillInvokeResponse(callId=req.callId, jobId=jobId, ok=True, status=job.status)  # type: ignore


@app.get("/jobs/{jobId}")
def get_job(jobId: str) -> dict:
    job = _jobs.get(jobId)
    if not job:
        raise HTTPException(status_code=404, detail="Unknown jobId")

    return {
        "jobId": job.jobId,
        "callId": job.callId,
        "skill": job.skill,
        "args": job.args,
        "status": job.status,
        "createdAt": job.createdAt,
        "startedAt": job.startedAt,
        "endedAt": job.endedAt,
        "returncode": job.returncode,
        "ok": job.status == "succeeded",
        "result": job.result,
        "error": job.error,
        "artifactsDir": job.artifactsDir,
        "artifacts": {
            "stdout": str(Path(job.artifactsDir) / "stdout.log"),
            "stderr": str(Path(job.artifactsDir) / "stderr.log"),
        },
    }


@app.get("/jobs/{jobId}/events")
async def job_events(jobId: str):
    job = _jobs.get(jobId)
    if not job:
        raise HTTPException(status_code=404, detail="Unknown jobId")

    async def gen():
        # Initial snapshot
        yield {"event": "lifecycle", "data": {"status": job.status, "jobId": job.jobId}}
        while True:
            evt = await job._events.get()
            yield {"event": evt.get("event", "message"), "data": json.dumps(evt)}
            if evt.get("event") == "lifecycle":
                data = evt.get("data") or {}
                if data.get("status") in ("succeeded", "failed", "canceled"):
                    break

    return EventSourceResponse(gen())


@app.post("/jobs/{jobId}/cancel")
async def cancel_job(jobId: str) -> dict:
    job = _jobs.get(jobId)
    if not job:
        raise HTTPException(status_code=404, detail="Unknown jobId")

    if job.status in ("succeeded", "failed", "canceled"):
        return {"ok": True, "status": job.status}

    job.status = "canceled"
    job.endedAt = time.time()

    if job._proc is not None:
        try:
            job._proc.terminate()
        except Exception:
            pass

    await _emit(job, "lifecycle", {"status": job.status})
    return {"ok": True, "status": job.status}


if __name__ == "__main__":
    import uvicorn

    port = int(os.environ.get("OPENCLAW_BRIDGE_PORT", str(DEFAULT_PORT)))
    uvicorn.run("bridge:app", host="127.0.0.1", port=port, reload=False)
