# supernet_server.py
import asyncio
import sys
import uuid
import os
import tempfile
import random
import logging
from typing import Dict, Any, Optional
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from pydantic import BaseModel
from datetime import datetime, timedelta
import requests
import uvicorn

# -----------------
# Logging Setup
# -----------------
logging.basicConfig(
    level=logging.DEBUG,
    format="[{asctime}] [{levelname}] {message}",
    style="{"
)
logger = logging.getLogger("supernet")

# -----------------
# CONFIG
# -----------------
COORDINATOR_URL = "http://YOUR_SERVER_IP:9000"

app = FastAPI()

# Keep registry + job store
registered_hosts = []
jobs: Dict[str, Dict[str, Any]] = {}  # stores running jobs (local/executing) and metadata

# SuperCredits setup
SUPER_CREDIT_REWARD_HOURLY = 500
SUPER_CREDIT_START = 700
SUPER_CREDIT_USAGE_COST = 10
SUPER_CREDIT_OVERAGE_COST = 1000
RAM_CAP = 4  # GB
CPU_CAP = 2  # Threads

# Tracks host metadata: credits, last_ping, uptime remainder
host_data: Dict[str, Dict[str, Any]] = {}  # host -> {credits, last_ping, total_uptime_seconds, queue}
# Simple user account store: user_id -> credits
USER_START_CREDITS = 100
user_data: Dict[str, Dict[str, Any]] = {}

# Job queue per host (host -> list of job dicts)
host_job_queue: Dict[str, asyncio.Queue] = {}

# Active host timeout (seconds) - consider host inactive if last_ping older than this
HOST_ACTIVE_TIMEOUT = 120

# Create a special name for the supernode (the server itself)
SUPERNODE_NAME = "supernode"

# -----------------
# Models
# -----------------
class CodeInput(BaseModel):
    code: str
    ram: int = 4
    cpu: int = 2
    host: Optional[str] = ""  # optional: if not provided, coordinator will pick
    user_id: Optional[str] = ""  # optional: who's submitting (non-host users allowed)

class HostInfo(BaseModel):
    host: str

class JobDone(BaseModel):
    job_id: str
    returncode: int
    logs: Optional[str] = ""  # optional aggregated logs

# -----------------
# Helpers
# -----------------
def _ensure_host_queue(host: str):
    if host not in host_job_queue:
        host_job_queue[host] = asyncio.Queue()

def _is_host_active(host: str) -> bool:
    info = host_data.get(host)
    if not info:
        return False
    last = info.get("last_ping")
    if not last:
        return False
    return (datetime.utcnow() - last).total_seconds() <= HOST_ACTIVE_TIMEOUT

async def _read_stream(stream, queue: asyncio.Queue, stream_name: str, job_id: str):
    try:
        while True:
            line = await stream.readline()
            if not line:
                break
            text = line.decode(errors="replace").rstrip("\n")
            logger.debug(f"[{stream_name.upper()}][{job_id}] {text}")
            await queue.put({"stream": stream_name, "line": text})
    except asyncio.CancelledError:
        logger.debug(f"Stream reader for {stream_name} on job {job_id} was cancelled.")
    except Exception as e:
        await queue.put({"stream": stream_name, "line": f"<read error: {e}>"})
        logger.error(f"Error reading {stream_name} stream for job {job_id}: {e}")
    finally:
        await queue.put({"stream": stream_name, "line": None})
        logger.debug(f"Stream {stream_name} closed for job {job_id}.")

async def _wait_and_finalize(process, job_id: str):
    returncode = await process.wait()
    meta = jobs.get(job_id)
    if meta:
        await meta["stdout_q"].put({"stream": "meta", "line": f"PROCESS_EXIT:{returncode}"})
        await meta["stderr_q"].put({"stream": "meta", "line": f"PROCESS_EXIT:{returncode}"})
        meta["returncode"] = returncode
        logger.info(f"Job {job_id} completed with return code {returncode}.")

