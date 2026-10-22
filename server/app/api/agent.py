"""One accepted task, short authenticated status reads, no automatic POST replay."""
import asyncio
import hashlib
import json
import re
import time
import uuid

from fastapi.responses import JSONResponse, StreamingResponse

from app.agent_bridge import (AgentRequestError, ASYNC_WAIT_SECONDS, complete_agent,
                              prepare_agent_job, read_agent_job, resume_agent_job)
from app.conversation import build_standard_transcript
from app.model_registry import is_supported_model

# Deployment uses one ASGI worker. Per-account durable bridge locks remain authoritative.
_tasks = {}


def _account(client):
    return hashlib.sha256(f"{client.space_id}:{client.user_id}".encode()).hexdigest()


def _error(exc):
    return JSONResponse(status_code=exc.status_code, content={"error": {
        "code": exc.code, "type": "notion_agent_error", "message": str(exc), "retryable": False}},
        headers={"Cache-Control": "no-store"})


def _completed(model, content, job_id=None):
    return {"id": "chatcmpl-" + (job_id or uuid.uuid4().hex), "object": "chat.completion", "created": int(time.time()),
            **({'notion_job_id': job_id} if job_id else {}),
            "model": model, "choices": [{"index": 0, "message": {"role": "assistant", "content": content}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}}


def _completed_job(model, job, job_id):
    error = job.get('terminal_error')
    if error:
        return _error(AgentRequestError(error['code'], error['message'], error['status']))
    return JSONResponse(content=_completed(model, job['reply'], job_id), headers={'Cache-Control': 'no-store'})


def _pending(job_id):
    return JSONResponse(status_code=202, content={"object": "notion.agent.job", "job_id": job_id,
                        "status": "running", "poll_after_ms": 3000}, headers={"Cache-Control": "no-store"})


def _schedule(client, job_id, model, run):
    async def work():
        try:
            return _completed(model, await asyncio.to_thread(run), job_id)
        except AgentRequestError as exc:
            return _error(exc)
        except Exception:
            return _error(AgentRequestError("AGENT_UNKNOWN", "Task state is unknown. Its retained ID must be checked before resubmission.", 500))
    entry = {"job_id": job_id, "model": model, "task": asyncio.create_task(work())}
    _tasks[_account(client)] = entry
    return entry


async def handle_agent_request(request, body):
    if not is_supported_model(body.model):
        return _error(AgentRequestError("UNSUPPORTED_MODEL", "Unsupported model.", 400))
    pool = request.app.state.account_pool
    scope = request.headers.get("x-deepseek-harness-session-id", "") or body.conversation_id or ""
    local_protocol = request.headers.get('x-notion-local-protocol') == 'dsh-local-tools-v2'
    messages = [msg.model_dump() for msg in body.messages]
    affinity = scope or json.dumps([body.model, messages], sort_keys=True)
    client = pool.clients[int(hashlib.sha256(affinity.encode()).hexdigest(), 16) % len(pool.clients)]
    transcript = build_standard_transcript(messages, body.model, {"user_id": client.user_id, "space_id": client.space_id})

    if request.headers.get("x-notion-async") == "1":
        identity = [scope, body.model, messages] + (['dsh-local-tools-v2'] if local_protocol else [])
        job_id = hashlib.sha256(json.dumps(identity, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        entry = _tasks.get(_account(client))
        if entry and entry["job_id"] == job_id and not entry["task"].done():
            return _pending(job_id)
        job = read_agent_job(client, job_id)
        if job and job["phase"] == "done":
            return _completed_job(body.model, job, job_id)
        if entry and not entry["task"].done():
            if entry["job_id"] != job_id:
                return _error(AgentRequestError("AGENT_BUSY", "Notion already has a running task; no new task was submitted."))
            return _pending(job_id)
        try:
            # This short local claim is durable before acknowledging acceptance.
            # It never polls the network or waits for an account lock.
            job = prepare_agent_job(client, transcript, scope, job_id, local_protocol=local_protocol)
        except AgentRequestError as exc:
            return _error(exc)
        if job["phase"] == "done":
            return _completed_job(body.model, job, job_id)
        _schedule(client, job_id, body.model,
                  lambda: complete_agent(client, transcript, scope=scope, job_id=job_id, wait_seconds=ASYNC_WAIT_SECONDS, local_protocol=local_protocol))
        return _pending(job_id)

    try:
        # Legacy clients receive a controlled timeout before the unchanged 300s proxy limit.
        content = await asyncio.to_thread(complete_agent, client, transcript, scope, local_protocol=local_protocol)
    except AgentRequestError as exc:
        return _error(exc)
    response = _completed(body.model, content)
    if not body.stream:
        return JSONResponse(content=response)

    async def chunks():
        response["object"] = "chat.completion.chunk"
        response["choices"] = [{"index": 0, "delta": {"role": "assistant", "content": content}, "finish_reason": None}]
        yield "data: " + json.dumps(response, ensure_ascii=False) + "\n\n"
        response["choices"] = [{"index": 0, "delta": {}, "finish_reason": "stop"}]
        yield "data: " + json.dumps(response) + "\n\ndata: [DONE]\n\n"
    return StreamingResponse(chunks(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


async def get_agent_job(request, job_id):
    if not re.fullmatch(r"[a-f0-9]{64}", job_id):
        return _error(AgentRequestError("AGENT_NOT_FOUND", "Retained task not found; do not submit a replacement automatically.", 404))
    for client in request.app.state.account_pool.clients:
        entry = _tasks.get(_account(client))
        if entry and entry["job_id"] == job_id and not entry["task"].done():
            return _pending(job_id)
        job = read_agent_job(client, job_id)
        if job and job["phase"] == "done":
            return _completed_job(job.get("model", "notion-agent"), job, job_id)
        if entry and entry["job_id"] == job_id:
            if not entry["task"].done():
                return _pending(job_id)
            result = entry["task"].result()
            if isinstance(result, JSONResponse):
                # Report a stopped worker once. A later explicit lookup may
                # resume only the retained queued/pending task after a timeout
                # or cloud-side action was resolved; it cannot replay pending.
                if not job or not entry.get('error_reported'):
                    entry['error_reported'] = True
                    return result
            # Success lives in the bounded durable result cache, not indefinitely
            # in the most recent account task after the cache has expired.
            if not job:
                continue
        if not job:
            continue
        model = job.get("model", "notion-agent")
        if entry and not entry["task"].done():
            return _error(AgentRequestError("AGENT_BUSY", "The retained task is being checked; no new task was submitted."))
        _schedule(client, job_id, model, lambda: resume_agent_job(client, job_id))
        return _pending(job_id)
    return _error(AgentRequestError("AGENT_NOT_FOUND", "Retained task not found; do not submit a replacement automatically.", 404))
