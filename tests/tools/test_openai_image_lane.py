import base64
import json
from pathlib import Path


PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32


def _set_openai_image_env(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("IMAGE_OPENAI_BASE_URL", "http://image-sidecar.test/v1")
    monkeypatch.setenv("IMAGE_OPENAI_API_KEY", "test-image-key")
    monkeypatch.setenv("IMAGE_OPENAI_MODEL", "gpt-image-2")
    monkeypatch.setenv("IMAGE_OPENAI_TIMEOUT", "30")
    monkeypatch.delenv("FAL_KEY", raising=False)


class _FakeImageResponse:
    def __init__(self, payload, *, status_code=200, content=b"", content_type="image/png"):
        self._payload = payload
        self.status_code = status_code
        self.content = content
        self.headers = {"content-type": content_type}

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


class _FakeImageClient:
    instances = []

    def __init__(self, *args, **kwargs):
        self.posts = []
        self.gets = []
        _FakeImageClient.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def post(self, url, headers=None, json=None):
        self.posts.append({"url": url, "headers": headers or {}, "json": json or {}})
        b64 = base64.b64encode(PNG_BYTES).decode("ascii")
        return _FakeImageResponse({"data": [{"b64_json": b64}]})

    def get(self, url, headers=None):
        self.gets.append({"url": url, "headers": headers or {}})
        return _FakeImageResponse({}, content=PNG_BYTES)


def test_openai_image_env_makes_image_tool_available(monkeypatch, tmp_path):
    _set_openai_image_env(monkeypatch, tmp_path)

    from tools import image_generation_tool as image_tool

    assert image_tool.check_image_generation_requirements() is True


def test_openai_image_generation_writes_file_and_returns_media(monkeypatch, tmp_path):
    _set_openai_image_env(monkeypatch, tmp_path)
    _FakeImageClient.instances.clear()
    monkeypatch.setattr("httpx.Client", _FakeImageClient)

    from tools import image_generation_tool as image_tool

    result = json.loads(image_tool.image_generate_tool("a simple blue dot on white background", aspect_ratio="square"))

    assert result["success"] is True
    assert result["model"] == "gpt-image-2"
    assert result["file_path"].startswith(str(tmp_path / "generated-images"))
    assert result["media"] == f"MEDIA:{result['file_path']}"
    image_path = Path(result["file_path"])
    assert image_path.exists()
    assert image_path.read_bytes() == PNG_BYTES

    client = _FakeImageClient.instances[0]
    assert client.posts[0]["url"] == "http://image-sidecar.test/v1/images/generations"
    assert client.posts[0]["headers"]["Authorization"] == "Bearer test-image-key"
    assert client.posts[0]["json"]["model"] == "gpt-image-2"
    assert client.posts[0]["json"]["prompt"] == "a simple blue dot on white background"
    assert client.posts[0]["json"]["size"] == "1024x1024"


def test_openai_image_generation_falls_back_to_backup(monkeypatch, tmp_path):
    _set_openai_image_env(monkeypatch, tmp_path)
    monkeypatch.setenv("IMAGE_OPENAI_BASE_URL", "http://primary-image.test/v1")
    monkeypatch.setenv("IMAGE_OPENAI_API_KEY", "primary-image-key")
    monkeypatch.setenv("IMAGE_OPENAI_BACKUP_BASE_URL", "http://backup-image.test/v1")
    monkeypatch.setenv("IMAGE_OPENAI_BACKUP_API_KEY", "backup-image-key")
    monkeypatch.setenv("IMAGE_OPENAI_BACKUP_MODEL", "gpt-image-2")
    _FakeImageClient.instances.clear()

    class _FallbackImageClient(_FakeImageClient):
        def post(self, url, headers=None, json=None):
            self.posts.append({"url": url, "headers": headers or {}, "json": json or {}})
            if "primary-image.test" in url:
                raise RuntimeError("primary image endpoint down")
            b64 = base64.b64encode(PNG_BYTES).decode("ascii")
            return _FakeImageResponse({"data": [{"b64_json": b64}]})

    monkeypatch.setattr("httpx.Client", _FallbackImageClient)

    from tools import image_generation_tool as image_tool

    result = json.loads(image_tool.image_generate_tool("a simple blue dot on white background", aspect_ratio="square"))

    assert result["success"] is True
    assert result["backend_role"] == "backup"
    assert result["model"] == "gpt-image-2"
    assert Path(result["file_path"]).exists()

    attempts = [post for client in _FakeImageClient.instances for post in client.posts]
    assert attempts[0]["url"] == "http://primary-image.test/v1/images/generations"
    assert attempts[1]["url"] == "http://backup-image.test/v1/images/generations"
    assert attempts[1]["headers"]["Authorization"] == "Bearer backup-image-key"


def test_image_generate_tool_is_exposed_when_openai_image_lane_is_configured(monkeypatch, tmp_path):
    _set_openai_image_env(monkeypatch, tmp_path)

    from model_tools import get_tool_definitions

    tools = get_tool_definitions(enabled_toolsets=["image_gen"], quiet_mode=True)
    names = [tool.get("function", tool).get("name") for tool in tools]

    assert "image_generate" in names
