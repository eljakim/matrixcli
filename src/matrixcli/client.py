"""Thin async wrapper around matrix-nio that powers the dashboard.

It owns the ``AsyncClient`` lifecycle (restore-from-token or password login),
keeps the encryption store warm, observes timeline events to track recency, and
derives the home screen's columns (see ``dashboard()``): spaces with the
selected space's rooms, invites, recently opened rooms, favourites, and DMs.

Message recency comes from the timestamps Matrix gives us; "recently opened" is
tracked locally (Matrix has no such concept) and stamped whenever you open a
room in the UI.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import logging
import os
import re
import time
import unicodedata
from collections import defaultdict, deque
from dataclasses import dataclass, fields, replace
from pathlib import Path
from urllib.parse import quote, urlsplit
from uuid import uuid4

_verify_log = logging.getLogger("matrixcli.verify")
# NullHandler so a swallowed-callback report never reaches logging's
# lastResort stderr handler and paints over the running TUI. The verify log
# attaches its own FileHandler to the child logger, so it is unaffected.
_log = logging.getLogger("matrixcli")
_log.addHandler(logging.NullHandler())

import hmac

import aiohttp
import vodozemac
from Crypto.Cipher import AES
from Crypto.PublicKey import ECC
from Crypto.Signature import eddsa

from nio import (
    AsyncClient,
    AsyncClientConfig,
    KeyVerificationAccept,
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
    ProfileSetDisplayNameResponse,
    ReactionEvent,
    RedactedEvent,
    RedactionEvent,
    RoomEncryptedMedia,
    RoomMessage,
    RoomMessageMedia,
    RoomMessagesError,
    SyncError,
    TagEvent,
    ToDeviceError,
    ToDeviceMessage,
    UnknownToDeviceEvent,
)

from nio.api import Api
from nio.crypto import decrypt_attachment
from nio.crypto.sas import Sas
from nio.events.room_events import Event

from .config import Config

HISTORY_LIMIT = 40
TIMELINE_CAP = 200
# The dashboard's Recent and Favourites sections never show fewer rows than
# this, whatever +/- or a shrinking terminal ask for.
MIN_SECTION_ROWS = 5
# Minimum seconds between read-marker POSTs per room (see mark_read): a busy
# open room refreshes every sync tick, and marking each tick spams the server.
MARK_READ_INTERVAL = 2.0

# A room's archive file is rewritten whole, so a 100k-message room costs tens
# of MB of JSON + AES-GCM per save. Once its backfill is done the only thing
# that dirties it is new head messages, which the main cache's live window
# also holds and re-folds in at the next save, so those rewrites can wait.
ARCHIVE_SAVE_INTERVAL = 600.0
# Prefix of the placeholders _to_message renders for undecryptable messages;
# edit folding checks it so an unreadable edit never displaces readable text.
UNDECRYPTABLE = "[encrypted"
# Refuse to buffer/decrypt/write an attachment bigger than this. nio's download
# reads the whole body into memory, so an unbounded one is a trivial OOM; the
# sender also controls the advertised size, so both are checked.
MAX_DOWNLOAD_BYTES = 512 * 1024 * 1024

# Control characters, C1 codes, DEL, and bidi overrides. Server-supplied
# strings are rendered straight to the terminal, and an unstripped ESC lets a
# remote sender emit OSC/CSI sequences (clipboard writes, forged lines, title
# changes). Tab and newline are kept.
_UNSAFE_RE = re.compile(
    "[\x00-\x08\x0b-\x1f\x7f-\x9f\u202a-\u202e\u2066-\u2069]"
)


class NeedsPassword(Exception):
    """No usable saved session, and no password to log in with (or the one we
    had was rejected). The app catches this and asks for credentials; the
    plain-CLI entry points turn it into a message."""


async def discover_homeserver(user_id: str) -> str:
    """The homeserver base URL for a user id, from the domain's
    .well-known/matrix/client if it publishes one (that is how @you:example.org
    lives on matrix.example.org), else https://<domain>. Empty for a user id
    with no domain."""
    domain = user_id.split(":", 1)[1].strip() if ":" in user_id else ""
    if not domain:
        return ""
    fallback = f"https://{domain}"
    try:
        timeout = aiohttp.ClientTimeout(total=8)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(
                f"https://{domain}/.well-known/matrix/client"
            ) as resp:
                if resp.status != 200:
                    return fallback
                # content_type=None: servers serve this as text/plain often
                # enough that insisting on application/json breaks discovery.
                data = await resp.json(content_type=None)
    except (aiohttp.ClientError, asyncio.TimeoutError, OSError, ValueError):
        return fallback
    base = ""
    if isinstance(data, dict):
        entry = data.get("m.homeserver")
        if isinstance(entry, dict) and isinstance(entry.get("base_url"), str):
            base = entry["base_url"].strip().rstrip("/")
    # https only: a well-known that downgrades to http would put the access
    # token on the wire in the clear.
    return base if base.startswith("https://") else fallback


def _clean(text) -> str:
    """Strip terminal-control and bidi-override characters from an untrusted
    server string before it can reach the renderer. Anything that is not a
    string reads as "": nio schema-checks only some content fields, so a
    hostile event can carry an int or a dict where the spec says string, and
    re.sub would raise TypeError out of a sync callback."""
    if not isinstance(text, str) or not text:
        return ""
    return _UNSAFE_RE.sub("", text)


def _reaction_key(key: str) -> str:
    """Canonical form of a reaction emoji, applied on both the receive and
    the send-side bookkeeping so they always agree. The emoji variation
    selector is stripped because clients disagree on sending "👍" vs
    "👍️", and a vote count split into two buckets over an invisible
    codepoint miscounts the vote; the cap keeps a malicious key short."""
    return _clean(key).replace("️", "")[:16]


def _sas_emoji(sas) -> list[tuple[str, str]]:
    """The seven SAS emoji for a vodozemac-backed nio ``Sas``, as (glyph, name).

    Maps vodozemac's already-final ``emoji_indices`` straight onto nio's emoji
    table. nio's own ``Sas.get_emoji()`` must NOT be used here: it re-packs those
    indices into 8-bit binary and regroups them into 6-bit chunks (correct for
    the old libolm raw-bytes API, wrong for vodozemac, which hands back the
    final indices), scrambling the result so it never matches the peer's emoji.
    See the nio-compatibility note in ``verify_interactive``.
    """
    indices = sas.established_sas.bytes(sas._extra_info).emoji_indices
    return [Sas.emoji[i] for i in indices]


def _single_key(obj) -> str | None:
    """The one ed25519 key a /keys/query cross-signing key object carries,
    or None for anything else."""
    if not isinstance(obj, dict):
        return None
    keys = obj.get("keys")
    if not isinstance(keys, dict) or len(keys) != 1:
        return None
    ((key_id, key),) = keys.items()
    return key if isinstance(key, str) and key_id == f"ed25519:{key}" else None


def _b64(data: bytes) -> str:
    """Unpadded standard base64, the Matrix convention for keys."""
    return base64.b64encode(data).decode().rstrip("=")


def _unb64(text) -> bytes:
    """base64 as clients write it (padding optional); ValueError on junk."""
    if not isinstance(text, str):
        raise ValueError("not base64 text")
    return base64.b64decode(text + "=" * (-len(text) % 4))


_BASE58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def _decode_security_key(text: str) -> bytes | None:
    """The 32 secret-storage key bytes behind an Element Security Key
    ("EsTx abcd ..."): base58 of 0x8B 0x01, the key, and a parity byte.
    None when the text is not one (a Security Phrase, or a typo)."""
    raw = "".join(text.split())
    if not raw or any(c not in _BASE58 for c in raw):
        return None
    n = 0
    for c in raw:
        n = n * 58 + _BASE58.index(c)
    leading = len(raw) - len(raw.lstrip("1"))
    data = bytes(leading) + n.to_bytes((n.bit_length() + 7) // 8, "big")
    parity = 0
    for b in data:
        parity ^= b
    if len(data) != 35 or data[:2] != b"\x8b\x01" or parity:
        return None
    return data[2:34]


def _ssss_keys(key: bytes, name: str) -> tuple[bytes, bytes]:
    """The AES and HMAC keys m.secret_storage.v1.aes-hmac-sha2 derives for
    one secret: HKDF-SHA256 over the storage key, zero salt, the secret's
    name as info, 64 bytes split in two."""
    prk = hmac.new(bytes(32), key, hashlib.sha256).digest()
    info = name.encode()
    aes_key = hmac.new(prk, info + b"\x01", hashlib.sha256).digest()
    mac_key = hmac.new(prk, aes_key + info + b"\x02", hashlib.sha256).digest()
    return aes_key, mac_key


def _ssss_ctr(aes_key: bytes, iv: bytes):
    """AES-256-CTR as WebCrypto runs it for secret storage: the low 64 bits
    of the 16-byte iv are the counter."""
    return AES.new(aes_key, AES.MODE_CTR, nonce=iv[:8], initial_value=iv[8:])


def _ssss_decrypt(key: bytes, name: str, blob) -> bytes | None:
    """One secret-storage payload ({iv, ciphertext, mac}) opened with the
    storage key: HMAC-SHA256 over the ciphertext checked first, then
    decrypted. None on a bad MAC or a malformed blob, which is where a
    wrong Security Key shows up."""
    if not isinstance(blob, dict):
        return None
    try:
        iv, ciphertext, mac = (_unb64(blob[k]) for k in ("iv", "ciphertext", "mac"))
    except (KeyError, ValueError):
        return None
    if len(iv) != 16:
        return None
    aes_key, mac_key = _ssss_keys(key, name)
    if not hmac.compare_digest(
        hmac.new(mac_key, ciphertext, hashlib.sha256).digest(), mac
    ):
        return None
    return _ssss_ctr(aes_key, iv).decrypt(ciphertext)


def _ssss_key_matches(key: bytes, description: dict) -> bool:
    """A key description carries 32 zero bytes encrypted under the empty
    name as a check value; recompute and compare. One without it (older
    clients) cannot be checked here and passes: a wrong key still fails
    the secret's own MAC."""
    try:
        iv, mac = _unb64(description["iv"]), _unb64(description["mac"])
    except (KeyError, ValueError):
        return True
    if len(iv) != 16:
        return False
    aes_key, mac_key = _ssss_keys(key, "")
    ciphertext = _ssss_ctr(aes_key, iv).encrypt(bytes(32))
    return hmac.compare_digest(
        hmac.new(mac_key, ciphertext, hashlib.sha256).digest(), mac
    )



def _ed25519_from_seed(seed: bytes):
    """A pycryptodome Ed25519 key from a 32-byte private seed. Its signatures
    verify under vodozemac (checked in the tests), which is what nio and the
    homeserver validate cross-signatures with."""
    return ECC.construct(curve="Ed25519", seed=seed)


def _ed25519_public(seed: bytes) -> str:
    return _b64(_ed25519_from_seed(seed).public_key().export_key(format="raw"))


def _sign_object(seed: bytes, obj: dict) -> str:
    """The account's canonical-json signature of ``obj`` (its own signatures
    and unsigned block excluded, per the spec) under the ed25519 ``seed``."""
    body = {k: v for k, v in obj.items() if k not in ("signatures", "unsigned")}
    signer = eddsa.new(_ed25519_from_seed(seed), "rfc8032")
    return _b64(signer.sign(Api.to_canonical_json(body).encode()))


def _signed_by(obj, user_id: str, key_id: str, key: str) -> bool:
    """True when ``obj`` (a /keys/query device or cross-signing key object)
    carries a valid ed25519 signature by ``user_id``'s ``ed25519:<key_id>``,
    checked against the public ``key``. Cross-signing keys sign under their
    own public key as the id; devices under their device id."""
    if not isinstance(obj, dict):
        return False
    sig = ((obj.get("signatures") or {}).get(user_id) or {}).get(f"ed25519:{key_id}")
    if not isinstance(sig, str):
        return False
    body = {k: v for k, v in obj.items() if k not in ("signatures", "unsigned")}
    try:
        vodozemac.Ed25519PublicKey.from_base64(key).verify_signature(
            Api.to_canonical_json(body).encode(),
            vodozemac.Ed25519Signature.from_base64(sig),
        )
    except (ValueError, TypeError):
        return False
    return True


def _base64_commitment_check(sas):
    """A drop-in for a Sas instance's ``_check_commitment`` that recomputes
    the responder's commitment as unpadded base64 instead of nio's hex, so an
    initiating matrixcli accepts the base64 commitment a responding matrixcli
    (or Element) sends. Same formula as the responder writes in ``on_start``."""

    def check(key: str) -> bool:
        raw = key.encode() + Api.to_canonical_json(sas.start_verification().content).encode()
        return sas.commitment == base64.b64encode(hashlib.sha256(raw).digest()).decode().rstrip("=")

    return check


def _mac_with_master(sas, master_pub: str):
    """A ``get_mac`` replacement for a Sas that also MACs the account master
    key under ``ed25519:<master_pub>``, the way Element does. nio's own
    ``get_mac`` covers only the device key, so a peer verifying against
    matrixcli could never learn (and pin) the identity; adding the master key
    here lets the peer confirm the identity from the emoji compare alone,
    without its own Security Key. The receiver's nio skips the non-device
    entry when checking, and our ``_master_key_from_mac`` reads it."""
    original = sas.get_mac

    def get_mac():
        message = original()
        info = (
            "MATRIX_KEY_VERIFICATION_MAC"
            f"{sas.own_user}{sas.own_device}"
            f"{sas.other_olm_device.user_id}{sas.other_olm_device.id}"
            f"{sas.transaction_id}"
        )
        key_id = f"ed25519:{master_pub}"
        mac = dict(message.content.get("mac") or {})
        mac[key_id] = sas.established_sas.calculate_mac(master_pub, info + key_id)
        message.content["mac"] = mac
        # The `keys` MAC covers the full sorted list of key ids; recompute it
        # now that the master key is in the set, or the peer rejects the mac.
        message.content["keys"] = sas.established_sas.calculate_mac(
            ",".join(sorted(mac)), info + "KEY_IDS"
        )
        return message

    return get_mac


def _master_key_from_mac(sas, event) -> str | None:
    """The account's master cross-signing key as vouched for by the peer's
    ``m.key.verification.mac``, or None. Alongside its device key, a
    cross-signed peer MACs its master key under the id ``ed25519:<key>``.
    nio checks the list of ids as a whole but skips every non-device entry,
    so this re-checks that one entry with the shared secret. Only meaningful
    once ``sas.verified``: the peer is then a device whose key we compared
    in person, so what it says the identity is, is the identity."""
    if event is None or not sas.verified or sas.other_olm_device is None:
        return None
    other = sas.other_olm_device
    info = (
        f"MATRIX_KEY_VERIFICATION_MAC{other.user_id}{other.id}"
        f"{sas.own_user}{sas.own_device}{sas.transaction_id}"
    )
    for key_id, mac in (event.mac or {}).items():
        algo, _, key = key_id.partition(":")
        if algo != "ed25519" or not key or key == other.id:
            continue
        if mac == sas.established_sas.calculate_mac(key, info + key_id):
            return key
    return None


@dataclass
class OwnDevice:
    """One session on our own account, as ``:verify`` lists it."""

    device_id: str
    display_name: str
    last_seen_ts: int  # ms, 0 when the server did not say
    last_seen_ip: str
    fingerprint: str  # ed25519 key; "" for a device that published none
    cross_signed: bool  # validly signed by the account's self-signing key
    verified_here: bool  # this client SAS-verified it (nio's trust state)
    is_this: bool


@dataclass
class SessionReport:
    devices: list[OwnDevice]
    master_key: str | None  # as the server publishes it; None: no cross-signing
    pinned_key: str | None  # the identity this client confirmed in person
    error: str = ""

    @property
    def identity_confirmed(self) -> bool:
        return bool(self.master_key) and self.master_key == self.pinned_key

    @property
    def identity_changed(self) -> bool:
        """The server now publishes a different identity than the one we
        pinned: someone reset cross-signing, or the server is lying."""
        return bool(self.master_key and self.pinned_key) and not self.identity_confirmed

    @property
    def this_device(self) -> OwnDevice | None:
        return next((d for d in self.devices if d.is_this), None)

    @property
    def this_verified(self) -> bool:
        """What other clients mean by a verified session: cross-signed by an
        identity this client has confirmed."""
        me = self.this_device
        return self.identity_confirmed and me is not None and me.cross_signed


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
    highlights: int = 0  # unread mentions/keyword hits, from the server


@dataclass
class Message:
    sender: str
    sender_name: str
    body: str
    ts: int
    event_id: str = ""
    thread_root: str = ""  # event id of the thread this message replies in
    thread_count: int = 0  # server-aggregated reply count (thread roots only)
    reply_to: str = ""  # event id this message replies to ("": not a reply)
    reply_name: str = ""  # quoted sender's display name, from the text fallback
    reply_snippet: str = ""  # first quoted line, from the text fallback
    media_url: str = ""  # mxc:// URL when this message is an uploaded file
    media_name: str = ""  # upload filename (body may be a caption)
    media_size: int = 0  # bytes, from content.info, 0 if unknown
    media_mime: str = ""  # mimetype from content.info ("" if unadvertised)
    media_crypt: dict | None = None  # key/iv/hash for encrypted attachments
    pending: bool = False  # local echo awaiting the server's event id
    replaces: str = ""  # an m.replace edit: the event id whose text it rewrites
    edited_ts: int = 0  # newest edit folded into this message (0: never edited)
    original_body: str = ""  # the text before the first edit, for the history popup
    mentions_me: bool = False  # names us: red-flagged in the timeline
    # When the message was deleted (0: not deleted). ``body`` then holds
    # whatever text we received before the deletion, which the server no
    # longer has; the timeline renders a tombstone and the history popup
    # (Shift+Enter) reveals it.
    redacted_ts: int = 0


