"""Textual front-end: a loading splash, a three-column home dashboard, a search
overlay, and a per-room read/send view.

The home screen shows three columns:

    Spaces             your spaces, with the selected space's rooms below,
                       then rooms that belong to no space at all
    Recent/Favourites  the 5 last-opened rooms, then rooms and DMs tagged
                       m.favourite (toggle with "f"); invites on top when any
    DMs                one entry per person, most-recently-active first

A background worker drives ``sync`` so unread counts, presence, and any open
room timeline stay live.
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import logging
import os
import re
import subprocess
import sys
import time
import unicodedata
from datetime import datetime
from io import BytesIO
from pathlib import Path
from uuid import uuid4
from zoneinfo import ZoneInfo

import keyring.errors

import textual.events
import textual.message
from textual import work
from textual.binding import Binding
from textual.color import Color as TextualColor
from textual.content import Content, Span as ContentSpan
from textual.style import Style as TextualStyle
from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen, Screen
from textual.strip import Strip
from textual.widgets import (
    Footer,
    Header,
    Input,
    Label,
    ListItem,
    ListView,
    Rule,
    Static,
    Switch,
    TextArea,
)

from rich.markup import escape
from rich.table import Table
from rich.text import Text

from nio import SyncResponse

from .client import (
    MIN_SECTION_ROWS,
    Entry,
    MatrixSession,
    Message,
    fold_edits,
    fold_text,
)
from .config import Config


# Colors that stay readable on a black background. Each participant gets one
# deterministically from their user id; my own name is orange.
MY_COLOR = "dark_orange"
SENDER_COLORS = [
    "deep_sky_blue1",
    "medium_purple",
    "spring_green2",
    "gold1",
    "hot_pink",
    "turquoise2",
    "cornflower_blue",
    "chartreuse2",
    "light_salmon1",
    "sky_blue2",
]


def _sender_color(user_id: str) -> str:
    return SENDER_COLORS[sum(ord(c) for c in user_id) % len(SENDER_COLORS)]


# Only http(s): the URL goes to the system's URL handler, and remote-sent
# schemes like file: or javascript: must not open on one keystroke. Brackets,
# quotes and whitespace end the match.
URL_RE = re.compile(r"https?://[^\s<>\"'`\[\]{}()]+", re.IGNORECASE)


def _find_urls(text: str) -> list[tuple[int, int, str]]:
    """(start, end, url) for every link in ``text``. Trailing sentence
    punctuation is stripped: "see https://x.y." ends the sentence, not the
    URL."""
    spans = []
    for match in URL_RE.finditer(text or ""):
        url = match.group(0).rstrip(".,;:!?")
        if url.endswith("//"):
            continue  # the strip ate the whole host, e.g. a bare "https://."
        spans.append((match.start(), match.start() + len(url), url))
    return spans


# Default quick-reaction row ("a" then a digit); the ones actually used
# bubble to the front (see ReactScreen).
DEFAULT_QUICK_REACTIONS = ["👍", "✅", "❤️", "😂", "🎉", "😮", "👀", "🙏", "➕"]

_EMOJI_NAMES: list[tuple[str, str]] | None = None


def _emoji_names() -> list[tuple[str, str]]:
    """(emoji, lowercase Unicode name) pairs for the reaction search, built
    once. The formal names are decent search keys and need no emoji-database
    dependency."""
    global _EMOJI_NAMES
    if _EMOJI_NAMES is None:
        pairs = []
        for start, stop in (
            (0x1F300, 0x1F5FF),
            (0x1F600, 0x1F64F),
            (0x1F680, 0x1F6FF),
            (0x1F900, 0x1F9FF),
            (0x1FA70, 0x1FAFF),
            (0x2600, 0x27BF),
            (0x2B00, 0x2BFF),
        ):
            for cp in range(start, stop + 1):
                name = unicodedata.name(chr(cp), "")
                if name:
                    pairs.append((chr(cp), name.lower()))
        _EMOJI_NAMES = pairs
    return _EMOJI_NAMES


def _version() -> str:
    try:
        from importlib.metadata import version

        return version("matrixcli")
    except Exception:
        return ""


# Timezone override for every rendered timestamp, set from the settings
# screen ("timezone" in state.json); None renders in the system's local time.
_display_tz: ZoneInfo | None = None


def _set_display_timezone(name: str) -> bool:
    """Point every rendered timestamp at an IANA zone (empty resets to the
    system's local time). False when the name is not a known zone."""
    global _display_tz
    if not name:
        _display_tz = None
        return True
    try:
        _display_tz = ZoneInfo(name)
    except Exception:
        return False
    return True


def _safe_localtime(ts_ms: int):
    """localtime of a server timestamp (in the configured display timezone,
    if any), or None when out of range: origin_server_ts comes off the wire,
    and one absurd value must not crash the render."""
    try:
        if _display_tz is not None:
            return datetime.fromtimestamp(ts_ms / 1000, _display_tz).timetuple()
        return time.localtime(ts_ms / 1000)
    except (OverflowError, OSError, ValueError):
        return None


def _fmt_time(ts: int) -> str:
    if not ts:
        return "     "
    lt = _safe_localtime(ts)
    return time.strftime("%H:%M", lt) if lt else "--:--"


def _fmt_size(size: int) -> str:
    if size <= 0:
        return ""
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return ""


# Fallback for uploads whose info block has no mimetype (optional in the
# spec).
IMAGE_EXTENSIONS = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".tiff", ".ico")


def _color_depth(color_system: str | None) -> str:
    """Preview capability tier: "truecolor", "256" (dithered blocks), or
    "basic" (16 colors or none: only the ASCII ramp is offered)."""
    if color_system in ("truecolor", "256"):
        return color_system
    return "basic"


def _is_image(m) -> bool:
    """True when the upload is worth offering the space-key preview for."""
    if not m.media_url or m.redacted_ts:
        return False
    if (m.media_mime or "").lower().startswith("image/"):
        return True
    name = (m.media_name or m.body or "").lower().strip()
    return name.endswith(IMAGE_EXTENSIONS)


