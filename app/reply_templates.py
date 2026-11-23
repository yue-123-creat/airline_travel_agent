"""短事实草稿模板：先填字段，再交千问轻度润色（不跳过 compose/organize）。"""

from __future__ import annotations

import re
from typing import Any

_TRAVEL_FACT_RE = re.compile(
    r"门票|票价|开放|几点|营业时间|地址|在哪|位置|怎么去|如何到达|交通"
)
_AVIATION_FACT_RE = re.compile(
    r"在哪|位置|怎么去|如何去|有没有|开放|费用|多少钱|分布|接驳|值机|休息室|安检"
)


def is_travel_short_fact(
    query: str,
    *,
    spot_detail: bool,
    guide_mode: bool,
) -> bool:
    """单点事实问（门票/开放/地址/交通），非攻略列表。"""
    if guide_mode or not spot_detail:
        return False
    q = (query or "").strip()
    if not q or len(q) > 64:
        return False
    return bool(_TRAVEL_FACT_RE.search(q))


def is_aviation_short_fact(query: str, *, materials: str) -> bool:
    """机场事实问，或资料较短可走短事实润色。"""
    q = (query or "").strip()
    if not q:
        return False
    if _AVIATION_FACT_RE.search(q) and len(q) <= 72:
        return True
    # 资料本身不长时，也用短事实结构润色
    return 0 < len((materials or "").strip()) <= 800 and len(q) <= 48


def build_travel_short_fact_draft(
    item: dict[str, Any],
    *,
    transport_note: str = "",
    opening: str = "",
    ticket: str = "",
    address: str = "",
    transport: str = "",
) -> str:
    name = str(item.get("name") or "").strip() or "景点"
    city = str(item.get("city_norm") or item.get("city") or "").strip()
    open_v = (opening or "").strip() or "暂未收录，建议以景区当日公告为准"
    ticket_v = (ticket or "").strip() or "暂未收录具体票价，建议通过官方渠道查询"
    addr_v = (address or "").strip() or "暂无详细地址"
    trans_v = (transport or transport_note or "").strip() or "建议在地图App导航至景区入口"
    city_bit = f"（{city}）" if city else ""
    return (
        f"📍 {name}{city_bit}\n\n"
        f"🕒 开放时间：{open_v}\n"
        f"🎫 门票信息：{ticket_v}\n"
        f"🏠 详细地址：{addr_v}\n"
        f"🚗 交通指引：{trans_v}\n\n"
        "💡 以上信息来自景点基础资料。想了解景点简介、游玩建议或附近美食，可以随时问我哦！"
    )


def build_aviation_short_fact_draft(
    *,
    airport_name_or_code: str,
    materials: str,
) -> str:
    label = (airport_name_or_code or "该机场").strip() or "该机场"
    body = (materials or "").strip()
    # 给模型的原料：保留原资料，由模型提炼为≤300字条目
    if len(body) > 1200:
        body = body[:1200].rstrip() + "…"
    return (
        f"✈️ 根据 {label} 的相关指引：\n\n"
        f"{body}\n\n"
        "💡 提示：如需了解更细的「值机岛分布、交通接驳费用、贵宾休息室」，"
        "请告诉我具体想查哪一类哦。\n\n"
        "【润色要求】请将上文资料提炼为核心要点并分条列出，总篇幅约不超过 300 字；"
        "保留✈️与💡结构，语气亲切，勿扩写成无关攻略。"
    )
