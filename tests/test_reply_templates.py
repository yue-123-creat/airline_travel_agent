from app.reply_templates import (
    build_aviation_short_fact_draft,
    build_travel_short_fact_draft,
    is_aviation_short_fact,
    is_travel_short_fact,
)


def test_travel_short_fact_detect():
    assert is_travel_short_fact("外滩门票多少", spot_detail=True, guide_mode=False)
    assert is_travel_short_fact("怎么去故宫", spot_detail=True, guide_mode=False)
    assert not is_travel_short_fact("外滩一日游怎么安排", spot_detail=True, guide_mode=True)
    assert not is_travel_short_fact("上海有什么好玩", spot_detail=False, guide_mode=False)


def test_travel_draft_shape():
    draft = build_travel_short_fact_draft(
        {"name": "外滩", "city_norm": "上海"},
        opening="",
        ticket="免费",
        address="中山东一路",
        transport="",
    )
    assert "📍 外滩（上海）" in draft
    assert "🕒 开放时间：暂未收录" in draft
    assert "🎫 门票信息：免费" in draft
    assert "🏠 详细地址：中山东一路" in draft
    assert "🚗 交通指引：建议在地图App导航" in draft
    assert "💡 以上信息来自景点基础资料" in draft


def test_aviation_short_fact_and_draft():
    assert is_aviation_short_fact("值机在哪", materials="值机柜台在一层")
    draft = build_aviation_short_fact_draft(
        airport_name_or_code="LJG",
        materials="交通：机场大巴；值机：国内岛",
    )
    assert "✈️ 根据 LJG 的相关指引：" in draft
    assert "💡 提示" in draft
    assert "值机" in draft
