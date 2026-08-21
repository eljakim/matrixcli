import asyncio
import html
import re
import time
from dataclasses import replace
from types import SimpleNamespace

from rich.text import Text
from textual.app import App
from textual.widgets import Label, ListView, Static

from matrixcli.app import (
    DEFAULT_QUICK_REACTIONS,
    SENDER_COLORS,
    ActionScreen,
    ComposerArea,
    DownloadScreen,
    HistoryScreen,
    HomeScreen,
    MatrixApp,
    MessageLine,
    PreviewScreen,
    ReactionsScreen,
    RoomScreen,
    SyncAllScreen,
    ThreadScreen,
    _ascii_art,
    _block_art,
    _emoji_names,
    SettingsScreen,
    _find_urls,
    _fmt_time,
    _is_image,
    _refresh_terminal_title,
    _sender_color,
    _set_display_timezone,
    _set_terminal_title,
)
from matrixcli.client import Entry, Message


async def _async(value):
    """A ready-made coroutine, so a SimpleNamespace can stand in for an async
    session method."""
    return value


def make_entry(**kw):
    defaults = dict(
        room_id="!a:hs",
        title="general",
        unread=0,
        is_direct=False,
        person=None,
        last_ts=0,
    )
    defaults.update(kw)
    return Entry(**defaults)


class TestHelpers:
    def test_sender_color_deterministic_and_in_palette(self):
        assert _sender_color("@a:hs") == _sender_color("@a:hs")
        assert _sender_color("@a:hs") in SENDER_COLORS

    def test_fmt_time_zero_is_blank_column(self):
        assert _fmt_time(0) == "     "
        assert len(_fmt_time(1700000000000)) == 5


class TestLabelFor:
    # _label_for never touches self, so a dummy stands in for the screen.
    def label(self, entry, is_selected_space=False):
        return HomeScreen._label_for(SimpleNamespace(), entry, is_selected_space)

    def test_hostile_title_renders_as_literal_text(self):
        for title in ("[test", "[b yellow](99)[/b yellow]", "x[/]y"):
            label = self.label(make_entry(title=title, unread=2))
            rendered = Text.from_markup(label)  # must not raise
            assert title in rendered.plain

    def test_markers_and_badges(self):
        assert self.label(make_entry(is_favourite=True)).startswith("★ ")
        assert self.label(make_entry(is_space=True), True).startswith("◦ ")
        plain = self.label(make_entry())
        assert plain.startswith("  ")
        assert self.label(make_entry(unread=3)).endswith("(3)[/b yellow]")
        assert "[green]●[/green]" in self.label(make_entry(online=True))


class TestReact:
    def screen(self, **kw):
        s = RoomScreen(make_entry())
        s.messages = [
            Message(sender="@a:hs", sender_name="A", body="hi", ts=1,
                    event_id="$1", **kw)
        ]
        s.selected = 0
        return s

    def test_a_delivered_message_offers_react(self):
        assert self.screen().check_action("react", ()) is True

    def test_pending_deleted_and_empty_do_not(self):
        assert self.screen(pending=True).check_action("react", ()) is False
        assert self.screen(redacted_ts=5).check_action("react", ()) is False
        empty = RoomScreen(make_entry())
        assert empty.check_action("react", ()) is False

    def test_quick_defaults_fill_the_nine_digits(self):
        assert len(DEFAULT_QUICK_REACTIONS) == 9
        assert len(set(DEFAULT_QUICK_REACTIONS)) == 9

    def test_search_finds_emoji_by_unicode_name(self):
        assert "🦒" in [e for e, n in _emoji_names() if "giraff" in n]
        assert "👍" in [e for e, n in _emoji_names() if "thumbs up" in n]


class TestFindUrls:
    def urls(self, text):
        return [u for _, _, u in _find_urls(text)]

    def test_bare_url(self):
        assert self.urls("see https://a.example/x?y=1 please") == [
            "https://a.example/x?y=1"
        ]

    def test_markdown_link_stops_at_the_closing_paren(self):
        text = "Yes, according to the [hotel's website](https://ferganahotel.com/en-gb/services)"
        assert self.urls(text) == ["https://ferganahotel.com/en-gb/services"]

    def test_trailing_sentence_punctuation_is_not_part_of_the_url(self):
        assert self.urls("go to http://a.example/b.") == ["http://a.example/b"]
        assert self.urls("http://a.example/b, and more") == ["http://a.example/b"]

    def test_several_urls_in_reading_order(self):
        assert self.urls("https://a.example and https://b.example/2") == [
            "https://a.example",
            "https://b.example/2",
        ]

    def test_only_http_schemes(self):
        assert self.urls("file:///etc/passwd javascript:alert(1) mailto:a@b.c") == []

    def test_spans_cover_the_url_in_the_source_text(self):
        text = "look: https://a.example/x!"
        (start, end, url), = _find_urls(text)
        assert text[start:end] == url

    def test_no_url(self):
        assert self.urls("plain text, no links here") == []
        assert self.urls("") == []


class TestActionOpen:
    """Enter routes to the right thing: the history behind a "*", download for
    a file, browser for a link, a picker when a message offers several."""

    class Screen(RoomScreen):
        # A plain attribute shadows RoomScreen.app, which needs a running App.
        app = SimpleNamespace(push_screen=lambda *a, **kw: None)

        def __init__(self, *a, **kw):
            super().__init__(*a, **kw)
            self.opened = []
            self.pushed = []
            self.reactions = []  # (key, count) pairs _actions_for consults
            self.app = SimpleNamespace(
                push_screen=lambda screen, cb=None: self.pushed.append(screen),
                session=SimpleNamespace(
                    reaction_summary=lambda room_id, event_id: list(self.reactions)
                ),
            )

        def _open_url(self, url):
            self.opened.append(url)

    def make_screen(self, body="", reactions=(), **kw):
        screen = self.Screen(make_entry())
        screen.messages = [
            Message(sender="@a:hs", sender_name="A", body=body, ts=1, event_id="$1", **kw)
        ]
        screen.selected = 0
        screen.reactions = list(reactions)
        return screen

    def test_single_link_opens_directly(self):
        screen = self.make_screen("look at https://a.example/x")
        screen._open_selected()
        assert screen.opened == ["https://a.example/x"]
        assert screen.pushed == []

    def test_several_links_show_the_picker(self):
        screen = self.make_screen("https://a.example and https://b.example")
        screen._open_selected()
        assert screen.opened == []
        assert isinstance(screen.pushed[0], ActionScreen)
        assert [a for _, a in screen.pushed[0].actions] == [
            ("link", "https://a.example"),
            ("link", "https://b.example"),
        ]

    def test_file_offers_the_download_dialog(self):
        screen = self.make_screen("photo.png", media_url="mxc://x/y")
        screen._open_selected()
        assert screen.opened == []
        assert isinstance(screen.pushed[0], DownloadScreen)

    def test_reactions_open_the_who_reacted_popup(self):
        screen = self.make_screen("popular take", reactions=[("👍", 2)])
        screen._open_selected()
        assert screen.opened == []
        assert isinstance(screen.pushed[0], ReactionsScreen)

    def test_link_and_reactions_show_the_picker(self):
        screen = self.make_screen(
            "see https://a.example", reactions=[("👍", 2)]
        )
        screen._open_selected()
        assert isinstance(screen.pushed[0], ActionScreen)
        assert [a for _, a in screen.pushed[0].actions] == [
            ("link", "https://a.example"),
            ("reactions", ""),
        ]

    def test_deleted_message_hides_its_reactions(self):
        screen = self.make_screen(
            "kept", reactions=[("👍", 2)], redacted_ts=400
        )
        assert screen._selected_actions() == []

    def test_shift_enter_opens_the_history(self):
        screen = self.make_screen("fixed", edited_ts=200, original_body="typo")
        screen.action_open_details()
        assert isinstance(screen.pushed[0], HistoryScreen)

    def test_enter_and_shift_enter_do_not_compete(self):
        # Enter follows the link, Shift+Enter shows the versions.
        screen = self.make_screen("fixed https://a.example", edited_ts=200)
        screen._open_selected()
        assert screen.opened == ["https://a.example"]
        screen.action_open_details()
        assert isinstance(screen.pushed[0], HistoryScreen)

    def test_deleted_message_offers_only_its_kept_text(self):
        # No links out of withdrawn text; the kept copy is all there is.
        screen = self.make_screen("see https://a.example", redacted_ts=400)
        assert screen._selected_actions() == []
        screen._open_selected()
        assert screen.opened == [] and screen.pushed == []
        screen.action_open_details()
        assert isinstance(screen.pushed[0], HistoryScreen)

    def test_shift_enter_does_nothing_without_history(self):
        screen = self.make_screen("plain")
        screen.action_open_details()
        assert screen.pushed == []

    def test_message_without_a_link_does_nothing(self):
        screen = self.make_screen("no links here")
        screen._open_selected()
        assert screen.opened == [] and screen.pushed == []

    def test_empty_room_does_nothing(self):
        screen = self.Screen(make_entry())
        screen._open_selected()
        assert screen.opened == [] and screen.pushed == []

    def test_the_footer_names_what_enter_will_do(self):
        cases = {
            "open_link": self.make_screen("https://a.example"),
            "open_download": self.make_screen("f.png", media_url="mxc://x/y"),
            "open_actions": self.make_screen(
                "f.png https://a.example", media_url="mxc://x/y"
            ),
        }
        for enabled, screen in cases.items():
            for action in cases:
                assert screen.check_action(action, None) is (action == enabled), (
                    f"{action} on the {enabled} message"
                )
        plain = self.make_screen("nothing to do here")
        assert not any(plain.check_action(a, None) for a in cases)

    def test_enter_leaves_a_reactions_only_message_to_the_spacebar(self):
        # The popup lives on the spacebar; Enter must not promise it too.
        screen = self.make_screen("hot take", reactions=[("👍", 2)])
        for action in ("open_link", "open_download", "open_actions"):
            assert screen.check_action(action, None) is False
        assert screen.check_action("show_reactions", None) is True

    def test_the_footer_offers_shift_enter_only_where_there_is_history(self):
        for message, expected in (
            (self.make_screen("x", edited_ts=200), True),
            (self.make_screen("kept", redacted_ts=400), True),
            (self.make_screen("", redacted_ts=400), False),
            (self.make_screen("plain https://a.example"), False),
        ):
            assert message.check_action("open_details", None) is expected


class TestSpacebar:
    """Space peeks at the selected message: the in-terminal preview on an
    image, the who-reacted popup on any other message wearing badges, and a
    second space closes what the first one opened."""

    def make_screen(self, body="", reactions=(), **kw):
        screen = TestActionOpen.Screen(make_entry())
        screen.messages = [
            Message(sender="@a:hs", sender_name="A", body=body, ts=1, event_id="$1", **kw)
        ]
        screen.selected = 0
        screen.reactions = list(reactions)
        return screen

    def test_footer_offers_exactly_one_space_action(self):
        image = self.make_screen(
            "pic.png", media_url="mxc://x/y", media_mime="image/png"
        )
        assert image.check_action("preview_image", None) is True
        assert image.check_action("show_reactions", None) is False
        reacted = self.make_screen("hot take", reactions=[("👍", 2)])
        assert reacted.check_action("preview_image", None) is False
        assert reacted.check_action("show_reactions", None) is True
        plain = self.make_screen("plain")
        assert plain.check_action("preview_image", None) is False
        assert plain.check_action("show_reactions", None) is False

    def test_space_opens_who_reacted(self):
        screen = self.make_screen("hot take", reactions=[("👍", 2)])
        screen.action_show_reactions()
        assert isinstance(screen.pushed[0], ReactionsScreen)

    def test_space_does_nothing_without_reactions(self):
        screen = self.make_screen("plain")
        screen.action_show_reactions()
        assert screen.pushed == []

    def test_reacted_image_keeps_space_for_the_preview(self):
        screen = self.make_screen(
            "pic.png",
            media_url="mxc://x/y",
            media_mime="image/png",
            reactions=[("👍", 2)],
        )
        assert screen.check_action("preview_image", None) is True
        assert screen.check_action("show_reactions", None) is False
        # Its reactions stay reachable through Enter's actions menu.
        assert screen.check_action("open_actions", None) is True

    def test_deleted_message_hides_its_reactions_from_space_too(self):
        screen = self.make_screen("kept", reactions=[("👍", 2)], redacted_ts=400)
        assert screen.check_action("show_reactions", None) is False
        screen.action_show_reactions()
        assert screen.pushed == []

    def test_space_closes_both_popups(self):
        def bound(cls):
            pairs = set()
            for b in cls.BINDINGS:
                if isinstance(b, tuple):
                    pairs.add((b[0], b[1]))
                else:
                    pairs.add((b.key, b.action))
            return pairs

        assert ("space", "cancel") in bound(PreviewScreen)
        assert ("space", "dismiss") in bound(ReactionsScreen)


class TestPickerKeys:
    # A subclass redeclaring BINDINGS would silently drop the vim keys.
    def test_pickers_take_vim_keys_and_escape(self):
        for cls in (DownloadScreen, ActionScreen):
            keys = cls._merged_bindings.key_to_bindings
            assert {"j", "k", "escape"} <= set(keys), cls.__name__


class TestComposerTitle:
    """The docked composer's header line names the message being replied to."""

    def msg(self, body="x", name="A"):
        return Message(
            sender="@a:hs", sender_name=name, body=body, ts=1, event_id="$1"
        )

    def test_new_message(self):
        assert RoomScreen(make_entry())._composer_title() == "New message"

    def test_reply_names_the_sender_and_quotes_the_first_line(self):
        screen = RoomScreen(make_entry())
        screen.reply_to = self.msg(body="first line\nsecond line", name="Chanelle")
        assert screen._composer_title() == "Reply to Chanelle: first line"

    def test_hostile_body_and_name_are_escaped(self):
        screen = RoomScreen(make_entry())
        screen.reply_to = self.msg(body="[b yellow]x", name="[/]")
        title = screen._composer_title()
        assert Text.from_markup(title).plain == "Reply to [/]: [b yellow]x"

    def test_thread_composer_says_so(self):
        screen = ThreadScreen(make_entry(), self.msg())
        assert screen._composer_title() == "Reply in thread"
        screen.reply_to = self.msg(name="Dana")
        assert screen._composer_title() == "Reply to Dana: x"


