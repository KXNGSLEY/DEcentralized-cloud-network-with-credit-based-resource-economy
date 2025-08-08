# supernet_server.py
import asyncio
import sys
import uuid
import os
import tempfile
import random
import logging
from typing import Dict, Any
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from pydantic import BaseModel
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

registered_hosts = []
jobs: Dict[str, Dict[str, Any]] = {}

# -----------------
# Models
# -----------------
class CodeInput(BaseModel):
    code: str

class HostInfo(BaseModel):
    host: str

# -----------------
# Helpers
# -----------------
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
# Coordinator endpoints
# -----------------
@app.post("/register_host")
def register_host(host: HostInfo):
    if host.host not in registered_hosts:
        registered_hosts.append(host.host)
        logger.info(f"Registered new host: {host.host}")
    return {"message": f"Host {host.host} registered successfully.", "hosts": registered_hosts}

@app.post("/submit")
async def submit_code(payload: CodeInput):
    job_id = str(uuid.uuid4())
    logger.info(f"Received new code submission, assigned job_id: {job_id}")
    logger.debug(f"Code:\n{payload.code}")
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
        logger.info(f"Launched subprocess for job {job_id}, PID: {process.pid}")

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
            "returncode": None
        }

        return {"message": "Job started", "job_id": job_id, "pid": process.pid}
    except Exception as e:
        logger.error(f"Failed to start job: {e}")
        return {"error": str(e)}

# -----------------
# Host endpoints
# -----------------
@app.post("/stop/{job_id}")
async def stop_job(job_id: str):
    meta = jobs.get(job_id)
    if not meta:
        return {"error": "Unknown job_id"}
    proc = meta["proc"]
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
    proc = meta["proc"]
    running = (proc.returncode is None)
    logger.debug(f"Status check for job {job_id}: running={running}")
    return {"job_id": job_id, "running": running, "pid": proc.pid, "returncode": meta.get("returncode")}

# -----------------
# WebSocket for live logs
# -----------------
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
                proc = meta["proc"]
                if proc.returncode is not None:
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

                       ══► SUPER𝙉𝙀𝙏 v1.0 - Coordinator + Host ◄══                 
                            ░░░ CREATED BY NONSKING2215 ░░░                            
    """)

    logger.info("Routes: POST /submit -> returns job_id, POST /stop/{job_id}, GET /status/{job_id}, ws /ws/logs/{job_id}")
    uvicorn.run(app, host="0.0.0.0", port=9000, log_level="debug")

# -----------------
# Main
# -----------------
if __name__ == "__main__":
    run_coord_server()
