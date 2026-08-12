import asyncio
from dataclasses import replace
from types import SimpleNamespace

from rich.text import Text

from matrixcli.app import (
    SENDER_COLORS,
    ActionScreen,
    DownloadScreen,
    HistoryScreen,
    HomeScreen,
    RoomScreen,
    ThreadScreen,
    _find_urls,
    _fmt_time,
    _sender_color,
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
        # RoomScreen.app is a property that needs a running App; a plain
        # attribute here shadows it, and the two side effects are recorded
        # instead of performed.
        app = SimpleNamespace(push_screen=lambda *a, **kw: None)

        def __init__(self, *a, **kw):
            super().__init__(*a, **kw)
            self.opened = []
            self.pushed = []
            self.app = SimpleNamespace(
                push_screen=lambda screen, cb=None: self.pushed.append(screen)
            )

        def _open_url(self, url):
            self.opened.append(url)

    def make_screen(self, body="", **kw):
        screen = self.Screen(make_entry())
        screen.messages = [
            Message(sender="@a:hs", sender_name="A", body=body, ts=1, event_id="$1", **kw)
        ]
        screen.selected = 0
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

    def test_shift_enter_opens_the_history(self):
        screen = self.make_screen("fixed", edited_ts=200, original_body="typo")
        screen.action_open_details()
        assert isinstance(screen.pushed[0], HistoryScreen)

    def test_enter_and_shift_enter_do_not_compete(self):
        # An edited message that also holds a link: Enter follows the link,
        # Shift+Enter shows the versions. Neither has to guess.
        screen = self.make_screen("fixed https://a.example", edited_ts=200)
        screen._open_selected()
        assert screen.opened == ["https://a.example"]
        screen.action_open_details()
        assert isinstance(screen.pushed[0], HistoryScreen)

    def test_deleted_message_offers_only_its_kept_text(self):
        # Nothing to download, and no following links out of text that was
        # withdrawn; the kept copy is all there is.
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

    def test_the_footer_offers_shift_enter_only_where_there_is_history(self):
        for message, expected in (
            (self.make_screen("x", edited_ts=200), True),
            (self.make_screen("kept", redacted_ts=400), True),
            (self.make_screen("", redacted_ts=400), False),
            (self.make_screen("plain https://a.example"), False),
        ):
            assert message.check_action("open_details", None) is expected


class TestPickerKeys:
    # A subclass redeclaring BINDINGS would silently drop the vim keys.
    def test_pickers_take_vim_keys_and_escape(self):
        for cls in (DownloadScreen, ActionScreen):
            keys = cls._merged_bindings.key_to_bindings
            assert {"j", "k", "escape"} <= set(keys), cls.__name__


class TestComposerTitle:
    """The docked composer's header line: it is the only thing naming the
    message being replied to, now that the editor no longer sits under it."""

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

    def test_no_signals_means_read(self):
        assert self.make_screen()._first_unread_index() is None

    def test_empty_room(self):
        assert self.make_screen(n=0, marker="$x")._first_unread_index() is None

    def test_divider_anchored_at_open_does_not_drift(self):
        # The count fallback is only meaningful against the opening snapshot;
        # once anchored to an event id, later arrivals must not move it.
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
        # The room's unread count counts main-timeline events; applying it to
        # a thread's reply list would place the divider at a meaningless spot.
        assert self.make_screen(unread=3)._first_unread_index() is None

    def test_marker_inside_thread_places_divider(self):
        assert self.make_screen(marker="$2")._first_unread_index() == 3

    def test_marker_at_last_reply_means_no_unread(self):
        assert self.make_screen(marker="$4")._first_unread_index() is None


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

        async def load_history(room_id):
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
        # The reply's root is older than the loaded window: it must stay
        # visible at its chronological position instead of being hidden.
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

        async def load_history(room_id):
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
        # Threads do not nest: T on a reply must open the reply's thread, not
        # start a spec-invalid thread rooted at the reply itself.
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
        # The sync echo can outrun the /send response; until the response
        # swaps in the real id, the echo and the arrived event are the same
        # message and must not render twice.
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
        # After confirmation swaps in the real event id, a reload that already
        # contains the sync echo must not duplicate the message.
        screen = RoomScreen(make_entry())
        echo = self.msg("$real", pending=True)
        screen._pending.append(echo)
        out = screen._splice_pending([self.msg("$real")])
        assert len(out) == 1

    def test_thread_latest_skips_pending_echoes(self):
        # A provisional "~local." id must never leave the client as the
        # thread reply-fallback event id.
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
        from matrixcli.app import MatrixApp

        assert MatrixApp.ENABLE_COMMAND_PALETTE is False

    def test_ctrl_q_neutralized_q_quits_and_about_bound(self):
        from matrixcli.app import MatrixApp

        actions = {}
        for b in MatrixApp.BINDINGS:
            key = b.key if hasattr(b, "key") else b[0]
            action = b.action if hasattr(b, "action") else b[1]
            actions[key] = action
        assert actions["ctrl+q"] == "noop"
        assert actions["q"] == "quit"
        assert actions["question_mark"] == "about"
        assert actions["ctrl+r"] == "force_refresh"


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
        import time

        text = self.render(time.monotonic() - 5, True)
        assert text.plain.startswith("● ")
        assert text.plain.endswith("5s")
        assert "offline" not in text.plain

    def test_failed_sync_shows_offline(self):
        import time

        text = self.render(time.monotonic() - 5, False)
        assert "offline" in text.plain
        assert text.plain.endswith("5s")

    def test_silently_hung_poll_counts_as_offline(self):
        # No error was raised, but nothing has synced within STALE_AFTER: a
        # dropped network hangs the long-poll without failing it.
        import time

        text = self.render(time.monotonic() - 120, True)
        assert "offline" in text.plain
        assert text.plain.endswith("2m")


class TestEditedMessages:
    """An edit folded into the timeline: one line, a trailing "*", and the "H"
    key offered only while that line is selected."""

    def screen(self, monkeypatch, history):
        screen = RoomScreen(make_entry())

        async def load_history(room_id):
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
        # The text is not on screen, but Shift+Enter can still show it: we
        # received it before the deletion and the server no longer has it.
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
        # Both rewrite a message in place, leaving the list of event ids
        # untouched; a signature of ids alone would skip the redraw.
        screen = self.screen(monkeypatch, self.history())
        before = screen._signature(screen.messages)
        edited = [replace(screen.messages[0], edited_ts=999), screen.messages[1]]
        deleted = [replace(screen.messages[0], redacted_ts=999), screen.messages[1]]
        assert screen._signature(edited) != before
        assert screen._signature(deleted) != before


class TestComposerPanel:
    """The composer is docked under the timeline, not mounted inside it, so a
    long draft scrolls in its own five rows and a live refresh cannot disturb
    what is being typed."""

    def run(self, steps):
        """Open a room in a headless app and hand it to `steps`."""
        from textual.app import App
        from textual.containers import Vertical

        from matrixcli.app import ComposerArea, MatrixApp

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
            load_history=lambda room_id, limit=40: _async(list(history)),
            mark_read=lambda room_id: _async(None),
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
            await pilot.press("R")
            await pilot.pause()
            editor = screen.query_one("#editor", composer_area)
            for _ in range(9):
                await pilot.press("x")
                await pilot.press("shift+enter")
            await pilot.press("y")
            await pilot.pause()
            # The cursor sits on line 10 of a five-row box: the box scrolled
            # rather than the draft running off the bottom of the screen.
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
            await pilot.press("R")
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
            await pilot.press("R")
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
        from textual.app import App

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
            load_history=lambda room_id, limit=40: _async([]),
            mark_read=lambda room_id: _async(None),
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

    def run(self, steps, history=None):
        from textual.app import App

        from matrixcli.app import ComposerArea, MatrixApp

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
            load_history=lambda room_id, limit=40: _async(list(messages)),
            mark_read=lambda room_id: _async(None),
            reset_pagination=lambda room_id: None,
            drafts={},
            reaction_summary=lambda room_id, event_id: [],
            send_edit=send_edit,
            redact=redact,
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
            async with app.run_test(size=(90, 24)) as pilot:
                await pilot.pause()
                return await steps(pilot, app, app.screen, session, calls,
                                   ComposerArea)

        return asyncio.run(go())

    def test_escape_stashes_the_draft_and_r_hands_it_back(self):
        async def steps(pilot, app, screen, session, calls, composer_area):
            await pilot.press("R")
            await pilot.pause()
            for ch in "wip":
                await pilot.press(ch)
            await pilot.press("escape")
            await pilot.pause()
            stashed = dict(session.drafts)
            await pilot.press("R")
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

    def test_day_change_inserts_a_divider(self):
        import html
        import re

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
        import time as _time

        stamp = _time.strftime("%a %d %b %Y", _time.localtime(1786500000000 / 1000))
        assert f"── {stamp} ──" in text


class TestHomeKeys:
    """hjkl on the dashboard, driven through a real (headless) app so the
    bindings, the hidden invites section, and focus all take part."""

    def dashboard(self, invites=(), dms=("Dana", "Eve")):
        def rooms(names, **kw):
            return [make_entry(room_id=f"!{n}:hs", title=n, **kw) for n in names]

        return {
            "spaces": rooms(["Space1", "Space2"], is_space=True),
            "space_rooms": rooms(["RoomA", "RoomB"]),
            "invites": rooms(invites, is_invite=True),
            "recent": rooms(["Chat1", "Chat2"]),
            "favourites": rooms(["Fav1"]),
            "dms": rooms(dms, is_direct=True),
        }

    def walk(self, keys, **kw):
        """Press keys on a freshly mounted HomeScreen; return the (list id,
        highlighted index) the cursor sits on after each one."""
        from textual.app import App
        from textual.widgets import ListView

        data = self.dashboard(**kw)
        session = SimpleNamespace(
            cfg=SimpleNamespace(user_id="@me:hs", save_state=lambda state: None),
            state={},
            dashboard=lambda selected_space: data,
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

    def test_l_and_h_step_between_columns_and_wrap(self):
        assert self.walk("lhh") == [("dms", 0), ("recent", 0), ("spaces", 0)]

    def test_h_returns_to_the_row_you_left_the_column_on(self):
        # Down to Favourites, out to DMs and back: Favourites, not the top.
        assert self.walk("jjlh")[-1] == ("favourites", 0)

    def test_f_follows_the_highlighted_row(self):
        # Rooms offer Favourite, favourites offer Unfavourite, spaces neither.
        from textual.app import App

        data = self.dashboard()
        data["favourites"] = [
            make_entry(room_id="!f:hs", title="Fav1", is_favourite=True)
        ]
        session = SimpleNamespace(
            cfg=SimpleNamespace(user_id="@me:hs", save_state=lambda state: None),
            state={},
            dashboard=lambda selected_space: data,
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