class TestComposerWordDelete:
    """Option+Backspace deletes the word before the cursor."""

    def press(self, key):
        class ComposerApp(App):
            def compose(self):
                yield ComposerArea(id="c")

        async def run():
            app = ComposerApp()
            async with app.run_test() as pilot:
                editor = app.query_one("#c", ComposerArea)
                editor.focus()
                editor.text = "hello wide world"
                editor.move_cursor(editor.document.end)
                await pilot.press(key)
                await pilot.pause()
                return editor.text

        return asyncio.run(run())

    def test_option_backspace_on_kitty_terminals(self):
        # Terminals speaking the kitty protocol send Option+Backspace as its
        # own key, which TextArea does not bind by default.
        assert self.press("alt+backspace") == "hello wide "

    def test_option_backspace_elsewhere(self):
        # Everywhere else the sequence folds onto ctrl+w.
        assert self.press("ctrl+w") == "hello wide "


class TestFirstUnread:
    def make_screen(self, n=5, marker=None, unread=0):
        screen = RoomScreen(make_entry(unread=unread))
        screen.messages = [
            Message(sender="@a:hs", sender_name="A", body="x", ts=i, event_id=f"${i}")
            for i in range(n)
        ]
        screen._opened_read_marker = marker
        return screen

    def test_marker_in_window(self):
        assert self.make_screen(marker="$2")._first_unread_index() == 3

    def test_marker_at_latest_message_means_no_unread(self):
        assert self.make_screen(marker="$4")._first_unread_index() is None

    def test_marker_missing_falls_back_to_unread_count(self):
        screen = self.make_screen(marker="$gone", unread=2)
        assert screen._first_unread_index() == 3

    def test_marker_ts_places_divider_after_the_read_horizon(self):
        # A reaction-id marker's timestamp beats the count fallback.
        screen = self.make_screen(marker="$react", unread=4)
        assert screen._first_unread_index(marker_ts=2) == 3

    def test_marker_ts_newer_than_every_row_means_read(self):
        # A reaction after the last message must not flag it unread.
        screen = self.make_screen(marker="$react", unread=2)
        assert screen._first_unread_index(marker_ts=9) is None

    def test_no_signals_means_read(self):
        assert self.make_screen()._first_unread_index() is None

    def test_empty_room(self):
        assert self.make_screen(n=0, marker="$x")._first_unread_index() is None

    def test_divider_anchored_at_open_does_not_drift(self):
        # Once anchored to an event id, later arrivals must not move it.
        screen = self.make_screen(unread=2)
        idx = screen._first_unread_index()
        screen._first_unread_event = screen.messages[idx].event_id
        assert screen._first_unread_pos() == 3
        screen.messages = screen.messages + [
            Message(sender="@a:hs", sender_name="A", body="x", ts=9, event_id="$9")
        ]
        assert screen._first_unread_pos() == 3


class TestThreadScreenUnread:
    def make_screen(self, marker=None, unread=0):
        root = Message(
            sender="@a:hs", sender_name="A", body="root", ts=0, event_id="$root"
        )
        screen = ThreadScreen(make_entry(unread=unread), root)
        screen.messages = [
            Message(sender="@a:hs", sender_name="A", body="x", ts=i, event_id=f"${i}")
            for i in range(5)
        ]
        screen._opened_read_marker = marker
        return screen

    def test_room_unread_count_is_ignored(self):
        # The room count covers main-timeline events, meaningless in a thread.
        assert self.make_screen(unread=3)._first_unread_index() is None

    def test_marker_inside_thread_places_divider(self):
        assert self.make_screen(marker="$2")._first_unread_index() == 3

    def test_marker_at_last_reply_means_no_unread(self):
        assert self.make_screen(marker="$4")._first_unread_index() is None

    def test_marker_ts_is_ignored_in_threads(self):
        # A room-level marker timestamp says nothing about thread replies.
        screen = self.make_screen(marker="$gone")
        assert screen._first_unread_index(marker_ts=1) is None
        assert screen._divider_ts_fallback is False


def msg(event_id, ts, root="", count=0):
    return Message(
        sender="@a:hs",
        sender_name="A",
        body=event_id,
        ts=ts,
        event_id=event_id,
        thread_root=root,
        thread_count=count,
    )


class TestLoadMessagesDisplay:
    def make_screen(self, monkeypatch, history, threaded=False, older=()):
        screen = RoomScreen(make_entry())

        async def load_history(room_id, cached_only=False):
            return list(history)

        fake_app = SimpleNamespace(
            session=SimpleNamespace(load_history=load_history)
        )
        monkeypatch.setattr(
            RoomScreen, "app", property(lambda self: fake_app), raising=False
        )
        screen.threaded = threaded
        screen.older = list(older)
        return screen

    def test_normal_view_collapses_replies_and_counts_them(self, monkeypatch):
        history = [
            msg("$root", 100),
            msg("$main", 150),
            msg("$a", 200, root="$root"),
            msg("$b", 300, root="$root"),
        ]
        screen = self.make_screen(monkeypatch, history)
        out = asyncio.run(screen._load_messages())
        assert [m.event_id for m in out] == ["$root", "$main"]
        assert screen.thread_counts == {"$root": 2}

    def test_server_aggregated_count_wins_over_local_window(self, monkeypatch):
        history = [msg("$root", 100, count=7), msg("$a", 200, root="$root")]
        screen = self.make_screen(monkeypatch, history)
        asyncio.run(screen._load_messages())
        assert screen.thread_counts == {"$root": 7}

    def test_threaded_view_nests_replies_under_their_root(self, monkeypatch):
        history = [
            msg("$root", 100),
            msg("$main", 150),
            msg("$a", 200, root="$root"),
        ]
        screen = self.make_screen(monkeypatch, history, threaded=True)
        out = asyncio.run(screen._load_messages())
        assert [m.event_id for m in out] == ["$root", "$a", "$main"]

    def test_threaded_view_keeps_orphan_replies_inline(self, monkeypatch):
        # The root is outside the loaded window: the reply must stay visible.
        history = [
            msg("$main", 150),
            msg("$orphan", 200, root="$gone"),
            msg("$later", 300),
        ]
        screen = self.make_screen(monkeypatch, history, threaded=True)
        out = asyncio.run(screen._load_messages())
        assert [m.event_id for m in out] == ["$main", "$orphan", "$later"]

    def test_scrolled_back_history_is_spliced_in(self, monkeypatch):
        history = [msg("$new", 300)]
        older = [msg("$old", 100), msg("$new", 300)]
        screen = self.make_screen(monkeypatch, history, older=older)
        out = asyncio.run(screen._load_messages())
        assert [m.event_id for m in out] == ["$old", "$new"]

    def test_older_threaded_replies_collapse_in_normal_view(self, monkeypatch):
        history = [msg("$root", 300)]
        older = [msg("$reply", 100, root="$root")]
        screen = self.make_screen(monkeypatch, history, older=older)
        out = asyncio.run(screen._load_messages())
        assert [m.event_id for m in out] == ["$root"]
        assert screen.thread_counts == {"$root": 1}


class TestExpandedThreads:
    def make_screen(self, monkeypatch, history, expanded=(), fetched=None):
        screen = RoomScreen(make_entry())

        async def load_history(room_id, cached_only=False):
            return list(history)

        fake_app = SimpleNamespace(
            session=SimpleNamespace(load_history=load_history)
        )
        monkeypatch.setattr(
            RoomScreen, "app", property(lambda self: fake_app), raising=False
        )
        screen.expanded = set(expanded)
        screen._thread_replies = dict(fetched or {})
        return screen

    def test_unfolded_root_shows_replies_inline(self, monkeypatch):
        history = [
            msg("$root", 100),
            msg("$main", 150),
            msg("$a", 200, root="$root"),
        ]
        screen = self.make_screen(monkeypatch, history, expanded={"$root"})
        out = asyncio.run(screen._load_messages())
        assert [m.event_id for m in out] == ["$root", "$a", "$main"]

    def test_folded_roots_stay_collapsed_alongside_unfolded_ones(self, monkeypatch):
        history = [
            msg("$r1", 100),
            msg("$r2", 150),
            msg("$a", 200, root="$r1"),
            msg("$b", 300, root="$r2"),
        ]
        screen = self.make_screen(monkeypatch, history, expanded={"$r2"})
        out = asyncio.run(screen._load_messages())
        assert [m.event_id for m in out] == ["$r1", "$r2", "$b"]

    def test_fetched_replies_merge_with_window_replies(self, monkeypatch):
        history = [msg("$root", 100), msg("$new", 300, root="$root")]
        fetched = {"$root": [msg("$old", 200, root="$root"), msg("$new", 300, root="$root")]}
        screen = self.make_screen(
            monkeypatch, history, expanded={"$root"}, fetched=fetched
        )
        out = asyncio.run(screen._load_messages())
        assert [m.event_id for m in out] == ["$root", "$old", "$new"]


class TestActionThread:
    def make_screen(self, monkeypatch, messages, timeline=()):
        screen = RoomScreen(make_entry())
        pushed = []
        notices = []
        fake_app = SimpleNamespace(
            session=SimpleNamespace(timelines={"!a:hs": list(timeline)}),
            push_screen=lambda s: pushed.append(s),
            notify=lambda *a, **k: notices.append(a),
        )
        monkeypatch.setattr(
            RoomScreen, "app", property(lambda self: fake_app), raising=False
        )
        screen.messages = list(messages)
        return screen, pushed, notices

    def test_plain_message_roots_its_own_thread(self, monkeypatch):
        screen, pushed, _ = self.make_screen(monkeypatch, [msg("$root", 100)])
        screen.selected = 0
        screen.action_thread()
        assert pushed and pushed[0].root.event_id == "$root"

    def test_reply_opens_the_thread_it_belongs_to(self, monkeypatch):
        # Threads do not nest: T on a reply opens the reply's thread.
        history = [msg("$root", 100), msg("$reply", 200, root="$root")]
        screen, pushed, _ = self.make_screen(monkeypatch, history)
        screen.selected = 1
        screen.action_thread()
        assert pushed and pushed[0].root.event_id == "$root"

    def test_reply_root_resolved_from_session_cache(self, monkeypatch):
        history = [msg("$reply", 200, root="$root")]
        screen, pushed, _ = self.make_screen(
            monkeypatch, history, timeline=[msg("$root", 100)]
        )
        screen.selected = 0
        screen.action_thread()
        assert pushed and pushed[0].root.event_id == "$root"

    def test_missing_root_notifies_instead_of_nesting(self, monkeypatch):
        history = [msg("$reply", 200, root="$gone")]
        screen, pushed, notices = self.make_screen(monkeypatch, history)
        screen.selected = 0
        screen.action_thread()
        assert not pushed
        assert notices


class TestPendingEcho:
    def msg(self, event_id, pending=False, ts=1, body="x"):
        return Message(
            sender="@a:hs",
            sender_name="A",
            body=body,
            ts=ts,
            event_id=event_id,
            pending=pending,
        )

    def test_splice_appends_in_flight_echoes(self):
        screen = RoomScreen(make_entry())
        echo = self.msg("~local.1", pending=True)
        screen._pending.append(echo)
        out = screen._splice_pending([self.msg("$1", body="something else")])
        assert out[-1] is echo

    def test_splice_skips_echo_whose_sync_copy_already_arrived(self):
        # The sync copy can outrun /send; the message must not render twice.
        screen = RoomScreen(make_entry())
        echo = self.msg("~local.1", pending=True, ts=1000)
        screen._pending.append(echo)
        out = screen._splice_pending([self.msg("$real", ts=1500)])
        assert [m.event_id for m in out] == ["$real"]

    def test_splice_claims_one_arrival_per_identical_echo(self):
        # Sending the same text twice: one arrival suppresses one echo only.
        screen = RoomScreen(make_entry())
        first = self.msg("~local.1", pending=True, ts=1000)
        second = self.msg("~local.2", pending=True, ts=2000)
        screen._pending.extend([first, second])
        out = screen._splice_pending([self.msg("$real", ts=1500)])
        assert [m.event_id for m in out] == ["$real", "~local.2"]

    def test_splice_ignores_identical_text_from_long_ago(self):
        screen = RoomScreen(make_entry())
        echo = self.msg("~local.1", pending=True, ts=10_000_000)
        screen._pending.append(echo)
        out = screen._splice_pending([self.msg("$old", ts=1000)])
        assert out[-1] is echo

    def test_splice_skips_echo_already_in_list(self):
        # A reload already holding the confirmed event must not duplicate it.
        screen = RoomScreen(make_entry())
        echo = self.msg("$real", pending=True)
        screen._pending.append(echo)
        out = screen._splice_pending([self.msg("$real")])
        assert len(out) == 1

    def test_thread_latest_skips_pending_echoes(self):
        # A "~local." id must never leave the client as the reply fallback.
        root = self.msg("$root")
        screen = ThreadScreen(make_entry(), root)
        screen.messages = [
            self.msg("$reply", ts=2),
            self.msg("~local.1", pending=True, ts=3),
        ]
        assert screen._send_kwargs(None)["thread_latest"] == "$reply"

    def test_thread_latest_all_pending_falls_back_to_root(self):
        root = self.msg("$root")
        screen = ThreadScreen(make_entry(), root)
        screen.messages = [self.msg("~local.1", pending=True)]
        assert screen._send_kwargs(None)["thread_latest"] == "$root"


class TestAppChrome:
    def test_command_palette_disabled(self):
        assert MatrixApp.ENABLE_COMMAND_PALETTE is False

    def test_ctrl_q_neutralized_quit_keys_and_about_bound(self):
        # A global q would let one keystroke in a room quit the whole app.

        def keymap(bindings):
            actions = {}
            for b in bindings:
                key = b.key if hasattr(b, "key") else b[0]
                action = b.action if hasattr(b, "action") else b[1]
                actions[key] = action
            return actions

        app_actions = keymap(MatrixApp.BINDINGS)
        assert app_actions["ctrl+q"] == "noop"
        assert "q" not in app_actions
        assert app_actions["colon"] == "command_line"
        assert app_actions["question_mark"] == "about"
        assert app_actions["ctrl+r"] == "force_refresh"
        assert keymap(HomeScreen.BINDINGS)["q"] == "app.quit"
        assert keymap(RoomScreen.BINDINGS)["q"] == "app.go_home"


