#!/usr/bin/env python3
"""P0: concurrent reserve must not oversell; TTL sweeper refunds abandoned reserves."""
from __future__ import annotations

import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass

import redis

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.quota.service import QuotaError, QuotaService


@dataclass
class FakeSettings:
    quota_daily_tokens: int = 5000
    quota_monthly_tokens: int = 50000
    quota_session_turns: int = 1000
    quota_reserve_ttl_sec: int = 3
    quota_est_default: int = 1000


def main() -> int:
    redis_url = os.getenv("REDIS_URL", "redis://localhost:6379/15")
    r = redis.Redis.from_url(redis_url, decode_responses=True)
    r.ping()
    r.flushdb()

    uid = f"p0_quota_{int(time.time())}"
    settings = FakeSettings()
    qs = QuotaService(r, settings)  # type: ignore[arg-type]

    est = 1000
    workers = 20
    ok = 0
    err = 0

    def one(i: int):
        try:
            rid, left = qs.check_and_reserve(uid, f"s_conc_{i}", est)
            return ("ok", rid, left)
        except QuotaError as e:
            return ("err", e.code, None)

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futs = [pool.submit(one, i) for i in range(workers)]
        for f in as_completed(futs):
            status, *_ = f.result()
            if status == "ok":
                ok += 1
            else:
                err += 1

    used = qs.daily_used(uid)
    print(f"concurrency: ok={ok} rejected={err} daily_used={used} limit={settings.quota_daily_tokens}")
    if used > settings.quota_daily_tokens:
        print("FAIL: oversold daily quota")
        return 1
    if ok + err != workers:
        print("FAIL: missing results")
        return 1
    if ok * est != used:
        print(f"FAIL: expected used={ok * est}, got {used}")
        return 1
    print("PASS: no oversell under concurrency")

    before = qs.daily_used(uid)
    time.sleep(settings.quota_reserve_ttl_sec + 1)
    released = qs.sweep_expired()
    after = qs.daily_used(uid)
    print(f"sweeper: released={released} daily_used {before} -> {after}")
    if after != 0:
        print("FAIL: abandoned reserves not fully refunded")
        return 1
    if released < 1:
        print("FAIL: sweeper released nothing")
        return 1
    print("PASS: TTL sweeper refunds pending reserves")
    r.flushdb()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
