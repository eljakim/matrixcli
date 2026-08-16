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
* ``store/timelines.cache`` and ``store/archive/<sha256(room id)>.cache``
  (secret): the per-room message windows plus one full-history archive file
  per room, so a restart paints rooms without refetching them. Message bodies
  include decrypted E2EE plaintext, so every file is AES-GCM encrypted with a
  key derived from the same Keychain store key that protects nio's crypto
  store. ``[cache] messages = false`` disables all of it; individual spaces
  opt out via ``cache_spaces`` in state.json (the "c" key on a space).
"""

from __future__ import annotations

import configparser
import hashlib
import json
import os
import secrets
from dataclasses import dataclass, field
from pathlib import Path

import keyring
from Crypto.Cipher import AES

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

[cache]
; Persist message history (including decrypted E2EE text, AES-encrypted at
; rest) so a restart paints rooms without refetching them. Set false to keep
; messages in memory only; any existing cache is deleted on the next launch.
; Individual spaces can also be excluded in-app ("c" on a space).
messages = true
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
    # Master switch for the encrypted on-disk message cache ([cache] messages).
    # False keeps history in memory only and wipes any existing cache files at
    # startup. Per-space opt-outs live in state.json ("cache_spaces"), not here.
    cache_messages: bool = True

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
        cache = parser["cache"] if parser.has_section("cache") else None
        try:
            cache_messages = (
                cache.getboolean("messages", fallback=True) if cache else True
            )
        except ValueError:
            cache_messages = True

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
            cache_messages=cache_messages,
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
        for key in (
            "last_event_ts",
            "last_opened_ts",
            "room_meta",
            "space_children",
            "cache_spaces",
        ):
            if not isinstance(state.get(key), dict):
                state[key] = {}
        return state

    def save_state(self, state: dict) -> None:
        tmp = self.state_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(state))
        tmp.replace(self.state_path)

    # --- Encrypted timeline cache (decrypted message windows at rest) ---

    @property
    def _timeline_cache_path(self) -> Path:
        # Lives in the store directory: created 0o700, and _reset_store wipes
        # its files, which is exactly right for a cache of decrypted content.
        return self.store_path / "timelines.cache"

    def _timeline_cache_aes_key(self) -> bytes:
        # Derived (domain-separated) from the Keychain store key rather than
        # stored anywhere itself; cached on the instance so every debounced
        # save does not round-trip the Keychain.
        key = getattr(self, "_timeline_key", None)
        if key is None:
            secret = "matrixcli-timeline-cache\0" + self.get_or_create_store_key()
            key = hashlib.sha256(secret.encode()).digest()
            self._timeline_key = key
        return key

    def _read_encrypted(self, path: Path) -> dict | None:
        """The decrypted payload of one cache file, or None for missing,
        corrupt, or foreign files (a failed GCM tag also lands here: wrong
        key or tampering)."""
        try:
            blob = path.read_bytes()
        except OSError:
            return None
        if len(blob) < 33 or blob[:1] != b"\x01":  # version byte + nonce + tag
            return None
        nonce, tag, ciphertext = blob[1:17], blob[17:33], blob[33:]
        try:
            cipher = AES.new(self._timeline_cache_aes_key(), AES.MODE_GCM, nonce=nonce)
            payload = json.loads(cipher.decrypt_and_verify(ciphertext, tag))
        except (ValueError, KeyError):
            return None
        return payload if isinstance(payload, dict) else None

    def _write_encrypted(self, path: Path, payload: dict) -> None:
        nonce = secrets.token_bytes(16)
        cipher = AES.new(self._timeline_cache_aes_key(), AES.MODE_GCM, nonce=nonce)
        ciphertext, tag = cipher.encrypt_and_digest(json.dumps(payload).encode())
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_bytes(b"\x01" + nonce + tag + ciphertext)
        tmp.replace(path)

    def load_timeline_cache(self) -> dict | None:
        return self._read_encrypted(self._timeline_cache_path)

    def save_timeline_cache(self, payload: dict) -> None:
        self._write_encrypted(self._timeline_cache_path, payload)

    # Full-history archives get one file per room so a save only rewrites the
    # rooms that changed (the main cache stays small and is rewritten whole).
    # Filenames are sha256(room id): stable, filesystem-safe, and the listing
    # of a stolen directory leaks no room ids.

    @property
    def _archive_dir(self) -> Path:
        return self.store_path / "archive"

    def _room_archive_path(self, room_id: str) -> Path:
        name = hashlib.sha256(room_id.encode()).hexdigest() + ".cache"
        return self._archive_dir / name

    def save_room_archive(self, room_id: str, payload: dict) -> None:
        self._archive_dir.mkdir(mode=0o700, exist_ok=True)
        self._write_encrypted(self._room_archive_path(room_id), payload)

    def load_room_archives(self) -> list[dict]:
        """Every readable per-room archive payload. Unreadable files are
        skipped, not deleted: the room simply re-downloads, and a foreign
        (other-account) file is not ours to destroy."""
        try:
            paths = sorted(self._archive_dir.glob("*.cache"))
        except OSError:
            return []
        out = []
        for path in paths:
            payload = self._read_encrypted(path)
            if payload is not None:
                out.append(payload)
        return out

    def clear_room_archive(self, room_id: str) -> None:
        try:
            self._room_archive_path(room_id).unlink()
        except OSError:
            pass

    def clear_timeline_cache(self) -> None:
        """Delete the whole message cache: the main file and every per-room
        archive (used by --import-keys and by `[cache] messages = false`)."""
        try:
            self._timeline_cache_path.unlink()
        except OSError:
            pass
        try:
            paths = list(self._archive_dir.glob("*.cache*"))
        except OSError:
            paths = []
        for path in paths:
            try:
                path.unlink()
            except OSError:
                pass
