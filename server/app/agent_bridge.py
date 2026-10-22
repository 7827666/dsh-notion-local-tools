"""Bounded, single-flight Notion agent bridge. Never replay an ambiguous POST."""
import hashlib
import json
import os
from pathlib import Path
import threading
import time
import uuid

import requests

from app.logger import logger
from app.notion_client import NotionUpstreamError
from app.agent_protocol import (parse_bridge_request, continuation_text,
                                classify_snapshot, validate_local_reply)

AGENT_MODEL = os.getenv("NOTION_AGENT_MODEL", "achira-donut")
NOTION_ORIGIN = os.getenv("NOTION_URL", "https://app.notion.com").rstrip("/")
CLIENT_VERSION = os.getenv("NOTION_CLIENT_VERSION", "23.13.20261015.0608")
USER_AGENT = os.getenv("NOTION_USER_AGENT", "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/155.0.0.0 Safari/537.36")
WAIT_SECONDS = min(240.0, float(os.getenv("NOTION_AGENT_WAIT_SECONDS", "240")))
ASYNC_WAIT_SECONDS = float(os.getenv("NOTION_AGENT_ASYNC_WAIT_SECONDS", "1800"))
COMPLETED_CACHE_SECONDS = 24 * 60 * 60
POLL_SECONDS = float(os.getenv("NOTION_AGENT_POLL_SECONDS", "3"))
STATE_DIR = Path(os.getenv("NOTION_AGENT_STATE_DIR", str(Path(__file__).resolve().parent.parent / "data" / "agent-jobs")))
_locks = {}
_locks_guard = threading.Lock()


class AgentRequestError(NotionUpstreamError):
    def __init__(self, code, message, status=409):
        super().__init__(message, status_code=status, retriable=False)
        self.code = code


def _cookie_header(client):
    jar = dict(getattr(client, "cookies", {}) or {})
    if getattr(client, "token_v2", ""):
        jar["token_v2"] = client.token_v2
    if getattr(client, "user_id", ""):
        jar["notion_user_id"] = client.user_id
    return "; ".join(f"{k}={v}" for k, v in jar.items() if v)


def _headers(client):
    return {"content-type": "application/json", "accept": "application/json",
            "cookie": _cookie_header(client), "origin": NOTION_ORIGIN,
            "referer": f"{NOTION_ORIGIN}/chat", "user-agent": USER_AGENT,
            "notion-client-version": CLIENT_VERSION,
            "x-notion-space-id": client.space_id, "x-notion-active-user-header": client.user_id}


def _notion_model(transcript):
    for block in transcript or []:
        if isinstance(block, dict) and block.get("type") == "config":
            model = (block.get("value") or {}).get("model")
            if isinstance(model, str) and model.strip():
                return model.strip()
    return AGENT_MODEL


def _user_text(transcript):
    parts = []
    for block in transcript or []:
        if not isinstance(block, dict) or block.get("type") != "user":
            continue
        value = block.get("value")
        if isinstance(value, list) and value and isinstance(value[0], list) and value[0]:
            parts.append(str(value[0][0]))
        elif isinstance(value, str):
            parts.append(value)
    return "\n\n".join(p for p in parts if p).strip()


def _assistant_texts(node, found):
    if isinstance(node, dict):
        if node.get("kind") == "assistant_message":
            content = node.get("content")
            if isinstance(content, list):
                text = "".join(item.get("text", "") for item in content
                               if isinstance(item, dict) and item.get("type") == "text" and isinstance(item.get("text"), str))
                if text.strip():
                    found.append(text)
            elif isinstance(content, str) and content.strip():
                found.append(content)
        for value in node.values():
            _assistant_texts(value, found)
    elif isinstance(node, list):
        for value in node:
            _assistant_texts(value, found)


def _completed(data):
    # Presence of assistant text alone does not mean the run has finished.
    return isinstance(data, dict) and isinstance(data.get("session"), dict) and data["session"].get("status") == "idle" and data.get("session_pending") is False


def _reply(data):
    found = []
    _assistant_texts(data, found)
    return found[-1].strip() if found else ""


def _save(path, job):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temp = path.with_suffix(".tmp")
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as stream:
        json.dump(job, stream, ensure_ascii=False)
        stream.flush()
        os.fsync(stream.fileno())
    temp.replace(path)


def _account(client):
    return hashlib.sha256(f"{client.space_id}:{client.user_id}".encode()).hexdigest()


