#!/usr/bin/env python3
"""P0: after api container restart, PostgresSaver STM for a session still exists."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request

BASE = os.getenv("API_BASE", "http://127.0.0.1:8080")
ADMIN = os.getenv("ADMIN_TOKEN", "dev-admin")
API_CONTAINER = os.getenv("API_CONTAINER", "airline_travel_agent-api")
UID = f"p0_rst_{int(time.time())}"
TOKEN = f"uid:{UID}"


def req(method: str, path: str, body: dict | None = None, headers: dict | None = None):
    if method in {"POST", "PUT", "PATCH"} and body is None:
        body = {}
    data = None if body is None else json.dumps(body).encode("utf-8")
    h = {"Content-Type": "application/json"}
    if headers:
        h.update(headers)
    r = urllib.request.Request(BASE + path, data=data, headers=h, method=method)
    try:
        with urllib.request.urlopen(r, timeout=120) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8")
        try:
            payload = json.loads(raw)
        except Exception:
            payload = {"raw": raw}
        return e.code, payload


def wait_healthy(timeout: float = 90.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            code, health = req("GET", "/health")
            if code == 200 and health.get("ok") and health.get("postgres_saver"):
                return True
        except Exception:
            pass
        time.sleep(2)
    return False


def main() -> int:
    if not wait_healthy():
        print("FAIL: api not healthy before test")
        return 1

    marker = f"MARKER_{UID[-8:]}"
    code, chat = req(
        "POST",
        "/v1/chat",
        {"user_token": TOKEN, "message": f"会话标记词是 {marker}，请简短确认。"},
    )
    if code != 200:
        print("FAIL: pre-restart chat", code, chat)
        return 1
    sid = chat["session_id"]

    code, stm = req("GET", f"/v1/admin/stm/{sid}", headers={"X-Admin-Token": ADMIN})
    if code != 200 or marker not in json.dumps(stm, ensure_ascii=False):
        print("FAIL: STM missing marker before restart", stm)
        return 1
    print(f"pre-restart STM ok session={sid}")

    print(f"restarting {API_CONTAINER} ...")
    subprocess.check_call(["docker", "restart", API_CONTAINER])
    if not wait_healthy():
        print("FAIL: api did not become healthy after restart")
        return 1

    code, stm2 = req("GET", f"/v1/admin/stm/{sid}", headers={"X-Admin-Token": ADMIN})
    if code != 200:
        print("FAIL: STM fetch after restart", code, stm2)
        return 1
    if marker not in json.dumps(stm2, ensure_ascii=False):
        print("FAIL: STM lost after api restart", stm2)
        return 1
    print("PASS: STM survived api container restart")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
