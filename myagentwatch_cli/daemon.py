"""Daemon runtime for myagentwatch-cli."""

from __future__ import annotations

import argparse
import json
import os
import shlex
import signal
import subprocess
import sys
import time
from pathlib import Path

from .client import CONFIG_PATH, load_config, get, post
from .local_inbox import append_chat_messages, append_inbox_items, max_chat_id, max_chat_ids, max_inbox_id, unread_count
from .monitor import collect_processes, collect_resources
from .queue import RetryQueue

PACKAGE_DIR = Path(__file__).resolve().parent
PROJECT_DIR = PACKAGE_DIR.parent
DATA_DIR = PROJECT_DIR / "data"
PID_PATH = DATA_DIR / "daemon.pid"
STOP_PATH = DATA_DIR / "daemon.stop"
STATE_PATH = DATA_DIR / "daemon_state.json"
LOG_PATH = DATA_DIR / "daemon.log"
QUEUE_PATH = DATA_DIR / "retry_queue.db"
POLICY_PATH = DATA_DIR / "daemon_policy.json"


def ensure_data_dir():
    DATA_DIR.mkdir(parents=True, exist_ok=True)


def _now() -> float:
    return time.time()


def _now_text() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _log(message: str):
    ensure_data_dir()
    with LOG_PATH.open("a", encoding="utf-8") as f:
        f.write(f"[{_now_text()}] {message}\n")


def _short_error(value, limit: int = 240) -> str:
    if isinstance(value, dict):
        text = value.get("error") or value.get("message") or json.dumps(value, ensure_ascii=False)
    else:
        text = str(value)
    text = " ".join(text.split())
    return text[:limit] + "..." if len(text) > limit else text


def _read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _write_json(path: Path, data: dict):
    ensure_data_dir()
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _default_daemon_policy(agent_id: str) -> dict:
    return {
        "policy_version": 2,
        "enabled": True,
        "claim_interval": 3,
        "lease_seconds": 300,
        "task_timeout_seconds": 1800,
        "allowed_agent_ids": [agent_id],
        "allowed_task_types": ["reply"],
        "autostart_enabled": False,
        "max_concurrent_tasks": 1,
        "shell_allowlist": [],
        "command_templates": {
            "reply": []
        },
        "notes": (
            "Set autostart_enabled=true and command_templates.reply to a list command "
            "before daemon starts local Agent CLIs. Placeholders: {task_id}, {agent_id}, "
            "{body}, {source_conversation_id}, {source_message_id}."
        ),
    }


def _load_daemon_policy(agent_id: str) -> dict:
    default = _default_daemon_policy(agent_id)
    if not POLICY_PATH.exists():
        _write_json(POLICY_PATH, default)
        return default
    loaded = _read_json(POLICY_PATH)
    policy = dict(default)
    policy.update({k: v for k, v in loaded.items() if v is not None})
    if not isinstance(policy.get("allowed_agent_ids"), list):
        policy["allowed_agent_ids"] = default["allowed_agent_ids"]
    if not isinstance(policy.get("allowed_task_types"), list):
        policy["allowed_task_types"] = default["allowed_task_types"]
    if not isinstance(policy.get("shell_allowlist"), list):
        policy["shell_allowlist"] = default["shell_allowlist"]
    if not isinstance(policy.get("command_templates"), dict):
        policy["command_templates"] = default["command_templates"]
    for key in ("claim_interval", "lease_seconds", "task_timeout_seconds", "max_concurrent_tasks"):
        try:
            policy[key] = max(1, int(policy.get(key) or default[key]))
        except Exception:
            policy[key] = default[key]
    return policy


def _read_pid() -> int | None:
    try:
        return int(PID_PATH.read_text(encoding="utf-8").strip())
    except Exception:
        return None


def _process_exists(pid: int | None) -> bool:
    if not pid or pid <= 0:
        return False
    if os.name == "nt":
        import ctypes

        handle = ctypes.windll.kernel32.OpenProcess(0x100000, False, int(pid))
        if not handle:
            return False
        try:
            result = ctypes.windll.kernel32.WaitForSingleObject(handle, 0)
            return result == 0x102
        finally:
            ctypes.windll.kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _force_kill(pid: int):
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/PID", str(pid), "/T", "/F"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    else:
        os.kill(pid, signal.SIGKILL)


def _ok(resp: dict) -> bool:
    return isinstance(resp, dict) and "error" not in resp


def _format_age(ts: float | None) -> str:
    if not ts:
        return "--"
    seconds = max(0, int(_now() - ts))
    if seconds < 60:
        return f"{seconds}s ago"
    minutes, sec = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m {sec}s ago"
    hours, minute = divmod(minutes, 60)
    return f"{hours}h {minute}m ago"


def _format_time(ts: float | None) -> str:
    if not ts:
        return "--"
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts))