class TestConnStatus:
    # render only reads self.app, so a dummy stands in for the widget.
    def render(self, last_sync_at, sync_ok):
        from matrixcli.app import ConnStatus

        fake = SimpleNamespace(
            app=SimpleNamespace(last_sync_at=last_sync_at, sync_ok=sync_ok)
        )
        return ConnStatus.render(fake)

    def test_before_first_sync_shows_nothing(self):
        assert self.render(None, True).plain == ""

    def test_fresh_sync_shows_dot_and_age(self):
        text = self.render(time.monotonic() - 5, True)
        assert text.plain.startswith("● ")
        assert text.plain.endswith("5s")
        assert "offline" not in text.plain

    def test_failed_sync_shows_offline(self):
        text = self.render(time.monotonic() - 5, False)
        assert "offline" in text.plain
        assert text.plain.endswith("5s")

    def test_silently_hung_poll_counts_as_offline(self):
        # A dropped network hangs the long-poll without failing it.

        text = self.render(time.monotonic() - 120, True)
        assert "offline" in text.plain
        assert text.plain.endswith("2m")


class TestEditedMessages:
    """An edit folded into the timeline: one line, a trailing "*", and the "H"
    key offered only while that line is selected."""

    def screen(self, monkeypatch, history):
        screen = RoomScreen(make_entry())

        async def load_history(room_id, cached_only=False):
            return list(history)

        fake_app = SimpleNamespace(
            session=SimpleNamespace(
                load_history=load_history,
                my_name="Me",
                cfg=SimpleNamespace(user_id="@me:hs"),
                reaction_summary=lambda room_id, event_id: [],
            )
        )
        monkeypatch.setattr(
            RoomScreen, "app", property(lambda self: fake_app), raising=False
        )
        screen.messages = asyncio.run(screen._load_messages())
        return screen

    def history(self):
        return [
            msg("$o", 100),
            Message(
                sender="@a:hs", sender_name="A", body="fixed", ts=200,
                event_id="$e", replaces="$o",
            ),
            msg("$plain", 300),
        ]

    def body_cell(self, screen, index):
        # Last row of the two-column grid: the timestamp and the message text.
        grid = screen._render_message(screen.messages[index], None, index)
        return list(grid.columns[1].cells)[-1].plain

    def test_edit_collapses_into_one_line_with_a_marker(self, monkeypatch):
        screen = self.screen(monkeypatch, self.history())
        assert [m.event_id for m in screen.messages] == ["$o", "$plain"]
        assert screen.messages[0].original_body == "$o"  # kept for the popup
        assert self.body_cell(screen, 0) == "fixed *"

    def test_plain_message_has_no_marker(self, monkeypatch):
        screen = self.screen(monkeypatch, self.history())
        assert self.body_cell(screen, 1) == "$plain"

    def test_history_is_offered_only_where_there_is_history(self, monkeypatch):
        screen = self.screen(monkeypatch, self.history())
        screen.selected = 0
        assert screen.check_action("open_details", None) is True
        screen.selected = 1
        assert screen.check_action("open_details", None) is False

    def test_deleted_message_shows_a_tombstone(self, monkeypatch):
        deleted = Message(
            sender="@a:hs", sender_name="A", body="what I said", ts=100,
            event_id="$d", redacted_ts=400,
        )
        screen = self.screen(monkeypatch, [deleted])
        # We got the text before the deletion; the server no longer has it.
        assert self.body_cell(screen, 0) == "this message has been deleted *"
        assert screen.check_action("open_details", None) is True

    def test_deletion_we_never_had_the_text_of_offers_nothing(self, monkeypatch):
        tombstone = Message(
            sender="@a:hs", sender_name="A", body="", ts=100,
            event_id="$d", redacted_ts=400,
        )
        screen = self.screen(monkeypatch, [tombstone])
        assert self.body_cell(screen, 0) == "this message has been deleted"
        assert screen.check_action("open_details", None) is False

    def test_an_edit_or_deletion_alone_still_triggers_a_redraw(self, monkeypatch):
        # A signature of event ids alone would skip the redraw.
        screen = self.screen(monkeypatch, self.history())
        before = screen._signature(screen.messages)
        edited = [replace(screen.messages[0], edited_ts=999), screen.messages[1]]
        deleted = [replace(screen.messages[0], redacted_ts=999), screen.messages[1]]
        assert screen._signature(edited) != before
        assert screen._signature(deleted) != before


class TestRefreshNames:
    """The member list arrives seconds after a room opens (fetched in the
    background, see MatrixSession._fetch_members); senders that painted as
    raw @user:server ids must repaint in place, without touching selection."""

    def make_screen(self, monkeypatch, cached):
        screen = RoomScreen(make_entry())

        async def load_history(room_id, cached_only=False):
            return list(cached)

        fake_app = SimpleNamespace(
            session=SimpleNamespace(load_history=load_history)
        )
        monkeypatch.setattr(
            RoomScreen, "app", property(lambda self: fake_app), raising=False
        )
        monkeypatch.setattr(
            RoomScreen, "is_attached", property(lambda self: True), raising=False
        )
        redraws = []

        async def fake_redraw(keep_scroll=False):
            redraws.append(keep_scroll)

        screen._redraw = fake_redraw
        return screen, redraws

    def test_resolved_name_repaints_in_place(self, monkeypatch):
        raw = Message(
            sender="@a:hs", sender_name="@a:hs", body="hi", ts=100, event_id="$1"
        )
        screen, redraws = self.make_screen(
            monkeypatch, [replace(raw, sender_name="Alice")]
        )
        screen.messages = [raw]
        screen.selected = 0
        asyncio.run(screen.refresh_names())
        assert [m.sender_name for m in screen.messages] == ["Alice"]
        assert redraws == [True]  # keep_scroll: only the text changed
        assert screen.selected == 0

    def test_unchanged_names_skip_the_redraw(self, monkeypatch):
        m = Message(
            sender="@a:hs", sender_name="Alice", body="hi", ts=100, event_id="$1"
        )
        screen, redraws = self.make_screen(monkeypatch, [m])
        screen.messages = [m]
        asyncio.run(screen.refresh_names())
        assert redraws == []


class TestComposerPanel:
    """The composer is docked under the timeline, not mounted inside it, so a
    long draft scrolls in its own five rows and a live refresh cannot disturb
    what is being typed."""

    def run(self, steps):
        """Open a room in a headless app and hand it to `steps`."""
        from textual.containers import Vertical

        history = [
            Message(
                sender="@a:hs", sender_name="A", body=f"m{i}", ts=100 + i,
                event_id=f"$m{i}",
            )
            for i in range(4)
        ]
        session = SimpleNamespace(
            cfg=SimpleNamespace(user_id="@me:hs"),
            my_name="Me",
            client=SimpleNamespace(rooms={}),
            last_event_id={},
            load_history=lambda room_id, limit=40, cached_only=False: _async(list(history)),
            fetch_fully_read=lambda room_id: _async(None),
            mark_read=lambda room_id: _async(None),
            start_backfill=lambda room_id: None,
            reset_pagination=lambda room_id: None,
            drafts={},
            reaction_summary=lambda room_id, event_id: [],
        )

        class RoomApp(App):
            # The real stylesheet: the editor's fixed height comes from it.
            CSS = MatrixApp.CSS

            def on_mount(self):
                self.session = session
                self.last_sync_at = None
                self.sync_ok = True
                return self.push_screen(RoomScreen(make_entry()))

        async def run() -> dict:
            app = RoomApp()
            async with app.run_test(size=(80, 24)) as pilot:
                await pilot.pause()
                return await steps(
                    pilot,
                    app.screen,
                    app.screen.query_one("#composer", Vertical),
                    ComposerArea,
                )

        return asyncio.run(run())

    def test_r_docks_a_five_row_editor_outside_the_timeline(self):
        async def steps(pilot, screen, panel, composer_area):
            assert panel.display is False
            await pilot.press("r")
            await pilot.pause()
            editor = screen.query_one("#editor", composer_area)
            timeline = screen.query_one("#timeline")
            return {
                "shown": panel.display,
                "rows": editor.size.height,
                "focused": editor.has_focus,
                "in_timeline": editor in timeline.walk_children(),
                "title": screen.query_one("#composertitle").parent is panel,
            }

        assert self.run(steps) == {
            "shown": True,
            "rows": 5,
            "focused": True,
            "in_timeline": False,
            "title": True,
        }

    def test_a_long_draft_scrolls_inside_the_panel(self):
        async def steps(pilot, screen, panel, composer_area):
            await pilot.press("n")
            await pilot.pause()
            editor = screen.query_one("#editor", composer_area)
            for _ in range(9):
                await pilot.press("x")
                await pilot.press("shift+enter")
            await pilot.press("y")
            await pilot.pause()
            # Line 10 in a five-row box: the box scrolled, not the screen.
            return {
                "rows": editor.size.height,
                "lines": editor.document.line_count,
                "scrolled": editor.scroll_offset.y > 0,
                "cursor_row": editor.cursor_location[0],
            }

        assert self.run(steps) == {
            "rows": 5,
            "lines": 10,
            "scrolled": True,
            "cursor_row": 9,
        }

    def test_a_redraw_leaves_the_draft_and_focus_alone(self):
        async def steps(pilot, screen, panel, composer_area):
            await pilot.press("n")
            await pilot.pause()
            editor = screen.query_one("#editor", composer_area)
            for ch in "hi":
                await pilot.press(ch)
            await pilot.pause()
            await screen._redraw()  # what a background sync triggers
            await pilot.pause()
            after = screen.query_one("#editor", composer_area)
            return {
                "same_widget": after is editor,
                "text": after.text,
                "focused": after.has_focus,
            }

        assert self.run(steps) == {"same_widget": True, "text": "hi", "focused": True}

    def test_escape_closes_the_panel_and_unmounts_the_editor(self):
        async def steps(pilot, screen, panel, composer_area):
            await pilot.press("n")
            await pilot.pause()
            await pilot.press("escape")
            await pilot.pause()
            return {"shown": panel.display, "editors": len(screen.query("#editor"))}

        assert self.run(steps) == {"shown": False, "editors": 0}


class TestMessageRendering:
    """Colour, mention flag, reaction row: what one message line shows."""

    def screen(self, monkeypatch, reactions=()):
        screen = RoomScreen(make_entry())
        fake_app = SimpleNamespace(
            session=SimpleNamespace(
                my_name="Me",
                cfg=SimpleNamespace(user_id="@me:hs"),
                reaction_summary=lambda room_id, event_id: list(reactions),
            )
        )
        monkeypatch.setattr(
            RoomScreen, "app", property(lambda self: fake_app), raising=False
        )
        return screen

    def body_cell(self, screen, m):
        grid = screen._render_message(m, None, 0)
        return list(grid.columns[1].cells)[-1]

    def test_delivered_own_message_uses_the_normal_colour(self, monkeypatch):
        screen = self.screen(monkeypatch)
        own = Message(sender="@me:hs", sender_name="Me", body="hi", ts=1,
                      event_id="$1")
        assert self.body_cell(screen, own).style == ""

    def test_only_pending_renders_gray(self, monkeypatch):
        screen = self.screen(monkeypatch)
        echo = Message(sender="@me:hs", sender_name="Me", body="hi", ts=1,
                       event_id="~local.1", pending=True)
        assert self.body_cell(screen, echo).style == "grey50"

    def test_mention_paints_the_time_cell_and_marks_the_body(self, monkeypatch):
        screen = self.screen(monkeypatch)
        ping = Message(sender="@a:hs", sender_name="A", body="hey you", ts=1,
                       event_id="$1", mentions_me=True)
        grid = screen._render_message(ping, None, 0)
        time_cell = list(grid.columns[0].cells)[-1]
        assert isinstance(time_cell, Text) and time_cell.style == "bold red"
        assert self.body_cell(screen, ping).plain.endswith(" @")

    def test_reply_quote_prefers_the_loaded_target(self, monkeypatch):
        screen = self.screen(monkeypatch)
        screen.messages = [
            Message(sender="@a:hs", sender_name="Alice",
                    body="original text\nmore", ts=1, event_id="$orig"),
        ]
        m = Message(sender="@b:hs", sender_name="Bob", body="the reply", ts=2,
                    event_id="$r", reply_to="$orig", reply_name="@a:hs",
                    reply_snippet="stale fallback")
        grid = screen._render_message(m, None, 0)
        cells = [c.plain for c in grid.columns[1].cells if isinstance(c, Text)]
        assert "> Alice: original text" in cells
        assert cells[-1] == "the reply"

    def test_reply_quote_falls_back_to_the_fallback_text(self, monkeypatch):
        # Target outside the loaded window: the fallback text is all we have.
        screen = self.screen(monkeypatch)
        m = Message(sender="@b:hs", sender_name="Bob", body="the reply", ts=2,
                    event_id="$r", reply_to="$gone", reply_name="Alice",
                    reply_snippet="what she said")
        grid = screen._render_message(m, None, 0)
        cells = [c.plain for c in grid.columns[1].cells if isinstance(c, Text)]
        assert "> Alice: what she said" in cells

    def test_reaction_row_renders_counts(self, monkeypatch):
        screen = self.screen(monkeypatch, reactions=[("👍", 3), ("🎉", 1)])
        m = Message(sender="@a:hs", sender_name="A", body="vote!", ts=1,
                    event_id="$1")
        grid = screen._render_message(m, None, 0)
        assert list(grid.columns[1].cells)[-1].plain == "👍 3  🎉 1"


