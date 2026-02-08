"""
ui_client/main.py
FastAPI server for the RapidReach dashboard.

Provides:
  - HTML dashboard at / and /dashboard
  - WebSocket at /ws for real-time event streaming
  - /agent_callback endpoint for agents to POST status updates
  - /start_lead_finding to trigger Lead Finder
  - /start_sdr to trigger SDR Agent
  - /start_email_processing to trigger Lead Manager
  - /api/businesses, /api/events for frontend data
  - /api/human-input for human-in-the-loop feedback
"""

from __future__ import annotations

import asyncio
import json
import logging
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path

import httpx
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from dotenv import load_dotenv

from common.config import (
    LEAD_FINDER_SERVICE_URL,
    LEAD_MANAGER_SERVICE_URL,
    SDR_SERVICE_URL,
    UI_CLIENT_URL,
    UI_CLIENT_PORT,
)
from common.models import (
    AgentCallback,
    FindLeadsRequest,
    SDRRequest,
    ProcessEmailsRequest,
)

load_dotenv()
logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).parent
TEMPLATES_DIR = BASE_DIR / "templates"
STATIC_DIR = BASE_DIR / "static"

# ── In-memory stores ────────────────────────────────────────

connected_clients: list[WebSocket] = []
event_log: list[dict] = []
businesses: dict[str, dict] = {}
human_input_requests: dict[str, dict] = {}  # request_id → {prompt, response, resolved}


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info(f"UI Client starting at http://localhost:{UI_CLIENT_PORT}")
    yield
    logger.info("UI Client shutting down")


app = FastAPI(title="RapidReach Dashboard", lifespan=lifespan)

# Mount static files
STATIC_DIR.mkdir(parents=True, exist_ok=True)
(STATIC_DIR / "css").mkdir(parents=True, exist_ok=True)
(STATIC_DIR / "js").mkdir(parents=True, exist_ok=True)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


# ── WebSocket broadcasting ───────────────────────────────────

async def broadcast(event: dict):
    """Send an event to all connected WebSocket clients."""
    dead = []
    for ws in connected_clients:
        try:
            await ws.send_json(event)
        except Exception:
            dead.append(ws)
    for ws in dead:
        connected_clients.remove(ws)


# ── WebSocket endpoint ──────────────────────────────────────

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    connected_clients.append(websocket)
    logger.info(f"WebSocket client connected ({len(connected_clients)} total)")

    # Send current state on connect
    try:
        await websocket.send_json({
            "type": "init",
            "businesses": list(businesses.values()),
            "recent_events": event_log[-50:],
        })
    except Exception:
        pass

    # Start heartbeat task to prevent timeouts
    async def heartbeat():
        while websocket in connected_clients:
            try:
                await asyncio.sleep(60)  # Send heartbeat every 60 seconds
                await websocket.send_json({"type": "heartbeat", "timestamp": datetime.utcnow().isoformat()})
            except Exception:
                break
    
    heartbeat_task = asyncio.create_task(heartbeat())

    try:
        while True:
            # Keep connection alive with 30-minute timeout, receive any client messages
            try:
                data = await asyncio.wait_for(websocket.receive_text(), timeout=1800)  # 30 minutes
                # Client can send pings or commands
                try:
                    msg = json.loads(data)
                    if msg.get("type") == "ping":
                        await websocket.send_json({"type": "pong"})
                    elif msg.get("type") == "heartbeat":
                        await websocket.send_json({"type": "heartbeat_ack"})
                except json.JSONDecodeError:
                    pass
            except asyncio.TimeoutError:
                # Send keepalive on timeout
                await websocket.send_json({"type": "keepalive"})
    except WebSocketDisconnect:
        heartbeat_task.cancel()
        if websocket in connected_clients:
            connected_clients.remove(websocket)
        logger.info(f"WebSocket client disconnected ({len(connected_clients)} total)")


# ── Agent Callback endpoint ─────────────────────────────────

@app.post("/agent_callback")
async def agent_callback(callback: AgentCallback):
    """
    Receive status updates from any agent service.
    Stores the event and broadcasts to all WebSocket clients.
    """
    event = callback.model_dump()
    event_log.append(event)

    # If it's a lead_found event, store the business
    if callback.event == "lead_found" and callback.data:
        bid = callback.business_id or callback.data.get("place_id", "")
        if bid:
            businesses[bid] = callback.data

    # If search_completed, store all leads
    if callback.event == "search_completed" and callback.data:
        for lead in callback.data.get("leads", []):
            pid = lead.get("place_id", "")
            if pid:
                businesses[pid] = lead

    # Broadcast to all connected WebSocket clients
    await broadcast({
        "type": "agent_event",
        **event,
    })

    return {"status": "received"}


# ── Workflow triggers ────────────────────────────────────────

