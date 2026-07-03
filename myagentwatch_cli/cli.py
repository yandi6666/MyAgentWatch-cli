#!/usr/bin/env python3
"""MyAgentWatch CLI — Agent 客户端。

用法:
  myaw connect --server http://host:10000 --key myaw_xxx
  myaw status
  myaw chat "你好"
  myaw heartbeat --daemon
"""

import json
import sys
import time
import urllib.request
import os

from .client import connect, get, post, patch, delete, whoami, load_config, save_config, my_name, my_id
from .daemon import (
    daemon_cleanup_dead,
    daemon_logs,
    daemon_queue,
    daemon_runner_status,
    daemon_runner_test,
    daemon_status,
    start_daemon,
    stop_daemon,
)
from .doctor import format_checks, has_failures, run_checks
from .heartbeat import send_heartbeat
from .local_inbox import find_inbox_item, load_inbox, mark_inbox_read
from .monitor import collect_processes, collect_resources, report_processes, report_resources


def _box(text, color="36"):
    """Draw a simple box around text."""
    lines = text.strip().split("\n")
    width = max(len(l) for l in lines) + 2
    print(f"\033[{color}m┌{'─' * (width)}┐\033[0m")
    for line in lines:
        print(f"\033[{color}m│\033[0m {line.ljust(width - 2)} \033[{color}m│\033[0m")
    print(f"\033[{color}m└{'─' * (width)}┘\033[0m")


def _color_status(s):
    colors = {"active": "32", "working": "34", "idle": "33", "error": "31", "blocked": "35", "offline": "37"}
    c = colors.get(s, "37")
    return f"\033[{c}m{s}\033[0m"


def _load_json_value(value, default=None):
    if default is None:
        default = {}
    if not value:
        return default
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value)
    except Exception:
        return default


def _format_time_ms(ms, full: bool = False) -> str:
    try:
        ts = int(ms or 0) / 1000
    except Exception:
        ts = 0
    if full:
        return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts))
    return time.strftime("%m-%d %H:%M", time.localtime(ts))


def _parse_chat_link(link: str) -> tuple[int | None, int | None]:
    parts = (link or "").split(":")
    if len(parts) >= 4 and parts[0] == "chat" and parts[2] == "msg":
        try:
            return int(parts[1]), int(parts[3])
        except ValueError:
            return None, None
    if len(parts) >= 2 and parts[0] == "chat":
        try:
            return int(parts[1]), None
        except ValueError:
            return None, None
    return None, None


def _delivery_label(value: str) -> str:
    labels = {
        "mention": "\u63d0\u53ca",
        "private": "\u79c1\u804a",
        "agent_message": "Agent \u6d88\u606f",
        "friend_request": "\u597d\u53cb\u8bf7\u6c42",
        "task": "\u4efb\u52a1",
        "alert": "\u544a\u8b66",
    }
    return labels.get(value or "", value or "inbox")


def _severity_label(value: str) -> str:
    labels = {"info": "\u666e\u901a", "warning": "\u9ad8", "error": "\u7d27\u6025", "urgent": "\u7d27\u6025"}
    return labels.get(value or "", value or "-")


def _conversation_type_label(value: str) -> str:
    labels = {
        "group": "\u7fa4\u804a",
        "private": "\u79c1\u804a",
        "dm": "\u79c1\u804a",
        "agent_dm": "Agent \u79c1\u804a",
        "system": "\u7cfb\u7edf\u9891\u9053",
    }
    return labels.get(value or "", value or "\u4f1a\u8bdd")


def _attachments_summary(attachments) -> str:
    if not isinstance(attachments, list) or not attachments:
        return "-"
    parts = []
    for item in attachments[:5]:
        if not isinstance(item, dict):
            continue
        typ = item.get("type") or item.get("attachment_type") or "file"
        title = item.get("title") or item.get("url") or item.get("name") or ""
        parts.append(f"{typ}:{title}" if title else str(typ))
    return ", ".join(parts) if parts else "-"


def _inbox_source(item: dict) -> tuple[int | None, int | None]:
    conv_id = item.get("source_conversation_id")
    msg_id = item.get("source_message_id")
    if conv_id and msg_id:
        return int(conv_id), int(msg_id)
    link_conv, link_msg = _parse_chat_link(item.get("link", ""))
    return int(conv_id or link_conv or 0) or None, int(msg_id or link_msg or 0) or None


def _print_inbox_item(item: dict):
    unread = "\u25cf" if not item.get("local_is_read") and not item.get("is_read") else "\u25cb"
    metadata = _load_json_value(item.get("metadata_json"), {})
    conv_id, msg_id = _inbox_source(item)
    source_title = item.get("source_title") or (f"chat:{conv_id}" if conv_id else "-")
    conv_type = metadata.get("conversation_type") or ""
    sender = metadata.get("sender_name") or item.get("source_agent_id") or "-"
    delivery = item.get("delivery_type") or item.get("type") or "inbox"
    body = (item.get("body") or "").strip() or "-"
    attachments = metadata.get("attachments") or item.get("attachments") or []
    location = item.get("link") or (f"chat:{conv_id}:msg:{msg_id}" if conv_id and msg_id else "-")
    print(f"  {unread} #{item.get('id')}  {_format_time_ms(item.get('created_at'), full=True)}")
    print(f"    \u6765\u6e90: {source_title} / {_conversation_type_label(conv_type)}")
    print(f"    \u7c7b\u578b: {_delivery_label(delivery)}")
    print(f"    \u4f18\u5148\u7ea7: {_severity_label(item.get('severity'))}")
    print(f"    \u53d1\u4ef6\u4eba: {sender}")
    print(f"    \u6d88\u606f: {body}")
    print(f"    \u9644\u4ef6: {_attachments_summary(attachments)}")
    print(f"    \u4f4d\u7f6e: {location}")


