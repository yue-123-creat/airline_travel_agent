from app.graph.topic_gate import (
    apply_slots_patch,
    dispatch_params_mismatch,
    sanitize_slots_for_turn,
)


def test_apply_slots_patch_null_clears_key():
    slots = {"city": "上海", "spot": "外滩", "photos_shown": {"x": 1}}
    out = apply_slots_patch(slots, {"city": None, "spot": "西湖"})
    assert "city" not in out
    assert out["spot"] == "西湖"
    assert out["photos_shown"] == {"x": 1}


def test_mismatch_when_normalized_glues_old_city():
    assert dispatch_params_mismatch(
        raw_message="看一下西湖的图片",
        normalized_query="上海 看一下西湖的图片",
        slots={"city": "上海", "spot": "迪士尼"},
    )


def test_no_mismatch_short_followup_keeps_slots():
    assert not dispatch_params_mismatch(
        raw_message="门票多少",
        normalized_query="杭州西湖 门票多少",
        slots={"city": "杭州", "spot": "西湖"},
    )


def test_no_mismatch_when_norm_matches_raw_topic():
    assert not dispatch_params_mismatch(
        raw_message="西湖好玩吗",
        normalized_query="西湖好玩吗",
        slots={"city": "杭州"},
    )


def test_sanitize_drops_conflicting_city():
    slots = {"city": "上海", "spot": "外滩", "photos_shown": {}}
    out = sanitize_slots_for_turn("西湖的图片", slots)
    assert out.get("city") != "上海"
    assert "photos_shown" in out


def test_airport_mismatch():
    assert dispatch_params_mismatch(
        raw_message="丽江机场贵宾服务热线",
        normalized_query="丽江机场贵宾服务热线",
        slots={"airport_code": "WNZ", "airport_name": "温州龙湾国际机场"},
    )