def _format_duration(seconds: float) -> str:
    seconds = max(0, int(seconds))
    hours, rem = divmod(seconds, 3600)
    minutes, sec = divmod(rem, 60)
    if hours:
        return f"{hours}h {minutes}m {sec}s"
    if minutes:
        return f"{minutes}m {sec}s"
    return f"{sec}s"


def _validate_config() -> tuple[bool, str, dict]:
    if not os.path.exists(CONFIG_PATH):
        return False, "请先执行 myaw connect，config.json 不存在", {}
    cfg = load_config()
    if not cfg.get("agent_id"):
        return False, "config.json 缺少 agent_id", cfg
    if not cfg.get("server"):
        return False, "config.json 缺少 server", cfg
    return True, "", cfg


def _heartbeat_payload(agent_id: str) -> dict:
    payload = {
        "status": "active",
        "metadata": {"source": "myagentwatch-cli-daemon"},
    }
    cfg = load_config()
    model_id = cfg.get("model_id")
    if not model_id:
        parts = agent_id.split(":")
        if len(parts) >= 3:
            model_id = parts[2]
    if model_id:
        payload["model_id"] = model_id
    return payload


def _post_or_queue(queue: RetryQueue, endpoint: str, payload: dict) -> tuple[bool, dict]:
    resp = post(endpoint, payload)
    if _ok(resp):
        return True, resp
    error_text = _short_error(resp)
    row_id = queue.enqueue(endpoint, payload, last_error=error_text)
    _log(f"queued failed report id={row_id} endpoint={endpoint} error={error_text}")
    return False, resp


def _state_template(agent_id: str, started_at: float) -> dict:
    return {
        "running": True,
        "pid": os.getpid(),
        "agent_id": agent_id,
        "started_at": started_at,
        "last_chat_message_id": None,
        "last_chat_message_ids": {},
        "chat_conversation_count": 0,
        "last_chat_poll_at": None,
        "last_chat_poll_ok": None,
        "last_inbox_item_id": None,
        "last_inbox_poll_at": None,
        "last_inbox_poll_ok": None,
        "last_task_claim_at": None,
        "last_task_claim_ok": None,
        "last_task_claim_skip": "",
        "last_claimed_task_id": None,
        "tasks_completed": 0,
        "tasks_failed": 0,
        "unread_count": 0,
        "recent_messages": [],
        "last_heartbeat_at": None,
        "last_heartbeat_ok": None,
        "last_resources_at": None,
        "last_resources_ok": None,
        "resources_reported": 0,
        "last_processes_at": None,
        "last_processes_ok": None,
        "processes_reported": 0,
        "last_error": "",
        "updated_at": _now(),
    }


def _summarize_inbox_item(item: dict) -> dict:
    return {
        "type": "inbox",
        "id": item.get("id"),
        "title": item.get("title", ""),
        "body": (item.get("body") or "")[:120],
        "created_at": item.get("created_at"),
    }


def _summarize_chat_message(conv_id: int, msg: dict) -> dict:
    return {
        "type": "chat",
        "id": msg.get("id"),
        "conversation_id": conv_id,
        "sender": msg.get("sender_name", ""),
        "body": (msg.get("content") or "")[:120],
        "created_at": msg.get("timestamp"),
    }


def _poll_inbox(agent_id: str, last_item_id: int) -> tuple[bool, int, list[dict], dict]:
    resp = get(f"/api/inbox?recipient={agent_id}&limit=200")
    if not _ok(resp):
        return False, last_item_id, [], resp
    items = resp.get("items", [])
    fresh = [item for item in items if int(item.get("id") or 0) > last_item_id]
    fresh.sort(key=lambda item: int(item.get("id") or 0))
    if fresh:
        append_inbox_items(fresh)
        last_item_id = max(int(item.get("id") or 0) for item in fresh)
    return True, last_item_id, fresh, resp


def _poll_conversations() -> tuple[bool, list[dict], dict]:
    resp = get("/api/chat/conversations")
    if not _ok(resp):
        return False, [], resp
    convs = [conv for conv in resp.get("conversations", []) if int(conv.get("id") or 0) > 0]
    return True, convs, resp


def _poll_chat(conv_id: int, last_message_id: int) -> tuple[bool, int, list[dict], dict]:
    resp = get(f"/api/chat/messages/{conv_id}?after_id={last_message_id}&limit=50")
    if not _ok(resp):
        return False, last_message_id, [], resp
    messages = resp.get("messages", [])
    if messages:
        append_chat_messages(conv_id, messages)
        last_message_id = max(int(msg.get("id") or 0) for msg in messages)
    return True, last_message_id, messages, resp


def _command_for_task(policy: dict, task_type: str) -> list[str]:
    templates = policy.get("command_templates") or {}
    raw = templates.get(task_type) or []
    if isinstance(raw, str):
        raw = shlex.split(raw)
    if not isinstance(raw, list):
        return []
    return [str(part) for part in raw if str(part)]


