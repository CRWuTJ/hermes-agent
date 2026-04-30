import base64
import json
import sys
import types
from importlib import reload

import pytest


class _FakeHTTPResponse:
    def __init__(self, status=200, body=b""):
        self.status = status
        self._body = body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return self._body


def _reload_image_tool(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.delenv("FAL_KEY", raising=False)
    monkeypatch.delenv("FAL_QUEUE_GATEWAY_URL", raising=False)
    monkeypatch.delenv("TOOL_GATEWAY_USER_TOKEN", raising=False)
    monkeypatch.setenv("IMAGE_OPENAI_BASE_URL", "http://127.0.0.1:8334/v1")
    monkeypatch.setenv("IMAGE_OPENAI_API_KEY", "image-key")
    monkeypatch.setenv("IMAGE_OPENAI_MODEL", "gpt-image-2")

    import tools.image_generation_tool as image_generation_tool

    return reload(image_generation_tool)


def test_openai_image_backend_saves_base64_image_to_hermes_home(monkeypatch, tmp_path):
    image_generation_tool = _reload_image_tool(monkeypatch, tmp_path)
    captured = {}
    png_bytes = b"fake-png-bytes"
    response_body = json.dumps(
        {
            "data": [
                {
                    "b64_json": base64.b64encode(png_bytes).decode("ascii"),
                    "revised_prompt": "a clearer prompt",
                }
            ]
        }
    ).encode()

    def fake_urlopen(request, timeout=None):
        captured["url"] = request.full_url
        captured["timeout"] = timeout
        captured["headers"] = dict(request.header_items())
        captured["body"] = json.loads(request.data.decode())
        return _FakeHTTPResponse(body=response_body)

    monkeypatch.setattr(image_generation_tool.urllib.request, "urlopen", fake_urlopen)

    result = json.loads(image_generation_tool.image_generate_tool("gold wing icon", aspect_ratio="square"))

    assert result["success"] is True
    assert result["provider"] == "openai"
    assert result["image"].startswith(str(tmp_path / "generated-images"))
    assert result["media_path"] == result["image"]
    assert result["revised_prompt"] == "a clearer prompt"
    assert (tmp_path / "generated-images").is_dir()
    assert open(result["image"], "rb").read() == png_bytes
    assert captured["url"] == "http://127.0.0.1:8334/v1/images/generations"
    assert captured["headers"]["Authorization"] == "Bearer image-key"
    assert captured["headers"]["Content-type"] == "application/json"
    assert captured["body"] == {
        "model": "gpt-image-2",
        "prompt": "gold wing icon",
        "size": "1024x1024",
        "n": 1,
    }


def test_check_requirements_accepts_openai_image_backend_without_fal(monkeypatch, tmp_path):
    image_generation_tool = _reload_image_tool(monkeypatch, tmp_path)

    assert image_generation_tool.check_image_generation_requirements() is True
