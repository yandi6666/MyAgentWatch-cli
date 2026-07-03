"""Local monitoring collectors for myagentwatch-cli."""

from __future__ import annotations

import os
import json
import shutil
import subprocess
import time

from .client import my_id, post

AGENT_KEYWORDS = (
    "myagentwatch",
    "codex",
    "claude",
    "deepseek",
    "openai",
    "opencode",
    "gemini",
    "cursor",
    "copilot",
    "qwen",
    "kimi",
)


def now_ms() -> int:
    return int(time.time() * 1000)


def _load_psutil():
    try:
        import psutil  # type: ignore
    except Exception as exc:
        return None, exc
    return psutil, None


def _powershell_json(command: str):
    try:
        completed = subprocess.run(
            ["powershell", "-NoProfile", "-Command", command],
            capture_output=True,
            text=True,
            timeout=5,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except Exception:
        return None
    if completed.returncode != 0 or not completed.stdout.strip():
        return None
    try:
        return json.loads(completed.stdout)
    except json.JSONDecodeError:
        return completed.stdout.strip()
    

def _windows_memory():
    if os.name != "nt":
        return None
    import ctypes

    class MEMORYSTATUSEX(ctypes.Structure):
        _fields_ = [
            ("dwLength", ctypes.c_ulong),
            ("dwMemoryLoad", ctypes.c_ulong),
            ("ullTotalPhys", ctypes.c_ulonglong),
            ("ullAvailPhys", ctypes.c_ulonglong),
            ("ullTotalPageFile", ctypes.c_ulonglong),
            ("ullAvailPageFile", ctypes.c_ulonglong),
            ("ullTotalVirtual", ctypes.c_ulonglong),
            ("ullAvailVirtual", ctypes.c_ulonglong),
            ("sullAvailExtendedVirtual", ctypes.c_ulonglong),
        ]

    stat = MEMORYSTATUSEX()
    stat.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
    if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(stat)):
        return None
    used = stat.ullTotalPhys - stat.ullAvailPhys
    return {
        "percent": float(stat.dwMemoryLoad),
        "used_mb": round(used / 1024 / 1024, 2),
        "total_mb": round(stat.ullTotalPhys / 1024 / 1024, 2),
    }


def _fallback_cpu_pct():
    data = _powershell_json(
        "(Get-Counter '\\Processor(_Total)\\% Processor Time').CounterSamples[0].CookedValue | ConvertTo-Json"
    )
    try:
        return round(float(data), 2)
    except (TypeError, ValueError):
        return None
    return psutil


def _system_disk_path() -> str:
    if os.name == "nt":
        return os.environ.get("SystemDrive", "C:") + "\\"
    return "/"


def _gpu_snapshot() -> tuple[float | None, float | None]:
    cmd = [
        "nvidia-smi",
        "--query-gpu=utilization.gpu,memory.used",
        "--format=csv,noheader,nounits",
    ]
    try:
        completed = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=2,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except Exception:
        return None, None
    if completed.returncode != 0 or not completed.stdout.strip():
        return None, None
    first = completed.stdout.strip().splitlines()[0]
    parts = [p.strip() for p in first.split(",")]
    try:
        gpu_pct = float(parts[0]) if parts else None
        gpu_mem = float(parts[1]) if len(parts) > 1 else None
        return gpu_pct, gpu_mem
    except ValueError:
        return None, None