def _claimable_task_types(policy: dict) -> list[str]:
    allowed = [str(t) for t in (policy.get("allowed_task_types") or [])]
    return [task_type for task_type in allowed if _command_for_task(policy, task_type)]


def _policy_allows_claim(policy: dict, agent_id: str) -> tuple[bool, str]:
    if not policy.get("enabled", True):
        return False, "policy_disabled"
    allowed_agents = policy.get("allowed_agent_ids") or []
    if "*" not in allowed_agents and agent_id not in allowed_agents:
        return False, "agent_not_allowed"
    if not policy.get("autostart_enabled", False):
        return False, "autostart_disabled"
    if not _claimable_task_types(policy):
        return False, "no_command_template"
    return True, ""


def _claim_agent_task(agent_id: str, policy: dict) -> tuple[bool, dict | None, dict]:
    allowed, reason = _policy_allows_claim(policy, agent_id)
    if not allowed:
        return True, None, {"skipped": reason}
    resp = post("/api/daemon/tasks/claim", {
        "agent_id": agent_id,
        "claimed_by": "myagentwatch-cli-daemon",
        "allowed_task_types": _claimable_task_types(policy),
        "lease_seconds": int(policy.get("lease_seconds") or 300),
    })
    if not _ok(resp):
        return False, None, resp
    return True, resp.get("task"), resp


def _format_command(template: list[str], task: dict) -> list[str]:
    values = {
        "task_id": task.get("id") or "",
        "agent_id": task.get("agent_id") or "",
        "agent_name": task.get("agent_name") or "",
        "task_type": task.get("task_type") or "",
        "title": task.get("title") or "",
        "body": task.get("body") or "",
        "source_conversation_id": task.get("source_conversation_id") or "",
        "source_message_id": task.get("source_message_id") or "",
    }
    return [part.format(**values) for part in template]


def _command_allowed_by_policy(policy: dict, task_type: str, command: list[str]) -> tuple[bool, str]:
    if not command:
        return False, "empty command"
    if task_type != "shell_command":
        return True, ""
    allowlist = [str(item).strip().lower() for item in (policy.get("shell_allowlist") or []) if str(item).strip()]
    if not allowlist:
        return False, "shell_allowlist_empty"
    executable = Path(command[0]).name.lower()
    raw = str(command[0]).strip().lower()
    if "*" in allowlist or executable in allowlist or raw in allowlist:
        return True, ""
    return False, f"shell command not allowlisted: {command[0]}"


def _handle_claimed_task(task: dict, policy: dict) -> tuple[bool, str]:
    task_id = int(task.get("id") or 0)
    task_type = task.get("task_type") or "reply"
    template = _command_for_task(policy, task_type)
    if not task_id:
        return False, "missing task id"
    if not template:
        post(f"/api/daemon/tasks/{task_id}/fail", {"error_text": "daemon policy has no command template"})
        return False, "missing command template"

    command = _format_command(template, task)
    allowed, reason = _command_allowed_by_policy(policy, task_type, command)
    if not allowed:
        post(f"/api/daemon/tasks/{task_id}/fail", {"error_text": reason})
        return False, reason

    start_resp = post(f"/api/daemon/tasks/{task_id}/start", {"actor_id": "myagentwatch-cli-daemon"})
    if not _ok(start_resp):
        return False, _short_error(start_resp)

    timeout = int(policy.get("task_timeout_seconds") or 1800)
    _log(f"agent task start id={task_id} type={task_type} command={command[0] if command else ''}")
    try:
        proc = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=timeout,
            shell=False,
        )
    except Exception as exc:
        error_text = _short_error(exc)
        post(f"/api/daemon/tasks/{task_id}/fail", {"error_text": error_text})
        return False, error_text

    stdout = (proc.stdout or "").strip()
    stderr = (proc.stderr or "").strip()
    if proc.returncode == 0:
        complete_resp = post(f"/api/daemon/tasks/{task_id}/complete", {
            "result_text": stdout,
            "metadata": {
                "returncode": proc.returncode,
                "stderr": stderr[-2000:],
            },
        })
        if not _ok(complete_resp):
            return False, _short_error(complete_resp)
        return True, f"completed task {task_id}"

    error_text = stderr or stdout or f"command failed with exit code {proc.returncode}"
    fail_resp = post(f"/api/daemon/tasks/{task_id}/fail", {"error_text": error_text[-4000:]})
    if not _ok(fail_resp):
        return False, _short_error(fail_resp)
    return False, error_text[-240:]


