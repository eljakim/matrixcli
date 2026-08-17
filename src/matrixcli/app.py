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
import os
import re
import subprocess
import sys
import time
import unicodedata
from pathlib import Path
from uuid import uuid4

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
from textual.widgets import (
    Footer,
    Header,
    Input,
    Label,
    ListItem,
    ListView,
    Rule,
    Static,
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
    fold_edits,
    fold_text,
)
from .config import Config


# Colors that stay readable on a black background (no navy/maroon/dark grays).
# Each other participant is assigned one deterministically from their user id,
# so a given person always renders in the same color. My own name is orange.
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


# Only http(s): the URL ends up as an argument to the system's URL handler, and
# schemes like file:, javascript: or smb: coming from a remote sender have no
# business being opened on one keystroke. Brackets, quotes and whitespace end
# the match, so the closing ")" of a markdown [label](url) link stays out of it.
URL_RE = re.compile(r"https?://[^\s<>\"'`\[\]{}()]+", re.IGNORECASE)


def _find_urls(text: str) -> list[tuple[int, int, str]]:
    """(start, end, url) for every link in ``text``, in reading order. Sentence
    punctuation directly after a URL is stripped: people write "see https://x.y."
    and mean the sentence to end there."""
    spans = []
    for match in URL_RE.finditer(text or ""):
        url = match.group(0).rstrip(".,;:!?")
        if url.endswith("//"):
            continue  # the strip ate the whole host, e.g. a bare "https://."
        spans.append((match.start(), match.start() + len(url), url))
    return spans


# The out-of-the-box quick-reaction row ("a" then a digit); once reactions
# have been sent, the ones actually used bubble to the front (see ReactScreen).
DEFAULT_QUICK_REACTIONS = ["👍", "✅", "❤️", "😂", "🎉", "😮", "👀", "🙏", "➕"]

_EMOJI_NAMES: list[tuple[str, str]] | None = None


def _emoji_names() -> list[tuple[str, str]]:
    """(emoji, lowercase Unicode name) pairs for the reaction search, built
    once on first use from the emoji blocks. The formal names are good
    search keys ("giraff" finds GIRAFFE, "thumbs" THUMBS UP SIGN) and need
    no emoji-database dependency."""
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


def _safe_localtime(ts_ms: int):
    """localtime of a server timestamp, or None when it is outside the
    platform's range: origin_server_ts is whatever some federated server put
    on the wire, and one absurd value must not crash the room render."""
    try:
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


# Fallback for uploads whose info block advertises no mimetype (the spec makes
# it optional); anything Pillow can plausibly open by filename.
IMAGE_EXTENSIONS = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".tiff", ".ico")


