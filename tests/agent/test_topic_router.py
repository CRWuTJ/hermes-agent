from agent.topic_router import route_telegram_dm_turn


DEFAULT_CONFIG = {
    "mode": "keyword",
    "resume_threshold": 0.7,
}


def test_route_telegram_dm_turn_resumes_mixed_language_title_from_acronym_message():
    result = route_telegram_dm_turn(
        message="继续聊 MCP",
        current_session_id="sess-current",
        current_title="当前话题",
        candidates=[{"id": "sess-mcp", "title": "MCP 服务器"}],
        config=DEFAULT_CONFIG,
    )

    assert result.action == "resume"
    assert result.target_session_id == "sess-mcp"
    assert result.target_title == "MCP 服务器"


def test_route_telegram_dm_turn_resumes_english_subset_for_longer_topic_title():
    result = route_telegram_dm_turn(
        message="继续聊 DM topics",
        current_session_id="sess-current",
        current_title="当前话题",
        candidates=[{"id": "sess-routing", "title": "Telegram DM topic routing"}],
        config=DEFAULT_CONFIG,
    )

    assert result.action == "resume"
    assert result.target_session_id == "sess-routing"
    assert result.target_title == "Telegram DM topic routing"
