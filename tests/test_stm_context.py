from app.memory.context import (
    LATEST_FOCUS_HINT,
    LATEST_USER_SEP,
    SUMMARY_SYSTEM,
    build_llm_context_payload,
    build_llm_context_text,
    clamp_summary,
    stm_recent_messages,
    window_message_limit,
)


def test_window_message_limit_is_two_per_turn():
    assert window_message_limit(8) == 16
    assert window_message_limit(1) == 2


def test_stm_recent_messages_keeps_last_n_turns():
    msgs = [{"role": "user" if i % 2 == 0 else "assistant", "content": str(i)} for i in range(20)]
    recent = stm_recent_messages(msgs, turns=3)
    assert len(recent) == 6
    assert recent[0]["content"] == "14"
    assert recent[-1]["content"] == "19"


def test_build_llm_context_includes_ltm_summary_recent_message():
    text = build_llm_context_text(
        message="冷不冷",
        summary="用户计划去上海外滩",
        recent=[{"role": "user", "content": "外滩怎么样"}],
        ltm=[{"type": "preference", "content": "怕冷"}],
    )
    assert "[LTM 偏好]" in text
    assert "怕冷" in text
    assert "[会话摘要]" in text
    assert "外滩" in text
    assert "[最近对话]" in text
    assert LATEST_USER_SEP in text
    assert "[当前用户]" not in text
    assert "冷不冷" in text
    assert LATEST_FOCUS_HINT in text
    assert text.index(LATEST_USER_SEP) > text.index("[最近对话]")
    assert text.index(LATEST_FOCUS_HINT) > text.index(LATEST_USER_SEP)

    payload = build_llm_context_payload(
        message="hi",
        summary="s",
        recent=[{"role": "user", "content": "a"}],
        ltm=[],
        slots={"city": "上海"},
    )
    assert payload["summary"] == "s"
    assert payload["slots"]["city"] == "上海"


def test_summary_system_drops_unresolved():
    # 摘要只记已确定事实；明确要求遗弃未回答历史问题（文案里可出现「未决」作否定对象）
    assert "只保留：目的地/景点、出行偏好与约束、未决问题" not in SUMMARY_SYSTEM
    assert "已确定" in SUMMARY_SYSTEM
    assert "遗弃" in SUMMARY_SYSTEM


def test_clamp_summary():
    assert clamp_summary("abcdefghij", 5) == "abcd…"
    assert clamp_summary("短", 10) == "短"
