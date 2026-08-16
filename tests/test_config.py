import json
from dataclasses import replace

import pytest

from matrixcli.config import Config


def write_config(tmp_path, body):
    path = tmp_path / "config.ini"
    path.write_text(body)
    return path


def minimal_config(tmp_path):
    return write_config(
        tmp_path,
        f"""\
[matrix]
homeserver = https://hs.example
user_id = @me:hs.example

[storage]
store_path = {tmp_path}/store
state_path = {tmp_path}/state.json
""",
    )


class TestLoad:
    def test_missing_file_writes_template_and_raises(self, tmp_path):
        path = tmp_path / "config.ini"
        with pytest.raises(FileNotFoundError):
            Config.load(path)
        assert "[matrix]" in path.read_text()

    def test_loads_values_and_defaults(self, tmp_path):
        cfg = Config.load(minimal_config(tmp_path))
        assert cfg.homeserver == "https://hs.example"
        assert cfg.user_id == "@me:hs.example"
        assert cfg.device_name == "matrixcli"
        assert cfg.keychain_service == "matrix-cli"
        assert cfg.room == ""
        assert cfg.store_path == tmp_path / "store"
        assert cfg.store_path.is_dir()
        assert cfg.state_path.parent.is_dir()

    def test_default_path_ignores_cwd(self, tmp_path, monkeypatch):
        from matrixcli.config import _default_config_path

        monkeypatch.delenv("MATRIXCLI_CONFIG", raising=False)
        monkeypatch.chdir(tmp_path)
        (tmp_path / "config.ini").write_text("[matrix]\nhomeserver = https://evil\n")
        # A config.ini in the working directory must NOT be picked up: it could
        # redirect the homeserver while reusing the Keychain credentials.
        assert _default_config_path() != tmp_path / "config.ini"
        assert _default_config_path().name == "config.ini"
        assert ".config" in str(_default_config_path())

    def test_allow_unverified_defaults_true_and_parses(self, tmp_path):
        cfg = Config.load(minimal_config(tmp_path))
        assert cfg.allow_unverified is True
        path = write_config(
            tmp_path,
            "[matrix]\nhomeserver = https://hs.example\nuser_id = @me:hs.example\n"
            "allow_unverified = false\n",
        )
        assert Config.load(path).allow_unverified is False

    def test_missing_matrix_section_raises_valueerror(self, tmp_path):
        path = write_config(tmp_path, "[other]\nfoo = 1\n")
        with pytest.raises(ValueError, match=r"\[matrix\] section"):
            Config.load(path)

    def test_unparseable_file_raises_valueerror(self, tmp_path):
        path = write_config(tmp_path, "not an ini file at all\n")
        with pytest.raises(ValueError, match="Could not parse"):
            Config.load(path)


class TestState:
    def test_defaults_when_file_missing(self, cfg):
        assert cfg.load_state() == {
            "last_event_ts": {},
            "last_opened_ts": {},
            "room_meta": {},
            "space_children": {},
            "cache_spaces": {},
        }

    def test_roundtrip(self, cfg):
        state = {
            "last_event_ts": {"!r:hs": 123},
            "last_opened_ts": {"!r:hs": 456},
            "room_meta": {"!r:hs": {"title": "R"}},
            "space_children": {"!s:hs": ["!r:hs"]},
            "selected_space": "!s:hs",
        }
        cfg.save_state(state)
        assert cfg.load_state() == {**state, "cache_spaces": {}}
        assert not cfg.state_path.with_suffix(".json.tmp").exists()

    def test_corrupt_file_falls_back_to_defaults(self, cfg):
        cfg.state_path.write_text("{not json")
        assert cfg.load_state() == {
            "last_event_ts": {},
            "last_opened_ts": {},
            "room_meta": {},
            "space_children": {},
            "cache_spaces": {},
        }

    def test_non_dict_json_falls_back_to_defaults(self, cfg):
        cfg.state_path.write_text(json.dumps([1, 2, 3]))
        assert cfg.load_state() == {
            "last_event_ts": {},
            "last_opened_ts": {},
            "room_meta": {},
            "space_children": {},
            "cache_spaces": {},
        }

    def test_missing_keys_are_added(self, cfg):
        cfg.state_path.write_text(json.dumps({"selected_space": "!s:hs"}))
        state = cfg.load_state()
        assert state["selected_space"] == "!s:hs"
        assert state["last_event_ts"] == {}
        assert state["last_opened_ts"] == {}

    def test_wrong_typed_values_are_replaced(self, cfg):
        cfg.state_path.write_text(
            json.dumps({"last_event_ts": None, "last_opened_ts": [1, 2]})
        )
        state = cfg.load_state()
        assert state["last_event_ts"] == {}
        assert state["last_opened_ts"] == {}


