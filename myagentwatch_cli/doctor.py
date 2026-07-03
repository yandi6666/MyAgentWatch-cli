"""Environment self-checks for myagentwatch-cli."""

from __future__ import annotations

from .client import get, load_config
from .heartbeat import send_heartbeat
from .monitor import collect_processes, collect_resources


def _pass(name: str, message: str, detail=None) -> dict:
    return {"name": name, "status": "PASS", "message": message, "detail": detail}


def _fail(name: str, message: str, detail=None) -> dict:
    return {"name": name, "status": "FAIL", "message": message, "detail": detail}


def _warn(name: str, message: str, detail=None) -> dict:
    return {"name": name, "status": "WARN", "message": message, "detail": detail}


def run_checks() -> list[dict]:
    cfg = load_config()
    checks: list[dict] = []

    server = cfg.get("server", "")
    key = cfg.get("key", "")
    agent_id = cfg.get("agent_id", "")
    agent_name = cfg.get("agent_name", "")

    checks.append(
        _pass("config.server", f"server = {server}") if server
        else _fail("config.server", "server 未配置，请先执行 myaw connect")
    )
    checks.append(
        _pass("config.key", f"PAT = {key[:10]}...") if key
        else _fail("config.key", "PAT 未配置，请先执行 myaw connect")
    )
    checks.append(
        _pass("config.identity", f"{agent_name or '(未命名)'} / {agent_id}") if agent_id
        else _fail("config.identity", "agent_id 未配置，无法用真实 Agent 身份上报")
    )

    status = get("/api/status")
    if "error" in status:
        checks.append(_fail("server.status", "MyAgentWatch 服务不可用", status))
    else:
        checks.append(_pass("server.status", f"version={status.get('version', '?')} uptime={status.get('uptime', '?')}"))

    users = get("/api/users")
    if "users" not in users:
        checks.append(_fail("auth.pat", "无法读取用户列表，PAT/服务状态异常", users))
    elif not key:
        checks.append(_fail("auth.pat", "PAT 缺失"))
    else:
        prefixes = [u.get("token_prefix") for u in users.get("users", []) if u.get("token_prefix")]
        if any(key.startswith(prefix) for prefix in prefixes):
            checks.append(_pass("auth.pat", "PAT 前缀已在 MyAgentWatch 用户表中登记"))
        else:
            checks.append(_warn("auth.pat", "未在用户表中找到当前 PAT 前缀，可能需要重新 connect"))

    heartbeat = send_heartbeat(status="active", metadata={"source": "doctor"})
    if heartbeat.get("ok"):
        checks.append(_pass("heartbeat", f"心跳已发送：{heartbeat.get('agent_id')}"))
    else:
        checks.append(_fail("heartbeat", "心跳发送失败", heartbeat.get("response") or heartbeat))

    try:
        resources = collect_resources(agent_id=agent_id)
        detail = {
            "cpu_pct": resources.get("cpu_pct"),
            "memory_pct": resources.get("memory_pct"),
            "disk_pct": resources.get("disk_pct"),
            "collector_backend": resources.get("collector_backend"),
            "collector_warning": resources.get("collector_warning"),
        }
        if resources.get("collector_backend") == "psutil":
            checks.append(_pass("monitor.resources", "资源采集可用 (psutil)", detail))
        else:
            checks.append(_warn(
                "monitor.resources",
                f"资源采集可用（{resources.get('collector_backend', 'fallback')} 低精度模式）",
                detail,
            ))
    except Exception as exc:
        checks.append(_fail("monitor.resources", str(exc)))

    try:
        processes = collect_processes(agent_id=agent_id)
        detail = {
            "collector_backend": processes.get("collector_backend"),
            "collector_warning": processes.get("collector_warning"),
        }
        status = _pass
        message = f"检测到 {len(processes.get('processes', []))} 个相关进程"
        if processes.get("collector_backend") and processes.get("collector_backend") != "psutil":
            status = _warn
            message += f"（{processes.get('collector_backend')} 低精度模式）"
        checks.append(status("monitor.process", message, detail))
    except Exception as exc:
        checks.append(_fail("monitor.process", str(exc)))

    return checks


def has_failures(checks: list[dict]) -> bool:
    return any(item.get("status") == "FAIL" for item in checks)


def format_checks(checks: list[dict]) -> str:
    lines = ["MyAgentWatch CLI doctor", ""]
    for item in checks:
        mark = {"PASS": "✓", "WARN": "!", "FAIL": "✗"}.get(item["status"], "?")
        lines.append(f"{mark} {item['status']:4s} {item['name']}: {item['message']}")
    return "\n".join(lines)