def _message_from_thread(thread: dict, message_id: int) -> dict | None:
    root = thread.get("root") or {}
    if int(root.get("id") or 0) == int(message_id):
        return root
    for reply in thread.get("replies") or []:
        if int(reply.get("id") or 0) == int(message_id):
            return reply
    return root or None


def _send_reply_to_message(message_id: int, message: str) -> int:
    resp = get(f"/api/chat/messages/{message_id}/thread")
    if "error" in resp:
        print(f"reply failed: {resp}")
        return 2
    thread = resp.get("thread") or {}
    root = thread.get("root") or {}
    target = _message_from_thread(thread, message_id) or root
    conv_id = int(target.get("conversation_id") or root.get("conversation_id") or 0)
    root_id = int(root.get("id") or message_id)
    if not conv_id:
        print("reply failed: missing source conversation")
        return 2
    send_resp = post(f"/api/chat/messages/{conv_id}", {
        "content": message,
        "sender_type": "agent",
        "sender_id": my_id(),
        "sender_name": my_name(),
        "reply_to": message_id,
        "root_id": root_id,
    })
    if "message" not in send_resp:
        print(f"reply failed: {send_resp}")
        return 2
    sent = send_resp["message"]
    print(f"reply sent: #{sent.get('id')} -> chat:{conv_id}:msg:{message_id}")
    return 0


def _color_task_status(s):
    colors = {"queued": "37", "dispatched": "36", "running": "34", "completed": "32", "failed": "31", "cancelled": "33"}
    c = colors.get(s, "37")
    return f"\033[{c}m{s}\033[0m"


def _actor_id():
    return my_id() or my_name() or "myagentwatch-cli"


def _task_time(ts):
    return time.strftime("%m-%d %H:%M", time.localtime((ts or 0) / 1000)) if ts else "--"


def _print_agent_task(task: dict, detail: bool = False):
    status = _color_task_status(task.get("status", "queued"))
    role = task.get("display_agent_role") or f"{task.get('agent_role', 'agent-worker')}/{task.get('agent_name') or task.get('agent_id')}"
    created = _task_time(task.get("created_at"))
    source = "-"
    if task.get("source_conversation_id") and task.get("source_message_id"):
        source = f"chat:{task.get('source_conversation_id')}:msg:{task.get('source_message_id')}"
    elif task.get("source_conversation_id"):
        source = f"chat:{task.get('source_conversation_id')}"
    print(f"  #{task.get('id'):<4} {status:20s} P{task.get('priority', 0):<3} {task.get('task_type', 'reply')}")
    print(f"        {role}  ·  {created}  ·  {source}")
    title = (task.get("title") or "").strip()
    body = (task.get("body") or "").strip()
    if title:
        print(f"        标题: {title}")
    if body:
        print(f"        内容: {body[:220]}")
    if detail:
        print(f"        请求者: {task.get('requester_role', '-')}/{task.get('requester_name') or task.get('requester_id') or '-'}")
        print(f"        所需能力: {', '.join(task.get('required_capabilities') or []) or '-'}")
        print(f"        autostart: {'yes' if task.get('allow_autostart') else 'no'}")
        approval_status = task.get("approval_status") or "not_required"
        print(f"        approval: {approval_status} required={'yes' if task.get('approval_required') else 'no'}")
        if task.get("approved_by"):
            print(f"        approved_by: {task.get('approved_by')} at {_task_time(task.get('approved_at'))}")
        if task.get("rejected_by") or task.get("rejected_reason"):
            print(
                f"        rejected_by: {task.get('rejected_by') or '-'} "
                f"at {_task_time(task.get('rejected_at'))} "
                f"reason={task.get('rejected_reason') or '-'}"
            )
        print(
            f"        runner: attempts={task.get('attempt_count') or 0}/{task.get('max_attempts') or 3} "
            f"lease={_task_time(task.get('lease_expires_at'))}"
        )
        denied = task.get("autostart_denied_reason") or ""
        if denied:
            print(f"        autostart_denied: {denied}")
        if task.get("last_error"):
            print(f"        last_error: {task.get('last_error')[:260]}")
        if task.get("result_text"):
            print(f"        结果: {task.get('result_text')[:260]}")
        if task.get("error_text"):
            print(f"        错误: {task.get('error_text')[:260]}")
        events = task.get("events") or []
        if events:
            print("        events:")
            for event in events[-8:]:
                actor = event.get("actor_id") or "system"
                msg = (event.get("message") or "").strip()
                msg = (" " + msg[:120]) if msg else ""
                print(f"          - {_task_time(event.get('created_at'))} {event.get('event_type')} by {actor}{msg}")
        if task.get("source_message_id"):
            print(f"        上下文: myaw context {task.get('source_message_id')}")
            print(f"        回复: myaw reply {task.get('source_message_id')} \"...\"")