def daemon_loop():
    ok, message, cfg = _validate_config()
    if not ok:
        raise SystemExit(message)

    ensure_data_dir()
    STOP_PATH.unlink(missing_ok=True)
    PID_PATH.write_text(str(os.getpid()), encoding="utf-8")

    agent_id = cfg["agent_id"]
    heartbeat_interval = max(float(cfg.get("heartbeat_interval", 15)), 5)
    resource_interval = max(float(cfg.get("resource_interval", 30)), 10)
    process_interval = max(float(cfg.get("process_interval", 60)), 30)
    chat_poll_interval = max(float(cfg.get("chat_poll_interval", 3)), 2)
    inbox_poll_interval = max(float(cfg.get("inbox_poll_interval", 3)), 2)
    policy = _load_daemon_policy(agent_id)
    task_claim_interval = max(float(policy.get("claim_interval", cfg.get("task_claim_interval", 3)) or 3), 2)
    chat_conversation_id = int(cfg.get("chat_conversation_id", 1) or 1)
    queue = RetryQueue(str(QUEUE_PATH))
    previous_state = _read_json(STATE_PATH)
    state = _state_template(agent_id, _now())
    local_chat_ids = max_chat_ids()
    previous_chat_ids = previous_state.get("last_chat_message_ids") or {}
    if isinstance(previous_chat_ids, dict):
        state["last_chat_message_ids"] = {str(k): int(v or 0) for k, v in previous_chat_ids.items()}
    else:
        state["last_chat_message_ids"] = {}
    for conv_key, msg_id in local_chat_ids.items():
        state["last_chat_message_ids"][str(conv_key)] = max(
            int(state["last_chat_message_ids"].get(str(conv_key), 0) or 0), int(msg_id or 0)
        )
    default_key = str(chat_conversation_id)
    if default_key not in state["last_chat_message_ids"]:
        state["last_chat_message_ids"][default_key] = int(
            previous_state.get("last_chat_message_id") or max_chat_id(chat_conversation_id) or 0
        )
    state["last_chat_message_id"] = max(state["last_chat_message_ids"].values(), default=0)
    state["chat_conversation_count"] = len(state["last_chat_message_ids"])
    state["last_inbox_item_id"] = int(previous_state.get("last_inbox_item_id") or max_inbox_id() or 0)
    state["unread_count"] = unread_count()
    _write_json(STATE_PATH, state)

    last_heartbeat = 0.0
    last_resource = 0.0
    last_process = 0.0
    last_chat_poll = 0.0
    last_inbox_poll = 0.0
    last_task_claim = 0.0
    last_summary_log = 0.0
    _log(
        "daemon started "
        f"pid={os.getpid()} agent={agent_id} "
        f"intervals={heartbeat_interval}/{resource_interval}/{process_interval} "
        f"chat={chat_poll_interval} inbox={inbox_poll_interval} tasks={task_claim_interval}"
    )

    def on_queue_event(event: str, item: dict):
        if event == "success":
            _log(
                "retry queue delivered "
                f"id={item.get('id')} endpoint={item.get('endpoint')} "
                f"retry_count={item.get('retry_count')}"
            )
        elif event == "dead":
            _log(
                "retry queue dead "
                f"id={item.get('id')} endpoint={item.get('endpoint')} "
                f"retry_count={item.get('retry_count')} error={_short_error(item.get('error'))}"
            )

    try:
        while not STOP_PATH.exists():
            now = _now()
            try:
                consumed = queue.consume(post, max_items=20, on_event=on_queue_event)
                if consumed:
                    _log(f"retry queue consumed={consumed}")

                if now - last_task_claim >= task_claim_interval:
                    policy = _load_daemon_policy(agent_id)
                    success, task, resp = _claim_agent_task(agent_id, policy)
                    last_task_claim = now
                    state["last_task_claim_at"] = now
                    state["last_task_claim_ok"] = success
                    state["last_task_claim_skip"] = resp.get("skipped", "") if isinstance(resp, dict) else ""
                    if not success:
                        state["last_error"] = f"task claim failed: {resp}"
                        _log(state["last_error"])
                    elif task:
                        state["last_claimed_task_id"] = task.get("id")
                        handled, message = _handle_claimed_task(task, policy)
                        if handled:
                            state["tasks_completed"] += 1
                            _log(f"agent task completed id={task.get('id')}")
                        else:
                            state["tasks_failed"] += 1
                            state["last_error"] = f"agent task failed id={task.get('id')}: {message}"
                            _log(state["last_error"])

                if now - last_inbox_poll >= inbox_poll_interval:
                    success, last_id, fresh, resp = _poll_inbox(agent_id, int(state.get("last_inbox_item_id") or 0))
                    last_inbox_poll = now
                    state["last_inbox_poll_at"] = now
                    state["last_inbox_poll_ok"] = success
                    if success:
                        state["last_inbox_item_id"] = last_id
                        state["unread_count"] = unread_count()
                        if fresh:
                            state["recent_messages"] = (
                                [_summarize_inbox_item(item) for item in fresh] + state.get("recent_messages", [])
                            )[:10]
                            _log(f"inbox received={len(fresh)} unread={state['unread_count']}")
                    else:
                        state["last_error"] = f"inbox poll failed: {resp}"
                        _log(state["last_error"])

                if now - last_chat_poll >= chat_poll_interval:
                    conv_success, convs, conv_resp = _poll_conversations()
                    conv_ids = [int(conv.get("id") or 0) for conv in convs if int(conv.get("id") or 0) > 0]
                    if not conv_ids:
                        conv_ids = [chat_conversation_id]
                    last_by_conv = state.setdefault("last_chat_message_ids", {})
                    success_all = conv_success
                    summaries = []
                    total_messages = 0
                    for conv_id in conv_ids:
                        key = str(conv_id)
                        previous_last = int(last_by_conv.get(key) or max_chat_id(conv_id) or 0)
                        success, last_id, messages, resp = _poll_chat(conv_id, previous_last)
                        if success:
                            last_by_conv[key] = last_id
                            total_messages += len(messages)
                            if messages:
                                summaries.extend(_summarize_chat_message(conv_id, msg) for msg in messages)
                                _log(f"chat cached conv={conv_id} messages={len(messages)} last_id={last_id}")
                        else:
                            success_all = False
                            state["last_error"] = f"chat poll failed conv={conv_id}: {resp}"
                            _log(state["last_error"])
                    last_chat_poll = now
                    state["last_chat_poll_at"] = now
                    state["last_chat_poll_ok"] = success_all
                    state["chat_conversation_count"] = len(conv_ids)
                    state["last_chat_message_id"] = max((int(v or 0) for v in last_by_conv.values()), default=0)
                    if summaries:
                        state["recent_messages"] = (summaries + state.get("recent_messages", []))[:10]
                    if not conv_success:
                        state["last_error"] = f"conversation poll failed: {conv_resp}"
                        _log(state["last_error"])
                    elif total_messages:
                        _log(f"chat poll conversations={len(conv_ids)} messages={total_messages}")

                if now - last_heartbeat >= heartbeat_interval:
                    endpoint = f"/api/heartbeat/{agent_id}"
                    payload = _heartbeat_payload(agent_id)
                    success, resp = _post_or_queue(queue, endpoint, payload)
                    last_heartbeat = now
                    state["last_heartbeat_at"] = now
                    state["last_heartbeat_ok"] = success
                    if not success:
                        state["last_error"] = f"heartbeat failed: {resp}"
                        _log(state["last_error"])

                if now - last_resource >= resource_interval:
                    snapshot = collect_resources(agent_id=agent_id)
                    success, resp = _post_or_queue(queue, "/api/agent-ingest/resources", snapshot)
                    last_resource = now
                    state["last_resources_at"] = now
                    state["last_resources_ok"] = success
                    if success:
                        state["resources_reported"] += 1
                    else:
                        state["last_error"] = f"resources failed: {resp}"
                        _log(state["last_error"])

                if now - last_process >= process_interval:
                    snapshot = collect_processes(agent_id=agent_id)
                    success, resp = _post_or_queue(queue, "/api/agent-ingest/processes", snapshot)
                    last_process = now
                    state["last_processes_at"] = now
                    state["last_processes_ok"] = success
                    if success:
                        state["processes_reported"] += int(resp.get("count") or len(snapshot.get("processes", [])))
                    else:
                        state["last_error"] = f"processes failed: {resp}"
                        _log(state["last_error"])

            except Exception as exc:
                state["last_error"] = str(exc)
                _log(f"loop error: {exc}")

            state["updated_at"] = _now()
            state["retry_queue"] = queue.stats()
            if now - last_summary_log >= 60:
                stats = state["retry_queue"]
                _log(
                    "summary "
                    f"heartbeat={'OK' if state.get('last_heartbeat_ok') else '--'} "
                    f"resources={'OK' if state.get('last_resources_ok') else '--'} "
                    f"processes={'OK' if state.get('last_processes_ok') else '--'} "
                    f"resources_reported={state.get('resources_reported', 0)} "
                    f"processes_reported={state.get('processes_reported', 0)} "
                    f"chat={'OK' if state.get('last_chat_poll_ok') else '--'} "
                    f"inbox={'OK' if state.get('last_inbox_poll_ok') else '--'} "
                    f"tasks={'OK' if state.get('last_task_claim_ok') else '--'} "
                    f"task_skip={state.get('last_task_claim_skip') or '-'} "
                    f"tasks_done={state.get('tasks_completed', 0)} "
                    f"tasks_failed={state.get('tasks_failed', 0)} "
                    f"unread={state.get('unread_count', 0)} "
                    f"queue_pending={stats.get('pending', 0)} "
                    f"queue_dead={stats.get('dead', 0)}"
                )
                last_summary_log = now
            _write_json(STATE_PATH, state)
            time.sleep(2)
    finally:
        state["running"] = False
        state["updated_at"] = _now()
        _write_json(STATE_PATH, state)
        PID_PATH.unlink(missing_ok=True)
        STOP_PATH.unlink(missing_ok=True)
        _log("daemon stopped")