class TestThreadKeyGating:
    """l/h appear in the footer only when they would do something."""

    def screen(self, monkeypatch, messages, counts=None, expanded=(),
               threaded=False):
        screen = RoomScreen(make_entry())
        fake_app = SimpleNamespace(
            session=SimpleNamespace(
                cfg=SimpleNamespace(user_id="@me:hs"),
                reaction_summary=lambda room_id, event_id: [],
            )
        )
        monkeypatch.setattr(
            RoomScreen, "app", property(lambda self: fake_app), raising=False
        )
        screen.messages = messages
        screen.thread_counts = counts or {}
        screen.expanded = set(expanded)
        screen.threaded = threaded
        return screen

    def test_expand_only_on_a_root_with_replies(self, monkeypatch):
        root, plain = msg("$root", 100, count=2), msg("$plain", 200)
        screen = self.screen(monkeypatch, [root, plain], counts={"$root": 2})
        screen.selected = 0
        assert screen.check_action("expand", None) is True
        screen.selected = 1
        assert screen.check_action("expand", None) is False

    def test_collapse_only_inside_an_unfolded_thread(self, monkeypatch):
        root = msg("$root", 100, count=1)
        reply = msg("$r", 200, root="$root")
        screen = self.screen(
            monkeypatch, [root, reply], counts={"$root": 1}, expanded={"$root"}
        )
        screen.selected = 1
        assert screen.check_action("collapse", None) is True
        screen.expanded = set()
        assert screen.check_action("collapse", None) is False


class TestOwnMessageActions:
    """e/d gating: only our own delivered, undeleted messages."""

    def screen(self, monkeypatch, m):
        screen = RoomScreen(make_entry())
        fake_app = SimpleNamespace(
            session=SimpleNamespace(cfg=SimpleNamespace(user_id="@me:hs"))
        )
        monkeypatch.setattr(
            RoomScreen, "app", property(lambda self: fake_app), raising=False
        )
        screen.messages = [m]
        screen.selected = 0
        return screen

    def own(self, **kw):
        return Message(sender="@me:hs", sender_name="Me", body="mine", ts=1,
                       event_id="$1", **kw)

    def test_own_text_message_offers_both(self, monkeypatch):
        screen = self.screen(monkeypatch, self.own())
        assert screen.check_action("edit_own", None) is True
        assert screen.check_action("delete_own", None) is True

    def test_someone_elses_message_offers_neither(self, monkeypatch):
        other = Message(sender="@a:hs", sender_name="A", body="x", ts=1,
                        event_id="$1")
        screen = self.screen(monkeypatch, other)
        assert screen.check_action("edit_own", None) is False
        assert screen.check_action("delete_own", None) is False

    def test_upload_can_be_deleted_but_not_edited(self, monkeypatch):
        screen = self.screen(monkeypatch, self.own(media_url="mxc://x/y"))
        assert screen.check_action("edit_own", None) is False
        assert screen.check_action("delete_own", None) is True

    def test_pending_and_deleted_offer_neither(self, monkeypatch):
        for m in (self.own(pending=True), self.own(redacted_ts=400)):
            screen = self.screen(monkeypatch, m)
            assert screen.check_action("edit_own", None) is False
            assert screen.check_action("delete_own", None) is False


class TestHistoryPositionReset:
    """Opening a room resets the session's back-pagination position: the
    messages a previous visit paged back died with its screen, and resuming
    from the old token would silently skip everything in between."""

    def fake_app(self, monkeypatch):
        calls = []
        fake = SimpleNamespace(
            session=SimpleNamespace(reset_pagination=calls.append)
        )
        monkeypatch.setattr(
            RoomScreen, "app", property(lambda self: fake), raising=False
        )
        return calls

    def test_room_screen_resets_the_rooms_position(self, monkeypatch):
        calls = self.fake_app(monkeypatch)
        RoomScreen(make_entry())._reset_history_position()
        assert calls == ["!a:hs"]

    def test_thread_screen_leaves_the_rooms_position_alone(self, monkeypatch):
        # The room screen beneath the thread still owns its paged-back window.
        calls = self.fake_app(monkeypatch)
        root = Message(sender="@a:hs", sender_name="A", body="r", ts=1, event_id="$r")
        ThreadScreen(make_entry(), root)._reset_history_position()
        assert calls == []


class TestSendResilience:
    """A send in flight when the user leaves the room must complete, and its
    failure must be reported: screen-owned workers die with the screen, so
    the send runs as an app worker."""

    def test_send_survives_leaving_the_room_and_failure_is_reported(self):
        gate = asyncio.Event()
        done = {}
        notices = []

        async def send(room_id, text, **kwargs):
            await gate.wait()
            done["sent"] = text
            return False, "boom"

        session = SimpleNamespace(
            cfg=SimpleNamespace(user_id="@me:hs"),
            my_name="Me",
            client=SimpleNamespace(rooms={}),
            last_event_id={},
            load_history=lambda room_id, limit=40, cached_only=False: _async([]),
            fetch_fully_read=lambda room_id: _async(None),
            mark_read=lambda room_id: _async(None),
            start_backfill=lambda room_id: None,
            reset_pagination=lambda room_id: None,
            drafts={},
            reaction_summary=lambda room_id, event_id: [],
            send=send,
        )

        class RoomApp(App):
            def on_mount(self):
                self.session = session
                self.last_sync_at = None
                self.sync_ok = True
                return self.push_screen(RoomScreen(make_entry()))

        async def run():
            app = RoomApp()
            async with app.run_test() as pilot:
                await pilot.pause()
                app.notify = lambda message, **kw: notices.append(message)
                # Empty room auto-opens the composer; type and send.
                for ch in "hi":
                    await pilot.press(ch)
                await pilot.press("enter")
                await pilot.pause()
                # Leave the room while the send is still in flight.
                await pilot.press("escape")
                await pilot.pause()
                assert "sent" not in done  # still blocked, not yet complete
                gate.set()
                await app.workers.wait_for_complete()
            return done, notices

        done, notices = asyncio.run(run())
        assert done.get("sent") == "hi"  # the worker was not cancelled
        assert any("boom" in n and "general" in n for n in notices)


class TestRoomFlows:
    """Headless end-to-end checks of the newer room behaviours: draft stash,
    edit/delete of own messages, in-room search, date dividers."""

    def run(self, steps, history=None, fully_read=None):
        calls = {"edits": [], "redacts": []}
        default_history = [
            Message(sender="@me:hs", sender_name="Me", body="mine", ts=1786400000000,
                    event_id="$mine"),
            Message(sender="@a:hs", sender_name="A", body="from Tashkent", ts=1786500000000,
                    event_id="$theirs"),
        ]
        messages = list(history) if history is not None else default_history

        async def send_edit(room_id, target, text):
            calls["edits"].append((target.event_id, text))
            return True, "$edited"

        async def redact(room_id, event_id):
            calls["redacts"].append(event_id)
            return True, "$del"

        session = SimpleNamespace(
            cfg=SimpleNamespace(user_id="@me:hs"),
            my_name="Me",
            client=SimpleNamespace(rooms={}),
            last_event_id={},
            load_history=lambda room_id, limit=40, cached_only=False: _async(list(messages)),
            mark_read=lambda room_id: _async(None),
            start_backfill=lambda room_id: None,
            reset_pagination=lambda room_id: None,
            drafts={},
            reaction_summary=lambda room_id, event_id: [],
            send_edit=send_edit,
            redact=redact,
            timelines={},
            archive_rows=lambda room_id: [],
            cache_allowed=lambda room_id: False,
            archive_done=set(),
            fetch_fully_read=lambda room_id: _async(fully_read),
        )

        class RoomApp(App):
            CSS = MatrixApp.CSS
            BINDINGS = MatrixApp.BINDINGS
            action_go_home = MatrixApp.action_go_home
            action_command_line = MatrixApp.action_command_line

            def on_mount(self):
                self.session = session
                self.last_sync_at = None
                self.sync_ok = True
                return self.push_screen(RoomScreen(make_entry()))

        async def go():
            app = RoomApp()
            async with app.run_test(size=(90, 24)) as pilot:
                await pilot.pause()
                return await steps(pilot, app, app.screen, session, calls,
                                   ComposerArea)

        return asyncio.run(go())

    def test_escape_stashes_the_draft_and_r_hands_it_back(self):
        async def steps(pilot, app, screen, session, calls, composer_area):
            await pilot.press("n")
            await pilot.pause()
            for ch in "wip":
                await pilot.press(ch)
            await pilot.press("escape")
            await pilot.pause()
            stashed = dict(session.drafts)
            await pilot.press("n")
            await pilot.pause()
            editor = screen.query_one("#editor", composer_area)
            return stashed, editor.text

        stashed, refilled = self.run(steps)
        assert stashed == {"!a:hs": "wip"}
        assert refilled == "wip"

    def test_e_prefills_and_sends_an_edit(self):
        async def steps(pilot, app, screen, session, calls, composer_area):
            screen.selected = 0  # our own message
            screen._highlight()
            await pilot.press("e")
            await pilot.pause()
            editor = screen.query_one("#editor", composer_area)
            prefilled = editor.text
            editor.text = "mine, corrected"
            await pilot.press("enter")
            await pilot.pause()
            await app.workers.wait_for_complete()
            return prefilled, calls["edits"]

        prefilled, edits = self.run(steps)
        assert prefilled == "mine"
        assert edits == [("$mine", "mine, corrected")]

    def test_unchanged_edit_sends_nothing(self):
        async def steps(pilot, app, screen, session, calls, composer_area):
            screen.selected = 0
            screen._highlight()
            await pilot.press("e")
            await pilot.pause()
            await pilot.press("enter")  # submit the unchanged text
            await pilot.pause()
            await app.workers.wait_for_complete()
            return calls["edits"]

        assert self.run(steps) == []

    def test_d_deletes_only_after_confirmation(self):
        from matrixcli.app import ConfirmScreen

        async def steps(pilot, app, screen, session, calls, composer_area):
            screen.selected = 0
            screen._highlight()
            await pilot.press("d")
            await pilot.pause()
            first_modal = type(app.screen).__name__
            await pilot.press("escape")  # back out
            await pilot.pause()
            after_cancel = list(calls["redacts"])
            await pilot.press("d")
            await pilot.pause()
            await pilot.press("enter")  # confirm
            await pilot.pause()
            await app.workers.wait_for_complete()
            return first_modal, after_cancel, calls["redacts"]

        first_modal, after_cancel, redacts = self.run(steps)
        assert first_modal == "ConfirmScreen"
        assert after_cancel == []
        assert redacts == ["$mine"]

    def test_slash_search_jumps_to_the_hit(self):
        async def steps(pilot, app, screen, session, calls, composer_area):
            await pilot.press("slash")
            await pilot.pause()
            modal = type(app.screen).__name__
            for ch in "tashkent":  # accent/case-insensitive match
                await pilot.press(ch)
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()
            return modal, type(app.screen).__name__, screen.selected

        modal, back, selected = self.run(steps)
        assert modal == "RoomSearchScreen"
        assert back == "RoomScreen"
        assert selected == 1  # jumped to "from Tashkent"

    def test_slash_search_finds_people_by_name_and_matrix_id(self):
        history = [
            Message(sender="@agnes:hs", sender_name="Ágnes", body="hello",
                    ts=1786400000000, event_id="$by-name"),
            Message(sender="@b:hs", sender_name="B", body="unrelated",
                    ts=1786500000000, event_id="$noise"),
            Message(sender="@carol:hs", sender_name="C", body="hi",
                    ts=1786600000000, event_id="$by-id"),
        ]

        def search(query):
            async def steps(pilot, app, screen, session, calls, composer_area):
                await pilot.press("slash")
                await pilot.pause()
                for ch in query:
                    await pilot.press(ch)
                await pilot.pause()
                await pilot.press("enter")
                await pilot.pause()
                return screen.messages[screen.selected].event_id

            return self.run(steps, history=history)

        # The accent-folded display name finds Ágnes's message even though
        # its body never mentions her.
        assert search("agnes") == "$by-name"
        # The matrix id works when only the account name is known.
        assert search("carol") == "$by-id"

    def test_slash_search_finds_collapsed_thread_replies(self):
        # Collapsed thread replies stay searchable; jumping unfolds the thread.
        root = Message(sender="@a:hs", sender_name="A", body="root here",
                       ts=1786400000000, event_id="$root")
        reply = Message(sender="@b:hs", sender_name="B", body="flag inside thread",
                        ts=1786400001000, event_id="$reply", thread_root="$root")

        async def steps(pilot, app, screen, session, calls, composer_area):
            session.timelines["!a:hs"] = [root, reply]
            await pilot.press("slash")
            await pilot.pause()
            # The popup's border names the room being searched.
            box = app.screen.query_one("#searchbox")
            assert str(box.border_title) == "Search messages in general"
            for ch in "flag":
                await pilot.press(ch)
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()
            await app.workers.wait_for_complete()
            return (
                screen.messages[screen.selected].event_id,
                "$root" in screen.expanded,
            )

        selected, expanded = self.run(steps, history=[root, reply])
        assert selected == "$reply"
        assert expanded

    def test_slash_search_reaches_archived_history(self):
        # An archived hit detaches into browse mode, like walking there with "g".
        old = Message(sender="@a:hs", sender_name="A", body="the flag debate",
                      ts=1786000000000, event_id="$old")
        recent = Message(sender="@b:hs", sender_name="B", body="hello now",
                         ts=1786500000000, event_id="$now")

        async def steps(pilot, app, screen, session, calls, composer_area):
            session.archive_rows = lambda room_id: [old, recent]
            session.cache_allowed = lambda room_id: True
            await pilot.press("slash")
            await pilot.pause()
            for ch in "debate":
                await pilot.press(ch)
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()
            await app.workers.wait_for_complete()
            detached = screen._browse is not None
            landed = screen.messages[screen.selected].event_id
            # "G" reattaches to the live tail.
            await pilot.press("G")
            await pilot.pause()
            await app.workers.wait_for_complete()
            return detached, landed, screen._browse is None

        detached, landed, reattached = self.run(steps, history=[recent])
        assert detached
        assert landed == "$old"
        assert reattached

    def test_open_with_jump_target_lands_on_the_archived_hit(self):
        # Opening a room from a global-search message hit jumps straight to
        # the event, even when it is older than the loaded window.

        old = Message(sender="@a:hs", sender_name="A", body="the flag debate",
                      ts=1786000000000, event_id="$old")
        recent = Message(sender="@b:hs", sender_name="B", body="hello now",
                         ts=1786500000000, event_id="$now")
        session = SimpleNamespace(
            cfg=SimpleNamespace(user_id="@me:hs"),
            my_name="Me",
            client=SimpleNamespace(rooms={}),
            last_event_id={},
            load_history=lambda room_id, limit=40, cached_only=False: _async(
                [recent]
            ),
            mark_read=lambda room_id: _async(None),
            start_backfill=lambda room_id: None,
            reset_pagination=lambda room_id: None,
            drafts={},
            reaction_summary=lambda room_id, event_id: [],
            timelines={},
            archive_rows=lambda room_id: [old, recent],
            cache_allowed=lambda room_id: True,
            archive_done=set(),
            fetch_fully_read=lambda room_id: _async(None),
        )

        class JumpApp(App):
            CSS = MatrixApp.CSS

            def on_mount(self):
                self.session = session
                self.last_sync_at = None
                self.sync_ok = True
                screen = RoomScreen(make_entry())
                screen._jump_target = "$old"
                return self.push_screen(screen)

        async def go():
            app = JumpApp()
            async with app.run_test(size=(90, 24)) as pilot:
                await pilot.pause()
                await app.workers.wait_for_complete()
                s = app.screen
                return s.messages[s.selected].event_id, s._browse is not None

        assert asyncio.run(go()) == ("$old", True)

    def test_divider_uses_the_fetched_marker_when_sync_has_none(self):
        # nio knows no read marker after a resumed sync; the screen fetches it.
        async def steps(pilot, app, screen, session, calls, composer_area):
            await app.workers.wait_for_complete()
            return screen._opened_read_marker, screen._first_unread_event

        marker, first_unread = self.run(steps, fully_read="$mine")
        assert marker == "$mine"
        assert first_unread == "$theirs"

    def test_q_leaves_the_room_instead_of_quitting(self):
        async def steps(pilot, app, screen, session, calls, composer_area):
            await pilot.press("q")
            await pilot.pause()
            return type(app.screen).__name__, bool(app._exit)

        top, exited = self.run(steps)
        assert top != "RoomScreen"
        assert exited is False

    def test_colon_q_bang_quits_from_a_room(self):
        async def steps(pilot, app, screen, session, calls, composer_area):
            await pilot.press("colon")
            await pilot.pause()
            for key in ("q", "exclamation_mark", "enter"):
                await pilot.press(key)
            await pilot.pause()
            return bool(app._exit)

        assert self.run(steps) is True

    def test_colon_q_bang_typed_into_the_composer_stays_text(self):
        async def steps(pilot, app, screen, session, calls, composer_area):
            await pilot.press("n")
            await pilot.pause()
            for key in ("colon", "q", "exclamation_mark"):
                await pilot.press(key)
            await pilot.pause()
            editor = screen.query_one("#editor", composer_area)
            return editor.text, bool(app._exit)

        text, exited = self.run(steps)
        assert text == ":q!"
        assert exited is False

    def test_first_message_of_a_day_keeps_its_name_header(self):
        # A divider must break a same-sender run, or the message shows no name.

        two_days = [
            Message(sender="@a:hs", sender_name="Antonia", body="yesterday",
                    ts=1786400000000, event_id="$1"),
            Message(sender="@a:hs", sender_name="Antonia", body="today",
                    ts=1786500000000, event_id="$2"),
        ]

        async def steps(pilot, app, screen, session, calls, composer_area):
            await pilot.pause()
            text = html.unescape(re.sub(r"<[^>]+>", "", app.export_screenshot()))
            return text.replace("\xa0", " ")

        text = self.run(steps, history=two_days)
        assert text.count("Antonia") == 2

    def test_absurd_timestamp_does_not_crash_the_render(self):
        # A ts outside localtime's range must blank the stamp, not raise.

        weird = [
            Message(sender="@a:hs", sender_name="A", body="fine",
                    ts=1786400000000, event_id="$1"),
            Message(sender="@b:hs", sender_name="B", body="from the far future",
                    ts=10**20, event_id="$2"),
        ]

        async def steps(pilot, app, screen, session, calls, composer_area):
            await pilot.pause()
            text = html.unescape(re.sub(r"<[^>]+>", "", app.export_screenshot()))
            return text.replace("\xa0", " ")

        text = self.run(steps, history=weird)
        assert "from the far future" in text
        assert "--:--" in text

    def test_day_change_inserts_a_divider(self):
        two_days = [
            Message(sender="@a:hs", sender_name="A", body="yesterday", ts=1786400000000,
                    event_id="$1"),
            Message(sender="@a:hs", sender_name="A", body="today", ts=1786500000000,
                    event_id="$2"),
        ]

        async def steps(pilot, app, screen, session, calls, composer_area):
            await pilot.pause()
            text = html.unescape(re.sub(r"<[^>]+>", "", app.export_screenshot()))
            return text.replace("\xa0", " ")

        text = self.run(steps, history=two_days)
        assert "── new ──" not in text  # unrelated divider, sanity
        # One divider, carrying the second day's date.

        stamp = time.strftime("%a %d %b %Y", time.localtime(1786500000000 / 1000))
        assert f"── {stamp} ──" in text