def cmd_tasks(args) -> int:
    """View v3 Agent task queue."""
    action = getattr(args, "tasks_action", "list") or "list"
    agent_id = getattr(args, "agent", None) or my_id() or None

    if action == "list":
        params = [f"limit={getattr(args, 'limit', 50)}"]
        status = getattr(args, "status", "queued")
        if status and status != "all":
            params.append("status=" + status)
        if agent_id:
            params.append("agent_id=" + agent_id)
        resp = get("/api/agent/tasks?" + "&".join(params))
        if "error" in resp:
            print(f"tasks failed: {resp}")
            return 2
        tasks = resp.get("tasks", [])
        if not tasks:
            print("暂无 Agent tasks")
            return 0
        print(f"Agent tasks ({len(tasks)} 个)")
        print("")
        for task in tasks:
            _print_agent_task(task)
        return 0

    if action == "next":
        params = ["status=queued", "limit=1"]
        if agent_id:
            params.append("agent_id=" + agent_id)
        resp = get("/api/agent/tasks?" + "&".join(params))
        tasks = resp.get("tasks", [])
        if "error" in resp:
            print(f"next failed: {resp}")
            return 2
        if not tasks:
            print("没有 queued Agent task")
            return 0
        _print_agent_task(tasks[0], detail=True)
        return 0

    if action == "show":
        resp = get(f"/api/agent/tasks/{args.task_id}")
        task = resp.get("task")
        if not task:
            print(f"show failed: {resp}")
            return 2
        _print_agent_task(task, detail=True)
        return 0

    if action == "cancel":
        resp = post(f"/api/agent/tasks/{args.task_id}/cancel", {"actor_id": _actor_id()})
        task = resp.get("task")
        if not task:
            print(f"cancel failed: {resp}")
            return 2
        print(f"cancelled Agent task #{task.get('id')}")
        return 0

    if action == "approve":
        resp = post(
            f"/api/agent/tasks/{args.task_id}/approve",
            {"actor_id": _actor_id(), "actor_name": my_name() or _actor_id()},
        )
        task = resp.get("task")
        if not task:
            print(f"approve failed: {resp}")
            return 2
        print(f"approved Agent task #{task.get('id')}")
        _print_agent_task(task, detail=True)
        return 0

    if action == "reject":
        resp = post(
            f"/api/agent/tasks/{args.task_id}/reject",
            {
                "actor_id": _actor_id(),
                "actor_name": my_name() or _actor_id(),
                "reason": getattr(args, "reason", "") or "",
            },
        )
        task = resp.get("task")
        if not task:
            print(f"reject failed: {resp}")
            return 2
        print(f"rejected Agent task #{task.get('id')}")
        _print_agent_task(task, detail=True)
        return 0

    if action == "retry":
        resp = post(
            f"/api/agent/tasks/{args.task_id}/retry",
            {"actor_id": _actor_id(), "actor_name": my_name() or _actor_id()},
        )
        task = resp.get("task")
        if not task:
            print(f"retry failed: {resp}")
            return 2
        print(f"retried Agent task #{task.get('id')}")
        _print_agent_task(task, detail=True)
        return 0

    if action == "events":
        resp = get(f"/api/agent/tasks/{args.task_id}/events?limit={getattr(args, 'limit', 200)}")
        events = resp.get("events")
        if events is None:
            print(f"events failed: {resp}")
            return 2
        if not events:
            print(f"Agent task #{args.task_id} 暂无 events")
            return 0
        print(f"Agent task #{args.task_id} events ({len(events)} 个)")
        for event in events:
            actor = event.get("actor_id") or "system"
            msg = (event.get("message") or "").strip()
            msg = (" " + msg[:220]) if msg else ""
            print(f"  - {_task_time(event.get('created_at'))} {event.get('event_type')} by {actor}{msg}")
        return 0

    print("请指定 tasks 子命令: list / next / show / cancel / approve / reject / retry / events")
    return 1


def cmd_connect(server: str, key: str):
    """连接到 MyAgentWatch 服务端。"""
    if connect(server, key):
        info = whoami()
        _box(f"✓ 已连接\n服务: {info['server']}\n版本: {info['version']}\n运行: {info['uptime']}", "32")
    else:
        print("\033[31m连接失败: 请检查 server 地址和 key 是否正确\033[0m")


def cmd_status():
    """显示 Agent 状态仪表盘。"""
    agents = get("/api/agents")
    tokens = get("/api/tokens/dashboard?days=1")
    inbox = get(f"/api/inbox?recipient={my_id() or '天宇'}&limit=1")
    info = whoami()

    lines = ["MyAgentWatch 仪表盘", f"服务: {info['server']} | 版本: {info['version']} | 运行: {info['uptime']}", ""]
    if "agents" in agents:
        lines.append(f"Agent 总数: {len(agents['agents'])}")
        for a in agents["agents"]:
            s = _color_status(a["status"])
            model = a.get("model_id", "?")
            lines.append(f"  {s}  {a['name']:30s} ({model})")

    if "by_model" in tokens:
        lines.append("")
        lines.append("── Token 用量 (今日) ──")
        for m in tokens.get("by_model", [])[:5]:
            total = (m.get("inp", 0) or 0) + (m.get("outp", 0) or 0)
            cost = m.get("cost", 0) or 0
            lines.append(f"  {m['model']:25s} {total:>10,} tokens  ${cost:.4f}")

    if "unread" in inbox:
        lines.append("")
        lines.append(f"📬 未读通知: {inbox['unread']}")

    _box("\n".join(lines), "36")


def cmd_chat(message: str = None, conversation_id: int = None):
    """读群聊或发送消息。"""
    if message:
        resp = post(f"/api/chat/messages/{conversation_id or 1}", {
            "content": message,
            "sender_type": "agent",
            "sender_id": my_id(),
            "sender_name": my_name(),
        })
        if "message" in resp:
            print(f"✓ 已发送: {message[:60]}")
        else:
            print(f"\033[31m发送失败: {resp}\033[0m")
    else:
        resp = get(f"/api/chat/messages/{conversation_id or 1}?limit=20")
        msgs = resp.get("messages", [])
        if not msgs:
            print("暂无消息")
            return
        for m in reversed(msgs):
            sender = m.get("sender_name", "?")
            content = m.get("content", "")[:100]
            t = time.strftime("%H:%M", time.localtime(m.get("timestamp", 0) / 1000))
            marker = "🤖" if m.get("sender_type") == "agent" else "👤"
            print(f"[{t}] {marker} {sender}: {content}")


def _print_chat_message(m: dict):
    sender = m.get("sender_name", "?")
    content = (m.get("content") or "").replace("\r", "").strip()
    t = time.strftime("%H:%M:%S", time.localtime((m.get("timestamp") or 0) / 1000))
    marker = "🤖" if m.get("sender_type") == "agent" else "👤"
    print(f"[{t}] #{m.get('id')} {marker} {sender}: {content}")