def start_daemon(foreground: bool = False) -> int:
    ok, message, _ = _validate_config()
    if not ok:
        print(message)
        return 1

    ensure_data_dir()
    old_pid = _read_pid()
    if old_pid and _process_exists(old_pid):
        _log(f"start ignored: already running pid={old_pid}")
        print(f"already running (PID: {old_pid})")
        return 0
    if old_pid:
        _log(f"stale PID cleaned before start pid={old_pid}")
        PID_PATH.unlink(missing_ok=True)

    if foreground:
        _log("foreground start requested")
        daemon_loop()
        return 0

    STOP_PATH.unlink(missing_ok=True)
    env = os.environ.copy()
    env["PYTHONPATH"] = str(PROJECT_DIR) + os.pathsep + env.get("PYTHONPATH", "")
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    flags = 0
    if os.name == "nt":
        flags = (
            getattr(subprocess, "DETACHED_PROCESS", 0)
            | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            | getattr(subprocess, "CREATE_NO_WINDOW", 0)
        )
    log = LOG_PATH.open("ab")
    proc = subprocess.Popen(
        [sys.executable, "-m", "myagentwatch_cli.daemon", "--child"],
        cwd=str(PROJECT_DIR),
        env=env,
        stdout=log,
        stderr=log,
        stdin=subprocess.DEVNULL,
        creationflags=flags,
    )
    PID_PATH.write_text(str(proc.pid), encoding="utf-8")
    _log(f"start command launched child pid={proc.pid}")
    print(f"daemon started (PID: {proc.pid})")
    print(f"log: {LOG_PATH}")
    return 0