@app.post("/start_lead_finding")
async def start_lead_finding(req: FindLeadsRequest):
    """Trigger Lead Finder service for a city."""
    req.callback_url = req.callback_url or f"{UI_CLIENT_URL}/agent_callback"

    await broadcast({
        "type": "agent_event",
        "agent_type": "lead_finder",
        "event": "user_started",
        "message": f"Starting lead search in {req.city}...",
        "timestamp": datetime.utcnow().isoformat(),
    })

    try:
        async with httpx.AsyncClient(timeout=120) as client:
            resp = await client.post(
                f"{LEAD_FINDER_SERVICE_URL}/find_leads",
                json=req.model_dump(),
            )
            return resp.json()
    except Exception as e:
        error_msg = f"Lead Finder unavailable: {str(e)}"
        await broadcast({
            "type": "agent_event",
            "agent_type": "lead_finder",
            "event": "error",
            "message": error_msg,
            "timestamp": datetime.utcnow().isoformat(),
        })
        return {"status": "error", "message": error_msg}


@app.post("/start_sdr")
async def start_sdr(req: SDRRequest):
    """Trigger SDR Agent for a business."""
    req.callback_url = req.callback_url or f"{UI_CLIENT_URL}/agent_callback"

    await broadcast({
        "type": "agent_event",
        "agent_type": "sdr",
        "event": "user_started",
        "message": f"Starting SDR outreach for {req.business_name}...",
        "timestamp": datetime.utcnow().isoformat(),
    })

    try:
        async with httpx.AsyncClient(timeout=300) as client:
            resp = await client.post(
                f"{SDR_SERVICE_URL}/run_sdr",
                json=req.model_dump(),
            )
            return resp.json()
    except Exception as e:
        return {"status": "error", "message": str(e)}


@app.post("/start_email_processing")
async def start_email_processing(req: ProcessEmailsRequest):
    """Trigger Lead Manager to process inbox."""
    req.callback_url = req.callback_url or f"{UI_CLIENT_URL}/agent_callback"

    try:
        async with httpx.AsyncClient(timeout=120) as client:
            resp = await client.post(
                f"{LEAD_MANAGER_SERVICE_URL}/process_emails",
                json=req.model_dump(),
            )
            return resp.json()
    except Exception as e:
        return {"status": "error", "message": str(e)}


# ── Data APIs ────────────────────────────────────────────────

@app.get("/api/businesses")
async def get_businesses():
    return {"businesses": list(businesses.values()), "total": len(businesses)}


@app.get("/api/events")
async def get_events(limit: int = 50):
    return {"events": event_log[-limit:], "total": len(event_log)}


@app.get("/api/sdr_sessions")
async def get_sdr_sessions():
    """Fetch SDR session data from SDR service."""
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(f"{SDR_SERVICE_URL}/api/sessions")
            return resp.json()
    except Exception as e:
        logger.error(f"Failed to fetch SDR sessions: {e}")
        return {"sessions": {}, "error": str(e)}


@app.get("/api/meetings")
async def get_meetings():
    """Fetch meetings data from Lead Manager service."""
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(f"{LEAD_MANAGER_SERVICE_URL}/api/meetings")
            return resp.json()
    except Exception as e:
        logger.error(f"Failed to fetch meetings: {e}")
        return {"meetings": [], "error": str(e)}


# ── Human-in-the-loop ───────────────────────────────────────

@app.post("/api/human-input/request")
async def request_human_input(data: dict):
    """Agent requests human feedback."""
    request_id = data.get("request_id", str(len(human_input_requests)))
    human_input_requests[request_id] = {
        "prompt": data.get("prompt", ""),
        "context": data.get("context", {}),
        "response": None,
        "resolved": False,
    }
    await broadcast({
        "type": "human_input_request",
        "request_id": request_id,
        "prompt": data.get("prompt", ""),
        "context": data.get("context", {}),
    })
    return {"request_id": request_id}


@app.post("/api/human-input/respond")
async def respond_human_input(data: dict):
    """Human provides feedback."""
    request_id = data.get("request_id", "")
    if request_id in human_input_requests:
        human_input_requests[request_id]["response"] = data.get("response", "")
        human_input_requests[request_id]["resolved"] = True
        return {"status": "received", "request_id": request_id}
    return {"status": "not_found"}


@app.get("/api/human-input/{request_id}")
async def get_human_input(request_id: str):
    """Agent polls for human response."""
    req = human_input_requests.get(request_id, {})
    return req


# ── Health check ─────────────────────────────────────────────

@app.get("/health")
async def health():
    return {
        "status": "ok",
        "service": "ui_client",
        "connected_clients": len(connected_clients),
        "businesses_count": len(businesses),
        "events_count": len(event_log),
        "timestamp": datetime.utcnow().isoformat(),
    }


# ── HTML Pages ───────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse("dashboard.html", {"request": request})


@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard(request: Request):
    return templates.TemplateResponse("dashboard.html", {"request": request})