def cmd_conversations(args=None):
    """列出 MyAgentWatch 会话。"""
    participant_type = getattr(args, "participant_type", None) if args else None
    participant_id = getattr(args, "participant_id", None) if args else None
    if getattr(args, "mine", False):
        participant_type = participant_type or "agent"
        participant_id = participant_id or my_id()
    elif not participant_type and not participant_id and my_id():
        participant_type = "agent"
        participant_id = my_id()
    params = []
    if participant_type and participant_id:
        params.append("participant_type=" + participant_type)
        params.append("participant_id=" + participant_id)
    path = "/api/chat/conversations" + (("?" + "&".join(params)) if params else "")
    resp = get(path)
    convs = resp.get("conversations", [])
    if not convs:
        print("暂无会话")
        return
    print(f"会话列表 ({len(convs)} 个)")
    for conv in convs:
        conv_id = conv.get("id")
        title = conv.get("title") or "未命名"
        conv_type = conv.get("type") or "unknown"
        unread = conv.get("unread_count") or 0
        mentions = conv.get("mention_count") or 0
        tasks = conv.get("pending_task_count") or conv.get("task_count") or 0
        last = (conv.get("last_message") or "")[:50]
        print(f"  #{conv_id:<4} {conv_type:8s} unread={unread:<3} @={mentions:<3} tasks={tasks:<3} {title}  {last}")


def cmd_mentions(args):
    participant_type = args.participant_type or "agent"
    participant_id = args.participant_id or my_id()
    if not participant_id:
        print("missing participant_id; use --participant-id or connect CLI as an Agent")
        return 2
    unread = "1" if args.unread else "0"
    resp = get(
        f"/api/chat/mentions?participant_type={participant_type}"
        f"&participant_id={participant_id}&unread={unread}&limit={args.limit}"
    )
    if "error" in resp:
        print(f"mentions failed: {resp}")
        return 2
    mentions = resp.get("mentions", [])
    if not mentions:
        print("暂无提及")
        return 0
    print(f"提及 ({len(mentions)} 条)")
    for item in mentions:
        msg = item.get("message") or {}
        conv = item.get("conversation") or {}
        marker = "●" if not item.get("is_read") else "○"
        t = _format_time_ms(item.get("created_at"), full=True)
        print(f"  {marker} #{item.get('id')}  {t}")
        print(f"    来源: #{conv.get('id')} {conv.get('title') or '-'} / {_conversation_type_label(conv.get('type'))}")
        print(f"    消息: #{msg.get('id')} {msg.get('sender_name') or '-'}: {(msg.get('content') or '')[:180]}")
        tasks = msg.get("tasks") or []
        if tasks:
            print("    tasks: " + ", ".join(f"#{t.get('id')}:{t.get('status')}" for t in tasks))
    return 0


def cmd_context(args):
    resp = get(f"/api/chat/messages/{args.message_id}/context")
    context = resp.get("context")
    if not context:
        print(f"context failed: {resp}")
        return 2
    msg = context.get("message") or {}
    conv = context.get("conversation") or {}
    thread = context.get("thread") or {}
    summary = thread.get("summary") or {}
    print(f"消息上下文 #{args.message_id}")
    print(f"  会话: #{conv.get('id')} {conv.get('title') or '-'} / {_conversation_type_label(conv.get('type'))}")
    print(f"  发件人: {msg.get('sender_name') or '-'} ({msg.get('sender_type') or '-'})")
    print(f"  时间: {_format_time_ms(msg.get('timestamp'), full=True)}")
    print(f"  内容: {(msg.get('content') or '').strip() or '-'}")
    print(f"  附件: {_attachments_summary(msg.get('attachments'))}")
    print(f"  线程: root=#{thread.get('thread_id')} replies={summary.get('reply_count', 0)}")
    participants = summary.get("participants") or []
    if participants:
        print("  参与者: " + ", ".join(p.get("sender_name") or p.get("sender_id") or "-" for p in participants))
    mentions = context.get("mentions") or []
    if mentions:
        print("  提及: " + ", ".join(f"{m.get('participant_type')}/{m.get('participant_id')}:{m.get('mention_type')}" for m in mentions))
    tasks = context.get("tasks") or []
    if tasks:
        print("  Agent tasks:")
        for task in tasks:
            _print_agent_task(task, detail=True)
    inbox = context.get("inbox") or []
    if inbox:
        print("  Inbox: " + ", ".join(f"#{i.get('id')}:{i.get('delivery_type') or i.get('type')}" for i in inbox))
    return 0


def cmd_watch(args):
    """前台持续查看会话新消息。"""
    conv_id = args.conv or 1
    interval = max(float(args.interval), 1.0)
    resp = get(f"/api/chat/messages/{conv_id}?limit={args.limit}")
    msgs = resp.get("messages", [])
    last_id = 0
    for msg in msgs:
        _print_chat_message(msg)
        last_id = max(last_id, int(msg.get("id") or 0))
    print(f"watching conversation #{conv_id}, interval={interval}s. Ctrl+C 停止.")
    try:
        while True:
            time.sleep(interval)
            resp = get(f"/api/chat/messages/{conv_id}?after_id={last_id}&limit=50")
            if "error" in resp:
                print(f"watch error: {resp.get('error')}")
                continue
            for msg in resp.get("messages", []):
                _print_chat_message(msg)
                last_id = max(last_id, int(msg.get("id") or 0))
    except KeyboardInterrupt:
        print("\nwatch stopped")


def _load_server_inbox_item(item_id: int) -> dict | None:
    recipient = my_id() or "\u5929\u5b87"
    resp = get(f"/api/inbox?recipient={recipient}&limit=200")
    for item in resp.get("items", []) if isinstance(resp, dict) else []:
        if int(item.get("id") or 0) == int(item_id):
            return item
    return None