def _media_info(event) -> dict:
    """Message kwargs for an uploaded file (image/file/video/audio), empty for
    ordinary text events. Encrypted rooms wrap the attachment key material in
    the event content; it is captured here so download can decrypt."""
    if not isinstance(event, (RoomMessageMedia, RoomEncryptedMedia)):
        return {}
    source = getattr(event, "source", {}) or {}
    content = source.get("content") if isinstance(source, dict) else None
    content = content if isinstance(content, dict) else {}
    info = content.get("info")
    info = info if isinstance(info, dict) else {}
    # nio schema-checks url and body on media events but nothing under info,
    # so size/mimetype/filename are whatever the sender put there. A string
    # size reaches _fmt_size during the timeline render, where it raises.
    size = info.get("size", 0)
    if not isinstance(size, int) or isinstance(size, bool):
        size = 0
    name = content.get("filename")
    if not isinstance(name, str) or not name:
        name = getattr(event, "body", "") or ""
    out = {
        "media_url": event.url or "",
        "media_name": _clean(name),
        "media_size": size,
        "media_mime": _clean(info.get("mimetype")),
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
    # nio's megolm-decrypted schema constrains only type and content, so
    # unsigned is whatever the sender put there; .get on a str would raise
    # straight out of the sync callback and abort the whole batch.
    unsigned = src.get("unsigned")
    relations = unsigned.get("m.relations") if isinstance(unsigned, dict) else None
    thread = relations.get("m.thread") if isinstance(relations, dict) else None
    count = thread.get("count", 0) or 0 if isinstance(thread, dict) else 0
    return root, count


def _event_room_id(room, event) -> str:
    """The room an event belongs to: the MatrixRoom when the caller has one,
    else the event's own room_id field (present on /messages fetches)."""
    if room is not None:
        return room.room_id
    return (getattr(event, "source", {}) or {}).get("room_id") or ""


def fold_text(text: str) -> str:
    """Accent-insensitive fold for matching: "agnes" finds "Ágnes" and typing
    the accents still works. Shared by the people/rooms search and the
    in-room message search."""
    return "".join(
        c
        for c in unicodedata.normalize("NFKD", text.lower())
        if not unicodedata.combining(c)
    )


def _edit_target(event) -> str:
    """The event id this event rewrites (an m.replace edit), or "". Read from
    the wire-format event for the same reason as _thread_info: an encrypted
    edit carries m.relates_to in its cleartext wrapper, with the new text
    inside the ciphertext."""
    src = getattr(event, "source", {}) or {}
    relates = (src.get("content", {}) or {}).get("m.relates_to") or {}
    if isinstance(relates, dict) and relates.get("rel_type") == "m.replace":
        return relates.get("event_id") or ""
    return ""


def _edit_body(event, fallback: str) -> str:
    """The replacement text of an m.replace edit. Senders put it in
    m.new_content and duplicate it in body prefixed with "* " as a fallback for
    clients that do not understand edits; strip that prefix when it is all we
    have."""
    src = getattr(event, "source", {}) or {}
    new_content = (src.get("content", {}) or {}).get("m.new_content") or {}
    body = new_content.get("body") if isinstance(new_content, dict) else None
    if isinstance(body, str) and body:
        return body
    return fallback[2:] if fallback.startswith("* ") else fallback


# Anchored, and every quantifier bounded. An unanchored search with an
# unbounded [^:>]+ is retried at every offset, so a body of "<@a:" x 16000
# costs seconds of blocking CPU on the sync loop before failing to match.
_REPLY_FALLBACK_RE = re.compile(r">\s*<(@[^:>\s]{1,255}:[^>\s]{1,255})>\s?(.*)")


def _reply_target(event) -> str:
    """The event id this event replies to, or "". A thread relation marked
    is_falling_back carries m.in_reply_to only as a compatibility pointer at
    the latest thread message, not as a reply, and reads as ""."""
    src = getattr(event, "source", {}) or {}
    relates = (src.get("content", {}) or {}).get("m.relates_to") or {}
    if not isinstance(relates, dict):
        return ""
    in_reply = relates.get("m.in_reply_to")
    if not isinstance(in_reply, dict):
        return ""
    if relates.get("rel_type") == "m.thread" and relates.get("is_falling_back"):
        return ""
    return in_reply.get("event_id") or ""


def _strip_reply_fallback(body: str) -> tuple[str, str, str]:
    """(body without the rich-reply text fallback, quoted sender's user id,
    first quoted line). Pre-v1.3 senders prefix a reply's body with the quoted
    message as "> <@mxid> text" lines; only called on events that carry
    m.in_reply_to, so a leading quote block is that fallback and not the
    sender's own text. Bodies without the fallback pass through untouched."""
    if not body.startswith("> "):
        return body, "", ""
    lines = body.split("\n")
    i = 0
    while i < len(lines) and lines[i].startswith(">"):
        i += 1
    rest = lines[i:]
    while rest and not rest[0].strip():
        rest.pop(0)
    if not rest:
        # Nothing but the quote block: the fallback assumption failed, keep
        # the text rather than render an empty message.
        return body, "", ""
    match = _REPLY_FALLBACK_RE.match(lines[0])
    sender = match.group(1) if match else ""
    snippet = (match.group(2) if match else lines[0].lstrip("> ")).strip()
    if not snippet and i > 1:
        snippet = lines[1].lstrip("> ").strip()
    return "\n".join(rest), sender, snippet


def fold_edits(messages: list[Message]) -> list[Message]:
    """Collapse m.replace edits into the message they rewrite: the newest text
    takes the original's place in the timeline, the text it replaced is kept
    for the history popup, and the edit event itself drops out. An edit whose
    target is outside the loaded window stays as its own message, so nothing a
    sender wrote is silently hidden."""
    targets = {m.event_id: m for m in messages if m.event_id and not m.replaces}

    def rewrites(m: Message) -> bool:
        # Only the original sender's own edits count, or a stranger could
        # rewrite what someone else said in front of us.
        target = targets.get(m.replaces)
        return target is not None and target.sender == m.sender

    newest: dict[str, Message] = {}
    for m in messages:
        if rewrites(m) and not m.redacted_ts:
            # A redacted edit is a retraction: the server un-applies it, and a
            # fresh client shows the previous text. Folding it in would keep
            # displaying deleted content for the rest of the session.
            current = newest.get(m.replaces)
            if current is None or m.ts > current.ts:
                newest[m.replaces] = m
    # (target id, edit ts) of every retracted edit, to recognize a fold the
    # server bundled into the target (_to_message bakes body/edited_ts in)
    # whose edit has since been deleted: the bake must be undone.
    retracted = {(m.replaces, m.ts) for m in messages if rewrites(m) and m.redacted_ts}
    out: list[Message] = []
    for m in messages:
        if rewrites(m):
            # Edits never render as their own row; a redacted one does not
            # render as a tombstone either, it simply stops applying.
            continue
        if (m.event_id, m.edited_ts) in retracted and m.original_body:
            m = replace(m, body=m.original_body, original_body="", edited_ts=0)
        edit = newest.get(m.event_id)
        # The server bundles the latest edit onto the original (see
        # _to_message), so a message can arrive already folded; only a newer
        # edit event than that one has anything to add.
        if edit is not None and edit.ts >= m.edited_ts:
            # An edit we hold no key for marks the message as edited but must
            # not displace text we can read.
            if edit.body.startswith(UNDECRYPTABLE):
                m = replace(m, edited_ts=edit.ts)
            else:
                # The rewritten text may add or drop the mention.
                m = replace(
                    m,
                    body=edit.body,
                    original_body=m.original_body or m.body,
                    edited_ts=edit.ts,
                    mentions_me=edit.mentions_me,
                )
        out.append(m)
    return out


def unread_summary(state: dict) -> dict:
    """What ``matrix --check`` reports, read from the dashboard snapshot in
    state.json: one row per room with unread notifications, plus the pending
    invites.

    The snapshot is the only source that survives a restart. A resumed sync
    returns just the rooms that changed since the token, so counting the live
    rooms alone would miss every quiet room that was already unread (see
    _all_entries, which refreshes the snapshot from the live rooms first).
    Reading it also lets a check run while the TUI holds the instance lock:
    the running app rewrites these same fields on every sync.
    """
    rooms = []
    for room_id, snap in (state.get("room_meta") or {}).items():
        if not isinstance(snap, dict) or snap.get("is_space"):
            continue
        unread = int(snap.get("unread") or 0)
        if unread <= 0:
            continue
        rooms.append(
            {
                "room_id": room_id,
                "title": snap.get("title") or room_id,
                "unread": unread,
                "highlights": int(snap.get("highlights") or 0),
                "is_direct": bool(snap.get("person")),
            }
        )
    # Loudest first, so a truncated read still shows what matters most.
    rooms.sort(key=lambda r: (-r["unread"], r["title"].lower()))
    invited = [
        {
            "room_id": room_id,
            "title": (snap.get("title") if isinstance(snap, dict) else "") or room_id,
            "inviter": (snap.get("inviter") if isinstance(snap, dict) else "") or "",
        }
        for room_id, snap in (state.get("invites") or {}).items()
    ]
    invited.sort(key=lambda r: r["title"].lower())
    unread = sum(r["unread"] for r in rooms)
    return {
        "unread": unread,
        "highlights": sum(r["highlights"] for r in rooms),
        "invites": len(invited),
        # An invite is one thing waiting for you, the same as one unread
        # message: --format count prints this single number.
        "total": unread + len(invited),
        "new": bool(unread or invited),
        "rooms": rooms,
        "invited": invited,
    }


class MatrixSession:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.state = cfg.load_state()
        # Cross-signing private seeds, used to self-sign, cross-sign peers,
        # and vouch for the identity in a SAS. Restored from the keyring if a
        # previous 'k' unlock persisted them (keyringless hosts keep them in
        # memory for the session only); unlock_cross_signing fills/saves them.
        self._cross_signing: dict[str, bytes] = cfg.load_cross_signing()
        self.my_name = cfg.user_id  # our own display name, filled on first sync
        self.direct_by_room: dict[str, str] = {}  # room_id -> other user id
        self.timelines: dict[str, deque[Message]] = defaultdict(
            lambda: deque(maxlen=TIMELINE_CAP)
        )
        self.last_event_id: dict[str, str] = {}  # room_id -> latest event id seen
        self.presence: dict[str, str] = {}  # user_id -> presence ("online" etc.)
        # space id -> child room ids, restored from state.json so a resumed
        # launch can paint the space columns before the (slow) raw-state
        # refresh has run; refresh_space_children keeps the mirror current.
        self.space_children: dict[str, set[str]] = {
            sid: {c for c in kids if isinstance(c, str)}
            for sid, kids in (self.state.get("space_children") or {}).items()
            if isinstance(kids, list)
        }
        self._space_refresh = None  # background refresh task (GC guard)
        # Back-pagination state per room: the /messages token to continue
        # from, and whether the very beginning of history has been reached.
        self.pagination_tokens: dict[str, str] = {}
        self.pagination_done: dict[str, bool] = {}
        # Rooms whose initial /messages window was fetched and merged this
        # session; reloads then serve the cache (live events keep it current).
        # A gappy sync un-marks the room (see _record_room_timestamps).
        self.history_loaded: set[str] = set()
        # Bumped per room whenever a sync comes back "limited" (the cache has
        # a hole). load_history snapshots it before a fetch: a bump seen
        # afterwards means the merged window may be stale, and any bump at all
        # disables the size-based serve-from-cache shortcut for that room.
        self.gap_gen: dict[str, int] = {}
        # In-flight full member-list fetches by room id (see _fetch_members).
        # Holding the Task matters: asyncio keeps only weak references, so an
        # unreferenced background task can be garbage-collected mid-flight.
        self._member_fetches: dict[str, asyncio.Task] = {}
        # Called with a room id after a background member fetch lands and
        # cached sender names were re-resolved; the app points this at the
        # open room screen so raw @user:server ids repaint as display names.
        self.on_members_loaded = None
        # user id -> display name from a /profile fetch, for DM titles when
        # the member event never arrived this session (lazy-loaded syncs only
        # deliver timeline senders). "" caches a failed fetch so it is not
        # retried on every dashboard rebuild.
        self.profile_names: dict[str, str] = {}
        # In-flight profile fetches by user id (see _fetch_profile). Like
        # _member_fetches, holding the Task keeps it from being GC'd.
        self._profile_fetches: dict[str, asyncio.Task] = {}
        # m.room.name state fetches by room id (see _fetch_room_name). An
        # entry stays after completion: one fetch per room per session.
        self._name_fetches: dict[str, asyncio.Task] = {}
        # Called after a background name fetch of any kind lands (a peer's
        # profile, a room's member list, a room's m.room.name); the app
        # points this at the dashboard refresh so stale titles repaint.
        self.on_names_loaded = None
        # room id -> target event id -> reaction key -> senders. Sets of
        # senders, not counts: history refetches and redelivered syncs would
        # double-count, and removing a reaction must subtract exactly one.
        self.reactions: dict[str, dict[str, dict[str, set[str]]]] = {}
        # reaction event id -> (room, target, key, sender), so a redaction of
        # the reaction event can find what to subtract.
        self._reaction_events: dict[str, tuple[str, str, str, str]] = {}
        # Read-marker debounce (see mark_read): last event id actually
        # POSTed per room, when, and the in-flight trailing sender Task.
        self._marker_sent: dict[str, str] = {}
        self._marker_time: dict[str, float] = {}
        self._marker_tasks: dict[str, asyncio.Task] = {}
        # Composer text stashed when an editor is cancelled, keyed by room id
        # (":<root>"-suffixed for threads); refilled on the next open there.
        # In-memory only; drafts are not worth persisting to disk.
        self.drafts: dict[str, str] = {}
        # Encrypted on-disk mirror of timelines/reactions (see
        # _restore_timelines): dirty when the in-memory copy has moved past
        # the file, saved debounced from the sync path and finally on close.
        self._cache_dirty = False
        self._cache_saved_at = 0.0
        self._cache_saving = False  # a threaded write is in flight
        # Set once _restore_timelines has read the file. A session that never
        # restored (--verify, --check, --export-keys: they connect without a
        # dashboard) holds empty timelines, so saving would rewrite the cache
        # as empty and throw away every room's window, reactions and archive
        # bookkeeping. Nothing is written until the file has been read.
        self._cache_loaded = False
        # The sync token the file on disk was written at. nio persists its
        # own token on every sync response, ours only when a save runs, so
        # close() rewrites the payload when the two drifted apart (see
        # _restore_timelines: a mismatch marks every archive stale).
        self._saved_next_batch = ""
        self._cache_save_task: asyncio.Task | None = None
        # Full-history archives, event id -> Message, downloaded in the
        # background and never evicted (unlike the TIMELINE_CAP'd live
        # window). The token is the deepest /messages position reached, so an
        # unfinished download resumes there; "done" rooms reached the start
        # of history; "stale" rooms may have a hole at the NEW end, which the
        # next backfill re-covers from the head.
        self.archives: dict[str, dict[str, Message]] = {}
        self.archive_tokens: dict[str, str] = {}
        self.archive_done: set[str] = set()
        self.archive_stale: set[str] = set()
        # Rooms present in client.rooms only because _ensure_room registered a
        # bare MatrixRoom for them. Their unread/highlight counters are nio
        # defaults (0), not server truth, so the dashboard must keep serving
        # those two fields from the persisted snapshot until a real sync lands.
        self._placeholder_rooms: set[str] = set()
        # Rooms whose archive moved past its on-disk file (one encrypted file
        # per room, see Config.save_room_archive); only these are rewritten
        # on a save, so a quiet save never re-serializes every big room.
        self._archive_dirty: set[str] = set()
        self._archive_saved_at: dict[str, float] = {}
        # How many archive rows load_older has served this visit, per room,
        # counted from the newest end; reset when the room screen reopens.
        self._archive_served: dict[str, int] = {}
        # One download at a time across all rooms: archiving is a background
        # nicety and must never compete with itself for a loaded homeserver.
        self._backfill_gate = asyncio.Semaphore(1)
        self._backfill_tasks: dict[str, asyncio.Task] = {}
        # Room whose walk currently holds the gate, for the sync-all popup.
        self.backfill_active: str | None = None
        # Image-preview bytes by mxc url, so j/k and reopened previews never
        # refetch this session; bounded FIFO (thumbnails are ~50 KB each).
        # The encrypted on-disk layer (cfg.save_media_cache) persists them
        # across restarts for rooms whose caching is allowed.
        self._media_cache: dict[str, bytes] = {}

        token = cfg.load_token()
        self._new_client(token["device_id"] if token else None)

    def _new_client(self, device_id: str | None = None) -> None:
        """(Re)build the AsyncClient. The encryption store binds to the device
        id at load time, so whenever the session's device id changes the client
        must be rebuilt rather than reused with a mismatched store."""
        client_config = AsyncClientConfig(
            store_sync_tokens=True,
            encryption_enabled=True,
            # nio's default max_timeouts=None retries a failed request
            # forever, so with the network down no call ever returns or
            # raises. Cap the retries and per-request time so an offline
            # failure surfaces as an exception the UI can recover from.
            request_timeout=30,
            max_timeouts=2,
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
        # Guarded: nio dispatches event callbacks with no try/except and sets
        # next_batch *before* running them, so one raise discards every
        # remaining event in that sync response permanently, including the
        # timeline.limited flag _record_room_timestamps needs to notice the
        # cache gap it just created.
        def guarded(callback):
            async def run(room, event) -> None:
                try:
                    await callback(room, event)
                except Exception:
                    _log.exception(
                        "dropping unhandled event %s",
                        getattr(event, "event_id", ""),
                    )
            return run

        self.client.add_event_callback(guarded(self._on_message), RoomMessage)
        self.client.add_event_callback(guarded(self._on_reaction), ReactionEvent)
        self.client.add_event_callback(
            guarded(self._on_redaction), RedactionEvent
        )
        self.client.add_room_account_data_callback(self._on_tags, TagEvent)
        self.client.add_presence_callback(self._on_presence, PresenceEvent)

        # nio decrypts a /messages chunk in place before room_messages()
        # returns, and the decrypted event it substitutes rebuilds
        # ``unsigned`` with only the transaction id, dropping the server's
        # m.relations aggregation (thread counts, the bundled newest edit)
        # that _to_message reads. Save each wrapper's unsigned before nio's
        # handler runs and graft it back on (its own keys win on collision).
        orig_handle = self.client._handle_messages_response

        def handle_messages(response) -> None:
            wrappers = {
                e.event_id: (getattr(e, "source", None) or {}).get("unsigned")
                for e in response.chunk
                if isinstance(e, MegolmEvent)
            }
            orig_handle(response)
            for e in response.chunk:
                u = wrappers.get(getattr(e, "event_id", None))
                if not u or isinstance(e, MegolmEvent):
                    continue  # nothing saved, or the event stayed encrypted
                merged = dict(u)
                own = (getattr(e, "source", None) or {}).get("unsigned")
                # dict.update on a str raises ValueError, out through nio's
                # receive_response and past load_history's network-only guard.
                merged.update(own if isinstance(own, dict) else {})
                e.source["unsigned"] = merged

        self.client._handle_messages_response = handle_messages

    # --- connection -------------------------------------------------------

    async def connect(self, progress=None) -> tuple[bool, str]:
        """Restore from a cached token if possible, else log in with the
        stored password and cache a fresh token. ``progress(msg)`` if given is
        called with short status strings for the loading screen. Raises
        NeedsPassword when only credentials are missing, so the caller can ask
        for them instead of failing the launch."""
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
                # the per-account store key. Unrecoverable with the new key, so
                # discard the stale session and log in fresh.
                step("encryption store unreadable; resetting for a fresh login")
                self.cfg.clear_token()
                self._reset_store()
                token = None
                store_reset = True
            else:
                step("checking session with server")
                try:
                    whoami = await self.client.whoami()
                except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as exc:
                    # A dead network must not kill the launch: the token,
                    # crypto store, and message cache on disk are enough to
                    # read everything. Once connectivity returns, the sync
                    # loop catches a revoked token and runs the keys_upload
                    # skipped here.
                    step(
                        "server unreachable "
                        f"({type(exc).__name__}); starting offline from cache"
                    )
                    return True, "offline: started from the local cache"
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
            # The token path above may have opened an http session; close it
            # so a CLI exit does not warn about an unclosed one. nio reopens
            # it on the next request, so a retry after asking still works.
            await self.client.close()
            if store_reset:
                raise NeedsPassword(
                    "The encryption store could not be opened (it was written "
                    "under the previous store key) and has been reset, so this "
                    "device has to sign in again. Afterwards run "
                    "'matrix --verify' and 'matrix --import-keys <your key "
                    "export>' to restore encrypted history."
                )
            raise NeedsPassword("No saved session; sign in to continue.")

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
            if getattr(resp, "status_code", "") in ("M_FORBIDDEN", "M_UNAUTHORIZED"):
                # Wrong password or wrong user id: worth asking again. Other
                # login errors (rate limited, server down) are not.
                await self.client.close()
                raise NeedsPassword(f"Login failed: {resp.message}")
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
        return True, "logged in with your password (token cached)"

    def _insecure_homeserver(self) -> bool:
        """True when the homeserver URL would send the bearer token in the
        clear. Plain http:// is tolerated only for loopback (local testing)."""
        hs = (self.cfg.homeserver or "").strip()
        if hs.startswith("https://"):
            return False
        try:
            host = urlsplit(hs if "://" in hs else "//" + hs).hostname or ""
        except ValueError:
            host = ""
        return host not in ("localhost", "127.0.0.1", "::1")

    def _reset_store(self) -> None:
        """Delete an unreadable crypto store and rebuild a fresh AsyncClient so
        the next login starts with a store keyed by the current pickle key."""
        try:
            store = Path(self.cfg.store_path)
            for child in store.glob("*"):
                if child.name == "instance.lock":
                    continue  # this process holds the flock on it
                if child.is_file():
                    child.unlink()
                elif child.is_dir():
                    # The archive/ subdirectory: its files were encrypted
                    # with the same pickle key and are equally unreadable.
                    for sub in child.glob("*"):
                        if sub.is_file():
                            sub.unlink()
        except OSError:
            pass
        self._new_client()

    async def export_keys(self, outfile: str, passphrase: str) -> None:
        """Write this device's Megolm room keys to an encrypted file in
        Element's "E2E room keys" format, so either client can read the
        other's export. The counterpart of import_keys, and the only backup
        there is: matrix-nio has no server-side key backup, so a lost store
        means unreadable history unless a file like this exists. Room keys
        only, never the device's own identity keys. Raises OSError if the
        file cannot be written."""
        await self.client.export_keys(outfile, passphrase)
        # nio writes through atomicwrites (temp file + rename), so the mode
        # is the process umask's, not ours: tighten it after the fact.
        try:
            os.chmod(outfile, 0o600)
        except OSError:
            pass

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
        # The on-disk mirror and the archives hold the same placeholders;
        # drop them too, or the next launch would seed them right back before
        # any re-decryption. Rooms simply re-download on their next open.
        self.archives.clear()
        self.archive_tokens.clear()
        self.archive_done.clear()
        self.archive_stale.clear()
        self._archive_dirty.clear()
        self.cfg.clear_timeline_cache()
        self._cache_dirty = False

    def cache_allowed(self, room_id: str) -> bool:
        """Whether this room's messages may be persisted to disk. Global
        switch first ([cache] messages in config.ini); then any space the
        room belongs to that was toggled off in-app wins over everything
        (privacy-first for rooms in several spaces). Membership is read from
        both link directions, exactly as the dashboard resolves a space's
        rooms, so "off" covers every room shown under that space. Spaceless
        rooms and DMs follow the global switch alone."""
        if not self.cfg.cache_messages:
            return False
        blocked = {
            space_id
            for space_id, allowed in (self.state.get("cache_spaces") or {}).items()
            if allowed is False
        }
        if not blocked:
            return True
        # Every way a space can claim a room, matching what the dashboard
        # lists under it. Checking this room's own parents (rather than
        # scanning all rooms for a pointer at the space) keeps this O(opt-outs),
        # which matters because _timeline_payload calls it per cached room.
        for space_id in blocked:
            if room_id == space_id or room_id in self.space_children.get(
                space_id, ()
            ):
                return False
            space = self.client.rooms.get(space_id)
            if room_id in (getattr(space, "children", None) or ()):
                return False
        room = self.client.rooms.get(room_id)
        if blocked & set(getattr(room, "parents", None) or ()):
            return False
        return True

    def space_cache_enabled(self, space_id: str) -> bool:
        return (self.state.get("cache_spaces") or {}).get(space_id) is not False

    def set_space_cache(self, space_id: str, enabled: bool) -> None:
        """Flip a space's caching override. Enabled means "inherit the global
        default", so the entry is removed rather than stored as True and
        state.json only carries actual opt-outs. Turning a space off purges
        its rooms from disk immediately: "off" must mean the decrypted text
        has left the disk, not that it lingers until the next debounced
        save."""
        overrides = self.state.setdefault("cache_spaces", {})
        if enabled:
            overrides.pop(space_id, None)
        else:
            overrides[space_id] = False
        self.cfg.save_state(self.state)
        if not enabled:
            for room_id, task in list(self._backfill_tasks.items()):
                if not self.cache_allowed(room_id):
                    task.cancel()
            self._cache_dirty = True
            self._cache_saved_at = 0.0  # bypass the debounce window
            self._maybe_save_timelines()

    def _restore_timelines(self, resume: bool) -> None:
        """Seed timelines, reactions, and last-seen ids from the encrypted
        cache, so rooms open instantly from local data after a restart.

        The cache is only fully trusted when this launch resumes from exactly
        the sync token the cache was saved at; otherwise (first run, wiped
        state.json, or a crash between nio's token write and our debounced
        save) events may sit between the cached tail and the resume point, so
        every seeded room gets a gap bump: the first open refetches over the
        cache and the merge keeps already-decrypted bodies (see load_history).
        """
        if not self.cfg.cache_messages:
            # Global off: nothing may live on disk, including caches written
            # before the setting changed. Saves are also disabled (see
            # _save_timelines), so this wipe is not undone a minute later.
            self.cfg.clear_timeline_cache()
            return
        msg_fields = {f.name for f in fields(Message)}

        def to_messages(rows) -> list[Message]:
            out = []
            for row in rows if isinstance(rows, list) else []:
                if not isinstance(row, dict):
                    continue
                try:
                    out.append(
                        Message(**{k: v for k, v in row.items() if k in msg_fields})
                    )
                except TypeError:
                    continue  # a field changed type across versions: skip it
            return out

        payload = self.cfg.load_timeline_cache()
        if not payload or payload.get("user_id") != self.cfg.user_id:
            payload = {}
        self._saved_next_batch = payload.get("next_batch") or ""
        # From here the in-memory copy mirrors the file, so saving it back is
        # safe (an unreadable or missing cache counts too: there is nothing
        # left to lose).
        self._cache_loaded = True
        for room_id, rows in (payload.get("timelines") or {}).items():
            if not self.cache_allowed(room_id):
                continue  # written before a space was toggled off
            restored = to_messages(rows)
            if restored:
                self.timelines[room_id].extend(restored)
        for room_id, targets in (payload.get("reactions") or {}).items():
            if not isinstance(targets, dict) or not self.cache_allowed(room_id):
                continue
            self.reactions[room_id] = {
                target: {key: set(senders) for key, senders in keys.items()}
                for target, keys in targets.items()
                if isinstance(keys, dict)
            }
        for event_id, row in (payload.get("reaction_events") or {}).items():
            if isinstance(row, list) and len(row) == 4 and self.cache_allowed(row[0]):
                self._reaction_events[event_id] = tuple(row)
        for room_id, event_id in (payload.get("last_event_id") or {}).items():
            if isinstance(event_id, str) and self.cache_allowed(room_id):
                self.last_event_id.setdefault(room_id, event_id)
        # A pre-split cache (version 1) carried the archives inline; adopt
        # them and mark them dirty so the next save migrates each to its own
        # per-room file and the inline copies stop being written.
        for room_id, rows in (payload.get("archives") or {}).items():
            if not self.cache_allowed(room_id):
                continue
            restored = to_messages(rows)
            if restored:
                arch = self.archives.setdefault(room_id, {})
                arch.update({m.event_id: m for m in restored if m.event_id})
                self._archive_dirty.add(room_id)
        for room_payload in self.cfg.load_room_archives():
            if room_payload.get("user_id") != self.cfg.user_id:
                continue
            room_id = room_payload.get("room_id")
            if not isinstance(room_id, str) or not room_id:
                continue
            if not self.cache_allowed(room_id):
                # Purge on sight: the space was toggled off since this file
                # was written (or while the app was not running).
                self.cfg.clear_room_archive(room_id)
                continue
            restored = to_messages(room_payload.get("messages"))
            if restored:
                arch = self.archives.setdefault(room_id, {})
                arch.update({m.event_id: m for m in restored if m.event_id})
        # Restore the archive bookkeeping only for rooms whose per-room file
        # actually loaded: a corrupt or deleted archive with a surviving
        # "done" flag (or depth token) would otherwise disable the
        # re-download forever while browse/search silently show nothing.
        for room_id, tok in (payload.get("archive_tokens") or {}).items():
            if (
                isinstance(tok, str)
                and tok
                and self.cache_allowed(room_id)
                and room_id in self.archives
            ):
                self.archive_tokens[room_id] = tok
        self.archive_done.update(
            r
            for r in payload.get("archive_done") or []
            if isinstance(r, str) and self.cache_allowed(r) and r in self.archives
        )
        self.archive_stale.update(
            r
            for r in payload.get("archive_stale") or []
            if isinstance(r, str) and self.cache_allowed(r)
        )
        token = self.client.next_batch or self.client.loaded_sync_token
        if not resume or payload.get("next_batch") != token:
            for room_id in list(self.timelines):
                self.gap_gen[room_id] = self.gap_gen.get(room_id, 0) + 1
            # Events between the cache's head and the resume point are also
            # absent from every archive; each gets re-covered on next open.
            # Archive files without a main payload (corrupt or deleted) land
            # here too, via the token mismatch: their alignment is unknown.
            self.archive_stale.update(self.archives)

    def _timeline_payload(self) -> dict:
        """The main cache payload (windows, reactions, archive bookkeeping;
        the archives themselves live in per-room files, see
        _pending_archive_writes). Built without a single await so it
        snapshots a consistent moment (vars() rows are safe to hand to a
        writer thread: Messages are replaced, never mutated, so the dicts
        stay frozen). Rooms the cache policy disallows are filtered from
        every section, which is also what purges them from disk: the next
        save simply rewrites the file without them."""
        for room_id, timeline in self.timelines.items():
            arch = self.archives.get(room_id)
            if arch is None:
                continue
            # The live window holds the freshest copy of overlapping events
            # (redaction marks, adopted server timestamps, live decrypts):
            # fold it into the archive so the disk copy inherits all of it.
            # With one asymmetry: a readable archived body must never be
            # displaced by a no-key placeholder or an emptied redaction
            # tombstone, because a refetch cannot reproduce that text.
            for m in timeline:
                if not m.event_id or m.pending:
                    continue
                old = arch.get(m.event_id)
                if old is not None and old.body and not old.body.startswith(
                    UNDECRYPTABLE
                ):
                    if m.body.startswith(UNDECRYPTABLE):
                        m = replace(old, ts=m.ts, redacted_ts=m.redacted_ts or old.redacted_ts)
                    elif m.redacted_ts and not m.body:
                        m = replace(old, redacted_ts=m.redacted_ts)
                    elif old.edited_ts > m.edited_ts:
                        # The archived copy carries a server-bundled edit the
                        # live one never saw. Backfill never overwrites an
                        # existing archive row, so letting the staler body
                        # win here loses that edit for good.
                        m = replace(
                            m,
                            body=old.body,
                            edited_ts=old.edited_ts,
                            original_body=old.original_body or m.original_body,
                        )
                if arch.get(m.event_id) != m:
                    arch[m.event_id] = m
                    self._archive_dirty.add(room_id)
        allowed = {
            room_id
            for room_id in (
                set(self.timelines)
                | set(self.reactions)
                | set(self.last_event_id)
                | set(self.archives)
                | set(self.archive_tokens)
                | self.archive_done
                | self.archive_stale
            )
            if self.cache_allowed(room_id)
        }
        return {
            "version": 2,
            "user_id": self.cfg.user_id,
            "next_batch": self.client.next_batch
            or self.client.loaded_sync_token
            or "",
            "timelines": {
                room_id: [vars(m) for m in timeline if not m.pending]
                for room_id, timeline in self.timelines.items()
                if timeline and room_id in allowed
            },
            "archive_tokens": {
                room_id: tok
                for room_id, tok in self.archive_tokens.items()
                if room_id in allowed
            },
            "archive_done": sorted(self.archive_done & allowed),
            "archive_stale": sorted(self.archive_stale & allowed),
            "reactions": {
                room_id: {
                    target: {key: sorted(senders) for key, senders in keys.items()}
                    for target, keys in targets.items()
                }
                for room_id, targets in self.reactions.items()
                if room_id in allowed
            },
            "reaction_events": {
                event_id: list(row)
                for event_id, row in self._reaction_events.items()
                if row[0] in allowed
            },
            "last_event_id": {
                room_id: event_id
                for room_id, event_id in self.last_event_id.items()
                if room_id in allowed
            },
        }

    def _pending_archive_writes(
        self, force: bool = False
    ) -> list[tuple[str, dict | None]]:
        """What the per-room archive files need to catch up with memory:
        (room_id, payload) rewrites for dirty allowed rooms, (room_id, None)
        deletions for rooms the policy no longer allows. Untouched rooms do
        not appear, so a save's cost scales with what changed. A fully
        backfilled room's rewrite is held back to ARCHIVE_SAVE_INTERVAL
        unless ``force`` (the flush from close()); it stays dirty until then.
        Deletions are never held back."""
        writes: list[tuple[str, dict | None]] = []
        now = time.monotonic()
        for room_id, arch in self.archives.items():
            if not self.cache_allowed(room_id):
                writes.append((room_id, None))
            elif room_id in self._archive_dirty and arch:
                if (
                    not force
                    and room_id in self.archive_done
                    and room_id not in self.archive_stale
                    and now - self._archive_saved_at.get(room_id, 0.0)
                    < ARCHIVE_SAVE_INTERVAL
                ):
                    # Backfill is finished for this room, so archive_tokens
                    # and archive_done cannot drift out of step with the file
                    # while we wait: the deferred rows are head messages the
                    # live window carries too.
                    continue
                writes.append(
                    (
                        room_id,
                        {
                            "version": 1,
                            "user_id": self.cfg.user_id,
                            "room_id": room_id,
                            "messages": [
                                vars(m)
                                for m in sorted(
                                    arch.values(),
                                    key=lambda m: (m.ts, m.event_id),
                                )
                            ],
                        },
                    )
                )
        return writes

    def _write_cache_files(
        self, payload: dict, archive_writes: list[tuple[str, dict | None]]
    ) -> None:
        # Archives first: the main payload carries the archive_done flags
        # and depth tokens, so a crash between the two writes must leave the
        # rows on disk without their bookkeeping (harmless, re-covered), not
        # the bookkeeping without its rows (a permanent undetected gap).
        for room_id, room_payload in archive_writes:
            if room_payload is None:
                self.cfg.clear_room_archive(room_id)
            else:
                self.cfg.save_room_archive(room_id, room_payload)
        self.cfg.save_timeline_cache(payload)
        self._saved_next_batch = payload.get("next_batch") or ""

    def _save_timelines(self, force: bool = False) -> None:
        if not self.cfg.cache_messages:
            return  # global off: nothing is ever written
        if not self._cache_loaded:
            return  # never read the file; writing would empty it
        payload = self._timeline_payload()
        writes = self._pending_archive_writes(force)
        try:
            self._write_cache_files(payload, writes)
        except Exception:
            return  # disk trouble: stay dirty and let a later save retry
        for room_id, room_payload in writes:
            if room_payload is not None:
                self._archive_saved_at[room_id] = time.monotonic()
        self._archive_dirty.difference_update(r for r, _ in writes)
        self._cache_dirty = False
        self._cache_saved_at = time.monotonic()

    def _maybe_save_timelines(self) -> None:
        """Debounced save: serializing every room's window is too heavy to
        run per 30s sync tick, and close() flushes whatever is left dirty.
        Archive files can reach tens of MB, so under a running loop only the
        payload snapshot happens inline and the serialize/encrypt/write of
        the changed files goes to a thread."""
        if not self.cfg.cache_messages or not self._cache_loaded:
            return
        if not self._cache_dirty or self._cache_saving:
            return
        if time.monotonic() - self._cache_saved_at < 60:
            return
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            self._save_timelines()
            return
        payload = self._timeline_payload()
        writes = self._pending_archive_writes()
        self._archive_dirty.difference_update(r for r, _ in writes)
        self._cache_dirty = False
        self._cache_saved_at = time.monotonic()
        self._cache_saving = True

        async def write() -> None:
            try:
                await asyncio.to_thread(self._write_cache_files, payload, writes)
            except Exception:
                # Retry on a later tick; re-dirty exactly what was pending.
                # Catching Exception, not OSError: the dirty flags were
                # already cleared above, so any other failure here would
                # leave the state claiming "saved" with nothing on disk.
                # The debounce stamp also moves forward so the recheck below
                # cannot hot-loop against a persistently failing disk.
                self._cache_dirty = True
                self._archive_dirty.update(r for r, p in writes if p is not None)
                self._cache_saved_at = time.monotonic()
            else:
                for room_id, room_payload in writes:
                    if room_payload is not None:
                        self._archive_saved_at[room_id] = time.monotonic()
            finally:
                self._cache_saving = False
            if self._cache_dirty:
                # Dirtiness that landed during the write bounced off the
                # _cache_saving guard. The urgent case is a space cache
                # opt-out: its purge must not wait for the next sync tick
                # (which may never come).
                self._maybe_save_timelines()

        self._cache_save_task = asyncio.create_task(write())

    async def initial_sync(self, progress=None) -> bool:
        """One launch sync. Returns whether it actually landed: False means
        the dashboard is being served from cache alone (--check reports that
        as a stale reading rather than "nothing new")."""
        def step(msg: str) -> None:
            if progress is not None:
                progress(msg)

        # The DM map and our own display name are independent HTTP calls;
        # fetch them alongside the sync instead of paying their latency first.
        # Both are awaited right after the sync and are cosmetic on failure.
        direct_fetch = asyncio.ensure_future(self._refresh_direct_map())
        name_fetch = asyncio.ensure_future(self.client.get_displayname())
        # lazy_load_members keeps launch syncs affordable: full member state
        # makes the server serialize every member of every room. Names still
        # resolve from timeline senders and each room's "heroes"; the full
        # member list is fetched in the background on first open (see
        # _fetch_members), and nio fetches it itself before the first send
        # in an encrypted room.
        sync_filter = {
            "room": {
                "timeline": {"limit": 10},
                "state": {"lazy_load_members": True},
            },
            "presence": {"limit": 1000},
        }
        # Resuming from the stored token is the only affordable launch path
        # on a loaded homeserver: a from-scratch sync is Synapse's slowest
        # code path (minutes of wall clock), and even full_state=True on an
        # incremental sync costs tens of seconds of state resolution. So
        # after the first run, sync incrementally and lean on persisted data:
        # room_meta in state.json, nio's own encrypted-room set (see
        # _ensure_room), and the space child map. Quiet rooms then never
        # enter client.rooms this session, which is fine: the dashboard
        # serves them from room_meta and opening one fetches via /messages.
        resume = bool(
            (self.client.next_batch or self.client.loaded_sync_token)
            and self.state.get("room_meta")
        )
        if resume:
            step("syncing new messages")
        else:
            # First run: one big seeding sync so ALL rooms get state, titles,
            # and timestamps. Clear BOTH token fields: nio resolves the sync
            # position as `next_batch or loaded_sync_token`, so clearing only
            # one would still resume.
            self.client.next_batch = ""
            self.client.loaded_sync_token = ""
            step("syncing all rooms (first run, this can take a while)")
        # Seed the in-memory caches from disk BEFORE the sync: _on_message
        # dedupes new arrivals against the seeded timelines by event id, so
        # order matters. After the token clearing above, so a non-resume
        # launch is seen as a token mismatch and gap-bumps the seeded rooms.
        self._restore_timelines(resume)
        # nio only retries rate limits itself: other errors return as
        # SyncError, and a connection dying mid-response raises the raw
        # aiohttp error. Retry a few times rather than killing the startup
        # worker or presenting an empty dashboard as "ready".
        resp = None
        for attempt in range(3):
            try:
                # timeout=0 makes nio pass no HTTP timeout down to aiohttp,
                # so on a half-open connection this request would pend for
                # hours. The wait_for is the only real bound; generous on the
                # big first-run sync, tight on the resume sync.
                resp = await asyncio.wait_for(
                    self.client.sync(
                        timeout=0 if resume else 30000,
                        full_state=not resume,
                        sync_filter=sync_filter,
                        set_presence="online",
                    ),
                    timeout=60 if resume else 600,
                )
            except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as exc:
                resp = None
                detail = type(exc).__name__
            else:
                if not isinstance(resp, SyncError):
                    break
                detail = (
                    getattr(resp, "message", "")
                    or getattr(resp, "status_code", "")
                    or "error"
                )
            if attempt == 2:
                break  # out of attempts; don't promise a retry or sleep first
            step(f"initial sync failed ({detail}); retrying")
            await asyncio.sleep(2 * (attempt + 1))
        synced = not (resp is None or isinstance(resp, SyncError))
        if not synced:
            step("initial sync failed; showing cached data, the background sync will keep retrying")
        try:
            await direct_fetch
        except Exception:
            pass  # DM titles fall back to user ids until the next refresh
        try:
            name_resp = await name_fetch
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError):
            # A dropped connection here is not worth failing startup over; the
            # display name is cosmetic and user_name() falls back to the id.
            name_resp = None
        if getattr(name_resp, "displayname", None):
            self.my_name = name_resp.displayname
        step("loading space hierarchy")
        if self.space_children:
            # Restored from state.json: paint with the cached map now and
            # refresh in the background (each space costs a multi-second
            # raw-state fetch on a loaded server; the sync loop also refetches
            # whenever a space's child links actually change).
            self._space_refresh = asyncio.ensure_future(
                self.refresh_space_children()
            )
        else:
            await self.refresh_space_children()
        step("organizing dashboard")
        self._record_room_timestamps(resp)
        self._persist_recency()
        return synced

    async def _on_presence(self, event: PresenceEvent) -> None:
        self.presence[event.user_id] = event.presence

    def _record_room_timestamps(self, response) -> None:
        """Record the latest event timestamp per room from a sync response so
        recency ranking matches what the server knows, not just the messages we
        happened to receive a typed callback for. Without this, most rooms keep
        last_ts == 0 and the dashboard columns come out in arbitrary order."""
        rooms = getattr(getattr(response, "rooms", None), "join", {}) or {}
        changed = False
        # A left room must leave the persisted snapshot too, or a resumed
        # launch (which never sees the room live) would list it forever.
        left = getattr(getattr(response, "rooms", None), "leave", {}) or {}
        meta = self.state.get("room_meta") or {}
        for room_id in left:
            for bucket in (meta, self.state["last_event_ts"], self.state["last_opened_ts"]):
                if bucket.pop(room_id, None) is not None:
                    changed = True
            # nio only drops a room from client.rooms on an explicit forget(),
            # which this app never sends; left in, the dashboard rebuild would
            # snapshot the room right back into room_meta.
            self.client.rooms.pop(room_id, None)
            self.client.invited_rooms.pop(room_id, None)
            # Rejecting an invite arrives as a leave; drop the snapshot with it.
            if (self.state.get("invites") or {}).pop(room_id, None) is not None:
                changed = True
            # Purge the cached history too. Without this the room's decrypted
            # messages stay in the main cache and in store/archive/<sha>.cache
            # forever: it has no Entry any more, so it is unreachable from the
            # dashboard and the per-space "c" opt-out can never reach it, and
            # _restore_timelines faithfully reloads it every launch.
            for store in (
                self.timelines,
                self.archives,
                self.reactions,
                self.last_event_id,
                self.archive_tokens,
            ):
                if store.pop(room_id, None) is not None:
                    self._cache_dirty = True
            # Keyed by reaction event id, so it has to be filtered by room.
            for event_id, row in list(self._reaction_events.items()):
                if row and row[0] == room_id:
                    del self._reaction_events[event_id]
                    self._cache_dirty = True
            self.gap_gen.pop(room_id, None)
            self.archive_done.discard(room_id)
            self.archive_stale.discard(room_id)
            self.history_loaded.discard(room_id)
            self._archive_dirty.discard(room_id)
            self._placeholder_rooms.discard(room_id)
            self._archive_saved_at.pop(room_id, None)
            self.cfg.clear_room_archive(room_id)
            # Drop the room both as a space (its child map) and as a child of
            # any space still joined, so it stops being counted as "in a
            # space" by the dashboard.
            mirror = self.state.get("space_children") or {}
            if self.space_children.pop(room_id, None) is not None:
                changed = True
            mirror.pop(room_id, None)
            for sid, kids in self.space_children.items():
                if room_id in kids:
                    kids.discard(room_id)
                    stale = mirror.get(sid)
                    if isinstance(stale, list) and room_id in stale:
                        stale.remove(room_id)
                    changed = True
        for room_id, joined in rooms.items():
            # Server truth has arrived for this room, so it is no longer a
            # placeholder standing in for the snapshot.
            self._placeholder_rooms.discard(room_id)
            # A "limited" timeline means the server skipped events between
            # the last sync and this window: the cache is missing a chunk,
            # so the next open must refetch instead of serving the cache.
            if getattr(getattr(joined, "timeline", None), "limited", False):
                self.history_loaded.discard(room_id)
                self.gap_gen[room_id] = self.gap_gen.get(room_id, 0) + 1
                # The skipped events are missing from the archive too; the
                # next backfill re-covers from the head (see _backfill).
                if room_id in self.archives:
                    self.archive_stale.add(room_id)
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
            if events:
                # Any timeline event moved the in-memory cache past the disk
                # copy (the sync callbacks already ran).
                self._cache_dirty = True
        if changed:
            self._persist_recency()
        self._maybe_save_timelines()

    async def close(self) -> None:
        for task in list(self._backfill_tasks.values()):
            task.cancel()
        # Every other background task too: left pending, one mid-flight
        # request wakes up to a closed aiohttp session after the TUI has
        # already restored the terminal, and refresh_space_children does not
        # catch the RuntimeError that raises, so it prints over the shell.
        for registry in (
            self._marker_tasks,
            self._member_fetches,
            self._profile_fetches,
            self._name_fetches,
        ):
            for task in list(registry.values()):
                task.cancel()
        if self._space_refresh is not None:
            self._space_refresh.cancel()
        # A threaded cache write may be mid-file; let it finish (or fail)
        # before the final flush, or two writers would race on the tmp file.
        # The write task can also chain a follow-up save (dirtiness that
        # bounced off the in-flight guard), so keep awaiting until no live
        # task remains; the sync loop is stopped here, so the chain is finite.
        while self._cache_save_task is not None and not self._cache_save_task.done():
            try:
                await self._cache_save_task
            except Exception:
                pass
        if self._cache_dirty or self._archive_dirty:
            self._save_timelines(force=True)
        elif self.cfg.cache_messages and self._cache_loaded:
            token = self.client.next_batch or self.client.loaded_sync_token or ""
            if token and token != self._saved_next_batch:
                # Nothing changed since the last save, but the sync token
                # moved on (an empty sync still advances it). Rewrite just
                # the main payload - cheap, no archive files - so the next
                # launch sees its token and does not read the cache as
                # misaligned, which would mark every archive stale and put
                # every room back in the full-sync queue.
                try:
                    self.cfg.save_timeline_cache(self._timeline_payload())
                    self._saved_next_batch = token
                except Exception:
                    pass
        await self.client.close()

    # --- interactive SAS (emoji) device verification ----------------------

    async def prepare_verification(self) -> None:
        """Drain any stale to-device backlog from earlier cancelled attempts
        before the plain-CLI flow starts listening. Draining happens here,
        with no callbacks registered, so old request/cancel events don't trip
        the live handler. (The TUI needs none of this: its sync loop has been
        consuming to-device events all along.)"""
        await self.client.sync(timeout=3000)
        # A couple of quick syncs flush (and mark processed) any redelivered
        # to-device events from previous runs.
        await self.client.sync(timeout=2000)
        await self.client.sync(timeout=2000)

    async def begin_verification(self, confirm, announce=None, *, initiate=None):
        """Arm an emoji (SAS) verification of one of your own sessions.

        With ``initiate=None`` we are the *responder*: you start the
        verification from the other device (Element, or another matrixcli)
        and we answer. With ``initiate=<device_id>`` we are the *initiator*:
        we send the request to that session and drive the handshake, which
        is how one matrixcli verifies another (neither can be started from
        a bare terminal otherwise).

        matrix-nio only models the legacy ``m.key.verification.start``
        handshake, not the ``request -> ready -> start`` phase modern Element
        opens with (it drops request/ready/done as ``UnknownToDeviceEvent``). So
        we run as the *responder*: you click "Verify session" in Element, we
        answer the request with a ``ready``, which makes Element send the
        ``start`` nio understands; we then drive accept/key/mac and finish with a
        ``done``. This is the path that makes Element share room keys with us.

        The handlers are fed by whatever ``sync()`` the caller keeps running:
        the TUI's sync loop, or ``verify_interactive``'s own. Returns
        ``(result, stop)``: ``result`` is a future that resolves to
        ``(ok, message)`` once the handshake ends either way, and
        ``await stop()`` removes the handlers, cancelling a verification still
        in flight so the other device stops waiting.

        ``confirm(emoji)`` is awaited with ``(glyph, name)`` tuples and returns
        True if they match. It runs in its own task, not inside the sync that
        delivered the key, so it may take as long as the user needs.
        ``announce(msg)`` if given is awaited with progress strings.
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
        #   4. Initiator commitment check: as the initiator nio verifies the
        #      responder's accept `commitment` with the same hex logic as #2
        #      (Sas._check_commitment), so against a base64 commitment it
        #      would cancel. Fix: the initiator's Sas gets an instance
        #      _check_commitment that recomputes base64 (in on_ready).
        Sas._mac_normal = "hkdf-hmac-sha256.v2"
        Sas._mac_v1 = ["hkdf-hmac-sha256.v2"]

        # If this session has unlocked cross-signing, it can vouch for the
        # account identity: include the master key in the mac we send so the
        # peer pins it (see _mac_with_master). None otherwise.
        master_seed = self._cross_signing.get("m.cross_signing.master")
        master_pub = _ed25519_public(master_seed) if master_seed else None

        # nio builds the SAS object for the incoming start from its device
        # store, so our own other devices' keys must be fresh: a session that
        # logged in since the last query would otherwise be "unknown" and its
        # start silently dropped.
        if self.client.olm is not None:
            self.client.olm.add_changed_users({self.cfg.user_id})
        try:
            await self.client.keys_query()
        except LocalProtocolError:
            pass

        result: asyncio.Future[tuple[bool, str]] = (
            asyncio.get_running_loop().create_future()
        )
        # The verification we've committed to. Once set, events for any other
        # transaction id (stale redeliveries from earlier attempts) are ignored.
        active: dict[str, str] = {}  # txn, other_user, other_device
        # The peer's mac event, kept for master-key pinning once verified.
        macs: dict[str, KeyVerificationMac] = {}
        pending: list[asyncio.Task] = []  # the confirm task, for stop()
        started_uuid = str(uuid4())  # our txn when we initiate

        def finish(msg: str, ok: bool = False) -> None:
            if not result.done():
                result.set_result((ok, msg))

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
                not result.done()
                and is_active(getattr(event, "transaction_id", None))
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
            if result.done():
                return
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
                etype == "m.key.verification.ready"
                and initiate is not None
                and is_active(txn)
                and event.sender == active.get("other_user")
            ):
                # We initiated: the peer accepted the request, now WE send the
                # start it will accept. nio's create-as-initiator generates its
                # own txn, so build the Sas directly with ours to keep the
                # request/ready/start framing on one transaction id.
                if txn in self.client.key_verifications:
                    return  # a duplicate ready; the start already went out
                device = self._peer_olm_device(active["other_device"])
                if device is None:
                    finish("could not find that session's keys to verify")
                    return
                sas = Sas(
                    self.cfg.user_id,
                    self.client.device_id,
                    self.client.olm.account.identity_keys["ed25519"],
                    device,
                    transaction_id=txn,
                    short_auth_string=["emoji"],
                    mac_methods=Sas._mac_v1,
                )
                # Bug 4: check the responder's accept commitment as base64.
                sas._check_commitment = _base64_commitment_check(sas)
                if master_pub:
                    sas.get_mac = _mac_with_master(sas, master_pub)
                self.client.key_verifications[txn] = sas
                await say("sending start…")
                start = sas.start_verification()
                resp = await self.client.to_device(start)
                if isinstance(resp, ToDeviceError):
                    finish(f"start failed: {resp.message}")
            elif (
                etype == "m.key.verification.done"
                and is_active(txn)
                and event.sender == active.get("other_user")
            ):
                await send_raw("m.key.verification.done", {})
                finish("verification complete; device verified", ok=True)

        async def on_accept(event) -> None:
            # Only the initiator receives an accept. nio has already run
            # receive_accept_event (state -> accepted) and, per the protocol,
            # it is now our turn to reveal our key.
            if initiate is None or not from_peer(event):
                return
            sas = self.client.key_verifications.get(event.transaction_id)
            if sas is None:
                return
            share = await self.client.to_device(sas.share_key())
            if isinstance(share, ToDeviceError):
                finish(f"key share failed: {share.message}")

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
                finish("other device does not support emoji verification")
                return
            # Rewrite nio's hex commitment as the unpadded base64 the spec
            # requires, before the accept (which reads sas.commitment) goes out.
            sas = self.client.key_verifications.get(event.transaction_id)
            if sas is not None:
                raw = (
                    sas.pubkey.encode()
                    + Api.to_canonical_json(event.source["content"]).encode()
                )
                sas.commitment = (
                    base64.b64encode(hashlib.sha256(raw).digest()).decode().rstrip("=")
                )
                _verify_log.debug("rewrote commitment to base64: %s", sas.commitment)
                if master_pub:
                    sas.get_mac = _mac_with_master(sas, master_pub)
            await say("accepting verification…")
            resp = await self.client.accept_key_verification(event.transaction_id)
            _verify_log.debug("accept resp=%s", type(resp).__name__)
            if isinstance(resp, ToDeviceError):
                finish(f"accept failed: {resp.message}")
            # Deliberately do NOT share our key here. As the responder, nio put a
            # commitment to our key in the accept; the SAS protocol requires us
            # to reveal the key only AFTER the initiator sends theirs. Sending it
            # now (before their key) makes the initiator discard it as an
            # out-of-order message and hang on the emoji screen forever. The key
            # goes out in on_key instead.

        async def complete_if_verified(txn: str) -> None:
            """Both macs are in and ours was confirmed: nio has marked the
            device verified (it covers either arrival order itself); pin the
            account identity the peer vouched for and close the exchange."""
            sas = self.client.key_verifications.get(txn)
            if sas is None or not sas.verified or result.done():
                return
            master = _master_key_from_mac(sas, macs.get(txn))
            if master:
                self.state.setdefault("master_keys", {})[self.cfg.user_id] = master
                self.cfg.save_state(self.state)
            # If we hold the account's self-signing key (unlocked with the
            # Security Key this session), cross-sign the peer now that SAS has
            # authenticated its device key: that is what turns the other
            # matrixcli into a "verified" session for every client, not just a
            # locally trusted one.
            dev = sas.other_olm_device.id if sas.other_olm_device else "?"
            note = ""
            if self._cross_signing.get("m.cross_signing.self_signing") and sas.other_olm_device:
                signed = await self._cross_sign_peer(sas.other_olm_device)
                note = " and cross-signed it" if signed else ""
            # Send our 'done'; the peer replies with its own, closing the
            # request/ready framing on its side. Settle the future first:
            # decide() and on_mac can both get here, and only the one that
            # resolves it sends the done.
            finish(f"verified the other device ({dev}){note}", ok=True)
            await send_raw("m.key.verification.done", {})

        async def decide(txn: str, emoji) -> None:
            """Ask the user, off the sync: a long stare at the emoji must not
            stall (or, under the TUI's sync timeout, cancel) the sync that
            delivered them."""
            try:
                matches = await confirm(emoji)
            except asyncio.CancelledError:
                return
            if result.done():
                return  # the peer cancelled, or stop() ran, while we looked
            if not matches:
                await self.client.cancel_key_verification(txn, reject=True)
                finish("you reported the emoji did not match; cancelled")
                return
            await say("confirming…")
            # Sends our mac; nio also marks the device verified here when the
            # peer's mac already landed.
            resp = await self.client.confirm_short_auth_string(txn)
            if isinstance(resp, ToDeviceError):
                finish(f"confirm failed: {resp.message}")
                return
            await complete_if_verified(txn)

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
            if initiate is None:
                # Responder: we just received the initiator's key (nio
                # established the SAS in receive_key_event before this
                # callback). Now reveal ours, so the initiator can compute the
                # same SAS and show its emoji, then compare. (As the initiator
                # our key already went out in on_accept.)
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
                    finish(f"key share failed: {share.message}")
                    return
            # Compute emoji via _sas_emoji (bug 3 above), not sas.get_emoji().
            emoji = _sas_emoji(sas)
            _verify_log.debug("SAS emoji=%s", [name for _, name in emoji])
            pending.append(asyncio.create_task(decide(event.transaction_id, emoji)))

        async def on_mac(event) -> None:
            _verify_log.debug(
                "RECV mac txn=%s sender=%s",
                getattr(event, "transaction_id", None),
                event.sender,
            )
            if not from_peer(event):
                return
            # nio has already checked this mac (receive_mac_event ran before
            # the callback). If we confirmed first the exchange is complete
            # now; otherwise decide() completes it after the user's answer.
            macs[event.transaction_id] = event
            await complete_if_verified(event.transaction_id)

        async def on_cancel(event) -> None:
            _verify_log.debug(
                "RECV cancel txn=%s sender=%s code=%s reason=%s",
                getattr(event, "transaction_id", None),
                event.sender,
                getattr(event, "code", None),
                getattr(event, "reason", None),
            )
            if not from_peer(event):
                return
            finish(f"the other device cancelled: {event.reason}")

        def guarded(func):
            """A raise inside a to-device callback would kill the caller's
            sync tick (the TUI swallows it and the flow silently hangs);
            surface it as the outcome instead."""

            async def run(*args) -> None:
                try:
                    await func(*args)
                except Exception as exc:
                    _verify_log.exception("%s failed", func.__name__)
                    finish(f"verification failed: {type(exc).__name__}: {exc}")

            return run

        decide = guarded(decide)
        handlers = (
            (guarded(on_unknown), (UnknownToDeviceEvent,)),
            (guarded(on_start), (KeyVerificationStart,)),
            (guarded(on_accept), (KeyVerificationAccept,)),
            (guarded(on_key), (KeyVerificationKey,)),
            (guarded(on_mac), (KeyVerificationMac,)),
            (guarded(on_cancel), (KeyVerificationCancel,)),
        )
        for func, kinds in handlers:
            self.client.add_to_device_callback(func, kinds)
        mine = {func for func, _ in handlers}

        if initiate is not None:
            # We drive: pin the transaction now and send the request; the
            # peer's ready (handled in on_unknown) makes us send the start.
            active["txn"] = started_uuid
            active["other_user"] = self.cfg.user_id
            active["other_device"] = initiate
            await say(f"asking session {initiate} to verify…")
            await send_raw(
                "m.key.verification.request",
                {
                    "from_device": self.client.device_id,
                    "methods": ["m.sas.v1"],
                    "timestamp": int(time.time() * 1000),
                },
            )

        async def stop() -> None:
            self.client.to_device_callbacks = [
                cb for cb in self.client.to_device_callbacks if cb.func not in mine
            ]
            for task in pending:
                task.cancel()
            if (result.done() and not result.cancelled()) or not active.get("txn"):
                return
            # Abandoned mid-flight: tell the other device, so Element stops
            # waiting on its emoji screen, and settle the future for anyone
            # still awaiting it.
            txn = active["txn"]
            try:
                if txn in self.client.key_verifications:
                    await self.client.cancel_key_verification(txn)
                else:
                    await send_raw(
                        "m.key.verification.cancel",
                        {"code": "m.user", "reason": "Verification cancelled"},
                    )
            except Exception:
                pass  # best effort; the peer times out on its own
            finish("cancelled")

        return result, stop

    async def verify_interactive(self, confirm, announce=None) -> str:
        """The plain-CLI wrapper around ``begin_verification``: runs its own
        sync loop until the handshake ends. Returns a human-readable result."""
        result, stop = await self.begin_verification(confirm, announce)
        try:
            while not result.done():
                try:
                    resp = await self.client.sync(timeout=10000)
                except (aiohttp.ClientError, asyncio.TimeoutError, OSError):
                    # Offline raises (max_timeouts) instead of retrying forever
                    # inside nio; keep polling rather than dumping a traceback
                    # over the interactive verification.
                    await asyncio.sleep(2)
                    continue
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
        finally:
            await stop()
        return result.result()[1]

    async def own_sessions(self) -> SessionReport:
        """Every session on this account and how far each is trusted, for
        ``:verify``. Cross-signing is read straight off ``/keys/query``
        (matrix-nio has no model for it): a device counts as cross-signed
        when the account's self-signing key validly signs it and the master
        key validly signs that self-signing key. The master key itself is
        only trusted once this client has pinned it (see
        ``_master_key_from_mac``); until then the server could publish any
        identity it likes, so the report says "cross-signed" but not
        "verified"."""
        user = self.cfg.user_id
        pinned = (self.state.get("master_keys") or {}).get(user)
        pinned = pinned if isinstance(pinned, str) else None
        keys = await self._get_json(
            "POST", "/_matrix/client/v3/keys/query", {"device_keys": {user: []}}
        )
        if keys is None:
            return SessionReport(
                [], None, pinned, error="could not fetch the account's keys"
            )
        devices = await self._get_json("GET", "/_matrix/client/v3/devices") or {}

        def for_user(section: str):
            block = keys.get(section)
            return block.get(user) if isinstance(block, dict) else None

        master_obj = for_user("master_keys")
        ssk_obj = for_user("self_signing_keys")
        master = _single_key(master_obj)
        ssk = _single_key(ssk_obj)
        ssk_ok = bool(master and ssk and _signed_by(ssk_obj, user, master, master))

        device_keys = keys.get("device_keys")
        device_keys = device_keys.get(user) if isinstance(device_keys, dict) else None
        if not isinstance(device_keys, dict):
            device_keys = {}
        meta: dict[str, dict] = {}
        for d in devices.get("devices") or []:
            if isinstance(d, dict) and isinstance(d.get("device_id"), str):
                meta[d["device_id"]] = d
        rows = []
        for device_id in set(device_keys) | set(meta):
            dk = device_keys.get(device_id)
            dk = dk if isinstance(dk, dict) else {}
            dk_keys = dk.get("keys")
            fingerprint = dk_keys.get(f"ed25519:{device_id}") if isinstance(dk_keys, dict) else None
            fingerprint = fingerprint if isinstance(fingerprint, str) else ""
            m = meta.get(device_id, {})
            name = m.get("display_name")
            unsigned = dk.get("unsigned")
            if not isinstance(name, str) and isinstance(unsigned, dict):
                name = unsigned.get("device_display_name")
            ts = m.get("last_seen_ts")
            ip = m.get("last_seen_ip")
            try:
                verified_here = self.client.device_store[user][device_id].verified
            except (KeyError, LocalProtocolError):
                verified_here = False
            rows.append(
                OwnDevice(
                    device_id=device_id,
                    display_name=_clean(name) if isinstance(name, str) else "",
                    last_seen_ts=ts if isinstance(ts, int) and not isinstance(ts, bool) else 0,
                    last_seen_ip=_clean(ip) if isinstance(ip, str) else "",
                    fingerprint=fingerprint,
                    cross_signed=bool(
                        ssk_ok and fingerprint and _signed_by(dk, user, ssk, ssk)
                    ),
                    verified_here=bool(verified_here),
                    is_this=device_id == self.client.device_id,
                )
            )
        rows.sort(key=lambda d: (not d.is_this, -d.last_seen_ts, d.device_id))

        if master and pinned is None:
            # Verified with an older matrixcli, before the mac pinned the
            # identity: a device we SAS-verified then may have signed the
            # master key itself, which vouches for it just as well.
            for d in rows:
                if d.verified_here and d.fingerprint and _signed_by(
                    master_obj, user, d.device_id, d.fingerprint
                ):
                    pinned = master
                    self.state.setdefault("master_keys", {})[user] = master
                    self.cfg.save_state(self.state)
                    break
        return SessionReport(rows, master, pinned)

    def _peer_olm_device(self, device_id: str):
        """The nio ``OlmDevice`` for one of our own other sessions, or None.
        begin_verification's keys_query has just refreshed the store."""
        try:
            return self.client.device_store[self.cfg.user_id][device_id]
        except (KeyError, LocalProtocolError):
            return None

    async def _cross_sign_peer(self, olm_device) -> bool:
        """Cross-sign a session we just authenticated over SAS, so every
        client sees it as verified. The device key is refetched and its
        fingerprint checked against the one SAS compared, so a server that
        swapped the key cannot get it signed."""
        ssk = self._cross_signing.get("m.cross_signing.self_signing")
        if not ssk:
            return False
        user = self.cfg.user_id
        keys = await self._get_json(
            "POST", "/_matrix/client/v3/keys/query", {"device_keys": {user: []}}
        )
        obj = (
            ((keys or {}).get("device_keys") or {}).get(user) or {}
        ).get(olm_device.id) if isinstance(keys, dict) else None
        if not isinstance(obj, dict):
            return False
        published = (obj.get("keys") or {}).get(f"ed25519:{olm_device.id}")
        if published != olm_device.ed25519:
            # The key SAS authenticated is not the one the server now serves.
            return False
        ok, _ = await self._upload_signatures(
            {user: {olm_device.id: self._cross_sign(ssk, obj)}}
        )
        return ok

    async def unlock_cross_signing(self, recovery: str) -> tuple[bool, str]:
        """Bring the account's cross-signing keys into matrixcli from
        server-side secret storage, unlocked with the account's Security Key
        (``EsTx ...``) or Security Phrase, and sign THIS device with the
        self-signing key so every client sees it as verified.

        This is the Element-free path to a verified session: nio cannot
        create or read cross-signing on its own, so we open secret storage
        by hand (SSSS, ``m.secret_storage.v1.aes-hmac-sha2``), decrypt the
        master and self-signing private keys, check they match the identity
        the server publishes, cross-sign this device, and pin the master key
        as confirmed. Returns ``(ok, message)``.

        The private keys are held in memory for the rest of the session (so
        the same unlock can also cross-sign a peer after an emoji check), but
        never written to disk.
        """
        recovery = recovery.strip()
        if not recovery:
            return False, "no Security Key entered"
        user = self.cfg.user_id

        default = await self._account_data("m.secret_storage.default_key")
        key_id = default.get("key") if isinstance(default, dict) else None
        if not isinstance(key_id, str) or not key_id:
            return False, (
                "this account has no default secret-storage key; set up "
                "Secure Backup in Element once, then try again"
            )
        desc = await self._account_data(f"m.secret_storage.key.{key_id}")
        if not isinstance(desc, dict):
            return False, "could not read the secret-storage key description"
        if desc.get("algorithm") != "m.secret_storage.v1.aes-hmac-sha2":
            return False, (
                f"unsupported secret-storage algorithm {desc.get('algorithm')!r}"
            )

        storage_key = self._derive_storage_key(recovery, desc)
        if storage_key is None:
            return False, (
                "that does not look like this account's Security Key or "
                "Security Phrase"
            )
        if not _ssss_key_matches(storage_key, desc):
            return False, "the Security Key or Phrase did not match"

        secrets: dict[str, bytes] = {}
        for name in (
            "m.cross_signing.master",
            "m.cross_signing.self_signing",
            "m.cross_signing.user_signing",
        ):
            data = await self._account_data(name)
            enc = (data or {}).get("encrypted") if isinstance(data, dict) else None
            blob = enc.get(key_id) if isinstance(enc, dict) else None
            plain = _ssss_decrypt(storage_key, name, blob)
            if plain is None:
                if name == "m.cross_signing.user_signing":
                    continue  # only needed to verify OTHER users; optional here
                return False, f"could not decrypt {name} from secret storage"
            try:
                secrets[name] = _unb64(plain.decode("ascii"))
            except (ValueError, UnicodeDecodeError):
                return False, f"{name} in secret storage is malformed"
            if len(secrets[name]) != 32:
                return False, f"{name} in secret storage is the wrong size"

        # The identity the server publishes must be the one whose private
        # keys we just decrypted; otherwise a tampered server could make us
        # sign under a key it controls.
        keys = await self._get_json(
            "POST", "/_matrix/client/v3/keys/query", {"device_keys": {user: []}}
        )
        if not isinstance(keys, dict):
            return False, "could not fetch the account's keys to check against"
        master_pub = _single_key((keys.get("master_keys") or {}).get(user))
        ssk_pub = _single_key((keys.get("self_signing_keys") or {}).get(user))
        if _ed25519_public(secrets["m.cross_signing.master"]) != master_pub:
            return False, (
                "the master key in secret storage does not match the one the "
                "server publishes; refusing to sign"
            )
        if _ed25519_public(secrets["m.cross_signing.self_signing"]) != ssk_pub:
            return False, "the self-signing key does not match the server's"

        device_obj = (
            ((keys.get("device_keys") or {}).get(user) or {}).get(self.client.device_id)
        )
        if not isinstance(device_obj, dict):
            return False, "the server has no device keys for this session yet"

        signed = self._cross_sign(secrets["m.cross_signing.self_signing"], device_obj)
        ok, detail = await self._upload_signatures({user: {self.client.device_id: signed}})
        if not ok:
            return False, f"signing this device failed: {detail}"

        # We hold the master private key, so the identity is confirmed for real.
        self.state.setdefault("master_keys", {})[user] = master_pub
        self.cfg.save_state(self.state)
        self._cross_signing = secrets
        # Persist to the keyring so a single unlock sticks across launches;
        # returns False (and stays memory-only) on a keyringless host.
        persisted = self.cfg.save_cross_signing(secrets)
        tail = "" if persisted else " (this session only; no keyring to store it in)"
        return True, "unlocked cross-signing and verified this session" + tail

    def _derive_storage_key(self, recovery: str, desc: dict) -> bytes | None:
        """The 32-byte secret-storage key from the recovery input: a Security
        Key decodes directly; a Security Phrase is stretched with the PBKDF2
        parameters in the key description. None when it is neither."""
        key = _decode_security_key(recovery)
        if key is not None:
            return key
        passphrase = desc.get("passphrase")
        if not isinstance(passphrase, dict) or passphrase.get("algorithm") != "m.pbkdf2":
            return None
        salt = passphrase.get("salt")
        iterations = passphrase.get("iterations")
        if not isinstance(salt, str) or not isinstance(iterations, int) or iterations <= 0:
            return None
        bits = passphrase.get("bits")
        length = bits // 8 if isinstance(bits, int) and bits > 0 else 32
        try:
            return hashlib.pbkdf2_hmac(
                "sha512", recovery.encode(), salt.encode(), iterations, dklen=length
            )
        except (ValueError, OverflowError):
            return None

    def _cross_sign(self, ssk_seed: bytes, key_obj: dict) -> dict:
        """``key_obj`` (a device key or another user's master key) with our
        account's self-signing signature added, ready to upload. Existing
        signatures are kept so the endpoint sees the device's own too."""
        ssk_pub = _ed25519_public(ssk_seed)
        signed = dict(key_obj)
        sigs = {
            u: dict(v) for u, v in (signed.get("signatures") or {}).items()
        }
        sigs.setdefault(self.cfg.user_id, {})[f"ed25519:{ssk_pub}"] = _sign_object(
            ssk_seed, key_obj
        )
        signed["signatures"] = sigs
        return signed

    async def _upload_signatures(self, signed: dict) -> tuple[bool, str]:
        resp = await self._get_json(
            "POST", "/_matrix/client/v3/keys/signatures/upload", signed
        )
        if resp is None:
            return False, "the server rejected the signature upload"
        failures = resp.get("failures") if isinstance(resp, dict) else None
        if failures:
            return False, f"the server reported {failures}"
        return True, "ok"

    async def _account_data(self, event_type: str):
        """One global account-data event, or None. Used for secret storage,
        which nio does not model."""
        return await self._get_json(
            "GET",
            f"/_matrix/client/v3/user/{quote(self.cfg.user_id, safe='')}"
            f"/account_data/{quote(event_type, safe='')}",
        )

    async def _get_json(self, method: str, path: str, body: dict | None = None):
        """One authenticated request on its own connection, parsed as JSON;
        None on any failure. nio's own helpers parse into its response
        classes, which have no model for what these endpoints return."""
        headers = {"Authorization": f"Bearer {self.client.access_token}"}
        try:
            timeout = aiohttp.ClientTimeout(total=15)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.request(
                    method,
                    f"{self.cfg.homeserver}{path}",
                    json=body,
                    headers=headers,
                    allow_redirects=False,
                ) as r:
                    if r.status != 200:
                        return None
                    data = await r.json()
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError, ValueError):
            return None
        return data if isinstance(data, dict) else None

    # --- event handling ---------------------------------------------------

    async def _on_message(self, room: MatrixRoom, event: RoomMessage) -> None:
        body = getattr(event, "body", None)
        if body is None:
            body = f"<{type(event).__name__}>"
        # Edits arrive as ordinary messages; they are cached with the id they
        # rewrite and folded into it at display time (see fold_edits).
        replaces = _edit_target(event)
        if replaces:
            body = _edit_body(event, body)
        reply_to = "" if replaces else _reply_target(event)
        reply_name = reply_snippet = ""
        if reply_to:
            body, quoted_sender, reply_snippet = _strip_reply_fallback(body)
            if quoted_sender:
                reply_name = room.user_name(quoted_sender) or quoted_sender
        body = _clean(body)
        timeline = self.timelines[room.room_id]
        # The event may already be cached as our own local echo (send()
        # appends it) or from a redelivered sync; do not append it twice. The
        # echo carries this machine's wall-clock time though, and every later
        # merge lets cached entries win, so a skewed clock would misorder the
        # timeline forever: adopt the server's timestamp here.
        cached_at = next(
            (
                i
                for i, m in enumerate(timeline)
                if event.event_id and m.event_id == event.event_id
            ),
            None,
        )
        if cached_at is not None:
            if timeline[cached_at].ts != event.server_timestamp:
                timeline[cached_at] = replace(
                    timeline[cached_at], ts=event.server_timestamp
                )
        else:
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
                    reply_to=reply_to,
                    reply_name=_clean(reply_name),
                    reply_snippet=_clean(reply_snippet),
                    replaces=replaces,
                    mentions_me=self._mentions_me(event, body),
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

    def _mentions_me(self, event, body: str) -> bool:
        """Whether an event names this account: the structured m.mentions list
        when the sender's client set one, else the reply-era fallback of our
        display name or user id appearing in the body. Own messages never
        count (quoting yourself is not a ping)."""
        if event.sender == self.cfg.user_id:
            return False
        content = (getattr(event, "source", {}) or {}).get("content", {}) or {}
        mentions = content.get("m.mentions")
        if isinstance(mentions, dict):
            ids = mentions.get("user_ids")
            if isinstance(ids, list) and self.cfg.user_id in ids:
                return True
        folded = (body or "").casefold()
        if self.cfg.user_id.casefold() in folded:
            return True
        name = (self.my_name or "").casefold()
        return bool(name) and name != self.cfg.user_id.casefold() and name in folded

    def _note_reaction(
        self, room_id: str, event_id: str, sender: str, target: str, key: str
    ) -> None:
        """Record one m.reaction into the per-message aggregates."""
        key = _reaction_key(key)
        if not (room_id and event_id and sender and target and key):
            return
        by_key = self.reactions.setdefault(room_id, {}).setdefault(target, {})
        by_key.setdefault(key, set()).add(sender)
        self._reaction_events[event_id] = (room_id, target, key, sender)

    def _drop_reaction_vote(
        self, room_id: str, target: str, key: str, sender: str
    ) -> None:
        """Subtract one vote, and prune whatever it emptied. Without the
        pruning every removed reaction leaves an empty sender set (and every
        emptied message an empty dict) in the aggregate for the life of the
        session, and each one is re-serialized into the cache on every save."""
        by_target = self.reactions.get(room_id)
        by_key = by_target.get(target) if by_target else None
        senders = by_key.get(key) if by_key else None
        if senders is None:
            return
        senders.discard(sender)
        if not senders:
            del by_key[key]
        if not by_key:
            del by_target[target]
        if not by_target:
            del self.reactions[room_id]

    def reaction_summary(self, room_id: str, event_id: str) -> list[tuple[str, int]]:
        """(key, count) pairs for one message, most-used first."""
        by_key = self.reactions.get(room_id, {}).get(event_id) or {}
        return sorted(
            ((k, len(s)) for k, s in by_key.items() if s),
            key=lambda kv: (-kv[1], kv[0]),
        )

    def reaction_detail(
        self, room_id: str, event_id: str
    ) -> list[tuple[str, list[tuple[str, str]]]]:
        """Who is behind each reaction badge: (key, [(sender id, display
        name), ...]) pairs, keys in reaction_summary order so the popup lines
        up with the badges, names alphabetically within a key. Names resolve
        through the room's member map and fall back to the bare user id."""
        room = self.client.rooms.get(room_id)
        by_key = self.reactions.get(room_id, {}).get(event_id) or {}
        out = []
        for key, _count in self.reaction_summary(room_id, event_id):
            senders = [
                (s, _clean((room.user_name(s) if room else None) or s) or s)
                for s in by_key.get(key, ())
            ]
            senders.sort(key=lambda sn: sn[1].lower())
            out.append((key, senders))
        return out

    async def _on_reaction(self, room: MatrixRoom, event: ReactionEvent) -> None:
        self._note_reaction(
            room.room_id, event.event_id, event.sender, event.reacts_to, event.key
        )
        # Nothing was appended to the timeline; the open room view redraws off
        # the latest-event id, so bump it (same as _on_redaction).
        if event.event_id:
            self.last_event_id[room.room_id] = event.event_id

    async def _on_redaction(self, room: MatrixRoom, event: RedactionEvent) -> None:
        """A deletion arrives as its own event naming the id it removes. Flag
        the cached copy instead of dropping it: the text is already gone from
        the server, so what we received earlier is all anyone can still show,
        and it stays available in the history popup for the rest of the session."""
        # Redacting a reaction takes the vote back: subtract that one sender.
        noted = self._reaction_events.pop(event.redacts or "", None)
        if noted is not None:
            room_id, target, key, sender = noted
            self._drop_reaction_vote(room_id, target, key, sender)
        timeline = self.timelines.get(room.room_id)
        for i, m in enumerate(timeline or ()):
            if m.event_id and m.event_id == event.redacts:
                timeline[i] = replace(m, redacted_ts=event.server_timestamp)
                break
        # Flag the archived copy too: a target older than the live window
        # only exists there, and the backfill's never-overwrite guard means
        # no refetch would ever mark it.
        self._flag_archived_redaction(
            room.room_id, event.redacts or "", event.server_timestamp
        )
        # Nothing was appended, so the open room view has to be told to redraw
        # some other way; the latest-event id is what it watches.
        if event.event_id:
            self.last_event_id[room.room_id] = event.event_id

    def _flag_archived_redaction(
        self, room_id: str, target: str, ts: int
    ) -> None:
        arch = self.archives.get(room_id)
        m = arch.get(target) if arch else None
        if m is not None and not m.redacted_ts:
            arch[target] = replace(m, redacted_ts=ts)
            self._archive_dirty.add(room_id)
            self._cache_dirty = True

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
            # Bounded: aiohttp's default total timeout is 5 minutes, far too
            # long for something awaited during startup.
            timeout = aiohttp.ClientTimeout(total=15)
            async with aiohttp.ClientSession(timeout=timeout) as session:
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
            # A resumed (incremental) launch leaves quiet spaces out of
            # client.rooms entirely; the persisted snapshot still knows them.
            for rid, m in (self.state.get("room_meta") or {}).items():
                if isinstance(m, dict) and m.get("is_space") and rid not in spaces:
                    spaces.append(rid)
        headers = {"Authorization": f"Bearer {self.client.access_token}"}
        try:
            # Bounded: the background sync loop awaits this on every child-link
            # change, and a hung request there would freeze every live update.
            timeout = aiohttp.ClientTimeout(total=15)
            async with aiohttp.ClientSession(timeout=timeout) as http:

                async def fetch(sid: str) -> None:
                    url = (
                        f"{self.cfg.homeserver}/_matrix/client/v3/rooms/"
                        f"{quote(sid, safe='')}/state"
                    )
                    async with http.get(url, headers=headers, allow_redirects=False) as r:
                        if r.status != 200:
                            return
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

                # In parallel: each space's raw state takes seconds on a
                # loaded server.
                await asyncio.gather(*(fetch(sid) for sid in spaces))
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
            return
        finally:
            # Mirror into state.json so the next launch paints the space
            # columns from disk and runs this refresh in the background.
            mirror = self.state.setdefault("space_children", {})
            changed = False
            for sid, kids in self.space_children.items():
                as_list = sorted(kids)
                if mirror.get(sid) != as_list:
                    mirror[sid] = as_list
                    changed = True
            if changed:
                self._persist_recency()

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
        if (
            person is None
            and not is_space
            and not (
                getattr(room, "name", None) or getattr(room, "canonical_alias", None)
            )
        ):
            # Chats the other side never flagged in m.direct: a two-member
            # room with no name or alias is treated as a DM. Under a
            # lazy-loaded resume sync the users map holds only timeline
            # senders, so a big room can look two-member; with members
            # unsynced a snapshot that says group wins, and the full member
            # list is fetched in the background to settle it.
            others = [uid for uid in room.users if uid != self.cfg.user_id]
            member_count = room.member_count or len(room.users)
            if member_count == 2 and len(others) == 1:
                if room.members_synced:
                    person = others[0]
                else:
                    self._fetch_members(room_id)
                    snap = self.state.get("room_meta", {}).get(room_id)
                    snap_says_group = (
                        isinstance(snap, dict)
                        and not snap.get("person")
                        and bool(snap.get("title"))
                    )
                    if not snap_says_group:
                        person = others[0]
        is_direct = person is not None
        if is_direct:
            # user_name() resolves only members seen this session; a DM whose
            # peer sent nothing since the resume token stays unresolved, so
            # fall back to a cached /profile name before the bare user id.
            title = (
                (room.user_name(person) or self.profile_names.get(person) or person)
                if person
                else room.display_name
            )
        else:
            title = room.display_name
        # notification_count already includes highlights (mentions); adding
        # unread_highlights on top would count every mention twice. The
        # highlight count is carried separately for the red (N!) badge.
        unread = room.unread_notifications or 0
        highlights = room.unread_highlights or 0
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
            highlights=highlights,
            online=online,
            is_favourite=is_favourite,
        )

    def _all_entries(self) -> list[Entry]:
        """One Entry per joined room: the live rooms from client.rooms plus,
        for rooms with no activity since the resume token (which a resumed
        launch never delivers, see initial_sync), entries rebuilt from the
        persisted room_meta snapshot. As a side effect the snapshot is
        refreshed from the live rooms, so the next launch's dashboard shows
        current titles and unread badges before any sync has landed."""
        meta = self.state.setdefault("room_meta", {})
        entries = []
        for room in self.client.rooms.values():
            e = self._entry(room)
            snap = meta.get(e.room_id)
            # A room synced incrementally has no state this session, so nio
            # invents a display name from the member "heroes", which must
            # never displace a real title: only an actual room name/alias or
            # a resolved DM peer outranks the snapshot. The same rule keeps
            # the snapshot from being poisoned for the next launch. Unread
            # counts stay live; the server sends them with every mention.
            if isinstance(snap, dict):
                named = (
                    room.named_room_name()
                    if hasattr(room, "named_room_name")
                    else room.display_name
                )
                snap_title = snap.get("title") or ""
                if (
                    not named
                    and not e.person
                    and snap_title
                    and not snap_title.startswith("!")
                ):
                    e = replace(e, title=snap_title)
                # DM counterpart: a peer with no member event this session
                # degrades the title to the bare @user:server id; a real name
                # from an earlier session outranks that and keeps the refresh
                # below from overwriting the snapshot with the raw id.
                if (
                    e.person
                    and e.title == e.person
                    and snap_title
                    and not snap_title.startswith(("!", "@"))
                ):
                    e = replace(e, title=snap_title)
                if snap.get("is_space") and not e.is_space:
                    e = replace(e, is_space=True)
                if e.room_id in self._placeholder_rooms:
                    # A placeholder reports 0/0 because nio never filled it
                    # in. Taking that as live would not just show the wrong
                    # badge: the snapshot refresh below writes it back to
                    # state.json, losing the count for good (sync never
                    # re-sends it for a quiet room).
                    e = replace(
                        e,
                        unread=int(snap.get("unread") or 0),
                        highlights=int(snap.get("highlights") or 0),
                    )
                if snap.get("is_favourite") and not (room.tags or {}):
                    e = replace(e, is_favourite=True)
            if e.person and e.title == e.person:
                # Still unresolved (no member event, no usable snapshot):
                # fetch the profile in the background; the repaint it
                # triggers rebuilds the entry with the fetched name.
                self._fetch_profile(e.person)
            if (
                not e.is_space
                and self.direct_by_room.get(e.room_id) is None
                and (e.person is None or not room.members_synced)
                and not (
                    getattr(room, "name", None)
                    or getattr(room, "canonical_alias", None)
                )
            ):
                # A live room with no name state this session rests on the
                # snapshot or on nio's heroes-based name; fetch m.room.name
                # once to settle it. Confirmed DMs are skipped, they have no
                # room name to find.
                self._fetch_room_name(e.room_id)
            entries.append(e)
        live = {e.room_id for e in entries}
        changed = False
        for e in entries:
            snap = {
                "title": e.title,
                "is_space": e.is_space,
                "person": e.person or "",
                "is_favourite": e.is_favourite,
                "unread": e.unread,
                "highlights": e.highlights,
            }
            if meta.get(e.room_id) != snap:
                meta[e.room_id] = snap
                changed = True
        if changed:
            self._persist_recency()
        for rid, m in meta.items():
            if rid in live or not isinstance(m, dict):
                continue
            person = m.get("person") or None
            title = m.get("title") or rid
            if person and title == person:
                # A poisoned snapshot stores the raw @user:server id; repair
                # it from the profile cache (the fetch also heals the
                # persisted snapshot).
                self._fetch_profile(person)
                title = self.profile_names.get(person) or title
            entries.append(
                Entry(
                    room_id=rid,
                    title=title,
                    unread=int(m.get("unread") or 0),
                    is_direct=bool(person),
                    person=person,
                    last_ts=self.state["last_event_ts"].get(rid, 0),
                    is_space=bool(m.get("is_space")),
                    highlights=int(m.get("highlights") or 0),
                    is_favourite=bool(m.get("is_favourite")),
                )
            )
        return entries

    def section_rows(self, section: str) -> int:
        """The dashboard row budget for the Recent or Favourites section,
        floored at MIN_SECTION_ROWS. HomeScreen's +/- adjust it and a
        shrinking terminal clamps it (both persist to state.json); the floor
        also keeps a hand-edited or corrupt state value from collapsing a
        section."""
        try:
            return max(MIN_SECTION_ROWS, int(self.state.get(f"{section}_rows") or 0))
        except (TypeError, ValueError):
            return MIN_SECTION_ROWS

    def dashboard(self, selected_space: str | None = None) -> dict:
        """Data for the three-column home screen.

        Keys:
        * spaces / space_rooms -- the list of spaces, and the child rooms of
                     the selected one (resolved from m.space.child state).
        * others  -- joined rooms that belong to no space at all (Element's
                     Home view shows these; without this key they would be
                     reachable only through search).
        * invites -- pending invitations (accept with Enter); shown above
                     Recent, hidden when empty.
        * recent  -- the rooms most recently opened in this client.
        * favourites -- rooms (or DMs) tagged m.favourite.
        * dms     -- one entry per person, most-recently-active first.
        """
        entries = self._all_entries()
        by_id = {e.room_id: e for e in entries}
        invites = self.invites()

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

        # --- Other rooms: joined rooms outside every space -----------------
        # A room counts as "in a space" when any joined space links to it from
        # either side: the space's child set (nio-parsed or the raw-state
        # fallback map) or the room's own parent pointer at a joined space.
        # Everything else would be invisible on this screen (search aside), so
        # it gets its own section.
        space_ids = {e.room_id for e in entries if e.is_space}
        in_some_space: set[str] = set()
        for sid in space_ids:
            room = self.client.rooms.get(sid)
            in_some_space |= set(getattr(room, "children", set()) or ())
        for sid in space_ids:
            # Only spaces you are still in may claim a child. Unfiltered, a
            # stale entry for a space you have left keeps hiding its former
            # rooms from "others" while the space itself no longer shows.
            in_some_space |= self.space_children.get(sid, set())
        for rid, room in self.client.rooms.items():
            if space_ids & (getattr(room, "parents", set()) or set()):
                in_some_space.add(rid)
        others = sorted(
            (
                e
                for e in entries
                if not e.is_space
                and not e.is_direct
                and e.room_id not in in_some_space
            ),
            key=lambda e: (e.unread == 0, -recency(e), e.title.lower()),
        )

        # --- Recent column: rooms you last opened, newest first -----------
        recent = sorted(
            (e for e in entries if not e.is_space and opened.get(e.room_id)),
            key=lambda e: -opened[e.room_id],
        )[: self.section_rows("recent")]

        # --- Favourites column --------------------------------------------
        favourites = sorted(
            (e for e in entries if e.is_favourite and not e.is_space),
            key=lambda e: (e.unread == 0, -recency(e), e.title.lower()),
        )[: self.section_rows("favourites")]

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
            "others": others,
            "invites": invites,
            "recent": recent,
            "favourites": favourites,
            "dms": dms,
            "all": list(by_id.values()),
        }

    def invites(self) -> list[Entry]:
        """Pending invitations, live ones from the sync merged with the
        persisted snapshot.

        The snapshot is what makes an invite survive a restart: the server
        sends an invite in the sync that carries its membership event and
        never again, so a resumed launch (the only affordable one, see
        initial_sync) would show an untouched invite for exactly one run and
        then quietly lose it. Answering the invite anywhere clears it: joining
        puts the room in room_meta, and rejecting it sends a leave that
        _record_room_timestamps drops."""
        stored = self.state.setdefault("invites", {})
        if not isinstance(stored, dict):
            stored = self.state["invites"] = {}
        live = getattr(self.client, "invited_rooms", {}) or {}
        changed = False
        for rid, room in live.items():
            snap = {
                "title": _clean(getattr(room, "display_name", "")) or rid,
                "inviter": getattr(room, "inviter", None) or "",
            }
            if stored.get(rid) != snap:
                stored[rid] = snap
                changed = True
        joined = self.state.get("room_meta") or {}
        for rid in [
            r
            for r in stored
            if r not in live and (r in joined or r in self.client.rooms)
        ]:
            del stored[rid]
            changed = True
        if changed:
            self._persist_recency()
        invites = [
            Entry(
                room_id=rid,
                title=(snap.get("title") if isinstance(snap, dict) else "") or rid,
                unread=0,
                is_direct=False,
                person=(snap.get("inviter") if isinstance(snap, dict) else "") or None,
                last_ts=0,
                is_invite=True,
            )
            for rid, snap in stored.items()
        ]
        invites.sort(key=lambda e: e.title.lower())
        return invites

    async def accept_invite(self, room_id: str) -> tuple[bool, str]:
        """Join a room we were invited to. The room moves from invited_rooms
        to rooms through the next sync; the invite entry is dropped locally
        right away so the dashboard reflects the acceptance immediately."""
        try:
            resp = await self.client.join(room_id)
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as exc:
            return False, f"could not join: {type(exc).__name__}"
        if isinstance(resp, JoinError):
            return False, f"could not join: {resp.message}"
        self.client.invited_rooms.pop(room_id, None)
        # The snapshot outlives the sync's copy, so it has to be cleared too
        # or the accepted invite would come back on the next launch.
        if (self.state.get("invites") or {}).pop(room_id, None) is not None:
            self._persist_recency()
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
            timeout = aiohttp.ClientTimeout(total=15)
            async with aiohttp.ClientSession(timeout=timeout) as session:
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
        # Heal the persisted snapshot too: the dashboard's favourite rescue
        # (_all_entries) trusts the snapshot whenever room.tags is empty, and
        # an empty dict is exactly what un-favouriting leaves behind, so a
        # stale True there would pin the room in Favourites forever.
        snap = (self.state.get("room_meta") or {}).get(room_id)
        if ok and isinstance(snap, dict) and bool(snap.get("is_favourite")) != favourite:
            snap["is_favourite"] = favourite
            self._persist_recency()
        return ok

    def _on_tags(self, room, event) -> None:
        """Room tag account data synced (this or another client changed the
        tags): mirror m.favourite into the persisted snapshot, so the
        favourite rescue in _all_entries cannot resurrect a tag the server
        no longer has. Must stay a plain function: nio's room-account-data
        dispatch asyncio.run()s any coroutine a callback returns, which
        blows up under the already-running loop."""
        snap = (self.state.get("room_meta") or {}).get(room.room_id)
        fav = "m.favourite" in (getattr(event, "tags", None) or {})
        if isinstance(snap, dict) and bool(snap.get("is_favourite")) != fav:
            snap["is_favourite"] = fav
            self._persist_recency()

    def find_room(self, ref: str) -> Entry | None:
        """Look up a joined room by exact room id or canonical alias, for the
        config's optional ``room =`` open-on-launch setting."""
        for room_id, room in self.client.rooms.items():
            if ref in (room_id, getattr(room, "canonical_alias", None)):
                return self._entry(room)
        # Rooms a resumed launch has not synced live yet (aliases are not in
        # the snapshot, so only an exact room id can match here).
        return next(
            (e for e in self._all_entries() if e.room_id == ref), None
        )

    def search(self, query: str, scope: str | None = None) -> list[Entry]:
        """Match people and rooms by title, person, or room id. ``scope``
        narrows the candidates to one dashboard section, "favourites" or
        "recent", covering the WHOLE section (the dashboard shows each
        truncated to its row budget). A scoped search with an empty query
        lists the full section, so "/" on Favourites is also the way to see
        every favourite; the global search keeps returning nothing until
        something is typed."""
        q = fold_text(query.strip())
        opened = self.state["last_opened_ts"]
        if scope is None and not q:
            # Hoisted above the rebuild: this runs on every keystroke in the
            # search box, and _all_entries() is O(rooms) with a state.json
            # write whenever an unread count moved.
            return []
        entries = self._all_entries()
        if scope == "favourites":
            entries = [e for e in entries if e.is_favourite and not e.is_space]
        elif scope == "recent":
            entries = [e for e in entries if not e.is_space and opened.get(e.room_id)]
        elif not q:
            return []
        out = []
        for e in entries:
            haystack = fold_text(f"{e.title} {e.person or ''} {e.room_id}")
            if not q or q in haystack:
                out.append(e)
        # A scoped list keeps its section's own order, so it reads as the
        # full version of what the dashboard shows truncated.
        if scope == "recent":
            out.sort(key=lambda e: -opened[e.room_id])
        elif scope == "favourites":
            out.sort(
                key=lambda e: (
                    e.unread == 0,
                    -max(opened.get(e.room_id, 0), e.last_ts),
                    e.title.lower(),
                )
            )
        else:
            out.sort(key=lambda e: (e.unread == 0, -e.last_ts, e.title.lower()))
        return out

    def message_index(self) -> list[tuple[Entry, Message]]:
        """(room entry, message) for every message held locally in any
        joined room: the full archives merged with the live windows, edits
        folded. Feeds the dashboard's global search, so a query can find a
        message without knowing which room it is in. Rooms that no longer
        resolve to an entry (left since their cache was written) stay out."""
        entries = {
            e.room_id: e for e in self._all_entries() if not e.is_space
        }
        by_room: dict[str, dict[str, Message]] = {}
        for room_id, arch in self.archives.items():
            if room_id in entries:
                by_room.setdefault(room_id, {}).update(arch)
        for room_id, timeline in self.timelines.items():
            if room_id not in entries:
                continue
            rows = by_room.setdefault(room_id, {})
            for m in timeline:
                if m.event_id and not m.pending:
                    rows[m.event_id] = m
        out: list[tuple[Entry, Message]] = []
        for room_id, rows in by_room.items():
            entry = entries[room_id]
            for m in fold_edits(
                sorted(rows.values(), key=lambda m: (m.ts, m.event_id))
            ):
                out.append((entry, m))
        return out

    # --- room view --------------------------------------------------------

    def _to_message(self, room, event, follow_bundle: bool = True) -> Message | None:
        """A fetched nio event as a Message, or None for non-message events.
        A /messages chunk arrives with decryptable events already decrypted
        by nio (and their wrapper's unsigned grafted back on, see
        _new_client); events from the raw /relations fetch are still
        encrypted and decrypted here. Ones we lack keys for become a
        placeholder rather than being dropped."""
        if isinstance(event, RedactedEvent):
            # A reaction we noted live whose redaction only ever reaches us
            # as this tombstone (the sync that carried the redaction was
            # skipped as gappy): subtract it now, or the badge stays one too
            # high for the rest of the session. _on_redaction does the same
            # subtraction when the redaction itself arrives.
            noted = self._reaction_events.pop(event.event_id or "", None)
            if noted is not None:
                noted_room, target, key, sender = noted
                self._drop_reaction_vote(noted_room, target, key, sender)
                return None
            # Only a deleted *message* gets a tombstone. Removing a reaction
            # redacts an event too, and the timeline never showed that one, so
            # marking it would invent a deletion the user cannot have seen.
            if event.type not in ("m.room.message", "m.room.encrypted"):
                return None
            # Same for a deleted edit: it retracts the rewrite, and the edit
            # never rendered as its own row (recognizable where the room
            # version preserves m.relates_to through redaction).
            if _edit_target(event):
                return None
            # Deletion strips the content server-side: nothing but who did it
            # and when survives the fetch. Any text we received before that
            # lives on in the timeline cache, which load_history's merge keeps.
            because = (
                (getattr(event, "source", {}) or {}).get("unsigned") or {}
            ).get("redacted_because") or {}
            return Message(
                sender=event.sender,
                sender_name=_clean(
                    (room.user_name(event.sender) if room else event.sender)
                    or event.sender
                ),
                body="",
                ts=event.server_timestamp,
                event_id=event.event_id,
                redacted_ts=because.get("origin_server_ts") or event.server_timestamp,
            )
        decrypt_error: Exception | None = None
        # Thread info is read before any decryption below: an encrypted
        # wrapper carries m.relates_to in cleartext plus the server's
        # unsigned m.relations aggregation, and nio's decrypt drops unsigned
        # (already-decrypted /messages events had theirs grafted back, see
        # _new_client).
        thread_root, thread_count = _thread_info(event)
        replaces = _edit_target(event)
        # The server aggregates edits onto the event they rewrite and bundles
        # the most recent one, in full, under unsigned. Using it means an old
        # message shows its current text even when the edit event itself is far
        # outside the fetched history window.
        unsigned = (getattr(event, "source", {}) or {}).get("unsigned") or {}
        relations = unsigned.get("m.relations") or {}
        bundled = relations.get("m.replace") if follow_bundle else None
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
        if isinstance(event, ReactionEvent):
            # Arrived plaintext or just decrypted from the Megolm wrapper:
            # record it and drop it. Reactions render as a badge on their
            # target, never as their own row. The room id comes from the
            # event itself when the rooms map is not populated yet.
            self._note_reaction(
                _event_room_id(room, event),
                event.event_id,
                event.sender,
                event.reacts_to,
                event.key,
            )
            return None
        if isinstance(event, RoomMessage):
            name = room.user_name(event.sender) if room else event.sender
            body = getattr(event, "body", None)
            if not body:
                src = getattr(event, "source", {}) or {}
                body = src.get("content", {}).get("body")
            if not body:
                body = f"[{type(event).__name__}]"
        elif isinstance(event, MegolmEvent):
            # An encrypted reaction we hold no key for is still fully legible:
            # the spec requires the whole m.relates_to (target AND key) in the
            # cleartext wrapper so servers can aggregate. Record it instead of
            # rendering a "[could not decrypt]" row for a mere thumbs-up.
            relates = (
                (getattr(event, "source", {}) or {}).get("content", {}) or {}
            ).get("m.relates_to") or {}
            if isinstance(relates, dict) and relates.get("rel_type") == "m.annotation":
                self._note_reaction(
                    _event_room_id(room, event),
                    event.event_id,
                    event.sender,
                    relates.get("event_id") or "",
                    relates.get("key") or "",
                )
                return None
            name = room.user_name(event.sender) if room else event.sender
            if decrypt_error is not None:
                body = f"[encrypted: could not decrypt ({_clean(str(decrypt_error))})]"
            else:
                body = "[encrypted: no key for this message]"
        else:
            return None
        if replaces:
            body = _edit_body(event, body)
        reply_to = "" if replaces else _reply_target(event)
        reply_name = reply_snippet = ""
        if reply_to:
            body, quoted_sender, reply_snippet = _strip_reply_fallback(body)
            if quoted_sender:
                reply_name = (
                    room.user_name(quoted_sender) if room else None
                ) or quoted_sender
        body = _clean(body)
        original_body = ""
        edited_ts = 0
        if isinstance(bundled, dict) and bundled.get("event_id"):
            newest = self._to_message(
                room, Event.parse_event(bundled), follow_bundle=False
            )
            if (
                newest is not None
                and newest.replaces == event.event_id
                and newest.sender == event.sender
            ):
                edited_ts = newest.ts
                # An edit we hold no key for must not displace text we can
                # read; the marker still says a newer version exists.
                if not newest.body.startswith(UNDECRYPTABLE):
                    original_body, body = body, newest.body
        return Message(
            sender=event.sender,
            sender_name=_clean(name or event.sender),
            body=body,
            ts=event.server_timestamp,
            event_id=event.event_id,
            thread_root=thread_root,
            thread_count=thread_count,
            reply_to=reply_to,
            reply_name=_clean(reply_name),
            reply_snippet=_clean(reply_snippet),
            replaces=replaces,
            edited_ts=edited_ts,
            original_body=original_body,
            mentions_me=self._mentions_me(event, body),
            **_media_info(event),
        )

    async def load_history(
        self,
        room_id: str,
        limit: int = HISTORY_LIMIT,
        cached_only: bool = False,
    ) -> list[Message]:
        """The most recent window of a room's timeline, oldest first. Serves
        the in-memory cache when it can answer (or when cached_only asks for
        an instant, possibly shallow, list to paint before the network round
        trip); otherwise fetches /messages windows, paginating deeper while
        the result is starved of main-timeline messages, and merges them in."""
        cached = list(self.timelines.get(room_id, []))
        # Startup syncs members lazily, and a resumed launch may not know the
        # room at all (nio's joined_members handler silently drops responses
        # for unknown rooms). Register the room and start the member fetch in
        # the background; awaiting it would stall every first open. Runs
        # before the cache early-return so a reopen retries a failed fetch.
        self._ensure_room(room_id)
        room = self.client.rooms[room_id]
        if not room.members_synced:
            self._fetch_members(room_id)
        # Serving a big cache without a fetch is only safe while no gappy
        # sync punched a hole in it; after one, "len >= limit" would return
        # history with the skipped chunk silently missing. history_loaded is
        # re-added below only if no new gap appeared during the fetch.
        gen = self.gap_gen.get(room_id, 0)
        # Count the window in MAIN-timeline messages, not raw events:
        # reactions and redactions never become Messages, thread replies
        # collapse out of the normal view, and a burst of either can fill a
        # whole raw window.
        floor = limit // 2

        def window(msgs: list[Message]) -> list[Message]:
            main = 0
            for i in range(len(msgs) - 1, -1, -1):
                if not msgs[i].thread_root:
                    main += 1
                    if main >= floor:
                        return msgs[max(0, min(i, len(msgs) - limit)):]
            return msgs

        cache_ok = (
            gen == 0
            and len(cached) >= limit
            and sum(1 for m in cached if not m.thread_root) >= floor
        )
        if cached_only or cache_ok or room_id in self.history_loaded:
            return window(cached)
        fetched: list[Message] = []
        start = self.client.next_batch or ""
        deepest = None
        exhausted = False
        # Paginate until the fetch holds enough main-timeline messages, the
        # room's history runs out, or TIMELINE_CAP raw events have been
        # walked (the cache would not keep more anyway). The common quiet
        # room still costs exactly one round trip.
        for _ in range(max(1, TIMELINE_CAP // limit)):
            try:
                resp = await self.client.room_messages(
                    room_id,
                    start=start,
                    direction=MessageDirection.back,
                    limit=limit,
                )
            except (aiohttp.ClientError, asyncio.TimeoutError, OSError):
                # Offline raises (max_timeouts) rather than retrying forever
                # inside nio; degrade exactly like an error response.
                resp = None
            if resp is None or isinstance(resp, RoomMessagesError):
                if deepest is None:
                    return window(cached)
                break  # keep the windows that did arrive
            for event in resp.chunk:
                msg = self._to_message(room, event)
                if msg is not None:
                    fetched.append(msg)
            end = getattr(resp, "end", None)
            if end:
                deepest = end
            if not end or not resp.chunk:
                exhausted = True
                break
            if sum(1 for m in fetched if not m.thread_root) >= floor:
                break
            start = end
        # Seed the back-pagination position from the deepest window consumed,
        # but never overwrite a token load_older has already advanced deeper.
        if deepest:
            self.pagination_tokens.setdefault(room_id, deepest)
        if exhausted:
            self.pagination_done[room_id] = True
        fetched.reverse()  # chunks come newest-first when paginating back
        # Merge on event id (timestamps collide for messages sent in the same
        # millisecond); cached entries win because they may already be decrypted.
        # Re-read the cache: a sync callback may have appended during the fetch.
        cached = list(self.timelines.get(room_id, []))
        merged: dict[str, Message] = {}
        for m in fetched:
            merged[m.event_id or f"ts:{m.ts}:{m.sender}"] = m
        for m in cached:
            key = m.event_id or f"ts:{m.ts}:{m.sender}"
            server = merged.get(key)
            if (
                server is not None
                and m.body.startswith(UNDECRYPTABLE)
                and not server.body.startswith(UNDECRYPTABLE)
            ):
                # Keys arrived since this placeholder was cached (live
                # key-share, not --import-keys, which clears the cache): the
                # fresh fetch decrypted what the cached copy could not, so
                # for once the server copy wins outright.
                continue
            if server is not None and server.redacted_ts and not m.redacted_ts:
                # The server says this one was deleted while we were away.
                # Keep the text we already hold (nobody can fetch it again)
                # but mark it, so the timeline shows the deletion.
                m = replace(m, redacted_ts=server.redacted_ts)
            if server is not None and server.ts != m.ts:
                # Cached wins on content (it may be decrypted), but the
                # server's origin_server_ts is authoritative: a send() echo
                # carries this machine's wall clock, and a skewed clock would
                # misplace our own messages in the ts-sorted timeline.
                m = replace(m, ts=server.ts)
            merged[key] = m
        history = sorted(merged.values(), key=lambda m: m.ts)
        # Write the merged history back into the in-memory timeline so the next
        # open (and the live-refresh path) does not refetch and re-decrypt the
        # same events from the server. The deque caps it at TIMELINE_CAP.
        timeline = self.timelines[room_id]
        timeline.clear()
        timeline.extend(history)
        # Freshly fetched (and possibly freshly decrypted) content is worth
        # having on disk; still debounced, a burst of first opens batches up.
        self._cache_dirty = True
        self._maybe_save_timelines()
        # A limited sync that landed while the fetch above was in flight
        # discarded history_loaded for a reason: the window just merged was
        # anchored at the pre-gap token and cannot contain the skipped
        # events. Re-adding the mark would declare the hole filled, so leave
        # it off and let the next refresh refetch.
        if self.gap_gen.get(room_id, 0) == gen:
            self.history_loaded.add(room_id)
        return window(history)

    def _fetch_members(self, room_id: str) -> None:
        """Fetch a room's full member list without blocking the caller.
        Launch syncs members lazily (see initial_sync), so the map is filled
        on first open instead; that fetch takes seconds on a big room and
        must not hold up load_history's return. When it lands, sender names
        in the cached timeline are re-resolved (they were built while the
        member map was incomplete) and on_members_loaded tells the UI to
        repaint them."""
        if room_id in self._member_fetches:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return  # no event loop yet; a later caller retries

        async def fetch() -> None:
            try:
                await self.client.joined_members(room_id)
            except Exception:
                return  # only display names lost; the next open retries
            finally:
                self._member_fetches.pop(room_id, None)
            room = self.client.rooms.get(room_id)
            # An error response leaves members_synced False; nothing to do.
            if room is None or not room.members_synced:
                return
            timeline = self.timelines.get(room_id)
            changed = False
            for i, m in enumerate(timeline or ()):
                name = _clean(room.user_name(m.sender) or m.sender) or m.sender
                if name != m.sender_name:
                    timeline[i] = replace(m, sender_name=name)
                    changed = True
            if changed and self.on_members_loaded is not None:
                self.on_members_loaded(room_id)
            # The full member map also feeds the dashboard's DM detection
            # (see _entry), so a home repaint is due even when no cached
            # timeline names changed.
            if self.on_names_loaded is not None:
                self.on_names_loaded()

        self._member_fetches[room_id] = loop.create_task(fetch())

    def _fetch_profile(self, user_id: str) -> None:
        """Fetch one user's display name via /profile without blocking the
        caller. Reached from dashboard builds for DM peers whose member event
        never arrived this session (see _entry). When it lands, any snapshot
        title still holding the bare user id is healed so future launches
        start with the real name, and on_names_loaded repaints the
        dashboard."""
        if user_id in self.profile_names or user_id in self._profile_fetches:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return  # no event loop yet; the next dashboard build retries

        async def fetch() -> None:
            try:
                resp = await self.client.get_displayname(user_id)
            except Exception:
                return  # transport error: leave uncached so a rebuild retries
            finally:
                self._profile_fetches.pop(user_id, None)
            # An error *response* (unknown user, disabled profiles) is cached
            # as "" so the fetch is not repeated on every dashboard rebuild.
            name = _clean(getattr(resp, "displayname", None) or "")
            self.profile_names[user_id] = name
            if not name:
                return
            changed = False
            for m in self.state.get("room_meta", {}).values():
                if isinstance(m, dict) and m.get("person") == user_id and m.get("title") == user_id:
                    m["title"] = name
                    changed = True
            if changed:
                self._persist_recency()
            if self.on_names_loaded is not None:
                self.on_names_loaded()

        self._profile_fetches[user_id] = loop.create_task(fetch())

    def _fetch_room_name(self, room_id: str) -> None:
        """Fetch a room's m.room.name state without blocking the caller.
        Reached from dashboard builds for live rooms that came up through a
        resumed sync with no name state (see _all_entries). On success the
        name is grafted onto the nio room (so every later title computation
        sees it), the room_meta snapshot is healed so future launches start
        right, and on_names_loaded repaints the dashboard. The task entry is
        kept after completion: one fetch per room per session, a room found
        genuinely unnamed (404) included."""
        if room_id in self._name_fetches:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return  # no event loop yet; the next dashboard build retries

        async def fetch() -> None:
            try:
                resp = await self.client.room_get_state_event(
                    room_id, "m.room.name"
                )
            except Exception:
                # Transport error: drop the guard so a later rebuild retries.
                self._name_fetches.pop(room_id, None)
                return
            content = getattr(resp, "content", None)
            name = _clean((content or {}).get("name") or "")
            if not name:
                return  # genuinely unnamed (or error response); nothing to fix
            room = self.client.rooms.get(room_id)
            if room is not None:
                room.name = name
            meta = self.state.get("room_meta", {}).get(room_id)
            if isinstance(meta, dict) and (
                meta.get("title") != name or meta.get("person")
            ):
                # A named room is a group room; clear any person a session
                # with a lazily-loaded member map misdetected onto it.
                meta["title"] = name
                meta["person"] = ""
                self._persist_recency()
            if self.on_names_loaded is not None:
                self.on_names_loaded()

        self._name_fetches[room_id] = loop.create_task(fetch())

    def reset_pagination(self, room_id: str) -> None:
        """Forget the back-pagination position for a room. Called when a room
        screen opens: the messages fetched by load_older live only on the
        screen and die with it, but the token would survive here, and a reopen
        that resumed from it would silently skip everything between the fresh
        visible window and the previous visit's depth."""
        self.pagination_tokens.pop(room_id, None)
        self.pagination_done.pop(room_id, None)
        self._archive_served.pop(room_id, None)

    async def load_older(self, room_id: str, limit: int = HISTORY_LIMIT) -> list[Message] | None:
        """The next batch of history older than what has been fetched so far,
        oldest first. Returns [] once the room's very first event has been
        reached, and None on a failed request: the two must stay distinct, or
        one transient network error would masquerade as "beginning of history"
        and stop back-pagination for the rest of the visit. The position is
        tracked per room; the first call continues from where load_history's
        initial window ended, so repeated calls walk arbitrarily far back."""
        if self.pagination_done.get(room_id):
            return []
        # Serve scroll-up from the local archive first: deep history becomes
        # instant and offline. The position counts from the newest end, so
        # the backfill worker prepending older rows never shifts what was
        # served. A batch straddling a stale room's head hole can miss
        # events; opening the room already started the walk that fills it.
        arch = self.archives.get(room_id)
        if arch:
            rows = sorted(arch.values(), key=lambda m: (m.ts, m.event_id))
            served = self._archive_served.get(room_id, 0)
            end_idx = len(rows) - served
            if end_idx > 0:
                batch = rows[max(0, end_idx - limit):end_idx]
                self._archive_served[room_id] = served + len(batch)
                return batch
            if room_id in self.archive_done:
                self.pagination_done[room_id] = True
                return []
            # Partial archive walked dry: continue over the wire from where
            # the download stopped, so nothing between is skipped.
            token = self.archive_tokens.get(room_id)
            if token:
                self.pagination_tokens.setdefault(room_id, token)
        room = self.client.rooms.get(room_id)
        start = self.pagination_tokens.get(room_id) or self.client.next_batch or ""
        try:
            resp = await self.client.room_messages(
                room_id,
                start=start,
                direction=MessageDirection.back,
                limit=limit,
            )
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError):
            # A dead network raises (max_timeouts); same contract as an error
            # response, and distinct from [] so it never reads as "beginning
            # of history".
            return None
        if isinstance(resp, RoomMessagesError):
            return None
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

    def archive_rows(self, room_id: str) -> list[Message]:
        """A room's archived history, oldest first, as a snapshot copy: the
        backfill worker keeps mutating the live dict, and the room screen's
        browse mode ("g") needs stable indices while it walks forward."""
        arch = self.archives.get(room_id)
        if not arch:
            return []
        return sorted(arch.values(), key=lambda m: (m.ts, m.event_id))

    def start_backfill(self, room_id: str) -> None:
        """Kick off (or resume) the full-history download for a room, called
        whenever a room screen opens. Idempotent: a room already downloading,
        or fully archived and not stale, is a no-op. The task reference is
        held (asyncio keeps only weak ones) and errors are swallowed: an
        aborted download resumes from its persisted token on the next open."""
        if not self.cache_allowed(room_id):
            return  # caching is off globally or for this room's space
        if room_id in self._backfill_tasks:
            return
        if room_id in self.archive_done and room_id not in self.archive_stale:
            return

        def reap(task: asyncio.Task) -> None:
            self._backfill_tasks.pop(room_id, None)
            if self.backfill_active == room_id:
                self.backfill_active = None
            if not task.cancelled():
                task.exception()  # retrieve it, or asyncio logs a warning

        task = asyncio.ensure_future(self._backfill(room_id))
        self._backfill_tasks[room_id] = task
        task.add_done_callback(reap)

    def backfill_all(self) -> int:
        """Kick off the full-history download for every joined room at once
        (the same background walk opening a room starts), so the local
        caches end up holding everything without visiting each room by
        hand. The shared gate still serializes the walks: a loaded
        homeserver sees one at a time. Returns how many rooms actually
        needed downloading, so the caller can word its notification."""
        pending = 0
        for e in self._all_entries():
            if e.is_space or e.is_invite:
                continue
            if not self.cache_allowed(e.room_id):
                continue
            if (
                e.room_id in self.archive_done
                and e.room_id not in self.archive_stale
            ):
                continue
            pending += 1
            self.start_backfill(e.room_id)
        return pending

    def backfill_remaining(self) -> int:
        """Rooms with a history walk still queued or running, so the
        sync-all popup can show progress."""
        return len(self._backfill_tasks)

    def cancel_backfills(self) -> None:
        """Stop every queued or running history walk (Escape in the
        sync-all popup). Nothing is lost: each room resumes from its
        persisted token the next time it opens or the next full sync."""
        for task in list(self._backfill_tasks.values()):
            task.cancel()

    def room_title(self, room_id: str) -> str:
        """A room's display name as the dashboard would show it."""
        room = self.client.rooms.get(room_id)
        name = _clean(getattr(room, "display_name", ""))
        if not name or room_id in self._placeholder_rooms:
            # A bare MatrixRoom has no state, so nio names it "Empty Room";
            # the full-sync popup would label every room it downloads that.
            snap = (self.state.get("room_meta") or {}).get(room_id)
            if isinstance(snap, dict) and snap.get("title"):
                return _clean(snap["title"]) or name or room_id
        return name or room_id

    async def _backfill(self, room_id: str) -> None:
        """Walk /messages backwards until the room's first event is reached,
        filling the archive. Everything _to_message yields is kept: normal
        messages, edit rows, redaction tombstones; reactions are recorded as
        a side effect exactly like the interactive fetch paths.

        A stale room walks from the current head instead of resuming from
        its depth token, to re-cover a possible hole at the new end; once the
        walk hits a chunk whose messages are all already archived, the room
        below that point is known contiguous. An already-done room stops
        there; a partial one jumps back to the depth its earlier walk reached
        instead of re-descending history it already holds."""
        async with self._backfill_gate:
            self.backfill_active = room_id
            arch = self.archives.setdefault(room_id, {})
            stale = room_id in self.archive_stale
            recover = stale and room_id in self.archive_done
            # The depth a partial archive was last walked to, held back until
            # the hole at the new end is covered: with it in hand the walk can
            # skip straight past the region it already has, and an interrupted
            # recovery still finds the deep token where it left it.
            deep = "" if recover or not stale else (
                self.archive_tokens.get(room_id) or ""
            )
            start = "" if stale else (self.archive_tokens.get(room_id) or "")
            if not start:
                start = self.client.next_batch or self.client.loaded_sync_token or ""
            self._ensure_room(room_id)
            room = self.client.rooms[room_id]
            while True:
                try:
                    resp = await self.client.room_messages(
                        room_id,
                        start=start,
                        direction=MessageDirection.back,
                        limit=100,
                    )
                except (aiohttp.ClientError, asyncio.TimeoutError, OSError):
                    return  # resumes from the persisted token next open
                if isinstance(resp, RoomMessagesError):
                    return
                chunk = resp.chunk or []
                seen = fresh = 0
                for event in chunk:
                    msg = self._to_message(room, event)
                    if msg is None or not msg.event_id:
                        continue
                    seen += 1
                    if msg.event_id not in arch:
                        fresh += 1
                        # setdefault semantics on purpose: an archived copy
                        # may hold decrypted text or a redaction body that a
                        # refetch cannot reproduce; never overwrite it here.
                        arch[msg.event_id] = msg
                self._cache_dirty = True
                if fresh:
                    self._archive_dirty.add(room_id)
                end = getattr(resp, "end", None)
                # A done room's token is meaningless (there is nothing below
                # to resume from), and a walk covering a hole above a partial
                # archive must not shrink that archive's resume depth either.
                if end and not recover and not deep:
                    self.archive_tokens[room_id] = end
                    self.archive_stale.discard(room_id)
                if not end or not chunk:
                    self.archive_done.add(room_id)
                    self.archive_stale.discard(room_id)
                    self._maybe_save_timelines()
                    return
                if seen and not fresh:
                    # Reached already-archived territory: the hole above is
                    # covered and everything below it was contiguous already.
                    if recover:
                        self.archive_stale.discard(room_id)
                        self._maybe_save_timelines()
                        return
                    if deep:
                        # Same, except this archive never reached the room's
                        # start: carry on from its old depth, not from here.
                        self.archive_stale.discard(room_id)
                        start, deep = deep, ""
                        self._maybe_save_timelines()
                        await asyncio.sleep(0.5)
                        continue
                start = end
                self._maybe_save_timelines()
                await asyncio.sleep(0.5)  # be gentle with a loaded homeserver

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
            timeout = aiohttp.ClientTimeout(total=15)
            async with aiohttp.ClientSession(timeout=timeout) as http:
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
        cached = list(self.timelines.get(room_id, []))
        # The caller passes the root Message it captured when the screen
        # opened; a deletion arriving since then only flagged the CACHED copy
        # (_on_redaction), and the merge below skips the root. Adopt the
        # flag, or the thread keeps rendering the deleted text while the
        # room view underneath shows the tombstone.
        cached_root = next(
            (m for m in cached if m.event_id == root.event_id), None
        )
        if (
            cached_root is not None
            and cached_root.redacted_ts
            and not root.redacted_ts
        ):
            root = replace(root, redacted_ts=cached_root.redacted_ts)
        for m in cached:
            if m.thread_root != root.event_id or not m.event_id:
                continue
            server = merged.get(m.event_id)
            if (
                server is not None
                and m.body.startswith(UNDECRYPTABLE)
                and not server.body.startswith(UNDECRYPTABLE)
            ):
                # Same rule as load_history's merge: a placeholder cached
                # before the keys arrived must not displace the copy this
                # fetch just decrypted.
                continue
            if server is not None:
                # Cached wins (it may be decrypted), but the server copy can
                # know things the cache does not: an edit it bundled and
                # _to_message folded in, or a deletion we were away for.
                # Without this the thread view would show a reply's pre-edit
                # text while the main timeline shows the current one.
                if server.edited_ts > m.edited_ts and not server.body.startswith(
                    UNDECRYPTABLE
                ):
                    m = replace(
                        m,
                        body=server.body,
                        original_body=server.original_body or m.body,
                        edited_ts=server.edited_ts,
                    )
                if server.redacted_ts and not m.redacted_ts:
                    m = replace(m, redacted_ts=server.redacted_ts)
            merged[m.event_id] = m
        # A live edit of a thread reply sits in the cache with no thread root
        # of its own (its relation is m.replace, not m.thread), and the
        # /relations m.thread fetch never returns it either; splice those in
        # so the screen's fold_edits can apply them.
        for m in cached:
            if (
                m.event_id
                and m.event_id not in merged
                and m.replaces
                and (m.replaces in merged or m.replaces == root.event_id)
            ):
                merged[m.event_id] = m
        replies = sorted(merged.values(), key=lambda m: m.ts)
        return [root] + replies

    async def load_edits(self, room_id: str, message: Message) -> list[Message]:
        """Every version of a message, oldest first: the text as first sent,
        then one entry per edit. Fetched from the same /relations endpoint as
        threads, so versions older than the loaded history window are included;
        falls back to the two versions folded into the message itself when the
        request fails."""
        room = self.client.rooms.get(room_id)
        url = (
            f"{self.cfg.homeserver}/_matrix/client/v1/rooms/"
            f"{quote(room_id, safe='')}"
            f"/relations/{quote(message.event_id, safe='')}/m.replace"
            f"?dir=b&limit=50"
        )
        headers = {"Authorization": f"Bearer {self.client.access_token}"}
        chunk: list = []
        # A deleted message has no server-side content left to ask about, so
        # what we still hold locally is the whole history there is.
        if not message.redacted_ts:
            try:
                # Bounded: this backs a popup the user is waiting in front of.
                timeout = aiohttp.ClientTimeout(total=10)
                async with aiohttp.ClientSession(timeout=timeout) as http:
                    async with http.get(
                        url, headers=headers, allow_redirects=False
                    ) as r:
                        if r.status == 200:
                            data = await r.json()
                            chunk = data.get("chunk", []) or []
            except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
                pass

        versions: dict[str, Message] = {}
        for source in chunk:
            edit = self._to_message(room, Event.parse_event(source))
            # Only replacements of this message, and only from its own sender:
            # anyone in the room may send an m.replace pointing at it, but the
            # server ignores the ones that are not the sender's own.
            if (
                edit is not None
                and edit.replaces == message.event_id
                and edit.sender == message.sender
            ):
                versions[edit.event_id] = edit
        edits = sorted(versions.values(), key=lambda m: m.ts)
        if not edits and message.edited_ts:
            edits = [replace(message, ts=message.edited_ts)]
        original = replace(
            message, body=message.original_body or message.body, edited_ts=0
        )
        return [original] + edits

    def _ensure_room(self, room_id: str) -> None:
        """Sending under encryption makes nio look the room up in its
        in-memory map, which a resumed (incremental) launch only fills for
        rooms with fresh activity; a quiet room would raise instead of
        sending. Register a bare MatrixRoom for it: the encrypted flag comes
        from nio's own persisted encrypted-rooms set (so an encrypted room
        stays encrypted even before any sync mentions it), and nio fetches
        the member list itself before the first encrypted send."""
        if room_id not in self.client.rooms:
            self.client.rooms[room_id] = MatrixRoom(
                room_id,
                self.cfg.user_id,
                room_id in getattr(self.client, "encrypted_rooms", set()),
            )
            self._placeholder_rooms.add(room_id)

    async def send(
        self,
        room_id: str,
        text: str,
        reply_to: str | None = None,
        thread_root: str | None = None,
        thread_latest: str | None = None,
    ) -> tuple[bool, str]:
        self._ensure_room(room_id)
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
                        reply_to=reply_to or "",
                    )
                )
            self.last_event_id[room_id] = resp.event_id
            return True, resp.event_id
        return False, getattr(resp, "message", "send failed")

    async def send_edit(
        self, room_id: str, target: Message, text: str
    ) -> tuple[bool, str]:
        """Rewrite one of our own messages: an m.replace event with the new
        text in m.new_content and the "* " fallback body for clients that do
        not understand edits (the same wire format we fold when receiving)."""
        self._ensure_room(room_id)
        content = {
            "msgtype": "m.text",
            "body": f"* {text}",
            "m.new_content": {"msgtype": "m.text", "body": text},
            "m.relates_to": {
                "rel_type": "m.replace",
                "event_id": target.event_id,
            },
        }
        try:
            resp = await self.client.room_send(
                room_id,
                message_type="m.room.message",
                content=content,
                ignore_unverified_devices=self.cfg.allow_unverified,
            )
        except Exception as exc:
            return False, str(exc)
        if hasattr(resp, "event_id") and resp.event_id:
            # Cache the edit right away so the very next redraw folds it; the
            # sync echo later merges on the event id.
            self.timelines[room_id].append(
                Message(
                    sender=self.cfg.user_id,
                    sender_name=self.my_name,
                    body=text,
                    ts=int(time.time() * 1000),
                    event_id=resp.event_id,
                    replaces=target.event_id,
                )
            )
            self.last_event_id[room_id] = resp.event_id
            return True, resp.event_id
        return False, getattr(resp, "message", "edit failed")

    async def redact(self, room_id: str, event_id: str) -> tuple[bool, str]:
        """Delete one of our own messages. On success the cached copy is
        flagged immediately, exactly as if the redaction had arrived from the
        server (see _on_redaction)."""
        try:
            resp = await self.client.room_redact(room_id, event_id)
        except Exception as exc:
            return False, str(exc)
        if hasattr(resp, "event_id") and resp.event_id:
            now = int(time.time() * 1000)
            timeline = self.timelines.get(room_id)
            for i, m in enumerate(timeline or ()):
                if m.event_id == event_id:
                    timeline[i] = replace(m, redacted_ts=now)
                    break
            self._flag_archived_redaction(room_id, event_id, now)
            self.last_event_id[room_id] = resp.event_id
            return True, resp.event_id
        return False, getattr(resp, "message", "delete failed")

    def my_reaction(self, room_id: str, target: str, key: str) -> str | None:
        """Event id of our own still-standing reaction on this message with
        this key, or None."""
        key = _reaction_key(key)
        me = self.cfg.user_id
        for event_id, noted in self._reaction_events.items():
            if noted == (room_id, target, key, me):
                senders = (
                    self.reactions.get(room_id, {}).get(target, {}).get(key)
                )
                if senders and me in senders:
                    return event_id
        return None

    async def toggle_reaction(
        self, room_id: str, target: str, key: str
    ) -> tuple[bool, str, bool]:
        """React to a message with ``key``, or take the reaction back if we
        already sent that exact one (removal is a redaction of our own
        m.reaction event). Returns (ok, event id or error, added), added
        False when this call removed instead."""
        existing = self.my_reaction(room_id, target, key)
        if existing:
            try:
                resp = await self.client.room_redact(room_id, existing)
            except Exception as exc:
                return False, str(exc), False
            if hasattr(resp, "event_id") and resp.event_id:
                # Subtract locally right away, exactly as _on_redaction
                # would; popping the note makes the later sync echo a no-op.
                noted = self._reaction_events.pop(existing, None)
                if noted is not None:
                    _room, tgt, k, sender = noted
                    self._drop_reaction_vote(room_id, tgt, k, sender)
                self.last_event_id[room_id] = resp.event_id
                return True, resp.event_id, False
            return False, getattr(resp, "message", "removal failed"), False
        self._ensure_room(room_id)
        content = {
            "m.relates_to": {
                "rel_type": "m.annotation",
                "event_id": target,
                "key": key,
            }
        }
        try:
            resp = await self.client.room_send(
                room_id,
                message_type="m.reaction",
                content=content,
                ignore_unverified_devices=self.cfg.allow_unverified,
            )
        except Exception as exc:
            return False, str(exc), True
        if hasattr(resp, "event_id") and resp.event_id:
            # Note it right away so the badge shows on the very next redraw;
            # the sync echo lands on the same event id and merges cleanly.
            self._note_reaction(
                room_id, resp.event_id, self.cfg.user_id, target, key
            )
            self.last_event_id[room_id] = resp.event_id
            return True, resp.event_id, True
        return False, getattr(resp, "message", "reaction failed"), True

    async def fetch_media_bytes(self, message: Message) -> tuple[bool, bytes | str]:
        """The full attachment as bytes in memory, decrypted when it came from
        an encrypted room. Returns (True, bytes) or (False, error string).
        Shared by download_media (which writes them out) and the image preview
        (which renders them)."""
        if not message.media_url:
            return False, "not a file message"
        # Reject before downloading when the server-advertised size is already
        # over the cap (it is attacker-controlled, so it is only an early-out).
        if message.media_size and message.media_size > MAX_DOWNLOAD_BYTES:
            return False, f"file too large ({message.media_size} bytes)"
        try:
            # nio's download() documents that it ignores request_timeout and
            # passes 0 (no HTTP timeout) to aiohttp, so this wait_for is the
            # only bound; generous because media can be large.
            resp = await asyncio.wait_for(
                self.client.download(mxc=message.media_url), timeout=120
            )
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as exc:
            return False, f"download failed: {type(exc).__name__}"
        body = getattr(resp, "body", None)
        if not isinstance(body, bytes):
            detail = getattr(resp, "message", "") or getattr(
                resp, "status_code", ""
            ) or "download failed"
            return False, str(detail)
        if len(body) > MAX_DOWNLOAD_BYTES:
            return False, f"file too large ({len(body)} bytes)"
        if message.media_crypt:
            c = message.media_crypt
            try:
                body = decrypt_attachment(body, c["key"], c["sha256"], c["iv"])
            except Exception as exc:
                return False, f"could not decrypt attachment: {exc}"
        return True, body

    async def fetch_preview_bytes(
        self, message: Message, width: int, height: int, room_id: str = ""
    ) -> tuple[bool, bytes | str]:
        """Image bytes for an in-terminal preview, at roughly width x height
        pixels. Served from cache when possible: in-memory first, then the
        encrypted on-disk media cache; a fetched result is stored back in
        both (disk only when the room's caching is allowed). Unencrypted
        media asks the server's thumbnail endpoint first (a pre-scaled image
        instead of a possibly huge original); encrypted attachments have no
        server-side thumbnails, so those (and a failed thumbnail request)
        fall back to the full download."""
        cached = self._media_cache.get(message.media_url)
        if cached is None and message.media_url and self.cache_allowed(room_id):
            cached = self.cfg.load_media_cache(message.media_url)
        if cached is not None:
            self._media_cache[message.media_url] = cached
            while len(self._media_cache) > 32:
                self._media_cache.pop(next(iter(self._media_cache)))
            return True, cached
        ok, result = await self._fetch_preview_uncached(message, width, height)
        if ok and message.media_url:
            self._media_cache[message.media_url] = result
            while len(self._media_cache) > 32:
                self._media_cache.pop(next(iter(self._media_cache)))
            if self.cache_allowed(room_id):
                try:
                    self.cfg.save_media_cache(message.media_url, result)
                except OSError:
                    pass  # a full disk must not break the preview itself
        return ok, result

    async def _fetch_preview_uncached(
        self, message: Message, width: int, height: int
    ) -> tuple[bool, bytes | str]:
        if message.media_url and not message.media_crypt:
            # mxc://server/media_id, the two parts nio's thumbnail() wants.
            parts = message.media_url.removeprefix("mxc://").split("/", 1)
            if len(parts) == 2 and all(parts):
                try:
                    resp = await asyncio.wait_for(
                        self.client.thumbnail(
                            parts[0], parts[1], max(width, 64), max(height, 64)
                        ),
                        timeout=60,
                    )
                except (aiohttp.ClientError, asyncio.TimeoutError, OSError):
                    resp = None
                body = getattr(resp, "body", None)
                if isinstance(body, bytes) and 0 < len(body) <= MAX_DOWNLOAD_BYTES:
                    return True, body
        return await self.fetch_media_bytes(message)

    async def download_media(self, message: Message, directory) -> tuple[bool, str]:
        """Download an uploaded file into ``directory`` (a Path), decrypting
        it when it came from an encrypted room. Returns (ok, saved-path) or
        (False, error). The filename comes from the upload, sanitized to its
        basename and deduplicated so nothing is overwritten."""
        ok, body = await self.fetch_media_bytes(message)
        if not ok:
            return False, body
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
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(body)
        except OSError as exc:
            # Never leave a truncated file wearing the real name: it looks
            # like the download, and a retry would dedup around it into
            # "name (1)" while the corrupt copy stays.
            try:
                os.unlink(target)
            except OSError:
                pass
            return False, f"write failed: {exc}"
        return True, str(target)

    async def event_timestamp(self, room_id: str, event_id: str) -> int | None:
        """Server timestamp of a single event, or None when it cannot be
        fetched. Lets the unread divider place itself when the stored read
        marker is not a timeline row (a reaction or redaction id, or an edit
        written by another client): everything at or before this moment had
        been read."""
        try:
            resp = await self.client.room_get_event(room_id, event_id)
        except Exception:
            return None
        ts = getattr(getattr(resp, "event", None), "server_timestamp", None)
        return ts if isinstance(ts, int) else None

    async def fetch_fully_read(self, room_id: str) -> str | None:
        """The room's ``m.fully_read`` marker straight from the server, or
        None. Sync cannot be relied on to deliver it: room account data
        older than the resume token is not re-sent (and matrix.ioinformatics
        omits it from initial syncs too), so nio's room.fully_read_marker
        stays None until the marker changes mid-session. The result is
        written back onto the nio room so the next open skips the round
        trip."""
        url = (
            f"{self.cfg.homeserver}/_matrix/client/v3/user/"
            f"{quote(self.cfg.user_id, safe='')}/rooms/"
            f"{quote(room_id, safe='')}/account_data/m.fully_read"
        )
        headers = {"Authorization": f"Bearer {self.client.access_token}"}
        try:
            timeout = aiohttp.ClientTimeout(total=15)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(
                    url, headers=headers, allow_redirects=False
                ) as r:
                    if r.status != 200:
                        return None  # 404: the room was never marked read
                    data = await r.json()
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
            return None
        event_id = data.get("event_id") if isinstance(data, dict) else None
        if not isinstance(event_id, str) or not event_id:
            return None
        room = self.client.rooms.get(room_id)
        if room is not None:
            room.fully_read_marker = event_id
        return event_id

    def total_unread(self) -> int:
        """Unread notifications summed across every joined room (spaces
        carry none), for the terminal titlebar counter."""
        return sum(
            room.unread_notifications or 0
            for room in self.client.rooms.values()
        )

    def check_summary(self) -> dict:
        """The --check report for this session: rebuild the dashboard
        snapshot from the rooms this sync delivered, then summarise it."""
        self._all_entries()
        self.invites()
        return unread_summary(self.state)

    def get_setting(self, key: str, default=None):
        """One in-app setting (the settings screen's fields), from the
        "settings" dict in state.json."""
        return (self.state.get("settings") or {}).get(key, default)

    def set_setting(self, key: str, value) -> None:
        self.state.setdefault("settings", {})[key] = value
        self.cfg.save_state(self.state)

    async def set_display_name(self, name: str) -> bool:
        """PUT the account's global display name; True on success."""
        try:
            resp = await self.client.set_displayname(name)
        except Exception:
            return False
        return isinstance(resp, ProfileSetDisplayNameResponse)

    async def fetch_email_addresses(self) -> list[str] | None:
        """The account's email 3PIDs via GET /account/3pid (nio has no API
        for it). None means the fetch failed; [] means none are bound."""
        url = f"{self.cfg.homeserver}/_matrix/client/v3/account/3pid"
        headers = {"Authorization": f"Bearer {self.client.access_token}"}
        try:
            timeout = aiohttp.ClientTimeout(total=15)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(
                    url, headers=headers, allow_redirects=False
                ) as r:
                    if r.status != 200:
                        return None
                    data = await r.json()
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
            return None
        pids = data.get("threepids") if isinstance(data, dict) else None
        if not isinstance(pids, list):
            return None
        return [
            str(p["address"])
            for p in pids
            if isinstance(p, dict)
            and p.get("medium") == "email"
            and p.get("address")
        ]

    async def mark_read(self, room_id: str) -> None:
        """Move the room's read marker to its latest event. Called after
        every refresh of an open room, which at peak traffic is once per
        sync tick: the HTTP POST is debounced (immediately on the first
        call, then at most one trailing send per window, carrying whatever
        the latest event is when it fires), while the local badge state
        below always updates right away."""
        event_id = self.last_event_id.get(room_id)
        if event_id and self._marker_sent.get(room_id) != event_id:
            if room_id not in self._marker_tasks:
                last = self._marker_time.get(room_id)
                delay = (
                    0.0
                    if last is None
                    else MARK_READ_INTERVAL - (time.monotonic() - last)
                )
                # Hold the Task ref: asyncio keeps only weak references.
                self._marker_tasks[room_id] = asyncio.ensure_future(
                    self._send_read_marker(room_id, delay)
                )
        # Reading a room clears its badge in the persisted snapshot too; a
        # room quiet since the resume token is never re-delivered by sync, so
        # nothing else would ever zero the cached count.
        snap = (self.state.get("room_meta") or {}).get(room_id)
        if isinstance(snap, dict) and (snap.get("unread") or snap.get("highlights")):
            snap["unread"] = 0
            snap["highlights"] = 0
        self.record_opened(room_id)

    async def _send_read_marker(self, room_id: str, delay: float) -> None:
        try:
            if delay > 0:
                await asyncio.sleep(delay)
            # Re-read after the wait so a burst's trailing send carries the
            # newest event, not the one that scheduled it.
            event_id = self.last_event_id.get(room_id)
            if not event_id or self._marker_sent.get(room_id) == event_id:
                return
            # m.fully_read should name a MAIN-TIMELINE message: last_event_id
            # may be a reaction or redaction id, and thread replies collapse
            # out of the room view, so a marker at any of those is invisible
            # to the unread divider on the next open. Edits map to the
            # message they rewrite, unless that target is itself a thread
            # reply.
            rows = list(self.timelines.get(room_id) or ())
            by_id = {m.event_id: m for m in rows if m.event_id}
            fully_read = event_id
            for m in reversed(rows):
                if not m.event_id or m.pending:
                    continue
                if m.thread_root:
                    continue  # collapsed out of the room view
                target = by_id.get(m.replaces) if m.replaces else None
                if target is not None and target.thread_root:
                    continue  # an edit of a thread reply is just as invisible
                fully_read = m.replaces or m.event_id
                break
            try:
                await self.client.room_read_markers(
                    room_id, fully_read_event=fully_read, read_event=event_id
                )
            except Exception:
                return  # the next refresh retries; the marker is cosmetic
            self._marker_sent[room_id] = event_id
            self._marker_time[room_id] = time.monotonic()
        finally:
            self._marker_tasks.pop(room_id, None)

    def note_opening(self, room_id: str) -> None:
        """Everything the dashboard shows about a room changes the moment it
        is opened: recency to the top of Recent, badges to zero (the read
        receipt makes the server agree shortly after). Applying it eagerly,
        at open time, lets the home screen rebuild behind the covering room,
        so coming back finds nothing to repaint instead of reordering the
        list in front of the user."""
        room = self.client.rooms.get(room_id)
        if room is not None:
            room.unread_notifications = 0
            room.unread_highlights = 0
        snap = (self.state.get("room_meta") or {}).get(room_id)
        if isinstance(snap, dict) and (snap.get("unread") or snap.get("highlights")):
            snap["unread"] = 0
            snap["highlights"] = 0
        self.record_opened(room_id)

    def record_opened(self, room_id: str) -> None:
        self.state["last_opened_ts"][room_id] = int(time.time() * 1000)
        self._persist_recency()

    def _persist_recency(self) -> None:
        self.cfg.save_state(self.state)