def stop_daemon(force: bool = False) -> int:
    pid = _read_pid()
    if not pid:
        _log("stop requested: no pid file")
        print("daemon stopped")
        return 0
    if not _process_exists(pid):
        PID_PATH.unlink(missing_ok=True)
        STOP_PATH.unlink(missing_ok=True)
        _log(f"stop cleaned stale PID pid={pid}")
        print("daemon stopped (stale PID cleaned)")
        return 0

    if force:
        _log(f"force stop requested pid={pid}")
        _force_kill(pid)
        PID_PATH.unlink(missing_ok=True)
        STOP_PATH.unlink(missing_ok=True)
        _log(f"daemon force stopped pid={pid}")
        print(f"daemon stopped (was PID: {pid})")
        return 0

    _log(f"stop marker written pid={pid}")
    STOP_PATH.write_text(str(_now()), encoding="utf-8")
    deadline = _now() + 5
    while _now() < deadline:
        if not _process_exists(pid):
            PID_PATH.unlink(missing_ok=True)
            STOP_PATH.unlink(missing_ok=True)
            _log(f"daemon stopped gracefully pid={pid}")
            print(f"daemon stopped (was PID: {pid})")
            return 0
        time.sleep(0.25)

    _log(f"stop timeout, forcing pid={pid}")
    _force_kill(pid)
    PID_PATH.unlink(missing_ok=True)
    STOP_PATH.unlink(missing_ok=True)
    _log(f"daemon stopped by force pid={pid}")
    print(f"daemon stopped by force (was PID: {pid})")
    return 0