def collect_resources(agent_id: str | None = None) -> dict:
    psutil, psutil_error = _load_psutil()
    if psutil:
        vm = psutil.virtual_memory()
        disk = psutil.disk_usage(_system_disk_path())
        net = psutil.net_io_counters()
        cpu_pct = psutil.cpu_percent(interval=0.2)
        memory_pct = vm.percent
        memory_used_mb = round((vm.total - vm.available) / 1024 / 1024, 2)
        memory_total_mb = round(vm.total / 1024 / 1024, 2)
        net_sent = net.bytes_sent
        net_recv = net.bytes_recv
    else:
        memory = _windows_memory()
        disk = shutil.disk_usage(_system_disk_path())
        cpu_pct = _fallback_cpu_pct()
        memory_pct = memory["percent"] if memory else None
        memory_used_mb = memory["used_mb"] if memory else None
        memory_total_mb = memory["total_mb"] if memory else None
        net_sent = None
        net_recv = None
    gpu_pct, gpu_mem = _gpu_snapshot()
    return {
        "agent_id": agent_id or my_id(),
        "cpu_pct": cpu_pct,
        "memory_pct": memory_pct,
        "memory_used_mb": memory_used_mb,
        "memory_total_mb": memory_total_mb,
        "disk_pct": round((disk.used / disk.total * 100), 2) if disk.total else None,
        "disk_used_gb": round(disk.used / 1024 / 1024 / 1024, 2),
        "disk_total_gb": round(disk.total / 1024 / 1024 / 1024, 2),
        "gpu_pct": gpu_pct,
        "gpu_memory_used_mb": gpu_mem,
        "net_sent_bytes": net_sent,
        "net_recv_bytes": net_recv,
        "collector_backend": "psutil" if psutil else "windows-fallback",
        "collector_warning": "" if psutil else f"psutil unavailable: {psutil_error}",
        "timestamp": now_ms(),
    }


def _detect_role(name: str, cmdline: str) -> str:
    text = f"{name} {cmdline}".lower()
    if "myagentwatch_cli" in text or "myaw" in text:
        return "myagentwatch-cli"
    if "codex" in text:
        return "codex"
    if "claude" in text:
        return "claude-code"
    if "deepseek" in text:
        return "deepseek"
    for keyword in AGENT_KEYWORDS:
        if keyword in text:
            return keyword
    return ""


def collect_processes(agent_id: str | None = None, limit: int = 80) -> dict:
    psutil, psutil_error = _load_psutil()
    if not psutil:
        # Fallback cannot read full command lines without CIM privileges. The
        # broad process-name filter is narrowed again by _detect_role().
        rows = _powershell_json(
            "Get-Process | Where-Object { $_.ProcessName -match 'myagentwatch|codex|claude|deepseek|openai|gemini|cursor|copilot|qwen|kimi|python|node' } "
            "| Select-Object Id,ProcessName,CPU,WorkingSet64 "
            "| ConvertTo-Json -Compress"
        )
        if isinstance(rows, dict):
            rows = [rows]
        items = []
        for row in rows if isinstance(rows, list) else []:
            name = row.get("ProcessName") or ""
            role = _detect_role(name, name)
            if not role:
                continue
            items.append({
                "pid": row.get("Id"),
                "process_name": name,
                "cmdline": "",
                "status": "",
                "cpu_pct": None,
                "memory_mb": round((row.get("WorkingSet64") or 0) / 1024 / 1024, 2),
                "detected_role": role,
            })
        return {
            "agent_id": agent_id or my_id(),
            "timestamp": now_ms(),
            "processes": items[:limit],
            "collector_backend": "windows-fallback",
            "collector_warning": f"psutil unavailable: {psutil_error}",
        }

    items = []
    for proc in psutil.process_iter(["pid", "name", "cmdline", "status", "memory_info"]):
        try:
            info = proc.info
            name = info.get("name") or ""
            cmdline_list = info.get("cmdline") or []
            cmdline = " ".join(str(part) for part in cmdline_list)
            role = _detect_role(name, cmdline)
            if not role:
                continue
            mem = info.get("memory_info")
            items.append({
                "pid": info.get("pid"),
                "process_name": name,
                "cmdline": cmdline[:1000],
                "status": info.get("status") or "",
                "cpu_pct": proc.cpu_percent(interval=None),
                "memory_mb": round((mem.rss if mem else 0) / 1024 / 1024, 2),
                "detected_role": role,
            })
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            continue
    items.sort(key=lambda item: (item.get("detected_role") or "", item.get("process_name") or ""))
    return {
        "agent_id": agent_id or my_id(),
        "timestamp": now_ms(),
        "processes": items[:limit],
    }


def report_resources(snapshot: dict) -> dict:
    return post("/api/agent-ingest/resources", snapshot)


def report_processes(snapshot: dict) -> dict:
    return post("/api/agent-ingest/processes", snapshot)
