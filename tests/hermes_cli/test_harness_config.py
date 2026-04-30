from hermes_cli.config import DEFAULT_CONFIG, _KNOWN_ROOT_KEYS


def test_default_config_has_harness_section():
    assert "harness" in DEFAULT_CONFIG
    harness = DEFAULT_CONFIG["harness"]
    assert harness["enabled"] is False
    assert "plan" in harness
    assert "acceptance" in harness
    assert "drift" in harness
    assert "audit" in harness
    assert harness["extra_write_roots"] == []


def test_known_root_keys_include_harness():
    assert "harness" in _KNOWN_ROOT_KEYS
