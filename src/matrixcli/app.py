"""Textual front-end: a loading splash, a three-column home dashboard, a search
overlay, and a per-room read/send view.

The home screen shows three columns:

    Spaces             your spaces, with the selected space's rooms below
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
from pathlib import Path
from uuid import uuid4

import textual.events
import textual.message
from textual import work
from textual.binding import Binding
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

from .client import Entry, MatrixSession
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


def _version() -> str:
    try:
        from importlib.metadata import version

        return version("matrixcli")
    except Exception:
        return ""


def _fmt_time(ts: int) -> str:
    if not ts:
        return "     "
    return time.strftime("%H:%M", time.localtime(ts / 1000))


def _fmt_size(size: int) -> str:
    if size <= 0:
        return ""
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return ""


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
        ("j", "down", "Down"),
        ("k", "up", "Up"),
        Binding("down", "down", "Down", show=False),
        Binding("up", "up", "Up", show=False),
        ("l", "expand", "Open thread"),
        ("h", "collapse", "Close thread"),
        ("u", "first_unread", "First unread"),
        Binding("enter", "open", "Open link / download", show=False),
        ("r", "reply", "Reply"),
        ("R", "compose", "New message"),
        # Two bindings share "t"; check_action enables exactly one, so the
        # footer always names the view you are currently in.
        Binding("t", "threads_on", "View: normal"),
        Binding("t", "threads_off", "View: threaded"),
        ("T", "thread", "Open thread"),
        ("c", "toggle_compact", "Compact"),
    ]

    def __init__(self, entry: Entry) -> None:
        super().__init__()
        self.entry = entry
        self.messages: list = []
        self.reply_to = None  # a Message we are replying to, or None
        self.compact = False  # default: blank line above each name header
        self.selected = 0  # index of the currently highlighted message
        self.thread_counts: dict[str, int] = {}  # root event id -> reply count
        self.threaded = False  # True: replies shown indented under their root
        self.expanded: set[str] = set()  # roots unfolded inline ("l") in normal view
        self._thread_replies: dict[str, list] = {}  # full reply lists, fetched on unfold
        self.older: list = []  # back-paginated messages, kept across refreshes
        self._paginating = False  # guard against overlapping fetches
        self._at_beginning = False  # history start reached and announced
        # The room's fully-read marker as it was when this screen opened;
        # everything after it renders below a "new" divider, and "u" jumps
        # there. Frozen at open so live arrivals stay marked until you leave.
        self._opened_read_marker: str | None = None
        # Event id of the first unread message, chosen once from the opening
        # snapshot (see on_mount); the divider is anchored to it thereafter.
        self._first_unread_event: str | None = None
        self._composing = False  # True while an "R" new-message editor is open
        self._last_seen_event: str | None = None  # latest event id we rendered
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
        yield StatusFooter()

    async def _load_messages(self) -> list:
        """The message list this screen renders. Normal view: the room's main
        timeline with thread replies collapsed out (they live in their own
        ThreadScreen) and per-root reply counts collected for the ⤷ badges.
        Threaded view: replies render indented directly under their root, and
        replies whose root fell out of the history window stay inline at their
        own position so nothing is hidden. ThreadScreen overrides this to load
        a single thread instead."""
        messages = await self.app.session.load_history(self.entry.room_id)
        if self.older:
            # Scrolled-back history lives on the screen (the session cache
            # only keeps a recent window); splice it back in on every reload.
            merged = {m.event_id: m for m in self.older}
            for m in messages:
                merged[m.event_id] = m
            messages = sorted(merged.values(), key=lambda m: m.ts)
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
        out of the timeline whenever a sync lands during the round-trip."""
        for p in self._pending:
            if all(m.event_id != p.event_id for m in messages):
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
        self.messages = await self._load_messages()
        self.selected = max(0, len(self.messages) - 1)
        # Anchor the "new" divider to an event id now: the count fallback in
        # _first_unread_index is only meaningful against the opening snapshot,
        # and recomputing it as live messages grow the list would drift the
        # divider onto messages that arrived while you were reading.
        idx = self._first_unread_index()
        self._first_unread_event = (
            self.messages[idx].event_id if idx is not None else None
        )
        if not self.messages:
            # Auto-open the composer in an empty room as real state, not an
            # unconditional mount, so Escape can cancel it and leave the room.
            self._composing = True
        self._last_seen_event = self.app.session.last_event_id.get(
            self.entry.room_id
        )
        await self._redraw()
        await self.app.session.mark_read(self.entry.room_id)

    async def refresh_messages(self) -> None:
        """Called by the app after each background sync. Reload and redraw if
        the room received events since we last drew, follow the tail if the
        selection was on it, and mark the new messages read (we are looking at
        the room, after all)."""
        # The sync loop schedules this with call_later; by the time it runs
        # (or resumes from the history await below) the user may have popped
        # the screen, whose widgets are then gone.
        if not self.is_attached:
            return
        latest = self.app.session.last_event_id.get(self.entry.room_id)
        if not latest or latest == self._last_seen_event:
            return
        self._last_seen_event = latest
        prev_counts = self.thread_counts
        messages = await self._load_messages()
        if not self.is_attached:
            return
        # A new thread reply changes only a badge count, not the main list.
        if (
            [m.event_id for m in messages] != [m.event_id for m in self.messages]
            or self.thread_counts != prev_counts
        ):
            follow = not self.messages or self.selected >= len(self.messages) - 1
            # The reload built fresh Message objects; re-point the reply target
            # at its new incarnation so the inline editor stays attached.
            if self.reply_to is not None and self.reply_to.event_id:
                self.reply_to = next(
                    (m for m in messages if m.event_id == self.reply_to.event_id),
                    self.reply_to,
                )
            self.messages = messages
            if follow:
                self.selected = max(0, len(messages) - 1)
            else:
                self.selected = min(self.selected, max(0, len(messages) - 1))
            await self._redraw()
        await self.app.session.mark_read(self.entry.room_id)

    def _is_reply_target(self, m) -> bool:
        r = self.reply_to
        if r is None:
            return False
        return r is m or bool(r.event_id) and r.event_id == m.event_id

    async def _redraw(self) -> None:
        """Rebuild the timeline as one MessageLine widget per message, plus any
        inline reply/compose editor inserted at the right spot. Serialized by a
        lock: two interleaved rebuilds would duplicate widgets, or crash on a
        second #editor id."""
        async with self._redraw_lock:
            if not self.is_attached:
                return
            try:
                tl = self.query_one("#timeline", VerticalScroll)
            except Exception:
                return
            # Preserve a half-typed draft: a live refresh replaces the editor
            # widget, and the new one would otherwise come up empty.
            draft = ""
            draft_selection = None
            try:
                editor = self.query_one("#editor", ComposerArea)
                draft = editor.text
                draft_selection = editor.selection
            except Exception:
                pass
            await tl.remove_children()
            if not self.messages:
                await tl.mount(Static(Text("(no messages yet)", style="dim")))
                if self._composing:
                    await self._mount_composer_at_end(tl)
                    self._restore_draft(draft, draft_selection)
                return

            first_unread = self._first_unread_pos()
            prev = None
            for i, m in enumerate(self.messages):
                if i == first_unread:
                    await tl.mount(
                        Static(Text("── new ──", style="red"), classes="unread-marker")
                    )
                line = MessageLine(self._render_message(m, prev, i), i)
                if m.thread_root and (
                    self.threaded or m.thread_root in self.expanded
                ):
                    line.add_class("thread-reply")
                await tl.mount(line)
                prev = m.sender
                # A lower-case 'r' reply editor sits directly below its target.
                if self._is_reply_target(m):
                    await self._mount_reply_editor(tl)

            # An upper-case 'R' new message editor sits at the very end.
            if self.reply_to is None and self._composing:
                await self._mount_composer_at_end(tl)

            self._restore_draft(draft, draft_selection)
            editing = self.reply_to is not None or self._composing
            # When an editor is open, keep it in view rather than snapping back
            # to the selected message; do it after layout settles so positions
            # exist.
            self._highlight(scroll=not editing)
            if editing:
                self.call_after_refresh(self._scroll_to_editor)

    def _restore_draft(self, draft: str, selection=None) -> None:
        if not draft:
            return
        try:
            editor = self.query_one("#editor", ComposerArea)
        except Exception:
            return
        editor.text = draft
        # The text is unchanged, so the captured selection is still valid;
        # restoring it keeps the cursor where the user was typing instead of
        # snapping it to the end on every live refresh.
        if selection is not None:
            try:
                editor.selection = selection
                return
            except Exception:
                pass
        editor.move_cursor(editor.document.end)

    def _scroll_to_editor(self) -> None:
        try:
            editor = self.query_one("#editor", ComposerArea)
        except Exception:
            return
        editor.scroll_visible(animate=False)
        editor.focus()

    def _editor(self, placeholder: str) -> "ComposerArea":
        editor = ComposerArea(
            id="editor",
            soft_wrap=True,
            placeholder=placeholder,
            compact=True,
            show_line_numbers=False,
        )
        return editor

    async def _mount_reply_editor(self, tl) -> None:
        await tl.mount(Rule(line_style="heavy"))
        editor = self._editor("Reply...")
        await tl.mount(editor)
        await tl.mount(Rule(line_style="heavy"))
        editor.focus()

    async def _mount_composer_at_end(self, tl) -> None:
        await tl.mount(Rule(line_style="heavy"))
        editor = self._editor("New message...")
        await tl.mount(editor)
        editor.focus()

    def _highlight(self, scroll: bool = True) -> None:
        for line in self.query(MessageLine):
            line.set_class(line.msg_index == self.selected, "selected")
        if not scroll:
            return
        try:
            target = next(
                l for l in self.query(MessageLine) if l.msg_index == self.selected
            )
        except StopIteration:
            return
        target.scroll_visible(animate=False)

    def action_toggle_compact(self) -> None:
        self.compact = not self.compact
        self.run_worker(self._redraw())

    def action_down(self) -> None:
        if self.messages:
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

    def _fetch_older(self) -> None:
        """Back-paginate when the selection pushes past the top: fetch an
        older batch, splice it in, and land the selection on the message just
        above the previous top. ThreadScreen overrides this to a no-op (a
        thread is already loaded whole via /relations)."""
        if self._paginating or self._at_beginning:
            return
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

    def _first_unread_index(self) -> int | None:
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
        # No marker recorded, or it predates the loaded window: fall back to
        # the unread count the room reported when opened; without either
        # signal, treat the room as read rather than flagging everything.
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
        return True

    def _toggle_threads(self) -> None:
        async def toggle() -> None:
            current = (
                self.messages[self.selected].event_id if self.messages else None
            )
            self.threaded = not self.threaded
            self.refresh_bindings()  # footer flips between the view labels
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
        name = self.app.session.my_name if mine else m.sender_name
        name_color = MY_COLOR if mine else _sender_color(m.sender)

        grid = Table.grid(expand=True, padding=(0, 1, 0, 0))
        grid.add_column(width=5, justify="left", style="dim", vertical="top")
        grid.add_column(ratio=1, justify="left")

        if m.sender != prev_sender:
            if not self.compact and prev_sender is not None:
                grid.add_row("", "")  # blank spacer line above the name header
            grid.add_row("", Text(name, style=f"bold {name_color}"))
        if m.media_url:
            label = Text()
            label.append(f"📎 {m.media_name or body}", style="bold deep_sky_blue1")
            size = _fmt_size(m.media_size)
            if size:
                label.append(f"  ({size})", style="dim")
            label.append("  Enter to download", style="dim italic")
            grid.add_row(_fmt_time(m.ts), label)
        else:
            # An unconfirmed local echo renders dimmer than a delivered own
            # message; _finish_send flips it to the normal color on confirm.
            if m.pending:
                style = "grey50"
            else:
                style = "grey70" if mine else ""
            text = Text(body, style=style)
            for start, end, url in _find_urls(body):
                # "link <url>" makes Rich emit an OSC 8 hyperlink, so the URL is
                # also mouse-clickable in terminals that support it; the
                # underline marks it in the ones that do not.
                text.stylize(f"underline deep_sky_blue1 link {url}", start, end)
            grid.add_row(_fmt_time(m.ts), text)
        count = self.thread_counts.get(m.event_id, 0)
        if count and not self.threaded and m.event_id not in self.expanded:
            label = "reply" if count == 1 else "replies"
            grid.add_row("", Text(f"⤷ {count} {label}", style="dim italic"))
        return grid

    def action_compose(self) -> None:
        self.reply_to = None
        self._composing = True
        self.run_worker(self._redraw())

    def action_reply(self) -> None:
        if not self.messages:
            return
        m = self.messages[self.selected]
        if m.pending:
            return  # no server event id yet to hang the reply relation on
        self.reply_to = m
        self._composing = False
        self.run_worker(self._redraw())

    def action_open(self) -> None:
        """Enter on the selected message: an uploaded file asks where to save
        it and downloads, a message with links opens one in the browser (with
        a picker when it holds several)."""
        if not self.messages:
            return
        m = self.messages[self.selected]
        if m.media_url:

            def when_chosen(directory: str | None) -> None:
                if directory:
                    self.run_worker(self._download(m, directory))

            self.app.push_screen(DownloadScreen(m), when_chosen)
            return
        urls = [url for _, _, url in _find_urls(m.body or "")]
        if not urls:
            return
        if len(urls) == 1:
            self._open_url(urls[0])
            return

        def when_picked(url: str | None) -> None:
            if url:
                self._open_url(url)

        self.app.push_screen(LinkScreen(urls), when_picked)

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
        if not self.is_attached:
            return
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
            pending=True,
        )
        self._pending.append(echo)
        self.messages.append(echo)
        self.reply_to = None
        self._composing = False
        self.selected = len(self.messages) - 1
        await self._redraw()
        self.run_worker(self._finish_send(echo, text, reply, kwargs))

    async def _finish_send(self, echo, text: str, reply, kwargs: dict) -> None:
        ok, info = await self.app.session.send(self.entry.room_id, text, **kwargs)
        if echo in self._pending:
            self._pending.remove(echo)
        if not self.is_attached:
            return
        if ok:
            self._last_seen_event = info
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
            self.app.notify(
                f"Failed to send: {info}",
                severity="error",
                timeout=10,
                markup=False,
            )
        await self._redraw()
        if not ok:
            # The redraw mounted a fresh, empty editor; refill it with the
            # failed text (there was no live editor for _restore_draft to
            # capture a draft from). If the user already started a new draft
            # during the round-trip, leave that one alone.
            try:
                editor = self.query_one("#editor", ComposerArea)
                if not editor.text:
                    editor.text = text
                    editor.move_cursor(editor.document.end)
            except Exception:
                pass


