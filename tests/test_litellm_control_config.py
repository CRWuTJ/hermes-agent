import yaml

from scripts.hermes_litellm_control_config import (
    CONTROL_MODELS,
    build_env,
    build_litellm_config,
)


def _sample_config():
    return {
        "custom_providers": [
            {
                "name": "gpt-mainline-codex-local",
                "base_url": "http://127.0.0.1:4311/v1",
                "api_key": "sk-codex-secret",
                "api_mode": "codex_responses",
                "models": {"gpt-5.5": {"context_length": 1048576}},
            },
        ]
    }


def test_control_config_exposes_only_public_aliases_and_env_key_refs():
    config = build_litellm_config(_sample_config())
    names = [item["model_name"] for item in config["model_list"]]
    upstream_models = {
        item["litellm_params"]["model"]
        for item in config["model_list"]
    }

    assert names == list(CONTROL_MODELS)
    assert upstream_models == {"openai/gpt-5.5"}
    dumped = yaml.safe_dump(config)
    assert "sk-codex-secret" not in dumped
    assert "os.environ/HERMES_EXECUTOR_GPT_SUBSCRIPTION_CODEX_KEY" in dumped
    assert "os.environ/HERMES_CONTROL_MASTER_KEY" in dumped
    assert "os.environ/DATABASE_URL" in dumped


def test_control_env_preserves_master_key_and_maps_executor_keys():
    env = build_env(_sample_config(), {"HERMES_CONTROL_MASTER_KEY": "keep-me"})

    assert env["HERMES_CONTROL_MASTER_KEY"] == "keep-me"
    assert env["HERMES_EXECUTOR_GPT_SUBSCRIPTION_CODEX_KEY"] == "sk-codex-secret"


def test_missing_required_executor_provider_is_rejected():
    config = {"custom_providers": []}

    try:
        build_litellm_config(config)
    except ValueError as exc:
        assert "gpt-mainline-codex-local" in str(exc)
    else:
        raise AssertionError("expected missing gpt-mainline-codex-local to fail")
