"""Reusable heartbeat helpers for myagentwatch-cli."""

from .client import load_config, post


def configured_agent_id() -> str:
    cfg = load_config()
    return cfg.get("agent_id", "")


def send_heartbeat(agent_id: str | None = None,
                   status: str = "active",
                   metadata: dict | None = None) -> dict:
    """Send one heartbeat and return a normalized result."""
    cfg = load_config()
    target = agent_id or cfg.get("agent_id", "")
    if not target:
        return {"ok": False, "error": "agent_id missing"}

    body = {"status": status}
    if metadata:
        body["metadata"] = metadata
    model_id = cfg.get("model_id")
    if not model_id:
        parts = target.split(":")
        if len(parts) >= 3:
            model_id = parts[2]
    if model_id:
        body["model_id"] = model_id

    resp = post(f"/api/heartbeat/{target}", body)
    ok = "error" not in resp and resp.get("status") == "ok"
    return {
        "ok": ok,
        "agent_id": target,
        "status": resp.get("agent_status", status),
        "response": resp,
    }
