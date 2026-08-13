import json

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
        assert cfg.load_state() == state
        assert not cfg.state_path.with_suffix(".json.tmp").exists()

    def test_corrupt_file_falls_back_to_defaults(self, cfg):
        cfg.state_path.write_text("{not json")
        assert cfg.load_state() == {
            "last_event_ts": {},
            "last_opened_ts": {},
            "room_meta": {},
            "space_children": {},
        }

    def test_non_dict_json_falls_back_to_defaults(self, cfg):
        cfg.state_path.write_text(json.dumps([1, 2, 3]))
        assert cfg.load_state() == {
            "last_event_ts": {},
            "last_opened_ts": {},
            "room_meta": {},
            "space_children": {},
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