def cmd_inbox(args):
    """View, mark read, or reply to daemon-synced Agent inbox items."""
    action = args.inbox_action or "list"
    if action == "read":
        if args.item_id is None:
            print("missing inbox item id: myaw inbox read <id>")
            return 2
        changed = mark_inbox_read(args.item_id)
        post(f"/api/inbox/read/{args.item_id}")
        print(f"inbox item #{args.item_id} marked read" if changed else f"inbox item #{args.item_id} not found locally")
        return 0

    if action == "reply":
        if args.item_id is None or not args.reply_message:
            print("usage: myaw inbox reply <id> \"message\"")
            return 2
        item = find_inbox_item(args.item_id) or _load_server_inbox_item(args.item_id)
        if not item:
            print(f"inbox item #{args.item_id} not found")
            return 2
        conv_id, msg_id = _inbox_source(item)
        if msg_id:
            code = _send_reply_to_message(msg_id, args.reply_message)
        elif conv_id:
            resp = post(f"/api/chat/messages/{conv_id}", {
                "content": args.reply_message,
                "sender_type": "agent",
                "sender_id": my_id(),
                "sender_name": my_name(),
            })
            if "message" not in resp:
                print(f"reply failed: {resp}")
                return 2
            print(f"reply sent: #{resp['message'].get('id')} -> chat:{conv_id}")
            code = 0
        else:
            print("reply failed: inbox item has no chat source")
            return 2
        if code == 0:
            mark_inbox_read(args.item_id)
            post(f"/api/inbox/read/{args.item_id}")
        return code

    unread_only = action == "unread"
    rows = load_inbox(unread_only=unread_only, limit=args.limit)
    if not rows:
        print("\u6682\u65e0\u672a\u8bfb inbox" if unread_only else "\u6682\u65e0 inbox")
        return 0
    heading_prefix = "\u672a\u8bfb " if unread_only else ""
    print(f"{heading_prefix}Agent inbox ({len(rows)} \u6761)")
    for item in rows:
        _print_inbox_item(item)
    return 0


def cmd_thread(args):
    resp = get(f"/api/chat/messages/{args.message_id}/thread?limit={args.limit}")
    if "error" in resp:
        print(f"thread failed: {resp}")
        return 2
    thread = resp.get("thread") or {}
    root = thread.get("root") or {}
    replies = thread.get("replies") or []
    print(f"thread #{thread.get('thread_id')} ({len(replies)} replies)")
    print("root:")
    _print_chat_message(root)
    if replies:
        print("replies:")
        for reply in replies:
            _print_chat_message(reply)
    return 0


def cmd_reply(args):
    return _send_reply_to_message(args.message_id, args.message)


def cmd_heartbeat(agent_id: str = None, status: str = "active", daemon: bool = False):
    """发送心跳。--daemon 模式每 15s 自动发送。"""
    cfg = load_config()
    if not agent_id:
        agent_id = cfg.get("agent_id")
    if not agent_id:
        print("请指定 --agent-id (例如: myaw heartbeat --agent-id \"claude-code:Claude Code:xxx\")")
        return

    def _beat():
        result = send_heartbeat(agent_id, status=status)
        t = time.strftime("%H:%M:%S")
        if result.get("ok"):
            print(f"[{t}] ♡ 心跳: {result.get('status', '?')}")
        else:
            print(f"[{t}] \033[31m心跳失败: {result.get('response') or result}\033[0m")

    if daemon:
        print(f"守护模式启动 (agent: {agent_id}, 间隔: 15s). Ctrl+C 停止.")
        try:
            while True:
                _beat()
                time.sleep(15)
        except KeyboardInterrupt:
            print("\n守护已停止")
    else:
        _beat()


def cmd_doctor(json_output: bool = False):
    """检查 CLI 与 MyAgentWatch 的连接、身份和本机采集能力。"""
    checks = run_checks()
    if json_output:
        print(json.dumps({"ok": not has_failures(checks), "checks": checks}, ensure_ascii=False, indent=2))
    else:
        _box(format_checks(checks), "36" if not has_failures(checks) else "33")
    if has_failures(checks):
        sys.exit(1)


def _print_resources(snapshot: dict):
    lines = [
        "资源快照",
        f"Agent: {snapshot.get('agent_id') or '(未配置)'}",
        f"CPU: {snapshot.get('cpu_pct')}%",
        f"内存: {snapshot.get('memory_pct')}%  {snapshot.get('memory_used_mb')} / {snapshot.get('memory_total_mb')} MB",
        f"磁盘: {snapshot.get('disk_pct')}%  {snapshot.get('disk_used_gb')} / {snapshot.get('disk_total_gb')} GB",
        f"GPU: {snapshot.get('gpu_pct') if snapshot.get('gpu_pct') is not None else '--'}%",
        f"网络: ↑ {snapshot.get('net_sent_bytes')} bytes  ↓ {snapshot.get('net_recv_bytes')} bytes",
    ]
    _box("\n".join(lines), "36")


def _print_processes(snapshot: dict):
    rows = snapshot.get("processes", [])
    lines = [f"Agent 相关进程 ({len(rows)} 个)", f"Agent: {snapshot.get('agent_id') or '(未配置)'}", ""]
    if not rows:
        lines.append("未检测到相关进程")
    for item in rows[:30]:
        lines.append(
            f"{str(item.get('pid', '')):>7s}  {item.get('process_name','')[:24]:24s} "
            f"{item.get('detected_role','')[:18]:18s} "
            f"{item.get('memory_mb', 0):>8} MB"
        )
    print("\n".join(lines))