def _color_depth(color_system: str | None) -> str:
    """The preview's capability tier for a rich color system: "truecolor"
    (full-color blocks), "256" (dithered blocks), or "basic" (16 colors or
    none: color art is hopeless, only the ASCII ramp is offered)."""
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
    """The image as classic ASCII art fitted into max_w x max_h cells: one
    glyph per pixel, picked from ``ramp`` (most ink first) by luminance.
    ramp[0] paints the brightest pixels, since dense glyphs read bright on a
    dark terminal. A cell is about twice as tall as it is wide, so the pixel
    grid is squeezed to half height to preserve the image's aspect."""
    from PIL.Image import Resampling

    gray = img.convert("L")
    scale = min(max_w / gray.width, 2 * max_h / gray.height)
    w = max(1, round(gray.width * scale))
    h = max(1, round(gray.height * scale / 2))
    # LANCZOS: proper area-averaging on the way down; the default (bicubic)
    # visibly aliases on diagonals at these tiny output sizes.
    px = gray.resize((w, h), Resampling.LANCZOS).load()
    span = len(ramp) - 1
    # no_wrap: rendered against a stale mid-resize width, a wrapping line
    # would spill one cell onto a blank row and stripe the whole picture;
    # cropping is invisible by comparison (a follow-up render fixes the size).
    text = Text(no_wrap=True)
    for y in range(h):
        line = "".join(ramp[(255 - px[x, y]) * span // 255] for x in range(w))
        text.append(line + ("\n" if y + 1 < h else ""))
    return text


# The 240 predictable xterm-256 entries (6x6x6 color cube + 24 grays), for
# dithering on non-truecolor terminals. The first 16 system colors are left
# out: terminals theme those freely, so their real RGB is unknowable.
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
    each cell is one "▀" whose foreground is the upper pixel and background
    the lower. The two stacked pixels fill the cell's ~1:2 shape, so pixels
    come out square and no aspect correction is needed.

    Returns Textual's native Content with ready-made Style objects: a rich
    Text here costs a style-string parse plus a rich-to-Textual conversion
    per span at paint time, and with thousands of unique colors defeating
    every cache that machinery dominates how long a full-screen photo takes
    to draw.

    ``dither`` is for non-truecolor terminals: the terminal would snap each
    RGB to the nearest 256-palette entry, posterizing smooth gradients into
    flat patches. Floyd-Steinberg dithering onto the same cube+gray palette,
    emitted as exact palette indices, trades those patches for fine noise,
    which reads far better at cell scale."""
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
        # Emit runs of identical color pairs as one span, not one span per
        # cell: photos still make many spans, but flat areas (and
        # screenshots are mostly flat) collapse to a few.
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


# The sync loop long-polls for 30s, so a healthy session sees a response at
# least that often; once the last one is older than this, the connection is
# presumed dead even if no request has errored out yet (a silently dropped
# network hangs the poll without raising until much later).
STALE_AFTER = 45.0


class ConnStatus(Static):
    """Connectivity/staleness indicator in the footer's bottom-right corner.

    Shows a green dot plus the age of the last successful sync while the
    connection is healthy, and a red "offline" marker with the same age (now:
    how stale everything on screen is) when the last sync failed or none has
    completed within STALE_AFTER. Re-rendered on a 1s timer so the age ticks.
    """

    def on_mount(self) -> None:
        # layout=True: the text width changes as the age grows ("59s" -> "1m",
        # "● 5s" -> "✗ offline 2m") and the dock: right slot must resize.
        self.set_interval(1.0, lambda: self.refresh(layout=True))

    def render(self) -> Text:
        last = getattr(self.app, "last_sync_at", None)
        ok = getattr(self.app, "sync_ok", True)
        if last is None:
            return Text("")  # still starting up; nothing meaningful to show
        age = max(0.0, time.monotonic() - last)
        if age < 60:
            age_str = f"{int(age)}s"
        elif age < 3600:
            age_str = f"{int(age // 60)}m"
        else:
            age_str = f"{int(age // 3600)}h"
        if ok and age <= STALE_AFTER:
            return Text.assemble(("● ", "green"), (age_str, "dim"))
        return Text.assemble(("✗ offline ", "bold red"), (age_str, "red"))


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
        # still paint one frame from its stale map before the next reflow
        # drops the widget; TextArea's per-frame theme application then
        # crashes the app on the half-torn-down state (closing the composer
        # no longer forces the timeline reflow that used to hide this
        # window). The editor is gone from the layout anyway: hand the
        # compositor blank lines instead of rendering.
        if not self.is_attached:
            from textual.strip import Strip

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
        # TextArea's own _on_key turns Enter into an inserted newline and stops
        # the event before bindings run, so the enter->send binding above is
        # footer decoration only; the real interception has to happen here.
        # Shift+Enter is not consumed by TextArea, so its binding fires
        # normally.
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
        # One send per editor instance: the editor stays mounted during the
        # network round-trip, so a second Enter would otherwise queue a
        # duplicate send. Every redraw mounts a fresh instance, resetting this.
        if self._submitted:
            return
        self._submitted = True
        self.post_message(self.Submitted(self.text))

    def action_cancel(self) -> None:
        self.post_message(self.Cancelled())


class RoomScreen(Screen):
    BINDINGS = [
        ("escape", "back", "Back"),
        # Straight to the dashboard, however deep the page stack is; only
        # the dashboard's own q quits (":q!" quits from anywhere).
        Binding("q", "app.go_home", "Home", show=False),
        ("j", "down", "Down"),
        ("k", "up", "Up"),
        Binding("down", "down", "Down", show=False),
        Binding("up", "up", "Up", show=False),
        # Vim's g/G: g browses the room's archived history from its very
        # first message (detaching from the live tail; j at the bottom walks
        # forward), or falls back to the first loaded message when nothing
        # is archived; G jumps to the newest message and reattaches.
        Binding("g", "first_message", "First", show=False),
        Binding("G", "last_message", "Last", show=False),
        # Gated by check_action to the messages they can actually act on, so
        # the footer only offers them when there is a thread to unfold/fold.
        ("l", "expand", "Unfold thread"),
        ("h", "collapse", "Fold thread"),
        ("u", "first_unread", "First unread"),
        Binding("slash", "search_room", "Search", show=False),
        # One key, three labels: check_action leaves exactly the one enabled
        # that says what Enter will do to the selected message (nothing at all
        # for a plain one), so the footer never promises what it cannot do.
        # The reactions popup is the spacebar's job below; Enter reaches it
        # only through the actions menu of a message that offers more.
        Binding("enter", "open_download", "Download"),
        Binding("enter", "open_link", "Open link"),
        Binding("enter", "open_actions", "Message actions"),
        # Two bindings share the spacebar like the "t"/"c" pairs: an image
        # upload gets an in-terminal rendering of the picture (ASCII art or
        # truecolor half-blocks, "~" in the popup flips between them), any
        # other message wearing reaction badges opens who sent them. Inside
        # either popup the spacebar closes it again, so the key is a toggle.
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
        # Two bindings share "c" like the "t" pair: check_action enables the
        # one naming the current spacing, so the footer reads as state.
        Binding("c", "compact_on", "Compact: off"),
        Binding("c", "compact_off", "Compact: on"),
        # Same pattern for "~" (vim's toggle-the-form key): the enabled label
        # names what the sender column currently shows (display names or raw
        # @user:server ids).
        Binding("tilde", "ids_on", "Show: names"),
        Binding("tilde", "ids_off", "Show: ids"),
    ]

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
        # Browse mode ("g"): a snapshot of the room's archived history while
        # the view is detached from the live tail and walking it from the
        # very first message; None when following the tail as usual. The
        # counter is how many snapshot rows are in the view so far ("j" at
        # the bottom extends it; "G" or walking past the end reattaches).
        self._browse: list | None = None
        self._browse_upto = 0
        # The room's fully-read marker as it was when this screen opened;
        # everything after it renders below a "new" divider, and "u" jumps
        # there. Frozen at open so live arrivals stay marked until you leave.
        self._opened_read_marker: str | None = None
        # Event id of the first unread message, chosen once from the opening
        # snapshot (see on_mount); the divider is anchored to it thereafter.
        self._first_unread_event: str | None = None
        # False until _finish_mount's full history fetch has run once;
        # refresh_messages stays a no-op before that (the opening load ends by
        # syncing _last_seen_event itself, so nothing is lost by waiting).
        self._loaded = False
        self._composing = False  # True while an "n" new-message editor is open
        self._editing = None  # own Message being rewritten with "e", or None
        self._last_seen_event: str | None = None  # latest event id we rendered
        # What _redraw last mounted: the per-message signature list, the
        # view-mode tuple, and the (sender, day) carry at the tail, so a
        # tail-append can continue instead of rebuilding everything.
        self._drawn_sig: list | None = None
        self._drawn_mode: tuple | None = None
        self._drawn_tail: tuple = (None, None)
        # Local echoes still in flight: shown in gray immediately on Enter and
        # spliced back into every reload so a background sync cannot drop them
        # before the server confirms (see _finish_send).
        self._pending: list = []
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
        """The message list this screen renders. Normal view: the room's main
        timeline with thread replies collapsed out (they live in their own
        ThreadScreen) and per-root reply counts collected for the ⤷ badges.
        Threaded view: replies render indented directly under their root, and
        replies whose root fell out of the history window stay inline at their
        own position so nothing is hidden. ThreadScreen overrides this to load
        a single thread instead. cached_only skips the network and serves
        whatever the timeline cache already holds; on_mount uses it to paint
        instantly before the (slow) full fetch."""
        if self._browse is not None:
            # Browse mode: the view is exactly the archive rows loaded so far
            # (kept in self.older) with the live window deliberately absent;
            # everything below (edit folding, thread collapse) still applies.
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
        """Append the in-flight local echoes to a freshly built display list.
        Reloads rebuild from the session cache, which knows nothing about a
        message whose send has not returned yet; without this it would blink
        out of the timeline whenever a sync lands during the round-trip.

        An echo whose real event already arrived via sync (outrunning the
        /send response, so the echo still has its provisional id) is skipped
        rather than shown next to it. Matching is one confirmed message per
        echo, so sending the same text twice still shows two entries."""
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
                    # now can be this echo's confirmation, not an identical
                    # text from an hour ago.
                    and abs(m.ts - p.ts) < 5 * 60 * 1000
                ),
                None,
            )
            if arrived is not None:
                claimed.add(arrived.event_id)
                continue
            messages.append(p)
        return messages

    async def on_mount(self) -> None:
        self.title = self.entry.title
        self.sub_title = self.entry.room_id
        # Keep the timeline unfocusable so arrow keys always reach the
        # screen's selection bindings instead of scrolling the container.
        self.query_one("#timeline", VerticalScroll).can_focus = False
        room = self.app.session.client.rooms.get(self.entry.room_id)
        self._opened_read_marker = getattr(room, "fully_read_marker", None)
        self._reset_history_position()
        # Paint whatever the sync-seeded cache already holds before touching
        # the network: the full /messages fetch takes seconds on a slow
        # homeserver, and a shallow timeline right away beats a blank
        # screen. The unread divider waits for the full window below, whose
        # count fallback needs the complete opening snapshot.
        quick = await self._load_messages(cached_only=True)
        if quick:
            self.messages = quick
            self.selected = len(quick) - 1
            await self._redraw()
        # The full fetch goes to the network; on this screen's message pump it
        # would block every key bound here (q, escape, :) until it returned,
        # and Textual's shutdown waits on this pump, so quitting would hang
        # too. In a worker the screen stays interactive from the first paint.
        self.run_worker(self._finish_mount(), group="initial_load", exclusive=True)

    async def _finish_mount(self) -> None:
        """The slow half of on_mount, run as a worker: the full history fetch,
        the unread divider (whose count fallback needs the complete opening
        snapshot), and the opening read receipt."""
        try:
            messages = await self._load_messages()
            if not self.is_attached:
                return  # backed out of the room while the fetch was in flight
            # The quick paint made the screen interactive during the fetch; if
            # the user moved off the tail meanwhile, keep their place (by event
            # id, since the full window may have inserted rows above it).
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
            # in _first_unread_index is only meaningful against the opening
            # snapshot, and recomputing it as live messages grow the list
            # would drift the divider onto messages that arrived while you
            # were reading.
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
                # Auto-open the composer in an empty room as real state, not
                # an unconditional mount, so Escape can cancel it and leave
                # the room.
                self._composing = True
            self._last_seen_event = self.app.session.last_event_id.get(
                self.entry.room_id
            )
            await self._redraw(keep_scroll=not follow)
            await self.app.session.mark_read(self.entry.room_id)
            # After the visible window is up: download the room's ENTIRE
            # history in the background, so everything from the first message
            # on survives locally (deletions and edit versions included).
            # No-op when the room is already archived or downloading; a
            # ThreadScreen mount kicks the same room, equally a no-op.
            self.app.session.start_backfill(self.entry.room_id)
        finally:
            # Even on a failed or superseded load, hand live updates over to
            # refresh_messages; its next run reloads everything this one
            # missed (the guard below keeps the two from interleaving).
            self._loaded = True

    async def refresh_names(self) -> None:
        """Called by the app when the background member fetch for this room
        lands (launch syncs members lazily, see MatrixSession._fetch_members):
        senders that painted as raw @user:server ids get their display names.
        Selection and scroll stay put; only the text changes."""
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
        # The sync loop runs this as a worker; by the time it runs (or
        # resumes from the history await below) the user may have popped the
        # screen, whose widgets are then gone. Before the opening load has
        # finished it would also race _finish_mount, which picks up anything
        # that arrives meanwhile itself.
        if not self.is_attached or not self._loaded:
            return
        # Browsing history from the top: a reload would clobber the detached
        # view with the live tail. _last_seen_event stays untouched, so the
        # first refresh after leaving browse mode catches everything up.
        if self._browse is not None:
            return
        latest = self.app.session.last_event_id.get(self.entry.room_id)
        if not latest or latest == self._last_seen_event:
            return
        prev_counts = self.thread_counts
        messages = await self._load_messages()
        if not self.is_attached:
            return
        # Only now mark the batch seen: as an exclusive worker this reload can
        # be cancelled mid-await by the next sync's, and marking before the
        # load would make that next run skip the batch entirely.
        self._last_seen_event = latest
        # A new thread reply changes only a badge count, not the main list.
        if (
            self._signature(messages) != self._signature(self.messages)
            or self.thread_counts != prev_counts
        ):
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
            # The reload built fresh Message objects; re-point the reply target
            # at its new incarnation, so the composer's header keeps quoting
            # the current text of the message being replied to.
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
        message. If an entry (and the _render_mode below) is unchanged, the
        widget on screen is still correct; _redraw compares these to skip
        work. A message also renders a quote of its reply TARGET, but any
        change to the target changes the target's own entry, which breaks
        the prefix match and forces the full rebuild that refreshes the
        quote."""
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
                # The header is only drawn on a sender change; without this
                # the first message of a day inherits suppression from the
                # same sender's last message of the previous day and renders
                # attributed to nobody.
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
        """Bring the timeline widgets in line with self.messages.
        Serialized by a lock: two interleaved rebuilds would duplicate
        widgets. The composer is not part of this: it lives in its own
        docked panel (see _sync_composer), so a rebuild can never swallow a
        half-typed draft.

        The mounted widgets are tracked by signature so the common live
        cases stay cheap: messages appended at the tail mount only the new
        rows, and a refresh that changed nothing visible keeps every widget
        in place. Anything else (an edit, a deletion, a reaction, a view
        toggle, cap eviction at the head) rebuilds the whole list, batched
        into a single mount call. A full teardown-and-remount used to cost
        ~300 ms of blocked event loop per sync tick on a back-paginated
        timeline.

        keep_scroll: restore the current scroll offset instead of snapping to
        the highlighted message. Live refreshes use it so a message arriving
        while the user has mouse-scrolled away does not yank the view back."""
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
                # After layout settles, put the view back where it was; new
                # content only grew the bottom, so the offset still points at
                # the same rows.
                self.call_after_refresh(
                    lambda: tl.scroll_to(y=scroll_y, animate=False)
                )
            else:
                self._highlight()
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
        editor is mounted once and then left alone, so live refreshes cannot
        disturb what you are typing; the panel's fixed height means a long
        draft scrolls inside it instead of running off the bottom of the
        screen."""
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
        if not self.messages:
            return
        if self._browse is not None and self.selected >= len(self.messages) - 1:
            # Bottom of the browse view: pull the next chunk of archived
            # history into it, or reattach to the live tail once the
            # snapshot is walked dry.
            self.run_worker(
                self._extend_browse(), group="browse", exclusive=True
            )
            return
        self.selected = min(self.selected + 1, len(self.messages) - 1)
        self._highlight()

    def action_up(self) -> None:
        if not self.messages:
            return
        if self.selected == 0:
            self._fetch_older()
            return
        self.selected -= 1
        self._highlight()

    # How much archived history "g" loads at once, and each "j" at the bottom
    # of the browse view adds. Widgets are not virtualized, so the chunk is
    # what keeps a 20k-message room from mounting 20k widgets in one go.
    BROWSE_CHUNK = 200

    def action_first_message(self) -> None:
        """g: jump to the room's first message. With archived history this
        detaches from the live tail and browses the archive from the very
        beginning; without any (download not started, caching off, thread
        view) it falls back to the first loaded message."""
        if self._browse is None:
            rows = self.app.session.archive_rows(self.entry.room_id)
            if rows:
                self._browse = rows
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
        live tail; otherwise it just jumps there."""
        if self._browse is not None:
            self.run_worker(self._exit_browse(), group="browse", exclusive=True)
            return
        if self.messages:
            self.selected = len(self.messages) - 1
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
        self.older = self._browse[: self._browse_upto]
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

    async def _exit_browse(self) -> None:
        self._browse = None
        self._browse_upto = 0
        self.older = []
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
        """Called once on mount. The messages a previous visit back-paginated
        died with its screen (self.older starts empty), so the session-held
        continuation token must die too, or scrolling up would resume from the
        old depth and silently skip everything in between. ThreadScreen
        overrides this to a no-op: opening a thread must not discard the
        position of the room screen still live beneath it."""
        self.app.session.reset_pagination(self.entry.room_id)

    def _fetch_older(self) -> None:
        """Back-paginate when the selection pushes past the top: fetch an
        older batch, splice it in, and land the selection on the message just
        above the previous top. ThreadScreen overrides this to a no-op (a
        thread is already loaded whole via /relations)."""
        if self._paginating or self._at_beginning:
            return
        if self._browse is not None:
            return  # the view already starts at the room's first message
        self._paginating = True

        async def fetch() -> None:
            try:
                known = {m.event_id for m in self.messages}
                known.update(m.event_id for m in self.older)
                # A batch can consist entirely of events we already have
                # (e.g. the first call re-covers the initial window, or
                # non-message events were filtered out); skip past those,
                # bounded so a dead room cannot loop forever.
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

    # The timestamp fallback below asks the server about a marker that is
    # not a display row; threads opt out (their override only trusts a
    # marker inside the thread, so the fetch would be wasted).
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
        # The marker exists but is not a row here (a reaction or redaction
        # id, or an event beyond the loaded window). Its fetched timestamp
        # is the exact read horizon: the first newer message starts the
        # unread run, and no newer message means everything was read.
        if marker_ts is not None:
            return next(
                (i for i, m in enumerate(self.messages) if m.ts > marker_ts),
                None,
            )
        # No marker recorded, or its timestamp could not be fetched: fall
        # back to the unread count the room reported when opened; without
        # either signal, treat the room as read rather than flagging
        # everything.
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
                # The spacebar owns the reactions popup: a message whose only
                # Enter action is its reactions is never an image (images
                # always offer a download too), so its spacebar is free, and
                # a second footer entry promising the popup on Enter would be
                # noise.
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
            # Only an unconfirmed local echo renders gray; a delivered own
            # message uses the same color as everyone else's (the orange name
            # already marks it as ours), so gray unambiguously means "not on
            # the server yet". _finish_send flips it on confirm.
            style = "grey50" if m.pending else ""
            text = Text(body, style=style)
            for start, end, url in _find_urls(body):
                # "link <url>" makes Rich emit an OSC 8 hyperlink, so the URL is
                # also mouse-clickable in terminals that support it; the
                # underline marks it in the ones that do not.
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
                # An app worker, same as _finish_send: a screen worker dies
                # with the screen, and leaving the room during the round-trip
                # must not silently drop the reaction.
                self.app.run_worker(self._finish_react(m, key))

        self.app.push_screen(ReactScreen(self.entry, m), when_picked)

    async def _finish_react(self, m, key: str) -> None:
        session = self.app.session
        ok, info, added = await session.toggle_reaction(
            self.entry.room_id, m.event_id, key
        )
        if not ok:
            # The room is named because the user may have moved on meanwhile
            # and the toast can appear anywhere in the app (as _finish_send).
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
                # An app worker, same as _finish_send: a screen worker dies
                # with the screen, so confirming and then leaving the room
                # during the round-trip would silently skip the deletion.
                self.app.run_worker(self._finish_delete(m))

        first_line = ((m.body or "").strip().splitlines() or [""])[0][:60]
        self.app.push_screen(
            ConfirmScreen(f"Delete this message?\n\n{first_line}"), when_answered
        )

    async def _finish_delete(self, m) -> None:
        ok, info = await self.app.session.redact(self.entry.room_id, m.event_id)
        if not ok:
            # The room is named because the user may have left it meanwhile
            # and the toast can appear anywhere in the app (as _finish_send).
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

    def action_search_room(self) -> None:
        """"/": search the loaded history and jump to the picked hit."""

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

        self.app.push_screen(RoomSearchScreen(list(self.messages)), when_picked)

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
                    # user asked for (screen workers die with the screen).
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
        # Stash (or clear) the draft before the composer unmounts: Escape must
        # never cost a paragraph. Typing r/R again hands it back. Cancelled
        # edits are not stashed: their text is the message's own, not a draft.
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
        from .client import Message

        # Optimistic local echo: show the message (in gray) and close the
        # editor immediately, then do the network round-trip in a worker.
        # The provisional "~local." id keeps it distinct in every event-id
        # keyed merge until the server assigns the real one.
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
        # An app worker, not a screen worker: unmounting a widget cancels its
        # workers, so a screen-owned send would be killed mid-flight by an
        # Escape during the round-trip, and the message would silently never
        # go out.
        self.app.run_worker(self._finish_send(echo, text, reply, kwargs))

    async def _finish_edit(self, target, text: str) -> None:
        ok, info = await self.app.session.send_edit(
            self.entry.room_id, target, text
        )
        if not ok:
            # Same shape as _finish_send: report even after leaving the room,
            # and park the rewrite in the drafts so it is not lost.
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
            # Before the is_attached gate: a failure must be reported even if
            # the user already left the room, or the message is lost with no
            # notice. The room is named because the toast can now appear
            # anywhere in the app.
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
            # that delivers our event can carry another user's message that
            # landed just before it, and pre-marking ours as seen would make
            # refresh_messages skip that batch entirely, hiding the other
            # message until something else bumps the room. The refresh this
            # costs is cheap (cache-served, signature-compared).
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
            # Sending closed the composer, so the redraw above reopened it
            # empty; refill it with the failed text. If the user already
            # started a new draft during the round-trip, leave that one alone.
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

    def action_first_message(self) -> None:
        # A thread is already loaded whole via /relations; "g" is a plain
        # jump to its root, never the room-archive browse the base class
        # would enter (that belongs to the room screen underneath).
        if self.messages:
            self.selected = 0
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
        # The room screen reloads from the timeline cache; this screen's
        # reload refetches the whole thread over /relations (seconds on a
        # loaded homeserver) and used to be triggered by EVERY room event,
        # nearly all of which have nothing to do with this thread. Only pay
        # for the fetch when the local caches show evidence the thread
        # changed; a quiet tick still advances the seen marker and the read
        # receipt.
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
            # escape(): the filename comes from the sender; unescaped, "[" in it
            # is parsed as console markup (crash on "[/", spoofed styling, even
            # a live [@click] action span). Truncated so a long name cannot blow
            # out the dialog.
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
            # escape(): labels quote sender-controlled text (URLs, filenames),
            # and unescaped a "[" in one is parsed as console markup (crash on
            # "[/", spoofed styling). Truncated so a long one cannot blow out
            # the dialog.
            item = ListItem(Label(escape(label[:200])))
            item.action = action
            await lv.append(item)
        lv.index = 0
        lv.focus()

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        self.dismiss(getattr(event.item, "action", None))


class PreviewScreen(ModalScreen):
    """An image upload rendered in the terminal (space on an image message):
    truecolor half-blocks (the default), or classic ASCII art built from
    the configured ramp. "~" flips between the two styles, and the choice
    is remembered in state.json; j/k walk to the room's next/previous image
    without leaving the preview; Escape (or q) closes, dismissing with the
    event id last shown so the timeline selection can follow. Each image is
    fetched once, scaled to the window, and rescaled on every resize."""

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
        # Two bindings share "~" like the room screen's t/c pairs:
        # check_action enables the one whose label names the style currently
        # on screen, so the footer reads as state.
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
        # 16-color-or-less terminals get no say: block art needs at least the
        # 256-color palette to dither onto, so only the ramp is honest there.
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
            # Ask the server for a thumbnail at twice what this window can
            # show (half-blocks paint two pixel rows per cell): servers snap
            # thumbnail requests to pre-generated buckets, and the headroom
            # keeps a portrait photo out of a tiny bucket whose upscaled JPEG
            # blocks would dominate the render. Encrypted media falls back to
            # the full download inside fetch_preview_bytes.
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
                # The imports sit inside the try: a broken Pillow install
                # must degrade to an error line in the popup, not crash the
                # worker (and with it the app).
                try:
                    from io import BytesIO

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
        # A render failure (an exotic image mode, a Pillow quirk) must land
        # in the popup as text, never take down the app: this runs on plain
        # UI paths like resize, outside any worker's safety net.
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
        # A cheap full re-render from the already-decoded image, deferred
        # past the layout pass: this Resize arrives before the children have
        # their new sizes, and measuring #previewbox now would rebuild the
        # art against the stale geometry.
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
            # the popup and Enter opening it.
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


class CommandScreen(ModalScreen):
    """":" anywhere outside a text editor: a one-line vim-style command
    entry docked at the bottom, dismissing with the typed command on Enter
    (None on Escape). The app interprets the result: "q!" quits
    unconditionally, "q" closes the page like the q key."""

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
    """"/" inside a room: accent-insensitive search over the loaded history
    (including anything paged back this visit), newest hit first. The query
    matches the message text, the sender's display name, or their matrix id,
    so "agnes" finds what Ágnes said as well as messages that mention her.
    Enter dismisses with the chosen message's event id; the room screen jumps
    its selection there. Searches what is loaded, not the server: at IOI
    scale that is the recent traffic being triaged."""

    BINDINGS = [
        ("escape", "dismiss", "Close"),
        Binding("down", "cursor_down", "Down", show=False),
        Binding("up", "cursor_up", "Up", show=False),
    ]

    def __init__(self, messages: list) -> None:
        super().__init__()
        self.messages = messages

    def compose(self) -> ComposeResult:
        with Vertical(id="searchbox"):
            yield Input(
                placeholder="Search messages and senders in loaded history…",
                id="q",
            )
            yield ListView(id="results")

    def on_mount(self) -> None:
        self.query_one("#q", Input).focus()

    async def on_input_changed(self, event: Input.Changed) -> None:
        q = fold_text(event.value.strip())
        lv = self.query_one("#results", ListView)
        await lv.clear()
        if not q:
            return
        hits = [
            m
            for m in reversed(self.messages)
            if m.event_id
            and (
                q in fold_text(m.body or "")
                or q in fold_text(m.sender_name or "")
                or q in fold_text(m.sender or "")
            )
        ]
        for m in hits[:30]:
            first_line = (m.body or "").strip().splitlines() or [""]
            # escape(): sender text; unescaped it is parsed as console markup.
            label = (
                f"[dim]{_fmt_time(m.ts)}[/dim] "
                f"{escape(m.sender_name[:20])}: {escape(first_line[0][:80])}"
            )
            item = ListItem(Label(label))
            item.event_id = m.event_id
            await lv.append(item)
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
    BINDINGS = [
        ("escape", "dismiss", "Close"),
        # The Input keeps focus while these move the result highlight, so you
        # can keep typing and pick a hit without tabbing to the list.
        Binding("down", "cursor_down", "Down", show=False),
        Binding("up", "cursor_up", "Up", show=False),
    ]

    def compose(self) -> ComposeResult:
        with Vertical(id="searchbox"):
            yield Input(placeholder="Search people and rooms…", id="q")
            yield ListView(id="results")

    def on_mount(self) -> None:
        self.query_one("#q", Input).focus()

    async def on_input_changed(self, event: Input.Changed) -> None:
        results = self.app.session.search(event.value)
        lv = self.query_one("#results", ListView)
        await lv.clear()
        for e in results[:30]:
            tag = f"  ({e.unread})" if e.unread else ""
            kind = "person" if e.is_direct else "room"
            # escape(): titles come from other users; unescaped they are parsed
            # as console markup (crash on "[x", spoofed styling).
            await lv.append(
                EntryItem(e, f"{escape(e.title)}{tag}  [dim]{kind}[/dim]")
            )
        if results:
            lv.index = 0  # Enter opens the top hit right away

    def action_cursor_down(self) -> None:
        self.query_one("#results", ListView).action_cursor_down()

    def action_cursor_up(self) -> None:
        self.query_one("#results", ListView).action_cursor_up()

    def _open(self, item) -> None:
        if isinstance(item, EntryItem):
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


class HomeScreen(Screen):
    BINDINGS = [
        ("slash", "app.search", "Search"),
        # The four movement keys take one footer slot: "hjkl" reads as a unit,
        # so only this binding is shown and the rest stay live but unlisted.
        Binding("j", "down", "Select room", key_display="hjkl"),
        Binding("k", "up", show=False),
        Binding("l", "focus_next_column", show=False),
        Binding("h", "focus_prev_column", show=False),
        Binding("tab", "focus_next_column", show=False),
        Binding("shift+tab", "focus_prev_column", show=False),
        # priority=True: the focused ListView binds the arrows (and enter)
        # itself, which would stop the cursor dead at each list's edge; these
        # route them through the same column-spilling _step as j/k, so the
        # arrows behave exactly like j/k everywhere in the app. Same for
        # Enter, which also puts "Open" in the footer (a focused widget's
        # hidden binding otherwise outranks the screen's for display).
        Binding("down", "down", show=False, priority=True),
        Binding("up", "up", show=False, priority=True),
        Binding("enter", "open_selected", "Open", priority=True),
        # Two labels for one key: check_action enables the one matching the
        # highlighted entry, and hides both on rows that cannot be tagged
        # (spaces, invites, empty placeholders).
        Binding("f", "favourite_add", "Favourite"),
        Binding("f", "favourite_remove", "Unfavourite"),
        # Two labels for one key, like "f" above but reading as state (the
        # t/c/~ pattern in rooms): shown only on a highlighted space, the
        # enabled one names whether that space's rooms are cached to disk,
        # and pressing "c" flips it. Hidden entirely when [cache] messages is
        # off in config.ini: the per-space toggle would do nothing.
        Binding("c", "cache_off", "Cache: on"),
        Binding("c", "cache_on", "Cache: off"),
        # Vim's window-resize chord (Ctrl-W +/-) boiled down to a single
        # key: with the cursor in Recent or Favourites, +/- grows/shrinks
        # that section's row budget. check_action gates them to those lists
        # and to what actually fits: neither section ever drops below
        # MIN_SECTION_ROWS, and growth stops where it would squeeze the
        # other section or run off the screen. "=" is the unshifted
        # convenience alias for "+", as in vim's own maps.
        Binding("plus", "grow_section", "More rows"),
        Binding("equals_sign", "grow_section", show=False),
        Binding("minus", "shrink_section", "Fewer rows"),
        ("q", "app.quit", "Quit"),
    ]

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
        if self.stale:
            self.stale = False
            self.run_worker(self.refresh_data())

    def compose(self) -> ComposeResult:
        yield Header()
        # VerticalScroll, not Vertical: a column whose stacked lists outgrow
        # the terminal scrolls instead of clipping (a space with many rooms
        # would otherwise push Favourites clean off the screen). Keyboard
        # focus scrolls the focused list into view on its own.
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
        # Unread items render bold; the count is appended for unread items.
        # A fixed two-cell marker slot keeps every title left-aligned: a "★"
        # for favourites, a "◦" (U+25E6) for the currently chosen space, or two
        # spaces so unmarked rows line up with the marked ones.
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
                # Remember the highlighted room by id, not position: a refresh
                # can reorder the list (a DM jumping to the top) and a
                # positional restore would silently land the cursor on a
                # different room.
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
                # One batched mount: appending row by row awaited a mount per
                # entry, ~300 ms of blocked event loop for a full dashboard.
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
        # Empty lists hold a single disabled "(none)" placeholder, so the row
        # count is not the entry count; real entries always start at index 0.
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
            # "cache_off" is the action that turns it off, so it is the one
            # offered (labelled "Cache: on") while caching is enabled.
            return enabled == (action == "cache_off")
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
        # The priority Enter binding bypasses the focused ListView's own
        # select action; delegate back to it (on_list_view_selected guards
        # against placeholder rows).
        lv = self._focused_list()
        if lv is not None:
            lv.action_select_cursor()

    def action_favourite_add(self) -> None:
        self.run_worker(self._toggle_favourite())

    def action_favourite_remove(self) -> None:
        self.run_worker(self._toggle_favourite())

    def action_down(self) -> None:
        self._step(1)

    def action_up(self) -> None:
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
            # In a worker, not on this pump: the join is a network round trip,
            # and awaiting it here froze the whole dashboard (q included)
            # whenever the connection was down.
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
            # Selecting a space loads its rooms into the column below. Refetch
            # its children first so newly added rooms show up without a
            # restart; in a worker so its 15s network timeout cannot stall the
            # dashboard's keys.
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
    #searchbox #results { max-height: 20; }
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
    AboutScreen, ConfirmScreen { align: center middle; }
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
        # "q" deliberately NOT bound here: on the dashboard it quits
        # (HomeScreen binds it), on every other page it goes back to the
        # dashboard (RoomScreen binds it), and quitting from anywhere is
        # ":q!" below. A global q made one keystroke while reading a room
        # exit the whole app.
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
        # Likewise repaint the dashboard when a background name fetch lands
        # (a DM peer's profile, a room's member list, a room's m.room.name).
        self.session.on_names_loaded = self.action_refresh_home
        self.fatal: str | None = None
        # Connection health for the footer's ConnStatus: monotonic time of the
        # last successful sync (None until the first one lands) and whether
        # the most recent sync attempt succeeded.
        self.last_sync_at: float | None = None
        self.sync_ok = True

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
            # With max_timeouts set on the client, offline network calls raise
            # instead of retrying forever inside nio; connect handles the ones
            # it can name, and anything that escapes (a keys_upload raise, a
            # transport error from the retry login) should end in the clean
            # fatal-message exit, not a worker traceback over the terminal.
            ok, message = False, f"could not connect: {exc}"
        if not ok:
            self.fatal = message
            self.exit()
            return
        try:
            await self.session.initial_sync(progress=loading.set_status)
        except Exception as exc:
            # initial_sync handles the failures it can name, but anything that
            # escapes it (a transport error from one of the other calls, a
            # raise from an event callback) would otherwise abort this worker
            # and dump a traceback over the terminal instead of starting the
            # app. The dashboard can open on cached data; sync_loop retries.
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
        # The first request uses timeout=0 so it returns immediately instead
        # of long-polling. That makes a forced refresh (ctrl+r restarts this
        # exclusive worker) update the screen and the staleness clock right
        # away rather than after up to 30s of empty long-poll.
        timeout = 0
        while True:
            try:
                # The wait_for is the only bound on a half-open connection
                # (network dropped without a TCP reset): timeout=0 makes nio
                # pass NO HTTP timeout down to aiohttp, and even the long-poll
                # request has no read timeout that covers a dead socket.
                resp = await asyncio.wait_for(
                    self.session.client.sync(timeout=timeout, full_state=False),
                    timeout=30 if timeout == 0 else timeout / 1000 + 30,
                )
            except Exception:
                # nio normally returns error *responses*, but event callbacks
                # run inside sync() and an unexpected raise from one (or from
                # the transport) would kill this worker, silently freezing
                # every live update for the rest of the session.
                self.sync_ok = False
                await asyncio.sleep(5)
                continue
            timeout = 30000
            if isinstance(resp, SyncResponse):
                self.sync_ok = True
                self.last_sync_at = time.monotonic()
                # An offline launch skips connect()'s startup keys_upload
                # (the network was down); top up one-time keys on the first
                # successful sync instead, or new senders could not open olm
                # channels to this device for the whole session.
                if self.session.client.should_upload_keys:
                    try:
                        await self.session.client.keys_upload()
                    except Exception:
                        pass  # the next sync tick retries
                self.session._record_room_timestamps(resp)
                # Rooms added to (or removed from) a space arrive as
                # m.space.child state, but the child map behind the Rooms
                # column is read over raw state and so never updates itself.
                # Refetch only the spaces this sync touched, so the column
                # keeps up without polling the state endpoint every 30s.
                for space_id in self.session.spaces_with_child_changes(resp):
                    await self.session.refresh_space_children(space_id)
                self.action_refresh_home()
                for screen in self.screen_stack:
                    if isinstance(screen, RoomScreen):
                        # A worker, NOT call_later: refresh_messages awaits the
                        # network, and call_later would run it on the App's own
                        # message pump, where a stalled request blocks every
                        # key event in the program (even ctrl+q). Exclusive per
                        # screen so a slow reload is superseded, not stacked.
                        screen.run_worker(
                            screen.refresh_messages(),
                            group="refresh_messages",
                            exclusive=True,
                        )
            elif getattr(resp, "status_code", None) == "M_UNKNOWN_TOKEN":
                # The server revoked our token (signed out elsewhere). Retrying
                # can never succeed; drop the cached token and exit so the next
                # launch logs in fresh with the Keychain password.
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

    def open_room(self, entry: Entry) -> None:
        if entry.is_space:
            # A space is not a room you read or post in (a RoomScreen would
            # happily send a message into it); select it on the home screen
            # instead. Reached from search results and the room= setting.
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
        # Rooms open in the view mode the last "t" toggle left behind, across
        # restarts (state.json). Set here rather than in RoomScreen.__init__
        # so ThreadScreen, which inherits it, always starts unthreaded (its
        # replies would otherwise all render with the thread-reply indent).
        screen.threaded = bool(self.session.state.get("threaded_view"))
        self.push_screen(screen)
        # The dashboard row changes the moment the room opens (to the top of
        # Recent, badge cleared): apply that and rebuild the home screen now,
        # invisibly behind the room just pushed, so Esc back to it finds an
        # unchanged signature and repaints nothing. Without this the reorder
        # happened at resume, as a flicker in front of the user.
        self.session.note_opening(entry.room_id)
        for s in self.screen_stack:
            if isinstance(s, HomeScreen):
                s.stale = False  # this rebuild covers anything deferred
                s.run_worker(s.refresh_data())
                break

    def action_search(self) -> None:
        self.push_screen(SearchScreen())

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
                # Same worker discipline as sync_loop: on the App pump this
                # await froze the whole program while the network was down.
                screen.run_worker(
                    screen.refresh_messages(),
                    group="refresh_messages",
                    exclusive=True,
                )
        self.sync_loop()

    def action_refresh_home(self) -> None:
        for screen in self.screen_stack:
            if isinstance(screen, HomeScreen):
                # Rebuilding the dashboard costs real time, and at peak the
                # unread counts change on nearly every sync; while a room
                # covers the dashboard, just note the staleness and rebuild
                # once on resume instead of on every tick.
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
    import logging
    import os

    from .client import MatrixSession

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
        # A bare input() here would freeze the asyncio loop, stalling the
        # sync that must keep flushing and receiving to-device verification
        # events while you decide. to_thread(input) is no good either: the
        # worker thread stuck in input() cannot be cancelled and is joined
        # at interpreter shutdown, so Ctrl+C at this prompt wedges the
        # process until the user also presses Enter. Reading via the event
        # loop's own readiness watcher keeps the prompt fully cancellable.
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
    import getpass

    from .client import MatrixSession

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
        # OSError covers more than the missing-config case: a custom
        # [storage] path can be unwritable or blocked by a same-named file,
        # and those deserve the same clean message, not a traceback.
        raise SystemExit(str(exc))

    if not cfg.homeserver or "@you:" in cfg.user_id or not cfg.user_id:
        raise SystemExit(
            f"Edit {cfg.config_path} with your real homeserver and user id first."
        )

    # Before anything touches the keyring or the store: a second live
    # instance corrupts the shared crypto store and caches (last writer
    # wins), so refuse to start while one is running.
    cfg.acquire_instance_lock()

    # Probe the system keyring now: without a usable backend (common on a bare
    # Linux box) every later credential access raises deep inside the running
    # app; here it can still be a plain, actionable message.
    import keyring.errors

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
