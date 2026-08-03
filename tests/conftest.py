"""Shared fixtures: a Config wired to temp paths and a MatrixSession that
never touches the macOS Keychain or the network."""

import pytest

from matrixcli.config import Config
from matrixcli.client import MatrixSession


@pytest.fixture
def cfg(tmp_path):
    store = tmp_path / "store"
    store.mkdir()
    return Config(
        homeserver="https://example.org",
        user_id="@me:example.org",
        device_name="test",
        room="",
        keychain_service="test-svc",
        store_path=store,
        state_path=tmp_path / "state.json",
        config_path=tmp_path / "config.ini",
    )


@pytest.fixture
def session(cfg, monkeypatch):
    monkeypatch.setattr(Config, "load_token", lambda self: None)
    monkeypatch.setattr(
        Config, "get_or_create_store_key", lambda self: "test-store-key"
    )
    return MatrixSession(cfg)


class FakeRoom:
    """The handful of MatrixRoom attributes that _entry() and dashboard()
    read, with DM-friendly defaults."""

    def __init__(
        self,
        room_id,
        display_name="",
        users=None,
        room_type=None,
        unread=0,
        highlights=0,
        tags=None,
        canonical_alias=None,
        children=None,
        parents=None,
        member_count=None,
        names=None,
    ):
        self.room_id = room_id
        self.display_name = display_name or room_id
        self.users = users or {}
        self.room_type = room_type
        self.unread_notifications = unread
        self.unread_highlights = highlights
        self.tags = tags or {}
        self.canonical_alias = canonical_alias
        self.children = children or set()
        self.parents = parents or set()
        self.member_count = (
            member_count if member_count is not None else len(self.users)
        )
        self._names = names or {}

    def user_name(self, user_id):
        return self._names.get(user_id, user_id)


@pytest.fixture
def fake_room():
    return FakeRoom