def _account_lock(account):
    with _locks_guard:
        return _locks.setdefault(account, threading.Lock())


def _load(path):
    # Corrupt state is a hard error, never permission to submit a replacement.
    return json.loads(path.read_text()) if path.exists() else None


def _expired_done(job):
    return job.get("phase") == "done" and "completed_at" in job and time.time() - job["completed_at"] >= COMPLETED_CACHE_SECONDS


def _cache_path(account, job_id):
    return STATE_DIR / "completed" / account / f"{job_id}.json"


def _cache_done(account, job):
    if job.get("phase") != "done" or not job.get("async_job_id") or _expired_done(job):
        return
    # Older retained done records have no completion timestamp. Archive them
    # before replacing the account index, with a full retention window.
    job.setdefault("completed_at", time.time())
    (STATE_DIR / "completed").mkdir(parents=True, exist_ok=True, mode=0o700)
    _save(_cache_path(account, job["async_job_id"]), job)


def _read_cached_done(account, job_id):
    path = _cache_path(account, job_id)
    job = _load(path)
    if job and job.get("phase") == "done":
        if _expired_done(job):
            path.unlink(missing_ok=True)
            return None
        return job
    return None


def _clean_completed(account):
    for path in (STATE_DIR / "completed" / account).glob("*.json"):
        job = _load(path)
        if job and _expired_done(job):
            path.unlink(missing_ok=True)


def _request_parts(transcript, scope, local_protocol=False):
    text = _user_text(transcript)
    if not text:
        raise AgentRequestError("AGENT_EMPTY_INPUT", "No user text to send.", 400)
    model = _notion_model(transcript)
    identity = [scope, model, text] + (['dsh-local-tools-v2'] if local_protocol else [])
    fingerprint = hashlib.sha256(json.dumps(identity, ensure_ascii=False).encode()).hexdigest()
    return text, model, fingerprint


def _context_path(account, scope):
    return STATE_DIR / 'threads' / account / (hashlib.sha256(scope.encode()).hexdigest() + '.json')


def _queued_job(text, model, fingerprint, job_id, client=None, scope='', local_protocol=False):
    job = {"fingerprint": fingerprint, "thread_id": str(uuid.uuid4()),
           "request_id": str(uuid.uuid4()), "phase": "queued", "created_at": time.time(),
           "model": model, "text": text}
    if job_id:
        job["async_job_id"] = job_id
    if local_protocol:
        try:
            current = parse_bridge_request(text)
            if current is None:
                raise ValueError('Missing v2 envelope')
        except ValueError:
            raise AgentRequestError('AGENT_PROTOCOL', 'Invalid local tool protocol request.', 400) from None
        job.update(local_protocol=True, manifest_hash=current['manifest_hash'],
                   message_hashes=current['message_hashes'], tool_names=current['tool_names'],
                   baseline_sequence=0, submit_kind='create')
        if scope:
            job['scope_hash'] = hashlib.sha256(scope.encode()).hexdigest()
            context = _load(_context_path(_account(client), scope))
            delta = continuation_text(current, context) if context and context.get('model') == model else None
            if delta and isinstance(context.get('last_sequence'), int) and context['last_sequence'] >= 0:
                job.update(thread_id=context['thread_id'], text=delta, submit_kind='continue',
                           baseline_sequence=context['last_sequence'])
    return job


def prepare_agent_job(client, transcript, scope, job_id, local_protocol=False):
    """Persist the claim before 202; this path never contacts Notion."""
    text, model, fingerprint = _request_parts(transcript, scope, local_protocol)
    account = _account(client)
    lock = _account_lock(account)
    if not lock.acquire(blocking=False):
        raise AgentRequestError("AGENT_BUSY", "Notion already has an in-flight request; no new task was submitted.")
    try:
        _clean_completed(account)
        cached = _read_cached_done(account, job_id)
        if cached:
            return cached
        path = STATE_DIR / f"{account}.json"
        job = _load(path)
        if job and job["phase"] == "done":
            _cache_done(account, job)
            if job["fingerprint"] == fingerprint and not _expired_done(job):
                job["async_job_id"] = job_id
                _cache_done(account, job)
                _save(path, job)
                return job
            job = None
        if job:
            if job["fingerprint"] != fingerprint or job.get("async_job_id") not in (None, job_id):
                raise AgentRequestError("AGENT_BUSY", "A retained task is queued, running or unknown; no new task was submitted. Check its original ID.")
            if not job.get("async_job_id"):
                job["async_job_id"] = job_id
                _save(path, job)
            return job
        job = _queued_job(text, model, fingerprint, job_id, client, scope, local_protocol)
        _save(path, job)
        return job
    finally:
        lock.release()


