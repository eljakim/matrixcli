"""Configuration, on-disk state, and system-keyring access for matrixcli.

Three kinds of persistence live here:

* ``config.ini`` (non-secret): homeserver, user id, device name, and the paths
  used for the crypto store and local state. Resolved from ``$MATRIXCLI_CONFIG``,
  then ``./config.ini``, then ``~/.config/matrixcli/config.ini``.
* The system keyring (via ``keyring``: macOS Keychain, Secret Service or
  KWallet on Linux): the login password (you store this yourself before first
  run) and, after the first login, a cached access token + device id so later
  launches never need the password again.
* ``state.json`` (non-secret): per-room "last event seen" and "last opened"
  timestamps, used to rank the home screen's Recent, Favourites, and DMs.
* ``store/timelines.cache``, ``store/archive/<sha256(room id)>.cache``, and
  ``store/media/<sha256(mxc url)>.cache`` (secret): the per-room message
  windows, one full-history archive file per room, and fetched image
  previews, so a restart paints rooms (and reopens previews) without
  refetching them. Message bodies and encrypted-room images are decrypted
  E2EE content, so every file is AES-GCM encrypted with a key derived from
  the same Keychain store key that protects nio's crypto store. ``[cache]
  messages = false`` disables all of it; individual spaces opt out via
  ``cache_spaces`` in state.json (the "c" key on a space).
"""

from __future__ import annotations

import configparser
import fcntl
import hashlib
import json
import os
import secrets
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

import keyring
from Crypto.Cipher import AES

# Paul Bourke's 70-level grayscale ramp (paulbourke.net/dataformats/asciiart/),
# ordered most ink first. The image preview maps the brightest pixels onto the
# densest glyphs, which is right for light text on a dark terminal; a light
# terminal wants the string reversed (see the template comments below).
DEFAULT_ASCII_RAMP = (
    "$@B%8&WM#*oahkbdpqwmZO0QLCJUYXzcvunxrjft/\\|()1{}[]?-_+~<>i!lI;:,\"^`'. "
)

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
; The keyring "service" name. Store your password before first run with:
;   macOS:  security add-generic-password -s "matrix-cli" -a "@you:matrix.org" -w
;   Linux:  keyring set matrix-cli @you:matrix.org
; (Linux needs a Secret Service keyring such as gnome-keyring, or KWallet.)
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

