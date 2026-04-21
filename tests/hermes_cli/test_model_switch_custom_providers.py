from hermes_cli import model_switch


def test_list_authenticated_providers_includes_custom_providers(monkeypatch):
    monkeypatch.setattr(
        "agent.models_dev.fetch_models_dev",
        lambda: {},
    )
    monkeypatch.setattr(
        "hermes_cli.config.load_config",
        lambda: {
            "custom_providers": [
                {
                    "name": "gpt54-pool-47claude",
                    "base_url": "https://47claude.com/v1",
                    "api_key": "secret",
                    "models": {"gpt-5.4": {}, "gpt-5.4-mini": {}},
                },
                {
                    "name": "nim-local",
                    "base_url": "http://127.0.0.1:4310/v1",
                    "api_key": "secret",
                    "models": {"glm-5": {}, "minimax": {}},
                },
            ],
        },
    )

    providers = model_switch.list_authenticated_providers(
        current_provider="nim-local",
        max_models=8,
    )

    slugs = {provider["slug"]: provider for provider in providers}
    assert "gpt54-pool-47claude" in slugs
    assert "nim-local" in slugs
    assert slugs["nim-local"]["is_current"] is True
    assert slugs["nim-local"]["models"] == ["glm-5", "minimax"]


def test_switch_model_accepts_explicit_custom_provider(monkeypatch):
    import hermes_cli.runtime_provider as runtime_provider

    monkeypatch.setattr(
        "agent.models_dev.fetch_models_dev",
        lambda: {},
    )
    # Pre-import runtime_provider, then patch hermes_cli.config.load_config.
    # The switch path should still see the updated custom_providers config.
    assert runtime_provider is not None
    monkeypatch.setattr(
        "hermes_cli.config.load_config",
        lambda: {
            "custom_providers": [
                {
                    "name": "nim-local",
                    "base_url": "http://127.0.0.1:4310/v1",
                    "api_key": "secret",
                    "api_mode": "chat_completions",
                    "models": {"glm-5": {}, "minimax": {}},
                },
            ],
        },
    )

    result = model_switch.switch_model(
        raw_input="glm-5",
        current_provider="openrouter",
        current_model="gpt-5.4",
        explicit_provider="nim-local",
    )

    assert result.success is True
    assert result.target_provider == "nim-local"
    assert result.new_model == "glm-5"
    assert result.base_url == "http://127.0.0.1:4310/v1"
    assert result.api_key == "secret"