class TestHomeKeys:
    """hjkl on the dashboard, driven through a real (headless) app so the
    bindings, the hidden invites section, and focus all take part."""

    def dashboard(self, invites=(), dms=("Dana", "Eve"), others=()):
        def rooms(names, **kw):
            return [make_entry(room_id=f"!{n}:hs", title=n, **kw) for n in names]

        return {
            "spaces": rooms(["Space1", "Space2"], is_space=True),
            "space_rooms": rooms(["RoomA", "RoomB"]),
            "others": rooms(others),
            "invites": rooms(invites, is_invite=True),
            "recent": rooms(["Chat1", "Chat2"]),
            "favourites": rooms(["Fav1"]),
            "dms": rooms(dms, is_direct=True),
        }

    def walk(self, keys, **kw):
        """Press keys on a freshly mounted HomeScreen; return the (list id,
        highlighted index) the cursor sits on after each one."""
        data = self.dashboard(**kw)
        session = SimpleNamespace(
            cfg=SimpleNamespace(
                user_id="@me:hs",
                save_state=lambda state: None,
                cache_messages=True,
            ),
            state={},
            dashboard=lambda selected_space: data,
            space_cache_enabled=lambda space_id: True,
            section_rows=lambda section: 5,
        )

        class HomeApp(App):
            def on_mount(self):
                self.session = session
                return self.push_screen(HomeScreen())

        async def run():
            positions = []
            app = HomeApp()
            async with app.run_test() as pilot:
                screen = app.screen
                for key in keys:
                    await pilot.press(key)
                    await pilot.pause()
                    for cid in [c for col in screen.COLUMNS for c in col]:
                        lv = screen.query_one(f"#{cid}", ListView)
                        if lv.has_focus:
                            positions.append((cid, lv.index))
                            break
            return positions

        return asyncio.run(run())

    def test_j_and_k_walk_a_column_as_one_list(self):
        # Focus starts on Recent; j runs off its end into Favourites below it,
        # stops at the bottom of the column, and k retraces the same path.
        assert self.walk("jjjkk") == [
            ("recent", 1),
            ("favourites", 0),
            ("favourites", 0),
            ("recent", 1),
            ("recent", 0),
        ]

    def test_arrow_keys_walk_the_column_like_j_and_k(self):
        # Priority bindings route the arrows through the same _step as j/k.
        assert self.walk(["down", "down", "down", "up", "up"]) == [
            ("recent", 1),
            ("favourites", 0),
            ("favourites", 0),
            ("recent", 1),
            ("recent", 0),
        ]

    def test_l_and_h_step_between_columns_and_wrap(self):
        assert self.walk("lhh") == [("dms", 0), ("recent", 0), ("spaces", 0)]

    def test_dms_heading_survives_focus(self):
        # A DMs list taller than the terminal used to scroll its own column
        # when focused, carrying the "DMs" heading off the top. The heading is
        # docked now, so the list scrolls inside itself and the heading stays.
        data = self.dashboard(dms=[f"Person{n}" for n in range(40)])
        session = SimpleNamespace(
            cfg=SimpleNamespace(
                user_id="@me:hs",
                save_state=lambda state: None,
                cache_messages=True,
            ),
            state={},
            dashboard=lambda selected_space: data,
            space_cache_enabled=lambda space_id: True,
            section_rows=lambda section: 5,
        )

        class HomeApp(App):
            CSS = MatrixApp.CSS

            def on_mount(self):
                self.session = session
                return self.push_screen(HomeScreen())

        async def run():
            app = HomeApp()
            async with app.run_test(size=(100, 24)) as pilot:
                screen = app.screen
                dms = screen.query_one("#dms", ListView)
                dms.focus()
                await pilot.pause()
                column = dms.parent
                label = screen.query_one("#dmslabel")
                return (
                    column.scroll_offset.y,
                    column.virtual_size.height <= column.size.height,
                    label.region.y == column.region.y,
                    dms.virtual_size.height > dms.size.height,
                )

        offset, fits, heading_on_top, list_scrolls = asyncio.run(run())
        assert offset == 0
        assert fits
        assert heading_on_top
        assert list_scrolls

    def test_slash_scopes_search_to_recent_and_favourites(self):
        # "/" is scoped on Recent/Favourites, global anywhere else.

        from matrixcli.app import SearchScreen

        data = self.dashboard()
        calls = []
        session = SimpleNamespace(
            cfg=SimpleNamespace(
                user_id="@me:hs",
                save_state=lambda state: None,
                cache_messages=True,
            ),
            state={},
            dashboard=lambda selected_space: data,
            space_cache_enabled=lambda space_id: True,
            section_rows=lambda section: 5,
            search=lambda q, scope=None: calls.append((q, scope)) or [],
            message_index=lambda: [],
        )

        class HomeApp(App):
            def on_mount(self):
                self.session = session
                return self.push_screen(HomeScreen())

        async def run():
            scopes = []
            app = HomeApp()
            async with app.run_test() as pilot:
                for keys in [["slash"], ["j", "j", "slash"], ["h", "slash"]]:
                    for key in keys:
                        await pilot.press(key)
                    await pilot.pause()
                    assert isinstance(app.screen, SearchScreen)
                    scopes.append(
                        (
                            app.screen.scope,
                            str(app.screen.query_one("#searchbox").border_title),
                        )
                    )
                    await pilot.press("escape")
                    await pilot.pause()
            return scopes, calls

        scopes, searched = asyncio.run(run())
        # Focus starts on Recent; jj lands on Favourites; h moves to Spaces.
        # The border names the scope; the placeholder vanishes once typing starts.
        assert scopes == [
            ("recent", "Search all recent rooms"),
            ("favourites", "Search all favourites"),
            (None, "Search people, rooms & messages"),
        ]
        # The scoped screens listed their whole section before any typing;
        # the global one waits for input.
        assert searched == [("", "recent"), ("", "favourites")]

    def test_global_search_finds_messages_and_opens_the_room_there(self):
        from matrixcli.app import MessageHitItem

        hit_entry = make_entry(room_id="!ga:hs", title="ioi.ga")
        hit = Message(sender="@a:hs", sender_name="A", body="capture the flag",
                      ts=1786400000000, event_id="$hit")
        data = self.dashboard()
        opened = []
        session = SimpleNamespace(
            cfg=SimpleNamespace(
                user_id="@me:hs",
                save_state=lambda state: None,
                cache_messages=True,
            ),
            state={},
            dashboard=lambda selected_space: data,
            space_cache_enabled=lambda space_id: True,
            section_rows=lambda section: 5,
            search=lambda q, scope=None: [],
            message_index=lambda: [(hit_entry, hit)],
        )

        class HomeApp(App):
            def on_mount(self):
                self.session = session
                return self.push_screen(HomeScreen())

            def open_room(self, entry, jump_to=None):
                opened.append((entry.room_id, jump_to))

        async def run():
            app = HomeApp()
            async with app.run_test() as pilot:
                await pilot.press("h")  # off Recent, so the search is global
                await pilot.press("slash")
                await pilot.pause()
                for ch in "flag":
                    await pilot.press(ch)
                await pilot.pause()

                rows = list(app.screen.query_one("#results", ListView).children)
                assert len(rows) == 1 and isinstance(rows[0], MessageHitItem)
                await pilot.press("enter")
                await pilot.pause()
            return opened

        # Enter on a message hit opens its room and jumps to the event.
        assert asyncio.run(run()) == [("!ga:hs", "$hit")]

    def test_scoped_search_matches_messages_in_the_sections_rooms(self):
        from matrixcli.app import MessageHitItem

        recent_entry = make_entry(room_id="!ga:hs", title="ioi.ga")
        other_entry = make_entry(room_id="!x:hs", title="elsewhere")
        in_recent = Message(sender="@a:hs", sender_name="A", body="flag one",
                            ts=1786400000000, event_id="$in")
        outside = Message(sender="@b:hs", sender_name="B", body="flag two",
                          ts=1786400001000, event_id="$out")
        data = self.dashboard()
        session = SimpleNamespace(
            cfg=SimpleNamespace(
                user_id="@me:hs",
                save_state=lambda state: None,
                cache_messages=True,
            ),
            state={"last_opened_ts": {"!ga:hs": 1}},
            dashboard=lambda selected_space: data,
            space_cache_enabled=lambda space_id: True,
            section_rows=lambda section: 5,
            search=lambda q, scope=None: [],
            message_index=lambda: [
                (recent_entry, in_recent),
                (other_entry, outside),
            ],
        )

        class HomeApp(App):
            def on_mount(self):
                self.session = session
                return self.push_screen(HomeScreen())

        async def run():
            app = HomeApp()
            async with app.run_test() as pilot:
                await pilot.press("slash")  # focus starts on Recent: scoped
                await pilot.pause()
                for ch in "flag":
                    await pilot.press(ch)
                await pilot.pause()
                rows = list(app.screen.query_one("#results", ListView).children)
                return [
                    (type(r).__name__, getattr(r, "event_id", None))
                    for r in rows
                ]

        # Only the hit from a recently opened room shows; "flag two" lives
        # in a room outside the scope.
        assert asyncio.run(run()) == [("MessageHitItem", "$in")]

    def _sync_all_session(self, calls, remaining):
        data = self.dashboard()
        return SimpleNamespace(
            cfg=SimpleNamespace(
                user_id="@me:hs",
                save_state=lambda state: None,
                cache_messages=True,
            ),
            state={},
            dashboard=lambda selected_space: data,
            space_cache_enabled=lambda space_id: True,
            section_rows=lambda section: 5,
            backfill_all=lambda: calls.append("start") or len(remaining),
            backfill_remaining=lambda: len(remaining),
            backfill_active="!a:hs",
            archives={"!a:hs": {"$1": object(), "$2": object()}},
            room_title=lambda rid: "Room A",
            cancel_backfills=lambda: calls.append("cancel"),
        )

    def test_sync_all_opens_the_progress_popup_and_escape_stops_it(self):
        calls = []
        session = self._sync_all_session(calls, remaining=["!a:hs", "!b:hs"])

        class HomeApp(App):
            def on_mount(self):
                self.session = session
                return self.push_screen(HomeScreen())

        async def run():
            app = HomeApp()
            async with app.run_test() as pilot:
                await pilot.press("S")
                await pilot.pause()
                assert isinstance(app.screen, SyncAllScreen)
                status = str(app.screen.query_one("#syncstatus").render())
                assert "Room A" in status
                assert "1 of 2" in status
                await pilot.press("escape")
                await pilot.pause()
                assert isinstance(app.screen, HomeScreen)
            return calls

        assert asyncio.run(run()) == ["start", "cancel"]

    def test_sync_all_popup_shows_completed_and_any_key_closes(self):
        calls = []
        session = self._sync_all_session(calls, remaining=[])

        class HomeApp(App):
            def on_mount(self):
                self.session = session
                return self.push_screen(HomeScreen())

        async def run():
            app = HomeApp()
            async with app.run_test() as pilot:
                await pilot.press("S")
                await pilot.pause()
                assert isinstance(app.screen, SyncAllScreen)
                status = str(app.screen.query_one("#syncstatus").render())
                assert "already fully downloaded" in status
                await pilot.press("x")
                await pilot.pause()
                assert isinstance(app.screen, HomeScreen)
            return calls

        assert asyncio.run(run()) == ["start"]

    def test_h_returns_to_the_row_you_left_the_column_on(self):
        # Down to Favourites, out to DMs and back: Favourites, not the top.
        assert self.walk("jjlh")[-1] == ("favourites", 0)

    def test_f_follows_the_highlighted_row(self):
        # Rooms offer Favourite, favourites offer Unfavourite, spaces neither.

        data = self.dashboard()
        data["favourites"] = [
            make_entry(room_id="!f:hs", title="Fav1", is_favourite=True)
        ]
        session = SimpleNamespace(
            cfg=SimpleNamespace(
                user_id="@me:hs",
                save_state=lambda state: None,
                cache_messages=True,
            ),
            state={},
            dashboard=lambda selected_space: data,
            space_cache_enabled=lambda space_id: True,
            section_rows=lambda section: 5,
        )

        class HomeApp(App):
            def on_mount(self):
                self.session = session
                return self.push_screen(HomeScreen())

        async def run():
            app = HomeApp()
            async with app.run_test() as pilot:
                await pilot.pause()
                screen = app.screen
                out = {}
                # Focus starts on Recent (a plain room).
                out["room"] = (
                    screen.check_action("favourite_add", None),
                    screen.check_action("favourite_remove", None),
                )
                await pilot.press("j")  # roll into Favourites
                await pilot.press("j")
                await pilot.pause()
                out["favourite"] = (
                    screen.check_action("favourite_add", None),
                    screen.check_action("favourite_remove", None),
                )
                await pilot.press("h")  # into the Spaces column
                await pilot.pause()
                out["space"] = (
                    screen.check_action("favourite_add", None),
                    screen.check_action("favourite_remove", None),
                )
                return out

        out = asyncio.run(run())
        assert out["room"] == (True, False)
        assert out["favourite"] == (False, True)
        assert out["space"] == (False, False)

    def test_hidden_and_empty_sections_are_skipped(self):
        # Invites is displayed only when there are any, and an all-empty column
        # is passed over rather than focused with nothing to highlight.
        assert self.walk("kk") == [("recent", 0), ("recent", 0)]
        assert self.walk("kk", invites=["Inv1"]) == [("invites", 0), ("invites", 0)]
        assert self.walk("l", dms=()) == [("spaces", 0)]

    def test_other_rooms_join_the_left_column(self):
        # Orphan rooms join below Rooms; without any the section is hidden.
        assert self.walk("hjjjj", others=["Weoi"]) == [
            ("spaces", 0),
            ("spaces", 1),
            ("space_rooms", 0),
            ("space_rooms", 1),
            ("other_rooms", 0),
        ]
        assert self.walk("hjjjj")[-1] == ("space_rooms", 1)