def _mark_done(client, path, job, reply, terminal_error=None):
    job.update(phase="done", reply=reply, completed_at=time.time())
    if terminal_error:
        job['terminal_error'] = terminal_error
    # The independent result must be durable before another account job can
    # replace this index. GETs for older accepted IDs do not use the index.
    _cache_done(_account(client), job)
    _save(path, job)
    if job.get('local_protocol') and job.get('scope_hash'):
        context_path = STATE_DIR / 'threads' / _account(client) / (job['scope_hash'] + '.json')
        if terminal_error:
            context_path.unlink(missing_ok=True)
        else:
            (STATE_DIR / 'threads').mkdir(parents=True, exist_ok=True, mode=0o700)
            context = {key: job[key] for key in ('thread_id', 'model', 'manifest_hash', 'message_hashes', 'last_sequence')}
            context.update(updated_at=time.time())
            _save(context_path, context)


def _job_reply(job):
    error = job.get('terminal_error')
    if error:
        raise AgentRequestError(error['code'], error['message'], error['status'])
    return job['reply']


def _submit_queued(client, path, job):
    payload = {"type": "personal_agent", "spaceId": client.space_id, "threadId": job["thread_id"],
               "enableSuggestedEditsTools": True, "model": job["model"], "reasoningEffort": "medium",
               "createdSource": "full_page_chat", "content": [{"type": "text", "text": [[job["text"]]]}],
               "policies": {"approval_mode": "review_for_me"},
               "agentMemorySettings": {"useMemories": True, "excludeChatFromMemories": False},
               "browserEnabled": False, "clientMessageId": job["request_id"]}
    endpoint = 'createAgentThread'
    if job.get('submit_kind') == 'continue':
        endpoint = 'sendEventToAgentThread'
        payload = {'spaceId': client.space_id, 'threadId': job['thread_id'],
                   'event': {'type': 'user.message', 'content': [{'type': 'text', 'text': [[job['text']]]}]},
                   'surface': 'full_page_chat', 'clientEventId': job['request_id']}
    # From this point an outcome is ambiguous after a crash: pending never
    # creates again. Only a queued claim can make its first submission.
    job["phase"] = "pending"
    job.pop("text", None)
    _save(path, job)
    event = 'agent_bridge_continue' if endpoint == 'sendEventToAgentThread' else 'agent_bridge_create'
    logger.info(event, extra={"request_info": {"event": event, "thread_id": job["thread_id"],
                'protocol': 'v2' if job.get('local_protocol') else 'legacy',
                'job': job.get('async_job_id', '')[:12]}})
    try:
        created = requests.post(f"{NOTION_ORIGIN}/api/v3/{endpoint}", headers=_headers(client), json=payload, timeout=30)
    except requests.RequestException:
        created = None
    if created is not None and created.status_code in (400, 401, 403, 422, 429):
        path.unlink()  # Explicit rejection; no accepted run to recover.
        raise AgentRequestError("AGENT_REJECTED", f"Notion rejected task creation (HTTP {created.status_code}); no automatic retry.", created.status_code)


