"""Configuration, on-disk state, and system-keyring access for matrixcli.

Three kinds of persistence live here:

* ``config.ini`` (non-secret): homeserver, user id, device name, and the paths
  used for the crypto store and local state. Resolved from ``--config``, then
  ``$MATRIXCLI_CONFIG``, then ``~/.config/matrixcli/config.ini``. The current
  working directory is deliberately NOT searched; see _default_config_path.
* Secrets (see SecretStore): the login password, a cached access token +
  device id, and the key that encrypts everything below. The system keyring
  holds them (macOS Keychain, Secret Service or KWallet on Linux) when the
  machine has one; a headless box falls back to a 0600 file.
* ``state.json`` (0600, not encrypted): per-room "last event seen" and "last
  opened" timestamps, used to rank the home screen's Recent, Favourites, and
  DMs, plus the room titles and DM peers those rankings display. No message
  bodies, but it is the list of who you talk to, so it is not world-readable.
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
homeserver = __HOMESERVER__
; Your full Matrix user id.
user_id = __USER_ID__
; Shown to other users as the name of this login/session.
device_name = __DEVICE_NAME__
; Optional: a room id or alias to open by default (leave blank for the dashboard).
room =

[keychain]
; The name matrixcli files its secrets (password, access token, cache key)
; under in the system keyring: the macOS Keychain, or Secret Service/KWallet
; on Linux. Without a keyring they go to secrets.json next to state.json
; below, mode 0600, and the password is never written to disk at all.
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


def render_config(homeserver: str, user_id: str, device_name: str) -> str:
    """CONFIG_TEMPLATE with one account filled in, comments and all. Plain
    replacement rather than str.format or %: the default ASCII ramp in the
    template is full of braces and percent signs."""
    return (
        CONFIG_TEMPLATE.replace("__HOMESERVER__", homeserver)
        .replace("__USER_ID__", user_id)
        .replace("__DEVICE_NAME__", device_name or "matrixcli")
    )


class SecretStore:
    """Where the password, the access token, and the cache key are kept.

    First choice is the OS keyring (macOS Keychain, Secret Service or KWallet
    on Linux). A server or container often has none of those, and refusing to
    run there is worse than the alternative, so we fall back to a 0600 JSON
    file beside the app's other state. That fallback never receives the login
    password: it holds the access token, device id, and cache key, so a
    leaked file costs one revokable session instead of the account.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self._keyring_ok: bool | None = None
        # Values held for this process only (the password, when there is no
        # keyring to put it in).
        self._memory: dict[tuple[str, str], str] = {}

    @property
    def uses_keyring(self) -> bool:
        if self._keyring_ok is None:
            try:
                # A real lookup, not a look at which backend class was picked:
                # Secret Service imports fine on any desktop Linux but raises
                # here when no daemon is listening (ssh session, container).
                keyring.get_password("matrixcli-probe", "matrixcli-probe")
            except Exception:
                self._keyring_ok = False
            else:
                self._keyring_ok = True
        return self._keyring_ok

    def describe(self) -> str:
        """Short phrase naming where secrets land, for the setup screen."""
        if self.uses_keyring:
            if sys.platform == "darwin":
                return "the macOS Keychain"
            if sys.platform == "win32":
                return "the Windows Credential Locker"
            return "the system keyring"
        try:
            shown = "~/" + str(self.path.relative_to(Path.home()))
        except ValueError:
            shown = str(self.path)
        return f"{shown} (0600)"

    def get(self, service: str, key: str) -> str | None:
        if (service, key) in self._memory:
            return self._memory[(service, key)]
        if self.uses_keyring:
            try:
                value = keyring.get_password(service, key)
            except Exception:
                value = None
            if value is not None:
                return value
        entry = self._read().get(service)
        return entry.get(key) if isinstance(entry, dict) else None

    def set(
        self, service: str, key: str, value: str, *, persist: bool = True
    ) -> None:
        """Store a secret. ``persist=False`` keeps it in memory for this run
        only, for secrets too sensitive for the file fallback."""
        if not persist:
            self._memory[(service, key)] = value
            return
        if self.uses_keyring:
            try:
                keyring.set_password(service, key, value)
                return
            except Exception:
                # Readable but not writable (a locked collection): fall
                # through to the file rather than lose the session.
                pass
        data = self._read()
        entry = data.get(service)
        if not isinstance(entry, dict):
            entry = data[service] = {}
        entry[key] = value
        self._write(data)

    def delete(self, service: str, key: str) -> None:
        self._memory.pop((service, key), None)
        if self.uses_keyring:
            try:
                keyring.delete_password(service, key)
            except Exception:
                pass
        data = self._read()
        entry = data.get(service)
        if isinstance(entry, dict) and entry.pop(key, None) is not None:
            self._write(data)

    def _read(self) -> dict:
        try:
            data = json.loads(self.path.read_text())
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def _write(self, data: dict) -> None:
        # 0600 from the moment it exists: this file is the login session.
        tmp = self.path.with_name(f"{self.path.name}.{os.getpid()}.tmp")
        try:
            self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as fh:
                json.dump(data, fh)
            tmp.replace(self.path)
        except OSError:
            # Losing the token costs a fresh login next launch; crashing the
            # app in the middle of one costs the whole session.
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass


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
        """The config at ``path``. A missing file is not an error: it yields
        an unconfigured Config (``needs_setup``), which the app fills in from
        its setup screen and writes with write_account. Nothing is created
        on disk here except the storage directories."""
        path = path or _default_config_path()
        # interpolation=None: with the default BasicInterpolation, a bare "%"
        # in any value raises lazily at m.get() time, past the except below.
        parser = configparser.ConfigParser(interpolation=None)
        if path.exists():
            try:
                parser.read(path)
            except configparser.Error as exc:
                raise ValueError(f"Could not parse {path}: {exc}")
            if not parser.has_section("matrix"):
                raise ValueError(
                    f"{path} has no [matrix] section. Fix it, or delete the "
                    "file and rerun to set the account up again."
                )
        else:
            parser.add_section("matrix")
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
        state_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)

        # Sweep tmp files stranded by a crash between write and rename. Only
        # touch files at least an hour old: a fresh one may be another
        # instance's in-flight write. The patterns stay anchored to our own
        # tmp naming; a bare *.tmp glob over a user-configured, shared
        # directory would eat other apps' files.
        cutoff = time.time() - 3600
        sweeps = [
            (state_path.parent, state_path.name + ".*.tmp"),
            (state_path.parent, state_path.name + ".tmp"),
            (state_path.parent, "secrets.json.*.tmp"),
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

    @property
    def needs_setup(self) -> bool:
        """True when there is no real account to connect with yet: no config
        file, an unedited template, or a half-filled one. The app answers this
        with its setup screen instead of an error message."""
        return (
            not self.homeserver
            or not self.user_id
            or "@you:" in self.user_id
        )

    def write_account(
        self, homeserver: str, user_id: str, device_name: str = ""
    ) -> "Config":
        """Save an account to config.ini and return the config read back from
        it. A file that already exists keeps its other sections, and its
        comments, byte for byte: only the three account keys are rewritten."""
        device_name = device_name or self.device_name or "matrixcli"
        wanted = {
            "homeserver": homeserver,
            "user_id": user_id,
            "device_name": device_name,
        }
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        if not self.config_path.exists():
            self.config_path.write_text(
                render_config(homeserver, user_id, device_name)
            )
            return Config.load(self.config_path)

        out: list[str] = []
        section = ""
        header_at = None  # where to add keys [matrix] does not have yet
        seen: set[str] = set()

        def close_matrix() -> None:
            missing = [f"{k} = {v}" for k, v in wanted.items() if k not in seen]
            if missing and header_at is not None:
                out[header_at + 1:header_at + 1] = missing

        for line in self.config_path.read_text().splitlines():
            stripped = line.strip()
            if stripped.startswith("[") and stripped.endswith("]"):
                if section == "matrix":
                    close_matrix()
                section = stripped[1:-1].strip().lower()
                if section == "matrix":
                    header_at = len(out)
            elif (
                section == "matrix"
                and "=" in stripped
                and not stripped.startswith((";", "#"))
            ):
                key = stripped.split("=", 1)[0].strip().lower()
                if key in wanted:
                    seen.add(key)
                    out.append(f"{key} = {wanted[key]}")
                    continue
            out.append(line)
        if section == "matrix":
            close_matrix()
        self.config_path.write_text("\n".join(out) + "\n")
        return Config.load(self.config_path)

    # --- Secrets: password (asked for once) and token cache (we write it) ---

    @property
    def secrets(self) -> SecretStore:
        store = getattr(self, "_secrets", None)
        if store is None:
            store = SecretStore(self.state_path.parent / "secrets.json")
            self._secrets = store
        return store

    @property
    def _token_service(self) -> str:
        return f"{self.keychain_service}-token"

    def get_password(self) -> str | None:
        return self.secrets.get(self.keychain_service, self.user_id)

    def set_password(self, password: str) -> None:
        """Remember the password typed into the setup screen. With a system
        keyring it goes there, so later launches can log in unattended even
        after the token is revoked. Without one it would land in a plain
        file, so it stays in memory for this run only: the access token
        cached by this login is what the next launch uses, and if that dies
        the app asks again."""
        self.secrets.set(
            self.keychain_service,
            self.user_id,
            password,
            persist=self.secrets.uses_keyring,
        )

    def get_or_create_store_key(self) -> str:
        """A per-account random key used to encrypt nio's Olm/Megolm store on
        disk, kept in the Keychain. Without this nio falls back to its literal
        hardcoded default, leaving the device identity and every inbound room
        session effectively at rest in the clear (any backup, synced folder, or
        malware running as the user recovers them). Generated once on first use.
        """
        service = f"{self.keychain_service}-store"
        key = self.secrets.get(service, self.user_id)
        if not key:
            key = secrets.token_urlsafe(32)
            self.secrets.set(service, self.user_id, key)
        return key

    def load_token(self) -> dict | None:
        raw = self.secrets.get(self._token_service, self.user_id)
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
        self.secrets.set(
            self._token_service,
            self.user_id,
            json.dumps({"access_token": access_token, "device_id": device_id}),
        )

    def clear_token(self) -> None:
        self.secrets.delete(self._token_service, self.user_id)

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
            # 0600, not the umask default: room_meta is the full list of who
            # you talk to (DM peers' MXIDs, room names, when you last opened
            # each), and the parent directory may be a user-configured shared
            # one, so the file's own mode is the only guard we control.
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as fh:
                json.dump(state, fh)
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
