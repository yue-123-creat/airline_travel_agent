from app.travel.photo_policy import (
    PHOTO_COOLDOWN_TURNS,
    gate_photos_by_cooldown,
    is_same_photo_topic,
    normalize_photos_shown,
    record_spot_photos,
    spot_photo_budget,
    spots_in_photo_cooldown,
    stamp_photos_last_turn,
    wants_attraction_photos,
)


def test_default_no_photos():
    assert wants_attraction_photos("下个月适合去吗，天气怎么样啊") is False
    assert wants_attraction_photos("西藏是不是也有酥油茶啊") is False


def test_tx_intent_blocks_photos():
    assert wants_attraction_photos("门票多少钱") is False
    assert wants_attraction_photos("怎么去") is False
    assert wants_attraction_photos("怎么坐地铁去") is False
    assert wants_attraction_photos("景点门票多少") is False
    assert wants_attraction_photos("好玩吗门票多少") is False  # 事务优先
    assert wants_attraction_photos("导航到雁荡山") is False
    assert wants_attraction_photos("附近有酒店吗") is False
    assert wants_attraction_photos("几点开门") is False


def test_visual_intent_allows_photos():
    assert wants_attraction_photos("看一下迪士尼照片") is True
    assert wants_attraction_photos("长什么样") is True
    assert wants_attraction_photos("这个景点再发几张照片看看") is True
    assert wants_attraction_photos("故宫可以拍写真吗，或者找代拍") is True


def test_explore_intent_allows_photos():
    assert wants_attraction_photos("雁荡山好玩吗") is True
    assert wants_attraction_photos("上海一日游怎么安排") is True
    assert wants_attraction_photos("外滩好玩吗介绍一下") is True
    assert wants_attraction_photos("有什么推荐景点") is True
    assert wants_attraction_photos("明天出发去成都了，推荐几个好玩的地方吧") is True
    assert wants_attraction_photos("推荐一下杭州") is True


def test_quota_and_url_dedupe():
    shown = {}
    shown = record_spot_photos(
        shown, name="景点A", urls=["http://a", "http://b"], turn_index=1
    )
    assert shown["景点A"]["last_turn"] == 1
    ok, used = spot_photo_budget("景点A", shown)
    assert ok is True
    assert "http://a" in used
    shown = record_spot_photos(
        shown, name="景点A", urls=["http://c"], turn_index=5
    )
    assert shown["景点A"]["last_turn"] == 5
    ok2, _ = spot_photo_budget("景点A", shown)
    assert ok2 is False  # rounds == 2
    shown2 = normalize_photos_shown(
        {"景点A": {"rounds": 1, "last_turn": 2, "urls": ["http://a"]}}
    )
    shown2b = record_spot_photos(shown2, name="景点A", urls=["http://a"])
    assert shown2b["景点A"]["rounds"] == 1
    assert shown2b["景点A"]["last_turn"] == 2


def test_same_topic_followup_and_new_entity():
    cool = ["雁荡山"]
    assert is_same_photo_topic("雁荡山门票多少钱", [], cool) is True
    assert is_same_photo_topic("怎么去", [], cool) is True
    assert is_same_photo_topic("好玩吗介绍一下", ["雁荡山风景区"], cool) is True
    assert is_same_photo_topic("那温州市区有什么好吃的", ["五马街"], cool) is False


def test_last_turn_cooldown_window():
    shown = stamp_photos_last_turn(
        {"雁荡山": {"rounds": 1, "last_turn": 0, "urls": ["http://a"]}},
        names=["雁荡山"],
        turn_index=5,
    )
    assert shown["雁荡山"]["last_turn"] == 5
    # turn 6..8 冷却；turn 9 放开
    assert spots_in_photo_cooldown(shown, turn_index=6) == ["雁荡山"]
    assert spots_in_photo_cooldown(shown, turn_index=5 + PHOTO_COOLDOWN_TURNS) == [
        "雁荡山"
    ]
    assert spots_in_photo_cooldown(shown, turn_index=5 + PHOTO_COOLDOWN_TURNS + 1) == []

    # 探索意图（好玩/推荐）不受同话题冷却拦截，按意图直接出图
    allow_explore, flag_explore = gate_photos_by_cooldown(
        query="明天去成都，推荐几个好玩的地方吧",
        turn_spots=["都江堰", "青城山"],
        photos_shown=shown,
        turn_index=6,
        intent_wants=True,
    )
    assert allow_explore is True and flag_explore is None

    # 强视觉追问仍走冷却，避免同景点连刷图
    allow, flag = gate_photos_by_cooldown(
        query="雁荡山再发几张照片看看",
        turn_spots=["雁荡山"],
        photos_shown=shown,
        turn_index=6,
        intent_wants=True,
    )
    assert allow is False and flag == "photos_skipped_cooldown"

    allow2, flag2 = gate_photos_by_cooldown(
        query="那温州市区有什么好吃的",
        turn_spots=["五马街"],
        photos_shown=shown,
        turn_index=6,
        intent_wants=True,
    )
    assert allow2 is True and flag2 is None

    allow3, _ = gate_photos_by_cooldown(
        query="雁荡山再来几张图片",
        turn_spots=["雁荡山"],
        photos_shown=shown,
        turn_index=5 + PHOTO_COOLDOWN_TURNS + 1,
        intent_wants=True,
    )
    assert allow3 is True