class ThreadScreen(RoomScreen):
    """A single thread: the root message plus its replies, with a composer
    that sends into the thread. Pushed on top of the RoomScreen with "t";
    Escape goes back to the room."""

    def __init__(self, entry: Entry, root) -> None:
        super().__init__(entry)
        self.root = root

    async def on_mount(self) -> None:
        # Open with the composer ready: a thread is usually opened to reply.
        self._composing = True
        await super().on_mount()
        self.title = f"Thread in {self.entry.title}"
        # splitlines() of a whitespace-only body is [], so guard the [0]: a
        # remote sender controls the body and " " must not crash the screen.
        lines = (self.root.body or "").strip().splitlines() or [""]
        self.sub_title = lines[0][:60]

    async def _load_messages(self) -> list:
        thread = await self.app.session.load_thread(self.entry.room_id, self.root)
        return self._splice_pending(thread)

    def _first_unread_index(self) -> int | None:
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


class LinkScreen(PickerScreen):
    """Pick which link to open when the selected message holds more than one.
    j/k or up/down to choose, Enter to open, Escape to cancel; dismisses with
    the chosen URL or None."""

    def __init__(self, urls: list[str]) -> None:
        super().__init__()
        self.urls = urls

    def compose(self) -> ComposeResult:
        with Vertical(id="linkbox"):
            yield Label("Open which link?", id="linktitle")
            yield ListView(id="links")

    async def on_mount(self) -> None:
        lv = self.query_one("#links", ListView)
        for url in self.urls:
            # escape(): the URL comes from the sender, and unescaped a "[" in
            # it is parsed as console markup (crash on "[/", spoofed styling).
            # Truncated so a long one cannot blow out the dialog.
            item = ListItem(Label(escape(url[:200])))
            item.url = url
            await lv.append(item)
        lv.index = 0
        lv.focus()

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        self.dismiss(getattr(event.item, "url", None))


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
        ("tab", "focus_next_column", "Next column"),
        ("shift+tab", "focus_prev_column", "Prev column"),
        ("f", "toggle_favourite", "Favourite"),
        ("q", "app.quit", "Quit"),
    ]

    COLUMN_IDS = ["spaces", "favourites", "dms"]

    def __init__(self) -> None:
        super().__init__()
        self._last_signature = None
        # refresh_data is reached from both the app pump (background sync) and
        # this screen's own handlers; the lock keeps rebuilds from interleaving.
        self._refresh_lock = asyncio.Lock()

    def compose(self) -> ComposeResult:
        yield Header()
        with Horizontal(id="columns"):
            with Vertical(classes="column"):
                yield Label("Spaces", classes="section")
                yield ListView(id="spaces")
                yield Label("Rooms", classes="section", id="roomslabel")
                yield ListView(id="space_rooms")
            with Vertical(classes="column"):
                yield Label("Invites", classes="section", id="inviteslabel")
                yield ListView(id="invites")
                yield Label("Recent", classes="section")
                yield ListView(id="recent")
                yield Label("Favourites", classes="section")
                yield ListView(id="favourites")
            with Vertical(classes="column"):
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
                for e in entries:
                    is_sel = e.room_id == selected_space
                    await lv.append(EntryItem(e, self._label_for(e, is_sel)))
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

            await fill("spaces", data["spaces"], selected_space=self.selected_space)
            await fill("space_rooms", data["space_rooms"])
            await fill("invites", data["invites"])
            await fill("recent", data["recent"])
            await fill("favourites", data["favourites"])
            await fill("dms", data["dms"])

    def _signature(self, data) -> tuple:
        """A cheap hashable summary of what the dashboard would render, so we
        can detect 'nothing changed' and skip the redraw."""
        def col(entries):
            return tuple(
                (e.room_id, e.title, e.unread, e.online, e.is_favourite)
                for e in entries
            )
        return (
            self.selected_space,
            col(data["spaces"]),
            col(data["space_rooms"]),
            col(data["invites"]),
            col(data["recent"]),
            col(data["favourites"]),
            col(data["dms"]),
        )

    def _focused_list(self) -> "ListView | None":
        for cid in self.COLUMN_IDS + ["space_rooms", "invites", "recent"]:
            lv = self.query_one(f"#{cid}", ListView)
            if lv.has_focus:
                return lv
        return None

    def action_focus_next_column(self) -> None:
        self._cycle_column(1)

    def action_focus_prev_column(self) -> None:
        self._cycle_column(-1)

    def _cycle_column(self, delta: int) -> None:
        order = [
            c
            for c in ["spaces", "space_rooms", "invites", "recent", "favourites", "dms"]
            if self.query_one(f"#{c}", ListView).display
        ]
        current = next((c for c in order if self.query_one(f"#{c}", ListView).has_focus), order[0])
        i = (order.index(current) + delta) % len(order)
        self.query_one(f"#{order[i]}", ListView).focus()

    async def on_list_view_selected(self, event: ListView.Selected) -> None:
        if not isinstance(event.item, EntryItem):
            return
        entry = event.item.entry
        if entry.is_invite:
            ok, msg = await self.app.session.accept_invite(entry.room_id)
            self.app.notify(
                f"{entry.title}: {msg}",
                severity="information" if ok else "error",
                timeout=6,
                markup=False,
            )
            if ok:
                # The joined room arrives with the next sync; redraw now so
                # the invite disappears immediately.
                await self.refresh_data()
            return
        if entry.is_space:
            # Selecting a space loads its rooms into the column below. Refetch
            # its children first so newly added rooms show up without a
            # restart.
            self.app.session.state["selected_space"] = entry.room_id
            self.app.session.cfg.save_state(self.app.session.state)
            await self.app.session.refresh_space_children(entry.room_id)
            await self.refresh_data()
            self.query_one("#space_rooms", ListView).focus()
        else:
            self.app.open_room(entry)

    async def action_toggle_favourite(self) -> None:
        lv = self._focused_list()
        if lv is None or lv.index is None:
            return
        try:
            item = list(lv.children)[lv.index]
        except IndexError:
            return
        if not isinstance(item, EntryItem):
            return
        entry = item.entry
        if entry.is_space:
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
    #downloadbox, #linkbox {
        width: 70%;
        height: auto;
        margin: 4 10;
        padding: 1;
        border: round $accent;
        background: $panel;
    }
    #downloadtitle, #linktitle { text-style: bold; padding: 0 0 1 0; }
    #linkbox #links { max-height: 12; }
    AboutScreen { align: center middle; }
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
    #editor {
        border: none;
        padding: 0;
        margin: 0 1;
        height: auto;
        background: $boost;
    }
    """

    # No command palette (ctrl+p) and no ctrl+q: "q" quits, and the palette's
    # commands are all covered by explicit bindings.
    ENABLE_COMMAND_PALETTE = False
    BINDINGS = [
        ("q", "quit", "Quit"),
        Binding("ctrl+q", "noop", show=False),
        Binding("question_mark", "about", "About", show=False),
        # Hidden to keep the footer lean. While a composer is focused its own
        # ctrl+r (Send) wins, as widget bindings shadow app bindings.
        Binding("ctrl+r", "force_refresh", "Refresh", show=False),
    ]

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
        ok, message = await self.session.connect(progress=loading.set_status)
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
                resp = await self.session.client.sync(timeout=timeout, full_state=False)
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
                        self.call_later(screen.refresh_messages)
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
        self.push_screen(RoomScreen(entry))

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
                self.call_later(screen.refresh_messages)
        self.sync_loop()

    def action_refresh_home(self) -> None:
        for screen in self.screen_stack:
            if isinstance(screen, HomeScreen):
                self.call_later(screen.refresh_data)
                break

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
        try:
            # Run the blocking read in a thread: a bare input() here would freeze
            # the asyncio loop, stalling the sync that must keep flushing and
            # receiving to-device verification events while you decide.
            raw = await asyncio.to_thread(input, "Do they match? [y/N] ")
        except EOFError:
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
    except (FileNotFoundError, ValueError) as exc:
        raise SystemExit(str(exc))

    if not cfg.homeserver or "@you:" in cfg.user_id or not cfg.user_id:
        raise SystemExit(
            f"Edit {cfg.config_path} with your real homeserver and user id first."
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
