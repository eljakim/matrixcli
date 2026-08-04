"""Thin async wrapper around matrix-nio that powers the dashboard.

It owns the ``AsyncClient`` lifecycle (restore-from-token or password login),
keeps the encryption store warm, observes timeline events to track recency, and
derives the three home-screen panels:

* **Unread** -- direct chats (people) that currently have unread notifications.
* **People** -- the most-recently-active direct chats *not* already in Unread.
* **Rooms** -- group rooms with unread messages, plus the rooms you most
  recently opened.

Recency for People comes from message timestamps Matrix gives us; recency for
"recently opened" Rooms is tracked locally (Matrix has no such concept) and
stamped whenever you open a room in the UI.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import time
import unicodedata
from collections import defaultdict, deque
from dataclasses import dataclass, replace
from pathlib import Path
from urllib.parse import quote

_verify_log = logging.getLogger("matrixcli.verify")

import aiohttp

from nio import (
    AsyncClient,
    AsyncClientConfig,
    KeyVerificationCancel,
    KeyVerificationKey,
    KeyVerificationMac,
    JoinError,
    KeyVerificationStart,
    LoginError,
    LocalProtocolError,
    MatrixRoom,
    MegolmEvent,
    MessageDirection,
    PresenceEvent,
    RoomEncryptedMedia,
    RoomMessage,
    RoomMessageMedia,
    RoomMessagesError,
    SyncError,
    ToDeviceError,
    ToDeviceMessage,
    UnknownToDeviceEvent,
)

from nio.events.room_events import Event

from .config import Config

HISTORY_LIMIT = 40
TIMELINE_CAP = 200
# Refuse to buffer/decrypt/write an attachment bigger than this. nio's download
# reads the whole body into memory, so an unbounded one is a trivial OOM; the
# sender also controls the advertised size, so both are checked.
MAX_DOWNLOAD_BYTES = 512 * 1024 * 1024

# Control characters, C1 codes, DEL, and bidi overrides. Server-supplied strings
# (message bodies, display names, filenames, room titles) are rendered straight
# to the terminal; an unstripped ESC (0x1b) lets a remote sender emit OSC/CSI
# sequences (clipboard writes via OSC 52, cursor moves that forge earlier lines,
# title changes). Tab (0x09) and newline (0x0a) are kept.
_UNSAFE_RE = re.compile(
    "[\x00-\x08\x0b-\x1f\x7f-\x9f\u202a-\u202e\u2066-\u2069]"
)


def _clean(text: str | None) -> str:
    """Strip terminal-control and bidi-override characters from an untrusted
    server string before it can reach the renderer."""
    if not text:
        return text or ""
    return _UNSAFE_RE.sub("", text)


def _sas_emoji(sas) -> list[tuple[str, str]]:
    """The seven SAS emoji for a vodozemac-backed nio ``Sas``, as (glyph, name).

    Maps vodozemac's already-final ``emoji_indices`` straight onto nio's emoji
    table. nio's own ``Sas.get_emoji()`` must NOT be used here: it re-packs those
    indices into 8-bit binary and regroups them into 6-bit chunks (correct for
    the old libolm raw-bytes API, wrong for vodozemac, which hands back the
    final indices), scrambling the result so it never matches the peer's emoji.
    See the nio-compatibility note in ``verify_interactive``.
    """
    from nio.crypto.sas import Sas

    indices = sas.established_sas.bytes(sas._extra_info).emoji_indices
    return [Sas.emoji[i] for i in indices]


@dataclass
class Entry:
    """A row on the dashboard: a person (DM) or a group room."""

    room_id: str
    title: str
    unread: int
    is_direct: bool
    person: str | None
    last_ts: int
    is_space: bool = False
    online: bool = False
    is_favourite: bool = False
    is_invite: bool = False  # a pending invite, not a joined room


@dataclass
class Message:
    sender: str
    sender_name: str
    body: str
    ts: int
    event_id: str = ""
    thread_root: str = ""  # event id of the thread this message replies in
    thread_count: int = 0  # server-aggregated reply count (thread roots only)
    media_url: str = ""  # mxc:// URL when this message is an uploaded file
    media_name: str = ""  # upload filename (body may be a caption)
    media_size: int = 0  # bytes, from content.info, 0 if unknown
    media_crypt: dict | None = None  # key/iv/hash for encrypted attachments


def _media_info(event) -> dict:
    """Message kwargs for an uploaded file (image/file/video/audio), empty for
    ordinary text events. Encrypted rooms wrap the attachment key material in
    the event content; it is captured here so download can decrypt."""
    if not isinstance(event, (RoomMessageMedia, RoomEncryptedMedia)):
        return {}
    content = (getattr(event, "source", {}) or {}).get("content", {}) or {}
    info = content.get("info") or {}
    out = {
        "media_url": event.url or "",
        "media_name": _clean(content.get("filename") or getattr(event, "body", "") or ""),
        "media_size": info.get("size", 0) or 0,
    }
    if isinstance(event, RoomEncryptedMedia):
        out["media_crypt"] = {
            "key": (event.key or {}).get("k", ""),
            "iv": event.iv,
            "sha256": (event.hashes or {}).get("sha256", ""),
        }
    return out


def _thread_info(event) -> tuple[str, int]:
    """(thread root id, aggregated reply count) for a nio event, from its raw
    source. Call it on the wire-format event: an encrypted event's wrapper
    carries m.relates_to in cleartext (the spec requires it so servers can
    aggregate) plus the server's ``unsigned`` m.relations counts, while nio's
    *decrypted* events keep only a copied m.relates_to and lose ``unsigned``
    (and with it the aggregated count) entirely."""
    src = getattr(event, "source", {}) or {}
    content = src.get("content", {}) or {}
    relates = content.get("m.relates_to") or {}
    root = ""
    if isinstance(relates, dict) and relates.get("rel_type") == "m.thread":
        root = relates.get("event_id") or ""
    unsigned = src.get("unsigned", {}) or {}
    relations = unsigned.get("m.relations") or {}
    thread = relations.get("m.thread") if isinstance(relations, dict) else None
    count = thread.get("count", 0) or 0 if isinstance(thread, dict) else 0
    return root, count


class MatrixSession:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.state = cfg.load_state()
        self.my_name = cfg.user_id  # our own display name, filled on first sync
        self.direct_by_room: dict[str, str] = {}  # room_id -> other user id
        self.timelines: dict[str, deque[Message]] = defaultdict(
            lambda: deque(maxlen=TIMELINE_CAP)
        )
        self.last_event_id: dict[str, str] = {}  # room_id -> latest event id seen
        self.presence: dict[str, str] = {}  # user_id -> presence ("online" etc.)
        self.space_children: dict[str, set[str]] = {}  # space id -> child room ids
        # Back-pagination state per room: the /messages token to continue
        # from, and whether the very beginning of history has been reached.
        self.pagination_tokens: dict[str, str] = {}
        self.pagination_done: dict[str, bool] = {}

        token = cfg.load_token()
        self._new_client(token["device_id"] if token else None)

    def _new_client(self, device_id: str | None = None) -> None:
        """(Re)build the AsyncClient. The encryption store binds to the device
        id at load time, so whenever the session's device id changes the client
        must be rebuilt rather than reused with a mismatched store."""
        client_config = AsyncClientConfig(
            store_sync_tokens=True,
            encryption_enabled=True,
            # Encrypt the on-disk Olm/Megolm store with a per-account random key
            # from the Keychain instead of nio's hardcoded default.
            pickle_key=self.cfg.get_or_create_store_key(),
        )
        self.client = AsyncClient(
            homeserver=self.cfg.homeserver,
            user=self.cfg.user_id,
            device_id=device_id,
            store_path=str(self.cfg.store_path),
            config=client_config,
        )
        self.client.add_event_callback(self._on_message, RoomMessage)
        self.client.add_presence_callback(self._on_presence, PresenceEvent)

    # --- connection -------------------------------------------------------

    async def connect(self, progress=None) -> tuple[bool, str]:
        """Restore from a cached token if possible, else log in with the
        Keychain password and cache a fresh token. ``progress(msg)`` if given is
        called with short status strings for the loading screen."""
        def step(msg: str) -> None:
            if progress is not None:
                progress(msg)

        insecure = self._insecure_homeserver()
        if insecure:
            return False, (
                f"refusing to connect over an insecure URL ({self.cfg.homeserver}): "
                "the access token would cross the network in cleartext. Use an "
                "https:// homeserver (http:// is allowed only for localhost)."
            )

        store_reset = False
        token = self.cfg.load_token()
        if token:
            step("restoring saved session")
            try:
                # restore_login loads the encryption store internally (nio calls
                # load_store() from here when encryption is enabled), so a store
                # written under the old pickle key fails RIGHT HERE with a
                # MAC-mismatch pickle error, not at any later load_store() call.
                self.client.restore_login(
                    user_id=self.cfg.user_id,
                    device_id=token["device_id"],
                    access_token=token["access_token"],
                )
            except Exception:
                # The store cannot be opened, almost always because it predates
                # the per-account store key (it was encrypted with nio's old
                # hardcoded default). It is unrecoverable with the new key, so
                # discard the stale session and log in fresh: a new, properly
                # keyed store is created and the user re-imports room keys.
                step("encryption store unreadable; resetting for a fresh login")
                self.cfg.clear_token()
                self._reset_store()
                token = None
                store_reset = True
            else:
                step("checking session with server")
                whoami = await self.client.whoami()
                # Errors come back as WhoamiError, which has no user_id attribute.
                if getattr(whoami, "user_id", None):
                    if whoami.user_id != self.cfg.user_id:
                        # The cached token authenticates a different account than
                        # this config's user_id; the local crypto store is built
                        # for cfg.user_id, so using it would mix identities.
                        self.cfg.clear_token()
                        return False, (
                            "the saved token belongs to "
                            f"{whoami.user_id}, not {self.cfg.user_id}; cleared it. "
                            "Rerun to log in fresh."
                        )
                    if self.client.should_upload_keys:
                        step("uploading encryption keys")
                        await self.client.keys_upload()
                    return True, "restored session from Keychain token"
                if getattr(whoami, "status_code", None) != "M_UNKNOWN_TOKEN":
                    # A transient server problem (proxy 502, timeout), not a
                    # rejected token: keep the cached token and fail this launch.
                    # Falling through to password login here would discard a valid
                    # session and mint a new device id, orphaning the old one's
                    # encryption state.
                    detail = (
                        getattr(whoami, "message", "")
                        or getattr(whoami, "status_code", "")
                        or "unknown error"
                    )
                    return False, (
                        f"could not verify the saved session: {detail}\n"
                        "The cached token was kept; try again in a moment."
                    )
                # Token rejected (expired/revoked): fall to password login.
                self.cfg.clear_token()

        password = self.cfg.get_password()
        if not password:
            if store_reset:
                return False, (
                    "Your encryption store could not be opened (it was written "
                    "under the previous store key) and has been reset. No login "
                    "password is in the Keychain to sign in fresh.\n"
                    f'Store one with: security add-generic-password -s '
                    f'"{self.cfg.keychain_service}" -a "{self.cfg.user_id}" -w\n'
                    "then relaunch, run 'matrix --verify', and "
                    "'matrix --import-keys <your key export>' to restore history."
                )
            return (
                False,
                "No cached token and no password in Keychain.\n"
                f'Store one with: security add-generic-password -s '
                f'"{self.cfg.keychain_service}" -a "{self.cfg.user_id}" -w',
            )

        step("logging in with password")
        # The token path may already have loaded the store for the now-revoked
        # device id (restore_login loads it before whoami can reject the
        # token). Remember that device id: if the server mints a DIFFERENT one
        # at login, carrying the loaded store over would keep signing with the
        # old device's olm account under the new device id.
        stale_device = self.client.device_id if self.client.store else None
        try:
            resp = await self.client.login(password, device_name=self.cfg.device_name)
        except Exception as exc:
            # On the no-token path login() is what loads the encryption store
            # (nio sets the access token, then calls load_store), so a store
            # written under the previous pickle key raises HERE, the same
            # failure the restore path catches at restore_login. The access
            # token tells the two raises apart: without one the request itself
            # failed (network), and wiping the store would be wrong.
            if not self.client.access_token:
                return False, f"login failed: {exc}"
            step("encryption store unreadable; resetting and retrying login")
            # Best effort: the server already minted a session for us before
            # the store raise; sign it out so it does not linger as an
            # orphaned device in the account's session list.
            try:
                await self.client.logout()
            except Exception:
                pass
            try:
                await self.client.close()
            except Exception:
                pass
            self._reset_store()
            store_reset = True
            resp = await self.client.login(password, device_name=self.cfg.device_name)
        if isinstance(resp, LoginError):
            return False, f"login failed: {resp.message}"
        if stale_device and resp.device_id != stale_device:
            await self.client.close()
            self._new_client(resp.device_id)
            self.client.restore_login(
                user_id=self.cfg.user_id,
                device_id=resp.device_id,
                access_token=resp.access_token,
            )
        self.cfg.save_token(resp.access_token, resp.device_id)
        self.client.load_store()
        if self.client.should_upload_keys:
            step("uploading encryption keys")
            await self.client.keys_upload()
        if store_reset:
            return True, (
                "encryption store was reset and you were logged in fresh as a new "
                "device. Run 'matrix --verify' and 'matrix --import-keys "
                "<your key export>' to restore encrypted history."
            )
        return True, "logged in with Keychain password (token cached)"

    def _insecure_homeserver(self) -> bool:
        """True when the homeserver URL would send the bearer token in the
        clear. Plain http:// is tolerated only for loopback (local testing)."""
        hs = (self.cfg.homeserver or "").strip()
        if hs.startswith("https://"):
            return False
        host = hs.split("://", 1)[-1].split("/", 1)[0].split(":", 1)[0]
        return host not in ("localhost", "127.0.0.1", "::1", "[::1]")

    def _reset_store(self) -> None:
        """Delete an unreadable crypto store and rebuild a fresh AsyncClient so
        the next login starts with a store keyed by the current pickle key."""
        try:
            store = Path(self.cfg.store_path)
            for child in store.glob("*"):
                if child.is_file():
                    child.unlink()
        except OSError:
            pass
        self._new_client()

    async def import_keys(self, infile: str, passphrase: str) -> None:
        """Import Megolm room keys from an Element 'Export E2E room keys' file.

        matrix-nio has no server-side key-backup (SSSS) support, so this is how
        you get history that predates this device: export the keys from a client
        that already has them (Element: Settings -> Security & Privacy -> Export
        E2E room keys) and load them here. Raises EncryptionError on a bad file
        or wrong passphrase, or the usual OSError if the file can't be read.
        Clears cached decrypt-failure placeholders so the next history load
        re-decrypts with the new keys."""
        await self.client.import_keys(infile, passphrase)
        self.timelines.clear()

    async def initial_sync(self, progress=None) -> None:
        def step(msg: str) -> None:
            if progress is not None:
                progress(msg)

        step("loading direct-message list")
        await self._refresh_direct_map()
        name_resp = await self.client.get_displayname()
        if getattr(name_resp, "displayname", None):
            self.my_name = name_resp.displayname
        # Force one fresh (non-incremental) sync so the server returns recent
        # timelines for ALL rooms, seeding last-activity timestamps. With only
        # the stored token, quiet rooms return empty timelines and would rank as
        # if silent (so e.g. yesterday's DM disappears). Clearing next_batch
        # makes nio sync from scratch; it then stores a new token and the live
        # loop continues incrementally from there. set_presence asks the server
        # to send presence for our contacts.
        # Clear BOTH token fields: nio resolves the sync position as
        # `next_batch or loaded_sync_token`, and loaded_sync_token is restored
        # from the store (store_sync_tokens=True). Clearing only next_batch still
        # resumes from the stored token, so quiet rooms return empty timelines
        # and never seed a timestamp. Zeroing both forces a true from-scratch
        # sync that returns recent timelines for every room.
        self.client.next_batch = ""
        self.client.loaded_sync_token = ""
        # No lazy_load_members: we need full member state so user_name() can
        # resolve display names (otherwise DMs show as raw @user:server ids).
        sync_filter = {
            "room": {"timeline": {"limit": 10}},
            "presence": {"limit": 1000},
        }
        step("syncing rooms and messages")
        # Non-429 errors come back immediately as SyncError (nio only retries
        # rate limits itself); retry a few times rather than silently
        # presenting an empty dashboard as "ready".
        for attempt in range(3):
            resp = await self.client.sync(
                timeout=30000,
                full_state=True,
                sync_filter=sync_filter,
                set_presence="online",
            )
            if not isinstance(resp, SyncError):
                break
            detail = (
                getattr(resp, "message", "")
                or getattr(resp, "status_code", "")
                or "error"
            )
            step(f"initial sync failed ({detail}); retrying")
            await asyncio.sleep(2)
        if isinstance(resp, SyncError):
            step("initial sync failed; showing cached data, the background sync will keep retrying")
        step("loading space hierarchy")
        await self.refresh_space_children()
        step("organizing dashboard")
        self._record_room_timestamps(resp)
        self._persist_recency()

    async def _on_presence(self, event: PresenceEvent) -> None:
        self.presence[event.user_id] = event.presence

    def _record_room_timestamps(self, response) -> None:
        """Record the latest event timestamp per room from a sync response so
        recency ranking matches what the server knows, not just the messages we
        happened to receive a typed callback for. Without this, most rooms keep
        last_ts == 0 and People/Rooms come out in arbitrary order."""
        rooms = getattr(getattr(response, "rooms", None), "join", {}) or {}
        changed = False
        for room_id, joined in rooms.items():
            events = getattr(getattr(joined, "timeline", None), "events", []) or []
            for ev in events:
                ts = getattr(ev, "server_timestamp", 0) or 0
                if ts > self.state["last_event_ts"].get(room_id, 0):
                    self.state["last_event_ts"][room_id] = ts
                    changed = True
                # Track the newest event id too, so mark_read works even in
                # rooms whose events never reach the RoomMessage callback
                # (e.g. encrypted events we lack keys for). Timeline events
                # arrive in stream order, so the last one wins; the timestamp
                # is deliberately not consulted (see _on_message).
                event_id = getattr(ev, "event_id", "")
                if event_id:
                    self.last_event_id[room_id] = event_id
        if changed:
            self._persist_recency()

    async def close(self) -> None:
        await self.client.close()

    # --- interactive SAS (emoji) device verification ----------------------

    async def list_own_devices(self) -> list:
        """Return this account's *other* OlmDevices (everything but this CLI
        session). nio only queries keys for users it shares encrypted rooms
        with, so our own user is usually untracked after a token restore; force
        it into the key-query set before querying."""
        await self.client.sync(timeout=10000)
        if self.cfg.user_id not in self.client.olm.tracked_users:
            self.client.olm.add_changed_users({self.cfg.user_id})
        try:
            await self.client.keys_query()
        except LocalProtocolError:
            pass  # nothing to query
        await self.client.sync(timeout=10000)
        devices = list(self.client.device_store.active_user_devices(self.cfg.user_id))
        return [d for d in devices if d.device_id != self.client.device_id]

    async def prepare_verification(self) -> None:
        """Make our own devices' keys known (so nio can build the SAS object for
        the incoming start) and drain any stale to-device backlog from earlier
        cancelled attempts, before we start listening. Draining happens here,
        with no callbacks registered, so old request/cancel events don't trip
        the live handler."""
        await self.client.sync(timeout=3000)
        if self.cfg.user_id not in self.client.olm.tracked_users:
            self.client.olm.add_changed_users({self.cfg.user_id})
        try:
            await self.client.keys_query()
        except LocalProtocolError:
            pass
        # A couple of quick syncs flush (and mark processed) any redelivered
        # to-device events from previous runs.
        await self.client.sync(timeout=2000)
        await self.client.sync(timeout=2000)

    async def verify_interactive(self, confirm, announce=None) -> str:
        """Respond to an emoji (SAS) verification that you start from Element.

        matrix-nio only models the legacy ``m.key.verification.start``
        handshake, not the ``request -> ready -> start`` phase modern Element
        opens with (it drops request/ready/done as ``UnknownToDeviceEvent``). So
        we run as the *responder*: you click "Verify session" in Element, we
        answer the request with a ``ready``, which makes Element send the
        ``start`` nio understands; we then drive accept/key/mac and finish with a
        ``done``. This is the path that makes Element share room keys with us.

        ``confirm(emoji)`` is awaited with ``(glyph, name)`` tuples and returns
        True if they match. ``announce(msg)`` if given is awaited with progress
        strings. Returns a human-readable result.
        """
        # matrix-nio 0.26's SAS handshake has THREE wire-incompatibilities with
        # modern Element (Rust crypto), each worked around here. All are scoped
        # to this verify flow and touch only the SAS handshake, never message
        # encryption. Without them Element cancels with m.key_mismatch (bugs
        # 1-2, before it can show emoji) or the emoji simply never match (bug 3).
        #   1. MAC method: nio advertises the legacy "hkdf-hmac-sha256" but
        #      actually computes MACs with vodozemac's corrected algorithm (what
        #      the spec calls "...v2"). Element verifies the legacy name with the
        #      legacy algorithm, so they never match. Fix: advertise v2 (here),
        #      aligning the name with what nio really computes.
        #   2. Commitment: nio sends the accept `commitment` as a hex digest; the
        #      spec and Element require unpadded base64. Fix: rewritten in
        #      on_start, before the accept goes out.
        #   3. Emoji: nio's Sas.get_emoji() re-packs vodozemac's already-final
        #      emoji_indices (old libolm raw-bytes logic), scrambling them. Fix:
        #      _sas_emoji() maps the indices directly; used in on_key.
        # (nio's _check_commitment has the same hex bug as #2 but is only reached
        # when nio *initiates*, which it cannot do here, so it never bites us.)
        from nio.crypto.sas import Sas

        Sas._mac_normal = "hkdf-hmac-sha256.v2"
        Sas._mac_v1 = ["hkdf-hmac-sha256.v2"]

        done: dict[str, str] = {}
        # The verification we've committed to. Once set, events for any other
        # transaction id (stale redeliveries from earlier attempts) are ignored.
        active: dict[str, str] = {}  # txn, other_user, other_device

        async def say(msg: str) -> None:
            if announce is not None:
                await announce(msg)

        def is_active(txn: str) -> bool:
            return active.get("txn") == txn

        def from_peer(event) -> bool:
            """The event belongs to the transaction we committed to AND comes
            from the same device we pinned. Guards every later step so a second
            sender cannot ride an in-progress transaction id."""
            return (
                is_active(getattr(event, "transaction_id", None))
                and event.sender == active.get("other_user")
            )

        async def send_raw(msg_type: str, content: dict) -> None:
            content = {**content, "transaction_id": active["txn"]}
            resp = await self.client.to_device(
                ToDeviceMessage(
                    msg_type, active["other_user"], active["other_device"], content
                )
            )
            _verify_log.debug(
                "SEND %s -> %s/%s content=%s resp=%s",
                msg_type,
                active.get("other_user"),
                active.get("other_device"),
                content,
                type(resp).__name__,
            )

        async def on_unknown(event) -> None:
            etype = event.source.get("type")
            content = event.source.get("content", {})
            txn = content.get("transaction_id")
            _verify_log.debug(
                "RECV unknown type=%s sender=%s content=%s",
                etype,
                getattr(event, "sender", None),
                content,
            )
            if etype == "m.key.verification.request":
                if not txn:
                    return
                # Only ever verify our OWN other sessions. A request from any
                # other user (or injected by a hostile server) must not hijack
                # the flow: we would otherwise trust an attacker's device while
                # you believe you are verifying your own laptop.
                if event.sender != self.cfg.user_id:
                    await say(
                        f"ignoring a verification request from {event.sender} "
                        "(not your account)"
                    )
                    return
                # One at a time: a later request cannot displace the one already
                # in progress and steal the emoji confirmation.
                if active.get("txn") and not is_active(txn):
                    await say("ignoring a second verification request; one is in progress")
                    return
                from_device = content.get("from_device")
                if not from_device:
                    return  # cannot target a reply without the peer device id
                active["txn"] = txn
                active["other_user"] = event.sender
                active["other_device"] = from_device
                await say(
                    f"got verification request from your device {from_device}; "
                    "replying ready…"
                )
                await send_raw(
                    "m.key.verification.ready",
                    {"from_device": self.client.device_id, "methods": ["m.sas.v1"]},
                )
            elif (
                etype == "m.key.verification.done"
                and is_active(txn)
                and event.sender == active.get("other_user")
            ):
                await send_raw("m.key.verification.done", {})
                done.setdefault("result", "verification complete; device verified")

        async def on_start(event) -> None:
            _verify_log.debug(
                "RECV start txn=%s sender=%s active=%s sas_methods=%s "
                "key_agreement=%s macs=%s",
                getattr(event, "transaction_id", None),
                event.sender,
                active,
                getattr(event, "short_authentication_string", None),
                getattr(event, "key_agreement_protocols", None),
                getattr(event, "message_authentication_codes", None),
            )
            if not from_peer(event):
                _verify_log.debug("start rejected by from_peer")
                return
            if "emoji" not in event.short_authentication_string:
                await self.client.cancel_key_verification(event.transaction_id, reject=True)
                done["result"] = "other device does not support emoji verification"
                return
            # Rewrite nio's hex commitment as the unpadded base64 the spec
            # requires, before the accept (which reads sas.commitment) goes out.
            sas = self.client.key_verifications.get(event.transaction_id)
            if sas is not None:
                import base64
                import hashlib

                from nio.api import Api

                raw = (
                    sas.pubkey.encode()
                    + Api.to_canonical_json(event.source["content"]).encode()
                )
                sas.commitment = (
                    base64.b64encode(hashlib.sha256(raw).digest()).decode().rstrip("=")
                )
                _verify_log.debug("rewrote commitment to base64: %s", sas.commitment)
            await say("accepting verification…")
            resp = await self.client.accept_key_verification(event.transaction_id)
            _verify_log.debug("accept resp=%s", type(resp).__name__)
            if isinstance(resp, ToDeviceError):
                done["result"] = f"accept failed: {resp.message}"
            # Deliberately do NOT share our key here. As the responder, nio put a
            # commitment to our key in the accept; the SAS protocol requires us
            # to reveal the key only AFTER the initiator sends theirs. Sending it
            # now (before their key) makes the initiator discard it as an
            # out-of-order message and hang on the emoji screen forever. The key
            # goes out in on_key instead.

        async def on_key(event) -> None:
            _verify_log.debug(
                "RECV key txn=%s sender=%s active=%s",
                getattr(event, "transaction_id", None),
                event.sender,
                active,
            )
            if not from_peer(event):
                _verify_log.debug("key rejected by from_peer")
                return
            sas = self.client.key_verifications.get(event.transaction_id)
            if sas is None:
                _verify_log.debug("no SAS object for txn")
                return
            # We just received the initiator's key (nio established the SAS in
            # receive_key_event before this callback). Now reveal ours, so the
            # initiator can compute the same SAS and show its emoji, then compare.
            key_msg = sas.share_key()
            _verify_log.debug(
                "sharing key: state=%s other=%s/%s content=%s",
                getattr(sas, "state", None),
                getattr(getattr(sas, "other_olm_device", None), "user_id", None),
                getattr(getattr(sas, "other_olm_device", None), "id", None),
                getattr(key_msg, "content", None),
            )
            share = await self.client.to_device(key_msg)
            _verify_log.debug("share_key resp=%s", type(share).__name__)
            if isinstance(share, ToDeviceError):
                done["result"] = f"key share failed: {share.message}"
                return
            # Compute emoji via _sas_emoji (bug 3 below), not sas.get_emoji().
            emoji = _sas_emoji(sas)
            _verify_log.debug("SAS emoji=%s", [name for _, name in emoji])
            matches = await confirm(emoji)
            if not matches:
                await self.client.cancel_key_verification(event.transaction_id, reject=True)
                done["result"] = "you reported the emoji did not match; cancelled"
                return
            await say("confirming…")
            resp = await self.client.confirm_short_auth_string(event.transaction_id)
            if isinstance(resp, ToDeviceError):
                done["result"] = f"confirm failed: {resp.message}"

        async def on_mac(event) -> None:
            _verify_log.debug(
                "RECV mac txn=%s sender=%s",
                getattr(event, "transaction_id", None),
                event.sender,
            )
            if not from_peer(event):
                return
            sas = self.client.key_verifications.get(event.transaction_id)
            if sas is None:
                return
            try:
                mac = sas.get_mac()
            except LocalProtocolError:
                # We haven't confirmed yet; the next sync re-delivers the event.
                return
            resp = await self.client.to_device(mac)
            if isinstance(resp, ToDeviceError):
                done["result"] = f"mac send failed: {resp.message}"
                return
            if sas.verified:
                # Send our 'done'; Element replies with its own, closing the
                # request/ready framing on its side.
                await send_raw("m.key.verification.done", {})
                dev = sas.other_olm_device.id if sas.other_olm_device else "?"
                done["result"] = f"verified the other device ({dev})"

        async def on_cancel(event) -> None:
            _verify_log.debug(
                "RECV cancel txn=%s sender=%s code=%s reason=%s",
                getattr(event, "transaction_id", None),
                event.sender,
                getattr(event, "code", None),
                getattr(event, "reason", None),
            )
            # Ignore cancels for stale transactions or from a different sender.
            if not from_peer(event):
                return
            done["result"] = f"the other device cancelled: {event.reason}"

        self.client.add_to_device_callback(on_unknown, (UnknownToDeviceEvent,))
        self.client.add_to_device_callback(on_start, (KeyVerificationStart,))
        self.client.add_to_device_callback(on_key, (KeyVerificationKey,))
        self.client.add_to_device_callback(on_mac, (KeyVerificationMac,))
        self.client.add_to_device_callback(on_cancel, (KeyVerificationCancel,))

        while "result" not in done:
            resp = await self.client.sync(timeout=10000)
            for ev in getattr(resp, "to_device_events", []) or []:
                _verify_log.debug(
                    "SYNC to_device %s from %s: %s",
                    type(ev).__name__,
                    getattr(ev, "sender", None),
                    getattr(ev, "source", None),
                )
            if isinstance(resp, SyncError):
                if getattr(resp, "status_code", None) == "M_UNKNOWN_TOKEN":
                    return "sync failed: this session was signed out by the server"
                # Error responses return immediately (no long-poll); back off
                # instead of hammering the server in a tight loop.
                await asyncio.sleep(2)
        return done["result"]

    # --- event handling ---------------------------------------------------

    async def _on_message(self, room: MatrixRoom, event: RoomMessage) -> None:
        body = getattr(event, "body", None)
        if body is None:
            body = f"<{type(event).__name__}>"
        body = _clean(body)
        timeline = self.timelines[room.room_id]
        # The event may already be cached as our own local echo (send()
        # appends it) or from a redelivered sync; do not append it twice.
        if not (
            event.event_id
            and any(m.event_id == event.event_id for m in timeline)
        ):
            thread_root, thread_count = _thread_info(event)
            timeline.append(
                Message(
                    sender=event.sender,
                    sender_name=_clean(room.user_name(event.sender) or event.sender),
                    body=body,
                    ts=event.server_timestamp,
                    event_id=event.event_id,
                    thread_root=thread_root,
                    thread_count=thread_count,
                    **_media_info(event),
                )
            )
        prev = self.state["last_event_ts"].get(room.room_id, 0)
        if event.server_timestamp > prev:
            self.state["last_event_ts"][room.room_id] = event.server_timestamp
        # Sync delivers events in stream order, so the latest event is simply
        # the last one seen. Deliberately not gated on the timestamp: a remote
        # homeserver's clock can lag ours, and a timestamp gate would then pin
        # this to an older event and live refresh would never trigger.
        if event.event_id:
            self.last_event_id[room.room_id] = event.event_id

    async def _refresh_direct_map(self) -> None:
        """Fetch the ``m.direct`` account-data event to learn which rooms are
        1:1 chats and with whom. Done over plain HTTP because nio has no typed
        call for it."""
        url = (
            f"{self.cfg.homeserver}/_matrix/client/v3/user/"
            f"{quote(self.cfg.user_id, safe='')}/account_data/m.direct"
        )
        headers = {"Authorization": f"Bearer {self.client.access_token}"}
        self.direct_by_room = {}
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, headers=headers, allow_redirects=False) as r:
                    if r.status != 200:
                        return
                    data = await r.json()
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
            # ValueError covers a malformed JSON body; a stalled connection
            # raises TimeoutError, which is not a ClientError.
            return
        if not isinstance(data, dict):
            return
        for user_id, room_ids in data.items():
            # Malformed account data happens in the wild (cf. the string `via`
            # in m.space.child); a string value here would be iterated
            # character by character, so insist on a list of strings.
            if not isinstance(room_ids, list):
                continue
            for room_id in room_ids:
                if isinstance(room_id, str):
                    self.direct_by_room[room_id] = user_id

    async def refresh_space_children(self, space_id: str | None = None) -> None:
        """Fetch each space's child links from its raw room state over HTTP,
        for the given space or all joined spaces.

        Neither nio's parsed room.children nor the /hierarchy endpoint can be
        trusted here: some servers hold m.space.child events with a malformed
        ``via`` (a plain string instead of an array), which nio drops at schema
        validation and the server excludes from /hierarchy as spec-invalid,
        leaving the space looking empty even though clients like Element still
        show the rooms. The raw state endpoint returns the events untouched;
        a child link counts as live when its content is non-empty (an emptied
        content means the link was removed)."""
        if space_id is not None:
            spaces = [space_id]
        else:
            spaces = [
                rid
                for rid, room in self.client.rooms.items()
                if room.room_type == "m.space"
            ]
        headers = {"Authorization": f"Bearer {self.client.access_token}"}
        try:
            # Bounded: the background sync loop awaits this on every child-link
            # change, and a hung request there would freeze every live update.
            timeout = aiohttp.ClientTimeout(total=15)
            async with aiohttp.ClientSession(timeout=timeout) as http:
                for sid in spaces:
                    url = (
                        f"{self.cfg.homeserver}/_matrix/client/v3/rooms/"
                        f"{quote(sid, safe='')}/state"
                    )
                    async with http.get(url, headers=headers, allow_redirects=False) as r:
                        if r.status != 200:
                            continue
                        state = await r.json()
                    children = set()
                    for ev in state if isinstance(state, list) else []:
                        if (
                            ev.get("type") == "m.space.child"
                            and ev.get("state_key")
                            and ev.get("content")
                        ):
                            children.add(ev["state_key"])
                    self.space_children[sid] = children
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
            return

    def spaces_with_child_changes(self, response) -> set[str]:
        """Ids of the rooms whose m.space.child state changed in this sync
        response, so the caller can refetch just those child maps.

        Matched on the raw ``source`` dict rather than on the parsed event
        type: the malformed child events refresh_space_children exists for come
        back as BadEvent, which carries no typed fields at all. State events
        reach us through the timeline on a live sync and through ``state`` on a
        gappy one, so both are scanned."""
        changed = set()
        rooms = getattr(getattr(response, "rooms", None), "join", {}) or {}
        for room_id, joined in rooms.items():
            events = list(getattr(getattr(joined, "timeline", None), "events", []) or [])
            events += list(getattr(joined, "state", None) or [])
            for ev in events:
                source = getattr(ev, "source", None)
                if isinstance(source, dict) and source.get("type") == "m.space.child":
                    changed.add(room_id)
                    break
        return changed

    # --- dashboard --------------------------------------------------------

    def _entry(self, room: MatrixRoom) -> Entry:
        room_id = room.room_id
        is_space = room.room_type == "m.space"
        person = None if is_space else self.direct_by_room.get(room_id)
        if person is None and not is_space:
            # Fallback for chats the other side never flagged in m.direct: any
            # two-member room is treated as a DM, with the non-self member as the
            # person. member_count can lag on first sync, so fall back to the
            # users map as well.
            others = [uid for uid in room.users if uid != self.cfg.user_id]
            member_count = room.member_count or len(room.users)
            if member_count == 2 and len(others) == 1:
                person = others[0]
        is_direct = person is not None
        if is_direct:
            title = (room.user_name(person) or person) if person else room.display_name
        else:
            title = room.display_name
        # notification_count already includes highlights (mentions); adding
        # unread_highlights on top would count every mention twice.
        unread = room.unread_notifications or 0
        online = bool(person) and self.presence.get(person) == "online"
        is_favourite = "m.favourite" in (room.tags or {})
        return Entry(
            room_id=room_id,
            title=_clean(title) or room_id,
            unread=unread,
            is_direct=is_direct,
            person=person,
            last_ts=self.state["last_event_ts"].get(room_id, 0),
            is_space=is_space,
            online=online,
            is_favourite=is_favourite,
        )

    def dashboard(self, selected_space: str | None = None) -> dict:
        """Data for the three-column home screen.

        Columns:
        * spaces  -- the list of spaces, plus the child rooms of the selected
                     one (room ids resolved from each space's m.space.child
                     state, exposed by nio as room.children).
        * invites -- pending invitations (accept with Enter); shown above
                     favourites, hidden when empty.
        * favourites -- rooms (or DMs) tagged m.favourite.
        * dms     -- one entry per person, most-recently-active first.
        """
        entries = [self._entry(r) for r in self.client.rooms.values()]
        by_id = {e.room_id: e for e in entries}

        invites = []
        for rid, room in (getattr(self.client, "invited_rooms", {}) or {}).items():
            inviter = getattr(room, "inviter", None)
            invites.append(
                Entry(
                    room_id=rid,
                    title=_clean(getattr(room, "display_name", "")) or rid,
                    unread=0,
                    is_direct=False,
                    person=inviter,
                    last_ts=0,
                    is_invite=True,
                )
            )
        invites.sort(key=lambda e: e.title.lower())

        opened = self.state["last_opened_ts"]

        def recency(e: Entry) -> int:
            return max(opened.get(e.room_id, 0), e.last_ts)

        # --- Spaces column -------------------------------------------------
        spaces = sorted(
            (e for e in entries if e.is_space),
            key=lambda e: e.title.lower(),
        )
        # Resolve the selected space's child rooms. nio records the link on
        # whichever side published the state event: the space's `children`
        # (m.space.child) and/or each room's `parents` (m.space.parent). Union
        # both so we do not miss rooms that only carry the parent pointer, and
        # add the raw-state fetch, which still has the links when the space's
        # m.space.child events are malformed and nio dropped them.
        space_rooms: list[Entry] = []
        if selected_space:
            space = self.client.rooms.get(selected_space)
            child_ids = set(getattr(space, "children", set()) or ()) if space else set()
            for rid, room in self.client.rooms.items():
                if selected_space in (getattr(room, "parents", set()) or set()):
                    child_ids.add(rid)
            child_ids |= self.space_children.get(selected_space, set())
            space_rooms = sorted(
                (
                    by_id[cid]
                    for cid in child_ids
                    if cid in by_id and not by_id[cid].is_space and not by_id[cid].is_direct
                ),
                key=lambda e: (e.unread == 0, -recency(e), e.title.lower()),
            )

        # --- Recent column: rooms you last opened, newest first -----------
        recent = sorted(
            (e for e in entries if not e.is_space and opened.get(e.room_id)),
            key=lambda e: -opened[e.room_id],
        )[:5]

        # --- Favourites column --------------------------------------------
        favourites = sorted(
            (e for e in entries if e.is_favourite and not e.is_space),
            key=lambda e: (e.unread == 0, -recency(e), e.title.lower()),
        )

        # --- DMs column: one entry per person -----------------------------
        direct_by_person: dict[str, Entry] = {}
        for e in entries:
            if not e.is_direct:
                continue
            key = e.person or e.room_id
            best = direct_by_person.get(key)
            # When merging unread counts across a person's rooms, mutate a
            # copy: the Entry objects are shared with the favourites and
            # space columns, which must keep their per-room counts.
            if best is None:
                direct_by_person[key] = e
            elif e.last_ts > best.last_ts:
                if best.unread > e.unread:
                    e = replace(e, unread=best.unread)
                direct_by_person[key] = e
            elif e.unread > best.unread:
                direct_by_person[key] = replace(best, unread=e.unread)
        # Match Element's default DM order: most recent activity first, by the
        # room's last event timestamp. We deliberately do not float unread to
        # the top (unread is shown in bold instead) and do not factor in the
        # local "when did I open this" time, since Element does neither.
        dms = sorted(
            direct_by_person.values(),
            key=lambda e: (-e.last_ts, e.title.lower()),
        )

        return {
            "spaces": spaces,
            "space_rooms": space_rooms,
            "invites": invites,
            "recent": recent,
            "favourites": favourites,
            "dms": dms,
            "all": list(by_id.values()),
        }

    async def accept_invite(self, room_id: str) -> tuple[bool, str]:
        """Join a room we were invited to. The room moves from invited_rooms
        to rooms through the next sync; the invite entry is dropped locally
        right away so the dashboard reflects the acceptance immediately."""
        resp = await self.client.join(room_id)
        if isinstance(resp, JoinError):
            return False, f"could not join: {resp.message}"
        self.client.invited_rooms.pop(room_id, None)
        return True, "invitation accepted"

    async def set_favourite(self, room_id: str, favourite: bool) -> bool:
        """Add or remove the m.favourite tag on a room via the tag API (nio has
        no typed helper, so use raw HTTP like the m.direct fetch). Returns True
        on success. The change comes back through sync and updates room.tags."""
        url = (
            f"{self.cfg.homeserver}/_matrix/client/v3/user/"
            f"{quote(self.cfg.user_id, safe='')}"
            f"/rooms/{quote(room_id, safe='')}/tags/m.favourite"
        )
        headers = {"Authorization": f"Bearer {self.client.access_token}"}
        try:
            async with aiohttp.ClientSession() as session:
                if favourite:
                    async with session.put(
                        url, headers=headers, json={"order": 0.5}, allow_redirects=False
                    ) as r:
                        ok = r.status == 200
                else:
                    async with session.delete(url, headers=headers, allow_redirects=False) as r:
                        ok = r.status == 200
        except (aiohttp.ClientError, asyncio.TimeoutError):
            return False
        # Reflect immediately in the local room object so the UI updates before
        # the change round-trips through the next sync.
        room = self.client.rooms.get(room_id)
        if ok and room is not None:
            if room.tags is None:
                room.tags = {}
            if favourite:
                room.tags["m.favourite"] = {"order": 0.5}
            else:
                room.tags.pop("m.favourite", None)
        return ok

    def find_room(self, ref: str) -> Entry | None:
        """Look up a joined room by exact room id or canonical alias, for the
        config's optional ``room =`` open-on-launch setting."""
        for room_id, room in self.client.rooms.items():
            if ref in (room_id, getattr(room, "canonical_alias", None)):
                return self._entry(room)
        return None

    def search(self, query: str) -> list[Entry]:
        # Accent-insensitive: fold both sides, so "agnes" finds "Ágnes" and
        # typing the accents still works.
        def fold(text: str) -> str:
            return "".join(
                c
                for c in unicodedata.normalize("NFKD", text.lower())
                if not unicodedata.combining(c)
            )

        q = fold(query.strip())
        if not q:
            return []
        out = []
        for room in self.client.rooms.values():
            e = self._entry(room)
            haystack = fold(f"{e.title} {e.person or ''} {e.room_id}")
            if q in haystack:
                out.append(e)
        out.sort(key=lambda e: (e.unread == 0, -e.last_ts, e.title.lower()))
        return out

    # --- room view --------------------------------------------------------

    def _to_message(self, room, event) -> Message | None:
        """A fetched nio event as a Message, or None for non-message events.
        Encrypted events are decrypted here (server fetches do not decrypt);
        ones we lack keys for become a placeholder rather than being
        dropped."""
        decrypt_error: Exception | None = None
        # Thread info comes from the wire-format event: the encrypted wrapper
        # still has the server's unsigned m.relations aggregation, which nio
        # drops when it rebuilds the event from the decrypted payload.
        thread_root, thread_count = _thread_info(event)
        if isinstance(event, MegolmEvent):
            try:
                event = self.client.decrypt_event(event)
            except Exception as exc:
                # Keep the reason: a decrypt failure can be a benign missing
                # key OR an integrity failure (bad MAC, sender-key mismatch)
                # that signals tampering; the two must not look identical.
                decrypt_error = exc
            else:
                root, count = _thread_info(event)
                thread_root = thread_root or root
                thread_count = max(thread_count, count)
        if isinstance(event, RoomMessage):
            name = room.user_name(event.sender) if room else event.sender
            body = getattr(event, "body", None)
            if not body:
                src = getattr(event, "source", {}) or {}
                body = src.get("content", {}).get("body")
            if not body:
                body = f"[{type(event).__name__}]"
        elif isinstance(event, MegolmEvent):
            name = room.user_name(event.sender) if room else event.sender
            if decrypt_error is not None:
                body = f"[encrypted: could not decrypt ({_clean(str(decrypt_error))})]"
            else:
                body = "[encrypted: no key for this message]"
        else:
            return None
        return Message(
            sender=event.sender,
            sender_name=_clean(name or event.sender),
            body=_clean(body),
            ts=event.server_timestamp,
            event_id=event.event_id,
            thread_root=thread_root,
            thread_count=thread_count,
            **_media_info(event),
        )

    async def load_history(self, room_id: str, limit: int = HISTORY_LIMIT) -> list[Message]:
        cached = list(self.timelines.get(room_id, []))
        if len(cached) >= limit:
            return cached[-limit:]

        room = self.client.rooms.get(room_id)
        resp = await self.client.room_messages(
            room_id,
            start=self.client.next_batch or "",
            direction=MessageDirection.back,
            limit=limit,
        )
        if isinstance(resp, RoomMessagesError):
            return cached[-limit:]
        # Seed the back-pagination position from this first window, but never
        # overwrite a token load_older has already advanced deeper.
        end = getattr(resp, "end", None)
        if end:
            self.pagination_tokens.setdefault(room_id, end)
        else:
            self.pagination_done[room_id] = True

        fetched: list[Message] = []
        for event in resp.chunk:
            msg = self._to_message(room, event)
            if msg is not None:
                fetched.append(msg)
        fetched.reverse()  # chunk comes newest-first when paginating back
        # Merge on event id (timestamps collide for messages sent in the same
        # millisecond); cached entries win because they may already be decrypted.
        # Re-read the cache: a sync callback may have appended during the fetch.
        cached = list(self.timelines.get(room_id, []))
        merged: dict[str, Message] = {}
        for m in fetched:
            merged[m.event_id or f"ts:{m.ts}:{m.sender}"] = m
        for m in cached:
            merged[m.event_id or f"ts:{m.ts}:{m.sender}"] = m
        history = sorted(merged.values(), key=lambda m: m.ts)
        # Write the merged history back into the in-memory timeline so the next
        # open (and the live-refresh path) does not refetch and re-decrypt the
        # same events from the server. The deque caps it at TIMELINE_CAP.
        timeline = self.timelines[room_id]
        timeline.clear()
        timeline.extend(history)
        return history[-limit:]

    async def load_older(self, room_id: str, limit: int = HISTORY_LIMIT) -> list[Message]:
        """The next batch of history older than what has been fetched so far,
        oldest first. Returns [] once the room's very first event has been
        reached (or on error). The position is tracked per room; the first
        call continues from where load_history's initial window ended, so
        repeated calls walk arbitrarily far back."""
        if self.pagination_done.get(room_id):
            return []
        room = self.client.rooms.get(room_id)
        start = self.pagination_tokens.get(room_id) or self.client.next_batch or ""
        resp = await self.client.room_messages(
            room_id,
            start=start,
            direction=MessageDirection.back,
            limit=limit,
        )
        if isinstance(resp, RoomMessagesError):
            return []
        end = getattr(resp, "end", None)
        if end:
            self.pagination_tokens[room_id] = end
        if not end or not resp.chunk:
            self.pagination_done[room_id] = True
        out: list[Message] = []
        for event in resp.chunk:
            msg = self._to_message(room, event)
            if msg is not None:
                out.append(msg)
        out.reverse()  # chunk comes newest-first when paginating back
        return out

    async def load_thread(self, room_id: str, root: Message, limit: int = 200) -> list[Message]:
        """A thread's replies, oldest first, with the root prepended. Fetched
        from the /relations endpoint over raw HTTP (nio has no typed call for
        it), since the room history window may not span a long thread. Falls
        back to whatever replies the local timeline cache holds if the request
        fails; cached entries also win on merge, as they may be decrypted."""
        room = self.client.rooms.get(room_id)
        url = (
            f"{self.cfg.homeserver}/_matrix/client/v1/rooms/"
            f"{quote(room_id, safe='')}"
            f"/relations/{quote(root.event_id, safe='')}/m.thread"
            f"?dir=b&limit={int(limit)}"
        )
        headers = {"Authorization": f"Bearer {self.client.access_token}"}
        chunk: list = []
        try:
            async with aiohttp.ClientSession() as http:
                async with http.get(url, headers=headers, allow_redirects=False) as r:
                    if r.status == 200:
                        data = await r.json()
                        chunk = data.get("chunk", []) or []
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
            pass

        fetched: list[Message] = []
        for source in chunk:
            event = Event.parse_event(source)
            msg = self._to_message(room, event)
            if msg is not None:
                fetched.append(msg)

        merged: dict[str, Message] = {m.event_id: m for m in fetched}
        for m in self.timelines.get(room_id, []):
            if m.thread_root == root.event_id and m.event_id:
                merged[m.event_id] = m
        replies = sorted(merged.values(), key=lambda m: m.ts)
        return [root] + replies

    async def send(
        self,
        room_id: str,
        text: str,
        reply_to: str | None = None,
        thread_root: str | None = None,
        thread_latest: str | None = None,
    ) -> tuple[bool, str]:
        content = {"msgtype": "m.text", "body": text}
        if thread_root:
            # A thread reply per the spec: is_falling_back marks the
            # in_reply_to as a mere fallback for clients without thread
            # support (pointing at the latest thread message); an explicit
            # reply inside the thread clears it.
            content["m.relates_to"] = {
                "rel_type": "m.thread",
                "event_id": thread_root,
                "is_falling_back": reply_to is None,
                "m.in_reply_to": {
                    "event_id": reply_to or thread_latest or thread_root
                },
            }
        elif reply_to:
            content["m.relates_to"] = {"m.in_reply_to": {"event_id": reply_to}}
        try:
            resp = await self.client.room_send(
                room_id,
                message_type="m.room.message",
                content=content,
                ignore_unverified_devices=self.cfg.allow_unverified,
            )
        except Exception as exc:  # nio raises various crypto/network errors
            return False, str(exc)
        if hasattr(resp, "event_id") and resp.event_id:
            # Cache the sent message right away (unless the sync echo beat us
            # to it): a concurrent sync-driven reload rebuilds the room's
            # history, and without this the message would vanish from the
            # timeline until the echo arrives.
            timeline = self.timelines[room_id]
            if not any(m.event_id == resp.event_id for m in timeline):
                timeline.append(
                    Message(
                        sender=self.cfg.user_id,
                        sender_name=self.my_name,
                        body=text,
                        ts=int(time.time() * 1000),
                        event_id=resp.event_id,
                        thread_root=thread_root or "",
                    )
                )
            self.last_event_id[room_id] = resp.event_id
            return True, resp.event_id
        return False, getattr(resp, "message", "send failed")

    async def download_media(self, message: Message, directory) -> tuple[bool, str]:
        """Download an uploaded file into ``directory`` (a Path), decrypting
        it when it came from an encrypted room. Returns (ok, saved-path) or
        (False, error). The filename comes from the upload, sanitized to its
        basename and deduplicated so nothing is overwritten."""
        if not message.media_url:
            return False, "not a file message"
        # Reject before downloading when the server-advertised size is already
        # over the cap (it is attacker-controlled, so it is only an early-out).
        if message.media_size and message.media_size > MAX_DOWNLOAD_BYTES:
            return False, f"file too large ({message.media_size} bytes)"
        resp = await self.client.download(mxc=message.media_url)
        body = getattr(resp, "body", None)
        if not isinstance(body, bytes):
            detail = getattr(resp, "message", "") or getattr(
                resp, "status_code", ""
            ) or "download failed"
            return False, str(detail)
        if len(body) > MAX_DOWNLOAD_BYTES:
            return False, f"file too large ({len(body)} bytes)"
        if message.media_crypt:
            from nio.crypto import decrypt_attachment

            c = message.media_crypt
            try:
                body = decrypt_attachment(body, c["key"], c["sha256"], c["iv"])
            except Exception as exc:
                return False, f"could not decrypt attachment: {exc}"
        # Basename only (strips any directory components), control characters
        # removed, and the degenerate names that would resolve to the directory
        # itself replaced.
        name = _clean(Path(message.media_name or message.body or "download").name)
        if name in ("", ".", ".."):
            name = "download"
        directory = Path(directory).expanduser()
        directory.mkdir(parents=True, exist_ok=True)
        target = directory / name
        stem, suffix = target.stem, target.suffix
        counter = 1
        # lexists (not exists) so a dangling or pre-planted symlink at the target
        # name is treated as taken instead of being followed.
        while os.path.lexists(target):
            target = directory / f"{stem} ({counter}){suffix}"
            counter += 1
        # O_EXCL closes the check-then-write race; O_NOFOLLOW refuses to write
        # through a symlink; 0o600 keeps decrypted plaintext owner-only.
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
        while True:
            try:
                fd = os.open(target, flags, 0o600)
                break
            except FileExistsError:
                target = directory / f"{stem} ({counter}){suffix}"
                counter += 1
        with os.fdopen(fd, "wb") as fh:
            fh.write(body)
        return True, str(target)

    async def mark_read(self, room_id: str) -> None:
        event_id = self.last_event_id.get(room_id)
        if event_id:
            try:
                await self.client.room_read_markers(
                    room_id, fully_read_event=event_id, read_event=event_id
                )
            except Exception:
                pass
        self.record_opened(room_id)

    def record_opened(self, room_id: str) -> None:
        self.state["last_opened_ts"][room_id] = int(time.time() * 1000)
        self._persist_recency()

    def _persist_recency(self) -> None:
        self.cfg.save_state(self.state)
