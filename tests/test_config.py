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
        assert not list(cfg.state_path.parent.glob("*.tmp"))

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
        assert not list(cfg.store_path.glob("*.tmp"))

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


class TestMediaCache:
    @pytest.fixture(autouse=True)
    def no_keychain(self, monkeypatch):
        monkeypatch.setattr(
            Config, "get_or_create_store_key", lambda self: "key-one"
        )

    def test_roundtrip_and_missing(self, cfg):
        assert cfg.load_media_cache("mxc://hs/x") is None
        cfg.save_media_cache("mxc://hs/x", b"jpeg bytes")
        assert cfg.load_media_cache("mxc://hs/x") == b"jpeg bytes"
        path = cfg._media_cache_path("mxc://hs/x")
        # sha256 filename: no mxc url leaks into a directory listing.
        assert "hs" not in path.name and path.name.endswith(".cache")

    def test_file_is_not_plaintext(self, cfg):
        cfg.save_media_cache("mxc://hs/x", b"a very secret picture")
        blob = cfg._media_cache_path("mxc://hs/x").read_bytes()
        assert b"very secret" not in blob

    def test_wrong_key_is_none(self, cfg, monkeypatch):
        from dataclasses import replace

        cfg.save_media_cache("mxc://hs/x", b"data")
        monkeypatch.setattr(
            Config, "get_or_create_store_key", lambda self: "key-two"
        )
        assert replace(cfg).load_media_cache("mxc://hs/x") is None

    def test_clear_timeline_cache_removes_media_too(self, cfg):
        cfg.save_media_cache("mxc://hs/x", b"data")
        cfg.clear_timeline_cache()
        assert cfg.load_media_cache("mxc://hs/x") is None
        assert not list(cfg._media_cache_dir.glob("*.cache"))

    def test_prunes_oldest_past_the_cap(self, cfg, monkeypatch):
        import os
        import time

        monkeypatch.setattr(Config, "MEDIA_CACHE_MAX_BYTES", 300)
        for i, key in enumerate(("mxc://hs/a", "mxc://hs/b", "mxc://hs/c")):
            cfg.save_media_cache(key, bytes(100))
            # Distinct mtimes without sleeping: prune sorts by them.
            past = time.time() - 100 + i
            os.utime(cfg._media_cache_path(key), (past, past))
        cfg.save_media_cache("mxc://hs/d", bytes(100))
        # ~133 encrypted bytes each: the two oldest had to go to get back
        # under the 300-byte cap.
        assert cfg.load_media_cache("mxc://hs/a") is None
        assert cfg.load_media_cache("mxc://hs/b") is None
        assert cfg.load_media_cache("mxc://hs/c") == bytes(100)
        assert cfg.load_media_cache("mxc://hs/d") == bytes(100)


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


class TestPasswordHint:
    def test_platform_specific_command(self, cfg, monkeypatch):
        import matrixcli.config as config_module

        monkeypatch.setattr(config_module.sys, "platform", "darwin")
        hint = cfg.store_password_hint()
        assert hint.startswith("security add-generic-password")
        assert cfg.user_id in hint
        monkeypatch.setattr(config_module.sys, "platform", "linux")
        hint = cfg.store_password_hint()
        assert hint.startswith("keyring set")
        assert cfg.user_id in hint