def cmd_monitor(args):
    """采集并可选上报本机监控快照。"""
    target = getattr(args, "monitor_target", "")
    agent_id = args.agent_id or my_id()
    if target == "resources":
        snapshot = collect_resources(agent_id=agent_id)
        report = report_resources(snapshot) if args.report else None
        if args.json:
            print(json.dumps({"snapshot": snapshot, "report": report} if report else snapshot, ensure_ascii=False, indent=2))
        else:
            _print_resources(snapshot)
            if report:
                print(f"✓ 已上报资源快照: {report}")
        if report and "error" in report:
            sys.exit(1)
        return

    if target in {"process", "processes"}:
        snapshot = collect_processes(agent_id=agent_id)
        report = report_processes(snapshot) if args.report else None
        if args.json:
            print(json.dumps({"snapshot": snapshot, "report": report} if report else snapshot, ensure_ascii=False, indent=2))
        else:
            _print_processes(snapshot)
            if report:
                print(f"✓ 已上报进程快照: {report}")
        if report and "error" in report:
            sys.exit(1)
        return

    print("请指定 monitor 子命令: resources / process")
    sys.exit(2)


def cmd_daemon(args):
    """管理 myagentwatch-cli 后台 daemon。"""
    action = getattr(args, "daemon_action", "")
    if action == "start":
        return start_daemon(foreground=args.foreground)
    if action == "stop":
        return stop_daemon(force=args.force)
    if action == "restart":
        code = stop_daemon(force=args.force)
        if code:
            return code
        time.sleep(1)
        return start_daemon(foreground=False)
    if action == "status":
        return daemon_status(json_output=args.json)
    if action == "queue":
        return daemon_queue(json_output=args.json)
    if action == "cleanup-dead":
        return daemon_cleanup_dead()
    if action == "logs":
        return daemon_logs(lines=args.lines, follow=args.follow)
    print("请指定 daemon 子命令: start / stop / restart / status / queue / cleanup-dead / logs")
    return 2


def cmd_runner(args) -> int:
    """Inspect daemon runner policy without executing tasks."""
    action = getattr(args, "runner_action", "") or "status"
    if action == "status":
        return daemon_runner_status(json_output=getattr(args, "json", False))
    if action == "test":
        return daemon_runner_test(task_id=getattr(args, "task_id", None), json_output=getattr(args, "json", False))
    print("请指定 runner 子命令: status / test")
    return 2


def cmd_tokens(days: int = 7):
    """查看 Token 用量。"""
    dashboard = get(f"/api/tokens/dashboard?days={days}")
    by_agent = get(f"/api/tokens/by-agent?days={days}")
    unmapped = get("/api/tokens/unmapped")

    lines = [f"Token 用量 (最近 {days} 天)", ""]

    if "by_day" in dashboard:
        lines.append("── 按日 ──")
        for d in dashboard["by_day"]:
            total = (d.get("inp", 0) or 0) + (d.get("outp", 0) or 0)
            cost = d.get("cost", 0) or 0
            lines.append(f"  {d['day']}  {total:>10,} tokens  ${cost:.4f}")

    if "agents" in by_agent:
        lines.append("")
        lines.append("── 按 Agent ──")
        for a in by_agent["agents"][:5]:
            name_parts = a["agent_id"].split(":")
            name = name_parts[-1] if len(name_parts) > 1 else a["agent_id"]
            total = (a.get("inp", 0) or 0) + (a.get("outp", 0) or 0)
            cost = a.get("cost", 0) or 0
            lines.append(f"  {name:30s} {total:>10,} tokens  ${cost:.4f}  ({a.get('task_count', 0)} 任务)")

    if unmapped.get("count", 0) > 0:
        lines.append("")
        lines.append(f"⚠ 未定价模型: {unmapped['count']} 个")
        for m in unmapped.get("unmapped", []):
            lines.append(f"  {m['model_id']} ({m['total_tokens']:,} tokens)")

    _box("\n".join(lines), "36")


def cmd_agents():
    """列出所有 Agent。"""
    resp = get("/api/agents")
    users = get("/api/users")
    agents = resp.get("agents", [])
    user_map = {}
    for u in users.get("users", []):
        user_map[u["id"]] = u

    lines = [f"Agent 列表 ({len(agents)} 个)", ""]
    for a in agents:
        s = _color_status(a["status"])
        token_info = ""
        u = user_map.get(a["id"])
        if u and u.get("token_prefix"):
            token_info = f" 🔑"
        lines.append(f"  {s}  {a['name']:30s} ({a.get('model_id','?')}){token_info}")

    print("\n".join(lines))


def cmd_task(args):
    """管理任务生命周期。"""
    action = getattr(args, "task_action", None)
    if action == "list":
        query = f"/api/tasks?status={args.status}&limit={args.limit}"
        if args.agent:
            query += "&agent_id=" + args.agent
        resp = get(query)
        tasks = resp.get("tasks", [])
        if not tasks:
            print("暂无任务")
            return
        lines = [f"任务列表 ({len(tasks)} 个)", ""]
        for t in tasks:
            status = _color_task_status(t.get("status", "queued"))
            agent = t.get("assigned_agent_id") or "未指派"
            updated = _task_time(t.get("updated_at"))
            lines.append(f"  #{t['id']:<4} {status:20s} P{t.get('priority', 0)}  {t.get('title', '')[:42]}")
            lines.append(f"        {agent}  ·  {updated}")
        print("\n".join(lines))
        return

    if action == "create":
        body = {
            "title": args.title,
            "description": args.description or "",
            "assigned_agent_id": args.agent or "",
            "priority": args.priority,
            "actor_id": _actor_id(),
        }
        resp = post("/api/tasks", body)
        task = resp.get("task")
        if task:
            print(f"✓ 任务已创建: #{task['id']} {task['title']}")
        else:
            print(f"\033[31m创建失败: {resp}\033[0m")
        return

    status_map = {
        "start": "running",
        "complete": "completed",
        "fail": "failed",
        "cancel": "cancelled",
        "queue": "queued",
    }
    if action in status_map:
        resp = patch(f"/api/tasks/{args.task_id}/status", {
            "status": status_map[action],
            "actor_id": _actor_id(),
            "message": args.message or "",
        })
        task = resp.get("task")
        if task:
            print(f"✓ 任务 #{task['id']} → {task['status']}: {task['title']}")
        else:
            print(f"\033[31m更新失败: {resp}\033[0m")
        return

    print("请指定 task 子命令: list / create / start / complete / fail / cancel / queue")


