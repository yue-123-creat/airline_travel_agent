from app.tools.weather import (
    WEATHER_QUERY_TOOL,
    WEATHER_UNAVAILABLE_REPLY,
    dispatch_weather_tool,
    run_weather_query,
)
from app.travel.service import TRAVEL_REPLY_SYSTEM
from app.travel.itinerary import ITINERARY_SYSTEM


def test_weather_query_tool_schema_name():
    assert WEATHER_QUERY_TOOL["function"]["name"] == "weather_query"


def test_missing_city_asks_user():
    res = run_weather_query(city="")
    assert res["ok"] is False
    assert res["reply"] == "您想查哪个城市的天气？"


def test_dispatch_weather_tool_ignores_other():
    assert dispatch_weather_tool("amap_route", {}) is None


def test_travel_prompt_has_weather_rules():
    assert "weather_query" in TRAVEL_REPLY_SYSTEM
    assert "按需调用" in TRAVEL_REPLY_SYSTEM
    assert "强事实不查" in TRAVEL_REPLY_SYSTEM
    assert "您想查哪个城市的天气？" in TRAVEL_REPLY_SYSTEM
    assert WEATHER_UNAVAILABLE_REPLY in TRAVEL_REPLY_SYSTEM
    # 原则：不靠代码 if「天气」；Prompt 强约束
    assert 'if "天气"' not in TRAVEL_REPLY_SYSTEM


def test_itinerary_prompt_has_weather_rules():
    assert "weather_query" in ITINERARY_SYSTEM
    assert WEATHER_UNAVAILABLE_REPLY in ITINERARY_SYSTEM
    assert "skip_weather_tool" not in ITINERARY_SYSTEM
