from app.intent_guards import is_greeting_only, is_identity_or_capability_query
from app.travel.photo_policy import enrich_query_with_context


def test_greeting_only():
    assert is_greeting_only("你好")
    assert is_greeting_only("hello")
    assert not is_greeting_only("你是干嘛的")
    assert not is_greeting_only("上海外滩怎么样")


def test_identity_queries():
    assert is_identity_or_capability_query("你是干嘛的")
    assert is_identity_or_capability_query("你是谁")
    assert is_identity_or_capability_query("你能做什么")
    assert is_identity_or_capability_query("你好")
    assert is_identity_or_capability_query("介绍一下你自己")


def test_not_identity_when_asking_about_spot():
    assert not is_identity_or_capability_query("布达拉宫门票多少")
    assert not is_identity_or_capability_query("西藏酥油茶怎么喝")
    assert not is_identity_or_capability_query("上海外滩怎么样")


def test_enrich_skips_identity():
    q = enrich_query_with_context(
        "你是干嘛的",
        slots={"city": "拉萨", "spot": "布达拉宫"},
        history=[{"role": "user", "content": "西藏酥油茶"}],
    )
    assert q == "你是干嘛的"
    assert "拉萨" not in q
    assert "酥油茶" not in q


def test_enrich_skips_when_new_place_named():
    """上海会话里改问西湖，不得再拼上海上下文。"""
    q = enrich_query_with_context(
        "看一下西湖的图片",
        slots={"city": "上海", "spot": "上海迪士尼"},
    )
    assert q == "看一下西湖的图片"
    assert "上海" not in q
    assert "迪士尼" not in q


def test_enrich_keeps_short_followup_context():
    q = enrich_query_with_context(
        "门票多少钱",
        slots={"city": "杭州", "spot": "西湖"},
    )
    assert "西湖" in q or "杭州" in q


def test_parse_xihu_name_from_photo_query():
    from app.travel.query_intent import parse_travel_intent

    intent = parse_travel_intent("看一下西湖的图片")
    assert "西湖" in (intent.name_query or intent.name_norm or "")
