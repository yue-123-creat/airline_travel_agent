from __future__ import annotations

import time
import uuid
from datetime import datetime, timezone

import redis

from app.config import Settings
from app.quota.lua_scripts import CHECK_AND_RESERVE, COMMIT, RELEASE, SWEEP_EXPIRED


class QuotaError(Exception):
    def __init__(self, code: str, message: str = ""):
        self.code = code
        self.message = message or code
        super().__init__(self.message)


class QuotaService:
    def __init__(self, client: redis.Redis, settings: Settings):
        self.r = client
        self.settings = settings
        self._check = client.register_script(CHECK_AND_RESERVE)
        self._commit = client.register_script(COMMIT)
        self._release = client.register_script(RELEASE)
        self._sweep = client.register_script(SWEEP_EXPIRED)

    def _day_month_keys(self, uid: str) -> tuple[str, str]:
        now = datetime.now(timezone.utc)
        day = now.strftime("%Y%m%d")
        month = now.strftime("%Y%m")
        return f"quota:daily:{uid}:{day}", f"quota:monthly:{uid}:{month}"

    def check_and_reserve(self, uid: str, session_id: str, est_tokens: int) -> tuple[str, int]:
        reserve_id = f"rsv_{uuid.uuid4().hex[:16]}"
        day_key, month_key = self._day_month_keys(uid)
        ttl = int(self.settings.quota_reserve_ttl_sec)
        expire_at = int(time.time()) + ttl
        result = self._check(
            args=[
                uid,
                session_id,
                str(est_tokens),
                str(self.settings.quota_daily_tokens),
                str(self.settings.quota_monthly_tokens),
                str(self.settings.quota_session_turns),
                reserve_id,
                str(ttl),
                day_key,
                month_key,
                str(expire_at),
            ]
        )
        status = _as_str(result[0])
        if status == "err":
            raise QuotaError(_as_str(result[1]))
        quota_left = int(_as_str(result[2]))
        return reserve_id, quota_left

    def commit(self, reserve_id: str, actual: int) -> None:
        result = self._commit(args=[reserve_id, str(max(0, actual))])
        if _as_str(result[0]) == "err":
            raise QuotaError(_as_str(result[1]))

    def release(self, reserve_id: str) -> None:
        result = self._release(args=[reserve_id])
        if _as_str(result[0]) == "err" and _as_str(result[1]) != "reserve_not_found":
            raise QuotaError(_as_str(result[1]))

    def sweep_expired(self) -> int:
        result = self._sweep(args=[str(int(time.time()))])
        if _as_str(result[0]) != "ok":
            return 0
        return int(_as_str(result[1]))

    def daily_used(self, uid: str) -> int:
        day_key, _ = self._day_month_keys(uid)
        raw = self.r.get(day_key)
        return int(raw or 0)


def _as_str(v) -> str:
    if isinstance(v, bytes):
        return v.decode()
    return str(v)