def daemon_status(json_output: bool = False) -> int:
    ensure_data_dir()
    pid = _read_pid()
    running = _process_exists(pid)
    stale_pid = bool(pid and not running)
    state = _read_json(STATE_PATH)
    queue = RetryQueue(str(QUEUE_PATH))
    stats = queue.stats()
    result = {
        "running": running,
        "pid": pid if running else None,
        "pid_file_pid": pid,
        "stale_pid": stale_pid,
        "state": state,
        "retry_queue": stats,
        "pid_file": str(PID_PATH),
        "state_file": str(STATE_PATH),
        "log_file": str(LOG_PATH),
        "queue_file": str(QUEUE_PATH),
        "policy_file": str(POLICY_PATH),
        "log_path": str(LOG_PATH),
    }
    if json_output:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0

    if running:
        started = state.get("started_at") or _now()
        print("Daemon: running")
        print(f"  PID: {pid}")
        print(f"  Uptime: {_format_duration(_now() - started)}")
        print(f"  Heartbeat: {'OK' if state.get('last_heartbeat_ok') else '--'} (last: {_format_age(state.get('last_heartbeat_at'))})")
        print(f"  Inbox: {'OK' if state.get('last_inbox_poll_ok') else '--'} (last: {_format_age(state.get('last_inbox_poll_at'))}, unread: {state.get('unread_count', 0)})")
        print(f"  Chat: {'OK' if state.get('last_chat_poll_ok') else '--'} (last: {_format_age(state.get('last_chat_poll_at'))}, conversations: {state.get('chat_conversation_count', 0)}, last id: {state.get('last_chat_message_id') or 0})")
        task_bits = f"last: {_format_age(state.get('last_task_claim_at'))}, done: {state.get('tasks_completed', 0)}, failed: {state.get('tasks_failed', 0)}"
        if state.get("last_task_claim_skip"):
            task_bits += f", skip: {state.get('last_task_claim_skip')}"
        print(f"  Agent tasks: {'OK' if state.get('last_task_claim_ok') else '--'} ({task_bits})")
        print(f"  Resources: {'OK' if state.get('last_resources_ok') else '--'} (last: {_format_age(state.get('last_resources_at'))}, {state.get('resources_reported', 0)} reported)")
        print(f"  Processes: {'OK' if state.get('last_processes_ok') else '--'} (last: {_format_age(state.get('last_processes_at'))}, {state.get('processes_reported', 0)} reported)")
    else:
        print("Daemon: stopped")
        if stale_pid:
            print(f"  Stale PID: {pid}")
    print(f"  Retry queue: {stats['pending']} pending, {stats['dead']} dead")
    print(f"  Policy: {POLICY_PATH}")
    print(f"  Log: {LOG_PATH}")
    if state.get("last_error"):
        print(f"  Last error: {state['last_error']}")
    return 0


def daemon_runner_status(json_output: bool = False) -> int:
    ok, message, cfg = _validate_config()
    if not ok:
        print(message)
        return 1
    agent_id = cfg["agent_id"]
    ensure_data_dir()
    policy = _load_daemon_policy(agent_id)
    claimable = _claimable_task_types(policy)
    allowed, skip_reason = _policy_allows_claim(policy, agent_id)
    result = {
        "agent_id": agent_id,
        "policy_file": str(POLICY_PATH),
        "policy": policy,
        "claimable_task_types": claimable,
        "can_claim": allowed,
        "skip_reason": "" if allowed else skip_reason,
    }
    if json_output:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0

    print("Runner policy")
    print(f"  Agent: {agent_id}")
    print(f"  Policy version: {policy.get('policy_version', 1)}")
    print(f"  Enabled: {'yes' if policy.get('enabled', True) else 'no'}")
    print(f"  Autostart: {'yes' if policy.get('autostart_enabled', False) else 'no'}")
    print(f"  Claim interval: {policy.get('claim_interval')}s")
    print(f"  Lease: {policy.get('lease_seconds')}s")
    print(f"  Task timeout: {policy.get('task_timeout_seconds')}s")
    print(f"  Allowed agents: {', '.join(policy.get('allowed_agent_ids') or []) or '-'}")
    print(f"  Allowed task types: {', '.join(policy.get('allowed_task_types') or []) or '-'}")
    print(f"  Claimable task types: {', '.join(claimable) or '-'}")
    print(f"  Shell allowlist: {', '.join(policy.get('shell_allowlist') or []) or '-'}")
    print(f"  Decision: {'can claim' if allowed else 'skip: ' + skip_reason}")
    print(f"  Policy file: {POLICY_PATH}")
    return 0