class TestAsciiRamp:
    def test_defaults_to_bourke_ramp(self, tmp_path):
        from matrixcli.config import DEFAULT_ASCII_RAMP

        cfg = Config.load(minimal_config(tmp_path))
        assert cfg.ascii_ramp == DEFAULT_ASCII_RAMP
        # Bourke's 70-level ramp, most ink first, ending on the space.
        assert len(DEFAULT_ASCII_RAMP) == 70
        assert DEFAULT_ASCII_RAMP[0] == "$" and DEFAULT_ASCII_RAMP[-1] == " "

    def test_quoted_value_keeps_edge_spaces(self, tmp_path):
        path = write_config(
            tmp_path,
            "[matrix]\nhomeserver = https://hs.example\nuser_id = @me:hs.example\n"
            '\n[preview]\nascii_ramp = "@%#*+=-:. "\n',
        )
        # Raw read: the "%" must not be treated as interpolation syntax, and
        # the quotes must protect the trailing space configparser would strip.
        assert Config.load(path).ascii_ramp == "@%#*+=-:. "

    def test_unquoted_value_and_short_fallback(self, tmp_path):
        from matrixcli.config import DEFAULT_ASCII_RAMP

        path = write_config(
            tmp_path,
            "[matrix]\nhomeserver = https://hs.example\nuser_id = @me:hs.example\n"
            "\n[preview]\nascii_ramp = @+.\n",
        )
        assert Config.load(path).ascii_ramp == "@+."
        path = write_config(
            tmp_path,
            "[matrix]\nhomeserver = https://hs.example\nuser_id = @me:hs.example\n"
            "\n[preview]\nascii_ramp = x\n",
        )
        # A single glyph cannot form a gradient: fall back to the default.
        assert Config.load(path).ascii_ramp == DEFAULT_ASCII_RAMP

    def test_template_documents_the_section(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            Config.load(tmp_path / "config.ini")
        assert "[preview]" in (tmp_path / "config.ini").read_text()


class TestAtomicWrites:
    def test_state_tmp_name_carries_pid(self, cfg, monkeypatch):
        """Two live instances must never share a tmp name: with the old fixed
        name, one instance renamed the other's tmp away mid-save and the loser
        crashed on FileNotFoundError."""
        import os
        from pathlib import Path

        seen = []
        real_replace = Path.replace

        def spy(self, target):
            seen.append(self.name)
            return real_replace(self, target)

        monkeypatch.setattr(Path, "replace", spy)
        cfg.save_state({})
        assert seen == [f"state.json.{os.getpid()}.tmp"]

    def test_save_state_survives_stolen_tmp(self, cfg, monkeypatch):
        from pathlib import Path

        def gone(self, target):
            raise FileNotFoundError(self)

        monkeypatch.setattr(Path, "replace", gone)
        cfg.save_state({"x": 1})  # must not raise

    def test_save_state_survives_missing_directory(self, cfg):
        cfg.state_path = cfg.state_path.parent / "nowhere" / "state.json"
        cfg.save_state({"x": 1})  # must not raise
        assert not cfg.state_path.exists()


class TestStartupSweep:
    def test_removes_only_our_stale_tmps(self, tmp_path):
        import os
        import time

        for d in ("store", "store/media", "store/archive"):
            (tmp_path / d).mkdir(parents=True, exist_ok=True)
        stale = [
            tmp_path / "state.json.123.tmp",
            tmp_path / "state.json.tmp",  # legacy fixed name
            tmp_path / "store" / "timelines.cache.99.tmp",
            tmp_path / "store" / "timelines.cache.tmp",  # legacy
            tmp_path / "store" / "media" / "ab12.cache.7.tmp",
            tmp_path / "store" / "archive" / "cd34.cache.7.tmp",
        ]
        kept = [
            # Not ours: state_path.parent is user-configurable, so a bare
            # *.tmp glob would eat other applications' files.
            tmp_path / "some-other-app.tmp",
            # Ours but fresh: may be another instance's in-flight write.
            tmp_path / "state.json.456.tmp",
        ]
        old = time.time() - 7200
        for f in stale + kept:
            f.write_text("x")
        for f in stale + [kept[0]]:
            os.utime(f, (old, old))
        Config.load(minimal_config(tmp_path))
        assert not [f for f in stale if f.exists()]
        assert all(f.exists() for f in kept)


class TestInstanceLock:
    def test_second_acquire_refused_then_freed_after_kill(self, cfg):
        """The lock must block a second instance while the holder lives, name
        the holder's pid, and evaporate when the holder dies without cleanup:
        flock lives on the fd, so a stale instance.lock file left on disk by
        a SIGKILLed process must not keep new launches out."""
        import signal
        import subprocess
        import sys

        child = (
            "import sys, time\n"
            "from pathlib import Path\n"
            "from matrixcli.config import Config\n"
            "cfg = object.__new__(Config)\n"
            "cfg.store_path = Path(sys.argv[1])\n"
            "cfg.acquire_instance_lock()\n"
            "print('locked', flush=True)\n"
            "time.sleep(60)\n"
        )
        proc = subprocess.Popen(
            [sys.executable, "-c", child, str(cfg.store_path)],
            stdout=subprocess.PIPE,
            text=True,
        )
        try:
            assert proc.stdout.readline().strip() == "locked"
            with pytest.raises(SystemExit, match=f"pid {proc.pid}"):
                cfg.acquire_instance_lock()
        finally:
            proc.send_signal(signal.SIGKILL)
            proc.wait(timeout=10)
        assert (cfg.store_path / "instance.lock").exists()
        cfg.acquire_instance_lock()  # stale file, dead holder: must succeed


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