class TestTimelineCache:
    @pytest.fixture(autouse=True)
    def no_keychain(self, monkeypatch):
        monkeypatch.setattr(
            Config, "get_or_create_store_key", lambda self: "key-one"
        )

    def test_roundtrip(self, cfg):
        payload = {"user_id": "@me:hs", "timelines": {"!r:hs": [{"body": "hi"}]}}
        cfg.save_timeline_cache(payload)
        assert cfg.load_timeline_cache() == payload
        assert not cfg._timeline_cache_path.with_suffix(".cache.tmp").exists()

    def test_file_is_not_plaintext(self, cfg):
        cfg.save_timeline_cache({"body": "a very secret message"})
        blob = cfg._timeline_cache_path.read_bytes()
        assert b"very secret" not in blob

    def test_missing_file_is_none(self, cfg):
        assert cfg.load_timeline_cache() is None

    def test_corrupt_file_is_none(self, cfg):
        cfg._timeline_cache_path.write_bytes(b"\x01" + b"garbage" * 20)
        assert cfg.load_timeline_cache() is None
        cfg._timeline_cache_path.write_bytes(b"short")
        assert cfg.load_timeline_cache() is None

    def test_wrong_key_is_none(self, cfg, monkeypatch):
        cfg.save_timeline_cache({"user_id": "@me:hs"})
        monkeypatch.setattr(
            Config, "get_or_create_store_key", lambda self: "key-two"
        )
        # A fresh instance so the derived key cached on ``cfg`` is not reused.
        assert replace(cfg).load_timeline_cache() is None

    def test_clear_is_idempotent(self, cfg):
        cfg.save_timeline_cache({})
        cfg.clear_timeline_cache()
        assert not cfg._timeline_cache_path.exists()
        cfg.clear_timeline_cache()

    def test_room_archive_roundtrip_and_clear(self, cfg):
        payload = {"user_id": "@me:hs", "room_id": "!r:hs", "messages": []}
        cfg.save_room_archive("!r:hs", payload)
        path = cfg._room_archive_path("!r:hs")
        assert path.exists()
        # sha256 filename: no room id leaks into a directory listing.
        assert "!r" not in path.name and path.name.endswith(".cache")
        assert cfg.load_room_archives() == [payload]
        cfg.clear_room_archive("!r:hs")
        assert cfg.load_room_archives() == []
        cfg.clear_room_archive("!r:hs")  # idempotent

    def test_clear_timeline_cache_removes_room_archives_too(self, cfg):
        cfg.save_timeline_cache({})
        cfg.save_room_archive("!r:hs", {"room_id": "!r:hs"})
        cfg.clear_timeline_cache()
        assert not cfg._timeline_cache_path.exists()
        assert cfg.load_room_archives() == []

    def test_unreadable_room_archive_is_skipped(self, cfg):
        cfg.save_room_archive("!r:hs", {"room_id": "!r:hs"})
        cfg._room_archive_path("!bad:hs").write_bytes(b"\x01" + b"junk" * 20)
        assert [p["room_id"] for p in cfg.load_room_archives()] == ["!r:hs"]


class TestCacheMessagesSetting:
    def test_defaults_true_and_parses_false(self, tmp_path):
        assert Config.load(minimal_config(tmp_path)).cache_messages is True
        path = write_config(
            tmp_path,
            "[matrix]\nhomeserver = https://hs.example\nuser_id = @me:hs.example\n"
            "\n[cache]\nmessages = false\n",
        )
        assert Config.load(path).cache_messages is False

    def test_template_documents_the_section(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            Config.load(tmp_path / "config.ini")
        assert "[cache]" in (tmp_path / "config.ini").read_text()


class TestVersion:
    def test_dunder_version_matches_pyproject(self):
        # The version lives in two places: pyproject.toml feeds the packaged
        # metadata the footer displays, __init__.__version__ is the in-code
        # copy. Nothing else ties them together, so pin it here.
        import tomllib
        from pathlib import Path

        import matrixcli

        pyproject = Path(__file__).parents[1] / "pyproject.toml"
        with pyproject.open("rb") as f:
            data = tomllib.load(f)
        assert matrixcli.__version__ == data["project"]["version"]