class TestSectionRows:
    """+/- on the dashboard's Recent/Favourites adjust that section's row
    budget: floored at MIN_SECTION_ROWS, capped at what the middle column
    fits, clamped back down when the terminal shrinks, and persisted."""

    def run_home(self, steps, state=None):
        def rooms(names, **kw):
            return [make_entry(room_id=f"!{n}:hs", title=n, **kw) for n in names]

        saved = []
        st = dict(state or {})

        def section_rows(section):
            try:
                return max(5, int(st.get(f"{section}_rows") or 0))
            except (TypeError, ValueError):
                return 5

        session = SimpleNamespace(
            cfg=SimpleNamespace(
                user_id="@me:hs",
                save_state=lambda s: saved.append(dict(s)),
                cache_messages=True,
            ),
            state=st,
            dashboard=lambda selected_space: {
                "spaces": rooms(["Space1"], is_space=True),
                "space_rooms": rooms(["RoomA"]),
                "others": [],
                "invites": [],
                "recent": rooms(["Chat1", "Chat2"]),
                "favourites": rooms(["Fav1"]),
                "dms": rooms(["Dana"], is_direct=True),
            },
            space_cache_enabled=lambda space_id: True,
            section_rows=section_rows,
        )

        class HomeApp(App):
            def on_mount(self):
                self.session = session
                return self.push_screen(HomeScreen())

        async def go():
            app = HomeApp()
            async with app.run_test(size=(90, 24)) as pilot:
                await pilot.pause()
                return await steps(pilot, app.screen)

        return asyncio.run(go()), st, saved

    def test_plus_grows_the_focused_section_up_to_the_fit(self):
        async def steps(pilot, screen):
            budget = screen._rows_budget()
            for _ in range(20):  # far past the ceiling: growth must stop
                await pilot.press("plus")
                await pilot.pause()
            return budget

        budget, state, saved = self.run_home(steps)
        assert budget >= 10  # sanity: the floors fit at this size
        # Recent grew to every free line; Favourites kept its floor.
        assert state["recent_rows"] == budget - 5
        assert "favourites_rows" not in state
        assert saved  # persisted along the way

    def test_each_section_grows_independently(self):
        async def steps(pilot, screen):
            await pilot.press("j")  # bottom of Recent
            await pilot.press("j")  # spills into Favourites
            await pilot.press("plus")
            await pilot.pause()

        _, state, _ = self.run_home(steps)
        assert state.get("favourites_rows") == 6
        assert "recent_rows" not in state

    def test_minus_never_goes_below_the_floor(self):
        async def steps(pilot, screen):
            await pilot.press("minus")
            await pilot.pause()

        _, state, saved = self.run_home(steps)
        assert "recent_rows" not in state
        assert saved == []

    def test_grow_then_shrink_round_trips(self):
        async def steps(pilot, screen):
            for key in ("plus", "plus", "minus"):
                await pilot.press(key)
                await pilot.pause()

        _, state, _ = self.run_home(steps)
        assert state["recent_rows"] == 6

    def test_shrinking_terminal_claws_rows_back(self):
        async def steps(pilot, screen):
            budget = screen._rows_budget()
            screen._clamp_section_rows()
            await pilot.pause()
            return budget

        budget, state, saved = self.run_home(
            steps, state={"recent_rows": 99, "favourites_rows": 6}
        )
        # The oversized section is trimmed first, down to what fits beside
        # the other one; neither ends below the floor.
        assert state["recent_rows"] + state["favourites_rows"] == budget
        assert state["recent_rows"] >= 5 and state["favourites_rows"] >= 5
        assert saved


class TestOpenRoomRefresh:
    """Opening a room from the dashboard rebuilds the covered home screen
    right away (recency to the top, badge cleared, stale cleared), so Esc
    back to it finds an unchanged signature and repaints nothing: the
    Recent list must not reorder in front of the user."""

    def test_recent_reorders_behind_the_room_not_on_return(self):
        opened = []
        st = {"last_opened_ts": {"!Chat1:hs": 2, "!Chat2:hs": 1}}

        def rooms(names, **kw):
            return [make_entry(room_id=f"!{n}:hs", title=n, **kw) for n in names]

        def dashboard(selected_space):
            order = sorted(
                ["Chat1", "Chat2"],
                key=lambda n: -st["last_opened_ts"].get(f"!{n}:hs", 0),
            )
            return {
                "spaces": [],
                "space_rooms": [],
                "others": [],
                "invites": [],
                "recent": rooms(order),
                "favourites": rooms(["Fav1"]),
                "dms": [],
            }

        def note_opening(rid):
            opened.append(rid)
            st["last_opened_ts"][rid] = max(st["last_opened_ts"].values()) + 1

        session = SimpleNamespace(
            cfg=SimpleNamespace(
                user_id="@me:hs",
                save_state=lambda s: None,
                cache_messages=True,
            ),
            state=st,
            dashboard=dashboard,
            space_cache_enabled=lambda space_id: True,
            section_rows=lambda section: 5,
            note_opening=note_opening,
            my_name="Me",
            client=SimpleNamespace(rooms={}),
            last_event_id={},
            load_history=lambda room_id, limit=40, cached_only=False: _async([]),
            fetch_fully_read=lambda room_id: _async(None),
            mark_read=lambda room_id: _async(None),
            start_backfill=lambda room_id: None,
            reset_pagination=lambda room_id: None,
            drafts={},
            reaction_summary=lambda room_id, event_id: [],
        )

        class HomeApp(App):
            open_room = MatrixApp.open_room

            def on_mount(self):
                self.session = session
                self.last_sync_at = None
                self.sync_ok = True
                return self.push_screen(HomeScreen())

        async def go():
            app = HomeApp()
            async with app.run_test(size=(90, 24)) as pilot:
                await pilot.pause()
                home = app.screen
                await pilot.press("j")  # highlight Chat2 (second row)
                await pilot.press("enter")
                await pilot.pause()
                await app.workers.wait_for_complete()
                covered_sig = home._last_signature
                in_room = type(app.screen).__name__
                await pilot.press("escape")
                await pilot.pause()
                await app.workers.wait_for_complete()
                first = list(home.query_one("#recent", ListView).children)[0]
                return (
                    in_room,
                    covered_sig,
                    home._last_signature,
                    first.entry.room_id,
                )

        in_room, covered_sig, resumed_sig, top = asyncio.run(go())
        assert opened == ["!Chat2:hs"]
        assert in_room == "RoomScreen"
        # The reorder happened while covered; the resume changed nothing.
        assert resumed_sig == covered_sig
        assert top == "!Chat2:hs"


class TestBrowseHistory:
    """g detaches into archived history at the first message; j at the bottom
    walks forward; G reattaches to the live tail."""

    def run(self, steps, archive_size=5, window_size=2, no_archive=False):
        rows = [
            Message(
                sender="@a:hs", sender_name="A", body=f"old{i}", ts=1000 + i,
                event_id=f"$a{i}",
            )
            for i in range(archive_size)
        ]
        # The live window is the newest slice of the same history.
        history = rows[-window_size:]
        archive = [] if no_archive else rows
        session = SimpleNamespace(
            cfg=SimpleNamespace(user_id="@me:hs"),
            my_name="Me",
            client=SimpleNamespace(rooms={}),
            last_event_id={},
            load_history=lambda room_id, limit=40, cached_only=False: _async(
                list(history)
            ),
            fetch_fully_read=lambda room_id: _async(None),
            mark_read=lambda room_id: _async(None),
            start_backfill=lambda room_id: None,
            reset_pagination=lambda room_id: None,
            archive_rows=lambda room_id: list(archive),
            archive_done={"!a:hs"},
            drafts={},
            reaction_summary=lambda room_id, event_id: [],
        )

        class RoomApp(App):
            CSS = MatrixApp.CSS

            def on_mount(self):
                self.session = session
                self.last_sync_at = None
                self.sync_ok = True
                return self.push_screen(RoomScreen(make_entry()))

        async def go():
            app = RoomApp()
            async with app.run_test(size=(80, 24)) as pilot:
                await pilot.pause()
                return await steps(pilot, app.screen)

        return asyncio.run(go())

    def test_g_browses_from_the_first_archived_message(self):
        async def steps(pilot, screen):
            await pilot.press("g")
            await pilot.pause()
            return {
                "selected": screen.selected,
                "first": screen.messages[0].event_id,
                "count": len(screen.messages),
                "browsing": screen._browse is not None,
            }

        # All 5 archived rows fit in one BROWSE_CHUNK; the view starts at $a0.
        assert self.run(steps) == {
            "selected": 0,
            "first": "$a0",
            "count": 5,
            "browsing": True,
        }

    def test_g_without_archive_jumps_to_first_loaded(self):
        async def steps(pilot, screen):
            await pilot.press("g")
            await pilot.pause()
            return {
                "selected": screen.selected,
                "browsing": screen._browse is not None,
                "count": len(screen.messages),
            }

        # archive_rows returns []: plain jump within the loaded window.
        assert self.run(steps, no_archive=True) == {
            "selected": 0,
            "browsing": False,
            "count": 2,
        }

    def test_g_then_G_reattaches_to_the_live_tail(self):
        async def steps(pilot, screen):
            await pilot.press("g")
            await pilot.pause()
            browsing = screen._browse is not None
            await pilot.press("G")
            await pilot.pause()
            return {
                "was_browsing": browsing,
                "browsing": screen._browse is not None,
                "count": len(screen.messages),
                "selected": screen.selected,
                "last": screen.messages[-1].event_id,
            }

        # Back on the live window: 2 messages, tail selected.
        assert self.run(steps) == {
            "was_browsing": True,
            "browsing": False,
            "count": 2,
            "selected": 1,
            "last": "$a4",
        }

    def test_j_at_the_bottom_extends_then_reattaches(self):
        async def steps(pilot, screen):
            await pilot.press("g")
            await pilot.pause()
            counts = [len(screen.messages)]
            # Walk to the bottom of the first chunk, then one more j.
            screen.selected = len(screen.messages) - 1
            await pilot.press("j")
            await pilot.pause()
            counts.append(len(screen.messages))
            return {
                "counts": counts,
                "browsing": screen._browse is not None,
                "selected": screen.selected,
            }

        # 250 archived rows: g shows the first 200, j at the bottom pulls the
        # remaining 50 and steps onto the first of them.
        out = self.run(steps, archive_size=250, window_size=2)
        assert out["counts"] == [200, 250]
        assert out["browsing"] is True
        assert out["selected"] == 200

    def test_refresh_is_ignored_while_browsing(self):
        async def steps(pilot, screen):
            await pilot.press("g")
            await pilot.pause()
            screen.app.session.last_event_id["!a:hs"] = "$new"
            await screen.refresh_messages()
            return {
                "count": len(screen.messages),
                "browsing": screen._browse is not None,
            }

        # Still the 5 archived rows: the live reload did not clobber the view.
        assert self.run(steps) == {"count": 5, "browsing": True}


