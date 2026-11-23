from __future__ import annotations

import asyncio
import logging

from app.quota.service import QuotaService

logger = logging.getLogger(__name__)


async def run_quota_sweeper(quota: QuotaService, interval_sec: float = 5.0) -> None:
    """Background loop: auto-release stale pending reserves after TTL deadline."""
    while True:
        try:
            n = await asyncio.to_thread(quota.sweep_expired)
            if n:
                logger.info("quota sweeper released %s stale reserve(s)", n)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("quota sweeper failed")
        await asyncio.sleep(interval_sec)
