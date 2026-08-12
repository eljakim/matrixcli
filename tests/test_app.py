import asyncio
from types import SimpleNamespace

from rich.text import Text

from matrixcli.app import (
    SENDER_COLORS,
    DownloadScreen,
    HomeScreen,
    LinkScreen,
    RoomScreen,
    ThreadScreen,
    _find_urls,
    _fmt_time,
    _sender_color,
)
from matrixcli.client import Entry, Message


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
    """Enter routes to the right thing: download for a file, browser for a
    link, a picker when a message holds several."""

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
        screen.action_open()
        assert screen.opened == ["https://a.example/x"]
        assert screen.pushed == []

    def test_several_links_show_the_picker(self):
        screen = self.make_screen("https://a.example and https://b.example")
        screen.action_open()
        assert screen.opened == []
        assert isinstance(screen.pushed[0], LinkScreen)
        assert screen.pushed[0].urls == ["https://a.example", "https://b.example"]

    def test_file_still_offers_the_download_dialog(self):
        screen = self.make_screen("photo.png https://a.example", media_url="mxc://x/y")
        screen.action_open()
        assert screen.opened == []
        assert isinstance(screen.pushed[0], DownloadScreen)

    def test_message_without_a_link_does_nothing(self):
        screen = self.make_screen("no links here")
        screen.action_open()
        assert screen.opened == [] and screen.pushed == []

    def test_empty_room_does_nothing(self):
        screen = self.Screen(make_entry())
        screen.action_open()
        assert screen.opened == [] and screen.pushed == []


class TestPickerKeys:
    # A subclass redeclaring BINDINGS would silently drop the vim keys.
    def test_pickers_take_vim_keys_and_escape(self):
        for cls in (DownloadScreen, LinkScreen):
            keys = cls._merged_bindings.key_to_bindings
            assert {"j", "k", "escape"} <= set(keys), cls.__name__


class TestIsReplyTarget:
    def make_screen(self):
        return RoomScreen(make_entry())

    def msg(self, event_id=""):
        return Message(
            sender="@a:hs", sender_name="A", body="x", ts=1, event_id=event_id
        )

    def test_no_reply_target(self):
        screen = self.make_screen()
        assert not screen._is_reply_target(self.msg("$1"))

    def test_identity_match(self):
        screen = self.make_screen()
        m = self.msg()
        screen.reply_to = m
        assert screen._is_reply_target(m)

    def test_event_id_match_across_reloaded_objects(self):
        screen = self.make_screen()
        screen.reply_to = self.msg("$1")
        assert screen._is_reply_target(self.msg("$1"))
        assert not screen._is_reply_target(self.msg("$2"))

    def test_empty_event_ids_do_not_match_each_other(self):
        screen = self.make_screen()
        screen.reply_to = self.msg("")
        assert not screen._is_reply_target(self.msg(""))


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
    def msg(self, event_id, pending=False, ts=1):
        return Message(
            sender="@a:hs",
            sender_name="A",
            body="x",
            ts=ts,
            event_id=event_id,
            pending=pending,
        )

    def test_splice_appends_in_flight_echoes(self):
        screen = RoomScreen(make_entry())
        echo = self.msg("~local.1", pending=True)
        screen._pending.append(echo)
        out = screen._splice_pending([self.msg("$1")])
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

    def test_edits_key_is_offered_only_on_an_edited_message(self, monkeypatch):
        screen = self.screen(monkeypatch, self.history())
        screen.selected = 0
        assert screen.check_action("edits", None) is True
        screen.selected = 1
        assert screen.check_action("edits", None) is False


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

    def test_hidden_and_empty_sections_are_skipped(self):
        # Invites is displayed only when there are any, and an all-empty column
        # is passed over rather than focused with nothing to highlight.
        assert self.walk("kk") == [("recent", 0), ("recent", 0)]
        assert self.walk("kk", invites=["Inv1"]) == [("invites", 0), ("invites", 0)]
        assert self.walk("l", dms=()) == [("spaces", 0)]