# -----------------
# Initialize supernode entry
# -----------------
def init_supernode():
    if SUPERNODE_NAME not in registered_hosts:
        registered_hosts.append(SUPERNODE_NAME)
    host_data[SUPERNODE_NAME] = {
        "credits": 10**9,  # supernode has effectively unlimited credits (or set to high number)
        "last_ping": datetime.utcnow(),
        "total_uptime_seconds": 0
    }
    _ensure_host_queue(SUPERNODE_NAME)
    logger.info("Supernode initialized and available as host: %s", SUPERNODE_NAME)

init_supernode()

# -----------------
# Coordinator endpoints
# -----------------
@app.post("/register_host")
def register_host(host: HostInfo):
    """
    Register or refresh a host. Any hostname sent here will be accepted.
    If the host already exists we refresh its last_ping so it's immediately active.
    """
    name = host.host.strip()
    if not name:
        return {"error": "Empty host name"}

    # register new host or refresh existing host's last_ping
    if name not in host_data:
        registered_hosts.append(name)
        host_data[name] = {
            "credits": SUPER_CREDIT_START,
            "last_ping": datetime.utcnow(),
            "total_uptime_seconds": 0
        }
        _ensure_host_queue(name)
        logger.info(f"Registered host: {name} with {SUPER_CREDIT_START} SuperCredits.")
    else:
        # refresh last_ping so newly-registered/returned hosts are immediately active
        host_data[name]["last_ping"] = datetime.utcnow()
        logger.info(f"Refreshed host registration: {name} (marked active).")

    return {
        "message": f"Host {name} registered successfully.",
        "hosts": registered_hosts,
        "credits": host_data[name]["credits"]
    }

@app.get("/hosts")
def list_hosts():
    """
    Return list of known hosts and whether they're active,
    their credits and uptime remainder.
    """
    out = []
    for h in registered_hosts:
        info = host_data.get(h, {})
        last = info.get("last_ping")
        last_delta = None
        if last:
            last_delta = (datetime.utcnow() - last).total_seconds()
        out.append({
            "host": h,
            "credits": info.get("credits"),
            "last_ping_seconds_ago": last_delta,
            "active": _is_host_active(h)
        })
    return {"hosts": out}