class TestVimMotions:
    """Typed counts (10j, 5G), :N jumps, {/} sender blocks, Ctrl+D paging,
    and the Ctrl+O/Ctrl+I jumplist."""

    def run(self, steps, archive_size=30, window_size=20, senders=None):
        def sender_of(i):
            return senders(i) if senders else "@a:hs"

        rows = [
            Message(
                sender=sender_of(i),
                sender_name=sender_of(i),
                body=f"m{i}",
                ts=1000 + i,
                event_id=f"$a{i}",
            )
            for i in range(archive_size)
        ]
        history = rows[-window_size:]
        session = SimpleNamespace(
            cfg=SimpleNamespace(user_id="@me:hs"),
            my_name="Me",
            client=SimpleNamespace(rooms={}),
            last_event_id={},
            load_history=lambda room_id, limit=40, cached_only=False: _async(
                list(history)
            ),
            fetch_fully_read=lambda room_id: _async(None),
            mark_read=lambda room_id: _async(None),
            start_backfill=lambda room_id: None,
            reset_pagination=lambda room_id: None,
            archive_rows=lambda room_id: list(rows),
            archive_done={"!a:hs"},
            drafts={},
            reaction_summary=lambda room_id, event_id: [],
        )

        class RoomApp(App):
            CSS = MatrixApp.CSS

            def on_mount(self):
                self.session = session
                self.last_sync_at = None
                self.sync_ok = True
                self.pending_count = ""
                return self.push_screen(RoomScreen(make_entry()))

        async def go():
            app = RoomApp()
            async with app.run_test(size=(80, 24)) as pilot:
                await pilot.pause()
                return await steps(pilot, app.screen)

        return asyncio.run(go())

    def test_count_repeats_j_and_k(self):
        async def steps(pilot, screen):
            screen.selected = 0
            screen._highlight()
            await pilot.press("1", "0")
            pending = screen.app.pending_count
            await pilot.press("j")
            down = screen.selected
            await pilot.press("4", "k")
            return pending, down, screen.selected, screen.app.pending_count

        assert self.run(steps) == ("10", 10, 6, "")

    def test_stray_key_cancels_a_pending_count(self):
        async def steps(pilot, screen):
            screen.selected = 0
            screen._highlight()
            # "x" is unbound: it must drop the half-typed count.
            await pilot.press("1", "0", "x", "j")
            return screen.selected

        assert self.run(steps) == 1

    def test_goto_message_number_browses_the_archive(self):
        async def steps(pilot, screen):
            screen.goto_message(5)
            await pilot.pause()
            return (
                screen.messages[screen.selected].event_id,
                screen._browse is not None,
            )

        # ":5" is 1-based: the fifth archived message.
        assert self.run(steps) == ("$a4", True)

    def test_goto_opens_a_window_around_the_target(self):
        from textual.containers import VerticalScroll

        async def steps(pilot, screen):
            # Start scrolled to the live tail, as a real room opens.
            tl = screen.query_one("#timeline", VerticalScroll)
            screen.goto_message(1)
            await pilot.pause()
            await pilot.pause()  # the deferred post-layout scroll snap
            first = (
                screen.messages[screen.selected].event_id,
                len(screen.messages),
                tl.scroll_y,
            )
            screen.goto_message(250)
            await pilot.pause()
            mid = (
                screen.messages[screen.selected].event_id,
                screen.messages[0].event_id,
                screen.messages[-1].event_id,
            )
            return first, mid

        # ":1" must fill the page below the first message (a whole chunk),
        # not show one lone row, and the view must snap to the top (the
        # pre-jump scroll offset must not survive the rebuild); a jump
        # outside the open window re-centers it, with context both ways.
        first, mid = self.run(steps, archive_size=300, window_size=20)
        assert first == ("$a0", 200, 0)
        assert mid == ("$a249", "$a150", "$a299")

    def test_goto_past_the_end_lands_on_the_newest(self):
        async def steps(pilot, screen):
            screen.goto_message(10_000_000_000_000)
            await pilot.pause()
            return (
                screen.messages[screen.selected].event_id,
                screen._browse is not None,
            )

        assert self.run(steps) == ("$a29", False)

    def test_count_G_is_goto_and_bare_G_returns(self):
        async def steps(pilot, screen):
            await pilot.press("5", "G")
            await pilot.pause()
            at_five = screen.messages[screen.selected].event_id
            await pilot.press("G")
            await pilot.pause()
            return (
                at_five,
                screen.messages[screen.selected].event_id,
                screen._browse is not None,
            )

        assert self.run(steps) == ("$a4", "$a29", False)

    def test_brace_motions_walk_sender_blocks(self):
        async def steps(pilot, screen):
            # Loaded window is $a10..$a29; blocks change every 3 messages.
            await pilot.press("left_curly_bracket")
            first = screen.messages[screen.selected].event_id
            await pilot.press("left_curly_bracket")
            second = screen.messages[screen.selected].event_id
            await pilot.press("right_curly_bracket")
            third = screen.messages[screen.selected].event_id
            await pilot.press("2", "left_curly_bracket")
            fourth = screen.messages[screen.selected].event_id
            return first, second, third, fourth

        out = self.run(steps, senders=lambda i: f"@{'ab'[(i // 3) % 2]}:hs")
        # From $a29: block start $a27, previous block $a24, } back to $a27,
        # then a count of 2 up to $a21.
        assert out == ("$a27", "$a24", "$a27", "$a21")

    def test_ctrl_d_and_u_page_by_window(self):
        async def steps(pilot, screen):
            screen.selected = 0
            screen._highlight()
            await pilot.press("ctrl+d")
            down = screen.selected
            await pilot.press("ctrl+u")
            return down, screen.selected

        down, back = self.run(steps)
        # Half a 24-row window: several messages, and Ctrl+U undoes it.
        assert down >= 3
        assert back <= 1

    def test_jumplist_walks_back_and_forward(self):
        async def steps(pilot, screen):
            await pilot.press("g")
            await pilot.pause()
            at_start = screen.messages[screen.selected].event_id
            await pilot.press("ctrl+o")
            await pilot.pause()
            returned = (
                screen.messages[screen.selected].event_id,
                screen._browse is not None,
            )
            await pilot.press("ctrl+i")
            await pilot.pause()
            forward = (
                screen.messages[screen.selected].event_id,
                screen._browse is not None,
            )
            return at_start, returned, forward

        assert self.run(steps) == (
            "$a0",
            ("$a29", False),
            ("$a0", True),
        )


class TestTerminalTitle:
    def test_writes_sanitized_osc2(self):
        writes = []
        app = SimpleNamespace(_driver=SimpleNamespace(write=writes.append))
        _set_terminal_title(app, "gen\x1b]0;evil\x07eral")
        # Control characters from a hostile room name are stripped; the rest
        # lands inside a single OSC 2 sequence.
        assert writes == ["\x1b]2;gen]0;evileral\x07"]

    def test_no_driver_is_a_no_op(self):
        _set_terminal_title(SimpleNamespace(), "anything")


class TestTitlebarUnread:
    def app(self, writes, unread, enabled=True):
        session = SimpleNamespace(
            get_setting=lambda key, default=None: enabled,
            total_unread=lambda: unread,
        )
        return SimpleNamespace(
            _driver=SimpleNamespace(write=writes.append), session=session
        )

    def test_counter_prefixes_the_base_title(self):
        writes = []
        _refresh_terminal_title(self.app(writes, 3), "general - matrixcli")
        assert writes == ["\x1b]2;(3) general - matrixcli\x07"]

    def test_zero_unread_shows_no_counter(self):
        writes = []
        _refresh_terminal_title(self.app(writes, 0), "general - matrixcli")
        assert writes == ["\x1b]2;general - matrixcli\x07"]

    def test_setting_off_shows_no_counter(self):
        writes = []
        _refresh_terminal_title(
            self.app(writes, 3, enabled=False), "general - matrixcli"
        )
        assert writes == ["\x1b]2;general - matrixcli\x07"]

    def test_sync_refresh_reuses_the_last_base(self):
        writes = []
        app = self.app(writes, 0)
        _refresh_terminal_title(app, "general - matrixcli")
        app.session.total_unread = lambda: 7
        # The sync loop passes no base: only the count is recomputed.
        _refresh_terminal_title(app)
        assert writes[-1] == "\x1b]2;(7) general - matrixcli\x07"


class TestDisplayTimezone:
    def test_override_applies_to_rendered_times(self):
        try:
            assert _set_display_timezone("UTC") is True
            assert _fmt_time(12 * 3600 * 1000) == "12:00"
            assert _set_display_timezone("Asia/Tokyo") is True
            assert _fmt_time(12 * 3600 * 1000) == "21:00"
        finally:
            _set_display_timezone("")

    def test_unknown_zone_is_rejected_and_keeps_the_old_one(self):
        try:
            _set_display_timezone("UTC")
            assert _set_display_timezone("Not/AZone") is False
            assert _fmt_time(12 * 3600 * 1000) == "12:00"
        finally:
            _set_display_timezone("")


class TestSettingsScreen:
    def run(self, steps, emails=None, set_name_ok=True):
        calls = {"set": [], "name": []}

        def set_display_name(name):
            calls["name"].append(name)
            return _async(set_name_ok)

        session = SimpleNamespace(
            cfg=SimpleNamespace(user_id="@me:hs"),
            my_name="Old Name",
            get_setting=lambda key, default=None: default,
            set_setting=lambda key, value: calls["set"].append((key, value)),
            fetch_email_addresses=lambda: _async(emails),
            set_display_name=set_display_name,
            total_unread=lambda: 0,
        )

        class SettingsApp(App):
            CSS = MatrixApp.CSS

            def on_mount(self):
                self.session = session
                return self.push_screen(SettingsScreen())

        async def go():
            app = SettingsApp()
            async with app.run_test(size=(80, 30)) as pilot:
                await pilot.pause()
                try:
                    return await steps(pilot, app)
                finally:
                    _set_display_timezone("")

        return asyncio.run(go()), calls

    def test_save_persists_toggle_and_timezone_and_closes(self):
        async def steps(pilot, app):
            from textual.widgets import Input, Switch

            app.screen.query_one("#set_tz", Input).value = "UTC"
            app.screen.query_one("#set_unread", Switch).value = False
            await pilot.press("enter")
            await pilot.pause()
            return isinstance(app.screen, SettingsScreen)

        still_open, calls = self.run(steps)
        assert still_open is False
        assert ("timezone", "UTC") in calls["set"]
        assert ("titlebar_unread", False) in calls["set"]
        assert calls["name"] == []  # unchanged name: no server call

    def test_unknown_timezone_keeps_the_screen_open(self):
        async def steps(pilot, app):
            from textual.widgets import Input

            app.screen.query_one("#set_tz", Input).value = "Nope/Nope"
            await pilot.press("enter")
            await pilot.pause()
            return isinstance(app.screen, SettingsScreen)

        still_open, calls = self.run(steps)
        assert still_open is True
        assert calls["set"] == []

    def test_display_name_change_hits_the_server(self):
        async def steps(pilot, app):
            from textual.widgets import Input

            app.screen.query_one("#set_name", Input).value = "New Name"
            await pilot.press("enter")
            await pilot.pause()
            return app.session.my_name

        my_name, calls = self.run(steps)
        assert calls["name"] == ["New Name"]
        assert my_name == "New Name"

    def test_emails_are_listed_read_only(self):
        async def steps(pilot, app):
            await pilot.pause()
            return str(app.screen.query_one("#set_emails", Static).render())

        text, _ = self.run(steps, emails=["a@x.org", "b@y.org"])
        assert "a@x.org" in text and "b@y.org" in text


