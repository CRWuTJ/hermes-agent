"""Regression tests for pairing requests with missing sender IDs."""

import pytest

from gateway.config import Platform
from gateway.platforms.base import MessageEvent
from gateway.run import GatewayRunner
from gateway.session import SessionSource


class RecordingPairingStore:
    def __init__(self):
        self.checked = []
        self.generated = []
        self.rate_limited = []
        self.recorded = []

    def is_approved(self, platform, user_id):
        self.checked.append((platform, user_id))
        return False

    def _is_rate_limited(self, platform, user_id):
        self.rate_limited.append((platform, user_id))
        raise AssertionError("missing user_id must not enter pairing rate-limit path")

    def generate_code(self, platform, user_id, user_name=""):
        self.generated.append((platform, user_id, user_name))
        raise AssertionError("missing user_id must not generate a pairing code")

    def _record_rate_limit(self, platform, user_id):
        self.recorded.append((platform, user_id))
        raise AssertionError("missing user_id must not record pairing rate-limit state")


class RecordingAdapter:
    def __init__(self):
        self.sent = []

    async def send(self, chat_id, text):
        self.sent.append((chat_id, text))


@pytest.mark.asyncio
async def test_unauthorized_dm_without_user_id_does_not_send_pairing_code():
    runner = object.__new__(GatewayRunner)
    runner.pairing_store = RecordingPairingStore()
    adapter = RecordingAdapter()
    runner.adapters = {Platform.TELEGRAM: adapter}

    event = MessageEvent(
        text="hello",
        source=SessionSource(
            platform=Platform.TELEGRAM,
            chat_id="6141351975",
            chat_type="dm",
            user_id=None,
            user_name=None,
        ),
    )

    result = await runner._handle_message(event)

    assert result is None
    assert adapter.sent == []
    assert runner.pairing_store.checked == []
    assert runner.pairing_store.generated == []
    assert runner.pairing_store.rate_limited == []
    assert runner.pairing_store.recorded == []