@app.post("/host_ping")
def host_ping(host: HostInfo):
    """
    Hosts should ping regularly (e.g., every 60s). The server accumulates uptime
    and rewards hourly credits.
    """
    if host.host not in host_data:
        return {"error": "Host not registered"}

    now = datetime.utcnow()
    info = host_data[host.host]
    delta = (now - info["last_ping"]).total_seconds()
    info["total_uptime_seconds"] += delta
    info["last_ping"] = now

    # Reward credits every full hour
    hours = int(info["total_uptime_seconds"] // 3600)
    if hours > 0:
        reward = hours * SUPER_CREDIT_REWARD_HOURLY
        info["credits"] += reward
        info["total_uptime_seconds"] %= 3600  # keep remainder
        logger.info(f"{host.host} earned {reward} SuperCredits for {hours}h uptime.")

    return {"credits": info["credits"], "uptime_secs_remainder": info["total_uptime_seconds"]}

@app.get("/get_job/{host_id}")
async def get_job_for_host(host_id: str):
    """
    Called by worker/host clients to fetch the next job assigned to them.
    This returns a job payload or an empty response if no jobs queued.
    """
    if host_id not in registered_hosts:
        return {"error": "Unknown host"}
    _ensure_host_queue(host_id)
    q = host_job_queue[host_id]
    try:
        job = q.get_nowait()
        logger.info(f"Dispatching job {job['job_id']} to host {host_id}")
        return {"job": job}
    except asyncio.QueueEmpty:
        return {"job": None}

@app.post("/job_done/{job_id}")
def job_done(job_id: str, payload: JobDone):
    """
    Worker calls this when job completes on their machine.
    payload: { job_id, returncode, logs (optional) }
    """
    meta = jobs.get(job_id)
    if not meta:
        # still accept the done report and record minimal metadata
        jobs[job_id] = {
            "proc": None,
            "stdout_q": asyncio.Queue(),
            "stderr_q": asyncio.Queue(),
            "task_stdout": None,
            "task_stderr": None,
            "task_wait": None,
            "job_file": None,
            "returncode": payload.returncode,
            "host": None,
            "status": "completed"
        }
        logger.info(f"Received job_done for unknown job {job_id} (registered minimal metadata).")
    else:
        meta["returncode"] = payload.returncode
        meta["status"] = "completed"

    logger.info(f"Job {job_id} finished with returncode {payload.returncode}.")
    if payload.logs:
        # store a short log snapshot
        jobs[job_id]["remote_logs"] = payload.logs
    return {"message": "acknowledged", "job_id": job_id}

# -----------------
# Submit endpoint (updated behavior)
# -----------------
@app.post("/submit")
async def submit_code(payload: CodeInput):
    """
    Submits a job. Behavior:
    - If payload.host is provided and that host is active -> use it.
    - Otherwise pick randomly among currently connected active non-supernode hosts.
    - If no non-supernode hosts are active, use SUPERNODE_NAME.
    - New users get USER_START_CREDITS (already preserved).
    """
    user_id = payload.user_id.strip() if payload.user_id else "anonymous"
    if user_id not in user_data:
        user_data[user_id] = {"credits": USER_START_CREDITS}
        logger.info(f"Created user account '{user_id}' with {USER_START_CREDITS} starting credits.")

    # build active non-supernode hosts list from host_data
    active_non_supernode = [
        h for h, info in host_data.items()
        if h != SUPERNODE_NAME and _is_host_active(h)
    ]

    # determine chosen host
    specified = payload.host.strip() if payload.host else None
    if specified:
        # if user explicitly chose supernode, allow that explicitly
        if specified == SUPERNODE_NAME:
            chosen_host = SUPERNODE_NAME
        elif specified in host_data and _is_host_active(specified):
            chosen_host = specified
        else:
            # explicit host not active / not known -> fall back to automatic selection
            chosen_host = random.choice(active_non_supernode) if active_non_supernode else SUPERNODE_NAME
    else:
        chosen_host = random.choice(active_non_supernode) if active_non_supernode else SUPERNODE_NAME

    logger.info(f"Job submission: user={user_id} chosen_host={chosen_host} (active_hosts={len(active_non_supernode)})")

    # compute cost and overage
    overage = False
    if payload.ram > RAM_CAP or payload.cpu > CPU_CAP:
        overage = True

    cost = SUPER_CREDIT_USAGE_COST + (SUPER_CREDIT_OVERAGE_COST if overage else 0)

    # ensure user has enough credits
    if user_data[user_id]["credits"] < cost:
        return {"error": "User does not have enough SuperCredits. Become a host to earn credits or top up."}

    # Deduct from user, credit to host (ensure host_data entry exists for chosen_host)
    user_data[user_id]["credits"] -= cost
    if chosen_host not in host_data:
        # should not normally happen because supernode is always present; defensive init
        host_data[chosen_host] = {
            "credits": SUPER_CREDIT_START,
            "last_ping": datetime.utcnow(),
            "total_uptime_seconds": 0
        }
        _ensure_host_queue(chosen_host)
        logger.info(f"Defensive-created host_data for {chosen_host}")

    host_data[chosen_host]["credits"] = host_data[chosen_host].get("credits", 0) + cost
    logger.info(f"User '{user_id}' charged {cost} credits for job; host '{chosen_host}' credited. "
                f"User remaining: {user_data[user_id]['credits']} Host credits: {host_data[chosen_host]['credits']}")

    # Create job metadata
    job_id = str(uuid.uuid4())
    logger.info(f"Received new code submission (user={user_id}), assigned job_id: {job_id}")
    logger.debug(f"Code:\n{payload.code}")

    # If chosen host is supernode -> run locally immediately
    if chosen_host == SUPERNODE_NAME:
        try:
            tmp = tempfile.NamedTemporaryFile(mode="w+", suffix=".py", delete=False)
            tmp.write(payload.code)
            tmp.flush()
            tmp.close()
            logger.debug(f"Code written to temp file: {tmp.name}")

            stdout_q: asyncio.Queue = asyncio.Queue()
            stderr_q: asyncio.Queue = asyncio.Queue()

            process = await asyncio.create_subprocess_exec(
                sys.executable, tmp.name,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            logger.info(f"Launched subprocess for job {job_id} on supernode, PID: {process.pid}")

            task_stdout = asyncio.create_task(_read_stream(process.stdout, stdout_q, "stdout", job_id))
            task_stderr = asyncio.create_task(_read_stream(process.stderr, stderr_q, "stderr", job_id))
            task_wait = asyncio.create_task(_wait_and_finalize(process, job_id))

            jobs[job_id] = {
                "proc": process,
                "stdout_q": stdout_q,
                "stderr_q": stderr_q,
                "task_stdout": task_stdout,
                "task_stderr": task_stderr,
                "task_wait": task_wait,
                "job_file": tmp.name,
                "returncode": None,
                "host": chosen_host,
                "user_id": user_id,
                "status": "running"
            }

            return {"message": "Job started on supernode", "job_id": job_id, "pid": process.pid, "charged": cost, "host": chosen_host}
        except Exception as e:
            logger.error(f"Failed to start local job: {e}")
            return {"error": str(e)}
    else:
        # assign job to host queue for remote worker to pick up
        job_payload = {
            "job_id": job_id,
            "code": payload.code,
            "ram": payload.ram,
            "cpu": payload.cpu,
            "user_id": user_id,
            "submitted_at": datetime.utcnow().isoformat()
        }
        _ensure_host_queue(chosen_host)
        await host_job_queue[chosen_host].put(job_payload)

        # register minimal metadata in jobs dict
        jobs[job_id] = {
            "proc": None,
            "stdout_q": asyncio.Queue(),
            "stderr_q": asyncio.Queue(),
            "task_stdout": None,
            "task_stderr": None,
            "task_wait": None,
            "job_file": None,
            "returncode": None,
            "host": chosen_host,
            "user_id": user_id,
            "status": "queued"
        }

        logger.info(f"Queued job {job_id} for host {chosen_host}.")
        return {"message": "Job queued for remote host", "job_id": job_id, "charged": cost, "host": chosen_host}


# -----------------
# Host endpoints (stop/status unchanged)
# -----------------
@app.post("/stop/{job_id}")
async def stop_job(job_id: str):
    meta = jobs.get(job_id)
    if not meta:
        return {"error": "Unknown job_id"}
    proc = meta.get("proc")
    if not proc:
        # cannot stop: remote worker handles stop
        return {"error": "Process not running on coordinator (remote host may be executing)"}
    if proc.returncode is not None:
        return {"message": "Process already exited", "returncode": proc.returncode}
    try:
        proc.terminate()
        logger.info(f"Sent terminate signal to job {job_id}")
        try:
            await asyncio.wait_for(proc.wait(), timeout=5)
        except asyncio.TimeoutError:
            proc.kill()
            logger.warning(f"Force killed job {job_id} after timeout")
        return {"message": f"Stop signal sent to job {job_id}"}
    except Exception as e:
        logger.error(f"Error stopping job {job_id}: {e}")
        return {"error": str(e)}

@app.get("/status/{job_id}")
async def status(job_id: str):
    meta = jobs.get(job_id)
    if not meta:
        return {"error": "Unknown job_id"}
    proc = meta.get("proc")
    running = (proc is not None and proc.returncode is None)
    return {
        "job_id": job_id,
        "running": running,
        "pid": proc.pid if proc else None,
        "returncode": meta.get("returncode"),
        "host": meta.get("host"),
        "status": meta.get("status")
    }

@app.websocket("/ws/logs/{job_id}")
async def websocket_logs(ws: WebSocket, job_id: str):
    await ws.accept()
    logger.info(f"WebSocket client connected for logs of job {job_id}")
    meta = jobs.get(job_id)
    if not meta:
        await ws.send_json({"error": "Unknown job_id"})
        await ws.close()
        logger.warning(f"WebSocket connection rejected: unknown job_id {job_id}")
        return

    stdout_q: asyncio.Queue = meta["stdout_q"]
    stderr_q: asyncio.Queue = meta["stderr_q"]

    async def forward(queue: asyncio.Queue):
        try:
            while True:
                item = await queue.get()
                await ws.send_json(item)
                if item.get("line") is None:
                    break
        except WebSocketDisconnect:
            logger.info(f"WebSocket disconnected for job {job_id}")
        except Exception as e:
            logger.error(f"WebSocket error for job {job_id}: {e}")

    f1 = asyncio.ensure_future(forward(stdout_q))
    f2 = asyncio.ensure_future(forward(stderr_q))

    try:
        while True:
            try:
                proc = meta.get("proc")
                if proc and proc.returncode is not None:
                    await asyncio.sleep(0.2)
                msg = await asyncio.wait_for(ws.receive_text(), timeout=5.0)
                if msg.strip().lower() == "close":
                    logger.info(f"WebSocket client requested close for job {job_id}")
                    break
            except asyncio.TimeoutError:
                if f1.done() and f2.done():
                    break
                continue
            except WebSocketDisconnect:
                break
    finally:
        for t in (f1, f2):
            if not t.done():
                t.cancel()
        try:
            await ws.close()
        except Exception:
            pass

@app.get("/credits/{host_id}")
def get_credits(host_id: str):
    if host_id not in host_data:
        return {"error": "Unknown host"}
    return {
        "host": host_id,
        "credits": host_data[host_id]["credits"],
        "uptime_seconds": host_data[host_id]["total_uptime_seconds"]
    }

@app.get("/user_credits/{user_id}")
def user_credits(user_id: str):
    if user_id not in user_data:
        return {"error": "Unknown user"}
    return {"user": user_id, "credits": user_data[user_id]["credits"]}

# -----------------
# CLI integration
# -----------------
def run_coord_server():
    logger.info(r"""
  ███████╗██╗   ██╗██████╗ ███████╗██████╗   ███╗   ██╗███████╗████████╗
  ██╔════╝██║   ██║██╔══██╗██╔════╝██╔══██╗  ████╗  ██║██╔════╝╚══██╔══╝
  ███████╗██║   ██║██████╔╝█████╗  ██████╔╝  ██╔██╗ ██║█████╗     ██║   
  ╚════██║██║   ██║██╔═══╝ ██╔══╝  ██╔══ ██║ ██║╚██╗██║██╔══╝     ██║   
  ███████║╚██████╔╝██║     ███████╗██║   ██║ ██║ ╚████║███████╗   ██║   
  ╚══════╝ ╚═════╝ ╚═╝     ╚══════╝╚═╝   ╚═╝ ╚═╝  ╚═══╝╚══════╝   ╚═╝   

                       ══► SUPER𝙉𝙀𝙏 v1.0 (in development) - Coordinator + Host ◄══                 
                            ░░░ CREATED BY KXNGSLEY ░░░                            
    """)

    logger.info("Routes: POST /submit -> returns job_id, POST /stop/{job_id}, GET /status/{job_id}, ws /ws/logs/{job_id}, GET /hosts, GET /get_job/{host_id}, POST /job_done/{job_id}")
    uvicorn.run(app, host="0.0.0.0", port=9000, log_level="debug")

# -----------------
# Main
# -----------------
if __name__ == "__main__":
    run_coord_server()