[preview]
; The character ramp for the ASCII-art image preview (space on an image),
; ordered most ink first. The brightest pixels get the densest glyphs, which
; suits light text on a dark terminal; reverse the string for a light
; terminal. Wrap the value in double quotes to keep a leading/trailing space.
; The default is Paul Bourke's 70-level ramp:
;   ascii_ramp = "$@B%8&WM#*oahkbdpqwmZO0QLCJUYXzcvunxrjft/\\|()1{}[]?-_+~<>i!lI;:,"^`'. "
; A classic shorter alternative:
;   ascii_ramp = "@%#*+=-:. "
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
    # Glyphs for the ASCII-art image preview, most ink first ([preview]
    # ascii_ramp). Read raw (the default ramp contains "%", which interpolation
    # would mangle); wrap the ini value in double quotes to keep edge spaces.
    ascii_ramp: str = DEFAULT_ASCII_RAMP

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

        # interpolation=None: with the default BasicInterpolation, a bare "%"
        # in any value raises lazily at m.get() time, past the except below.
        parser = configparser.ConfigParser(interpolation=None)
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

        # Sweep tmp files stranded by a crash between write and rename. Only
        # touch files at least an hour old: a fresh one may be another
        # instance's in-flight write. The patterns stay anchored to our own
        # tmp naming; a bare *.tmp glob over a user-configured, shared
        # directory would eat other apps' files.
        cutoff = time.time() - 3600
        sweeps = [
            (state_path.parent, state_path.name + ".*.tmp"),
            (state_path.parent, state_path.name + ".tmp"),
            (store_path, "*.cache.*.tmp"),
            (store_path, "*.cache.tmp"),
            (store_path / "media", "*.cache.*.tmp"),
            (store_path / "media", "*.cache.tmp"),
            (store_path / "archive", "*.cache.*.tmp"),
            (store_path / "archive", "*.cache.tmp"),
        ]
        for directory, pattern in sweeps:
            for stale in directory.glob(pattern):
                try:
                    if stale.stat().st_mtime < cutoff:
                        stale.unlink()
                except OSError:
                    pass

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

        ramp = parser.get("preview", "ascii_ramp", raw=True, fallback="")
        if len(ramp) >= 2 and ramp[0] == ramp[-1] == '"':
            ramp = ramp[1:-1]
        if len(ramp) < 2:  # unset, or too short to form a gradient
            ramp = DEFAULT_ASCII_RAMP

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
            ascii_ramp=ramp,
        )

    # --- Keychain: password (user-provided) and token cache (we write it) ---

    @property
    def _token_service(self) -> str:
        return f"{self.keychain_service}-token"

    def get_password(self) -> str | None:
        return keyring.get_password(self.keychain_service, self.user_id)

    def store_password_hint(self) -> str:
        """The platform's one-liner for putting the login password into the
        system keyring, quoted in error messages and docs. macOS has the
        Keychain's own tool; everywhere else the keyring package's bundled
        CLI talks to whatever backend is installed (Secret Service, KWallet)."""
        if sys.platform == "darwin":
            return (
                f'security add-generic-password -s "{self.keychain_service}" '
                f'-a "{self.user_id}" -w'
            )
        return f'keyring set "{self.keychain_service}" "{self.user_id}"'

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
            data = json.loads(raw)
        except (ValueError, TypeError):
            return None
        # A malformed keyring entry must fall back to password login, not
        # crash startup when the session indexes into it.
        if not isinstance(data, dict) or not isinstance(
            data.get("access_token"), str
        ) or not isinstance(data.get("device_id"), str):
            return None
        return data

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
            "settings",
        ):
            if not isinstance(state.get(key), dict):
                state[key] = {}
        return state

    def acquire_instance_lock(self) -> None:
        """Exclusive advisory lock held for the process lifetime, released by
        the OS on any exit. Two live instances share the nio crypto store,
        state.json, and the caches as unlocked whole-file writes: the loser's
        stale one-time-key state makes new messages permanently undecryptable,
        and its stale saves silently revert the other's cached history and
        privacy opt-outs. Raises SystemExit when another instance holds it."""
        path = self.store_path / "instance.lock"
        fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            holder = os.pread(fd, 32, 0).decode(errors="replace").strip()
            os.close(fd)
            raise SystemExit(
                "Another matrixcli instance is already running"
                + (f" (pid {holder})" if holder else "")
                + ". A second instance would corrupt the shared encryption "
                "store and caches; close it first."
            )
        os.ftruncate(fd, 0)
        os.pwrite(fd, str(os.getpid()).encode(), 0)
        self._instance_lock_fd = fd

    def save_state(self, state: dict) -> None:
        # The tmp name carries the pid: with a fixed name, a second running
        # instance can rename the file away between our write and replace,
        # crashing this one with FileNotFoundError.
        tmp = self.state_path.with_name(f"{self.state_path.name}.{os.getpid()}.tmp")
        try:
            tmp.write_text(json.dumps(state))
            tmp.replace(self.state_path)
        except OSError:
            # This snapshot only speeds up the next launch; a failed write
            # (disk full, dir deleted, permissions) must degrade to stale
            # data then, not crash the TUI out of a background worker.
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass

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
        tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
        tmp.write_bytes(b"\x01" + nonce + tag + ciphertext)
        tmp.replace(path)

    def load_timeline_cache(self) -> dict | None:
        return self._read_encrypted(self._timeline_cache_path)

    def save_timeline_cache(self, payload: dict) -> None:
        self._write_encrypted(self._timeline_cache_path, payload)

    # Fetched image previews, so reopening one (or coming back offline) skips
    # the network. Same at-rest protection as the timelines: the bytes stored
    # are post-decryption, so they are AES-GCM encrypted with the derived key
    # (version byte \x02: raw bytes, not JSON). Filenames are sha256(mxc url):
    # a stolen directory listing leaks nothing.

    MEDIA_CACHE_MAX_BYTES = 64 * 1024 * 1024

    @property
    def _media_cache_dir(self) -> Path:
        return self.store_path / "media"

    def _media_cache_path(self, key: str) -> Path:
        return self._media_cache_dir / (
            hashlib.sha256(key.encode()).hexdigest() + ".cache"
        )

    def load_media_cache(self, key: str) -> bytes | None:
        try:
            blob = self._media_cache_path(key).read_bytes()
        except OSError:
            return None
        if len(blob) < 33 or blob[:1] != b"\x02":
            return None
        nonce, tag, ciphertext = blob[1:17], blob[17:33], blob[33:]
        try:
            cipher = AES.new(self._timeline_cache_aes_key(), AES.MODE_GCM, nonce=nonce)
            return cipher.decrypt_and_verify(ciphertext, tag)
        except (ValueError, KeyError):
            return None

    def save_media_cache(self, key: str, data: bytes) -> None:
        if len(data) > self.MEDIA_CACHE_MAX_BYTES:
            # One over-cap blob would make the prune below evict everything,
            # itself included; not worth caching at all.
            return
        self._media_cache_dir.mkdir(mode=0o700, exist_ok=True)
        nonce = secrets.token_bytes(16)
        cipher = AES.new(self._timeline_cache_aes_key(), AES.MODE_GCM, nonce=nonce)
        ciphertext, tag = cipher.encrypt_and_digest(data)
        path = self._media_cache_path(key)
        tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
        tmp.write_bytes(b"\x02" + nonce + tag + ciphertext)
        tmp.replace(path)
        # Prune oldest-first past the cap, so a scroll through a photo-heavy
        # room cannot grow the directory without bound.
        try:
            files = [
                (p.stat().st_mtime, p.stat().st_size, p)
                for p in self._media_cache_dir.glob("*.cache")
            ]
        except OSError:
            return
        total = sum(size for _, size, _ in files)
        for _, size, p in sorted(files):
            if total <= self.MEDIA_CACHE_MAX_BYTES:
                break
            try:
                p.unlink()
                total -= size
            except OSError:
                pass

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
        """Delete the whole message cache: the main file, every per-room
        archive, and the media previews (used by --import-keys and by
        `[cache] messages = false`)."""
        try:
            self._timeline_cache_path.unlink()
        except OSError:
            pass
        paths = []
        for directory in (self._archive_dir, self._media_cache_dir):
            try:
                paths.extend(directory.glob("*.cache*"))
            except OSError:
                pass
        for path in paths:
            try:
                path.unlink()
            except OSError:
                pass