class TestImagePreview:
    def message(self, **kw):
        defaults = dict(
            sender="@a:hs", sender_name="A", body="pic", ts=1, event_id="$1"
        )
        defaults.update(kw)
        return Message(**defaults)

    def test_is_image_by_mimetype_extension_and_not_otherwise(self):
        assert _is_image(self.message(media_url="mxc://hs/x", media_mime="image/jpeg"))
        assert _is_image(self.message(media_url="mxc://hs/x", media_name="cat.PNG"))
        assert not _is_image(self.message(media_url="mxc://hs/x", media_name="doc.pdf"))
        assert not _is_image(self.message())  # plain text, no upload
        assert not _is_image(
            self.message(media_url="mxc://hs/x", media_mime="image/png", redacted_ts=5)
        )

    def test_check_action_gates_p_to_image_uploads(self):
        screen = RoomScreen(make_entry())
        screen.messages = [self.message(media_url="mxc://hs/x", media_mime="image/png")]
        screen.selected = 0
        assert screen.check_action("preview_image", ()) is True
        screen.messages = [self.message(media_url="mxc://hs/x", media_name="doc.pdf")]
        assert screen.check_action("preview_image", ()) is False
        assert RoomScreen(make_entry()).check_action("preview_image", ()) is False

    def gradient(self):
        from PIL import Image

        img = Image.new("L", (64, 32))
        for x in range(64):
            for y in range(32):
                img.putpixel((x, y), min(255, x * 4))
        return img

    def test_ascii_art_fits_and_keeps_aspect(self):
        art = _ascii_art(self.gradient(), 40, 40, "@:. ")
        lines = art.plain.split("\n")
        assert all(len(line) <= 40 for line in lines) and len(lines) <= 40
        # A cell is twice as tall as wide: a 2:1 image lands near 4:1 in cells.
        assert 3.0 < len(lines[0]) / len(lines) <= 4.5

    def test_ascii_art_maps_bright_to_dense(self):
        from PIL import Image

        img = Image.new("L", (64, 32), 0)
        img.paste(255, (32, 0, 64, 32))  # left half black, right half white
        lines = _ascii_art(img, 40, 40, "@ ").plain.split("\n")
        # ramp[0] carries the most ink and must paint the BRIGHT half (light
        # text on a dark terminal); pure black gets the ramp's last glyph.
        assert lines[0][-1] == "@" and lines[0][0] == " "

    def test_block_art_fits_and_is_all_half_blocks(self):
        art = _block_art(self.gradient(), 40, 40)
        lines = art.plain.split("\n")
        assert all(len(line) <= 40 for line in lines) and len(lines) <= 40
        assert set("".join(lines)) == {"▀"}
        # Half-blocks show square pixels: a 2:1 image lands near 4:1 in cells
        # (two pixel rows per cell row).
        assert 3.0 < len(lines[0]) / len(lines) <= 4.5

    def test_block_art_dithered_emits_exact_palette_indices(self):
        art = _block_art(self.gradient(), 40, 40, dither=True)
        assert "▀" in art.plain
        assert art.spans
        for span in art.spans:
            for color in (span.style.foreground, span.style.background):
                # ansi carries the exact palette index to the terminal; only
                # the predictable cube/gray entries are allowed, never the
                # 16 themeable system colors.
                assert color.ansi is not None and 16 <= color.ansi <= 255

    def test_one_pixel_image_does_not_crash(self):
        from PIL import Image

        tiny = Image.new("L", (1, 1), 0)
        assert _ascii_art(tiny, 80, 24, "@ ").plain.strip("\n") != ""
        assert "▀" in _block_art(tiny, 80, 24).plain

    def test_color_depth_ladder(self):
        from matrixcli.app import _color_depth

        assert _color_depth("truecolor") == "truecolor"
        assert _color_depth("256") == "256"
        # 16-color, Windows legacy, and no-color consoles all land on basic.
        assert _color_depth("standard") == "basic"
        assert _color_depth("windows") == "basic"
        assert _color_depth(None) == "basic"


class TestPreviewScreen:
    def run(self, steps):
        """Open a room holding two image uploads (a text row between them) in
        a headless app, with a session whose fetch_preview_bytes serves a real
        PNG, and hand the pilot to `steps`. The selection starts on the last
        row, cat2.png."""
        import io

        from PIL import Image

        buf = io.BytesIO()
        Image.new("RGB", (32, 16), (200, 30, 30)).save(buf, format="PNG")
        png = buf.getvalue()

        history = [
            Message(
                sender="@a:hs", sender_name="A", body="cat1.png", ts=100,
                event_id="$img1", media_url="mxc://hs/img1",
                media_name="cat1.png", media_mime="image/png",
            ),
            Message(
                sender="@a:hs", sender_name="A", body="just text", ts=101,
                event_id="$txt",
            ),
            Message(
                sender="@a:hs", sender_name="A", body="cat2.png", ts=102,
                event_id="$img2", media_url="mxc://hs/img2",
                media_name="cat2.png", media_mime="image/png",
            ),
        ]
        state = {}
        session = SimpleNamespace(
            cfg=SimpleNamespace(
                user_id="@me:hs",
                ascii_ramp="@:. ",
                save_state=lambda s: None,
            ),
            my_name="Me",
            client=SimpleNamespace(rooms={}),
            last_event_id={},
            load_history=lambda room_id, limit=40, cached_only=False: _async(list(history)),
            fetch_fully_read=lambda room_id: _async(None),
            mark_read=lambda room_id: _async(None),
            start_backfill=lambda room_id: None,
            reset_pagination=lambda room_id: None,
            drafts={},
            reaction_summary=lambda room_id, event_id: [],
            state=state,
            fetch_preview_bytes=lambda m, w, h, room_id="": _async((True, png)),
        )

        class RoomApp(App):
            CSS = MatrixApp.CSS

            def on_mount(self):
                self.session = session
                self.last_sync_at = None
                self.sync_ok = True
                return self.push_screen(RoomScreen(make_entry()))

        async def run() -> dict:
            app = RoomApp()
            async with app.run_test(size=(80, 24)) as pilot:
                await pilot.pause()
                return await steps(pilot, app, PreviewScreen, state)

        return asyncio.run(run())

    def test_space_opens_toggles_persists_and_closes(self):
        async def steps(pilot, app, preview_cls, state):
            await pilot.press("space")
            await pilot.pause()  # popup mounts, fetch worker starts
            await pilot.pause()  # worker lands and renders
            assert isinstance(app.screen, preview_cls)
            blocks = app.screen.query_one("#previewart", Static).content.plain
            assert "▀" in blocks  # half-blocks are the default style

            await pilot.press("tilde")
            await pilot.pause()
            ascii_art = app.screen.query_one("#previewart", Static).content.plain
            # Solid (200,30,30) red has luminance ~81, which the "@:. " ramp
            # maps to ".": a grid of dots, drawn purely from ramp glyphs.
            assert set(ascii_art) <= set("@:. \n") and "." in ascii_art

            await pilot.press("tilde")
            await pilot.pause()
            back = app.screen.query_one("#previewart", Static).content.plain
            mode_after_two_flips = state.get("preview_mode")

            await pilot.press("escape")
            await pilot.pause()
            return {
                "blocks_again": "▀" in back,
                "mode": mode_after_two_flips,
                "closed": isinstance(app.screen, RoomScreen),
            }

        out = self.run(steps)
        assert out["blocks_again"] is True
        assert out["mode"] == "blocks"  # ~ twice lands back where it started
        assert out["closed"] is True

    def test_j_k_walk_the_images_and_selection_follows(self):
        async def steps(pilot, app, preview_cls, state):
            def title():
                # startswith: on a non-truecolor console the title carries a
                # dim "256-color terminal" suffix after the filename.
                return str(app.screen.query_one("#previewtitle", Label).content)

            await pilot.press("space")
            await pilot.pause()
            await pilot.pause()
            assert title().startswith("cat2.png")
            # No image below cat2: j is gated off and must do nothing.
            assert app.screen.check_action("next_image", ()) is False
            await pilot.press("j")
            await pilot.pause()
            assert title().startswith("cat2.png")

            # k skips the text row and lands on cat1.
            await pilot.press("k")
            await pilot.pause()
            await pilot.pause()
            assert title().startswith("cat1.png")
            assert app.screen.check_action("prev_image", ()) is False

            await pilot.press("escape")
            await pilot.pause()
            room = app.screen
            return {"closed": isinstance(room, RoomScreen), "selected": room.selected}

        out = self.run(steps)
        assert out["closed"] is True
        assert out["selected"] == 0  # the timeline followed the j/k walk

    def test_window_resize_rescales_the_art(self):
        async def steps(pilot, app, preview_cls, state):
            await pilot.press("space")
            await pilot.pause()
            await pilot.pause()
            wide = app.screen.query_one("#previewart", Static).content.plain
            await pilot.resize_terminal(40, 24)
            await pilot.pause()
            narrow = app.screen.query_one("#previewart", Static).content.plain
            return {
                "wide": max(len(line) for line in wide.split("\n")),
                "narrow": max(len(line) for line in narrow.split("\n")),
            }

        out = self.run(steps)
        # 80->40 columns: the art re-rendered to roughly half the width, with
        # no refetch (the decoded image is cached on the screen).
        assert out["narrow"] <= 40 < out["wide"]

    def test_basic_terminal_forces_ascii_and_hides_the_toggle(self, monkeypatch):
        import matrixcli.app as app_module

        monkeypatch.setattr(app_module, "_color_depth", lambda system: "basic")

        async def steps(pilot, app, preview_cls, state):
            state["preview_mode"] = "blocks"  # a remembered choice cannot win
            await pilot.press("space")
            await pilot.pause()
            await pilot.pause()
            screen = app.screen
            art = screen.query_one("#previewart", Static).content.plain
            title = str(screen.query_one("#previewtitle", Label).content)
            return {
                "mode": screen.mode,
                "no_blocks": "▀" not in art and "." in art,
                "toggle_hidden": screen.check_action("mode_blocks", ())
                or screen.check_action("mode_ascii", ()),
                "titled": "16-color" in title,
            }

        out = self.run(steps)
        assert out["mode"] == "ascii"
        assert out["no_blocks"] is True
        assert out["toggle_hidden"] is False
        assert out["titled"] is True

    def test_render_failure_shows_error_instead_of_crashing(self, monkeypatch):
        import matrixcli.app as app_module

        def boom(*args, **kwargs):
            raise ValueError("bad mode")

        monkeypatch.setattr(app_module, "_block_art", boom)

        async def steps(pilot, app, preview_cls, state):
            await pilot.press("space")
            await pilot.pause()
            await pilot.pause()
            art = app.screen.query_one("#previewart", Static).content.plain
            return {"error_shown": "Preview failed" in art and "bad mode" in art}

        out = self.run(steps)
        assert out["error_shown"] is True


class TestIncrementalRedraw:
    """_redraw rebuilds only from the first row whose signature changed, so a
    reaction on a recent message does not remount the whole window. Whatever
    it produces must match what a from-scratch rebuild produces."""

    DAY_MS = 24 * 3600 * 1000

    def history(self):
        # Two calendar days, so a day divider lands mid-list and the
        # (prev sender, prev day) carry into a partial rebuild is exercised.
        return [
            Message(
                sender=f"@u{i % 3}:hs",
                sender_name=f"U{i % 3}",
                body=f"m{i}",
                ts=1700000000000 + (0 if i < 6 else self.DAY_MS) + i * 1000,
                event_id=f"$m{i}",
            )
            for i in range(12)
        ]

    def shape(self, screen):
        """Every mounted row as (kind, message index, rendered text, classes),
        which is exactly what the partial rebuild has to get right."""
        from rich.console import Console
        from textual.containers import VerticalScroll

        console = Console(width=78, no_color=True, legacy_windows=False)
        out = []
        for w in screen.query_one("#timeline", VerticalScroll).children:
            with console.capture() as cap:
                console.print(w.content)
            out.append(
                (
                    "line" if isinstance(w, MessageLine) else "other",
                    getattr(w, "msg_index", None),
                    cap.get().rstrip(),
                    tuple(sorted(w.classes)),
                )
            )
        return out

    def run(self, mutate):
        """Draw a room, apply `mutate`, redraw incrementally, then force a
        full rebuild of the same state; return both shapes."""
        rows = self.history()
        session = SimpleNamespace(
            cfg=SimpleNamespace(user_id="@me:hs"),
            my_name="Me",
            client=SimpleNamespace(rooms={}),
            last_event_id={},
            load_history=lambda room_id, limit=40, cached_only=False: _async(
                list(rows)
            ),
            fetch_fully_read=lambda room_id: _async(None),
            mark_read=lambda room_id: _async(None),
            start_backfill=lambda room_id: None,
            reset_pagination=lambda room_id: None,
            drafts={},
            reaction_summary=lambda room_id, event_id: [],
        )

        class RoomApp(App):
            CSS = MatrixApp.CSS

            def on_mount(self):
                self.session = session
                self.last_sync_at = None
                self.sync_ok = True
                return self.push_screen(RoomScreen(make_entry()))

        async def go():
            app = RoomApp()
            async with app.run_test(size=(80, 24)) as pilot:
                await pilot.pause()
                screen = app.screen
                mutate(screen)
                await screen._redraw()
                await pilot.pause()
                incremental = self.shape(screen)
                screen._drawn_sig = None  # force the from-scratch path
                await screen._redraw()
                await pilot.pause()
                return incremental, self.shape(screen)

        return asyncio.run(go())

    def check(self, mutate):
        incremental, full = self.run(mutate)
        assert incremental == full
        return incremental

    def test_edit_of_the_first_message(self):
        # Divergence at index 0: the whole window is rebuilt, as before.
        def mutate(screen):
            screen.messages[0] = replace(
                screen.messages[0], body="EDITED", edited_ts=99
            )

        assert any("EDITED" in row[2] for row in self.check(mutate))

    def test_edit_of_a_message_after_a_day_divider(self):
        def mutate(screen):
            screen.messages[6] = replace(
                screen.messages[6], body="EDITED6", edited_ts=99
            )

        assert any("EDITED6" in row[2] for row in self.check(mutate))

    def test_deletion_in_the_middle(self):
        self.check(
            lambda screen: screen.messages.__setitem__(
                5, replace(screen.messages[5], redacted_ts=123)
            )
        )

    def test_messages_dropped_from_the_tail(self):
        def mutate(screen):
            del screen.messages[8:]

        self.check(mutate)

    def test_plain_tail_append(self):
        def mutate(screen):
            screen.messages.append(
                Message(
                    sender="@z:hs", sender_name="Z", body="brand new",
                    ts=screen.messages[-1].ts + 1000, event_id="$new",
                )
            )

        assert any("brand new" in row[2] for row in self.check(mutate))

    def test_sender_change_repairs_header_suppression(self):
        # The carry into a partial rebuild is the previous row's sender; get
        # it wrong and the next message renders attributed to nobody.
        def mutate(screen):
            screen.messages[4] = replace(
                screen.messages[4], sender="@zz:hs", sender_name="ZZ"
            )

        self.check(mutate)

    def test_every_message_replaced(self):
        def mutate(screen):
            screen.messages[:] = [
                replace(m, body=f"x{i}", event_id=f"$x{i}")
                for i, m in enumerate(screen.messages)
            ]

        self.check(mutate)
