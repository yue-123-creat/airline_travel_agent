#!/usr/bin/env python3
"""P0: session isolation + same-uid LTM mock injection."""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

BASE = os.getenv("API_BASE", "http://127.0.0.1:8080")
ADMIN = os.getenv("ADMIN_TOKEN", "dev-admin")
UID = f"p0_iso_{int(time.time())}"
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


def main() -> int:
    code, health = req("GET", "/health")
    if code != 200 or not health.get("postgres_saver"):
        print("FAIL: api unhealthy or PostgresSaver not ready", health)
        return 1

    code, seeded = req(
        "POST",
        f"/v1/admin/ltm/{UID}/seed",
        headers={"X-Admin-Token": ADMIN},
    )
    if code != 200:
        print("FAIL: ltm seed", code, seeded)
        return 1
    items = seeded.get("items") or []
    if not any(i.get("key") == "home_city" for i in items):
        print("FAIL: LTM mock missing home_city", seeded)
        return 1
    print("PASS: Mock LTM seeded for uid")

    secret = f"SECRET_{UID[-6:]}"
    code, a = req(
        "POST",
        "/v1/chat",
        {"user_token": TOKEN, "message": f"请记住暗号：{secret}。我常住杭州。"},
    )
    if code != 200:
        print("FAIL: chat A", code, a)
        return 1
    sid_a = a["session_id"]
    print(f"session A={sid_a} llm_calls={a.get('llm_calls')}")

    code, stm_a = req(
        "GET",
        f"/v1/admin/stm/{sid_a}",
        headers={"X-Admin-Token": ADMIN},
    )
    if code != 200:
        print("FAIL: stm A", code, stm_a)
        return 1
    blob_a = json.dumps(stm_a, ensure_ascii=False)
    if secret not in blob_a:
        print("FAIL: STM A missing secret", stm_a)
        return 1
    print("PASS: STM A holds session context")

    code, created = req("POST", "/v1/sessions", {"user_token": TOKEN})
    if code != 200:
        print("FAIL: create B", code, created)
        return 1
    sid_b = created["session_id"]
    code, stm_b = req(
        "GET",
        f"/v1/admin/stm/{sid_b}",
        headers={"X-Admin-Token": ADMIN},
    )
    if code != 200:
        print("FAIL: stm B", code, stm_b)
        return 1
    if stm_b.get("stm_messages"):
        print("FAIL: new session STM not empty", stm_b)
        return 1
    print("PASS: new session_id STM empty (isolated)")

    code, b = req(
        "POST",
        "/v1/chat",
        {
            "user_token": TOKEN,
            "session_id": sid_b,
            "message": "随便打个招呼就好",
        },
    )
    if code != 200:
        print("FAIL: chat B", code, b)
        return 1

    code, stm_b2 = req(
        "GET",
        f"/v1/admin/stm/{sid_b}",
        headers={"X-Admin-Token": ADMIN},
    )
    blob_b = json.dumps(stm_b2, ensure_ascii=False)
    if secret in blob_b:
        print("FAIL: session B leaked A's STM secret")
        return 1
    print("PASS: session B STM does not contain A's secret")

    data_path = Path(__file__).resolve().parents[1] / "data" / "ltm_mock.json"
    if data_path.exists():
        data = json.loads(data_path.read_text(encoding="utf-8"))
        entry = data.get(UID) or {}
        if "home_city" not in (entry.get("items") or {}):
            print("FAIL: LTM file missing home_city for uid")
            return 1
        print("PASS: same uid LTM mock remains available")
    else:
        print("FAIL: ltm_mock.json missing on host")
        return 1

    print("ALL SESSION CHECKS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