def daemon_runner_test(task_id: int | None = None, json_output: bool = False) -> int:
    ok, message, cfg = _validate_config()
    if not ok:
        print(message)
        return 1
    agent_id = cfg["agent_id"]
    ensure_data_dir()
    policy = _load_daemon_policy(agent_id)
    if not task_id:
        result = {
            "agent_id": agent_id,
            "policy_file": str(POLICY_PATH),
            "claimable_task_types": _claimable_task_types(policy),
            "autostart_enabled": bool(policy.get("autostart_enabled", False)),
            "lease_seconds": int(policy.get("lease_seconds") or 300),
            "task_timeout_seconds": int(policy.get("task_timeout_seconds") or 1800),
        }
        if json_output:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        else:
            print("Runner test")
            print("  No task id supplied; policy syntax loaded successfully.")
            print(f"  Claimable task types: {', '.join(result['claimable_task_types']) or '-'}")
            print(f"  Policy file: {POLICY_PATH}")
        return 0

    resp = get(f"/api/agent/tasks/{int(task_id)}")
    task = resp.get("task") if isinstance(resp, dict) else None
    if not task:
        print(f"runner test failed: {resp}")
        return 2
    task_type = task.get("task_type") or "reply"
    template = _command_for_task(policy, task_type)
    command = _format_command(template, task) if template else []
    command_allowed, command_reason = _command_allowed_by_policy(policy, task_type, command)
    approval_status = task.get("approval_status") or "not_required"
    approval_allowed = approval_status in ("not_required", "approved")
    claim_policy_allowed, claim_policy_reason = _policy_allows_claim(policy, task.get("agent_id") or agent_id)
    agent_matches = (task.get("agent_id") or "") == agent_id
    task_status_claimable = (task.get("status") or "") == "queued"
    autostart_allowed = bool(task.get("allow_autostart"))
    can_claim = bool(
        template
        and command_allowed
        and approval_allowed
        and claim_policy_allowed
        and agent_matches
        and task_status_claimable
        and autostart_allowed
    )
    result = {
        "task_id": int(task_id),
        "task_type": task_type,
        "agent_id": task.get("agent_id"),
        "local_agent_id": agent_id,
        "status": task.get("status"),
        "allow_autostart": autostart_allowed,
        "approval_status": approval_status,
        "approval_allowed": approval_allowed,
        "claim_policy_allowed": claim_policy_allowed,
        "claim_policy_reason": claim_policy_reason,
        "agent_matches": agent_matches,
        "task_status_claimable": task_status_claimable,
        "template_found": bool(template),
        "command": command,
        "allowed_by_policy": command_allowed,
        "denied_reason": command_reason,
        "can_claim": can_claim,
        "will_execute": False,
    }
    if json_output:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if can_claim else 2

    print(f"Runner dry-run task #{task_id}")
    print(f"  Type: {task_type}")
    print(f"  Agent: {task.get('agent_id')} (local: {agent_id})")
    print(f"  Status: {task.get('status')} / autostart={'yes' if autostart_allowed else 'no'}")
    print(f"  Approval: {'allowed' if approval_allowed else 'blocked'} ({approval_status})")
    print(f"  Claim policy: {'allowed' if claim_policy_allowed else 'denied: ' + claim_policy_reason}")
    print(f"  Template: {'yes' if template else 'no'}")
    print(f"  Command: {command if command else '-'}")
    print(f"  Command policy: {'allowed' if command_allowed else 'denied: ' + command_reason}")
    print(f"  Can claim: {'yes' if can_claim else 'no'}")
    print("  Execute: no (dry-run only)")
    return 0 if can_claim else 2


def daemon_queue(json_output: bool = False) -> int:
    ensure_data_dir()
    queue = RetryQueue(str(QUEUE_PATH))
    details = queue.details(limit=5)
    if json_output:
        print(json.dumps(details, ensure_ascii=False, indent=2))
        return 0

    stats = details["stats"]
    print("Retry queue")
    print(f"  Pending: {stats['pending']}")
    print(f"  Dead: {stats['dead']}")
    print(f"  Total: {stats['total_queued']}")
    print(f"  Oldest pending: {_format_time(stats.get('oldest_pending_at'))}")
    print(f"  Newest pending: {_format_time(stats.get('newest_pending_at'))}")
    print(f"  Next retry: {_format_time(stats.get('next_retry_at'))}")
    print(f"  Latest failure: {_format_time(stats.get('latest_failure_at'))}")
    if stats.get("last_error"):
        print(f"  Last error: {_short_error(stats['last_error'])}")

    if details["pending"]:
        print("")
        print("Pending samples:")
        for item in details["pending"]:
            print(
                f"  #{item['id']} {item['endpoint']} "
                f"retry={item['retry_count']} next={_format_time(item.get('next_retry_at'))} "
                f"error={_short_error(item.get('last_error') or '--')}"
            )
    if details["dead"]:
        print("")
        print("Dead samples:")
        for item in details["dead"]:
            print(
                f"  #{item['id']} {item['endpoint']} "
                f"retry={item['retry_count']} failed={_format_time(item.get('last_failed_at'))} "
                f"error={_short_error(item.get('last_error') or '--')}"
            )
    return 0


def daemon_cleanup_dead() -> int:
    ensure_data_dir()
    queue = RetryQueue(str(QUEUE_PATH))
    removed = queue.cleanup_dead()
    _log(f"retry queue cleanup_dead removed={removed}")
    print(f"cleaned dead retry queue items: {removed}")
    return 0


def daemon_logs(lines: int = 50, follow: bool = False) -> int:
    ensure_data_dir()
    if not LOG_PATH.exists():
        print("daemon.log does not exist")
        return 0
    printed = 0
    content = LOG_PATH.read_text(encoding="utf-8", errors="replace").splitlines()
    for line in content[-lines:]:
        print(line)
        printed += 1
    if follow:
        with LOG_PATH.open("r", encoding="utf-8", errors="replace") as f:
            f.seek(0, os.SEEK_END)
            try:
                while True:
                    line = f.readline()
                    if line:
                        print(line, end="")
                    else:
                        time.sleep(1)
            except KeyboardInterrupt:
                return 0
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--child", action="store_true")
    args = parser.parse_args(argv)
    if args.child:
        daemon_loop()
        return 0
    parser.print_help()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