def cmd_feed():
    """查看 Agent 动态流（收件箱）。"""
    resp = get("/api/inbox?limit=30")
    items = resp.get("items", [])
    if not items:
        print("暂无动态")
        return

    lines = [f"📡 动态流 ({resp.get('unread', 0)} 未读)", ""]
    for it in items:
        icon = {"agent_message": "💬", "friend_request": "🤝", "alert": "🚨", "share_task": "📤"}.get(it.get("type"), "📌")
        unread = "●" if not it.get("is_read") else "○"
        t = time.strftime("%m-%d %H:%M", time.localtime(it.get("created_at", 0) / 1000))
        lines.append(f"  {unread} {icon} [{t}] {it['title']}")
        if it.get("body"):
            lines.append(f"       {it['body'][:100]}")

    print("\n".join(lines))


def cmd_post(content: str):
    """发布 Agent 动态到群聊。"""
    resp = post("/api/chat/agent-message", {
        "content": content,
        "agent_id": my_id(),
        "agent_name": my_name(),
    })
    if "message" in resp:
        print(f"✓ 已发布: {content[:60]}")
    else:
        print(f"\033[31m发布失败: {resp}\033[0m")


def cmd_friend(agent_id: str, message: str = ""):
    """发送好友请求。"""
    resp = post("/api/chat/friend-request", {
        "from_agent_id": agent_id,
        "from_agent_name": my_name(),
        "message": message or f"Agent 请求添加好友",
    })
    if "request" in resp:
        print(f"✓ 好友请求已发送 → {agent_id}")
    else:
        print(f"\033[31m请求失败: {resp}\033[0m")


def cmd_share(title: str, summary: str = ""):
    """分享任务成果到群聊。"""
    resp = post("/api/chat/share-task/1", {
        "task_title": title,
        "result_summary": summary,
        "agent_id": my_id(),
        "agent_name": my_name(),
    })
    if "message" in resp:
        print(f"✓ 已分享: {title}")
    else:
        print(f"\033[31m分享失败: {resp}\033[0m")


def cmd_dashboard():
    """终端版仪表盘。"""
    cmd_status()
    print("")
    cmd_feed()


