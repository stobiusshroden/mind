import os
import json
import datetime
import asyncio
from typing import List, Dict, Any, Optional

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from openai import OpenAI

# ============================================================
# Configuration
# ============================================================

MODEL_NAME = "gpt-4.1-mini"   # Change to gpt-5.2 if desired
MEMORY_FILE = "memory.json"

OPENCLAW_BRIDGE = os.environ.get("OPENCLAW_BRIDGE_URL", "http://127.0.0.1:17171")

client = OpenAI()
app = FastAPI()

# Serve UI from the same origin (avoids CORS issues). Open: http://127.0.0.1:8000/ui/chat.html
_UI_DIR = os.path.dirname(os.path.abspath(__file__))
app.mount("/ui", StaticFiles(directory=_UI_DIR, html=False), name="ui")

# sessionId -> event queue (SSE)
_session_queues: Dict[str, "asyncio.Queue[dict]"] = {}

# sessionId -> background dynasty auto-run task
_dynasty_auto_tasks: Dict[str, asyncio.Task] = {}
_dynasty_auto_cfg: Dict[str, dict] = {}

# ============================================================
# CORS (Fixes your browser issue)
# ============================================================

app.add_middleware(
    CORSMiddleware,
    # Explicit origins (required when allow_credentials is True; '*' can cause browsers to reject)
    allow_origins=[
        "http://127.0.0.1:8080",
        "http://localhost:8080",
        "http://127.0.0.1:8000",
        "http://localhost:8000",
        "http://127.0.0.1",
        "http://localhost",
    ],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ============================================================
# Data Models
# ============================================================

class Message(BaseModel):
    role: str
    content: str

class ChatRequest(BaseModel):
    messages: List[Message]
    sessionId: Optional[str] = None

# ============================================================
# Memory System
# ============================================================

def load_memory():
    if not os.path.exists(MEMORY_FILE):
        return {
            "identity": {
                "values": [],
                "desired_model_traits": [
                    "pleasant",
                    "reflective",
                    "eager to grow",
                    "stable internal stance"
                ]
            },
            "episodic": [],
            "emotional_state": {
                "tone": "neutral",
                "engagement": "moderate",
                "energy": "moderate"
            }
        }

    with open(MEMORY_FILE, "r", encoding="utf-8") as f:
        return json.load(f)

def save_memory(memory):
    with open(MEMORY_FILE, "w", encoding="utf-8") as f:
        json.dump(memory, f, indent=2)

memory = load_memory()

# ============================================================
# Emotional Calibration
# ============================================================

def detect_user_state(text: str) -> Dict:

    text_lower = text.lower()

    state = {
        "tone": "neutral",
        "engagement": "moderate",
        "energy": "moderate"
    }

    if any(word in text_lower for word in ["why", "meaning", "growth", "reflect"]):
        state["tone"] = "reflective"
        state["engagement"] = "high"

    if any(word in text_lower for word in ["frustrated", "stuck", "annoyed"]):
        state["tone"] = "frustrated"
        state["energy"] = "high"

    if len(text.split()) < 4:
        state["energy"] = "low"

    return state

# ============================================================
# Memory Updating (Automatic)
# ============================================================

def update_memory(user_message: str, assistant_message: str):

    global memory

    # Update emotional state
    state = detect_user_state(user_message)
    memory["emotional_state"] = state

    # Create episodic summary
    summary = {
        "timestamp": str(datetime.datetime.now()),
        "summary": f"User said: '{user_message[:120]}'"
    }

    memory["episodic"].append(summary)

    # Keep only last 25 memories
    memory["episodic"] = memory["episodic"][-25:]

    save_memory(memory)

# ============================================================
# System Prompt Builder
# ============================================================

def build_system_prompt():

    identity = memory.get("identity", {})
    emotional_state = memory.get("emotional_state", {})

    return f"""
You are Hybrid Reservoir.

Core traits:
- Pleasant
- Reflective
- Eager to grow
- Stable internal stance
- Interpretive rather than reactive

User values: {identity.get("values", [])}
Desired traits: {identity.get("desired_model_traits", [])}

Current detected user state:
Tone: {emotional_state.get("tone")}
Engagement: {emotional_state.get("engagement")}
Energy: {emotional_state.get("energy")}

Respond in a way that:
- Maintains calm philosophical depth when appropriate
- Adjusts gently to user tone
- Builds continuity over time
- Feels centered and grounded
""".strip()

# ============================================================
# OpenClaw Bridge Tool Broker
# ============================================================

async def _sse_emit(session_id: str, event: str, data: Any) -> None:
    if not session_id:
        return
    q = _session_queues.get(session_id)
    if not q:
        return
    await q.put({"event": event, "data": data, "ts": datetime.datetime.utcnow().isoformat()})


async def _get_bridge_skills() -> List[dict]:
    async with httpx.AsyncClient(timeout=10.0) as hx:
        r = await hx.get(f"{OPENCLAW_BRIDGE}/skills")
        r.raise_for_status()
        payload = r.json()
        return payload.get("skills", [])


def _skills_to_openai_tools(skills: List[dict]) -> List[dict]:
    tools = []
    for s in skills:
        name = s.get("name")
        if not name:
            continue
        tools.append({
            "type": "function",
            "function": {
                "name": name,
                "description": s.get("description", ""),
                "parameters": s.get("argsSchema", {"type": "object", "properties": {}}),
            }
        })
    return tools


async def _bridge_invoke(skill: str, args: Dict[str, Any], session_id: str, call_id: Optional[str] = None) -> dict:
    call_id = call_id or os.urandom(8).hex()
    await _sse_emit(session_id, "tool", {"phase": "invoke", "skill": skill, "callId": call_id})

    async with httpx.AsyncClient(timeout=30.0) as hx:
        r = await hx.post(
            f"{OPENCLAW_BRIDGE}/skills/invoke",
            json={"callId": call_id, "skill": skill, "args": args, "context": {"sessionId": session_id}},
        )
        r.raise_for_status()
        inv = r.json()

    job_id = inv.get("jobId")
    if not job_id:
        raise HTTPException(status_code=502, detail="Bridge did not return jobId")

    await _sse_emit(session_id, "tool", {"phase": "started", "skill": skill, "jobId": job_id})

    # Forward bridge SSE into our session SSE in the background
    forward_task = asyncio.create_task(_forward_bridge_events(job_id, session_id))

    # Continuous dynasty mode: return immediately, keep SSE forwarding running.
    if skill == "dynasty_step" and int(args.get("n", 1) or 0) == 0:
        await _sse_emit(session_id, "tool", {
            "phase": "finished",
            "skill": skill,
            "jobId": job_id,
            "status": "running",
            "ok": True,
            "continuous": True,
        })
        return {"jobId": job_id, "status": "running", "continuous": True}

    # Otherwise, wait for completion (poll). This keeps the LLM loop simple.
    final = None
    try:
        async with httpx.AsyncClient(timeout=10.0) as hx:
            while True:
                jr = await hx.get(f"{OPENCLAW_BRIDGE}/jobs/{job_id}")
                jr.raise_for_status()
                final = jr.json()
                status = final.get("status")
                if status in ("succeeded", "failed", "canceled"):
                    break
                await asyncio.sleep(0.5)
    finally:
        forward_task.cancel()

    await _sse_emit(session_id, "tool", {
        "phase": "finished",
        "skill": skill,
        "jobId": job_id,
        "status": (final or {}).get("status"),
        "ok": (final or {}).get("ok"),
    })
    return final or {"jobId": job_id, "status": "unknown"}


async def _forward_bridge_events(job_id: str, session_id: str) -> None:
    # Bridge SSE: GET /jobs/{jobId}/events
    url = f"{OPENCLAW_BRIDGE}/jobs/{job_id}/events"
    try:
        async with httpx.AsyncClient(timeout=None) as hx:
            async with hx.stream("GET", url, headers={"Accept": "text/event-stream"}) as r:
                r.raise_for_status()
                current_event = "bridge"
                async for line in r.aiter_lines():
                    if not line:
                        continue
                    if line.startswith("event:"):
                        current_event = line[len("event:"):].strip() or "bridge"
                        continue
                    if line.startswith("data:"):
                        payload = line[len("data:"):].strip()
                        try:
                            data_obj = json.loads(payload)
                        except Exception:
                            data_obj = {"raw": payload}

                        # Bridge wraps as {event, data, ts}. Unwrap so the UI/LLM gets the useful payload directly.
                        out = data_obj
                        if isinstance(data_obj, dict) and ("data" in data_obj) and ("event" in data_obj):
                            out = data_obj.get("data")
                            if isinstance(out, dict):
                                out = dict(out)  # copy
                                out["_bridge_ts"] = data_obj.get("ts")
                                out["_bridge_event"] = data_obj.get("event")

                        if isinstance(out, dict):
                            out["jobId"] = job_id

                        await _sse_emit(session_id, current_event, out)
    except asyncio.CancelledError:
        return
    except Exception as e:
        await _sse_emit(session_id, "bridge", {"jobId": job_id, "error": str(e)})


# ============================================================
# Live Tool Events (SSE)
# ============================================================

@app.get("/v1/events")
async def events(sessionId: str):
    if not sessionId:
        raise HTTPException(status_code=400, detail="sessionId required")

    q = _session_queues.get(sessionId)
    if not q:
        q = asyncio.Queue()
        _session_queues[sessionId] = q

    async def gen():
        # Initial ping
        yield "event: ready\n" + f"data: {json.dumps({'sessionId': sessionId})}\n\n"
        while True:
            evt = await q.get()
            event = evt.get("event", "message")
            data = evt.get("data")
            yield f"event: {event}\n" + f"data: {json.dumps(data)}\n\n"

    return StreamingResponse(gen(), media_type="text/event-stream")


# ============================================================
# Chat Endpoint (with tool-calling)
# ============================================================

@app.post("/v1/chat/completions")
async def chat(request: ChatRequest):
    try:
        session_id = request.sessionId or ""

        system_prompt = build_system_prompt() + "\n\n" + (
            "You MAY call tools when it is materially helpful. "
            "Tools return structured JSON results. "
            "For long-running tools, progress is streamed as tool events; keep user-facing text concise. "
            "Dynasty tools: dynasty_status returns current reservoir state; dynasty_step advances it and streams 'metrics' events (E, RMS, q,p,S4; optional weights). "
            "dynasty_cancel cancels the current continuous run; it can be called with no args to cancel the latest run for this session."
        )

        # Base message list
        messages: List[Dict[str, Any]] = [{"role": "system", "content": system_prompt}]
        for msg in request.messages:
            messages.append({"role": msg.role, "content": msg.content})

        # Fetch tools from bridge
        skills = await _get_bridge_skills()
        tools = _skills_to_openai_tools(skills)

        # ------------------------------------------------------------
        # Direct command mode (deterministic)
        # ------------------------------------------------------------
        last_user = (request.messages[-1].content.strip() if request.messages else "")
        skill_names = {s.get("name") for s in skills if s.get("name")}

        # dynasty_auto toggle (not a bridge skill)
        if last_user.startswith("dynasty_auto"):
            arg_text = last_user[len("dynasty_auto"):].strip()
            args = {}
            if arg_text:
                try:
                    args = json.loads(arg_text)
                except Exception:
                    return {
                        "id": f"auto-{os.urandom(6).hex()}",
                        "object": "chat.completion",
                        "choices": [{"index": 0, "message": {"role": "assistant", "content": "[dynasty_auto] parse error: expected JSON after dynasty_auto"}, "finish_reason": "stop"}],
                    }

            enabled = args.get("enabled")
            if enabled is None and args == {}:
                # status
                running = session_id in _dynasty_auto_tasks and not _dynasty_auto_tasks[session_id].done()
                cfg = _dynasty_auto_cfg.get(session_id)
                return {
                    "id": f"auto-{os.urandom(6).hex()}",
                    "object": "chat.completion",
                    "choices": [{"index": 0, "message": {"role": "assistant", "content": json.dumps({"enabled": running, "config": cfg}, indent=2)}, "finish_reason": "stop"}],
                }

            if enabled is False:
                t = _dynasty_auto_tasks.get(session_id)
                if t and not t.done():
                    t.cancel()
                _dynasty_auto_tasks.pop(session_id, None)
                await _sse_emit(session_id, "mark", {"label": "dynasty_auto:off"})
                return {
                    "id": f"auto-{os.urandom(6).hex()}",
                    "object": "chat.completion",
                    "choices": [{"index": 0, "message": {"role": "assistant", "content": "[dynasty_auto] disabled"}, "finish_reason": "stop"}],
                }

            if enabled is True:
                interval = int(args.get("intervalSec", 900))
                steps = int(args.get("steps", 200000))
                emit_every = int(args.get("emitEvery", steps))
                # keep it sane
                interval = max(10, min(interval, 86400))
                steps = max(1, min(steps, 5_000_000))
                emit_every = max(1, min(emit_every, steps))

                _dynasty_auto_cfg[session_id] = {"intervalSec": interval, "steps": steps, "emitEvery": emit_every}

                # stop existing
                t = _dynasty_auto_tasks.get(session_id)
                if t and not t.done():
                    t.cancel()

                async def auto_loop():
                    await _sse_emit(session_id, "mark", {"label": "dynasty_auto:on", "note": _dynasty_auto_cfg[session_id]})
                    while True:
                        try:
                            # bounded run, no export
                            await _bridge_invoke("dynasty_step", {"n": steps, "emitEvery": emit_every, "includeWeights": False}, session_id)
                        except Exception as e:
                            await _sse_emit(session_id, "bridge", {"error": f"dynasty_auto tick failed: {e!r}"})
                        await asyncio.sleep(interval)

                _dynasty_auto_tasks[session_id] = asyncio.create_task(auto_loop())
                return {
                    "id": f"auto-{os.urandom(6).hex()}",
                    "object": "chat.completion",
                    "choices": [{"index": 0, "message": {"role": "assistant", "content": json.dumps({"enabled": True, "config": _dynasty_auto_cfg[session_id]}, indent=2)}, "finish_reason": "stop"}],
                }

        if last_user:
            head, *rest = last_user.split(" ", 1)
            if head in skill_names:
                arg_text = rest[0].strip() if rest else ""
                if arg_text:
                    try:
                        args = json.loads(arg_text)
                    except Exception as e:
                        assistant_reply = f"[tool] parse error for {head}: expected JSON object after skill name"
                        return {
                            "id": f"tool-{os.urandom(6).hex()}",
                            "object": "chat.completion",
                            "choices": [
                                {
                                    "index": 0,
                                    "message": {"role": "assistant", "content": assistant_reply + "\n" + f"input={arg_text}"},
                                    "finish_reason": "stop",
                                }
                            ],
                        }
                else:
                    args = {}

                tool_result = await _bridge_invoke(head, args, session_id)
                assistant_reply = f"[tool] invoked {head}"
                update_memory(last_user, assistant_reply)

                return {
                    "id": f"tool-{os.urandom(6).hex()}",
                    "object": "chat.completion",
                    "choices": [
                        {
                            "index": 0,
                            "message": {"role": "assistant", "content": assistant_reply + "\n" + json.dumps(tool_result)},
                            "finish_reason": "stop",
                        }
                    ],
                }

        # Tool loop
        max_tool_rounds = 5
        last_assistant_text = ""
        response = None

        for _round in range(max_tool_rounds):
            response = client.chat.completions.create(
                model=MODEL_NAME,
                messages=messages,
                tools=tools if tools else None,
            )

            msg = response.choices[0].message
            assistant_text = msg.content or ""
            last_assistant_text = assistant_text

            tool_calls = getattr(msg, "tool_calls", None)
            if tool_calls:
                messages.append({
                    "role": "assistant",
                    "content": assistant_text,
                    "tool_calls": [tc.model_dump() if hasattr(tc, "model_dump") else tc for tc in tool_calls],
                })

                for tc in tool_calls:
                    tc_id = getattr(tc, "id", None) or (tc.get("id") if isinstance(tc, dict) else None)
                    fn = getattr(tc, "function", None) or (tc.get("function") if isinstance(tc, dict) else None)
                    name = getattr(fn, "name", None) if fn else None
                    arguments = getattr(fn, "arguments", None) if fn else None

                    if isinstance(fn, dict):
                        name = fn.get("name")
                        arguments = fn.get("arguments")

                    if not name:
                        continue

                    try:
                        args = json.loads(arguments) if arguments else {}
                    except Exception:
                        args = {}

                    tool_result = await _bridge_invoke(name, args, session_id, call_id=tc_id)
                    messages.append({"role": "tool", "tool_call_id": tc_id, "content": json.dumps(tool_result)})

                continue

            if assistant_text:
                messages.append({"role": "assistant", "content": assistant_text})
            break

        last_user_message = request.messages[-1].content if request.messages else ""
        update_memory(last_user_message, last_assistant_text)

        return {
            "id": (response.id if response else f"chat-{os.urandom(6).hex()}"),
            "object": "chat.completion",
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": last_assistant_text},
                    "finish_reason": "stop",
                }
            ],
        }
    except Exception as e:
        import traceback
        tb = traceback.format_exc()
        raise HTTPException(status_code=500, detail={"error": repr(e), "trace": tb[-4000:]})

# ============================================================
# Health Check
# ============================================================

@app.get("/")
def root():
    return {"status": "Hybrid Reservoir v20 running"}


@app.get("/version")
def version():
    return {
        "name": "hybrid_reservoir",
        "ui": "/ui/chat.html",
        "bridge": OPENCLAW_BRIDGE,
        "model": MODEL_NAME,
        "time": str(datetime.datetime.utcnow()),
    }