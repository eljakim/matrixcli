"""Configuration, on-disk state, and macOS Keychain access for matrixcli.

Three kinds of persistence live here:

* ``config.ini`` (non-secret): homeserver, user id, device name, and the paths
  used for the crypto store and local state. Resolved from ``$MATRIXCLI_CONFIG``,
  then ``./config.ini``, then ``~/.config/matrixcli/config.ini``.
* The macOS Keychain (via ``keyring``): the login password (you store this
  yourself before first run) and, after the first login, a cached access token +
  device id so later launches never need the password again.
* ``state.json`` (non-secret): per-room "last event seen" and "last opened"
  timestamps, used to rank the home screen's Recent, Favourites, and DMs.
"""

from __future__ import annotations

import configparser
import json
import os
import secrets
from dataclasses import dataclass, field
from pathlib import Path

import keyring

CONFIG_TEMPLATE = """\
[matrix]
; Your homeserver's base URL.
homeserver = https://matrix.org
; Your full Matrix user id.
user_id = @you:matrix.org
; Shown to other users as the name of this login/session.
device_name = matrixcli
; Optional: a room id or alias to open by default (leave blank for the dashboard).
room =

[keychain]
; The Keychain "service" name. Store your password before first run with:
;   security add-generic-password -s "matrix-cli" -a "@you:matrix.org" -w
service = matrix-cli

[storage]
; Where nio keeps its encryption store and where we keep local UI state.
; "~" is expanded. Defaults are under ~/.local/share/matrixcli/ when blank.
store_path =
state_path =
"""


def _default_config_path() -> Path:
    # Deliberately NOT the current working directory: a config.ini dropped into
    # a directory you happen to run `matrix` from could point keychain_service +
    # user_id at your real credentials while redirecting homeserver to an
    # attacker, exfiltrating the Keychain password at login. Only an explicit
    # $MATRIXCLI_CONFIG (or --config) and the fixed per-user path are trusted.
    env = os.environ.get("MATRIXCLI_CONFIG")
    if env:
        return Path(env).expanduser()
    return Path.home() / ".config" / "matrixcli" / "config.ini"


@dataclass
class Config:
    homeserver: str
    user_id: str
    device_name: str
    room: str
    keychain_service: str
    store_path: Path
    state_path: Path
    config_path: Path = field(default_factory=Path)
    # Encrypt to unverified/unknown recipient devices without prompting. True
    # keeps a CLI usable when peers rotate devices; set `allow_unverified = false`
    # in [matrix] to refuse (a device silently added to a recipient can no
    # longer receive your plaintext).
    allow_unverified: bool = True

    @classmethod
    def load(cls, path: Path | None = None) -> "Config":
        path = path or _default_config_path()
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(CONFIG_TEMPLATE)
            raise FileNotFoundError(
                f"No config found. A template was written to {path}.\n"
                "Edit it with your homeserver and user id, then store your "
                "password in the Keychain (see the comments in that file)."
            )

        parser = configparser.ConfigParser()
        try:
            parser.read(path)
        except configparser.Error as exc:
            raise ValueError(f"Could not parse {path}: {exc}")
        if not parser.has_section("matrix"):
            raise ValueError(
                f"{path} has no [matrix] section. Fix it, or delete the file "
                "and rerun to regenerate the template."
            )
        m = parser["matrix"]
        kc = parser["keychain"] if parser.has_section("keychain") else {}
        st = parser["storage"] if parser.has_section("storage") else {}

        data_dir = Path.home() / ".local" / "share" / "matrixcli"
        store_raw = (st.get("store_path") or "").strip()
        state_raw = (st.get("state_path") or "").strip()
        store_path = Path(store_raw).expanduser() if store_raw else data_dir / "store"
        state_path = Path(state_raw).expanduser() if state_raw else data_dir / "state.json"

        # Create the store directory already restricted, so there is no window
        # in which it exists with umask-default perms: it holds the device's
        # Olm/Megolm key material and, in older stores, the pickle key was the
        # hardcoded nio default, so directory perms were the only at-rest guard.
        store_path.mkdir(mode=0o700, parents=True, exist_ok=True)
        store_path.chmod(0o700)  # tighten if it pre-existed under a looser mode
        state_path.parent.mkdir(parents=True, exist_ok=True)

        try:
            allow_unverified = m.getboolean("allow_unverified", fallback=True)
        except ValueError:
            allow_unverified = True

        return cls(
            homeserver=m.get("homeserver", "").strip(),
            user_id=m.get("user_id", "").strip(),
            device_name=(m.get("device_name") or "matrixcli").strip(),
            room=(m.get("room") or "").strip(),
            keychain_service=(kc.get("service") or "matrix-cli").strip(),
            store_path=store_path,
            state_path=state_path,
            config_path=path,
            allow_unverified=allow_unverified,
        )

    # --- Keychain: password (user-provided) and token cache (we write it) ---

    @property
    def _token_service(self) -> str:
        return f"{self.keychain_service}-token"

    def get_password(self) -> str | None:
        return keyring.get_password(self.keychain_service, self.user_id)

    def get_or_create_store_key(self) -> str:
        """A per-account random key used to encrypt nio's Olm/Megolm store on
        disk, kept in the Keychain. Without this nio falls back to its literal
        hardcoded default, leaving the device identity and every inbound room
        session effectively at rest in the clear (any backup, synced folder, or
        malware running as the user recovers them). Generated once on first use.
        """
        service = f"{self.keychain_service}-store"
        key = keyring.get_password(service, self.user_id)
        if not key:
            key = secrets.token_urlsafe(32)
            keyring.set_password(service, self.user_id, key)
        return key

    def load_token(self) -> dict | None:
        raw = keyring.get_password(self._token_service, self.user_id)
        if not raw:
            return None
        try:
            return json.loads(raw)
        except (ValueError, TypeError):
            return None

    def save_token(self, access_token: str, device_id: str) -> None:
        keyring.set_password(
            self._token_service,
            self.user_id,
            json.dumps({"access_token": access_token, "device_id": device_id}),
        )

    def clear_token(self) -> None:
        try:
            keyring.delete_password(self._token_service, self.user_id)
        except keyring.errors.PasswordDeleteError:
            pass

    # --- Local UI state (recency ranking) ---

    def load_state(self) -> dict:
        state: dict = {}
        if self.state_path.exists():
            try:
                loaded = json.loads(self.state_path.read_text())
                if isinstance(loaded, dict):
                    state = loaded
            except (ValueError, OSError):
                pass
        # Callers index these directly; guarantee well-typed dicts even if
        # state.json predates a key or was edited by hand (a null or list
        # value would crash the first .get on it).
        for key in ("last_event_ts", "last_opened_ts", "room_meta", "space_children"):
            if not isinstance(state.get(key), dict):
                state[key] = {}
        return state

    def save_state(self, state: dict) -> None:
        tmp = self.state_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(state))
        tmp.replace(self.state_path)
