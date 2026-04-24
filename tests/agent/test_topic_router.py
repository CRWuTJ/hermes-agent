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


def test_route_telegram_dm_turn_uses_workstream_match_across_languages():
    result = route_telegram_dm_turn(
        message="先解决工具上限的问题，再继续推进",
        current_session_id="sess-current",
        current_title="Hermes gateway worker 改造",
        candidates=[
            {"id": "sess-gateway", "title": "Hermes gateway worker detached runtime"},
            {"id": "sess-tool-limit", "title": "Hermes tool quota recovery"},
        ],
        config=DEFAULT_CONFIG,
    )

    assert result.action == "resume"
    assert result.target_session_id == "sess-tool-limit"
    assert result.target_title == "Hermes tool quota recovery"


def test_route_telegram_dm_turn_stays_when_current_title_matches_workstream():
    result = route_telegram_dm_turn(
        message="工具上限恢复后继续推进队列",
        current_session_id="sess-current",
        current_title="Hermes tool quota recovery",
        candidates=[
            {"id": "sess-gateway", "title": "Hermes gateway worker detached runtime"},
            {"id": "sess-current", "title": "Hermes tool quota recovery"},
        ],
        config=DEFAULT_CONFIG,
    )

    assert result.action == "stay"
    assert result.reason == "current session matches topic"


def test_route_telegram_dm_turn_matches_candidate_summary_when_title_is_generic():
    result = route_telegram_dm_turn(
        message="继续处理 CPA proxy 8317 的模型链路",
        current_session_id="sess-current",
        current_title="Hermes gateway worker 改造",
        candidates=[
            {
                "id": "sess-cpa",
                "title": "Hermes 改造计划",
                "summary": "CPA proxy 8317 model chain and provider routing verification",
            },
            {"id": "sess-worker", "title": "Gateway worker detached runtime"},
        ],
        config=DEFAULT_CONFIG,
    )

    assert result.action == "resume"
    assert result.target_session_id == "sess-cpa"