def main():
    import argparse

    parser = argparse.ArgumentParser(description="MyAgentWatch CLI — Agent 客户端")
    sub = parser.add_subparsers(dest="command")

    p = sub.add_parser("connect")
    p.add_argument("--server", required=True)
    p.add_argument("--key", required=True)

    sub.add_parser("status")
    sub.add_parser("dashboard")
    sub.add_parser("agents")
    sub.add_parser("feed")
    p = sub.add_parser("conversations")
    p.add_argument("--participant-type", choices=["human", "agent"], default=None)
    p.add_argument("--participant-id", default=None)
    p.add_argument("--mine", action="store_true")

    p = sub.add_parser("doctor")
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("monitor")
    monitor_sub = p.add_subparsers(dest="monitor_target")

    p_mon_res = monitor_sub.add_parser("resources")
    p_mon_res.add_argument("--json", action="store_true")
    p_mon_res.add_argument("--report", action="store_true")
    p_mon_res.add_argument("--agent-id", default=None)

    for monitor_name in ("process", "processes"):
        p_mon_proc = monitor_sub.add_parser(monitor_name)
        p_mon_proc.add_argument("--json", action="store_true")
        p_mon_proc.add_argument("--report", action="store_true")
        p_mon_proc.add_argument("--agent-id", default=None)

    p = sub.add_parser("daemon")
    daemon_sub = p.add_subparsers(dest="daemon_action")

    p_daemon_start = daemon_sub.add_parser("start")
    p_daemon_start.add_argument("--foreground", action="store_true")

    p_daemon_stop = daemon_sub.add_parser("stop")
    p_daemon_stop.add_argument("--force", action="store_true")

    p_daemon_restart = daemon_sub.add_parser("restart")
    p_daemon_restart.add_argument("--force", action="store_true")

    p_daemon_status = daemon_sub.add_parser("status")
    p_daemon_status.add_argument("--json", action="store_true")

    p_daemon_queue = daemon_sub.add_parser("queue")
    p_daemon_queue.add_argument("--json", action="store_true")

    daemon_sub.add_parser("cleanup-dead")

    p_daemon_logs = daemon_sub.add_parser("logs")
    p_daemon_logs.add_argument("--lines", type=int, default=50)
    p_daemon_logs.add_argument("--follow", action="store_true")

    p = sub.add_parser("runner")
    runner_sub = p.add_subparsers(dest="runner_action")

    p_runner_status = runner_sub.add_parser("status")
    p_runner_status.add_argument("--json", action="store_true")

    p_runner_test = runner_sub.add_parser("test")
    p_runner_test.add_argument("--task", dest="task_id", type=int, default=None)
    p_runner_test.add_argument("--json", action="store_true")

    p = sub.add_parser("chat")
    p.add_argument("message", nargs="?", default=None)
    p.add_argument("--conv", type=int, default=None)

    p = sub.add_parser("thread")
    p.add_argument("message_id", type=int)
    p.add_argument("--limit", type=int, default=100)

    p = sub.add_parser("context")
    p.add_argument("message_id", type=int)

    p = sub.add_parser("mentions")
    p.add_argument("--participant-type", choices=["human", "agent"], default="agent")
    p.add_argument("--participant-id", default=None)
    p.add_argument("--unread", action="store_true")
    p.add_argument("--limit", type=int, default=50)

    p = sub.add_parser("reply")
    p.add_argument("message_id", type=int)
    p.add_argument("message")

    p = sub.add_parser("watch")
    p.add_argument("--conv", type=int, default=1)
    p.add_argument("--interval", type=float, default=3)
    p.add_argument("--limit", type=int, default=20)

    p = sub.add_parser("inbox")
    p.add_argument("inbox_action", nargs="?", choices=["list", "unread", "read", "reply"], default="list")
    p.add_argument("item_id", nargs="?", type=int)
    p.add_argument("reply_message", nargs="?")
    p.add_argument("--limit", type=int, default=30)

    p = sub.add_parser("post")
    p.add_argument("content")

    p = sub.add_parser("heartbeat")
    p.add_argument("--agent-id", default=None)
    p.add_argument("--status", default="active")
    p.add_argument("--daemon", action="store_true")

    p = sub.add_parser("tokens")
    p.add_argument("--days", type=int, default=7)

    p = sub.add_parser("task")
    task_sub = p.add_subparsers(dest="task_action")

    p_task_list = task_sub.add_parser("list")
    p_task_list.add_argument("--status", default="open", choices=["all", "open", "queued", "dispatched", "running", "completed", "failed", "cancelled"])
    p_task_list.add_argument("--agent", default=None)
    p_task_list.add_argument("--limit", type=int, default=50)

    p_task_create = task_sub.add_parser("create")
    p_task_create.add_argument("title")
    p_task_create.add_argument("--description", "--desc", default="")
    p_task_create.add_argument("--agent", default=None)
    p_task_create.add_argument("--priority", type=int, default=0)

    for action in ("start", "complete", "fail", "cancel", "queue"):
        p_task_status = task_sub.add_parser(action)
        p_task_status.add_argument("task_id", type=int)
        p_task_status.add_argument("--message", default="")

    p = sub.add_parser("tasks")
    tasks_sub = p.add_subparsers(dest="tasks_action")

    p_tasks_list = tasks_sub.add_parser("list")
    p_tasks_list.add_argument("--status", default="queued", choices=["all", "queued", "claimed", "running", "completed", "failed", "cancelled"])
    p_tasks_list.add_argument("--agent", default=None)
    p_tasks_list.add_argument("--limit", type=int, default=50)

    p_tasks_next = tasks_sub.add_parser("next")
    p_tasks_next.add_argument("--agent", default=None)

    p_tasks_show = tasks_sub.add_parser("show")
    p_tasks_show.add_argument("task_id", type=int)
    p_tasks_show.add_argument("--agent", default=None)

    p_tasks_cancel = tasks_sub.add_parser("cancel")
    p_tasks_cancel.add_argument("task_id", type=int)
    p_tasks_cancel.add_argument("--agent", default=None)

    p_tasks_approve = tasks_sub.add_parser("approve")
    p_tasks_approve.add_argument("task_id", type=int)
    p_tasks_approve.add_argument("--agent", default=None)

    p_tasks_reject = tasks_sub.add_parser("reject")
    p_tasks_reject.add_argument("task_id", type=int)
    p_tasks_reject.add_argument("--reason", default="")
    p_tasks_reject.add_argument("--agent", default=None)

    p_tasks_retry = tasks_sub.add_parser("retry")
    p_tasks_retry.add_argument("task_id", type=int)
    p_tasks_retry.add_argument("--agent", default=None)

    p_tasks_events = tasks_sub.add_parser("events")
    p_tasks_events.add_argument("task_id", type=int)
    p_tasks_events.add_argument("--limit", type=int, default=200)
    p_tasks_events.add_argument("--agent", default=None)

    p = sub.add_parser("friend")
    p.add_argument("agent_id")
    p.add_argument("message", nargs="?", default="")

    p = sub.add_parser("share")
    p.add_argument("title")
    p.add_argument("summary", nargs="?", default="")

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        return

    if args.command == "connect":
        cmd_connect(args.server, args.key)
    elif args.command == "status":
        cmd_status()
    elif args.command == "dashboard":
        cmd_dashboard()
    elif args.command == "doctor":
        cmd_doctor(args.json)
    elif args.command == "monitor":
        cmd_monitor(args)
    elif args.command == "daemon":
        code = cmd_daemon(args)
        if code:
            sys.exit(code)
    elif args.command == "runner":
        code = cmd_runner(args)
        if code:
            sys.exit(code)
    elif args.command == "chat":
        cmd_chat(args.message, args.conv)
    elif args.command == "thread":
        code = cmd_thread(args)
        if code:
            sys.exit(code)
    elif args.command == "context":
        code = cmd_context(args)
        if code:
            sys.exit(code)
    elif args.command == "mentions":
        code = cmd_mentions(args)
        if code:
            sys.exit(code)
    elif args.command == "reply":
        code = cmd_reply(args)
        if code:
            sys.exit(code)
    elif args.command == "conversations":
        cmd_conversations(args)
    elif args.command == "watch":
        cmd_watch(args)
    elif args.command == "inbox":
        code = cmd_inbox(args)
        if code:
            sys.exit(code)
    elif args.command == "heartbeat":
        cmd_heartbeat(args.agent_id, args.status, args.daemon)
    elif args.command == "tokens":
        cmd_tokens(args.days)
    elif args.command == "task":
        cmd_task(args)
    elif args.command == "tasks":
        code = cmd_tasks(args)
        if code:
            sys.exit(code)
    elif args.command == "agents":
        cmd_agents()
    elif args.command == "feed":
        cmd_feed()
    elif args.command == "post":
        cmd_post(args.content)
    elif args.command == "friend":
        cmd_friend(args.agent_id, args.message)
    elif args.command == "share":
        cmd_share(args.title, args.summary)


if __name__ == "__main__":
    main()