def _poll_v2(client, thread_id, timeout=20):
    """Rebuild one full bounded snapshot; page requests never generate a turn."""
    deadline = time.monotonic() + timeout
    payload = {'spaceId': client.space_id, 'threadId': thread_id, 'direction': 'backward', 'limit': 100}
    merged = None
    seen = set()
    for _ in range(20):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        try:
            response = requests.post(f'{NOTION_ORIGIN}/api/v3/getThreadTranscript', headers=_headers(client),
                                     json=payload, timeout=max(0.1, remaining))
            if response.status_code in (401, 403):
                raise AgentRequestError('AGENT_AUTH', 'Notion access failed while checking the retained task.', response.status_code)
            if response.status_code != 200:
                return {'_bridge_poll_status': response.status_code}
            page = response.json()
        except (requests.RequestException, ValueError):
            return {'_bridge_poll_status': 'transport'}
        if not isinstance(page, dict) or not isinstance(page.get('patches'), list):
            return {'_bridge_poll_status': 'invalid'}
        if merged is None:
            merged = dict(page, patches=list(page['patches']), _bridge_poll_status=200)
        elif payload['direction'] == 'backward':
            # Backward pages are older. Apply them before the fresh newest page.
            merged['patches'] = page['patches'] + merged['patches']
            merged['has_more_backward'] = page.get('has_more_backward')
        else:
            patches = merged['patches'] + page['patches']
            merged.update(page, patches=patches, has_more_backward=False)
        if merged.get('has_more_backward') is False and merged.get('has_more_forward') is False:
            return merged
        direction = 'backward' if merged.get('has_more_backward') else 'forward'
        cursor = merged.get(direction + '_cursor') if direction == 'forward' else page.get('backward_cursor')
        key = json.dumps([direction, cursor], sort_keys=True)
        if not cursor or key in seen:
            break
        seen.add(key)
        payload = dict(payload, direction=direction, cursor=cursor)
    return dict(merged or {}, _bridge_poll_status='incomplete', has_more_backward=True)


def _poll(client, thread_id, timeout=20):
    try:
        response = requests.post(f"{NOTION_ORIGIN}/api/v3/getThreadTranscript", headers=_headers(client),
                                 json={"spaceId": client.space_id, "threadId": thread_id, "direction": "backward", "limit": 20},
                                 timeout=timeout)
        if response.status_code in (401, 403):
            raise AgentRequestError("AGENT_AUTH", "Notion authentication/access failed while checking the retained task. No new task was submitted.", response.status_code)
        if response.status_code == 200:
            data = response.json()
            if isinstance(data, dict):
                data["_bridge_poll_status"] = 200
                return data
        return {"_bridge_poll_status": response.status_code}
    except (requests.RequestException, ValueError):
        return {"_bridge_poll_status": "network_or_decode_error"}


def complete_agent(client, transcript, scope="", *, job_id=None, wait_seconds=None, local_protocol=False):
    text, model, fingerprint = _request_parts(transcript, scope, local_protocol)
    account = _account(client)
    lock = _account_lock(account)
    if not lock.acquire(blocking=False):
        raise AgentRequestError("AGENT_BUSY", "Notion already has an in-flight request for this account; no new task was submitted.")
    try:
        path = STATE_DIR / f"{account}.json"
        if job_id:
            cached = _read_cached_done(account, job_id)
            if cached:
                return _job_reply(cached)
        job = _load(path)
        if job and job["fingerprint"] != fingerprint and job["phase"] != "done":
            if job.get('local_protocol'):
                raise AgentRequestError('AGENT_PENDING', 'The previous local task must be checked by its original ID; no new turn was submitted.')
            if job["phase"] == "queued":
                raise AgentRequestError("AGENT_BUSY", "A task is already queued; no new task was submitted.")
            data = _poll(client, job["thread_id"])
            reply = _reply(data) if _completed(data) else ""
            if not reply:
                raise AgentRequestError("AGENT_PENDING", "The previous Notion task is still running or its state is unknown; no new task was submitted. Check Notion before retrying.")
            _mark_done(client, path, job, reply)
        if job and job["phase"] == "done":
            _cache_done(account, job)
            if job["fingerprint"] != fingerprint or _expired_done(job):
                job = None
        if job and job_id and job.get("async_job_id") != job_id:
            if job.get("async_job_id") and job["phase"] != "done":
                raise AgentRequestError("AGENT_BUSY", "The original task has another retained ID; no new task was submitted.")
            job["async_job_id"] = job_id
            _cache_done(account, job)
            _save(path, job)
        if job and job["phase"] == "done":
            logger.info("agent_bridge_cache_hit")
            return _job_reply(job)
        deadline = time.monotonic() + (WAIT_SECONDS if wait_seconds is None else wait_seconds)
        if not job:
            job = _queued_job(text, model, fingerprint, job_id, client, scope, local_protocol)
            _save(path, job)
        if job["phase"] == "queued":
            _submit_queued(client, path, job)
        else:
            logger.info("agent_bridge_resume")
        client.current_thread_id = job["thread_id"]
        return _wait_for_job(client, path, job, deadline)
    finally:
        lock.release()



