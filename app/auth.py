from __future__ import annotations

from fastapi import HTTPException


def parse_user_token(user_token: str, auth_mode: str = "mock") -> str:
    token = (user_token or "").strip()
    if auth_mode == "mock":
        if token.startswith("uid:"):
            uid = token[4:].strip()
            if uid:
                return uid
        raise HTTPException(status_code=401, detail={"code": "unauthorized", "message": "expected user_token uid:<id>"})
    raise HTTPException(status_code=401, detail={"code": "unauthorized", "message": "unsupported auth mode"})
