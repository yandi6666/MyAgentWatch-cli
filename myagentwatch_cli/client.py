"""MyAgentWatch CLI — HTTP + WebSocket client."""

import json
import os
import time
import urllib.parse
import urllib.request
import urllib.error

CONFIG_PATH = os.path.join(os.path.dirname(__file__), "..", "config.json")


def load_config():
    if os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH, "r", encoding="utf-8-sig") as f:
            return json.load(f)
    return {}


def save_config(cfg):
    os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)


def _req(method, path, body=None, timeout=30):
    cfg = load_config()
    server = cfg.get("server", "http://127.0.0.1:10000")
    key = cfg.get("key", "")
    # URL-encode path segments (agent IDs may contain spaces and colons)
    safe_path = urllib.parse.quote(path, safe="/?=&:%")
    url = server.rstrip("/") + safe_path
    data = json.dumps(body).encode() if body else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    if key:
        req.add_header("Authorization", "Bearer " + key)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            content = resp.read().decode()
            return json.loads(content) if content else {}
    except urllib.error.HTTPError as e:
        body = e.read().decode() if e.fp else ""
        try:
            return json.loads(body)
        except Exception:
            return {"error": str(e.code), "message": body}
    except Exception as e:
        return {"error": str(e)}


def get(path):
    return _req("GET", path)


def post(path, body=None):
    return _req("POST", path, body)


def patch(path, body=None):
    return _req("PATCH", path, body)


def delete(path):
    return _req("DELETE", path)


def connect(server, key):
    cfg = {"server": server, "key": key}
    save_config(cfg)
    resp = get("/api/users")
    if "users" in resp:
        # Look up which user this token belongs to
        for u in resp["users"]:
            if u.get("token_prefix") and key.startswith(u["token_prefix"].replace("…", "")):
                cfg["agent_name"] = u["name"]
                cfg["agent_id"] = u["id"]
                save_config(cfg)
                break
        return True
    return False


def my_name():
    cfg = load_config()
    return cfg.get("agent_name", "CLI Agent")


def my_id():
    cfg = load_config()
    return cfg.get("agent_id", "")


def whoami():
    cfg = load_config()
    resp = get("/api/status")
    return {
        "server": cfg.get("server"),
        "key_prefix": cfg.get("key", "")[:10] + "..." if cfg.get("key") else "(未设置)",
        "version": resp.get("version", "?"),
        "uptime": resp.get("uptime", "?"),
    }