def _wait_for_job(client, path, job, deadline):
    while time.monotonic() < deadline:
        data = (_poll_v2 if job.get('local_protocol') else _poll)(client, job["thread_id"], timeout=max(0.1, min(20, deadline - time.monotonic())))
        poll_status = data.get("_bridge_poll_status", 200) if isinstance(data, dict) else "unknown"
        if job.get("last_poll_status") != poll_status:
            job["last_poll_status"] = poll_status
            _save(path, job)
        if job.get('local_protocol') and poll_status == 200:
            try:
                state = classify_snapshot(data, request_id=job['request_id'], baseline_sequence=job['baseline_sequence'])
            except ValueError:
                state = {'status': 'ambiguous'}
            status = state['status']
            if status == 'completed':
                job['last_sequence'] = state['last_sequence']
                reply = state['reply']
                if validate_local_reply(reply, job['tool_names']):
                    _mark_done(client, path, job, reply)
                    logger.info('agent_bridge_completed', extra={'request_info': {'event': 'agent_bridge_completed',
                                'thread_id': job['thread_id'], 'protocol': 'v2', 'job': job.get('async_job_id', '')[:12]}})
                    return reply
                error = dict(code='AGENT_PROTOCOL', message='The completed Notion turn did not contain a valid local tool decision.', status=502)
                _mark_done(client, path, job, '', terminal_error=error)
                return _job_reply(job)
            if status == 'requires_action':
                raise AgentRequestError('AGENT_REQUIRES_ACTION', 'The retained Notion turn requires a cloud-side action; no local result was fabricated.')
            if status == 'superseded':
                raise AgentRequestError('AGENT_SUPERSEDED', 'A later message changed the retained thread; no earlier reply was returned.')
            if status == 'deleted' or (status in ('interrupted', 'error') and _completed(data)):
                code = 'AGENT_INTERRUPTED' if status == 'interrupted' else 'AGENT_UPSTREAM_FAILED'
                error = dict(code=code, message='The retained Notion turn ended without a valid local decision.', status=502)
                _mark_done(client, path, job, '', terminal_error=error)
                return _job_reply(job)
        elif not job.get('local_protocol') and _completed(data):
            reply = _reply(data)
            if reply:
                _mark_done(client, path, job, reply)
                logger.info("agent_bridge_completed", extra={"request_info": {"event": "agent_bridge_completed", "thread_id": job["thread_id"]}})
                return reply
        remaining = deadline - time.monotonic()
        if remaining > 0:
            delay = POLL_SECONDS if time.time() - job.get("created_at", time.time()) < 30 else max(10, POLL_SECONDS)
            time.sleep(min(delay, remaining))
    logger.warning("agent_bridge_pending")
    code = "AGENT_PENDING" if job.get("last_poll_status") == 200 else "AGENT_UNKNOWN"
    raise AgentRequestError(code, "Wait limit reached; the Notion task is still running or its state is unknown. Its ID was retained. Resending the same request resumes polling without creating another task.", 504)


def read_agent_job(client, job_id):
    account = _account(client)
    cached = _read_cached_done(account, job_id)
    if cached:
        return cached
    path = STATE_DIR / f"{account}.json"
    job = _load(path)
    if not job or job.get("async_job_id") != job_id:
        return None
    if not _expired_done(job):
        return job
    # Re-read under the account lock: a concurrent worker may have replaced
    # this expired done index with a queued/pending job since the first read.
    lock = _account_lock(account)
    if lock.acquire(blocking=False):
        try:
            current = _load(path)
            if current and _expired_done(current):
                path.unlink(missing_ok=True)
        finally:
            lock.release()
    return None


def resume_agent_job(client, job_id):
    account = _account(client)
    lock = _account_lock(account)
    if not lock.acquire(blocking=False):
        raise AgentRequestError("AGENT_BUSY", "The original task is already being checked; no new task was submitted.")
    try:
        job = read_agent_job(client, job_id)
        if not job:
            raise AgentRequestError("AGENT_NOT_FOUND", "Retained task not found; do not automatically submit a replacement.", 404)
        if job["phase"] == "done":
            return _job_reply(job)
        path = STATE_DIR / f"{account}.json"
        deadline = time.monotonic() + ASYNC_WAIT_SECONDS
        if job["phase"] == "queued":
            _submit_queued(client, path, job)
        return _wait_for_job(client, path, job, deadline)
    finally:
        lock.release()

def stream_agent(client, transcript):
    yield {"type": "content", "text": complete_agent(client, transcript)}