def _ascii_art(img, max_w: int, max_h: int, ramp: str) -> Text:
    """The image as ASCII art fitted into max_w x max_h cells, one glyph per
    pixel picked from ``ramp`` by luminance. ramp[0] paints the brightest
    pixels (dense glyphs read bright on a dark terminal). Cells are about
    twice as tall as wide, so the grid is halved in height to keep the
    aspect."""
    from PIL.Image import Resampling

    gray = img.convert("L")
    scale = min(max_w / gray.width, 2 * max_h / gray.height)
    w = max(1, round(gray.width * scale))
    h = max(1, round(gray.height * scale / 2))
    # LANCZOS: the default bicubic visibly aliases at these tiny sizes.
    px = gray.resize((w, h), Resampling.LANCZOS).load()
    span = len(ramp) - 1
    # no_wrap: a line wrapping against a stale mid-resize width would stripe
    # the picture; cropping is invisible by comparison.
    text = Text(no_wrap=True)
    for y in range(h):
        line = "".join(ramp[(255 - px[x, y]) * span // 255] for x in range(w))
        text.append(line + ("\n" if y + 1 < h else ""))
    return text


# The 240 predictable xterm-256 entries (6x6x6 cube + 24 grays). The first 16
# system colors are terminal-themed, so their real RGB is unknowable.
_XTERM_LEVELS = (0, 95, 135, 175, 215, 255)
_XTERM_PALETTE_IMG = None
_XTERM_TEXTUAL_COLORS = None  # index-preserving Textual colors, built once


def _xterm_palette():
    """A Pillow palette image holding the cube+gray entries, built once."""
    global _XTERM_PALETTE_IMG
    if _XTERM_PALETTE_IMG is None:
        from PIL import Image

        flat = [
            channel
            for r in _XTERM_LEVELS
            for g in _XTERM_LEVELS
            for b in _XTERM_LEVELS
            for channel in (r, g, b)
        ]
        flat += [channel for gray in range(8, 239, 10) for channel in (gray,) * 3]
        img = Image.new("P", (1, 1))
        img.putpalette(flat)
        _XTERM_PALETTE_IMG = img
    return _XTERM_PALETTE_IMG


def _xterm_index(i: int) -> int:
    """Palette position -> xterm color number (cube 16-231, grays 232-255).
    Pillow pads the palette to 256 entries with black; those land on 16."""
    if i < 216:
        return 16 + i
    if i < 240:
        return 232 + (i - 216)
    return 16


def _block_art(img, max_w: int, max_h: int, dither: bool = False) -> Content:
    """The image as truecolor half-blocks fitted into max_w x max_h cells:
    each cell is one "▀" with the upper pixel as foreground and the lower as
    background, so pixels come out square.

    Returns Textual's native Content with ready-made Style objects: rich Text
    costs a style parse and conversion per span at paint time, which
    dominates a full-screen photo with thousands of unique colors.

    ``dither`` is for non-truecolor terminals: Floyd-Steinberg onto the
    cube+gray palette, emitted as exact indices, trades posterized patches
    for fine noise, which reads far better at cell scale."""
    from PIL.Image import Dither, Resampling

    rgb = img.convert("RGB")
    scale = min(max_w / rgb.width, 2 * max_h / rgb.height)
    w = max(1, round(rgb.width * scale))
    h = max(2, round(rgb.height * scale))
    h -= h % 2  # rows are consumed in upper/lower pairs
    resized = rgb.resize((w, h), Resampling.LANCZOS)  # see _ascii_art
    if dither:
        global _XTERM_TEXTUAL_COLORS
        if _XTERM_TEXTUAL_COLORS is None:
            entries = [
                (r, g, b)
                for r in _XTERM_LEVELS
                for g in _XTERM_LEVELS
                for b in _XTERM_LEVELS
            ] + [(gray, gray, gray) for gray in range(8, 239, 10)]
            # ansi= makes the driver emit the exact palette index, so the
            # dithered colors survive the terminal untouched.
            _XTERM_TEXTUAL_COLORS = [
                TextualColor(r, g, b, ansi=_xterm_index(i))
                for i, (r, g, b) in enumerate(entries)
            ]
        qx = resized.quantize(
            palette=_xterm_palette(), dither=Dither.FLOYDSTEINBERG
        ).load()

        def color_at(x: int, y: int) -> TextualColor:
            i = qx[x, y]
            # Pillow pads the palette to 256 entries with black: those few
            # map onto the cube's own black at position 0.
            return _XTERM_TEXTUAL_COLORS[i if i < 240 else 0]

    else:
        px = resized.load()

        def color_at(x: int, y: int) -> TextualColor:
            r, g, b = px[x, y]
            return TextualColor(r, g, b)

    lines, spans, pos = [], [], 0
    for y in range(0, h, 2):
        if y:
            pos += 1  # the newline joining this row to the previous one
        # One span per run of identical color pairs, not per cell: flat
        # areas (most screenshots) collapse to a few spans.
        run_style, run_len = None, 0
        for x in range(w):
            style = TextualStyle(
                foreground=color_at(x, y), background=color_at(x, y + 1)
            )
            if style == run_style:
                run_len += 1
                continue
            if run_len:
                spans.append(ContentSpan(pos, pos + run_len, run_style))
                pos += run_len
            run_style, run_len = style, 1
        spans.append(ContentSpan(pos, pos + run_len, run_style))
        pos += run_len
        lines.append("▀" * w)
    return Content("\n".join(lines), spans)


# The sync loop long-polls for 30s; once the last response is older than
# this, the connection is presumed dead (a silently dropped network hangs the
# poll without raising).
STALE_AFTER = 45.0


def _set_terminal_title(app, text: str) -> None:
    """Put text in the terminal window's own titlebar (OSC 2); Textual has
    no API for it, so the escape goes straight to the driver. Printable
    characters only: room names come from the homeserver and must not
    smuggle their own escape sequences (the same worry as client._clean)."""
    driver = getattr(app, "_driver", None)
    if driver is None:
        return
    safe = "".join(ch for ch in text if ch.isprintable())[:120]
    try:
        driver.write(f"\x1b]2;{safe}\x07")
    except Exception:
        pass  # a title is cosmetic; never let it take the app down


def _refresh_terminal_title(app, base: str | None = None) -> None:
    """Recompute the terminal titlebar: the current page's name, prefixed
    with the total unread count as "(N) " when the settings screen has the
    counter on. Screens pass their base text on resume; the sync loop calls
    with no base to update just the count in place."""
    if base is not None:
        app._title_base = base
    text = getattr(app, "_title_base", "matrixcli")
    session = getattr(app, "session", None)
    if (
        session is not None
        and getattr(session, "get_setting", None) is not None
        and session.get_setting("titlebar_unread", True)
    ):
        unread = session.total_unread()
        if unread:
            text = f"({unread}) {text}"
    _set_terminal_title(app, text)


class ConnStatus(Static):
    """Connectivity indicator in the footer's bottom-right corner: a green
    dot plus the age of the last successful sync, or a red "offline" marker
    with the same age (how stale the screen is). Re-rendered on a 1s timer.
    Also vim's showcmd corner: digits of a half-typed count ("10" of "10j")
    show here while they are pending (see VimCount)."""

    def on_mount(self) -> None:
        # layout=True: the text width changes as the age grows, and the
        # dock: right slot must resize.
        self.set_interval(1.0, lambda: self.refresh(layout=True))

    def render(self) -> Text:
        count = getattr(self.app, "pending_count", "")
        lead = ((count + " ", "bold"),) if count else ()
        last = getattr(self.app, "last_sync_at", None)
        ok = getattr(self.app, "sync_ok", True)
        if last is None:
            # Still starting up; only the count, if any, means anything yet.
            return Text.assemble(*lead)
        age = max(0.0, time.monotonic() - last)
        if age < 60:
            age_str = f"{int(age)}s"
        elif age < 3600:
            age_str = f"{int(age // 60)}m"
        else:
            age_str = f"{int(age // 3600)}h"
        if ok and age <= STALE_AFTER:
            return Text.assemble(*lead, ("● ", "green"), (age_str, "dim"))
        return Text.assemble(*lead, ("✗ offline ", "bold red"), (age_str, "red"))


class StatusFooter(Footer):
    """The standard key-binding footer with a ConnStatus docked at its right
    edge. Composed as a child (not overlaid) so the Footer's own recompose on
    binding changes rebuilds the indicator along with the keys."""

    def compose(self) -> ComposeResult:
        yield from super().compose()
        yield ConnStatus()


class EntryItem(ListItem):
    """A ListView row that remembers which room/person it points at."""

    def __init__(self, entry: Entry, label: str) -> None:
        super().__init__(Label(label))
        self.entry = entry


class MessageHitItem(ListItem):
    """A global-search result row pointing at one message in one room:
    opening it opens the room and jumps to that message."""

    def __init__(self, entry: Entry, event_id: str, label: str) -> None:
        super().__init__(Label(label))
        self.entry = entry
        self.event_id = event_id


class MessageLine(Static):
    """One rendered message in the scrollable list. Holds the index into the
    screen's message list so navigation and replies can find it back."""

    def __init__(self, renderable, index: int) -> None:
        super().__init__(renderable)
        self.msg_index = index


class ComposerArea(TextArea):
    """A borderless, soft-wrapping multi-line editor where Enter sends and
    Shift+Enter inserts a newline. The send/cancel bindings are real Textual
    bindings (show=True) so they appear in the footer while the editor is
    focused."""

    BINDINGS = [
        Binding("enter", "send", "Send", show=True),
        Binding("shift+enter", "newline", "New line", show=True),
        # Fallback for terminals without the kitty keyboard protocol, where
        # Shift+Enter is indistinguishable from Enter.
        Binding("alt+enter", "newline", "New line", show=False),
        Binding("escape", "cancel", "Cancel", show=True),
    ]

    def render_lines(self, crop):
        # Textual wipes a pruned widget's styles, but the compositor can
        # still paint one frame from its stale map; TextArea's per-frame
        # theme application then crashes on the half-torn-down state. Hand
        # the compositor blank lines instead.
        if not self.is_attached:
            return [Strip.blank(crop.size.width) for _ in range(crop.size.height)]
        return super().render_lines(crop)

    class Submitted(textual.message.Message):
        def __init__(self, value: str) -> None:
            super().__init__()
            self.value = value

    class Cancelled(textual.message.Message):
        pass

    _submitted = False

    async def _on_key(self, event: textual.events.Key) -> None:
        # TextArea's own _on_key consumes Enter before bindings run, so the
        # enter->send binding above is footer decoration only; intercept
        # here. Shift+Enter is not consumed, so its binding fires normally.
        if event.key == "enter":
            event.stop()
            event.prevent_default()
            self.action_send()
            return
        await super()._on_key(event)

    def action_newline(self) -> None:
        start, end = self.selection
        self._replace_via_keyboard("\n", start, end)

    def action_send(self) -> None:
        # One send per editor instance: a second Enter during the round-trip
        # would queue a duplicate. Every redraw mounts a fresh instance.
        if self._submitted:
            return
        self._submitted = True
        self.post_message(self.Submitted(self.text))

    def action_cancel(self) -> None:
        self.post_message(self.Cancelled())


class VimCount:
    """Vim-style count prefix, mixed into the screens with a j/k list. Typed
    digits accumulate (shown in the footer's right corner, like showcmd) and
    the next motion consumes them, so "10j" moves ten rows. Any key outside
    COUNT_KEYS drops the pending count, as vim does. Digits reach on_key
    before any binding fires, and never while an Input/TextArea is focused
    (it consumes them as text), so typing numbers into a draft is safe."""

    COUNT_KEYS: tuple = ()

    _count = ""

    def on_key(self, event: textual.events.Key) -> None:
        # A bare leading 0 is not a count in vim either.
        if event.key.isdigit() and (self._count or event.key != "0"):
            self._count += event.key
            self._show_count()
            event.stop()
            event.prevent_default()
        elif self._count and event.key not in self.COUNT_KEYS:
            self._count = ""
            self._show_count()

    def _take_count(self) -> int | None:
        """The pending count, consumed; None when none was typed."""
        if not self._count:
            return None
        n = int(self._count)
        self._count = ""
        self._show_count()
        return n

    def _show_count(self) -> None:
        self.app.pending_count = self._count
        for status in self.query(ConnStatus):
            status.refresh(layout=True)


class RoomScreen(VimCount, Screen):
    BINDINGS = [
        ("escape", "back", "Back"),
        # Straight to the dashboard, however deep the page stack is; only
        # the dashboard's own q quits (":q!" quits from anywhere).
        Binding("q", "app.go_home", "Home", show=False),
        ("j", "down", "Down"),
        ("k", "up", "Up"),
        Binding("down", "down", "Down", show=False),
        Binding("up", "up", "Up", show=False),
        # Vim's g/G: g browses archived history from the room's first message
        # (or falls back to the first loaded one); G jumps to the newest
        # message and reattaches to the live tail.
        Binding("g", "first_message", "First", show=False),
        Binding("G", "last_message", "Last", show=False),
        # More of vim's motions: Ctrl+D/U and Ctrl+F/B page by window
        # height, Ctrl+O/Ctrl+I walk the jumplist left behind by long jumps
        # (g, G, :N, u, search), and "{"/"}" step between sender blocks.
        Binding("ctrl+d", "half_page_down", "Half page down", show=False),
        Binding("ctrl+u", "half_page_up", "Half page up", show=False),
        Binding("ctrl+f", "page_down", "Page down", show=False),
        Binding("ctrl+b", "page_up", "Page up", show=False),
        Binding("ctrl+o", "jump_back", "Jump back", show=False),
        # Most terminals send Ctrl+I as a plain Tab; bind both spellings.
        Binding("ctrl+i", "jump_forward", "Jump forward", show=False),
        Binding("tab", "jump_forward", "Jump forward", show=False),
        Binding("left_curly_bracket", "prev_block", "Prev sender", show=False),
        Binding("right_curly_bracket", "next_block", "Next sender", show=False),
        # Gated by check_action to the messages they can actually act on, so
        # the footer only offers them when there is a thread to unfold/fold.
        ("l", "expand", "Unfold thread"),
        ("h", "collapse", "Fold thread"),
        ("u", "first_unread", "First unread"),
        Binding("slash", "search_room", "Search", show=False),
        # One key, three labels: check_action enables exactly the one that
        # says what Enter will do to the selected message. The reactions
        # popup is the spacebar's job; Enter reaches it only via the actions
        # menu.
        Binding("enter", "open_download", "Download"),
        Binding("enter", "open_link", "Open link"),
        Binding("enter", "open_actions", "Message actions"),
        # Two bindings share the spacebar: an image gets an in-terminal
        # preview, any other message with reaction badges shows who sent
        # them. Inside either popup space closes it again: a toggle.
        ("space", "preview_image", "Preview"),
        Binding("space", "show_reactions", "Who reacted"),
        # Enter acts on what a message says; Shift+Enter looks behind it, at
        # the versions of an edited one or the text of a deleted one.
        Binding("shift+enter", "open_details", "Show history"),
        # Fallback for terminals without the kitty keyboard protocol, where
        # Shift+Enter is indistinguishable from Enter (as in the composer).
        Binding("alt+enter", "open_details", "Show history", show=False),
        ("r", "reply", "Reply"),
        # Gated by check_action to delivered, undeleted messages.
        ("a", "react", "React"),
        ("n", "compose", "New message"),
        # Gated by check_action to our own delivered messages.
        ("e", "edit_own", "Edit"),
        ("d", "delete_own", "Delete"),
        # Two bindings share "t"; check_action enables exactly one, so the
        # footer always names the view you are currently in.
        Binding("t", "threads_on", "View: normal"),
        Binding("t", "threads_off", "View: threaded"),
        ("T", "thread", "Open thread"),
        # Same pattern as "t": the enabled label names the current spacing.
        Binding("c", "compact_on", "Compact: off"),
        Binding("c", "compact_off", "Compact: on"),
        # Same pattern for "~": the enabled label names what the sender
        # column shows (display names or raw @user:server ids).
        Binding("tilde", "ids_on", "Show: names"),
        Binding("tilde", "ids_off", "Show: ids"),
    ]

    # Keys whose actions read a typed count; any other key drops it.
    COUNT_KEYS = (
        "j", "k", "down", "up", "g", "G",
        "left_curly_bracket", "right_curly_bracket",
    )

    def __init__(self, entry: Entry) -> None:
        super().__init__()
        self.entry = entry
        self.messages: list = []
        self.reply_to = None  # a Message we are replying to, or None
        self.compact = False  # default: blank line above each name header
        self.show_ids = False  # True: sender column shows @user:server ids
        self.selected = 0  # index of the currently highlighted message
        self.thread_counts: dict[str, int] = {}  # root event id -> reply count
        self.threaded = False  # True: replies shown indented under their root
        self.expanded: set[str] = set()  # roots unfolded inline ("l") in normal view
        self._thread_replies: dict[str, list] = {}  # full reply lists, fetched on unfold
        self.older: list = []  # back-paginated messages, kept across refreshes
        self._paginating = False  # guard against overlapping fetches
        self._at_beginning = False  # history start reached and announced
        # Browse mode ("g", or a search jump into deep history): a snapshot
        # of the archived history, detached from the live tail; None while
        # following the tail. [start, upto) is the slice of snapshot rows in
        # view.
        self._browse: list | None = None
        self._browse_start = 0
        self._browse_upto = 0
        # The room's fully-read marker as it was when this screen opened;
        # everything after it renders below a "new" divider, and "u" jumps
        # there. Frozen at open so live arrivals stay marked until you leave.
        self._opened_read_marker: str | None = None
        # Event id to land the selection on once the initial load is in, set
        # by open_room for a global-search message hit; consumed once.
        self._jump_target: str | None = None
        # Signature of the message list the sync-refresh path last acted on
        # (see refresh_messages); None forces the first refresh to compare
        # against the initial load.
        self._live_sig: list | None = None
        # Event id of the first unread message, chosen once from the opening
        # snapshot (see on_mount); the divider is anchored to it thereafter.
        self._first_unread_event: str | None = None
        # False until _finish_mount's full history fetch has run once;
        # refresh_messages stays a no-op before that.
        self._loaded = False
        self._composing = False  # True while an "n" new-message editor is open
        self._editing = None  # own Message being rewritten with "e", or None
        self._last_seen_event: str | None = None  # latest event id we rendered
        # What _redraw last mounted, so a tail-append can continue instead of
        # rebuilding everything.
        self._drawn_sig: list | None = None
        self._drawn_mode: tuple | None = None
        self._drawn_tail: tuple = (None, None)
        # In-flight local echoes, spliced back into every reload so a sync
        # cannot drop them before the server confirms (see _finish_send).
        self._pending: list = []
        # Vim's jumplist: positions left behind by g/G/:N/u/search jumps,
        # as (event_id, thread_root, was_browsing) spots. Ctrl+O walks back
        # through them, Ctrl+I (Tab) forward; _jump_pos == len(_jumps) means
        # we are at the newest position, past every recorded spot.
        self._jumps: list[tuple] = []
        self._jump_pos = 0
        # _redraw is reached from workers, this screen's own handlers, and
        # app-level sync callbacks; the lock keeps rebuilds from interleaving.
        self._redraw_lock = asyncio.Lock()

    def compose(self) -> ComposeResult:
        yield Header()
        yield VerticalScroll(id="timeline")
        # Docked below the timeline and empty until "r"/"n" fills it, so the
        # editor is never part of the scrolling history.
        yield Vertical(id="composer")
        yield StatusFooter()

    async def _load_messages(self, cached_only: bool = False) -> list:
        """The message list this screen renders. Normal view: the main
        timeline with thread replies collapsed out and per-root reply counts
        for the ⤷ badges. Threaded view: replies indented under their root;
        replies whose root fell out of the window stay inline. ThreadScreen
        overrides this to load a single thread. cached_only skips the network
        so on_mount can paint instantly before the full fetch."""
        if self._browse is not None:
            # Browse mode: exactly the archive rows loaded so far (in
            # self.older), the live window deliberately absent.
            messages = list(self.older)
        else:
            messages = await self.app.session.load_history(
                self.entry.room_id, cached_only=cached_only
            )
            if self.older:
                # Scrolled-back history lives on the screen (the session cache
                # only keeps a recent window); splice it back in on every
                # reload.
                merged = {m.event_id: m for m in self.older}
                for m in messages:
                    merged[m.event_id] = m
                messages = sorted(merged.values(), key=lambda m: m.ts)
        # After the splice, so an edit found by back-pagination still folds
        # into a target from the live window (and vice versa).
        messages = fold_edits(messages)
        counts: dict[str, int] = {}
        replies: dict[str, list] = {}
        main = []
        for m in messages:
            if m.thread_root:
                counts[m.thread_root] = counts.get(m.thread_root, 0) + 1
                replies.setdefault(m.thread_root, []).append(m)
            else:
                main.append(m)
        # The server's aggregated count on the root is authoritative when it
        # exceeds what the local history window happens to contain.
        for m in main:
            if m.thread_count > counts.get(m.event_id, 0):
                counts[m.event_id] = m.thread_count
        self.thread_counts = counts
        if not self.threaded:
            out = []
            for m in main:
                out.append(m)
                # Threads unfolded with "l" show their replies inline, merged
                # from the history window and the full fetch done on unfold.
                if m.event_id in self.expanded:
                    merged = {
                        r.event_id: r
                        for r in self._thread_replies.get(m.event_id, [])
                    }
                    for r in replies.get(m.event_id, []):
                        merged[r.event_id] = r
                    out.extend(sorted(merged.values(), key=lambda r: r.ts))
            return self._splice_pending(out)
        root_ids = {m.event_id for m in main}
        out = []
        for m in messages:
            if m.thread_root and m.thread_root in root_ids:
                continue  # nested under its root below
            out.append(m)
            if not m.thread_root:
                out.extend(replies.get(m.event_id, []))
        return self._splice_pending(out)

    def _splice_pending(self, messages: list) -> list:
        """Append the in-flight local echoes to a freshly built display list;
        without this an unconfirmed message would blink out whenever a sync
        lands during the round-trip. An echo whose real event already arrived
        via sync is skipped, one confirmed message per echo, so sending the
        same text twice still shows two entries."""
        claimed: set[str] = set()
        for p in self._pending:
            if any(m.event_id == p.event_id for m in messages):
                continue
            arrived = next(
                (
                    m
                    for m in messages
                    if not m.pending
                    and m.event_id not in claimed
                    and m.sender == p.sender
                    and m.body == p.body
                    # Window against clock skew: only a message from roughly
                    # now can be this echo's confirmation.
                    and abs(m.ts - p.ts) < 5 * 60 * 1000
                ),
                None,
            )
            if arrived is not None:
                claimed.add(arrived.event_id)
                continue
            messages.append(p)
        return messages

    def on_screen_resume(self) -> None:
        # The terminal window's own titlebar names the room being read.
        _refresh_terminal_title(self.app, f"{self.entry.title} - matrixcli")

    async def on_mount(self) -> None:
        self.title = self.entry.title
        self.sub_title = self.entry.room_id
        # Keep the timeline unfocusable so arrow keys always reach the
        # screen's selection bindings instead of scrolling the container.
        self.query_one("#timeline", VerticalScroll).can_focus = False
        room = self.app.session.client.rooms.get(self.entry.room_id)
        self._opened_read_marker = getattr(room, "fully_read_marker", None)
        self._reset_history_position()
        # Paint from the sync-seeded cache before touching the network: the
        # full fetch can take seconds, and a shallow timeline beats a blank
        # screen. The unread divider waits for the full window below.
        quick = await self._load_messages(cached_only=True)
        if quick:
            self.messages = quick
            self.selected = len(quick) - 1
            await self._redraw()
        # In a worker: on this screen's message pump the network fetch would
        # block every key bound here, and Textual's shutdown waits on the
        # pump, so quitting would hang too.
        self.run_worker(self._finish_mount(), group="initial_load", exclusive=True)

    async def _finish_mount(self) -> None:
        """The slow half of on_mount, run as a worker: the full history fetch,
        the unread divider (whose count fallback needs the complete opening
        snapshot), and the opening read receipt."""
        try:
            messages = await self._load_messages()
            if not self.is_attached:
                return  # backed out of the room while the fetch was in flight
            # Captured NOW, not after the marker fetches below: an event
            # arriving during those round trips must still look new to the
            # first sync refresh, or it would be marked seen unrendered.
            opening_latest = self.app.session.last_event_id.get(
                self.entry.room_id
            )
            # If the user moved off the tail during the fetch, keep their
            # place by event id (the full window may have inserted rows
            # above it).
            follow = (
                not self.messages or self.selected >= len(self.messages) - 1
            )
            anchor = None if follow else self.messages[self.selected].event_id
            self.messages = messages
            if follow:
                self.selected = max(0, len(messages) - 1)
            else:
                pos = next(
                    (i for i, m in enumerate(messages) if m.event_id == anchor),
                    None,
                )
                self.selected = (
                    pos
                    if pos is not None
                    else min(self.selected, max(0, len(messages) - 1))
                )
            # Anchor the "new" divider to an event id now: the count fallback
            # is only meaningful against the opening snapshot, and
            # recomputing it as the list grows would drift the divider.
            if self._opened_read_marker is None:
                # nio usually has no marker on the first open after a launch
                # (resumed syncs do not carry old room account data). Fetch
                # it rather than trust the unread-count guess below, which
                # counts thread replies and can misplace the divider.
                self._opened_read_marker = (
                    await self.app.session.fetch_fully_read(self.entry.room_id)
                )
            marker = self._opened_read_marker
            marker_ts = None
            if (
                self._divider_ts_fallback
                and marker
                and self.messages
                and all(m.event_id != marker for m in self.messages)
            ):
                marker_ts = await self.app.session.event_timestamp(
                    self.entry.room_id, marker
                )
            idx = self._first_unread_index(marker_ts)
            self._first_unread_event = (
                self.messages[idx].event_id if idx is not None else None
            )
            if not self.messages:
                # Auto-open the composer in an empty room as real state, so
                # Escape can cancel it and leave the room.
                self._composing = True
            self._last_seen_event = opening_latest
            self._live_sig = self._signature(messages)
            await self._redraw(keep_scroll=not follow)
            await self.app.session.mark_read(self.entry.room_id)
            if self._jump_target:
                # Opened from a global-search hit: land on it, wherever it
                # lives.
                target, self._jump_target = self._jump_target, None
                idx = next(
                    (
                        i
                        for i, m in enumerate(self.messages)
                        if m.event_id == target
                    ),
                    None,
                )
                if idx is not None:
                    self.selected = idx
                    self._highlight()
                else:
                    corpus = self._search_corpus()
                    hit = next(
                        (m for m in corpus if m.event_id == target), None
                    )
                    if hit is not None:
                        await self._jump_to_hit(hit, corpus)
            # With the visible window up, download the room's ENTIRE history
            # in the background so everything survives locally (deletions
            # and edit versions included). No-op when already archived or
            # downloading.
            self.app.session.start_backfill(self.entry.room_id)
        finally:
            # Even on a failed load, hand live updates over to
            # refresh_messages; its next run reloads what this one missed.
            self._loaded = True

    async def refresh_names(self) -> None:
        """Called when the background member fetch for this room lands:
        senders painted as raw @user:server ids get their display names.
        Selection and scroll stay put."""
        if not self.is_attached:
            return
        messages = await self._load_messages(cached_only=True)
        if not messages or not self.is_attached:
            return
        if [m.sender_name for m in messages] == [
            m.sender_name for m in self.messages
        ]:
            return
        self.messages = messages
        self.selected = min(self.selected, len(messages) - 1)
        await self._redraw(keep_scroll=True)

    async def refresh_messages(self) -> None:
        """Called by the app after each background sync. Reload and redraw if
        the room received events since we last drew, follow the tail if the
        selection was on it, and mark the new messages read (we are looking at
        the room, after all)."""
        # Runs as a worker: the screen may have been popped by now, and
        # before the opening load finishes it would race _finish_mount.
        if not self.is_attached or not self._loaded:
            return
        # Browsing history: a reload would clobber the detached view.
        # _last_seen_event stays untouched, so the first refresh after
        # leaving browse mode catches up.
        if self._browse is not None:
            return
        latest = self.app.session.last_event_id.get(self.entry.room_id)
        if not latest or latest == self._last_seen_event:
            return
        prev_counts = self.thread_counts
        messages = await self._load_messages()
        if not self.is_attached:
            return
        # Compare against the signature this path last acted on, NOT a fresh
        # signature of self.messages: reaction summaries are looked up live,
        # so a reaction would otherwise never repaint. A new thread reply
        # only changes a badge count, hence the counts check.
        new_sig = self._signature(messages)
        if new_sig != self._live_sig or self.thread_counts != prev_counts:
            follow = not self.messages or self.selected >= len(self.messages) - 1
            # Keep the selection on the same message by event id, not index:
            # a thread reply arriving in threaded (or unfolded) view inserts
            # ABOVE the selection, and a kept index would silently land the
            # highlight on a different message.
            anchor = (
                None
                if follow or self.selected >= len(self.messages)
                else self.messages[self.selected].event_id
            )
            # The reload built fresh Message objects; re-point the reply
            # target so the composer keeps quoting current text.
            if self.reply_to is not None and self.reply_to.event_id:
                self.reply_to = next(
                    (m for m in messages if m.event_id == self.reply_to.event_id),
                    self.reply_to,
                )
            self.messages = messages
            if follow:
                self.selected = max(0, len(messages) - 1)
            else:
                pos = next(
                    (i for i, m in enumerate(messages) if m.event_id == anchor),
                    None,
                )
                self.selected = (
                    pos
                    if pos is not None
                    else min(self.selected, max(0, len(messages) - 1))
                )
            # Not following the tail means the user is reading somewhere
            # above; keep their scroll position instead of snapping back.
            await self._redraw(keep_scroll=not follow)
        # Only now mark the batch seen: this exclusive worker can be
        # cancelled mid-await by the next sync's run, and marking earlier
        # would make that run skip the batch, leaving a stale screen.
        self._live_sig = new_sig
        self._last_seen_event = latest
        await self.app.session.mark_read(self.entry.room_id)

    def _signature(self, messages: list) -> list:
        """What a reload has to change for the timeline to need redrawing. Not
        just the event ids: an edit, a deletion, or a reaction changes a
        message in place, leaving the list of ids exactly as it was; so does
        a sender name resolving once the member list arrives."""
        return [
            (
                m.event_id,
                m.sender_name,
                m.edited_ts,
                m.redacted_ts,
                tuple(
                    self.app.session.reaction_summary(
                        self.entry.room_id, m.event_id
                    )
                ),
            )
            for m in messages
        ]

    def _render_sig(self) -> list:
        """Everything a message's mounted widget depends on, one tuple per
        message; _redraw compares these to skip work. A change to a reply's
        TARGET changes the target's own entry, breaking the prefix match and
        forcing the rebuild that refreshes the quote."""
        return [
            (
                m.event_id,
                m.sender,
                m.sender_name,
                m.body,
                m.ts,
                m.pending,
                m.edited_ts,
                m.redacted_ts,
                m.media_url,
                m.mentions_me,
                self.thread_counts.get(m.event_id, 0) if m.event_id else 0,
                tuple(
                    self.app.session.reaction_summary(
                        self.entry.room_id, m.event_id
                    )
                ),
            )
            for m in self.messages
        ]

    def _render_mode(self) -> tuple:
        """View-wide switches the whole render depends on; any change forces
        a full rebuild even when no message changed."""
        return (
            self.threaded,
            self.compact,
            self.show_ids,
            self._first_unread_pos(),
            tuple(sorted(self.expanded)),
            self.app.session.my_name,
        )

    def _build_rows(self, start: int, prev, prev_day, first_unread):
        """Widgets for messages[start:], continuing from the (previous
        sender, previous day) carry; returns (rows, prev, prev_day) so an
        append can resume where the last draw stopped."""
        rows = []
        for i in range(start, len(self.messages)):
            m = self.messages[i]
            # A divider wherever the calendar day changes, so scrolled-back
            # history says which day it is (timestamps are HH:MM only).
            lt = _safe_localtime(m.ts) if m.ts else None
            day = lt[:3] if lt else prev_day
            if day != prev_day and prev_day is not None:
                stamp = time.strftime("%a %d %b %Y", lt)
                rows.append(Static(Text(f"── {stamp} ──", style="dim")))
                # Without this the first message of a day inherits header
                # suppression from the previous day and renders attributed
                # to nobody.
                prev = None
            prev_day = day
            if i == first_unread:
                rows.append(
                    Static(Text("── new ──", style="red"), classes="unread-marker")
                )
                prev = None  # same rule: the first unread names its sender
            line = MessageLine(self._render_message(m, prev, i), i)
            if m.thread_root and (
                self.threaded or m.thread_root in self.expanded
            ):
                line.add_class("thread-reply")
            rows.append(line)
            prev = m.sender
        return rows, prev, prev_day

    async def _redraw(self, keep_scroll: bool = False) -> None:
        """Bring the timeline widgets in line with self.messages. Serialized
        by a lock: two interleaved rebuilds would duplicate widgets. The
        composer lives in its own docked panel (see _sync_composer), so a
        rebuild can never swallow a half-typed draft.

        Widgets are tracked by signature: tail-appends mount only the new
        rows and a no-change refresh keeps every widget; anything else
        rebuilds the whole list in a single batched mount.

        keep_scroll: restore the scroll offset instead of snapping to the
        highlighted message, so a live refresh does not yank the view back."""
        async with self._redraw_lock:
            if not self.is_attached:
                return
            try:
                tl = self.query_one("#timeline", VerticalScroll)
            except Exception:
                return
            scroll_y = tl.scroll_y
            sig = self._render_sig()
            mode = self._render_mode()
            drawn = self._drawn_sig
            if (
                self.messages
                and drawn
                and self._drawn_mode == mode
                and len(sig) >= len(drawn)
                and sig[: len(drawn)] == drawn
            ):
                if len(sig) > len(drawn):
                    prev, prev_day = self._drawn_tail
                    rows, prev, prev_day = self._build_rows(
                        len(drawn), prev, prev_day, self._first_unread_pos()
                    )
                    await tl.mount(*rows)
                    self._drawn_sig = sig
                    self._drawn_tail = (prev, prev_day)
                # Equal length: nothing visible changed; keep every widget.
            else:
                await tl.remove_children()
                if not self.messages:
                    self._drawn_sig = None
                    await tl.mount(Static(Text("(no messages yet)", style="dim")))
                    await self._sync_composer()
                    return
                rows, prev, prev_day = self._build_rows(
                    0, None, None, self._first_unread_pos()
                )
                await tl.mount(*rows)
                self._drawn_sig = sig
                self._drawn_mode = mode
                self._drawn_tail = (prev, prev_day)

            if keep_scroll:
                self._highlight(scroll=False)
                # After layout settles, put the view back; new content only
                # grew the bottom, so the offset still points at the same
                # rows.
                self.call_after_refresh(
                    lambda: tl.scroll_to(y=scroll_y, animate=False)
                )
            else:
                self._highlight(scroll=False)
                # Freshly mounted rows have no geometry yet, so an immediate
                # scroll_visible is a no-op and the old offset would survive
                # (":1" landing mid-window); snap once layout has settled.
                self.call_after_refresh(self._highlight)
            await self._sync_composer()

    def _composer_title(self) -> str:
        if self._editing is not None:
            return "Edit message"
        if self.reply_to is None:
            return "New message"
        # escape(): both the name and the quoted line come from the sender.
        lines = (self.reply_to.body or "").strip().splitlines() or [""]
        return escape(
            f"Reply to {self.reply_to.sender_name}: {lines[0][:60]}"
        )

    async def _sync_composer(self) -> None:
        """Bring the docked composer in line with the editing state. An open
        editor is mounted once and left alone, so live refreshes cannot
        disturb typing; its fixed height makes a long draft scroll
        internally."""
        panel = self.query_one("#composer", Vertical)
        if self.reply_to is None and not self._composing:
            await panel.remove_children()
            panel.display = False
            return
        panel.display = True
        if panel.children:
            self.query_one("#composertitle", Label).update(self._composer_title())
            return
        editor = ComposerArea(
            id="editor", soft_wrap=True, compact=True, show_line_numbers=False
        )
        await panel.mount(Label(self._composer_title(), id="composertitle"), editor)
        if self._editing is not None:
            # Editing starts from the message's current text, not a draft.
            editor.text = self._editing.body
            editor.move_cursor(editor.document.end)
        else:
            # An earlier Escape stashed its text; hand it back instead of
            # opening empty (cleared on send or on cancelling it empty).
            stashed = self.app.session.drafts.get(self._draft_key())
            if stashed:
                editor.text = stashed
                editor.move_cursor(editor.document.end)
        editor.focus()
        # The timeline just lost the panel's rows; put the selected message
        # back in view once the new layout has settled.
        self.call_after_refresh(self._highlight)

    def _draft_key(self) -> str:
        return self.entry.room_id

    def _highlight(self, scroll: bool = True) -> None:
        for line in self.query(MessageLine):
            line.set_class(line.msg_index == self.selected, "selected")
        # The Enter/Shift+Enter footer labels belong to the selected message,
        # so the footer has to re-evaluate them every time the selection moves.
        self.refresh_bindings()
        if not scroll:
            return
        try:
            target = next(
                l for l in self.query(MessageLine) if l.msg_index == self.selected
            )
        except StopIteration:
            return
        target.scroll_visible(animate=False)

    def action_compact_on(self) -> None:
        self._toggle_compact()

    def action_compact_off(self) -> None:
        self._toggle_compact()

    def _toggle_compact(self) -> None:
        self.compact = not self.compact
        self.refresh_bindings()  # flip the footer label with the state
        self.run_worker(self._redraw())

    def action_ids_on(self) -> None:
        self._toggle_ids()

    def action_ids_off(self) -> None:
        self._toggle_ids()

    def _toggle_ids(self) -> None:
        self.show_ids = not self.show_ids
        self.refresh_bindings()  # flip the footer label with the state
        self.run_worker(self._redraw())

    def action_down(self) -> None:
        n = self._take_count() or 1  # "10j" moves ten
        if not self.messages:
            return
        if self._browse is not None and self.selected >= len(self.messages) - 1:
            # Bottom of the browse view: pull the next chunk of archive, or
            # reattach to the live tail once the snapshot is walked dry.
            self.run_worker(
                self._extend_browse(), group="browse", exclusive=True
            )
            return
        self.selected = min(self.selected + n, len(self.messages) - 1)
        self._highlight()

    def action_up(self) -> None:
        n = self._take_count() or 1  # "10k" moves ten
        if not self.messages:
            return
        if self.selected == 0:
            self._fetch_older()
            return
        self.selected = max(0, self.selected - n)
        self._highlight()

    # Archived history loaded per chunk: widgets are not virtualized, so
    # this keeps a 20k-message room from mounting 20k widgets in one go.
    BROWSE_CHUNK = 200

    def action_first_message(self) -> None:
        """g: jump to the room's first message. With archived history this
        detaches from the live tail and browses the archive from the very
        beginning; without any (download not started, caching off, thread
        view) it falls back to the first loaded message. A count makes it
        absolute: "10g" goes to message 10, like ":10"."""
        n = self._take_count()
        self._push_jump()
        if n is not None:
            self.run_worker(self._goto_index(n), group="browse", exclusive=True)
            return
        if self._browse is None:
            rows = self.app.session.archive_rows(self.entry.room_id)
            if rows:
                self._browse = rows
                self._browse_start = 0
                self._browse_upto = min(len(rows), self.BROWSE_CHUNK)
                self.older = rows[: self._browse_upto]
                if self.entry.room_id not in self.app.session.archive_done:
                    self.app.notify(
                        "History is still downloading; starting at the "
                        "oldest message fetched so far.",
                        timeout=4,
                    )
                self.run_worker(
                    self._apply_browse(0), group="browse", exclusive=True
                )
                return
        if self.messages:
            self.selected = 0
            self._highlight()

    def action_last_message(self) -> None:
        """G: the newest message. From browse mode this reattaches to the
        live tail; otherwise it just jumps there. A count makes it vim's
        goto-line: "10G" goes to message 10."""
        n = self._take_count()
        self._push_jump()
        if n is not None:
            self.run_worker(self._goto_index(n), group="browse", exclusive=True)
            return
        if self._browse is not None:
            self.run_worker(self._exit_browse(), group="browse", exclusive=True)
            return
        if self.messages:
            self.selected = len(self.messages) - 1
            self._highlight()

    def goto_message(self, n: int) -> None:
        """":N" from the command line: jump to the N-th message."""
        self._push_jump()
        self.run_worker(self._goto_index(n), group="browse", exclusive=True)

    async def _goto_index(self, n: int) -> None:
        """The N-th message (1-based) of everything held for this room,
        browsing the archive as "g" does; past the end it lands on the
        newest message and reattaches to the live tail, like "G"."""
        rows = (
            self._browse
            if self._browse is not None
            else self.app.session.archive_rows(self.entry.room_id)
        )
        if not rows:
            # Nothing archived (caching off): count within what is loaded.
            if self.messages:
                self.selected = max(0, min(n - 1, len(self.messages) - 1))
                self._highlight()
            return
        if n >= len(rows):
            if self._browse is not None:
                await self._exit_browse()
            elif self.messages:
                self.selected = len(self.messages) - 1
                self._highlight()
            return
        m = rows[max(0, n - 1)]
        await self._land_on_event(m.event_id, m.thread_root, rows)

    def _push_jump(self) -> None:
        """Remember the current position before a long jump; Ctrl+O returns
        here. A new jump discards the forward (Ctrl+I) tail, as in vim."""
        if not self.messages or self.selected >= len(self.messages):
            return
        m = self.messages[self.selected]
        del self._jumps[self._jump_pos:]
        if not self._jumps or self._jumps[-1][0] != m.event_id:
            self._jumps.append(
                (m.event_id, m.thread_root, self._browse is not None)
            )
        self._jump_pos = len(self._jumps)

    def action_jump_back(self) -> None:
        if self._jump_pos == 0:
            return
        if self._jump_pos == len(self._jumps):
            # Leaving the newest position: record it so Ctrl+I can return.
            if self.messages and self.selected < len(self.messages):
                m = self.messages[self.selected]
                if self._jumps[-1][0] != m.event_id:
                    self._jumps.append(
                        (m.event_id, m.thread_root, self._browse is not None)
                    )
        self._jump_pos -= 1
        self.run_worker(
            self._restore_jump(self._jumps[self._jump_pos]),
            group="browse",
            exclusive=True,
        )

    def action_jump_forward(self) -> None:
        if self._jump_pos >= len(self._jumps) - 1:
            return
        self._jump_pos += 1
        self.run_worker(
            self._restore_jump(self._jumps[self._jump_pos]),
            group="browse",
            exclusive=True,
        )

    async def _restore_jump(self, spot: tuple) -> None:
        event_id, thread_root, was_browsing = spot
        if not was_browsing and self._browse is not None:
            # The spot was on the live tail: reattach before looking for it.
            await self._exit_browse()
        idx = next(
            (i for i, m in enumerate(self.messages) if m.event_id == event_id),
            None,
        )
        if idx is not None:
            self.selected = idx
            self._highlight()
            return
        await self._land_on_event(event_id, thread_root)

    def action_half_page_down(self) -> None:
        self._page(0.5)

    def action_half_page_up(self) -> None:
        self._page(-0.5)

    def action_page_down(self) -> None:
        self._page(1.0)

    def action_page_up(self) -> None:
        self._page(-1.0)

    def _page(self, factor: float) -> None:
        """Ctrl+D/U/F/B: move the selection about half or a whole window.
        Messages vary in height, so the target is resolved through widget
        geometry rather than a fixed row count."""
        if not self.messages or isinstance(self.focused, TextArea):
            return
        lines = sorted(self.query(MessageLine), key=lambda l: l.virtual_region.y)
        current = next(
            (l for l in lines if l.msg_index == self.selected), None
        )
        if current is None:
            return
        tl = self.query_one("#timeline", VerticalScroll)
        delta = int(tl.container_size.height * factor)
        target_y = current.virtual_region.y + delta
        if delta > 0:
            below = [l for l in lines if l.msg_index > self.selected]
            pick = next(
                (l for l in below if l.virtual_region.y >= target_y),
                below[-1] if below else None,
            )
        else:
            above = [l for l in lines if l.msg_index < self.selected]
            pick = next(
                (l for l in reversed(above) if l.virtual_region.y <= target_y),
                above[0] if above else None,
            )
        if pick is None:
            # Already at the edge: fall back to j/k, which page in more
            # history (or the next browse chunk) when there is any.
            (self.action_down if delta > 0 else self.action_up)()
            return
        self.selected = pick.msg_index
        self._highlight()

    def action_prev_block(self) -> None:
        """"{": the start of the current sender's run of messages, then the
        start of the run above. Vim's paragraph motion, for chat."""
        if not self.messages:
            return
        i = self.selected
        for _ in range(self._take_count() or 1):
            if i == 0:
                break
            i -= 1
            while i > 0 and self.messages[i].sender == self.messages[i - 1].sender:
                i -= 1
        if i != self.selected:
            self.selected = i
            self._highlight()

    def action_next_block(self) -> None:
        """"}": the first message of the next sender's run."""
        if not self.messages:
            return
        i = self.selected
        for _ in range(self._take_count() or 1):
            j = i + 1
            while (
                j < len(self.messages)
                and self.messages[j].sender == self.messages[j - 1].sender
            ):
                j += 1
            if j >= len(self.messages):
                i = len(self.messages) - 1
                break
            i = j
        if i != self.selected:
            self.selected = i
            self._highlight()

    async def _apply_browse(self, select: int) -> None:
        messages = await self._load_messages()
        if not self.is_attached:
            return
        self.messages = messages
        self.selected = max(0, min(select, len(messages) - 1))
        await self._redraw()

    async def _extend_browse(self) -> None:
        if self._browse is None:
            return
        if self._browse_upto >= len(self._browse):
            await self._exit_browse()
            return
        # Keep the selection anchored by event id: the rebuild refolds edits,
        # so indices can shift even though rows were only appended.
        anchor = (
            self.messages[self.selected].event_id
            if self.selected < len(self.messages)
            else None
        )
        self._browse_upto = min(
            len(self._browse), self._browse_upto + self.BROWSE_CHUNK
        )
        self.older = self._browse[self._browse_start : self._browse_upto]
        messages = await self._load_messages()
        if not self.is_attached:
            return
        self.messages = messages
        pos = next(
            (i for i, m in enumerate(messages) if m.event_id == anchor),
            None,
        )
        # The pressed "j" still means "one step down" from where we were.
        self.selected = min(
            (pos + 1) if pos is not None else len(messages) - 1,
            len(messages) - 1,
        )
        await self._redraw(keep_scroll=True)
        self._highlight()

    async def _extend_browse_up(self) -> None:
        """"k" at the top of a browse window that does not start at the
        room's first message (a search jump landed it mid-history): pull the
        previous chunk of snapshot rows into view."""
        if self._browse is None or self._browse_start <= 0:
            return
        anchor = (
            self.messages[self.selected].event_id
            if self.selected < len(self.messages)
            else None
        )
        self._browse_start = max(0, self._browse_start - self.BROWSE_CHUNK)
        self.older = self._browse[self._browse_start : self._browse_upto]
        messages = await self._load_messages()
        if not self.is_attached:
            return
        self.messages = messages
        pos = next(
            (i for i, m in enumerate(messages) if m.event_id == anchor),
            None,
        )
        # The pressed "k" still means "one step up" from where we were.
        self.selected = max(0, (pos - 1) if pos is not None else 0)
        await self._redraw()

    async def _exit_browse(self) -> None:
        self._browse = None
        self._browse_start = 0
        self._browse_upto = 0
        self.older = []
        # Browsing advanced the pagination depth past rows this view no
        # longer holds; resuming from it would silently skip messages.
        self.app.session.reset_pagination(self.entry.room_id)
        self._at_beginning = False
        messages = await self._load_messages()
        if not self.is_attached:
            return
        self.messages = messages
        self.selected = max(0, len(messages) - 1)
        await self._redraw()
        # _last_seen_event was frozen while browsing; let the next sync tick
        # (or this explicit refresh) catch the view up with what arrived.
        await self.refresh_messages()

    def _reset_history_position(self) -> None:
        """Called once on mount: self.older starts empty, so the session-held
        continuation token must be reset too, or scrolling up would resume
        from the old depth and skip messages. ThreadScreen overrides this to
        a no-op (the room screen beneath still owns its position)."""
        self.app.session.reset_pagination(self.entry.room_id)

    def _fetch_older(self) -> None:
        """Back-paginate when the selection pushes past the top: fetch an
        older batch, splice it in, and land the selection on the message just
        above the previous top. ThreadScreen overrides this to a no-op (a
        thread is already loaded whole via /relations)."""
        if self._paginating or self._at_beginning:
            return
        if self._browse is not None:
            # A window landed mid-history by a search jump grows upward;
            # one that starts at the room's first message has nothing above.
            if self._browse_start > 0:
                self.run_worker(
                    self._extend_browse_up(), group="browse", exclusive=True
                )
            return
        self._paginating = True

        async def fetch() -> None:
            try:
                known = {m.event_id for m in self.messages}
                known.update(m.event_id for m in self.older)
                # A batch can consist entirely of events we already have;
                # skip past those, bounded so a dead room cannot loop
                # forever.
                fresh: list = []
                for _ in range(3):
                    batch = await self.app.session.load_older(self.entry.room_id)
                    if batch is None:
                        # A failed request, not the start of history: stay
                        # retryable, the next "k" at the top tries again.
                        if not fresh:
                            self.app.notify(
                                "Could not fetch older messages; try again.",
                                severity="warning",
                                timeout=4,
                            )
                            return
                        break
                    if not batch:
                        if not fresh:
                            # Announce once; further presses at the top are
                            # ignored instead of stacking notifications.
                            self._at_beginning = True
                            self.app.notify("Beginning of history.", timeout=4)
                            return
                        break
                    fresh.extend(m for m in batch if m.event_id not in known)
                    if fresh:
                        break
                if not fresh or not self.is_attached:
                    return
                anchor = self.messages[0].event_id if self.messages else None
                merged = {m.event_id: m for m in [*fresh, *self.older]}
                self.older = sorted(merged.values(), key=lambda m: m.ts)
                messages = await self._load_messages()
                if not self.is_attached:
                    return
                self.messages = messages
                idx = next(
                    (i for i, m in enumerate(messages) if m.event_id == anchor),
                    1,
                )
                self.selected = max(0, idx - 1)
                await self._redraw()
            finally:
                self._paginating = False

        self.run_worker(fetch())

    async def _reload_view(self, keep: str | None = None) -> None:
        """Rebuild the display list and redraw, keeping the selection on the
        message with event id ``keep`` if it is still visible."""
        messages = await self._load_messages()
        if not self.is_attached:
            return
        self.messages = messages
        idx = next(
            (i for i, m in enumerate(messages) if m.event_id == keep), None
        )
        if idx is not None:
            self.selected = idx
        else:
            self.selected = min(self.selected, max(0, len(messages) - 1))
        await self._redraw()

    def action_expand(self) -> None:
        """"l": unfold the selected message's thread inline; on an already
        open thread, step into its first reply."""
        if not self.messages:
            return
        m = self.messages[self.selected]
        if m.thread_root:
            return
        if self.threaded or m.event_id in self.expanded:
            if (
                self.selected + 1 < len(self.messages)
                and self.messages[self.selected + 1].thread_root == m.event_id
            ):
                self.selected += 1
                self._highlight()
            return
        if not self.thread_counts.get(m.event_id):
            return

        async def unfold() -> None:
            # Fetch the complete reply list: the history window may hold only
            # a tail (or none) of a long thread.
            thread = await self.app.session.load_thread(self.entry.room_id, m)
            self._thread_replies[m.event_id] = thread[1:]
            self.expanded.add(m.event_id)
            await self._reload_view(keep=m.event_id)

        self.run_worker(unfold())

    def action_collapse(self) -> None:
        """"h": fold the selected thread back up; from a reply, this lands on
        the root (in threaded view, where nothing folds, it just jumps out)."""
        if not self.messages:
            return
        m = self.messages[self.selected]
        root_id = m.thread_root or m.event_id
        if self.threaded:
            if m.thread_root:
                idx = next(
                    (
                        i
                        for i, x in enumerate(self.messages)
                        if x.event_id == root_id
                    ),
                    None,
                )
                if idx is not None:
                    self.selected = idx
                    self._highlight()
            return
        if root_id not in self.expanded:
            return
        self.expanded.discard(root_id)
        self.run_worker(self._reload_view(keep=root_id))

    # Threads opt out of the timestamp fallback below: their override only
    # trusts a marker inside the thread, so the fetch would be wasted.
    _divider_ts_fallback = True

    def _first_unread_index(self, marker_ts: int | None = None) -> int | None:
        """Index in the display list of the first message after the read
        marker captured at open, or None when everything was already read.
        Evaluated once against the opening snapshot (on_mount stores the
        chosen event id); use _first_unread_pos for later lookups."""
        if not self.messages:
            return None
        marker = self._opened_read_marker
        if marker:
            pos = next(
                (i for i, m in enumerate(self.messages) if m.event_id == marker),
                None,
            )
            if pos is not None:
                return pos + 1 if pos + 1 < len(self.messages) else None
        # The marker is not a row here (a reaction id, or beyond the loaded
        # window); its fetched timestamp is the exact read horizon.
        if marker_ts is not None:
            return next(
                (i for i, m in enumerate(self.messages) if m.ts > marker_ts),
                None,
            )
        # No marker and no timestamp: fall back to the unread count at open;
        # without either signal, treat the room as read.
        if self.entry.unread:
            return max(0, len(self.messages) - self.entry.unread)
        return None

    def _first_unread_pos(self) -> int | None:
        """Current display-list position of the divider event chosen at open,
        or None when there was none (or it is not visible in this view)."""
        if not self._first_unread_event:
            return None
        return next(
            (
                i
                for i, m in enumerate(self.messages)
                if m.event_id == self._first_unread_event
            ),
            None,
        )

    def action_first_unread(self) -> None:
        first_unread = self._first_unread_pos()
        if first_unread is None:
            self.app.notify("No unread messages.", timeout=4)
            return
        self._push_jump()  # "u" is a jump: Ctrl+O comes back here
        self.selected = first_unread
        self._highlight()

    def action_threads_on(self) -> None:
        self._toggle_threads()

    def action_threads_off(self) -> None:
        self._toggle_threads()

    def check_action(self, action: str, parameters) -> bool:
        if action == "threads_on":
            return not self.threaded
        if action == "threads_off":
            return self.threaded
        if action == "compact_on":
            return not self.compact
        if action == "compact_off":
            return self.compact
        if action == "ids_on":
            return not self.show_ids
        if action == "ids_off":
            return self.show_ids
        if action in ("edit_own", "delete_own"):
            if self.selected >= len(self.messages):
                return False
            m = self.messages[self.selected]
            return self._can_edit(m) if action == "edit_own" else self._can_delete(m)
        if action == "react":
            if self.selected >= len(self.messages):
                return False
            m = self.messages[self.selected]
            return bool(m.event_id) and not m.pending and not m.redacted_ts
        if action in ("expand", "collapse"):
            # Mirror exactly what the actions would do, so "l Unfold thread" /
            # "h Fold thread" appear only when pressing them changes anything.
            if self.selected >= len(self.messages):
                return False
            m = self.messages[self.selected]
            if action == "expand":
                if m.thread_root:
                    return False
                if self.threaded or m.event_id in self.expanded:
                    return (
                        self.selected + 1 < len(self.messages)
                        and self.messages[self.selected + 1].thread_root
                        == m.event_id
                    )
                return bool(self.thread_counts.get(m.event_id))
            if self.threaded:
                return bool(m.thread_root)
            return (m.thread_root or m.event_id) in self.expanded
        if action == "open_details":
            if self.selected >= len(self.messages):
                return False
            return self._has_history(self.messages[self.selected])
        if action == "preview_image":
            if self.selected >= len(self.messages):
                return False
            return _is_image(self.messages[self.selected])
        if action == "show_reactions":
            if self.selected >= len(self.messages):
                return False
            m = self.messages[self.selected]
            # Deleted messages hide their reactions (as on Enter), and on an
            # image the spacebar is taken by the preview: its reactions stay
            # reachable through Enter's actions menu.
            return not m.redacted_ts and not _is_image(m) and bool(
                self.app.session.reaction_summary(self.entry.room_id, m.event_id)
            )
        if action.startswith("open_"):
            actions = self._selected_actions()
            if not actions:
                return False
            if len(actions) > 1:
                return action == "open_actions"
            kind = actions[0][1][0]
            if kind == "reactions":
                # The spacebar owns the reactions popup; a second footer
                # entry promising it on Enter would be noise.
                return False
            return action == f"open_{kind}"
        return True

    def _toggle_threads(self) -> None:
        async def toggle() -> None:
            current = (
                self.messages[self.selected].event_id if self.messages else None
            )
            self.threaded = not self.threaded
            self.refresh_bindings()  # footer flips between the view labels
            # Remember the choice across rooms and restarts (state.json);
            # rooms already open keep their own view until reopened.
            session = self.app.session
            session.state["threaded_view"] = self.threaded
            session.cfg.save_state(session.state)
            messages = await self._load_messages()
            if not self.is_attached:
                return
            self.messages = messages
            # Keep the selection on the same message across the mode switch.
            idx = next(
                (i for i, m in enumerate(messages) if m.event_id == current),
                None,
            )
            self.selected = idx if idx is not None else max(0, len(messages) - 1)
            await self._redraw()

        self.run_worker(toggle())

    def _render_message(self, m, prev_sender, index):
        """One message as a left-aligned, two-column grid. The sender name is
        shown only when the speaker changes; the timestamp sits in the left
        margin in front of the first body line. In threaded view, reply
        widgets are indented as a whole via the thread-reply CSS class (see
        _redraw), which also draws the full-height thread bar on their left."""
        mine = m.sender == self.app.session.cfg.user_id
        body = (m.body or "").strip() or "[no text]"
        if self.show_ids:
            name = m.sender
        else:
            name = self.app.session.my_name if mine else m.sender_name
        name_color = MY_COLOR if mine else _sender_color(m.sender)

        grid = Table.grid(expand=True, padding=(0, 1, 0, 0))
        grid.add_column(width=5, justify="left", style="dim", vertical="top")
        grid.add_column(ratio=1, justify="left")

        if m.sender != prev_sender:
            if not self.compact and prev_sender is not None:
                grid.add_row("", "")  # blank spacer line above the name header
            grid.add_row("", Text(name, style=f"bold {name_color}"))
        if (m.reply_to or m.reply_name) and not m.redacted_ts:
            # A one-line quote of the replied-to message. The loaded window
            # is the best source (current text, display name); a target
            # outside it falls back to what the sender's text fallback said.
            target = next(
                (
                    x
                    for x in self.messages
                    if m.reply_to and x.event_id == m.reply_to
                ),
                None,
            )
            if (
                target is not None
                and not target.redacted_ts
                and (target.body or "").strip()
            ):
                if self.show_ids:
                    qname = target.sender
                else:
                    qname = (
                        self.app.session.my_name
                        if target.sender == self.app.session.cfg.user_id
                        else target.sender_name
                    )
                qtext = (target.body or "").strip().splitlines()[0]
            else:
                qname, qtext = m.reply_name, m.reply_snippet
            if qname or qtext:
                if len(qtext) > 70:
                    qtext = qtext[:70] + "…"
                quote = Text("> ", style="dim")
                if qname:
                    quote.append(qname, style="dim bold")
                    quote.append(": ", style="dim")
                quote.append(qtext, style="dim italic")
                grid.add_row("", quote)
        # A mention paints the timestamp cell red and flags the line, so a
        # scan down the left margin finds every ping.
        time_cell = (
            Text(_fmt_time(m.ts), style="bold red")
            if m.mentions_me
            else _fmt_time(m.ts)
        )
        if m.redacted_ts:
            # Deleted: the text is gone from the server, so the tombstone
            # stands in for it whether or not we still hold a copy.
            tomb = Text("this message has been deleted", style="dim italic")
            self._mark_history(tomb, m)
            grid.add_row(time_cell, tomb)
        elif m.media_url:
            label = Text()
            label.append(f"📎 {m.media_name or body}", style="bold deep_sky_blue1")
            size = _fmt_size(m.media_size)
            if size:
                label.append(f"  ({size})", style="dim")
            hint = "  Enter to download" + (", space to preview" if _is_image(m) else "")
            label.append(hint, style="dim italic")
            if m.mentions_me:
                label.append(" @", style="bold red")
            self._mark_history(label, m)
            grid.add_row(time_cell, label)
        else:
            # Only an unconfirmed local echo renders gray, so gray
            # unambiguously means "not on the server yet". _finish_send
            # flips it on confirm.
            style = "grey50" if m.pending else ""
            text = Text(body, style=style)
            for start, end, url in _find_urls(body):
                # "link <url>": Rich emits an OSC 8 hyperlink, mouse-clickable
                # where supported; the underline marks it elsewhere.
                text.stylize(f"underline deep_sky_blue1 link {url}", start, end)
            if m.mentions_me:
                text.append(" @", style="bold red")
            self._mark_history(text, m)
            grid.add_row(time_cell, text)
        count = self.thread_counts.get(m.event_id, 0)
        if count and not self.threaded and m.event_id not in self.expanded:
            label = "reply" if count == 1 else "replies"
            grid.add_row("", Text(f"⤷ {count} {label}", style="dim italic"))
        reactions = self.app.session.reaction_summary(
            self.entry.room_id, m.event_id
        )
        if reactions:
            # At most 8 distinct keys; at IOI scale a vote can gather many
            # distinct emoji and the row must stay one line.
            shown = "  ".join(f"{key} {n}" for key, n in reactions[:8])
            if len(reactions) > 8:
                shown += "  …"
            grid.add_row("", Text(shown, style="dim"))
        return grid

    def _has_history(self, m) -> bool:
        """Whether there is anything behind this message to show: an earlier
        version of one that was rewritten, or the text of a deleted one that we
        received before it went. A deletion we only ever met as a tombstone has
        nothing behind it."""
        return bool(m.edited_ts or (m.redacted_ts and m.body))

    def _mark_history(self, text: Text, m) -> None:
        """A trailing "*": this line is not the whole story, Shift+Enter has
        the rest."""
        if self._has_history(m):
            text.append(" *", style="dim")

    def action_compose(self) -> None:
        self.reply_to = None
        self._editing = None
        self._composing = True
        self.run_worker(self._redraw())

    def action_reply(self) -> None:
        if not self.messages:
            return
        m = self.messages[self.selected]
        if m.pending:
            return  # no server event id yet to hang the reply relation on
        self.reply_to = m
        self._editing = None
        self._composing = False
        self.run_worker(self._redraw())

    def action_react(self) -> None:
        """"a": react to the selected message. A digit sends from the quick
        row instantly, "/" searches every emoji by name; picking one we
        already sent takes it back."""
        if self.selected >= len(self.messages):
            return
        m = self.messages[self.selected]
        if m.pending or m.redacted_ts or not m.event_id:
            return

        def when_picked(key) -> None:
            if key:
                # An app worker: a screen worker dies with the screen, and
                # leaving the room mid-round-trip must not drop the reaction.
                self.app.run_worker(self._finish_react(m, key))

        self.app.push_screen(ReactScreen(self.entry, m), when_picked)

    async def _finish_react(self, m, key: str) -> None:
        session = self.app.session
        ok, info, added = await session.toggle_reaction(
            self.entry.room_id, m.event_id, key
        )
        if not ok:
            # The room is named: the toast can appear anywhere in the app.
            self.app.notify(
                f"Failed to react in {self.entry.title}: {info}",
                severity="error",
                timeout=10,
                markup=False,
            )
        elif added:
            # Count the pick so the quick row converges on the reactions
            # actually used; removals don't count against it.
            usage = session.state.setdefault("reaction_usage", {})
            usage[key] = usage.get(key, 0) + 1
            session.cfg.save_state(session.state)
        if self.is_attached:
            await self._reload_view(keep=m.event_id)

    def action_edit_own(self) -> None:
        """"e": rewrite the selected own message; the composer opens with its
        current text and sends an m.replace."""
        if self.selected >= len(self.messages):
            return
        m = self.messages[self.selected]
        if not self._can_edit(m):
            return
        self._editing = m
        self.reply_to = None
        self._composing = True
        self.run_worker(self._redraw())

    def action_delete_own(self) -> None:
        """"d": delete the selected own message, behind a confirm gate (a
        deletion cannot be undone by anyone)."""
        if self.selected >= len(self.messages):
            return
        m = self.messages[self.selected]
        if not self._can_delete(m):
            return

        def when_answered(yes) -> None:
            if yes:
                # An app worker: a screen worker dies with the screen, and
                # leaving the room mid-round-trip must not skip the deletion.
                self.app.run_worker(self._finish_delete(m))

        first_line = ((m.body or "").strip().splitlines() or [""])[0][:60]
        self.app.push_screen(
            ConfirmScreen(f"Delete this message?\n\n{first_line}"), when_answered
        )

    async def _finish_delete(self, m) -> None:
        ok, info = await self.app.session.redact(self.entry.room_id, m.event_id)
        if not ok:
            # The room is named: the toast can appear anywhere in the app.
            self.app.notify(
                f"Failed to delete in {self.entry.title}: {info}",
                severity="error",
                timeout=10,
                markup=False,
            )
        if self.is_attached:
            await self._reload_view(keep=m.event_id)

    def _can_edit(self, m) -> bool:
        return (
            m.sender == self.app.session.cfg.user_id
            and not m.pending
            and not m.redacted_ts
            and not m.media_url  # only text is rewritten, not uploads
        )

    def _can_delete(self, m) -> bool:
        return (
            m.sender == self.app.session.cfg.user_id
            and not m.pending
            and not m.redacted_ts
        )

    def _actions_for(self, m) -> list[tuple[str, tuple[str, str]]]:
        """Everything Enter could do with one message, as (menu label, (kind,
        argument)) pairs, in the order they are offered. Its history is not
        among them: that is Shift+Enter's job, so neither key has to guess
        which of the two you meant."""
        if m.redacted_ts:
            # Nothing survives a deletion to download or follow.
            return []
        actions = []
        if m.media_url:
            size = _fmt_size(m.media_size)
            name = m.media_name or (m.body or "").strip() or "file"
            actions.append(
                (f"Download {name}" + (f" ({size})" if size else ""), ("download", ""))
            )
        actions.extend(
            (f"Open {url}", ("link", url)) for _, _, url in _find_urls(m.body or "")
        )
        if self.app.session.reaction_summary(self.entry.room_id, m.event_id):
            actions.append(("Who reacted", ("reactions", "")))
        return actions

    def _selected_actions(self) -> list[tuple[str, tuple[str, str]]]:
        # check_action runs on every footer redraw, which can land between a
        # reload and the selection being clamped to it.
        if self.selected >= len(self.messages):
            return []
        return self._actions_for(self.messages[self.selected])

    # ThreadScreen turns this off: a thread's "/" searches only the thread
    # itself, never the whole room archive underneath it.
    _archive_search = True

    def _search_corpus(self) -> list:
        """Every message this client holds for the room: the downloaded
        archive merged with the live window and anything paged back this
        visit, edits folded, thread replies included (the display list drops
        those in normal view; search must not)."""
        if not self._archive_search:
            return list(self.messages)
        session = self.app.session
        merged = {
            m.event_id: m for m in session.archive_rows(self.entry.room_id)
        }
        rows = [
            *session.timelines.get(self.entry.room_id, ()),
            *self.older,
            *self.messages,
        ]
        for m in rows:
            if m.event_id and not m.pending:
                merged[m.event_id] = m
        return fold_edits(
            sorted(merged.values(), key=lambda m: (m.ts, m.event_id))
        )

    def action_search_room(self) -> None:
        """"/": search everything held locally and jump to the picked hit."""
        session = self.app.session
        corpus = self._search_corpus()
        downloading = bool(
            self._archive_search
            and session.cache_allowed(self.entry.room_id)
            and self.entry.room_id not in session.archive_done
        )

        def when_picked(event_id) -> None:
            if not event_id:
                return
            idx = next(
                (
                    i
                    for i, m in enumerate(self.messages)
                    if m.event_id == event_id
                ),
                None,
            )
            if idx is not None:
                self.selected = idx
                self._highlight()
                return
            hit = next((m for m in corpus if m.event_id == event_id), None)
            if hit is not None:
                self.run_worker(
                    self._jump_to_hit(hit, corpus),
                    group="browse",
                    exclusive=True,
                )

        where = (
            self.entry.title
            if self._archive_search
            else f"this thread ({self.entry.title})"
        )
        self.app.push_screen(
            RoomSearchScreen(corpus, downloading, where), when_picked
        )

    async def _jump_to_hit(self, hit, corpus: list) -> None:
        self._push_jump()
        await self._land_on_event(hit.event_id, hit.thread_root, corpus)

    async def _land_on_event(
        self, event_id: str, thread_root: str | None, corpus: list | None = None
    ) -> None:
        """Land the selection on a message the display list may not contain
        (a search hit, a ":N" target, a jumplist spot): unfold its thread
        and reload; if still not visible, detach into archive browse mode
        (as "g" does) with the window grown just far enough to cover it.
        "G" reattaches as usual."""
        if thread_root:
            self.expanded.add(thread_root)
        await self._reload_view(keep=event_id)
        if (
            self.messages
            and self.selected < len(self.messages)
            and self.messages[self.selected].event_id == event_id
        ):
            return
        rows = (
            self._browse
            or corpus
            or self.app.session.archive_rows(self.entry.room_id)
        )
        pos = next(
            (i for i, m in enumerate(rows) if m.event_id == event_id), None
        )
        if pos is None:
            return  # gone from the snapshot being browsed: nowhere to land
        # A bounded window around the hit: a deep hit in a big room must not
        # mount thousands of rows in one go, but the page below the landing
        # spot must be filled too (":1" showing a single message until "j"
        # extends it reads as broken). "k" at the top and "j" at the bottom
        # grow it chunk by chunk as usual.
        self._browse = rows
        start = max(0, pos + 1 - self.BROWSE_CHUNK // 2)
        self._browse_upto = min(len(rows), start + self.BROWSE_CHUNK)
        if thread_root:
            # The reply renders under its root; keep the root in the window
            # even when the thread is longer than a whole chunk.
            root_pos = next(
                (
                    i
                    for i, m in enumerate(rows)
                    if m.event_id == thread_root
                ),
                None,
            )
            if root_pos is not None:
                start = min(start, root_pos)
        self._browse_start = start
        self.older = rows[self._browse_start : self._browse_upto]
        messages = await self._load_messages()
        if not self.is_attached:
            return
        self.messages = messages
        idx = next(
            (i for i, m in enumerate(messages) if m.event_id == event_id),
            None,
        )
        if idx is None and thread_root:
            # The reply itself cannot render (its root is missing from the
            # browsed rows); settle for the closest visible position.
            idx = next(
                (
                    i
                    for i, m in enumerate(messages)
                    if m.event_id == thread_root
                ),
                None,
            )
        self.selected = idx if idx is not None else max(0, len(messages) - 1)
        await self._redraw()

    def action_open_details(self) -> None:
        if not self.messages:
            return
        m = self.messages[self.selected]
        if self._has_history(m):
            self.app.push_screen(HistoryScreen(self.entry, m))

    def action_preview_image(self) -> None:
        """Space on an image upload: render it right in the terminal."""
        if self.selected >= len(self.messages):
            return
        if not _is_image(self.messages[self.selected]):
            return

        def when_closed(event_id) -> None:
            # j/k inside the preview walked to another image: land the
            # timeline selection on the one last shown.
            idx = next(
                (i for i, m in enumerate(self.messages) if m.event_id == event_id),
                None,
            )
            if idx is not None and idx != self.selected:
                self.selected = idx
                self._highlight()

        self.app.push_screen(
            PreviewScreen(list(self.messages), self.selected, self.entry.room_id),
            when_closed,
        )

    def action_show_reactions(self) -> None:
        """Space on a reacted message: who is behind each badge."""
        if self.selected >= len(self.messages):
            return
        m = self.messages[self.selected]
        if m.redacted_ts or _is_image(m):
            return
        if self.app.session.reaction_summary(self.entry.room_id, m.event_id):
            self.app.push_screen(ReactionsScreen(self.entry, m))

    # Three names for one key: check_action enables exactly the one that
    # describes what Enter will do here, so the footer names it.
    def action_open_download(self) -> None:
        self._open_selected()

    def action_open_link(self) -> None:
        self._open_selected()

    def action_open_actions(self) -> None:
        self._open_selected()

    def _open_selected(self) -> None:
        """Enter on the selected message: download an uploaded file or open a
        link in the browser, asking which first when it offers several. The
        history behind a "*" is Shift+Enter's job (action_open_details)."""
        actions = self._selected_actions()
        if not actions:
            return
        m = self.messages[self.selected]
        if len(actions) == 1:
            self._run_action(m, actions[0][1])
            return

        def when_picked(action) -> None:
            if action:
                self._run_action(m, action)

        self.app.push_screen(ActionScreen(actions), when_picked)

    def _run_action(self, m, action: tuple[str, str]) -> None:
        kind, argument = action
        if kind == "download":

            def when_chosen(directory: str | None) -> None:
                if directory:
                    # App worker: leaving the room must not abort a save the
                    # user asked for.
                    self.app.run_worker(self._download(m, directory))

            self.app.push_screen(DownloadScreen(m), when_chosen)
        elif kind == "reactions":
            self.app.push_screen(ReactionsScreen(self.entry, m))
        else:
            self._open_url(argument)

    def _open_url(self, url: str) -> None:
        """Hand the URL to the desktop's default handler (Safari, or whatever
        is set on this machine). Started detached and without a shell: the
        handler must never inherit our terminal, block the UI, or let a
        remote sender's URL turn into shell words."""
        try:
            if sys.platform == "win32":
                os.startfile(url)  # Windows' own URL handler, no shell involved
            else:
                subprocess.Popen(
                    ["open" if sys.platform == "darwin" else "xdg-open", url],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
        except Exception as exc:
            self.app.notify(
                f"Could not open link: {exc}",
                severity="error",
                timeout=8,
                markup=False,
            )
            return
        self.app.notify(f"Opening {url}", timeout=4, markup=False)

    async def _download(self, m, directory: str) -> None:
        try:
            ok, result = await self.app.session.download_media(m, directory)
        except Exception as exc:
            ok, result = False, str(exc)
        # No is_attached gate: nothing below touches the screen, and the
        # outcome of a download must be reported even after leaving the room.
        if ok:
            session = self.app.session
            session.state["download_dir"] = directory
            session.cfg.save_state(session.state)
            # markup=False: result holds a filename; "[" in it would raise
            # MarkupError inside the toast and crash the app.
            self.app.notify(f"Saved {result}", timeout=6, markup=False)
        else:
            self.app.notify(
                f"Download failed: {result}",
                severity="error",
                timeout=8,
                markup=False,
            )

    def action_thread(self) -> None:
        """Open the selected message's thread; on a message without one this
        starts a new thread (the first sent reply creates it). On a thread
        reply this opens the thread it belongs to: threads do not nest, and
        rooting a new thread at a reply would send spec-invalid relations
        that other clients render as a detached bogus thread."""
        if not self.messages:
            return
        root = self.messages[self.selected]
        if root.pending:
            return  # rooting a thread needs the server-assigned event id
        if root.thread_root:
            root_id = root.thread_root
            root = next(
                (m for m in self.messages if m.event_id == root_id),
                next(
                    (
                        m
                        for m in self.app.session.timelines.get(
                            self.entry.room_id, ()
                        )
                        if m.event_id == root_id
                    ),
                    None,
                ),
            )
            if root is None:
                self.app.notify(
                    "The thread's first message is not loaded.", timeout=4
                )
                return
        if not root.event_id:
            return
        self.app.push_screen(ThreadScreen(self.entry, root))

    def action_back(self) -> None:
        if self.reply_to is not None or self._composing:
            self._cancel_editor()
        else:
            self.app.pop_screen()

    def _cancel_editor(self) -> None:
        # Stash (or clear) the draft before the composer unmounts: Escape
        # must never cost a paragraph. Cancelled edits are not stashed: their
        # text is the message's own, not a draft.
        try:
            text = self.query_one("#editor", ComposerArea).text
        except Exception:
            text = ""
        if self._editing is None:
            if text.strip():
                self.app.session.drafts[self._draft_key()] = text
            else:
                self.app.session.drafts.pop(self._draft_key(), None)
        self._editing = None
        self.reply_to = None
        self._composing = False
        self.set_focus(None)
        self.run_worker(self._redraw())

    def on_composer_area_cancelled(self, event: ComposerArea.Cancelled) -> None:
        self._cancel_editor()

    def _send_kwargs(self, reply) -> dict:
        """Relation arguments for session.send. ThreadScreen overrides this to
        attach the thread relation; here, replying to a thread reply (visible
        in threaded view) lands the message in that thread too."""
        if reply is not None and reply.thread_root:
            return {"reply_to": reply.event_id, "thread_root": reply.thread_root}
        return {"reply_to": reply.event_id if reply else None}

    async def on_composer_area_submitted(self, event: ComposerArea.Submitted) -> None:
        if self._editing is not None:
            target, self._editing = self._editing, None
            self._composing = False
            text = event.value.strip()
            await self._redraw()
            # Same body or emptied: nothing to send. (Deleting is "d", not an
            # empty edit.)
            if text and text != target.body:
                self.app.run_worker(self._finish_edit(target, text))
            return
        # Whatever happens next, the draft is spoken for: it is either being
        # sent or was consciously emptied.
        self.app.session.drafts.pop(self._draft_key(), None)
        text = event.value.strip()
        if not text:
            self.reply_to = None
            self._composing = False
            await self._redraw()
            return
        reply = self.reply_to
        # Capture the relation arguments before touching any state: the
        # thread override reads self.messages for the latest-reply fallback.
        kwargs = self._send_kwargs(reply)
        # Optimistic local echo: show it gray and close the editor now, do
        # the network round-trip in a worker. The provisional "~local." id
        # keeps it distinct until the server assigns the real one.
        echo = Message(
            sender=self.app.session.cfg.user_id,
            sender_name=self.app.session.my_name,
            body=text,
            ts=int(time.time() * 1000),
            event_id=f"~local.{uuid4().hex}",
            thread_root=kwargs.get("thread_root") or "",
            reply_to=reply.event_id if reply else "",
            pending=True,
        )
        self._pending.append(echo)
        self.messages.append(echo)
        self.reply_to = None
        self._composing = False
        self.selected = len(self.messages) - 1
        await self._redraw()
        # An app worker: unmounting cancels a screen's workers, so an Escape
        # during the round-trip would kill a screen-owned send mid-flight.
        self.app.run_worker(self._finish_send(echo, text, reply, kwargs))

    async def _finish_edit(self, target, text: str) -> None:
        ok, info = await self.app.session.send_edit(
            self.entry.room_id, target, text
        )
        if not ok:
            # Report even after leaving the room, and park the rewrite in
            # the drafts so it is not lost.
            self.app.session.drafts[self._draft_key()] = text
            self.app.notify(
                f"Failed to edit in {self.entry.title}: {info}",
                severity="error",
                timeout=10,
                markup=False,
            )
        if self.is_attached:
            await self._reload_view(keep=target.event_id)

    async def _finish_send(self, echo, text: str, reply, kwargs: dict) -> None:
        ok, info = await self.app.session.send(self.entry.room_id, text, **kwargs)
        if echo in self._pending:
            self._pending.remove(echo)
        if not ok:
            # Before the is_attached gate: a failure must be reported even
            # after leaving the room. The room is named because the toast
            # can appear anywhere in the app.
            self.app.notify(
                f"Failed to send in {self.entry.title}: {info}",
                severity="error",
                timeout=10,
                markup=False,
            )
        if not self.is_attached:
            return
        if ok:
            # Deliberately NOT recorded as _last_seen_event: the sync batch
            # delivering our event can carry another user's message, and
            # pre-marking ours as seen would make refresh_messages skip the
            # batch. The extra refresh is cheap.
            if any(m.event_id == info for m in self.messages):
                # The sync echo landed during the round-trip; drop the local
                # copy instead of showing the message twice.
                if echo in self.messages:
                    self.messages.remove(echo)
            else:
                # Confirm in place: the real event id makes later reloads
                # merge it with the sync echo, and clearing pending flips the
                # rendering from gray to the normal color.
                echo.event_id = info
                echo.pending = False
            self.selected = min(self.selected, max(0, len(self.messages) - 1))
        else:
            if echo in self.messages:
                self.messages.remove(echo)
            self.selected = min(self.selected, max(0, len(self.messages) - 1))
            # Reopen the editor the send came from so the text survives and
            # the user can retry or copy it out.
            self.reply_to = reply
            self._composing = reply is None
        await self._redraw()
        if not ok:
            # The redraw above reopened the composer empty; refill it with
            # the failed text unless a new draft was already started.
            try:
                editor = self.query_one("#editor", ComposerArea)
                if not editor.text:
                    editor.text = text
                    editor.move_cursor(editor.document.end)
            except Exception:
                pass


class ThreadScreen(RoomScreen):
    """A single thread: the root message plus its replies, with a composer
    that sends into the thread. Pushed on top of the RoomScreen with "T";
    Escape goes back to the room."""

    def __init__(self, entry: Entry, root) -> None:
        super().__init__(entry)
        self.root = root
        self._thread_seen: tuple | None = None  # see refresh_messages

    def on_screen_resume(self) -> None:
        _refresh_terminal_title(
            self.app, f"Thread in {self.entry.title} - matrixcli"
        )

    def action_first_message(self) -> None:
        # A thread is loaded whole via /relations; "g" is a plain jump to
        # its root, never the base class's archive browse.
        n = self._take_count()
        self._push_jump()
        if n is not None:
            self.run_worker(self._goto_index(n))
            return
        if self.messages:
            self.selected = 0
            self._highlight()

    async def _goto_index(self, n: int) -> None:
        # The whole thread is already loaded: ":N" is a plain index jump.
        if self.messages:
            self.selected = max(0, min(n - 1, len(self.messages) - 1))
            self._highlight()

    async def _land_on_event(self, event_id, thread_root, corpus=None) -> None:
        # No archive browsing inside a thread; land only on what is here.
        idx = next(
            (i for i, m in enumerate(self.messages) if m.event_id == event_id),
            None,
        )
        if idx is not None:
            self.selected = idx
            self._highlight()

    async def on_mount(self) -> None:
        # Open with the composer ready: a thread is usually opened to reply.
        self._composing = True
        await super().on_mount()
        self.title = f"Thread in {self.entry.title}"
        # splitlines() of a whitespace-only body is [], so guard the [0]: a
        # remote sender controls the body and " " must not crash the screen.
        lines = (self.root.body or "").strip().splitlines() or [""]
        self.sub_title = lines[0][:60]
        self._thread_seen = self._thread_evidence()

    def _thread_evidence(self) -> tuple:
        """What the local caches know about this thread: new replies, edits,
        deletions, and reactions all leave a trace here, because they arrive
        through the room's sync stream like any other event."""
        session = self.app.session
        root_id = self.root.event_id
        shown = {m.event_id for m in self.messages if m.event_id}
        rows = tuple(
            (m.event_id, m.edited_ts, m.redacted_ts)
            for m in session.timelines.get(self.entry.room_id, ())
            if m.event_id == root_id
            or m.thread_root == root_id
            or (m.replaces and (m.replaces == root_id or m.replaces in shown))
        )
        reacts = tuple(
            tuple(session.reaction_summary(self.entry.room_id, m.event_id))
            for m in self.messages
            if m.event_id
        )
        return (rows, reacts)

    async def refresh_messages(self) -> None:
        # Reloading here refetches the whole thread over /relations (seconds
        # on a loaded homeserver), and nearly all room events have nothing
        # to do with this thread. Only pay when the local caches show
        # evidence it changed; a quiet tick still advances the seen marker
        # and the read receipt.
        if not self.is_attached or not self._loaded:
            return
        latest = self.app.session.last_event_id.get(self.entry.room_id)
        if not latest or latest == self._last_seen_event:
            return
        evidence = self._thread_evidence()
        if evidence == self._thread_seen:
            self._last_seen_event = latest
            await self.app.session.mark_read(self.entry.room_id)
            return
        self._thread_seen = evidence
        await super().refresh_messages()

    async def _load_messages(self, cached_only: bool = False) -> list:
        # A thread is fetched whole via /relations; there is no local cache
        # to serve a quick first paint (or a name refresh) from.
        if cached_only:
            return []
        thread = await self.app.session.load_thread(self.entry.room_id, self.root)
        return self._splice_pending(fold_edits(thread))

    def _composer_title(self) -> str:
        if self.reply_to is None:
            return "Reply in thread"
        return super()._composer_title()

    def _draft_key(self) -> str:
        # Distinct from the room's, so a thread draft and a room draft in the
        # same room do not overwrite each other.
        return f"{self.entry.room_id}:{self.root.event_id}"

    _divider_ts_fallback = False

    def _first_unread_index(self, marker_ts: int | None = None) -> int | None:
        """The room-level unread count counts main-timeline events, so it says
        nothing about this thread; only an explicit read marker that lands
        inside the thread can place the divider meaningfully."""
        marker = self._opened_read_marker
        if not marker or not self.messages:
            return None
        pos = next(
            (i for i, m in enumerate(self.messages) if m.event_id == marker),
            None,
        )
        if pos is None or pos + 1 >= len(self.messages):
            return None
        return pos + 1

    def _send_kwargs(self, reply) -> dict:
        # Skip in-flight echoes: their provisional "~local." ids must never
        # leave the client as the reply-fallback event id.
        latest = next(
            (m.event_id for m in reversed(self.messages) if not m.pending),
            "",
        )
        return {
            "reply_to": reply.event_id if reply else None,
            "thread_root": self.root.event_id,
            "thread_latest": latest or self.root.event_id,
        }

    def check_action(self, action: str, parameters) -> bool:
        # Threads do not nest, and this screen IS a thread view; hide the
        # inherited thread-open, view-toggle, and fold/unfold bindings.
        if action in ("thread", "threads_on", "threads_off", "expand", "collapse"):
            return False
        return super().check_action(action, parameters)

    # "/" here searches the thread that is on screen, not the room archive:
    # the room screen underneath already offers the whole-room search.
    _archive_search = False

    def _fetch_older(self) -> None:
        # A thread is loaded whole via /relations; there is nothing older to
        # paginate at the room level from here.
        pass

    def _reset_history_position(self) -> None:
        # The room screen beneath this thread still owns its back-paginated
        # window; resetting the room's token from here would strand it.
        pass


class PickerScreen(ModalScreen):
    """Shared keys for the pick-one-row modals: j/k next to the arrows (the
    room view is driven with them, so the popups it opens should be too), and
    Escape to dismiss with None. Subclasses compose exactly one ListView and
    dismiss with the chosen row's value."""

    BINDINGS = [
        ("escape", "cancel", "Cancel"),
        Binding("j", "cursor_down", "Down", show=False),
        Binding("k", "cursor_up", "Up", show=False),
    ]

    def action_cancel(self) -> None:
        self.dismiss(None)

    def action_cursor_down(self) -> None:
        self.query_one(ListView).action_cursor_down()

    def action_cursor_up(self) -> None:
        self.query_one(ListView).action_cursor_up()


class DownloadScreen(PickerScreen):
    """Pick a destination directory for a file download: the last-used
    directory first (when there is one), then ~/Desktop, ~/Downloads, and the
    current directory. j/k or up/down to choose, Enter to download, Escape to
    cancel; dismisses with the chosen path string or None."""

    def __init__(self, message) -> None:
        super().__init__()
        self.message = message

    def compose(self) -> ComposeResult:
        with Vertical(id="downloadbox"):
            # escape(): the filename comes from the sender; unescaped "[" is
            # parsed as console markup (crash, spoofed styling). Truncated so
            # a long name cannot blow out the dialog.
            raw = (self.message.media_name or self.message.body or "file")[:120]
            name = escape(raw)
            size = _fmt_size(self.message.media_size)
            title = f"Download {name}" + (f" ({size})" if size else "") + " to:"
            yield Label(title, id="downloadtitle")
            yield ListView(id="dirs")

    async def on_mount(self) -> None:
        last = self.app.session.state.get("download_dir")
        options = [last] if last else []
        for candidate in ("~/Desktop", "~/Downloads", "."):
            expanded = str(Path(candidate).expanduser().resolve())
            if all(
                expanded != str(Path(o).expanduser().resolve()) for o in options
            ):
                options.append(candidate)
        lv = self.query_one("#dirs", ListView)
        for option in options:
            item = ListItem(Label(option))
            item.dir_path = option
            await lv.append(item)
        lv.index = 0
        lv.focus()

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        self.dismiss(getattr(event.item, "dir_path", None))


class ActionScreen(PickerScreen):
    """Which of several things Enter should do with the selected message: open
    one of its links, or download its file. j/k or up/down to choose, Enter to
    run it, Escape to cancel; dismisses with the chosen (kind, argument) pair
    or None."""

    def __init__(self, actions: list[tuple[str, tuple[str, str]]]) -> None:
        super().__init__()
        self.actions = actions

    def compose(self) -> ComposeResult:
        with Vertical(id="actionbox"):
            yield Label("What would you like to do?", id="actiontitle")
            yield ListView(id="actions")

    async def on_mount(self) -> None:
        lv = self.query_one("#actions", ListView)
        for label, action in self.actions:
            # escape(): labels quote sender-controlled text (URLs, filenames);
            # unescaped "[" is parsed as console markup. Truncated to fit the
            # dialog.
            item = ListItem(Label(escape(label[:200])))
            item.action = action
            await lv.append(item)
        lv.index = 0
        lv.focus()

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        self.dismiss(getattr(event.item, "action", None))


class PreviewScreen(ModalScreen):
    """An image upload rendered in the terminal (space on an image message):
    truecolor half-blocks or ASCII art, "~" flips between them (remembered
    in state.json). j/k walk the room's images; Escape or q closes,
    dismissing with the event id last shown so the timeline selection can
    follow."""

    BINDINGS = [
        ("escape", "cancel", "Close"),
        Binding("q", "cancel", "Close", show=False),
        # The key that opened the preview closes it: space is a toggle.
        Binding("space", "cancel", "Close", show=False),
        # Gated by check_action to whether another image exists in that
        # direction, so the footer drops them at the ends of the gallery.
        ("j", "next_image", "Next image"),
        ("k", "prev_image", "Previous image"),
        Binding("down", "next_image", "Next image", show=False),
        Binding("up", "prev_image", "Previous image", show=False),
        # Same "~" pattern as the room screen: the enabled label names the
        # style currently on screen.
        Binding("tilde", "mode_blocks", "Style: ascii"),
        Binding("tilde", "mode_ascii", "Style: blocks"),
    ]

    def __init__(self, messages: list, index: int, room_id: str = "") -> None:
        super().__init__()
        self.messages = messages  # a snapshot of the room's rows, for j/k
        self.index = index  # the row on show; always an image message
        self.room_id = room_id  # gates the on-disk media cache per room
        self.image = None  # a PIL image once fetched and decoded
        self.error = ""
        self._cache = {}  # event id -> decoded image, so j/k never refetches
        self._seq = 0  # fetch generation: a stale in-flight fetch must not paint

    @property
    def message(self):
        return self.messages[self.index]

    @property
    def mode(self) -> str:
        # 16-color terminals get no say: block art needs at least the
        # 256-color palette to dither onto.
        if self._depth() == "basic":
            return "ascii"
        mode = self.app.session.state.get("preview_mode")
        return mode if mode in ("ascii", "blocks") else "blocks"

    def _depth(self) -> str:
        return _color_depth(self.app.console.color_system)

    def _title_text(self) -> Text:
        # Text(), not markup: the filename comes from the sender, and Text
        # keeps any "[" in it literal. Truncated so it cannot blow out the row.
        text = Text((self.message.media_name or self.message.body or "image")[:120])
        depth = self._depth()
        if depth == "256":
            # Name the degradation, and the way out for terminals that do
            # support 24-bit color but do not advertise it.
            text.append(
                "  · 256-color terminal, dithered (COLORTERM=truecolor may help)",
                style="dim",
            )
        elif depth == "basic":
            text.append("  · 16-color terminal, ASCII art only", style="dim")
        return text

    def compose(self) -> ComposeResult:
        with Vertical(id="previewbox"):
            yield Label(self._title_text(), id="previewtitle")
            yield Static(Text("fetching image...", style="dim italic"), id="previewart")

    def on_mount(self) -> None:
        # In a worker, so Escape closes the popup even while the fetch is in
        # flight; until it lands the box shows its placeholder line.
        self.run_worker(self._fetch())

    async def _fetch(self) -> None:
        seq = self._seq
        message = self.message
        image, error = self._cache.get(message.event_id), ""
        if image is None:
            # Ask for a thumbnail at twice the window size (half-blocks
            # paint two pixel rows per cell): servers snap to pre-generated
            # buckets, and the headroom keeps a portrait photo out of a tiny
            # upscaled one. Encrypted media falls back to the full download.
            try:
                ok, result = await self.app.session.fetch_preview_bytes(
                    message,
                    self.app.size.width * 2,
                    self.app.size.height * 4,
                    self.room_id,
                )
            except Exception as exc:
                ok, result = False, str(exc)
            if ok:
                # Imports inside the try: a broken Pillow install must
                # degrade to an error line, not crash the worker.
                try:
                    from PIL import Image

                    image = Image.open(BytesIO(result))
                    image.load()  # decode now: errors must land here, not mid-render
                except Exception as exc:
                    image, error = None, f"could not decode image: {exc}"
                else:
                    self._cache[message.event_id] = image  # GIFs: first frame
            else:
                error = str(result)
        if seq != self._seq or not self.is_attached:
            return  # j/k moved on (or the popup closed) while this was in flight
        self.image, self.error = image, error
        self._render_art()

    def _render_art(self) -> None:
        art = self.query_one("#previewart", Static)
        if self.error:
            art.update(Text(f"Preview failed: {self.error}", style="bold red"))
            return
        if self.image is None:
            return  # still fetching; the placeholder line stays up
        box = self.query_one("#previewbox", Vertical)
        w = max(2, box.content_size.width)
        h = max(2, box.content_size.height - 1)  # minus the title line
        # A render failure must land in the popup as text: this runs on
        # plain UI paths like resize, outside any worker's safety net.
        try:
            if self.mode == "blocks":
                art.update(
                    _block_art(self.image, w, h, dither=self._depth() == "256")
                )
            else:
                art.update(
                    _ascii_art(self.image, w, h, self.app.session.cfg.ascii_ramp)
                )
        except Exception as exc:
            art.update(Text(f"Preview failed: {exc}", style="bold red"))

    def on_resize(self, event) -> None:
        # Deferred past the layout pass: this Resize arrives before the
        # children have their new sizes.
        self.call_after_refresh(self._render_art)

    def _image_index(self, delta: int) -> int | None:
        """The row index of the nearest image message in that direction, or
        None when the current one is the last of them."""
        i = self.index + delta
        while 0 <= i < len(self.messages):
            if _is_image(self.messages[i]):
                return i
            i += delta
        return None

    def _go(self, delta: int) -> None:
        target = self._image_index(delta)
        if target is None:
            return
        self.index = target
        self._seq += 1  # orphan any fetch still in flight for the old image
        self.image, self.error = None, ""
        self.query_one("#previewtitle", Label).update(self._title_text())
        self.query_one("#previewart", Static).update(
            Text("fetching image...", style="dim italic")
        )
        self.refresh_bindings()  # the ends of the gallery drop a j/k label
        self.run_worker(self._fetch())

    def action_next_image(self) -> None:
        self._go(1)

    def action_prev_image(self) -> None:
        self._go(-1)

    def check_action(self, action: str, parameters) -> bool:
        if action in ("mode_blocks", "mode_ascii"):
            if self._depth() == "basic":
                return False  # no block style to toggle to; hide the pair
            if action == "mode_blocks":
                return self.mode == "ascii"
            return self.mode == "blocks"
        if action == "next_image":
            return self._image_index(1) is not None
        if action == "prev_image":
            return self._image_index(-1) is not None
        return True

    def _set_mode(self, mode: str) -> None:
        session = self.app.session
        session.state["preview_mode"] = mode
        session.cfg.save_state(session.state)
        self.refresh_bindings()  # footer flips between the style labels
        self._render_art()

    def action_mode_blocks(self) -> None:
        self._set_mode("blocks")

    def action_mode_ascii(self) -> None:
        self._set_mode("ascii")

    def action_cancel(self) -> None:
        # The event id last on show, so the room lands its selection there
        # after a j/k walk through the gallery.
        self.dismiss(self.message.event_id)


class HistoryScreen(ModalScreen):
    """What the timeline is not showing for one message: every version of a
    rewritten one, oldest first, or the text of a deleted one as we last
    received it. Escape or Enter closes it."""

    BINDINGS = [
        ("escape", "dismiss", "Close"),
        ("enter", "dismiss", "Close"),
        # The version list can outgrow the box; same keys as everywhere else.
        Binding("j", "scroll_versions(1)", "Down", show=False),
        Binding("k", "scroll_versions(-1)", "Up", show=False),
        Binding("down", "scroll_versions(1)", "Down", show=False),
        Binding("up", "scroll_versions(-1)", "Up", show=False),
    ]

    def action_scroll_versions(self, direction: int) -> None:
        box = self.query_one("#versions", VerticalScroll)
        box.scroll_relative(y=direction, animate=False)

    def __init__(self, entry: Entry, message) -> None:
        super().__init__()
        self.entry = entry
        self.message = message

    def compose(self) -> ComposeResult:
        # escape(): the display name comes from the sender, and unescaped a
        # "[" in it is parsed as console markup.
        what = "Deleted message" if self.message.redacted_ts else "Edit history"
        with Vertical(id="editsbox"):
            yield Label(f"{what}: {escape(self.message.sender_name)}", id="editstitle")
            with VerticalScroll(id="versions"):
                yield Static(Text("fetching earlier versions...", style="dim italic"))

    def on_mount(self) -> None:
        # In a worker, so Escape closes the popup even while the fetch is in
        # flight; until it lands the box shows its placeholder line.
        self.run_worker(self._fill())

    async def _fill(self) -> None:
        versions = await self.app.session.load_edits(
            self.entry.room_id, self.message
        )
        if not self.is_attached:
            return
        box = self.query_one("#versions", VerticalScroll)
        await box.remove_children()
        deleted = bool(self.message.redacted_ts)
        for i, v in enumerate(versions):
            grid = Table.grid(expand=True, padding=(0, 1, 0, 0))
            grid.add_column(width=5, justify="left", style="dim", vertical="top")
            grid.add_column(ratio=1, justify="left")
            # The bodies are remote text: Text() (not markup) keeps "[b" and
            # friends literal, exactly as the timeline renders them.
            text = Text((v.body or "").strip() or "[no text]")
            if i == len(versions) - 1 and len(versions) > 1 and not deleted:
                text.append("  (shown in the timeline)", style="dim italic")
            grid.add_row(_fmt_time(v.ts), text)
            await box.mount(Static(grid))
            if i != len(versions) - 1:
                await box.mount(Rule(line_style="dashed"))
        if deleted:
            # Say plainly that this is a local copy: the server has dropped the
            # content, and closing matrixcli drops it here too.
            note = Text(
                f"deleted {_fmt_time(self.message.redacted_ts).strip()} — kept "
                "only in this session",
                style="dim italic",
            )
            await box.mount(Rule(line_style="dashed"), Static(note))


class ReactionsScreen(ModalScreen):
    """Who is behind each reaction badge on one message: a line per emoji
    with the names of everyone who sent it. Escape, Enter, or the spacebar
    that opened it closes it."""

    BINDINGS = [
        ("escape", "dismiss", "Close"),
        ("enter", "dismiss", "Close"),
        # The key that opened the popup closes it: space is a toggle.
        Binding("space", "dismiss", "Close", show=False),
        # A well-voted message can outgrow the box; same keys as everywhere.
        Binding("j", "scroll_reactors(1)", "Down", show=False),
        Binding("k", "scroll_reactors(-1)", "Up", show=False),
        Binding("down", "scroll_reactors(1)", "Down", show=False),
        Binding("up", "scroll_reactors(-1)", "Up", show=False),
    ]

    def action_scroll_reactors(self, direction: int) -> None:
        box = self.query_one("#reactors", VerticalScroll)
        box.scroll_relative(y=direction, animate=False)

    def __init__(self, entry: Entry, message) -> None:
        super().__init__()
        self.entry = entry
        self.message = message

    def compose(self) -> ComposeResult:
        # escape(): the display name comes from the sender, and unescaped a
        # "[" in it is parsed as console markup.
        with Vertical(id="reactionsbox"):
            yield Label(
                f"Reactions: {escape(self.message.sender_name)}",
                id="reactionstitle",
            )
            yield VerticalScroll(id="reactors")

    async def on_mount(self) -> None:
        session = self.app.session
        detail = session.reaction_detail(
            self.entry.room_id, self.message.event_id
        )
        box = self.query_one("#reactors", VerticalScroll)
        if not detail:
            # The last reaction can be withdrawn between the footer offering
            # the popup and it opening.
            await box.mount(Static(Text("no reactions", style="dim italic")))
            return
        for key, senders in detail:
            grid = Table.grid(expand=True, padding=(0, 1, 0, 0))
            grid.add_column(width=5, justify="left", style="dim", vertical="top")
            grid.add_column(ratio=1, justify="left")
            names = Text()
            for i, (sender, name) in enumerate(senders):
                if i:
                    names.append(", ", style="dim")
                # The same colors the timeline gives these people's names, so
                # the popup reads as the same cast.
                if sender == session.cfg.user_id:
                    names.append(session.my_name, style=MY_COLOR)
                else:
                    names.append(name, style=_sender_color(sender))
            grid.add_row(f"{key} {len(senders)}", names)
            await box.mount(Static(grid))


class ReactScreen(ModalScreen):
    """Pick an emoji to react to one message with: digits 1-9 send from the
    quick row instantly, "/" opens a search over every emoji by Unicode name
    (arrows to choose a hit, Enter sends it). Picking one we already sent
    takes it back; the quick row ticks those. Dismisses with the emoji
    string or None."""

    BINDINGS = [
        ("escape", "cancel", "Cancel"),
        Binding("slash", "search", "Search", show=False),
        # Arrows move the search-hit highlight while the Input keeps focus,
        # same as the room search popup (j/k here would just type letters).
        Binding("down", "cursor_down", "Down", show=False),
        Binding("up", "cursor_up", "Up", show=False),
        *[Binding(str(d), f"quick({d})", show=False) for d in range(1, 10)],
    ]

    def __init__(self, entry: Entry, message) -> None:
        super().__init__()
        self.entry = entry
        self.message = message
        self.quick: list[str] = []

    def compose(self) -> ComposeResult:
        # escape(): the display name comes from the sender, and unescaped a
        # "[" in it is parsed as console markup.
        with Vertical(id="reactbox"):
            yield Label(
                f"React: {escape(self.message.sender_name)}", id="reacttitle"
            )
            yield Static(id="quickrow")
            yield Label(
                Text("1-9 react · / search by name · esc cancel", style="dim"),
                id="reacthint",
            )

    def on_mount(self) -> None:
        session = self.app.session
        # The quick row: reactions actually used before, most-used first,
        # padded out of the defaults; capped at the nine digits.
        usage = session.state.get("reaction_usage") or {}
        for key in sorted(usage, key=lambda k: -usage[k]) + DEFAULT_QUICK_REACTIONS:
            if key not in self.quick:
                self.quick.append(key)
        del self.quick[9:]
        row = Text()
        for i, key in enumerate(self.quick):
            if i:
                row.append("   ")
            row.append(f"{i + 1} ", style="dim")
            row.append(key)
            if session.my_reaction(
                self.entry.room_id, self.message.event_id, key
            ):
                # Already ours: the same digit now takes the reaction back.
                row.append("✓", style="dim")
        self.query_one("#quickrow", Static).update(row)

    def action_cancel(self) -> None:
        self.dismiss(None)

    def action_quick(self, n: int) -> None:
        if n <= len(self.quick):
            self.dismiss(self.quick[n - 1])

    def action_search(self) -> None:
        if self.query("#reactquery"):
            self.query_one("#reactquery", Input).focus()
            return
        box = self.query_one("#reactbox", Vertical)
        box.mount(
            Input(placeholder="Search emoji by name…", id="reactquery"),
            after=self.query_one("#quickrow"),
        )
        box.mount(
            ListView(id="reacthits"), after=self.query_one("#reactquery")
        )
        self.query_one("#reactquery", Input).focus()

    async def on_input_changed(self, event: Input.Changed) -> None:
        q = event.value.strip().lower()
        lv = self.query_one("#reacthits", ListView)
        await lv.clear()
        if not q:
            return
        hits = [(e, n) for e, n in _emoji_names() if q in n]
        hits.sort(
            key=lambda en: (not en[1].startswith(q), en[1].find(q), len(en[1]))
        )
        for e, n in hits[:30]:
            item = ListItem(Label(f"{e}  {n}"))
            item.reaction_key = e
            await lv.append(item)
        if hits:
            lv.index = 0  # Enter sends the best hit right away

    def _pick(self, item) -> None:
        key = getattr(item, "reaction_key", None)
        if key:
            self.dismiss(key)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        lv = self.query_one("#reacthits", ListView)
        items = list(lv.children)
        idx = lv.index if lv.index is not None else (0 if items else None)
        if idx is not None and idx < len(items):
            self._pick(items[idx])

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        self._pick(event.item)

    def action_cursor_down(self) -> None:
        for lv in self.query("#reacthits"):
            lv.action_cursor_down()

    def action_cursor_up(self) -> None:
        for lv in self.query("#reacthits"):
            lv.action_cursor_up()


class ConfirmScreen(ModalScreen):
    """A yes/no gate for destructive actions: Enter confirms, Escape backs
    out. Dismisses with True/False."""

    BINDINGS = [
        ("escape", "no", "Cancel"),
        ("enter", "yes", "Confirm"),
        Binding("y", "yes", "Confirm", show=False),
        Binding("n", "no", "Cancel", show=False),
    ]

    def __init__(self, question: str) -> None:
        super().__init__()
        self.question = question

    def compose(self) -> ComposeResult:
        # Text(), not markup: the question quotes remote message text.
        yield Static(Text(self.question), id="confirmbox")

    def action_yes(self) -> None:
        self.dismiss(True)

    def action_no(self) -> None:
        self.dismiss(False)


class AboutScreen(ModalScreen):
    BINDINGS = [
        ("escape", "dismiss", "Close"),
        ("enter", "dismiss", "Close"),
        ("question_mark", "dismiss", "Close"),
    ]

    def compose(self) -> ComposeResult:
        v = _version()
        text = Text(justify="center")
        text.append("MatrixCLI\n", style="bold")
        if v:
            text.append(f"version {v}\n", style="dim")
        text.append("\n(c) 2026 Eljakim Schrijvers\n")
        text.append("eljakim@gmail.com", style="dim")
        yield Static(text, id="aboutbox")


class SettingsScreen(ModalScreen):
    """":settings" (or ":set"): account and app settings. The display name
    saves to the homeserver; the email addresses are read-only (changing
    them needs a validation mail the server may not even send, so that
    stays in Element); the titlebar unread counter and the timezone live
    in state.json and apply immediately on save."""

    BINDINGS = [
        ("escape", "dismiss", "Cancel"),
        Binding("ctrl+s", "save", "Save"),
    ]

    def compose(self) -> ComposeResult:
        session = self.app.session
        with Vertical(id="settingsbox"):
            yield Label("Settings", id="settingstitle")
            yield Label("Display name")
            yield Input(
                value=session.my_name or "", id="set_name", compact=True
            )
            yield Label("Email addresses", classes="settingslabel")
            yield Static(Text("fetching...", style="dim"), id="set_emails")
            with Horizontal(id="set_unread_row"):
                yield Switch(
                    value=bool(session.get_setting("titlebar_unread", True)),
                    id="set_unread",
                )
                yield Label("Unread count in the terminal titlebar")
            yield Label(
                "Timezone (IANA name; empty = system)",
                classes="settingslabel",
            )
            yield Input(
                value=str(session.get_setting("timezone", "") or ""),
                placeholder="Europe/Amsterdam",
                id="set_tz",
                compact=True,
            )
            yield Static(
                Text("Enter or Ctrl+S saves - Esc cancels", style="dim"),
                id="settingshint",
            )

    def on_mount(self) -> None:
        self.query_one("#set_name", Input).focus()
        self.run_worker(self._load_emails())

    async def _load_emails(self) -> None:
        emails = await self.app.session.fetch_email_addresses()
        if not self.is_attached:
            return
        if emails is None:
            text = Text("could not fetch (offline?)", style="dim")
        elif not emails:
            text = Text("none on this account", style="dim")
        else:
            text = Text("\n".join(emails))
        self.query_one("#set_emails", Static).update(text)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self.action_save()

    def action_save(self) -> None:
        self.run_worker(self._save())

    async def _save(self) -> None:
        session = self.app.session
        tz_name = self.query_one("#set_tz", Input).value.strip()
        if not _set_display_timezone(tz_name):
            self.app.notify(
                f"Unknown timezone: {tz_name}",
                severity="warning", timeout=4, markup=False,
            )
            return
        session.set_setting("timezone", tz_name)
        session.set_setting(
            "titlebar_unread",
            bool(self.query_one("#set_unread", Switch).value),
        )
        name = self.query_one("#set_name", Input).value.strip()
        if name and name != session.my_name:
            if await session.set_display_name(name):
                session.my_name = name
            else:
                self.app.notify(
                    "The server rejected the display name change.",
                    severity="warning", timeout=4,
                )
                return
        # Timestamps may now render in another zone: force-rebuild every
        # open room (an unchanged signature would keep the old widgets).
        for scr in self.app.screen_stack:
            if isinstance(scr, RoomScreen):
                scr._drawn_sig = None
                scr.run_worker(scr._redraw())
        _refresh_terminal_title(self.app)
        self.app.notify("Settings saved.", timeout=3)
        self.dismiss(None)


class CommandScreen(ModalScreen):
    """":" anywhere outside a text editor: a one-line vim-style command
    entry docked at the bottom, dismissing with the typed command on Enter
    (None on Escape). The app interprets the result: "q!" quits
    unconditionally, "q" closes the page like the q key, and a bare number
    jumps to that message in the open room (":1" = the oldest)."""

    BINDINGS = [("escape", "dismiss", "Cancel")]

    def compose(self) -> ComposeResult:
        with Horizontal(id="commandbox"):
            yield Label(":", id="commandprompt")
            yield Input(id="command", compact=True)

    def on_mount(self) -> None:
        self.query_one("#command", Input).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self.dismiss(event.value.strip())


class RoomSearchScreen(ModalScreen):
    """"/" inside a room: accent-insensitive search over every message this
    client holds for the room (full downloaded archive included), newest hit
    first. The query matches message text, display name, or matrix id;
    thread replies match too. Enter dismisses with the chosen event id and
    the room screen jumps there. Local only, so the count line says when the
    background download is still running."""

    # Widgets are not virtualized: only the newest slice is mounted and the
    # count line says what was clipped.
    MAX_HITS = 100

    BINDINGS = [
        ("escape", "dismiss", "Close"),
        Binding("down", "cursor_down", "Down", show=False),
        Binding("up", "cursor_up", "Up", show=False),
    ]

    def __init__(
        self, messages: list, downloading: bool = False, where: str = ""
    ) -> None:
        super().__init__()
        self.messages = messages
        self.downloading = downloading
        self.where = where  # "ioi.ga", "thread in ioi.ga": shown on the border
        # Fold every haystack once at open: refolding on each keystroke
        # would make typing lag in a big room.
        self._index = [
            (
                fold_text(f"{m.body or ''} {m.sender_name or ''} {m.sender or ''}"),
                m,
            )
            for m in messages
            if m.event_id
        ]

    def compose(self) -> ComposeResult:
        with Vertical(id="searchbox") as box:
            # The border names what is searched (this popup looks identical
            # to the dashboard search). escape(): the room title comes from
            # other users.
            box.border_title = (
                f"Search messages in {escape(self.where)}"
                if self.where
                else "Search messages"
            )
            yield Input(placeholder="Search messages and senders…", id="q")
            yield Static("", id="hitcount")
            yield ListView(id="results")

    def on_mount(self) -> None:
        self.query_one("#q", Input).focus()
        self._set_count("")

    def _set_count(self, status: str) -> None:
        if self.downloading:
            status += " · " if status else ""
            status += "history still downloading"
        self.query_one("#hitcount", Static).update(
            f"[dim]{status}[/dim]" if status else ""
        )

    async def on_input_changed(self, event: Input.Changed) -> None:
        q = fold_text(event.value.strip())
        lv = self.query_one("#results", ListView)
        await lv.clear()
        if not q:
            self._set_count("")
            return
        hits = [m for folded, m in reversed(self._index) if q in folded]
        items = []
        for m in hits[: self.MAX_HITS]:
            first_line = (m.body or "").strip().splitlines() or [""]
            # escape(): sender text; unescaped it is parsed as console markup.
            label = (
                f"[dim]{_fmt_time(m.ts)}[/dim] "
                f"{escape(m.sender_name[:20])}: {escape(first_line[0][:80])}"
            )
            item = ListItem(Label(label))
            item.event_id = m.event_id
            items.append(item)
        await lv.extend(items)
        if len(hits) > self.MAX_HITS:
            self._set_count(f"newest {self.MAX_HITS} of {len(hits)} matches")
        else:
            self._set_count(f"{len(hits)} match" + ("" if len(hits) == 1 else "es"))
        if hits:
            lv.index = 0  # Enter jumps to the newest hit right away

    def action_cursor_down(self) -> None:
        self.query_one("#results", ListView).action_cursor_down()

    def action_cursor_up(self) -> None:
        self.query_one("#results", ListView).action_cursor_up()

    def _jump(self, item) -> None:
        target = getattr(item, "event_id", None)
        if target:
            self.dismiss(target)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        lv = self.query_one("#results", ListView)
        items = list(lv.children)
        idx = lv.index if lv.index is not None else (0 if items else None)
        if idx is not None and idx < len(items):
            self._jump(items[idx])

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        self._jump(event.item)


class SearchScreen(ModalScreen):
    """"/" on the dashboard: search people, rooms, and every locally held
    message. With the cursor in Recent or Favourites the search is scoped to
    that WHOLE section (the dashboard shows it truncated). Opening a message
    hit opens its room and jumps to it."""

    MAX_HITS = 30  # per group (rooms/people, then messages)

    BINDINGS = [
        ("escape", "dismiss", "Close"),
        # The Input keeps focus while these move the result highlight, so you
        # can keep typing and pick a hit without tabbing to the list.
        Binding("down", "cursor_down", "Down", show=False),
        Binding("up", "cursor_up", "Up", show=False),
    ]

    def __init__(self, scope: str | None = None) -> None:
        super().__init__()
        self.scope = scope
        self._msgs: list = []  # (folded haystack, entry, message), global only

    def compose(self) -> ComposeResult:
        placeholder = {
            "favourites": "Search all favourites…",
            "recent": "Search all recent rooms…",
        }.get(self.scope, "Search people, rooms, and messages…")
        with Vertical(id="searchbox") as box:
            # The border names the scope: a scoped search must stay visibly
            # different from the global and in-room ones.
            box.border_title = {
                "favourites": "Search all favourites",
                "recent": "Search all recent rooms",
            }.get(self.scope, "Search people, rooms & messages")
            yield Input(placeholder=placeholder, id="q")
            yield ListView(id="results")

    async def on_mount(self) -> None:
        self.query_one("#q", Input).focus()
        # Which rooms' messages this popup may match: all of them globally,
        # only the section's rooms when scoped.
        opened = self.app.session.state.get("last_opened_ts") or {}

        def keep(e: Entry) -> bool:
            if self.scope == "favourites":
                return e.is_favourite
            if self.scope == "recent":
                return e.room_id in opened
            return True
        # One folded haystack per message, built once at open: refolding
        # thousands of bodies per keystroke would make typing lag.
        self._msgs = [
            (
                fold_text(
                    f"{m.body or ''} {m.sender_name or ''} {m.sender or ''}"
                ),
                e,
                m,
            )
            for e, m in self.app.session.message_index()
            if m.event_id and keep(e)
        ]
        if self.scope:
            await self._show_results("")  # the whole section, before typing

    async def on_input_changed(self, event: Input.Changed) -> None:
        await self._show_results(event.value)

    async def _show_results(self, query: str) -> None:
        results = self.app.session.search(query, self.scope)
        lv = self.query_one("#results", ListView)
        await lv.clear()
        items = []
        for e in results[: self.MAX_HITS]:
            tag = f"  ({e.unread})" if e.unread else ""
            kind = "person" if e.is_direct else "room"
            # escape(): titles come from other users; unescaped they are parsed
            # as console markup (crash on "[x", spoofed styling).
            items.append(
                EntryItem(e, f"{escape(e.title)}{tag}  [dim]{kind}[/dim]")
            )
        q = fold_text(query.strip())
        if q:
            # Message hits after the room/people hits, newest first, each
            # naming its room so the mixed list stays readable.
            hits = [(e, m) for folded, e, m in self._msgs if q in folded]
            hits.sort(key=lambda pair: -pair[1].ts)
            for e, m in hits[: self.MAX_HITS]:
                first_line = (m.body or "").strip().splitlines() or [""]
                label = (
                    f"[dim]{_fmt_time(m.ts)}[/dim] {escape(e.title)}"
                    f" · {escape(m.sender_name[:20])}: "
                    f"{escape(first_line[0][:60])}"
                )
                items.append(MessageHitItem(e, m.event_id, label))
        await lv.extend(items)
        if items:
            lv.index = 0  # Enter opens the top hit right away

    def action_cursor_down(self) -> None:
        self.query_one("#results", ListView).action_cursor_down()

    def action_cursor_up(self) -> None:
        self.query_one("#results", ListView).action_cursor_up()

    def _open(self, item) -> None:
        if isinstance(item, MessageHitItem):
            entry, event_id = item.entry, item.event_id
            self.dismiss()
            self.app.open_room(entry, jump_to=event_id)
        elif isinstance(item, EntryItem):
            entry = item.entry
            self.dismiss()
            self.app.open_room(entry)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        lv = self.query_one("#results", ListView)
        items = list(lv.children)
        idx = lv.index if lv.index is not None else (0 if items else None)
        if idx is not None and idx < len(items):
            self._open(items[idx])

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        self._open(event.item)


class LoadingScreen(Screen):
    """Startup splash: a big title plus a running list of progress steps, so
    the screen is never blank while we connect and sync."""

    def compose(self) -> ComposeResult:
        v = _version()
        title = "MatrixCLI" + (f" v{v}" if v else "")
        with Vertical(id="loadingbox"):
            yield Static(Text(title, style="bold"), id="loadingtitle")
            yield Static(
                Text("(c) 2026 Eljakim Schrijvers", style="dim"),
                id="loadingcopy",
            )
            yield Static("", id="loadingsteps")

    def on_mount(self) -> None:
        self._steps: list[str] = []

    def set_status(self, msg: str) -> None:
        steps = getattr(self, "_steps", None)
        if steps is None:
            self._steps = steps = []
        # Mark previous steps done, current one active.
        steps.append(msg)
        lines = Text()
        for s in steps[:-1]:
            lines.append(f"  ✓ {s}\n", style="green")
        lines.append(f"  • {steps[-1]}", style="bold")
        self.query_one("#loadingsteps", Static).update(lines)


class HomeScreen(VimCount, Screen):
    BINDINGS = [
        ("slash", "search", "Search"),
        # The four movement keys take one footer slot: "hjkl" reads as a unit,
        # so only this binding is shown and the rest stay live but unlisted.
        Binding("j", "down", "Select room", key_display="hjkl"),
        Binding("k", "up", show=False),
        Binding("l", "focus_next_column", show=False),
        Binding("h", "focus_prev_column", show=False),
        Binding("tab", "focus_next_column", show=False),
        Binding("shift+tab", "focus_prev_column", show=False),
        # priority=True: the focused ListView binds the arrows (and enter)
        # itself, stopping the cursor at each list's edge; these route them
        # through the same column-spilling _step as j/k. Enter also puts
        # "Open" in the footer (a focused widget's hidden binding otherwise
        # outranks the screen's for display).
        Binding("down", "down", show=False, priority=True),
        Binding("up", "up", show=False, priority=True),
        Binding("enter", "open_selected", "Open", priority=True),
        # Two labels for one key: check_action enables the one matching the
        # highlighted entry, and hides both on rows that cannot be tagged
        # (spaces, invites, empty placeholders).
        Binding("f", "favourite_add", "Favourite"),
        Binding("f", "favourite_remove", "Unfavourite"),
        # Two labels for one key, reading as state: shown only on a
        # highlighted space, "c" flips whether its rooms are cached to disk.
        # Hidden when [cache] messages is off: the toggle would do nothing.
        Binding("c", "cache_off", "Cache: on"),
        Binding("c", "cache_on", "Cache: off"),
        # Download every room's full history without opening each room by
        # hand. Hidden when [cache] messages is off: nothing would be kept.
        Binding("S", "sync_all", "Sync all"),
        # Vim's resize chord as single keys: in Recent or Favourites, +/-
        # grows/shrinks that section's row budget, gated to what fits and
        # never below MIN_SECTION_ROWS. "=" is the unshifted alias for "+".
        Binding("plus", "grow_section", "More rows"),
        Binding("equals_sign", "grow_section", show=False),
        Binding("minus", "shrink_section", "Fewer rows"),
        ("q", "app.quit", "Quit"),
    ]

    # Keys whose actions read a typed count; any other key drops it.
    COUNT_KEYS = ("j", "k", "down", "up")

    # The three visual columns, each a top-to-bottom stack of lists. "j"/"k"
    # walk a whole column as if it were one list (spilling from the bottom of
    # one into the top of the next), "h"/"l" step between columns.
    COLUMNS = [
        ["spaces", "space_rooms", "other_rooms"],
        ["invites", "recent", "favourites"],
        ["dms"],
    ]

    def __init__(self) -> None:
        super().__init__()
        self._last_signature = None
        # Column index -> id of the list last focused there, so "h"/"l" return
        # you to the row you were using instead of the top of the column.
        self._last_in_column: dict[int, str] = {}
        # refresh_data is reached from both the app pump (background sync) and
        # this screen's own handlers; the lock keeps rebuilds from interleaving.
        self._refresh_lock = asyncio.Lock()
        # Set by the app instead of refreshing while a room covers this
        # screen; the deferred rebuild happens once, on resume.
        self.stale = False

    def on_screen_resume(self) -> None:
        _refresh_terminal_title(self.app, "matrixcli")
        if self.stale:
            self.stale = False
            self.run_worker(self.refresh_data())

    def compose(self) -> ComposeResult:
        yield Header()
        # VerticalScroll, not Vertical: a column whose stacked lists outgrow
        # the terminal scrolls instead of clipping. Keyboard focus scrolls
        # the focused list into view on its own.
        with Horizontal(id="columns"):
            with VerticalScroll(classes="column"):
                yield Label("Spaces", classes="section")
                yield ListView(id="spaces")
                yield Label("Rooms", classes="section", id="roomslabel")
                yield ListView(id="space_rooms")
                yield Label("Other rooms", classes="section", id="otherslabel")
                yield ListView(id="other_rooms")
            with VerticalScroll(classes="column"):
                yield Label("Invites", classes="section", id="inviteslabel")
                yield ListView(id="invites")
                yield Label("Recent", classes="section")
                yield ListView(id="recent")
                yield Label("Favourites", classes="section")
                yield ListView(id="favourites")
            with VerticalScroll(classes="column"):
                yield Label("DMs", classes="section")
                yield ListView(id="dms")
        yield StatusFooter()

    async def on_mount(self) -> None:
        self.title = "matrixcli"
        self.sub_title = self.app.session.cfg.user_id
        await self.refresh_data()
        # Start on the most recently opened room; fall back to Favourites
        # when Recent is empty (fresh state file).
        recent = self.query_one("#recent", ListView)
        if any(isinstance(c, EntryItem) for c in recent.children):
            recent.focus()
        else:
            self.query_one("#favourites", ListView).focus()

    @property
    def selected_space(self) -> str | None:
        return self.app.session.state.get("selected_space")

    def _label_for(self, e: Entry, is_selected_space: bool = False) -> str:
        # A fixed two-cell marker slot keeps every title left-aligned: "✉"
        # invite, "★" favourite, "◦" the chosen space, or two spaces.
        if e.is_invite:
            marker = "✉ "
        elif e.is_favourite:
            marker = "★ "
        elif is_selected_space:
            marker = "◦ "
        else:
            marker = "  "
        # escape(): titles come from other users; unescaped they are parsed as
        # console markup (crash on "[x", spoofed badges/styling).
        name = marker + escape(e.title)
        if e.unread:
            name = f"[b]{name}[/b]"
        if e.online:
            name += " [green]●[/green]"
        if e.unread:
            name += f"  [b yellow]({e.unread})[/b yellow]"
        if e.highlights:
            # Mentions get their own red badge: at peak traffic the yellow
            # unread counts are everywhere, but a ping must stand out.
            name += f" [b red]({e.highlights}!)[/b red]"
        return name

    async def refresh_data(self) -> None:
        # Serialized by a lock: two interleaved runs would duplicate rows, and
        # the signature cache would then skip the redraw that could fix them.
        async with self._refresh_lock:
            if not self.is_attached:
                return
            data = self.app.session.dashboard(self.selected_space)

            # Skip the redraw entirely if nothing visible changed, so the
            # screen does not flicker on every background sync.
            signature = self._signature(data)
            if signature == self._last_signature:
                return
            self._last_signature = signature

            async def fill(list_id: str, entries, selected_space=None) -> None:
                lv = self.query_one(f"#{list_id}", ListView)
                # Remember the highlighted room by id, not position: a
                # refresh can reorder the list, and a positional restore
                # would land the cursor on a different room.
                idx = lv.index
                highlighted = None
                if idx is not None:
                    try:
                        item = list(lv.children)[idx]
                        if isinstance(item, EntryItem):
                            highlighted = item.entry.room_id
                    except IndexError:
                        pass
                await lv.clear()
                if not entries:
                    placeholder = ListItem(Label("[dim](none)[/dim]"))
                    placeholder.disabled = True
                    await lv.append(placeholder)
                    return
                # One batched mount: a mount await per entry blocks the
                # event loop.
                await lv.extend(
                    EntryItem(e, self._label_for(e, e.room_id == selected_space))
                    for e in entries
                )
                new_index = next(
                    (i for i, e in enumerate(entries) if e.room_id == highlighted),
                    None,
                )
                if new_index is None and idx is not None:
                    new_index = min(idx, len(entries) - 1)
                if new_index is None:
                    # Nothing was highlighted before: land on the first row,
                    # so tabbing into a column allows Enter immediately.
                    new_index = 0
                lv.index = new_index

            # The invites section only occupies space when there are any.
            show_invites = bool(data["invites"])
            self.query_one("#inviteslabel", Label).display = show_invites
            self.query_one("#invites", ListView).display = show_invites

            # Same for Other rooms: most accounts have every room in a space,
            # and an always-present "(none)" would just push Rooms around.
            show_others = bool(data["others"])
            self.query_one("#otherslabel", Label).display = show_others
            self.query_one("#other_rooms", ListView).display = show_others

            await fill("spaces", data["spaces"], selected_space=self.selected_space)
            await fill("space_rooms", data["space_rooms"])
            await fill("other_rooms", data["others"])
            await fill("invites", data["invites"])
            await fill("recent", data["recent"])
            await fill("favourites", data["favourites"])
            await fill("dms", data["dms"])

    def _signature(self, data) -> tuple:
        """A cheap hashable summary of what the dashboard would render, so we
        can detect 'nothing changed' and skip the redraw."""
        def col(entries):
            return tuple(
                (e.room_id, e.title, e.unread, e.online, e.is_favourite, e.highlights)
                for e in entries
            )
        return (
            self.selected_space,
            col(data["spaces"]),
            col(data["space_rooms"]),
            col(data["others"]),
            col(data["invites"]),
            col(data["recent"]),
            col(data["favourites"]),
            col(data["dms"]),
        )

    def _focused_list(self) -> "ListView | None":
        for cid in [c for column in self.COLUMNS for c in column]:
            lv = self.query_one(f"#{cid}", ListView)
            if lv.has_focus:
                return lv
        return None

    def _focused_column(self) -> int:
        for i, column in enumerate(self.COLUMNS):
            if any(self.query_one(f"#{c}", ListView).has_focus for c in column):
                return i
        return 0

    def _entries_of(self, lv: ListView) -> list:
        # Empty lists hold a single disabled "(none)" placeholder; real
        # entries always start at index 0.
        return [c for c in lv.children if isinstance(c, EntryItem)]

    def on_descendant_focus(self, event: textual.events.DescendantFocus) -> None:
        for i, column in enumerate(self.COLUMNS):
            if event.widget.id in column:
                self._last_in_column[i] = event.widget.id
        self.refresh_bindings()

    def on_list_view_highlighted(self, event: ListView.Highlighted) -> None:
        # The f label (Favourite/Unfavourite) follows the highlighted row.
        self.refresh_bindings()

    def _selected_entry(self) -> "Entry | None":
        lv = self._focused_list()
        if lv is None or lv.index is None:
            return None
        try:
            item = list(lv.children)[lv.index]
        except IndexError:
            return None
        return item.entry if isinstance(item, EntryItem) else None

    def check_action(self, action: str, parameters) -> bool:
        if action in ("favourite_add", "favourite_remove"):
            entry = self._selected_entry()
            if entry is None or entry.is_space or entry.is_invite:
                return False
            wants_remove = action == "favourite_remove"
            return entry.is_favourite == wants_remove
        if action in ("cache_on", "cache_off"):
            session = self.app.session
            if not session.cfg.cache_messages:
                return False
            entry = self._selected_entry()
            if entry is None or not entry.is_space:
                return False
            enabled = session.space_cache_enabled(entry.room_id)
            # "cache_off" turns it off, so it is the one offered (labelled
            # "Cache: on") while caching is enabled.
            return enabled == (action == "cache_off")
        if action == "sync_all":
            return bool(self.app.session.cfg.cache_messages)
        if action in ("grow_section", "shrink_section"):
            section = self._resizable_section()
            if section is None:
                return False
            session = self.app.session
            if action == "shrink_section":
                return session.section_rows(section) > MIN_SECTION_ROWS
            return (
                session.section_rows("recent")
                + session.section_rows("favourites")
                < self._rows_budget()
            )
        return True

    def _resizable_section(self) -> str | None:
        """"recent" or "favourites" when the focus sits in that list, else
        None: the +/- row-budget keys only act there."""
        fid = getattr(self.focused, "id", None)
        return fid if fid in ("recent", "favourites") else None

    def _rows_budget(self) -> int:
        """Entry rows the middle column can devote to Recent and Favourites
        together without scrolling: its height minus the section labels, the
        invites section when shown, and the lists' own margins."""
        recent = self.query_one("#recent", ListView)
        column = recent.parent
        budget = column.content_size.height
        for w in column.children:
            if not w.display:
                continue
            margin = w.styles.margin
            budget -= margin.top + margin.bottom
            if w.id not in ("recent", "favourites"):
                budget -= w.outer_size.height
        return budget

    def action_grow_section(self) -> None:
        self._resize_section(1)

    def action_shrink_section(self) -> None:
        self._resize_section(-1)

    def _resize_section(self, delta: int) -> None:
        section = self._resizable_section()
        if section is None:
            return
        session = self.app.session
        rows = session.section_rows(section)
        total = session.section_rows("recent") + session.section_rows("favourites")
        if delta > 0 and total >= self._rows_budget():
            return  # no free line without squeezing the other section
        if delta < 0 and rows <= MIN_SECTION_ROWS:
            return
        session.state[f"{section}_rows"] = rows + delta
        session.cfg.save_state(session.state)
        self.refresh_bindings()  # +/- appear and vanish with the limits
        self.run_worker(self.refresh_data())

    def on_resize(self, event) -> None:
        self._clamp_section_rows()

    def _clamp_section_rows(self) -> None:
        """A smaller terminal takes stored row budgets back down so both
        sections still fit, trimming the larger one first, never below the
        MIN_SECTION_ROWS floor."""
        budget = self._rows_budget()
        if budget < 2 * MIN_SECTION_ROWS:
            return  # not laid out yet, or too small even for the floors
        session = self.app.session
        recent = session.section_rows("recent")
        favs = session.section_rows("favourites")
        changed = False
        while recent + favs > budget and (
            recent > MIN_SECTION_ROWS or favs > MIN_SECTION_ROWS
        ):
            if recent >= favs and recent > MIN_SECTION_ROWS:
                recent -= 1
            else:
                favs -= 1
            changed = True
        if changed:
            session.state["recent_rows"] = recent
            session.state["favourites_rows"] = favs
            session.cfg.save_state(session.state)
            self.refresh_bindings()
            self.run_worker(self.refresh_data())

    def action_cache_on(self) -> None:
        self._toggle_cache()

    def action_cache_off(self) -> None:
        self._toggle_cache()

    def _toggle_cache(self) -> None:
        entry = self._selected_entry()
        if entry is None or not entry.is_space:
            return
        session = self.app.session
        session.set_space_cache(
            entry.room_id, not session.space_cache_enabled(entry.room_id)
        )
        self.refresh_bindings()  # flip the footer label with the state

    def action_open_selected(self) -> None:
        # Enter is a priority binding, so on_key never saw it: drop any
        # half-typed count instead of carrying it into the room.
        self._take_count()
        # The priority Enter binding bypasses the focused ListView's own
        # select action; delegate back to it (on_list_view_selected guards
        # against placeholder rows).
        lv = self._focused_list()
        if lv is not None:
            lv.action_select_cursor()

    def action_search(self) -> None:
        # From the Recent or Favourites list, "/" searches that whole
        # section (the dashboard shows it truncated to a row budget);
        # anywhere else it is the global people-rooms-and-messages search.
        lv = self._focused_list()
        scope = lv.id if lv is not None and lv.id in ("recent", "favourites") else None
        self.app.push_screen(SearchScreen(scope))

    def action_sync_all(self) -> None:
        """"S": start the full-history download for every room at once, so
        the local caches (and the global message search) end up covering
        everything without opening each room by hand."""
        n = self.app.session.backfill_all()
        if n:
            self.app.notify(
                f"Downloading full history for {n} room"
                + ("s" if n != 1 else "")
                + " in the background.",
                timeout=4,
            )
        else:
            self.app.notify(
                "Every room's history is already fully downloaded.", timeout=4
            )

    def action_favourite_add(self) -> None:
        self.run_worker(self._toggle_favourite())

    def action_favourite_remove(self) -> None:
        self.run_worker(self._toggle_favourite())

    def action_down(self) -> None:
        # Stepping one row at a time keeps the column-spilling logic in one
        # place; the cap keeps a wild count ("999999j") from spinning.
        for _ in range(min(self._take_count() or 1, 999)):
            self._step(1)

    def action_up(self) -> None:
        for _ in range(min(self._take_count() or 1, 999)):
            self._step(-1)

    def _step(self, delta: int) -> None:
        lv = self._focused_list()
        if lv is None:
            return
        if self._entries_of(lv):
            before = lv.index
            if delta > 0:
                lv.action_cursor_down()
            else:
                lv.action_cursor_up()
            if lv.index != before:
                return
        # Already at the end of this list: continue into the next one down (or
        # up) in the same column, skipping hidden and empty sections.
        ids = self.COLUMNS[self._focused_column()]
        i = (ids.index(lv.id) if lv.id in ids else 0) + delta
        while 0 <= i < len(ids):
            nxt = self.query_one(f"#{ids[i]}", ListView)
            entries = self._entries_of(nxt)
            if nxt.display and entries:
                nxt.index = 0 if delta > 0 else len(entries) - 1
                nxt.focus()
                return
            i += delta

    def action_focus_next_column(self) -> None:
        self._cycle_column(1)

    def action_focus_prev_column(self) -> None:
        self._cycle_column(-1)

    def _cycle_column(self, delta: int) -> None:
        # Columns with nothing to highlight are skipped rather than focused, so
        # "h"/"l" never land you somewhere the cursor cannot go.
        start = self._focused_column()
        for step in range(1, len(self.COLUMNS) + 1):
            target = (start + delta * step) % len(self.COLUMNS)
            ids = self.COLUMNS[target]
            remembered = self._last_in_column.get(target)
            order = ([remembered] if remembered in ids else []) + [
                c for c in ids if c != remembered
            ]
            for cid in order:
                lv = self.query_one(f"#{cid}", ListView)
                if lv.display and self._entries_of(lv):
                    lv.focus()
                    return

    async def on_list_view_selected(self, event: ListView.Selected) -> None:
        if not isinstance(event.item, EntryItem):
            return
        entry = event.item.entry
        if entry.is_invite:
            # In a worker: the join is a network round trip and must not
            # block the dashboard's keys.
            async def accept() -> None:
                ok, msg = await self.app.session.accept_invite(entry.room_id)
                self.app.notify(
                    f"{entry.title}: {msg}",
                    severity="information" if ok else "error",
                    timeout=6,
                    markup=False,
                )
                if ok and self.is_attached:
                    # The joined room arrives with the next sync; redraw now
                    # so the invite disappears immediately.
                    await self.refresh_data()

            self.run_worker(accept())
            return
        if entry.is_space:
            # Selecting a space loads its rooms into the column below.
            # Refetch its children first so new rooms show up without a
            # restart; in a worker so the network cannot stall the keys.
            self.app.session.state["selected_space"] = entry.room_id
            self.app.session.cfg.save_state(self.app.session.state)

            async def load_space() -> None:
                await self.app.session.refresh_space_children(entry.room_id)
                if not self.is_attached:
                    return
                await self.refresh_data()
                self.query_one("#space_rooms", ListView).focus()

            self.run_worker(load_space())
        else:
            self.app.open_room(entry)

    async def _toggle_favourite(self) -> None:
        entry = self._selected_entry()
        # Spaces cannot be favourited and an invite is not joined yet; both
        # are also excluded from the footer by check_action.
        if entry is None or entry.is_space or entry.is_invite:
            return
        ok = await self.app.session.set_favourite(
            entry.room_id, not entry.is_favourite
        )
        if ok:
            await self.refresh_data()


class MatrixApp(App):
    CSS = """
    .section {
        text-style: bold;
        color: $accent;
        padding: 1 1 0 1;
    }
    #columns { height: 1fr; }
    .column {
        width: 1fr;
        border-right: solid $panel;
    }
    #columns ListView {
        height: auto;
        max-height: 1fr;
        margin: 0 1 1 1;
        border-left: solid $surface;
    }
    #columns ListView:focus { border-left: solid $accent; }
    /* Recent and Favourites keep their floor height even half-empty, so the
       layout does not jump as rooms enter and leave the lists. */
    #recent, #favourites { min-height: 5; }
    #loadingbox {
        align: center middle;
        height: 1fr;
    }
    #loadingtitle { width: auto; color: $accent; }
    #loadingcopy { width: auto; padding: 0 0 1 0; }
    #loadingsteps { width: auto; }
    #searchbox {
        width: 80%;
        height: auto;
        max-height: 80%;
        margin: 2 10;
        padding: 1;
        border: round $accent;
        background: $panel;
    }
    /* height auto: a ListView is a ScrollView (height 1fr by default), which
       inflated the popup to its max height even while empty. */
    #searchbox #results { height: auto; max-height: 20; }
    #commandbox {
        dock: bottom;
        height: 1;
        background: $panel;
    }
    #commandprompt { width: 1; }
    #commandbox Input { background: $panel; }
    #downloadbox, #actionbox, #editsbox, #reactionsbox, #reactbox {
        width: 70%;
        height: auto;
        margin: 4 10;
        padding: 1;
        border: round $accent;
        background: $panel;
    }
    #downloadtitle, #actiontitle, #editstitle, #reactionstitle, #reacttitle {
        text-style: bold;
        padding: 0 0 1 0;
    }
    #reactbox #reacthint { padding: 1 0 0 0; }
    #reactbox #reacthits { max-height: 12; }
    #actionbox #actions { max-height: 12; }
    #editsbox #versions { height: auto; max-height: 20; }
    /* Full-screen and opaque: the art must not blend into the timeline
       behind it, and every cell of the window is canvas. */
    #previewbox {
        width: 100%;
        height: 100%;
        padding: 0 1;
        background: $background;
        align: center middle;
    }
    #previewtitle {
        text-style: bold;
        height: 1;
        color: $foreground 60%;
    }
    /* nowrap: rendered against a stale mid-resize width, a wrapping art
       line would spill one cell onto a blank row and stripe the picture. */
    #previewart { width: auto; height: auto; text-wrap: nowrap; }
    #reactionsbox #reactors { height: auto; max-height: 20; }
    AboutScreen, ConfirmScreen, SettingsScreen { align: center middle; }
    #settingsbox {
        width: 60%;
        max-width: 70;
        height: auto;
        padding: 1 2;
        border: round $accent;
        background: $panel;
    }
    #settingstitle { text-style: bold; padding: 0 0 1 0; }
    #settingsbox .settingslabel { padding: 1 0 0 0; }
    #settingsbox Input { background: $boost; }
    #set_unread_row { height: auto; padding: 1 0 0 0; align-vertical: middle; }
    #set_unread_row Label { padding: 1 0 0 1; }
    #settingshint { padding: 1 0 0 0; }
    #confirmbox {
        width: auto;
        max-width: 70%;
        height: auto;
        padding: 1 4;
        border: round $error;
        background: $panel;
    }
    #aboutbox {
        width: auto;
        height: auto;
        padding: 1 4;
        border: round $accent;
        background: $panel;
    }
    /* Bottom-right of the footer, same slot Textual's own command-palette
       key uses; the vkey border separates it from the binding keys. */
    ConnStatus {
        dock: right;
        width: auto;
        height: 1;
        padding: 0 1;
        border-left: vkey $foreground 20%;
    }
    #timeline { height: 1fr; padding: 0 1; }
    MessageLine { height: auto; border-left: wide $background; }
    /* Threaded view: indent the whole reply (timestamp included) under the
       root's text; the border doubles as the full-height thread bar. */
    MessageLine.thread-reply { margin-left: 6; border-left: wide $panel; }
    MessageLine.selected { background: $boost; border-left: wide $accent; }
    .unread-marker { color: red; text-style: bold; }
    /* The composer is docked under the timeline rather than mounted inside
       it: a fixed five rows that scroll internally, so a long draft never
       runs off the bottom of the screen. */
    #composer {
        display: none;
        height: auto;
        border-top: solid $accent;
    }
    #composertitle { color: $accent; padding: 0 1; }
    #editor {
        border: none;
        padding: 0;
        margin: 0 1;
        height: 5;
        background: $boost;
    }
    """

    # No command palette (ctrl+p) and no ctrl+q: "q" quits, and the palette's
    # commands are all covered by explicit bindings.
    ENABLE_COMMAND_PALETTE = False
    BINDINGS = [
        # "q" deliberately NOT bound here: the dashboard's q quits, a room's
        # q goes home, and ":q!" quits from anywhere. A global q would make
        # one keystroke while reading a room exit the whole app.
        Binding("ctrl+q", "noop", show=False),
        Binding("question_mark", "about", "About", show=False),
        # Hidden to keep the footer lean.
        Binding("ctrl+r", "force_refresh", "Refresh", show=False),
        # vim-style command line; ":" only reaches this binding when no text
        # editor is focused (a focused TextArea/Input consumes the
        # character), so typing ":q!" INTO a draft cannot quit anything.
        Binding("colon", "command_line", "Command", show=False),
    ]

    def action_go_home(self) -> None:
        """Pop everything above the dashboard (q on any page)."""
        while (
            not isinstance(self.screen, HomeScreen) and len(self.screen_stack) > 1
        ):
            self.pop_screen()


    def action_command_line(self) -> None:
        def when_entered(cmd) -> None:
            if not cmd:
                return
            if cmd == "q!":
                self.exit()
            elif cmd == "q":
                # ":q" closes the current page, like the q key: back to the
                # dashboard, or quit when already on it.
                if isinstance(self.screen, HomeScreen):
                    self.exit()
                else:
                    self.action_go_home()
            elif cmd.isdigit():
                # ":N" jumps to message N in the open room; past-the-end
                # numbers land on the newest message, like "G".
                if isinstance(self.screen, RoomScreen):
                    self.screen.goto_message(int(cmd))
                else:
                    self.notify(
                        "':<number>' jumps to a message inside a room",
                        severity="warning", timeout=4,
                    )
            elif cmd in ("settings", "set"):
                if not isinstance(self.screen, SettingsScreen):
                    self.push_screen(SettingsScreen())
            else:
                self.notify(
                    f"Not a command: {cmd}", severity="warning", timeout=4,
                    markup=False,
                )

        self.push_screen(CommandScreen(), when_entered)

    def action_noop(self) -> None:
        pass

    def _fatal_error(self) -> None:
        """Render an unhandled-exception traceback WITHOUT local variables.

        Textual's default (app.py `_fatal_error`) uses show_locals=True, which
        dumps frame locals to the terminal. On this app those frames hold live
        secrets: the store pickle key, the access token, and the key-export
        passphrase (all visible in nio's restore/load-store call chain). Drop
        the locals so a crash can never leak them."""
        from rich.segment import Segments
        from rich.traceback import Traceback

        self.bell()
        traceback = Traceback(show_locals=False, width=None)
        self._exit_renderables.append(
            Segments(self.console.render(traceback, self.console.options))
        )
        self._close_messages_no_wait()

    def action_about(self) -> None:
        if not isinstance(self.screen, AboutScreen):
            self.push_screen(AboutScreen())

    def __init__(self, cfg: Config) -> None:
        super().__init__()
        self.cfg = cfg
        self.session = MatrixSession(cfg)
        # Repaint the open room when its background member-list fetch lands
        # and raw @user:server ids can resolve to display names.
        self.session.on_members_loaded = self._on_members_loaded
        # Likewise repaint the dashboard when a background name fetch lands.
        self.session.on_names_loaded = self.action_refresh_home
        self.fatal: str | None = None
        # Connection health for the footer's ConnStatus: monotonic time of the
        # last successful sync (None until the first one lands) and whether
        # the most recent sync attempt succeeded.
        self.last_sync_at: float | None = None
        self.sync_ok = True
        # Digits of a vim count being typed, mirrored into the footer's
        # ConnStatus corner (see VimCount).
        self.pending_count = ""

    async def on_mount(self) -> None:
        await self.push_screen(LoadingScreen())
        self.startup()

    @work
    async def startup(self) -> None:
        loading = self.screen
        host = self.cfg.homeserver.replace("https://", "").replace("http://", "")
        loading.set_status(f"connecting to {host}")
        try:
            ok, message = await self.session.connect(progress=loading.set_status)
        except Exception as exc:
            # With max_timeouts set, offline network calls raise instead of
            # retrying forever inside nio; anything connect cannot name
            # should end in the clean fatal exit, not a worker traceback.
            ok, message = False, f"could not connect: {exc}"
        if not ok:
            self.fatal = message
            self.exit()
            return
        # The saved timezone override applies to every timestamp rendered
        # from here on; an unknown zone name falls back to system local.
        _set_display_timezone(str(self.session.get_setting("timezone", "") or ""))
        try:
            await self.session.initial_sync(progress=loading.set_status)
        except Exception as exc:
            # Anything initial_sync cannot handle would abort this worker
            # and dump a traceback instead of starting the app. The
            # dashboard can open on cached data; sync_loop retries.
            self.notify(
                f"Initial sync failed ({type(exc).__name__}); showing cached "
                "data while the background sync retries.",
                severity="warning",
                markup=False,
            )
        loading.set_status("ready")
        await self.switch_screen(HomeScreen())
        if self.cfg.room:
            entry = self.session.find_room(self.cfg.room)
            if entry is not None:
                self.open_room(entry)
            else:
                self.notify(
                    f"Default room {self.cfg.room!r} not found among joined "
                    "rooms.",
                    severity="warning",
                    markup=False,
                )
        self.sync_loop()

    @work(exclusive=True, group="sync")
    async def sync_loop(self) -> None:
        # The first request uses timeout=0 so it returns immediately: a
        # forced refresh (ctrl+r restarts this worker) updates right away
        # instead of after up to 30s of empty long-poll.
        timeout = 0
        while True:
            try:
                # The wait_for is the only bound on a half-open connection:
                # nio passes no HTTP timeout that covers a dead socket.
                resp = await asyncio.wait_for(
                    self.session.client.sync(timeout=timeout, full_state=False),
                    timeout=30 if timeout == 0 else timeout / 1000 + 30,
                )
            except Exception:
                # Event callbacks run inside sync(); an unexpected raise
                # would kill this worker and silently freeze every live
                # update for the rest of the session.
                self.sync_ok = False
                await asyncio.sleep(5)
                continue
            timeout = 30000
            if isinstance(resp, SyncResponse):
                self.sync_ok = True
                self.last_sync_at = time.monotonic()
                # An offline launch skipped the startup keys_upload; top up
                # on the first good sync, or new senders could not open olm
                # channels to this device.
                if self.session.client.should_upload_keys:
                    try:
                        await self.session.client.keys_upload()
                    except Exception:
                        pass  # the next sync tick retries
                self.session._record_room_timestamps(resp)
                # m.space.child changes never update the raw-state child map
                # behind the Rooms column on their own; refetch only the
                # spaces this sync touched.
                for space_id in self.session.spaces_with_child_changes(resp):
                    await self.session.refresh_space_children(space_id)
                self.action_refresh_home()
                _refresh_terminal_title(self)  # unread count may have moved
                for screen in self.screen_stack:
                    if isinstance(screen, RoomScreen):
                        # A worker, NOT call_later: refresh_messages awaits
                        # the network, and on the App's own pump a stalled
                        # request blocks every key event. Exclusive per
                        # screen so a slow reload is superseded, not stacked.
                        screen.run_worker(
                            screen.refresh_messages(),
                            group="refresh_messages",
                            exclusive=True,
                        )
            elif getattr(resp, "status_code", None) == "M_UNKNOWN_TOKEN":
                # Token revoked (signed out elsewhere): retrying can never
                # succeed; drop the cached token and exit so the next launch
                # logs in fresh.
                self.session.cfg.clear_token()
                self.fatal = (
                    "This session was signed out by the server. "
                    "Start the app again to log in fresh."
                )
                self.exit()
                return
            else:
                # Other error responses return immediately (no long-poll), so
                # back off instead of hammering the server in a tight loop.
                self.sync_ok = False
                await asyncio.sleep(5)

    def open_room(self, entry: Entry, jump_to: str | None = None) -> None:
        if entry.is_space:
            # A space is not a room you post in; select it on the home
            # screen instead. Reached from search results and room=.
            async def select_space() -> None:
                self.session.state["selected_space"] = entry.room_id
                self.session.cfg.save_state(self.session.state)
                await self.session.refresh_space_children(entry.room_id)
                for screen in self.screen_stack:
                    if isinstance(screen, HomeScreen):
                        await screen.refresh_data()
                        if screen is self.screen:
                            screen.query_one("#space_rooms", ListView).focus()
                        break

            self.run_worker(select_space())
            return
        screen = RoomScreen(entry)
        # Rooms open in the view mode the last "t" toggle left behind
        # (state.json). Set here rather than in RoomScreen.__init__ so
        # ThreadScreen, which inherits it, always starts unthreaded.
        screen.threaded = bool(self.session.state.get("threaded_view"))
        # A global-search message hit: once the initial load lands, the
        # screen jumps its selection to this event (unfolding its thread or
        # detaching into deep history as needed).
        screen._jump_target = jump_to
        self.push_screen(screen)
        # Apply the dashboard reorder now, invisibly behind the room just
        # pushed, so Esc back to it finds an unchanged signature and
        # repaints nothing.
        self.session.note_opening(entry.room_id)
        for s in self.screen_stack:
            if isinstance(s, HomeScreen):
                s.stale = False  # this rebuild covers anything deferred
                s.run_worker(s.refresh_data())
                break

    def action_force_refresh(self) -> None:
        """ctrl+r: resync now. Restarting the exclusive sync worker cancels
        the in-flight 30s long-poll and issues an immediate sync (its first
        request uses timeout=0), so a wedged connection is retried on the
        spot. The screens' change-detection caches are cleared first so they
        rebuild even if the sync brings nothing new."""
        if isinstance(self.screen, LoadingScreen):
            return  # startup owns the connection until the home screen is up
        for screen in self.screen_stack:
            if isinstance(screen, HomeScreen):
                screen._last_signature = None
                self.call_later(screen.refresh_data)
            elif isinstance(screen, RoomScreen):
                screen._last_seen_event = None
                # Same worker discipline as sync_loop: this await must not
                # run on the App pump.
                screen.run_worker(
                    screen.refresh_messages(),
                    group="refresh_messages",
                    exclusive=True,
                )
        self.sync_loop()

    def action_refresh_home(self) -> None:
        for screen in self.screen_stack:
            if isinstance(screen, HomeScreen):
                # Rebuilding costs real time and at peak the counts change
                # on nearly every sync; while a room covers the dashboard,
                # note the staleness and rebuild once on resume.
                if screen is self.screen:
                    self.call_later(screen.refresh_data)
                else:
                    screen.stale = True
                break

    def _on_members_loaded(self, room_id: str) -> None:
        for screen in self.screen_stack:
            if isinstance(screen, RoomScreen) and screen.entry.room_id == room_id:
                self.call_later(screen.refresh_names)

    async def on_unmount(self) -> None:
        await self.session.close()


async def _run_verify(cfg: Config) -> None:
    """Plain-terminal SAS verification. The CLI acts as the *responder*: you
    start it from Element ("Verify session"), and this answers the request,
    shows the emoji, and completes. matrix-nio can't initiate the modern
    request/ready handshake, but it can respond to one, so this is the path that
    works (and that makes Element share room keys with this device)."""
    if os.environ.get("MATRIXCLI_VERIFY_DEBUG"):
        log_path = os.path.abspath("matrixcli-verify.log")
        handler = logging.FileHandler(log_path, mode="w")
        handler.setFormatter(
            logging.Formatter("%(asctime)s %(name)s %(levelname)s %(message)s")
        )
        for name in ("matrixcli.verify", "nio"):
            lg = logging.getLogger(name)
            lg.setLevel(logging.DEBUG)
            lg.addHandler(handler)
        print(f"[debug] writing verification trace to {log_path}")

    session = MatrixSession(cfg)
    ok, message = await session.connect()
    print(message)
    if not ok:
        await session.close()
        raise SystemExit(1)

    print("\nPreparing (querying keys, clearing stale events)…")
    await session.prepare_verification()

    print(
        "\nReady. In Element, open Settings -> Sessions, find this CLI session "
        f"({session.client.device_id}), and click 'Verify session', then choose "
        "'Compare unique emoji' -> Start.\nWaiting for the request…\n"
    )

    async def announce(msg) -> None:
        print(f"  · {msg}")

    async def confirm(emoji) -> bool:
        line = "  ".join(f"{glyph} {name}" for glyph, name in emoji)
        print("\nCompare these emoji with Element:\n")
        print("    " + line + "\n")
        # A bare input() would freeze the asyncio loop (and the sync that
        # must keep flowing), and to_thread(input) cannot be cancelled, so
        # Ctrl+C would wedge until Enter. Reading via the event loop's own
        # readiness watcher keeps the prompt fully cancellable.
        print("Do they match? [y/N] ", end="", flush=True)
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[str] = loop.create_future()

        def _read_line() -> None:
            line = sys.stdin.readline()
            if not fut.done():
                fut.set_result(line)

        try:
            loop.add_reader(sys.stdin.fileno(), _read_line)
        except (OSError, ValueError):
            # stdin is not selectable (e.g. redirected from a regular
            # file); fall back to the thread, accepting the Ctrl+C wedge.
            raw = await asyncio.to_thread(sys.stdin.readline)
        else:
            try:
                raw = await fut
            finally:
                loop.remove_reader(sys.stdin.fileno())
        if not raw:  # EOF
            return False
        return raw.strip().lower() in ("y", "yes")

    try:
        result = await session.verify_interactive(confirm, announce)
    finally:
        await session.close()
    print("\n" + result)


async def _run_import_keys(cfg: Config, infile: str) -> None:
    """Import Megolm room keys exported from another client (Element: Settings
    -> Security & Privacy -> Export E2E room keys). This is how the CLI gets
    history older than this device, since matrix-nio has no key-backup support.
    Prompts for the export passphrase (hidden input)."""
    path = Path(infile).expanduser()
    if not path.is_file():
        raise SystemExit(f"No such file: {path}")

    session = MatrixSession(cfg)
    ok, message = await session.connect()
    print(message)
    if not ok:
        await session.close()
        raise SystemExit(1)

    try:
        passphrase = getpass.getpass("Export passphrase: ")
    except EOFError:
        passphrase = ""

    print("Importing keys...")
    try:
        await session.import_keys(str(path), passphrase)
    except Exception as exc:
        await session.close()
        raise SystemExit(f"Import failed: {exc}")
    await session.close()
    print(
        "Done. Keys imported. Launch the client (poetry run matrix) and older "
        "encrypted messages should now decrypt."
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Terminal Matrix client.")
    parser.add_argument(
        "-c", "--config", help="Path to config.ini (default: ./ or ~/.config/matrixcli/)"
    )
    parser.add_argument(
        "--verify",
        action="store_true",
        help="Verify this session via emoji (SAS), started from another device.",
    )
    parser.add_argument(
        "--import-keys",
        metavar="FILE",
        help="Import Megolm room keys exported from another client (Element: "
        "Export E2E room keys) to decrypt older history.",
    )
    args = parser.parse_args()

    try:
        cfg = Config.load(Path(args.config).expanduser() if args.config else None)
    except (OSError, ValueError) as exc:
        # OSError also covers an unwritable or blocked [storage] path; same
        # clean message, not a traceback.
        raise SystemExit(str(exc))

    if not cfg.homeserver or "@you:" in cfg.user_id or not cfg.user_id:
        raise SystemExit(
            f"Edit {cfg.config_path} with your real homeserver and user id first."
        )

    # A second live instance corrupts the shared crypto store and caches
    # (last writer wins); refuse to start while one is running.
    cfg.acquire_instance_lock()

    # Probe the keyring now: without a usable backend every later credential
    # access raises deep inside the app; here it can be an actionable
    # message.
    try:
        cfg.load_token()
    except keyring.errors.KeyringError as exc:
        raise SystemExit(
            f"No usable system keyring: {exc}\n"
            "matrixcli keeps your password, access token, and cache keys in "
            "the system keyring (macOS Keychain; Secret Service or KWallet "
            "on Linux). On Linux, install and unlock one (e.g. gnome-keyring "
            "or KWallet), then store your password with:\n"
            f"  {cfg.store_password_hint()}"
        )

    if args.verify:
        asyncio.run(_run_verify(cfg))
        return

    if args.import_keys:
        asyncio.run(_run_import_keys(cfg, args.import_keys))
        return

    app = MatrixApp(cfg)
    app.run()
    if app.fatal:
        raise SystemExit(app.fatal)


if __name__ == "__main__":
    main()
