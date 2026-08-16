import asyncio
from dataclasses import replace
from types import SimpleNamespace

import aiohttp
from nio.events.room_events import RoomMessageText

from matrixcli.client import MatrixSession, Message, fold_edits

ME = "@me:example.org"
ALICE = "@alice:example.org"
BOB = "@bob:example.org"


def text_event(event_id, sender, ts, body, relates=None, unsigned=None):
    content = {"msgtype": "m.text", "body": body}
    if relates:
        content["m.relates_to"] = relates
    event = {
        "type": "m.room.message",
        "event_id": event_id,
        "sender": sender,
        "origin_server_ts": ts,
        "content": content,
    }
    if unsigned:
        event["unsigned"] = unsigned
    return RoomMessageText.from_dict(event)


def edit_source(event_id, sender, ts, new_body, target):
    """The wire form of an m.replace edit, as senders write it: the new text in
    m.new_content, and a "* "-prefixed copy in body for clients that do not
    understand edits."""
    return {
        "type": "m.room.message",
        "event_id": event_id,
        "sender": sender,
        "origin_server_ts": ts,
        "content": {
            "msgtype": "m.text",
            "body": f"* {new_body}",
            "m.new_content": {"msgtype": "m.text", "body": new_body},
            "m.relates_to": {"rel_type": "m.replace", "event_id": target},
        },
    }


def edit_event(event_id, sender, ts, new_body, target):
    return RoomMessageText.from_dict(
        edit_source(event_id, sender, ts, new_body, target)
    )


class TestEntry:
    def test_dm_via_m_direct(self, session, fake_room):
        room = fake_room("!a:hs", users={ME: {}, ALICE: {}}, names={ALICE: "Alice"})
        session.direct_by_room["!a:hs"] = ALICE
        e = session._entry(room)
        assert e.is_direct and e.person == ALICE and e.title == "Alice"

    def test_dm_via_two_member_fallback(self, session, fake_room):
        room = fake_room("!a:hs", users={ME: {}, ALICE: {}})
        e = session._entry(room)
        assert e.is_direct and e.person == ALICE

    def test_group_room(self, session, fake_room):
        room = fake_room(
            "!g:hs", display_name="general", users={ME: {}, ALICE: {}, BOB: {}}
        )
        e = session._entry(room)
        assert not e.is_direct and e.person is None and e.title == "general"

    def test_space_and_favourite_and_unread(self, session, fake_room):
        room = fake_room(
            "!s:hs",
            room_type="m.space",
            unread=2,
            highlights=1,
            tags={"m.favourite": {}},
        )
        e = session._entry(room)
        # notification_count already includes highlights, so unread is 2,
        # not 2 + 1.
        assert e.is_space and e.is_favourite and e.unread == 2


class TestDashboard:
    def test_dms_dedup_per_person_keeps_newest_and_merges_unread(
        self, session, fake_room
    ):
        old = fake_room("!old:hs", users={ME: {}, ALICE: {}}, unread=5)
        new = fake_room("!new:hs", users={ME: {}, ALICE: {}})
        session.client.rooms.update({"!old:hs": old, "!new:hs": new})
        session.state["last_event_ts"] = {"!old:hs": 100, "!new:hs": 200}
        dms = session.dashboard()["dms"]
        assert [e.room_id for e in dms] == ["!new:hs"]
        assert dms[0].unread == 5

    def test_dms_sorted_most_recent_first(self, session, fake_room):
        a = fake_room("!a:hs", users={ME: {}, ALICE: {}})
        b = fake_room("!b:hs", users={ME: {}, BOB: {}})
        session.client.rooms.update({"!a:hs": a, "!b:hs": b})
        session.state["last_event_ts"] = {"!a:hs": 100, "!b:hs": 200}
        dms = session.dashboard()["dms"]
        assert [e.room_id for e in dms] == ["!b:hs", "!a:hs"]

    def test_favourites_exclude_spaces(self, session, fake_room):
        fav = fake_room("!f:hs", tags={"m.favourite": {}})
        space = fake_room("!s:hs", room_type="m.space", tags={"m.favourite": {}})
        plain = fake_room("!p:hs")
        session.client.rooms.update(
            {"!f:hs": fav, "!s:hs": space, "!p:hs": plain}
        )
        favs = session.dashboard()["favourites"]
        assert [e.room_id for e in favs] == ["!f:hs"]

    def test_space_rooms_union_children_and_parents(self, session, fake_room):
        space = fake_room("!s:hs", room_type="m.space", children={"!child:hs"})
        child = fake_room("!child:hs")
        viaparent = fake_room("!via:hs", parents={"!s:hs"})
        dm = fake_room("!dm:hs", users={ME: {}, ALICE: {}}, parents={"!s:hs"})
        session.client.rooms.update(
            {
                "!s:hs": space,
                "!child:hs": child,
                "!via:hs": viaparent,
                "!dm:hs": dm,
            }
        )
        rooms = session.dashboard("!s:hs")["space_rooms"]
        assert {e.room_id for e in rooms} == {"!child:hs", "!via:hs"}

    def test_space_rooms_include_hierarchy_fallback(self, session, fake_room):
        # A server can publish malformed m.space.child events (e.g. via as a
        # string), which nio drops, so neither children nor parents carry the
        # link; the /hierarchy fetch cached in space_children must fill in.
        space = fake_room("!s:hs", room_type="m.space")
        orphan = fake_room("!orphan:hs")
        session.client.rooms.update({"!s:hs": space, "!orphan:hs": orphan})
        session.space_children["!s:hs"] = {"!orphan:hs", "!notjoined:hs"}
        rooms = session.dashboard("!s:hs")["space_rooms"]
        assert {e.room_id for e in rooms} == {"!orphan:hs"}

    def test_others_lists_rooms_outside_every_space(self, session, fake_room):
        # In a space via each of the three link kinds: none of these are
        # orphans. The space itself, DMs, and never-linked rooms are judged
        # on their own rules.
        space = fake_room("!s:hs", room_type="m.space", children={"!child:hs"})
        child = fake_room("!child:hs")
        viaparent = fake_room("!via:hs", parents={"!s:hs"})
        fallback = fake_room("!fb:hs")
        dm = fake_room("!dm:hs", users={ME: {}, ALICE: {}})
        orphan = fake_room("!weoi:hs")
        # A parent pointer at a space we are not joined to does not rescue a
        # room: it would still be unreachable from the Spaces column.
        stray = fake_room("!stray:hs", parents={"!unjoined:hs"})
        session.client.rooms.update(
            {
                "!s:hs": space,
                "!child:hs": child,
                "!via:hs": viaparent,
                "!fb:hs": fallback,
                "!dm:hs": dm,
                "!weoi:hs": orphan,
                "!stray:hs": stray,
            }
        )
        session.space_children["!s:hs"] = {"!fb:hs"}
        others = session.dashboard()["others"]
        assert {e.room_id for e in others} == {"!weoi:hs", "!stray:hs"}

    def test_recent_lists_last_opened_first_capped_at_five(
        self, session, fake_room
    ):
        for i in range(7):
            rid = f"!r{i}:hs"
            session.client.rooms[rid] = fake_room(rid, display_name=f"room{i}")
            session.state["last_opened_ts"][rid] = 100 + i
        session.client.rooms["!s:hs"] = fake_room("!s:hs", room_type="m.space")
        session.state["last_opened_ts"]["!s:hs"] = 1000
        session.client.rooms["!never:hs"] = fake_room("!never:hs")
        recent = session.dashboard()["recent"]
        assert [e.room_id for e in recent] == [
            "!r6:hs", "!r5:hs", "!r4:hs", "!r3:hs", "!r2:hs"
        ]


class TestRecordRoomTimestamps:
    def test_records_ts_and_event_id_and_persists(self, session, fake_room):
        resp = SimpleNamespace(
            rooms=SimpleNamespace(
                join={
                    "!a:hs": SimpleNamespace(
                        timeline=SimpleNamespace(
                            events=[
                                SimpleNamespace(server_timestamp=100, event_id="$1"),
                                SimpleNamespace(server_timestamp=300, event_id="$2"),
                            ]
                        )
                    )
                }
            )
        )
        session._record_room_timestamps(resp)
        assert session.state["last_event_ts"]["!a:hs"] == 300
        assert session.last_event_id["!a:hs"] == "$2"
        assert session.cfg.load_state()["last_event_ts"]["!a:hs"] == 300

    def test_older_ts_does_not_regress_but_event_id_follows_stream(self, session):
        session.state["last_event_ts"]["!a:hs"] = 500
        session.last_event_id["!a:hs"] = "$new"
        resp = SimpleNamespace(
            rooms=SimpleNamespace(
                join={
                    "!a:hs": SimpleNamespace(
                        timeline=SimpleNamespace(
                            events=[
                                SimpleNamespace(server_timestamp=100, event_id="$skewed")
                            ]
                        )
                    )
                }
            )
        )
        session._record_room_timestamps(resp)
        # The recency timestamp never goes backwards, but the latest event id
        # follows sync stream order even when the sender's clock lags: a
        # freshly synced event is newer regardless of its server_timestamp.
        assert session.state["last_event_ts"]["!a:hs"] == 500
        assert session.last_event_id["!a:hs"] == "$skewed"


class TestFindRoom:
    def test_by_id_and_alias(self, session, fake_room):
        room = fake_room("!a:hs", canonical_alias="#general:hs")
        session.client.rooms["!a:hs"] = room
        assert session.find_room("!a:hs").room_id == "!a:hs"
        assert session.find_room("#general:hs").room_id == "!a:hs"
        assert session.find_room("#nope:hs") is None


class TestSearch:
    def test_matches_title_person_and_id(self, session, fake_room):
        dm = fake_room("!dm:hs", users={ME: {}, ALICE: {}}, names={ALICE: "Alice"})
        session.direct_by_room["!dm:hs"] = ALICE
        group = fake_room("!g:hs", display_name="general chat")
        session.client.rooms.update({"!dm:hs": dm, "!g:hs": group})
        assert [e.room_id for e in session.search("alice")] == ["!dm:hs"]
        assert [e.room_id for e in session.search("general")] == ["!g:hs"]
        assert [e.room_id for e in session.search("!dm")] == ["!dm:hs"]
        assert session.search("   ") == []

    def test_accent_insensitive(self, session, fake_room):
        agnes = fake_room(
            "!a:hs", users={ME: {}, ALICE: {}}, names={ALICE: "IC_\u00c1gnes_N\u00e9meth"}
        )
        session.direct_by_room["!a:hs"] = ALICE
        mares = fake_room("!m:hs", display_name="ITC-Martin Mare\u0161")
        session.client.rooms.update({"!a:hs": agnes, "!m:hs": mares})
        assert [e.room_id for e in session.search("agnes")] == ["!a:hs"]
        assert [e.room_id for e in session.search("nemeth")] == ["!a:hs"]
        assert [e.room_id for e in session.search("mares")] == ["!m:hs"]
        assert [e.room_id for e in session.search("\u00c1gnes")] == ["!a:hs"]


class TestSanitize:
    def test_clean_strips_escape_and_bidi_keeps_newline_tab(self):
        from matrixcli.client import _clean

        assert "\x1b" not in _clean("a\x1b]52;c;x\x1b\\b")
        assert _clean("a\x7f\x9bb") == "ab"
        assert _clean("a‮b⁦c") == "abc"
        assert _clean("a\nb\tc") == "a\nb\tc"
        assert _clean(None) == ""

    def test_message_body_and_sender_are_sanitized(self, session, fake_room):
        room = fake_room("!a:hs", names={ALICE: "Ali\x1b[31mce"})
        session.client.rooms["!a:hs"] = room
        ev = text_event("$1", ALICE, 100, "hi\x1b[2Jthere")
        asyncio.run(session._on_message(room, ev))
        msg = session.timelines["!a:hs"][-1]
        assert "\x1b" not in msg.body
        assert "\x1b" not in msg.sender_name

    def test_entry_title_is_sanitized(self, session, fake_room):
        room = fake_room("!r:hs", display_name="Room\x1b]0;pwned\x07", room_type=None)
        e = session._entry(room)
        assert "\x1b" not in e.title


class TestSasEmoji:
    """Regression guard for the nio-vs-vodozemac SAS emoji bug: vodozemac's
    emoji_indices are the seven final SAS indices and must map straight onto the
    emoji table. nio's own get_emoji() re-packs them and scrambles the result,
    so _sas_emoji bypasses it. If a future nio bump 'fixes' get_emoji upstream,
    this stays correct because it never calls get_emoji at all."""

    def _fake_sas(self, indices):
        return SimpleNamespace(
            _extra_info="MATRIX_KEY_VERIFICATION_SAS|...",
            established_sas=SimpleNamespace(
                bytes=lambda info: SimpleNamespace(emoji_indices=list(indices))
            ),
        )

    def test_maps_indices_directly_to_emoji_table(self):
        from matrixcli.client import _sas_emoji

        # These indices are a real SAS run that matched Element (Tree, Flower,
        # Panda, Rocket, Apple, Octopus, Train).
        emoji = _sas_emoji(self._fake_sas([16, 15, 8, 54, 24, 13, 51]))
        assert [name for _, name in emoji] == [
            "Tree",
            "Flower",
            "Panda",
            "Rocket",
            "Apple",
            "Octopus",
            "Train",
        ]

    def test_does_not_reinterpret_indices_as_bytes(self):
        # nio's buggy get_emoji() would re-pack these indices into 8-bit binary
        # and regroup into 6-bit chunks, yielding a different sequence. Assert we
        # do NOT reproduce that scrambling for a simple, distinctive input.
        from matrixcli.client import _sas_emoji
        from nio.crypto.sas import Sas

        indices = [1, 2, 3, 4, 5, 6, 7]
        emoji = _sas_emoji(self._fake_sas(indices))
        assert [g for g, _ in emoji] == [Sas.emoji[i][0] for i in indices]


class TestLoadHistory:
    def run(self, session, room_id="!a:hs", limit=10, chunk=()):
        async def fake_room_messages(*args, **kwargs):
            return SimpleNamespace(chunk=list(chunk))

        session.client.room_messages = fake_room_messages
        return asyncio.run(session.load_history(room_id, limit=limit))

    def test_member_fetch_runs_in_background(self, session, fake_room):
        # Startup syncs members lazily; the first open of a room kicks off the
        # member fetch so older senders get display names. On a big room
        # /joined_members takes seconds server-side, so the history must
        # return WITHOUT waiting for it (awaiting it froze every first open);
        # when it lands, cached sender names re-resolve and the UI is told.
        room = fake_room("!a:hs")
        room.members_synced = False
        session.client.rooms["!a:hs"] = room
        calls = []
        repainted = []
        session.on_members_loaded = repainted.append

        async def main():
            release = asyncio.Event()

            async def fake_joined_members(room_id):
                calls.append(room_id)
                await release.wait()
                room._names[ALICE] = "Alice"
                room.members_synced = True
                return SimpleNamespace()

            async def fake_room_messages(*args, **kwargs):
                return SimpleNamespace(chunk=[text_event("$1", ALICE, 100, "hi")])

            session.client.joined_members = fake_joined_members
            session.client.room_messages = fake_room_messages
            msgs = await session.load_history("!a:hs", limit=10)
            # History arrived while the member fetch was still blocked.
            assert [m.event_id for m in msgs] == ["$1"]
            assert [m.sender_name for m in msgs] == [ALICE]
            release.set()
            for _ in range(5):
                await asyncio.sleep(0)

        asyncio.run(main())
        assert calls == ["!a:hs"]
        assert [m.sender_name for m in session.timelines["!a:hs"]] == ["Alice"]
        assert repainted == ["!a:hs"]

    def test_quiet_room_absent_from_rooms_map_still_gets_names(self, session):
        # A resumed (incremental) launch never mentions a room without fresh
        # activity, so it is absent from client.rooms entirely. Opening one
        # used to skip the member fetch (and nio would drop the /joined_members
        # response for an unknown room anyway), leaving every sender a raw
        # @user:server id forever. load_history must register the room itself
        # so the background fetch runs and the names resolve.
        repainted = []
        session.on_members_loaded = repainted.append

        async def main():
            async def fake_joined_members(room_id):
                room = session.client.rooms[room_id]
                room.add_member(ALICE, "Alice", None)
                room.members_synced = True
                return SimpleNamespace()

            async def fake_room_messages(*args, **kwargs):
                return SimpleNamespace(chunk=[text_event("$1", ALICE, 100, "hi")])

            session.client.joined_members = fake_joined_members
            session.client.room_messages = fake_room_messages
            await session.load_history("!quiet:hs", limit=10)
            for _ in range(5):
                await asyncio.sleep(0)

        asyncio.run(main())
        assert "!quiet:hs" in session.client.rooms
        assert [m.sender_name for m in session.timelines["!quiet:hs"]] == ["Alice"]
        assert repainted == ["!quiet:hs"]

    def test_failed_member_fetch_only_costs_names(self, session, fake_room):
        room = fake_room("!a:hs")
        room.members_synced = False
        session.client.rooms["!a:hs"] = room
        repainted = []
        session.on_members_loaded = repainted.append

        async def main():
            async def fake_joined_members(room_id):
                raise RuntimeError("server hiccup")

            async def fake_room_messages(*args, **kwargs):
                return SimpleNamespace(chunk=[text_event("$1", ALICE, 100, "hi")])

            session.client.joined_members = fake_joined_members
            session.client.room_messages = fake_room_messages
            msgs = await session.load_history("!a:hs", limit=10)
            for _ in range(5):
                await asyncio.sleep(0)
            return msgs

        msgs = asyncio.run(main())
        assert [m.event_id for m in msgs] == ["$1"]
        assert repainted == []
        # The in-flight guard was released, so a later open can retry.
        assert "!a:hs" not in session._member_fetches

    def test_reloads_serve_the_cache_after_the_first_fetch(self, session):
        # A room smaller than the window used to refetch /messages on every
        # reload (each one seconds on a slow homeserver); once the initial
        # window has been merged, the sync-fed cache is authoritative.
        calls = []

        async def fake_room_messages(*args, **kwargs):
            calls.append(1)
            return SimpleNamespace(chunk=[text_event("$1", ALICE, 100, "hi")])

        session.client.room_messages = fake_room_messages
        first = asyncio.run(session.load_history("!a:hs", limit=10))
        again = asyncio.run(session.load_history("!a:hs", limit=10))
        assert [m.event_id for m in first] == ["$1"]
        assert [m.event_id for m in again] == ["$1"]
        assert len(calls) == 1

    def test_gappy_sync_refetches_even_with_a_full_cache(self, session):
        # A limited sync only discarded history_loaded, but the serve-from-
        # cache size shortcut ran first: a room with >= limit cached messages
        # kept serving the cache with the skipped chunk silently missing.
        calls = []

        async def fake_room_messages(*args, **kwargs):
            calls.append(1)
            return SimpleNamespace(chunk=[text_event("$s", ALICE, 5000, "srv")])

        session.client.room_messages = fake_room_messages
        for i in range(12):
            session.timelines["!a:hs"].append(
                Message(
                    sender=ALICE,
                    sender_name="Alice",
                    body=f"m{i}",
                    ts=i + 1,
                    event_id=f"${i}",
                )
            )
        gappy = SimpleNamespace(
            rooms=SimpleNamespace(
                join={
                    "!a:hs": SimpleNamespace(
                        timeline=SimpleNamespace(events=[], limited=True)
                    )
                }
            )
        )
        session._record_room_timestamps(gappy)
        asyncio.run(session.load_history("!a:hs", limit=10))
        assert len(calls) == 1

    def test_gap_landing_mid_fetch_leaves_the_room_unloaded(self, session):
        # The fetch was anchored at the pre-gap sync token, so a limited sync
        # completing during the await means the merged window cannot contain
        # the skipped events; marking the room history_loaded anyway would
        # declare the hole filled and never refetch.
        async def main():
            async def fake_room_messages(*args, **kwargs):
                gappy = SimpleNamespace(
                    rooms=SimpleNamespace(
                        join={
                            "!a:hs": SimpleNamespace(
                                timeline=SimpleNamespace(events=[], limited=True)
                            )
                        }
                    )
                )
                session._record_room_timestamps(gappy)
                return SimpleNamespace(chunk=[text_event("$1", ALICE, 100, "hi")])

            session.client.room_messages = fake_room_messages
            await session.load_history("!a:hs", limit=10)

        asyncio.run(main())
        assert "!a:hs" not in session.history_loaded

    def test_decrypted_fetch_beats_cached_placeholder(self, session):
        # Keys that arrive mid-session (live key-share after verification)
        # let a refetch decrypt what an earlier fetch could not; the cached
        # "[encrypted: ...]" placeholder must not win the merge over it.
        session.timelines["!a:hs"].append(
            Message(
                sender=ALICE,
                sender_name="Alice",
                body="[encrypted: no key for this message]",
                ts=100,
                event_id="$1",
            )
        )
        async def fake_room_messages(*args, **kwargs):
            return SimpleNamespace(chunk=[text_event("$1", ALICE, 100, "secret")])

        session.client.room_messages = fake_room_messages
        msgs = asyncio.run(session.load_history("!a:hs", limit=10))
        assert [m.body for m in msgs] == ["secret"]

    def test_gappy_sync_forces_a_refetch(self, session):
        # A "limited" sync timeline means events were skipped: the cache is
        # missing a chunk, so serving it would show history with a hole.
        calls = []

        async def fake_room_messages(*args, **kwargs):
            calls.append(1)
            return SimpleNamespace(chunk=[text_event("$1", ALICE, 100, "hi")])

        session.client.room_messages = fake_room_messages
        asyncio.run(session.load_history("!a:hs", limit=10))
        gappy = SimpleNamespace(
            rooms=SimpleNamespace(
                join={
                    "!a:hs": SimpleNamespace(
                        timeline=SimpleNamespace(events=[], limited=True)
                    )
                }
            )
        )
        session._record_room_timestamps(gappy)
        asyncio.run(session.load_history("!a:hs", limit=10))
        assert len(calls) == 2

    def test_cached_only_never_touches_the_network(self, session):
        async def fake_room_messages(*args, **kwargs):
            raise AssertionError("cached_only must not fetch")

        session.client.room_messages = fake_room_messages
        assert asyncio.run(session.load_history("!a:hs", cached_only=True)) == []
        session.timelines["!a:hs"].append(
            Message(sender=ALICE, sender_name="Alice", body="hi", ts=100, event_id="$1")
        )
        msgs = asyncio.run(session.load_history("!a:hs", cached_only=True))
        assert [m.event_id for m in msgs] == ["$1"]

    def test_merges_on_event_id_and_keeps_same_timestamp_messages(self, session):
        # Server returns newest-first; two distinct events share ts=100.
        chunk = [
            text_event("$3", BOB, 200, "newest"),
            text_event("$2", ALICE, 100, "same ts"),
            text_event("$1", BOB, 100, "also same ts"),
        ]
        msgs = self.run(session, chunk=chunk)
        assert [m.event_id for m in msgs] == ["$1", "$2", "$3"]
        assert [m.body for m in msgs] == ["also same ts", "same ts", "newest"]

    def test_cached_version_wins_over_fetched(self, session):
        cached = Message(
            sender=ALICE,
            sender_name="Alice",
            body="decrypted",
            ts=100,
            event_id="$1",
        )
        session.timelines["!a:hs"].append(cached)
        chunk = [text_event("$1", ALICE, 100, "from server")]
        msgs = self.run(session, chunk=chunk)
        assert [m.body for m in msgs] == ["decrypted"]

    def test_merged_history_is_written_back_to_cache(self, session):
        chunk = [text_event("$1", ALICE, 100, "hi")]
        self.run(session, chunk=chunk)
        assert [m.event_id for m in session.timelines["!a:hs"]] == ["$1"]

    def test_thread_metadata_is_extracted(self, session):
        chunk = [
            text_event(
                "$reply",
                ALICE,
                200,
                "in thread",
                relates={"rel_type": "m.thread", "event_id": "$root"},
            ),
            text_event(
                "$root",
                BOB,
                100,
                "root",
                unsigned={"m.relations": {"m.thread": {"count": 7}}},
            ),
        ]
        msgs = self.run(session, chunk=chunk)
        by_id = {m.event_id: m for m in msgs}
        assert by_id["$reply"].thread_root == "$root"
        assert by_id["$root"].thread_root == ""
        assert by_id["$root"].thread_count == 7

    def test_reply_fallback_is_stripped_and_quoted(self, session, fake_room):
        session.client.rooms["!a:hs"] = fake_room(
            "!a:hs", names={ALICE: "Alice"}
        )
        chunk = [
            text_event(
                "$reply",
                BOB,
                200,
                "> <@alice:example.org> original text\n"
                "> second quoted line\n"
                "\n"
                "the actual reply",
                relates={"m.in_reply_to": {"event_id": "$orig"}},
            ),
        ]
        m = self.run(session, chunk=chunk)[0]
        assert m.body == "the actual reply"
        assert m.reply_to == "$orig"
        assert m.reply_name == "Alice"
        assert m.reply_snippet == "original text"

    def test_reply_without_fallback_keeps_body_and_target(self, session):
        chunk = [
            text_event(
                "$reply",
                ALICE,
                200,
                "just the reply",
                relates={"m.in_reply_to": {"event_id": "$orig"}},
            )
        ]
        m = self.run(session, chunk=chunk)[0]
        assert m.body == "just the reply"
        assert m.reply_to == "$orig"
        assert m.reply_name == ""
        assert m.reply_snippet == ""

    def test_thread_falling_back_pointer_is_not_a_reply(self, session):
        chunk = [
            text_event(
                "$t",
                ALICE,
                200,
                "in thread",
                relates={
                    "rel_type": "m.thread",
                    "event_id": "$root",
                    "is_falling_back": True,
                    "m.in_reply_to": {"event_id": "$latest"},
                },
            )
        ]
        m = self.run(session, chunk=chunk)[0]
        assert m.reply_to == ""
        assert m.body == "in thread"

    def test_explicit_thread_reply_is_a_reply(self, session):
        chunk = [
            text_event(
                "$t",
                ALICE,
                200,
                "answering you",
                relates={
                    "rel_type": "m.thread",
                    "event_id": "$root",
                    "is_falling_back": False,
                    "m.in_reply_to": {"event_id": "$target"},
                },
            )
        ]
        m = self.run(session, chunk=chunk)[0]
        assert m.reply_to == "$target"

    def test_quoted_own_mxid_in_fallback_is_not_a_mention(self, session):
        # Before the fallback was stripped, our own user id inside the quoted
        # block made every reply-to-us light up as a mention.
        chunk = [
            text_event(
                "$reply",
                ALICE,
                200,
                f"> <{ME}> what I said\n\nagreed",
                relates={"m.in_reply_to": {"event_id": "$mine"}},
            )
        ]
        m = self.run(session, chunk=chunk)[0]
        assert m.body == "agreed"
        assert not m.mentions_me

    def test_media_metadata_is_extracted(self, session):
        from nio.events.room_events import Event

        ev = Event.parse_event(
            {
                "type": "m.room.message",
                "event_id": "$img",
                "sender": ALICE,
                "origin_server_ts": 100,
                "room_id": "!a:hs",
                "content": {
                    "msgtype": "m.image",
                    "body": "photo.jpg",
                    "url": "mxc://hs/abc",
                    "info": {"size": 2048, "mimetype": "image/jpeg"},
                },
            }
        )
        msgs = self.run(session, chunk=[ev])
        m = msgs[0]
        assert m.media_url == "mxc://hs/abc"
        assert m.media_name == "photo.jpg"
        assert m.media_size == 2048
        assert m.media_crypt is None

    def test_short_circuits_on_full_cache(self, session):
        for i in range(5):
            session.timelines["!a:hs"].append(
                Message(sender=ALICE, sender_name="A", body=str(i), ts=i, event_id=f"${i}")
            )

        async def boom(*args, **kwargs):
            raise AssertionError("should not hit the network")

        session.client.room_messages = boom
        msgs = asyncio.run(session.load_history("!a:hs", limit=5))
        assert [m.body for m in msgs] == ["0", "1", "2", "3", "4"]


def reaction_event(event_id, sender, ts, target):
    from nio.events.room_events import Event

    return Event.parse_event(
        {
            "type": "m.reaction",
            "event_id": event_id,
            "sender": sender,
            "origin_server_ts": ts,
            "content": {
                "m.relates_to": {
                    "rel_type": "m.annotation",
                    "event_id": target,
                    "key": "👍",
                }
            },
        }
    )


class TestLoadHistoryStarvedWindow:
    """A raw /messages window says nothing about how many rows it paints:
    reactions and redactions never become Messages, and thread replies
    collapse out of the normal view. A reaction/thread flood (a busy room
    during a live event) used to leave the first window with zero
    main-timeline messages and the room rendered as "(no messages yet)"."""

    def serve(self, session, windows):
        """Serve each (chunk, end) in turn and record the start tokens."""
        calls = []

        async def fake_room_messages(room_id, start, direction, limit):
            calls.append(start)
            chunk, end = windows[min(len(calls) - 1, len(windows) - 1)]
            return SimpleNamespace(chunk=list(chunk), end=end)

        session.client.room_messages = fake_room_messages
        return calls

    def test_reaction_flood_paginates_to_real_messages(self, session):
        flood = [
            reaction_event(f"$r{i}", ALICE, 300 - i, "$old") for i in range(10)
        ]
        older = [
            text_event(f"${i}", ALICE, 200 - i, f"msg {i}") for i in range(10)
        ]
        calls = self.serve(session, [(flood, "t1"), (older, "t2")])
        msgs = asyncio.run(session.load_history("!a:hs", limit=10))
        assert calls == ["", "t1"]
        assert len(msgs) == 10
        assert all(m.body.startswith("msg") for m in msgs)
        # load_older must continue from the deepest window consumed, not
        # refetch the flood.
        assert session.pagination_tokens["!a:hs"] == "t2"

    def test_thread_heavy_tail_extends_the_returned_slice(self, session):
        threads = [
            text_event(
                f"$t{i}",
                ALICE,
                300 - i,
                f"reply {i}",
                relates={"rel_type": "m.thread", "event_id": "$root"},
            )
            for i in range(7)
        ]
        first = threads + [
            text_event(f"$m{i}", BOB, 250 - i, f"main {i}") for i in range(3)
        ]
        older = [
            text_event(f"$o{i}", BOB, 200 - i, f"older {i}") for i in range(5)
        ]
        self.serve(session, [(first, "t1"), (older, "t2")])
        msgs = asyncio.run(session.load_history("!a:hs", limit=10))
        # The slice grows past `limit` entries until it holds limit // 2
        # main-timeline messages; the thread replies ride along inside it.
        assert sum(1 for m in msgs if not m.thread_root) == 5
        assert sum(1 for m in msgs if m.thread_root) == 7

    def test_quiet_room_still_costs_one_round_trip(self, session):
        chunk = [text_event(f"${i}", ALICE, 200 - i, f"m{i}") for i in range(6)]
        calls = self.serve(session, [(chunk, "t1")])
        msgs = asyncio.run(session.load_history("!a:hs", limit=10))
        assert len(calls) == 1
        assert len(msgs) == 6

    def test_pagination_stops_at_start_of_history(self, session):
        flood = [
            reaction_event(f"$r{i}", ALICE, 300 - i, "$old") for i in range(10)
        ]
        calls = self.serve(session, [(flood, "t1"), ([], None)])
        msgs = asyncio.run(session.load_history("!a:hs", limit=10))
        assert calls == ["", "t1"]
        assert msgs == []
        assert session.pagination_done["!a:hs"]

    def test_error_mid_pagination_keeps_the_windows_that_arrived(self, session):
        from nio.responses import RoomMessagesError

        chunk = [text_event("$1", ALICE, 100, "hi")]
        calls = []

        async def fake_room_messages(room_id, start, direction, limit):
            calls.append(start)
            if len(calls) == 1:
                return SimpleNamespace(chunk=list(chunk), end="t1")
            return RoomMessagesError("boom")

        session.client.room_messages = fake_room_messages
        msgs = asyncio.run(session.load_history("!a:hs", limit=10))
        assert [m.event_id for m in msgs] == ["$1"]
        assert len(calls) == 2

    def test_thread_only_cache_does_not_short_circuit(self, session):
        # A cache holding `limit` messages used to satisfy any reload, even
        # when every one of them is a thread reply and the main view would
        # still paint empty.
        for i in range(10):
            session.timelines["!a:hs"].append(
                Message(
                    sender=ALICE,
                    sender_name="A",
                    body=f"reply {i}",
                    ts=100 + i,
                    event_id=f"$t{i}",
                    thread_root="$root",
                )
            )
        chunk = [text_event(f"$m{i}", BOB, 90 - i, f"main {i}") for i in range(6)]
        calls = self.serve(session, [(chunk, "t1")])
        msgs = asyncio.run(session.load_history("!a:hs", limit=10))
        assert len(calls) == 1
        # The slice walks back to limit // 2 main-timeline messages.
        assert sum(1 for m in msgs if not m.thread_root) == 5

    def test_floor_unreachable_returns_everything_held(self, session):
        # A room whose entire history holds fewer main-timeline messages
        # than the floor must return what exists, not loop or come back
        # empty.
        chunk = [text_event(f"${i}", ALICE, 200 - i, f"m{i}") for i in range(3)]
        calls = self.serve(session, [(chunk, "t1"), ([], None)])
        msgs = asyncio.run(session.load_history("!a:hs", limit=10))
        assert calls == ["", "t1"]
        assert [m.body for m in msgs] == ["m2", "m1", "m0"]
        assert session.pagination_done["!a:hs"]

    def test_pagination_is_capped_at_timeline_cap_raw_events(self, session):
        from matrixcli.client import TIMELINE_CAP

        # A pathological room that never yields a main-timeline message
        # (endless reactions) must stop at the raw-event budget, not walk
        # history forever.
        flood = [
            reaction_event(f"$r{i}", ALICE, 300 - i, "$old") for i in range(10)
        ]
        calls = self.serve(session, [(flood, "next")])
        msgs = asyncio.run(session.load_history("!a:hs", limit=10))
        assert len(calls) == TIMELINE_CAP // 10
        assert msgs == []

    def test_starved_cached_only_paint_serves_the_whole_cache(self, session):
        for i in range(3):
            session.timelines["!a:hs"].append(
                Message(
                    sender=BOB,
                    sender_name="B",
                    body=f"main {i}",
                    ts=50 + i,
                    event_id=f"$m{i}",
                )
            )
        for i in range(10):
            session.timelines["!a:hs"].append(
                Message(
                    sender=ALICE,
                    sender_name="A",
                    body=f"reply {i}",
                    ts=100 + i,
                    event_id=f"$t{i}",
                    thread_root="$root",
                )
            )

        async def boom(*args, **kwargs):
            raise AssertionError("cached_only must not fetch")

        session.client.room_messages = boom
        msgs = asyncio.run(session.load_history("!a:hs", limit=10, cached_only=True))
        # A plain [-limit:] tail would be thread replies only; the slice
        # keeps reaching back, here to the whole cache.
        assert len(msgs) == 13
        assert sum(1 for m in msgs if not m.thread_root) == 3


class TestThreads:
    def test_send_attaches_thread_relation(self, session):
        sent = {}

        async def fake_room_send(room_id, message_type, content, **kwargs):
            sent["content"] = content
            return SimpleNamespace(event_id="$new")

        session.client.room_send = fake_room_send
        ok, event_id = asyncio.run(
            session.send(
                "!a:hs", "hi", thread_root="$root", thread_latest="$last"
            )
        )
        assert ok and event_id == "$new"
        rel = sent["content"]["m.relates_to"]
        assert rel["rel_type"] == "m.thread"
        assert rel["event_id"] == "$root"
        assert rel["is_falling_back"] is True
        assert rel["m.in_reply_to"] == {"event_id": "$last"}
        # The local echo carries the thread root so the main timeline can
        # keep it collapsed.
        echo = list(session.timelines["!a:hs"])[-1]
        assert echo.event_id == "$new" and echo.thread_root == "$root"

    def test_explicit_reply_in_thread_clears_fallback(self, session):
        sent = {}

        async def fake_room_send(room_id, message_type, content, **kwargs):
            sent["content"] = content
            return SimpleNamespace(event_id="$new")

        session.client.room_send = fake_room_send
        asyncio.run(
            session.send(
                "!a:hs", "hi", reply_to="$target", thread_root="$root"
            )
        )
        rel = sent["content"]["m.relates_to"]
        assert rel["is_falling_back"] is False
        assert rel["m.in_reply_to"] == {"event_id": "$target"}

    def test_load_thread_falls_back_to_cache_when_offline(
        self, session, monkeypatch
    ):
        class Boom:
            def __init__(self, *args, **kwargs):
                raise aiohttp.ClientError("no network")

        monkeypatch.setattr("matrixcli.client.aiohttp.ClientSession", Boom)
        root = Message(
            sender=ALICE, sender_name="A", body="root", ts=100, event_id="$root"
        )
        for i, (eid, troot) in enumerate(
            [("$r1", "$root"), ("$other", "$elsewhere"), ("$r2", "$root")]
        ):
            session.timelines["!a:hs"].append(
                Message(
                    sender=BOB,
                    sender_name="B",
                    body=f"m{i}",
                    ts=200 + i,
                    event_id=eid,
                    thread_root=troot,
                )
            )
        msgs = asyncio.run(session.load_thread("!a:hs", root))
        assert [m.event_id for m in msgs] == ["$root", "$r1", "$r2"]

    def _root(self):
        return Message(
            sender=ALICE, sender_name="A", body="root", ts=100, event_id="$root"
        )

    def test_thread_merge_takes_the_servers_edit_fold_over_a_stale_cache(
        self, session, monkeypatch
    ):
        # The reply was edited; the /relations fetch returns it with the
        # bundled edit already folded in, but the cache still holds the
        # pre-edit copy. Cached must win on identity (decryption) without
        # reverting the text the whole rest of the app shows.
        reply_source = {
            "type": "m.room.message",
            "event_id": "$r1",
            "sender": BOB,
            "origin_server_ts": 200,
            "content": {
                "msgtype": "m.text",
                "body": "old text",
                "m.relates_to": {"rel_type": "m.thread", "event_id": "$root"},
            },
            "unsigned": {
                "m.relations": {
                    "m.replace": edit_source("$e", BOB, 300, "new text", "$r1")
                }
            },
        }
        fake = TestRefreshSpaceChildren.FakeHttp({"chunk": [reply_source]})
        monkeypatch.setattr(
            "matrixcli.client.aiohttp.ClientSession", lambda **kw: fake
        )
        session.timelines["!a:hs"].append(
            Message(
                sender=BOB, sender_name="B", body="old text", ts=200,
                event_id="$r1", thread_root="$root",
            )
        )
        msgs = asyncio.run(session.load_thread("!a:hs", self._root()))
        (reply,) = [m for m in msgs if m.event_id == "$r1"]
        assert reply.body == "new text"
        assert reply.edited_ts == 300
        assert reply.original_body == "old text"

    def test_thread_includes_cached_live_edits_so_the_screen_can_fold_them(
        self, session, monkeypatch
    ):
        # An edit received live sits in the cache with an m.replace relation
        # and no thread root; /relations m.thread never returns it. It must
        # ride along, or the thread view shows the reply's pre-edit text.
        class Boom:
            def __init__(self, *args, **kwargs):
                raise aiohttp.ClientError("no network")

        monkeypatch.setattr("matrixcli.client.aiohttp.ClientSession", Boom)
        session.timelines["!a:hs"].extend(
            [
                Message(
                    sender=BOB, sender_name="B", body="old", ts=200,
                    event_id="$r1", thread_root="$root",
                ),
                Message(
                    sender=BOB, sender_name="B", body="new", ts=300,
                    event_id="$e", replaces="$r1",
                ),
            ]
        )
        msgs = asyncio.run(session.load_thread("!a:hs", self._root()))
        assert [m.event_id for m in msgs] == ["$root", "$r1", "$e"]
        folded = fold_edits(msgs)
        assert [(m.event_id, m.body) for m in folded] == [
            ("$root", "root"),
            ("$r1", "new"),
        ]

    def test_thread_merge_carries_a_deletion_the_cache_missed(
        self, session, monkeypatch
    ):
        reply_source = {
            "type": "m.room.message",
            "event_id": "$r1",
            "sender": BOB,
            "origin_server_ts": 200,
            "content": {},
            "unsigned": {
                "redacted_because": {
                    "type": "m.room.redaction",
                    "event_id": "$del",
                    "sender": BOB,
                    "origin_server_ts": 400,
                    "redacts": "$r1",
                    "content": {},
                }
            },
        }
        fake = TestRefreshSpaceChildren.FakeHttp({"chunk": [reply_source]})
        monkeypatch.setattr(
            "matrixcli.client.aiohttp.ClientSession", lambda **kw: fake
        )
        session.timelines["!a:hs"].append(
            Message(
                sender=BOB, sender_name="B", body="kept text", ts=200,
                event_id="$r1", thread_root="$root",
            )
        )
        msgs = asyncio.run(session.load_thread("!a:hs", self._root()))
        (reply,) = [m for m in msgs if m.event_id == "$r1"]
        # Deletion learned from the server, text kept from the cache.
        assert reply.redacted_ts == 400
        assert reply.body == "kept text"


class TestToMessageEncrypted:
    def test_wrapper_thread_info_survives_failed_decrypt(self, session):
        # nio rebuilds decrypted events from the plaintext payload and drops
        # the wrapper's unsigned aggregation, so _to_message must read the
        # thread info from the wire-format event BEFORE decrypting. Here
        # decryption fails (no key), which must still yield a placeholder
        # message carrying the wrapper's thread root and server count.
        from nio.events.room_events import Event

        ev = Event.parse_event(
            {
                "type": "m.room.encrypted",
                "event_id": "$enc",
                "sender": ALICE,
                "origin_server_ts": 100,
                "room_id": "!a:hs",
                "content": {
                    "algorithm": "m.megolm.v1.aes-sha2",
                    "ciphertext": "xxx",
                    "device_id": "DEV",
                    "sender_key": "k",
                    "session_id": "s",
                    "m.relates_to": {"rel_type": "m.thread", "event_id": "$root"},
                },
                "unsigned": {"m.relations": {"m.thread": {"count": 5}}},
            }
        )
        m = session._to_message(None, ev)
        assert m is not None
        assert m.thread_root == "$root"
        assert m.thread_count == 5
        assert "encrypted" in m.body

    def test_messages_fetch_grafts_wrapper_unsigned_onto_decrypted(self, session):
        # nio's _handle_messages_response swaps decryptable events for their
        # decrypted forms IN the chunk, and those keep none of the wrapper's
        # unsigned (thread counts, bundled edits). The wrap installed in
        # _new_client must graft it back so _to_message still sees it.
        from nio.events.room_events import Event

        wrapper = Event.parse_event(
            {
                "type": "m.room.encrypted",
                "event_id": "$enc",
                "sender": ALICE,
                "origin_server_ts": 100,
                "room_id": "!a:hs",
                "content": {
                    "algorithm": "m.megolm.v1.aes-sha2",
                    "ciphertext": "xxx",
                    "device_id": "DEV",
                    "sender_key": "k",
                    "session_id": "s",
                },
                "unsigned": {"m.relations": {"m.thread": {"count": 7}}},
            }
        )
        decrypted = text_event(
            "$enc", ALICE, 100, "hi", unsigned={"transaction_id": "t1"}
        )
        session.client.olm = SimpleNamespace(
            _decrypt_megolm_no_error=lambda e: decrypted
        )
        resp = SimpleNamespace(chunk=[wrapper])
        session.client._handle_messages_response(resp)
        assert resp.chunk[0] is decrypted
        unsigned = decrypted.source["unsigned"]
        assert unsigned["m.relations"]["m.thread"]["count"] == 7
        # The decrypted event's own unsigned keys win on collision.
        assert unsigned["transaction_id"] == "t1"


class TestEdits:
    """m.replace edits: parsed out of the event, folded into the message they
    rewrite, and never trusted from anyone but the original sender."""

    def msg(self, event_id, sender, ts, body, **kw):
        return Message(
            sender=sender, sender_name=sender, body=body, ts=ts,
            event_id=event_id, **kw,
        )

    def test_new_content_is_preferred_over_the_star_fallback(self, session):
        m = session._to_message(None, edit_event("$e", ALICE, 200, "fixed", "$o"))
        assert m.replaces == "$o"
        assert m.body == "fixed"

    def test_star_fallback_is_stripped_when_new_content_is_missing(self, session):
        ev = text_event(
            "$e", ALICE, 200, "* fixed",
            relates={"rel_type": "m.replace", "event_id": "$o"},
        )
        assert session._to_message(None, ev).body == "fixed"

    def test_fold_rewrites_the_target_and_drops_the_edit(self):
        folded = fold_edits([
            self.msg("$o", ALICE, 100, "typo"),
            self.msg("$e", ALICE, 200, "fixed", replaces="$o"),
        ])
        assert [m.event_id for m in folded] == ["$o"]
        assert folded[0].body == "fixed"
        assert folded[0].original_body == "typo"
        assert folded[0].edited_ts == 200

    def test_fold_applies_only_the_newest_edit(self):
        folded = fold_edits([
            self.msg("$o", ALICE, 100, "v1"),
            self.msg("$e1", ALICE, 200, "v2", replaces="$o"),
            self.msg("$e2", ALICE, 300, "v3", replaces="$o"),
        ])
        assert [m.body for m in folded] == ["v3"]
        assert folded[0].original_body == "v1"

    def test_another_users_replace_is_not_folded_in(self):
        # Anyone may send an m.replace pointing at anyone's event; folding one
        # in would let a stranger rewrite what someone else said.
        folded = fold_edits([
            self.msg("$o", ALICE, 100, "as written"),
            self.msg("$e", BOB, 200, "as forged", replaces="$o"),
        ])
        assert [m.body for m in folded] == ["as written", "as forged"]
        assert folded[0].edited_ts == 0

    def test_edit_of_a_message_outside_the_window_stays_visible(self):
        folded = fold_edits([self.msg("$e", ALICE, 200, "fixed", replaces="$gone")])
        assert [m.body for m in folded] == ["fixed"]

    def test_a_deleted_edit_stops_applying(self):
        # Redacting an edit retracts it: the server un-applies it and a fresh
        # client shows the previous text. Ours must not keep displaying the
        # retracted body (which may be exactly what the sender deleted it for).
        folded = fold_edits([
            self.msg("$o", ALICE, 100, "as written"),
            self.msg("$e", ALICE, 200, "pasted secret", replaces="$o",
                     redacted_ts=300),
        ])
        assert [(m.event_id, m.body) for m in folded] == [("$o", "as written")]
        assert folded[0].edited_ts == 0

    def test_a_deleted_edit_falls_back_to_the_previous_edit(self):
        folded = fold_edits([
            self.msg("$o", ALICE, 100, "v1"),
            self.msg("$e1", ALICE, 200, "v2", replaces="$o"),
            self.msg("$e2", ALICE, 300, "v3", replaces="$o", redacted_ts=400),
        ])
        assert [m.body for m in folded] == ["v2"]
        assert folded[0].edited_ts == 200

    def test_a_retracted_bundled_fold_is_undone(self):
        # The server bundled the edit onto the original, so _to_message baked
        # the new text into the target; then the edit was deleted live. The
        # bake must revert to the original text.
        baked = Message(
            sender=ALICE, sender_name=ALICE, body="pasted secret", ts=100,
            event_id="$o", edited_ts=200, original_body="as written",
        )
        folded = fold_edits([
            baked,
            self.msg("$e", ALICE, 200, "pasted secret", replaces="$o",
                     redacted_ts=300),
        ])
        assert [(m.event_id, m.body) for m in folded] == [("$o", "as written")]
        assert folded[0].edited_ts == 0

    def test_undecryptable_edit_marks_but_does_not_replace(self):
        folded = fold_edits([
            self.msg("$o", ALICE, 100, "readable"),
            self.msg("$e", ALICE, 200, "[encrypted: no key]", replaces="$o"),
        ])
        assert folded[0].body == "readable"
        assert folded[0].edited_ts == 200

    def test_bundled_edit_applies_without_the_edit_event(self, session):
        # The server aggregates the newest edit onto the event it rewrites, so
        # an old message shows current text even when the edit is far outside
        # the fetched window.
        ev = text_event(
            "$o", ALICE, 100, "typo",
            unsigned={"m.relations": {"m.replace": edit_source("$e", ALICE, 200, "fixed", "$o")}},
        )
        m = session._to_message(None, ev)
        assert (m.body, m.original_body, m.edited_ts) == ("fixed", "typo", 200)
        # Folding the same edit in again from the window must not lose the
        # original text it already replaced.
        folded = fold_edits([m, self.msg("$e", ALICE, 200, "fixed", replaces="$o")])
        assert [m.event_id for m in folded] == ["$o"]
        assert (folded[0].body, folded[0].original_body) == ("fixed", "typo")

    def test_bundled_edit_from_another_sender_is_ignored(self, session):
        ev = text_event(
            "$o", ALICE, 100, "as written",
            unsigned={"m.relations": {"m.replace": edit_source("$e", BOB, 200, "as forged", "$o")}},
        )
        m = session._to_message(None, ev)
        assert (m.body, m.edited_ts) == ("as written", 0)


class TestRedactions:
    """A deleted message keeps its place as a tombstone, and any text we
    received before the deletion stays available locally."""

    def redacted_source(self, event_id, sender, ts, redacted_ts, type="m.room.message"):
        return {
            "type": type,
            "event_id": event_id,
            "sender": sender,
            "origin_server_ts": ts,
            "content": {},
            "unsigned": {
                "redacted_because": {
                    "type": "m.room.redaction",
                    "event_id": "$r",
                    "sender": sender,
                    "origin_server_ts": redacted_ts,
                    "redacts": event_id,
                    "content": {},
                }
            },
        }

    def test_fetched_deletion_becomes_an_empty_tombstone(self, session):
        from nio.events.room_events import Event

        ev = Event.parse_event(self.redacted_source("$d", ALICE, 100, 400))
        m = session._to_message(None, ev)
        assert (m.event_id, m.body, m.ts, m.redacted_ts) == ("$d", "", 100, 400)

    def test_a_removed_reaction_is_not_a_deleted_message(self, session):
        # Taking back a 👍 redacts an event too. The timeline never showed it,
        # so it must not turn into a "this message has been deleted" line.
        from nio.events.room_events import Event

        ev = Event.parse_event(
            self.redacted_source("$x", ALICE, 100, 400, type="m.reaction")
        )
        assert session._to_message(None, ev) is None

    def test_live_redaction_flags_the_cached_copy_and_keeps_its_text(self, session):
        from nio import RedactionEvent

        session.timelines["!a:hs"].append(
            Message(sender=ALICE, sender_name="Alice", body="secret", ts=100,
                    event_id="$d")
        )
        event = RedactionEvent.from_dict(
            {
                "type": "m.room.redaction",
                "event_id": "$r",
                "sender": ALICE,
                "origin_server_ts": 400,
                "redacts": "$d",
                "content": {},
            }
        )
        room = SimpleNamespace(room_id="!a:hs")
        asyncio.run(session._on_redaction(room, event))
        cached = session.timelines["!a:hs"][0]
        assert (cached.body, cached.redacted_ts) == ("secret", 400)
        # Nothing was appended, so the open room view needs another cue to
        # redraw: the latest-event id it watches.
        assert session.last_event_id["!a:hs"] == "$r"

    def test_history_merge_marks_a_cached_message_the_server_lost(self, session):
        # Deleted while we were away: the fetch brings back an empty
        # tombstone, the cache still holds the text. Keep both facts.
        from nio.events.room_events import Event

        session.timelines["!a:hs"].append(
            Message(sender=ALICE, sender_name="Alice", body="secret", ts=100,
                    event_id="$d")
        )

        async def fake_room_messages(*args, **kwargs):
            return SimpleNamespace(
                chunk=[Event.parse_event(self.redacted_source("$d", ALICE, 100, 400))]
            )

        session.client.room_messages = fake_room_messages
        msgs = asyncio.run(session.load_history("!a:hs", limit=10))
        assert [(m.body, m.redacted_ts) for m in msgs] == [("secret", 400)]


class TestLoadEdits:
    def run(self, session, chunk, message, monkeypatch, status=200):
        fake = TestRefreshSpaceChildren.FakeHttp({"chunk": chunk}, status=status)
        # **kw: this call passes a ClientTimeout to the session constructor.
        monkeypatch.setattr(
            "matrixcli.client.aiohttp.ClientSession", lambda **kw: fake
        )
        return asyncio.run(session.load_edits("!a:hs", message))

    def folded(self):
        return Message(
            sender=ALICE, sender_name="Alice", body="v3", ts=100,
            event_id="$o", edited_ts=300, original_body="v1",
        )

    def test_versions_come_back_oldest_first(self, session, monkeypatch):
        chunk = [
            edit_source("$e2", ALICE, 300, "v3", "$o"),
            edit_source("$e1", ALICE, 200, "v2", "$o"),
        ]
        versions = self.run(session, chunk, self.folded(), monkeypatch)
        assert [v.body for v in versions] == ["v1", "v2", "v3"]

    def test_replaces_from_other_senders_are_dropped(self, session, monkeypatch):
        chunk = [edit_source("$e", BOB, 200, "as forged", "$o")]
        versions = self.run(session, chunk, self.folded(), monkeypatch)
        assert [v.body for v in versions] == ["v1", "v3"]

    def test_falls_back_to_the_folded_versions_when_offline(
        self, session, monkeypatch
    ):
        versions = self.run(session, [], self.folded(), monkeypatch, status=500)
        assert [(v.ts, v.body) for v in versions] == [(100, "v1"), (300, "v3")]

    def test_a_deleted_message_is_never_fetched_for(self, session, monkeypatch):
        # The server has dropped the content; only the local copy is left, and
        # asking /relations about it would just be a pointless round-trip.
        fake = TestRefreshSpaceChildren.FakeHttp({"chunk": []})
        monkeypatch.setattr(
            "matrixcli.client.aiohttp.ClientSession", lambda **kw: fake
        )
        message = Message(
            sender=ALICE, sender_name="Alice", body="secret", ts=100,
            event_id="$d", redacted_ts=400,
        )
        versions = asyncio.run(session.load_edits("!a:hs", message))
        assert [v.body for v in versions] == ["secret"]
        assert fake.urls == []


class TestRefreshDirectMap:
    def run(self, session, payload, monkeypatch, status=200):
        fake = TestRefreshSpaceChildren.FakeHttp(payload, status=status)
        monkeypatch.setattr(
            "matrixcli.client.aiohttp.ClientSession", lambda **kw: fake
        )
        asyncio.run(session._refresh_direct_map())

    def test_maps_rooms_to_users(self, session, monkeypatch):
        self.run(session, {ALICE: ["!a:hs"], BOB: ["!b1:hs", "!b2:hs"]}, monkeypatch)
        assert session.direct_by_room == {
            "!a:hs": ALICE,
            "!b1:hs": BOB,
            "!b2:hs": BOB,
        }

    def test_malformed_values_are_skipped(self, session, monkeypatch):
        # A string value would be iterated character by character; non-string
        # room ids and null values must be dropped, not crash or pollute.
        payload = {
            ALICE: ["!good:hs", 7],
            BOB: "!oops:hs",
            "@c:hs": None,
        }
        self.run(session, payload, monkeypatch)
        assert session.direct_by_room == {"!good:hs": ALICE}

    def test_non_dict_body_is_ignored(self, session, monkeypatch):
        self.run(session, ["!a:hs"], monkeypatch)
        assert session.direct_by_room == {}


class TestInitialSync:
    """A homeserver that drops the connection mid-response makes nio raise the
    raw aiohttp error instead of returning a SyncError; initial_sync must ride
    that out rather than let it kill the startup worker."""

    def run(self, session, monkeypatch, syncs, get_displayname=None, refresh=None):
        calls = []

        async def noop(*a, **kw):
            return None

        async def fake_sync(**kw):
            outcome = syncs[len(calls)]
            calls.append(kw)
            if isinstance(outcome, Exception):
                raise outcome
            return outcome

        monkeypatch.setattr(session, "_refresh_direct_map", noop)
        monkeypatch.setattr(session, "refresh_space_children", refresh or noop)
        monkeypatch.setattr(
            session.client, "get_displayname", get_displayname or noop
        )
        monkeypatch.setattr(session.client, "sync", fake_sync)
        monkeypatch.setattr(asyncio, "sleep", noop)
        steps = []
        asyncio.run(session.initial_sync(progress=steps.append))
        return calls, steps

    def test_transport_error_is_retried_then_succeeds(self, session, monkeypatch):
        good = SimpleNamespace(rooms=SimpleNamespace(join={}))
        calls, steps = self.run(
            session,
            monkeypatch,
            [aiohttp.ClientPayloadError("payload not completed"), good],
        )
        assert len(calls) == 2
        assert any("retrying" in s for s in steps)

    def test_transport_error_every_time_does_not_raise(self, session, monkeypatch):
        boom = ConnectionResetError(54, "Connection reset by peer")
        calls, steps = self.run(session, monkeypatch, [boom, boom, boom])
        assert len(calls) == 3
        assert any("showing cached data" in s for s in steps)

    def test_displayname_transport_error_is_not_fatal(self, session, monkeypatch):
        async def boom(*a, **kw):
            raise aiohttp.ClientPayloadError("payload not completed")

        good = SimpleNamespace(rooms=SimpleNamespace(join={}))
        calls, steps = self.run(
            session, monkeypatch, [good], get_displayname=boom
        )
        assert len(calls) == 1
        assert not any("retrying" in s for s in steps)

    def test_first_run_syncs_from_scratch(self, session, monkeypatch):
        # No stored token and no persisted snapshot: the one big seeding sync,
        # from position zero, with full state.
        good = SimpleNamespace(rooms=SimpleNamespace(join={}))
        calls, steps = self.run(session, monkeypatch, [good])
        assert calls[0]["full_state"] is True
        assert calls[0]["timeout"] == 30000
        assert session.client.next_batch == ""

    def test_later_runs_resume_from_the_stored_token(self, session, monkeypatch):
        # A from-scratch sync is Synapse's slowest path (minutes on a loaded
        # server); once a token and the room_meta snapshot exist, launch must
        # sync incrementally and must NOT clear the stored position.
        session.client.loaded_sync_token = "s123"
        session.state["room_meta"] = {"!a:hs": {"title": "A"}}
        good = SimpleNamespace(rooms=SimpleNamespace(join={}))
        calls, steps = self.run(session, monkeypatch, [good])
        assert calls[0]["full_state"] is False
        assert calls[0]["timeout"] == 0
        assert session.client.loaded_sync_token == "s123"
        assert any("syncing new messages" in s for s in steps)

    def test_cached_space_hierarchy_refreshes_in_the_background(
        self, session, monkeypatch
    ):
        # With the child map restored from state.json, startup must not wait
        # for the raw-state refetch (multi-second per space on a loaded
        # server): a refresh that never finishes would otherwise hang this.
        session.space_children = {"!s:hs": {"!c:hs"}}
        started = []

        async def never_finishes(space_id=None):
            started.append(True)
            await asyncio.Event().wait()

        good = SimpleNamespace(rooms=SimpleNamespace(join={}))
        self.run(session, monkeypatch, [good], refresh=never_finishes)

    def test_empty_space_map_still_blocks_on_the_first_fetch(
        self, session, monkeypatch
    ):
        # First run: nothing cached, so the space columns would be empty
        # without waiting for the fetch.
        done = []

        async def refresh(space_id=None):
            done.append(True)

        good = SimpleNamespace(rooms=SimpleNamespace(join={}))
        self.run(session, monkeypatch, [good], refresh=refresh)
        assert done == [True]


class TestRoomMetaSnapshot:
    """A resumed (incremental) launch never delivers quiet rooms, so the
    dashboard serves them from the room_meta snapshot persisted in state.json
    and refreshed from every live dashboard build."""

    def test_dashboard_lists_snapshot_rooms_missing_from_client(self, session):
        session.state["room_meta"] = {
            "!quiet:hs": {
                "title": "Quiet room",
                "is_space": False,
                "person": "",
                "is_favourite": True,
                "unread": 2,
                "highlights": 1,
            },
            "!dm:hs": {"title": "Alice", "person": ALICE},
        }
        session.state["last_event_ts"]["!quiet:hs"] = 1234
        dash = session.dashboard()
        fav = next(e for e in dash["favourites"] if e.room_id == "!quiet:hs")
        assert fav.title == "Quiet room"
        assert fav.unread == 2
        assert fav.highlights == 1
        assert fav.last_ts == 1234
        assert [e.room_id for e in dash["dms"]] == ["!dm:hs"]

    def test_live_rooms_win_over_and_refresh_the_snapshot(self, session, fake_room):
        session.state["room_meta"] = {"!a:hs": {"title": "Stale name"}}
        session.client.rooms["!a:hs"] = fake_room("!a:hs", display_name="Fresh name")
        dash = session.dashboard()
        entries = [e for e in dash["all"] if e.room_id == "!a:hs"]
        assert [e.title for e in entries] == ["Fresh name"]
        assert session.state["room_meta"]["!a:hs"]["title"] == "Fresh name"

    def test_stateless_live_room_backfills_from_snapshot(self, session, fake_room):
        # A room delivered by a resumed (incremental) sync has no state this
        # session: nio invents a title and knows nothing of space/favourite
        # status. Those fields must come from the snapshot, and the snapshot
        # must not be poisoned by the stateless entry either.
        session.state["room_meta"] = {
            "!a:hs": {"title": "IOI 2026", "is_space": True, "is_favourite": True}
        }
        room = fake_room("!a:hs", display_name="Empty Room")
        room.named_room_name = lambda: None
        session.client.rooms["!a:hs"] = room
        dash = session.dashboard()
        e = next(x for x in dash["all"] if x.room_id == "!a:hs")
        assert e.title == "IOI 2026"
        assert e.is_space
        assert e.is_favourite
        assert session.state["room_meta"]["!a:hs"]["title"] == "IOI 2026"

    def test_heroes_blob_title_never_beats_the_snapshot(self, session, fake_room):
        # An ACTIVE room in a resumed session has members (lazy loading still
        # delivers the message senders' member events), and nio then builds a
        # heroes-based group name. That blob must not displace the real name
        # in the snapshot; only m.room.name/alias or a DM peer outranks it.
        session.state["room_meta"] = {"!d:hs": {"title": "ioi.discuss"}}
        room = fake_room(
            "!d:hs",
            display_name="ALB-DL-Emanuel, ALB-TL-Erida and 316 others",
            users={"@alb:hs": None, "@arg:hs": None},
        )
        room.named_room_name = lambda: None
        session.client.rooms["!d:hs"] = room
        dash = session.dashboard()
        e = next(x for x in dash["all"] if x.room_id == "!d:hs")
        assert e.title == "ioi.discuss"
        assert session.state["room_meta"]["!d:hs"]["title"] == "ioi.discuss"

    def test_real_room_name_beats_the_snapshot(self, session, fake_room):
        session.state["room_meta"] = {"!d:hs": {"title": "old name"}}
        room = fake_room("!d:hs", display_name="new name")
        room.named_room_name = lambda: "new name"
        session.client.rooms["!d:hs"] = room
        dash = session.dashboard()
        e = next(x for x in dash["all"] if x.room_id == "!d:hs")
        assert e.title == "new name"
        assert session.state["room_meta"]["!d:hs"]["title"] == "new name"

    def test_dm_mxid_title_rescued_from_snapshot_without_poisoning_it(
        self, session, fake_room
    ):
        # A DM whose peer sent nothing since the resume token has no member
        # event under lazy loading, so the live title degrades to the bare
        # user id. The snapshot's real name must win, on screen and in the
        # snapshot refresh.
        session.state["room_meta"] = {
            "!dm:hs": {"title": "Alice Liddell", "person": ALICE}
        }
        room = fake_room("!dm:hs", users={ME: {}, ALICE: {}}, names={ALICE: None})
        session.client.rooms["!dm:hs"] = room
        dash = session.dashboard()
        e = next(x for x in dash["dms"] if x.room_id == "!dm:hs")
        assert e.title == "Alice Liddell"
        assert session.state["room_meta"]["!dm:hs"]["title"] == "Alice Liddell"

    def test_dm_title_resolves_from_profile_cache(self, session, fake_room):
        room = fake_room("!dm:hs", users={ME: {}, ALICE: {}}, names={ALICE: None})
        session.profile_names[ALICE] = "Alice Liddell"
        e = session._entry(room)
        assert e.title == "Alice Liddell"

    def test_poisoned_snapshot_dm_title_repaired_from_profile_cache(self, session):
        # Snapshots written before the DM rescue existed can hold the bare
        # user id as the title; the profile cache repairs those too.
        session.state["room_meta"] = {"!dm:hs": {"title": ALICE, "person": ALICE}}
        session.profile_names[ALICE] = "Alice Liddell"
        dash = session.dashboard()
        e = next(x for x in dash["dms"] if x.room_id == "!dm:hs")
        assert e.title == "Alice Liddell"

    def test_profile_fetch_heals_snapshot_and_repaints(self, session):
        session.state["room_meta"] = {"!dm:hs": {"title": ALICE, "person": ALICE}}
        repaints = []
        session.on_names_loaded = lambda: repaints.append(True)

        async def get_displayname(user_id=None):
            return SimpleNamespace(displayname="Alice Liddell")

        session.client.get_displayname = get_displayname

        async def run():
            session._fetch_profile(ALICE)
            await asyncio.gather(*session._profile_fetches.values())

        asyncio.run(run())
        assert session.profile_names[ALICE] == "Alice Liddell"
        assert session.state["room_meta"]["!dm:hs"]["title"] == "Alice Liddell"
        assert repaints == [True]

    def test_named_room_never_becomes_dm_via_fallback(self, session, fake_room):
        # A big room seen through a lazy resume sync can hold exactly two
        # users (self + the one member who spoke); a real room name means it
        # is a group room regardless.
        room = fake_room("!ga:hs", users={ME: {}, ALICE: {}})
        room.name = "ioi.ga"
        e = session._entry(room)
        assert not e.is_direct and e.person is None

    def test_snapshot_group_record_blocks_dm_misdetection(
        self, session, fake_room
    ):
        # Same lazy two-member illusion, but the room has no name state this
        # session. A snapshot that recorded the room as not-a-DM outranks
        # the heuristic while the member map is incomplete, and the real
        # title comes back from the snapshot rescue.
        session.state["room_meta"] = {"!ga:hs": {"title": "ioi.ga", "person": ""}}
        room = fake_room(
            "!ga:hs",
            display_name="PHL-TL-Cisco Ortega",
            users={ME: {}, ALICE: {}},
            names={ALICE: "PHL-TL-Cisco Ortega"},
        )
        room.named_room_name = lambda: None
        room.members_synced = False
        session.client.rooms["!ga:hs"] = room
        dash = session.dashboard()
        e = next(x for x in dash["all"] if x.room_id == "!ga:hs")
        assert not e.is_direct and e.person is None
        assert e.title == "ioi.ga"
        assert session.state["room_meta"]["!ga:hs"]["person"] == ""
        assert session.state["room_meta"]["!ga:hs"]["title"] == "ioi.ga"

    def test_synced_two_member_room_still_becomes_dm(self, session, fake_room):
        # With the full member list fetched, two members really is a DM,
        # whatever a stale snapshot says.
        session.state["room_meta"] = {"!dm:hs": {"title": "old group", "person": ""}}
        room = fake_room("!dm:hs", users={ME: {}, ALICE: {}}, names={ALICE: "Alice"})
        e = session._entry(room)
        assert e.is_direct and e.person == ALICE and e.title == "Alice"

    def test_room_name_fetch_heals_poisoned_snapshot(self, session, fake_room):
        # A session that misread the lazy two-member illusion as a DM wrote
        # the peer's name (and possibly the peer) into the snapshot; the
        # m.room.name fetch repairs the room, the snapshot, and repaints.
        session.state["room_meta"] = {
            "!ga:hs": {"title": "PHL-TL-Cisco Ortega", "person": ALICE}
        }
        room = fake_room("!ga:hs", users={ME: {}, ALICE: {}})
        session.client.rooms["!ga:hs"] = room
        repaints = []
        session.on_names_loaded = lambda: repaints.append(True)

        async def room_get_state_event(room_id, event_type, state_key=""):
            return SimpleNamespace(content={"name": "ioi.ga"})

        session.client.room_get_state_event = room_get_state_event

        async def run():
            session._fetch_room_name("!ga:hs")
            await asyncio.gather(*session._name_fetches.values())

        asyncio.run(run())
        assert room.name == "ioi.ga"
        assert session.state["room_meta"]["!ga:hs"]["title"] == "ioi.ga"
        assert session.state["room_meta"]["!ga:hs"]["person"] == ""
        assert repaints == [True]

    def test_room_name_fetch_runs_once_per_room(self, session):
        calls = []

        async def room_get_state_event(room_id, event_type, state_key=""):
            calls.append(room_id)
            return SimpleNamespace()

        session.client.room_get_state_event = room_get_state_event

        async def run():
            session._fetch_room_name("!unnamed:hs")
            await asyncio.gather(*session._name_fetches.values())
            session._fetch_room_name("!unnamed:hs")
            await asyncio.gather(*session._name_fetches.values())

        asyncio.run(run())
        assert calls == ["!unnamed:hs"]

    def test_failed_profile_fetch_is_cached_and_not_retried(self, session):
        calls = []

        async def get_displayname(user_id=None):
            calls.append(user_id)
            return SimpleNamespace()

        session.client.get_displayname = get_displayname

        async def run():
            session._fetch_profile(ALICE)
            await asyncio.gather(*session._profile_fetches.values())
            session._fetch_profile(ALICE)
            await asyncio.gather(*session._profile_fetches.values())

        asyncio.run(run())
        assert calls == [ALICE]
        assert session.profile_names[ALICE] == ""

    def test_left_room_is_dropped_from_the_snapshot(self, session):
        session.state["room_meta"] = {"!gone:hs": {"title": "Left"}}
        response = SimpleNamespace(
            rooms=SimpleNamespace(join={}, leave={"!gone:hs": SimpleNamespace()})
        )
        session._record_room_timestamps(response)
        assert "!gone:hs" not in session.state["room_meta"]

    def test_mark_read_zeroes_the_snapshot_badge(self, session):
        session.state["room_meta"] = {
            "!a:hs": {"title": "A", "unread": 5, "highlights": 2}
        }
        asyncio.run(session.mark_read("!a:hs"))
        assert session.state["room_meta"]["!a:hs"]["unread"] == 0
        assert session.state["room_meta"]["!a:hs"]["highlights"] == 0

    def test_mark_read_debounces_the_marker_post(self, session, monkeypatch):
        # At peak an open room refreshes (and marked read) once per sync
        # tick; only the first call may POST immediately, a burst then folds
        # into one trailing send carrying the newest event id.
        import matrixcli.client as client_mod

        monkeypatch.setattr(client_mod, "MARK_READ_INTERVAL", 0.05)
        posts = []

        async def fake_markers(room_id, fully_read_event=None, read_event=None):
            posts.append(fully_read_event)

        session.client.room_read_markers = fake_markers

        async def main():
            session.last_event_id["!a:hs"] = "$1"
            await session.mark_read("!a:hs")
            for _ in range(5):
                await asyncio.sleep(0)
            session.last_event_id["!a:hs"] = "$2"
            await session.mark_read("!a:hs")
            session.last_event_id["!a:hs"] = "$3"
            await session.mark_read("!a:hs")
            task = session._marker_tasks.get("!a:hs")
            if task is not None:
                await task

        asyncio.run(main())
        assert posts == ["$1", "$3"]

    def test_ensure_room_registers_with_persisted_encryption_flag(self, session):
        # Sending into a room the resumed session has not seen live: nio's
        # room_send looks the room up (KeyError without this) and its
        # encrypted flag decides plaintext vs megolm, so it MUST come from
        # nio's persisted encrypted-rooms set, never default to False.
        session.client.encrypted_rooms = {"!enc:hs"}
        session._ensure_room("!enc:hs")
        session._ensure_room("!plain:hs")
        assert session.client.rooms["!enc:hs"].encrypted is True
        assert session.client.rooms["!plain:hs"].encrypted is False

    def test_ensure_room_leaves_known_rooms_alone(self, session, fake_room):
        room = fake_room("!a:hs")
        session.client.rooms["!a:hs"] = room
        session._ensure_room("!a:hs")
        assert session.client.rooms["!a:hs"] is room


class TestInvites:
    def test_dashboard_lists_invites(self, session):
        session.client.invited_rooms["!inv:hs"] = SimpleNamespace(
            display_name="Secret Club", inviter=ALICE
        )
        invites = session.dashboard()["invites"]
        assert [e.room_id for e in invites] == ["!inv:hs"]
        entry = invites[0]
        assert entry.is_invite
        assert entry.title == "Secret Club"
        assert entry.person == ALICE

    def test_accept_invite_joins_and_drops_the_invite(self, session):
        session.client.invited_rooms["!inv:hs"] = SimpleNamespace(
            display_name="x", inviter=ALICE
        )

        async def fake_join(room_id):
            return SimpleNamespace(room_id=room_id)

        session.client.join = fake_join
        ok, msg = asyncio.run(session.accept_invite("!inv:hs"))
        assert ok
        assert "!inv:hs" not in session.client.invited_rooms

    def test_accept_invite_failure_keeps_the_invite(self, session):
        from nio import JoinError

        session.client.invited_rooms["!inv:hs"] = SimpleNamespace(
            display_name="x", inviter=ALICE
        )

        async def fake_join(room_id):
            return JoinError.from_dict({"errcode": "M_FORBIDDEN", "error": "nope"})

        session.client.join = fake_join
        ok, msg = asyncio.run(session.accept_invite("!inv:hs"))
        assert not ok and "nope" in msg
        assert "!inv:hs" in session.client.invited_rooms


class TestLoadOlder:
    def test_paginates_and_stops_at_the_beginning(self, session):
        calls = []
        batches = {
            "": SimpleNamespace(chunk=[text_event("$3", ALICE, 300, "c")], end="tok1"),
            "tok1": SimpleNamespace(chunk=[text_event("$2", ALICE, 200, "b")], end="tok2"),
            "tok2": SimpleNamespace(chunk=[], end=None),
        }

        async def fake_room_messages(room_id, start, direction, limit):
            calls.append(start)
            return batches[start]

        session.client.room_messages = fake_room_messages
        first = asyncio.run(session.load_older("!a:hs"))
        assert [m.event_id for m in first] == ["$3"]
        second = asyncio.run(session.load_older("!a:hs"))
        assert [m.event_id for m in second] == ["$2"]
        assert asyncio.run(session.load_older("!a:hs")) == []
        assert asyncio.run(session.load_older("!a:hs")) == []
        assert calls == ["", "tok1", "tok2"]

    def test_oldest_first_within_a_batch(self, session):
        async def fake_room_messages(room_id, start, direction, limit):
            return SimpleNamespace(
                chunk=[
                    text_event("$new", ALICE, 300, "newer"),
                    text_event("$old", BOB, 100, "older"),
                ],
                end="tok",
            )

        session.client.room_messages = fake_room_messages
        msgs = asyncio.run(session.load_older("!a:hs"))
        assert [m.event_id for m in msgs] == ["$old", "$new"]

    def test_reset_pagination_forgets_position_and_done_flag(self, session):
        # The screen that owned the back-paginated messages is gone; a token
        # resuming from its depth would skip everything in between on reopen.
        session.pagination_tokens["!a:hs"] = "deep-token"
        session.pagination_done["!a:hs"] = True
        session.reset_pagination("!a:hs")

        async def fake_room_messages(room_id, start, direction, limit):
            return SimpleNamespace(
                chunk=[text_event("$1", ALICE, 100, "hi")], end="tok"
            )

        session.client.room_messages = fake_room_messages
        # Pagination works again (done flag cleared) and starts from the
        # sync position, not the dead screen's depth.
        session.client.next_batch = "now"
        msgs = asyncio.run(session.load_older("!a:hs"))
        assert [m.event_id for m in msgs] == ["$1"]

    def test_reset_pagination_on_an_unvisited_room_is_a_noop(self, session):
        session.reset_pagination("!never:hs")
        assert session.pagination_tokens == {}
        assert session.pagination_done == {}


class TestEchoTimestamp:
    """A send() echo carries this machine's wall clock; the server's
    origin_server_ts must displace it wherever the real event shows up."""

    def test_sync_echo_corrects_the_cached_timestamp(self, session, fake_room):
        session.timelines["!a:hs"].append(
            Message(sender=ME, sender_name="Me", body="hi", ts=99_999_999,
                    event_id="$sent")
        )
        room = fake_room("!a:hs")
        asyncio.run(session._on_message(room, text_event("$sent", ME, 1000, "hi")))
        (cached,) = session.timelines["!a:hs"]
        assert cached.ts == 1000
        assert cached.body == "hi"  # everything else untouched

    def test_history_merge_adopts_the_server_timestamp(self, session):
        session.timelines["!a:hs"].append(
            Message(sender=ME, sender_name="Me", body="hi", ts=99_999_999,
                    event_id="$sent")
        )

        async def fake_room_messages(*args, **kwargs):
            return SimpleNamespace(chunk=[text_event("$sent", ME, 1000, "hi")])

        session.client.room_messages = fake_room_messages
        msgs = asyncio.run(session.load_history("!a:hs", limit=10))
        assert [(m.event_id, m.ts) for m in msgs] == [("$sent", 1000)]


class TestLoadOlderErrors:
    def test_error_is_none_not_beginning(self, session):
        from nio import RoomMessagesError

        async def failing(*args, **kwargs):
            return RoomMessagesError.from_dict(
                {"errcode": "M_UNKNOWN", "error": "boom"}, "!a:hs"
            )

        session.client.room_messages = failing
        assert asyncio.run(session.load_older("!a:hs")) is None
        # And the room is NOT marked done: the next attempt retries.
        assert not session.pagination_done.get("!a:hs")


class TestReactions:
    def react(self, event_id, sender, target, key="👍", ts=100):
        from nio.events.room_events import Event

        return Event.parse_event(
            {
                "type": "m.reaction",
                "event_id": event_id,
                "sender": sender,
                "origin_server_ts": ts,
                "content": {
                    "m.relates_to": {
                        "rel_type": "m.annotation",
                        "event_id": target,
                        "key": key,
                    }
                },
            }
        )

    def test_reactions_aggregate_per_key(self, session, fake_room):
        room = fake_room("!a:hs")
        for eid, sender, key in (
            ("$r1", ALICE, "👍"),
            ("$r2", BOB, "👍"),
            ("$r3", ALICE, "🎉"),
        ):
            asyncio.run(session._on_reaction(room, self.react(eid, sender, "$m", key)))
        assert session.reaction_summary("!a:hs", "$m") == [("👍", 2), ("🎉", 1)]
        # The room view is nudged to redraw.
        assert session.last_event_id["!a:hs"] == "$r3"

    def test_reaction_detail_names_the_senders(self, session, fake_room):
        # Keys in badge (summary) order, names resolved through the member
        # map and sorted; an unknown sender falls back to the bare id.
        room = fake_room("!a:hs", names={ALICE: "Alice", BOB: "Bob"})
        session.client.rooms["!a:hs"] = room
        for eid, sender, key in (
            ("$r1", BOB, "👍"),
            ("$r2", ALICE, "👍"),
            ("$r3", "@stranger:hs", "🎉"),
        ):
            asyncio.run(session._on_reaction(room, self.react(eid, sender, "$m", key)))
        assert session.reaction_detail("!a:hs", "$m") == [
            ("👍", [(ALICE, "Alice"), (BOB, "Bob")]),
            ("🎉", [("@stranger:hs", "@stranger:hs")]),
        ]

    def test_variation_selector_variants_count_as_one_key(self, session, fake_room):
        # "👍" and "👍️" are the same vote; clients disagree on which
        # form they send.
        room = fake_room("!a:hs")
        asyncio.run(session._on_reaction(room, self.react("$r1", ALICE, "$m", "👍")))
        asyncio.run(
            session._on_reaction(room, self.react("$r2", BOB, "$m", "👍️"))
        )
        assert session.reaction_summary("!a:hs", "$m") == [("👍", 2)]

    def test_redelivered_reaction_does_not_double_count(self, session, fake_room):
        room = fake_room("!a:hs")
        asyncio.run(session._on_reaction(room, self.react("$r1", ALICE, "$m")))
        asyncio.run(session._on_reaction(room, self.react("$r1", ALICE, "$m")))
        assert session.reaction_summary("!a:hs", "$m") == [("👍", 1)]

    def test_refetched_reaction_tombstone_subtracts_that_sender(
        self, session, fake_room
    ):
        # The redaction that removed a noted reaction can be skipped by a
        # gappy sync; the only trace is then the reaction's tombstone in a
        # /messages refetch, which must subtract the vote instead of leaving
        # the badge one too high forever.
        from nio.events.room_events import Event

        room = fake_room("!a:hs")
        session.client.rooms["!a:hs"] = room
        asyncio.run(session._on_reaction(room, self.react("$r1", ALICE, "$m")))
        assert session.reaction_summary("!a:hs", "$m") == [("👍", 1)]
        tombstone = Event.parse_event(
            {
                "type": "m.reaction",
                "event_id": "$r1",
                "sender": ALICE,
                "origin_server_ts": 100,
                "content": {},
                "unsigned": {
                    "redacted_because": {
                        "type": "m.room.redaction",
                        "event_id": "$rx",
                        "sender": ALICE,
                        "origin_server_ts": 300,
                        "content": {},
                        "redacts": "$r1",
                    }
                },
            }
        )
        assert session._to_message(room, tombstone) is None
        assert session.reaction_summary("!a:hs", "$m") == []

    def test_redacting_a_reaction_subtracts_that_sender(self, session, fake_room):
        from nio import RedactionEvent

        room = fake_room("!a:hs")
        asyncio.run(session._on_reaction(room, self.react("$r1", ALICE, "$m")))
        asyncio.run(session._on_reaction(room, self.react("$r2", BOB, "$m")))
        redaction = RedactionEvent.from_dict(
            {
                "type": "m.room.redaction",
                "event_id": "$del",
                "sender": ALICE,
                "origin_server_ts": 400,
                "redacts": "$r1",
                "content": {},
            }
        )
        asyncio.run(session._on_redaction(SimpleNamespace(room_id="!a:hs"), redaction))
        assert session.reaction_summary("!a:hs", "$m") == [("👍", 1)]

    def test_fetched_reaction_is_recorded_not_rendered(self, session, fake_room):
        room = fake_room("!a:hs")
        assert session._to_message(room, self.react("$r1", ALICE, "$m")) is None
        assert session.reaction_summary("!a:hs", "$m") == [("👍", 1)]

    def test_undecryptable_encrypted_reaction_is_still_counted(
        self, session, fake_room
    ):
        # The wrapper carries the whole relation (target AND key) in
        # cleartext; no key material is needed, and no "[could not decrypt]"
        # row may appear for a thumbs-up.
        from nio.events.room_events import Event

        ev = Event.parse_event(
            {
                "type": "m.room.encrypted",
                "event_id": "$enc",
                "sender": ALICE,
                "origin_server_ts": 100,
                "room_id": "!a:hs",
                "content": {
                    "algorithm": "m.megolm.v1.aes-sha2",
                    "ciphertext": "xxx",
                    "device_id": "DEV",
                    "sender_key": "k",
                    "session_id": "s",
                    "m.relates_to": {
                        "rel_type": "m.annotation",
                        "event_id": "$m",
                        "key": "👍",
                    },
                },
            }
        )
        room = fake_room("!a:hs")
        assert session._to_message(room, ev) is None
        assert session.reaction_summary("!a:hs", "$m") == [("👍", 1)]


class TestMentions:
    def test_structured_mention_flags_the_message(self, session):
        ev = text_event("$1", ALICE, 100, "please check this")
        ev.source["content"]["m.mentions"] = {"user_ids": [ME]}
        assert session._to_message(None, ev).mentions_me is True

    def test_display_name_in_body_flags_the_message(self, session):
        session.my_name = "Treasurer"
        ev = text_event("$1", ALICE, 100, "ask treasurer about invoices")
        assert session._to_message(None, ev).mentions_me is True

    def test_plain_chatter_is_not_flagged(self, session):
        session.my_name = "Treasurer"
        ev = text_event("$1", ALICE, 100, "lunch is at noon")
        assert session._to_message(None, ev).mentions_me is False

    def test_own_message_is_never_a_ping(self, session):
        ev = text_event("$1", ME, 100, f"I am {ME}")
        assert session._to_message(None, ev).mentions_me is False

    def test_entry_carries_the_highlight_count(self, session, fake_room):
        room = fake_room("!a:hs", unread=5, highlights=2)
        e = session._entry(room)
        assert (e.unread, e.highlights) == (5, 2)


class TestOwnEdits:
    def run_send(self, session, coro):
        sent = {}

        async def room_send(room_id, message_type, content, **kwargs):
            sent.update(room_id=room_id, type=message_type, content=content)
            return SimpleNamespace(event_id="$new")

        session.client.room_send = room_send
        result = asyncio.run(coro)
        return sent, result

    def test_send_edit_wire_format_and_local_cache(self, session):
        target = Message(sender=ME, sender_name="Me", body="typo", ts=100,
                         event_id="$o")
        sent, (ok, info) = self.run_send(
            session, session.send_edit("!a:hs", target, "fixed")
        )
        assert ok and info == "$new"
        content = sent["content"]
        assert content["body"] == "* fixed"
        assert content["m.new_content"]["body"] == "fixed"
        assert content["m.relates_to"] == {
            "rel_type": "m.replace",
            "event_id": "$o",
        }
        # Cached at once, so the next redraw folds without waiting for sync.
        cached = list(session.timelines["!a:hs"])
        assert [(m.event_id, m.replaces, m.body) for m in cached] == [
            ("$new", "$o", "fixed")
        ]
        folded = fold_edits([target, *cached])
        assert [(m.event_id, m.body) for m in folded] == [("$o", "fixed")]

    def test_redact_flags_the_cached_copy(self, session):
        session.timelines["!a:hs"].append(
            Message(sender=ME, sender_name="Me", body="oops", ts=100,
                    event_id="$o")
        )

        async def room_redact(room_id, event_id, reason=None):
            return SimpleNamespace(event_id="$del")

        session.client.room_redact = room_redact
        ok, info = asyncio.run(session.redact("!a:hs", "$o"))
        assert ok
        (cached,) = session.timelines["!a:hs"]
        assert cached.redacted_ts > 0
        assert cached.body == "oops"  # kept for the history popup


class TestConnect:
    def _prep(self, session, monkeypatch, whoami, password=None):
        from nio import AsyncClient

        monkeypatch.setattr(
            session.cfg,
            "load_token",
            lambda: {"access_token": "tok", "device_id": "DEV"},
            raising=False,
        )
        cleared = []
        monkeypatch.setattr(
            session.cfg, "clear_token", lambda: cleared.append(True), raising=False
        )
        saved = {}
        monkeypatch.setattr(
            session.cfg,
            "save_token",
            lambda token, device: saved.update(token=token, device=device),
            raising=False,
        )
        monkeypatch.setattr(
            session.cfg, "get_password", lambda: password, raising=False
        )
        monkeypatch.setattr(session.client, "load_store", lambda: None)
        monkeypatch.setattr(AsyncClient, "should_upload_keys", False)

        async def fake_whoami():
            return whoami

        monkeypatch.setattr(session.client, "whoami", fake_whoami)
        return cleared, saved

    def test_valid_token_restores_session(self, session, monkeypatch):
        cleared, saved = self._prep(session, monkeypatch, SimpleNamespace(user_id=ME))
        ok, msg = asyncio.run(session.connect())
        assert ok
        assert not cleared and not saved

    def test_transient_whoami_error_keeps_token(self, session, monkeypatch):
        whoami = SimpleNamespace(message="Gateway timeout", status_code="M_UNKNOWN")
        cleared, saved = self._prep(session, monkeypatch, whoami, password="hunter2")
        login_calls = []

        async def fake_login(*args, **kwargs):
            login_calls.append(True)

        monkeypatch.setattr(session.client, "login", fake_login)
        ok, msg = asyncio.run(session.connect())
        assert not ok
        assert not cleared, "a transient error must not discard the token"
        assert not login_calls, "must not mint a new device via password login"
        assert "kept" in msg

    def test_offline_whoami_starts_from_cache(self, session, monkeypatch):
        cleared, saved = self._prep(
            session, monkeypatch, SimpleNamespace(user_id=ME), password="hunter2"
        )

        async def dead_network():
            raise aiohttp.ClientError("no route to host")

        monkeypatch.setattr(session.client, "whoami", dead_network)
        login_calls = []

        async def fake_login(*args, **kwargs):
            login_calls.append(True)

        monkeypatch.setattr(session.client, "login", fake_login)
        ok, msg = asyncio.run(session.connect())
        # A dead network is not a fatal launch error: the token, crypto
        # store, and message cache are enough to read everything offline.
        assert ok
        assert not cleared, "the cached token must survive an offline launch"
        assert not login_calls, "must not mint a new device via password login"
        assert "offline" in msg

    def test_rejected_token_clears_and_relogins(self, session, monkeypatch):
        whoami = SimpleNamespace(status_code="M_UNKNOWN_TOKEN", message="bad token")
        cleared, saved = self._prep(session, monkeypatch, whoami, password="hunter2")

        async def fake_login(password, device_name=None):
            assert password == "hunter2"
            return SimpleNamespace(access_token="new-tok", device_id="NEWDEV")

        monkeypatch.setattr(session.client, "login", fake_login)
        ok, msg = asyncio.run(session.connect())
        assert ok
        assert cleared
        assert saved == {"token": "new-tok", "device": "NEWDEV"}

    def test_relogin_with_new_device_id_rebuilds_the_client(
        self, session, monkeypatch
    ):
        # The token path loads the store for the revoked device id before
        # whoami can reject the token. If the password login then mints a
        # DIFFERENT device id, keeping the loaded client would sign with the
        # old device's olm account; connect must rebuild the client so the
        # store is re-bound to the new device id.
        whoami = SimpleNamespace(status_code="M_UNKNOWN_TOKEN", message="bad")
        cleared, saved = self._prep(session, monkeypatch, whoami, password="hunter2")
        original = session.client
        monkeypatch.setattr(session.client, "store", object(), raising=False)

        async def fake_login(password, device_name=None):
            return SimpleNamespace(access_token="new-tok", device_id="OTHERDEV")

        monkeypatch.setattr(session.client, "login", fake_login)
        ok, msg = asyncio.run(session.connect())
        assert ok
        assert session.client is not original
        assert session.client.device_id == "OTHERDEV"
        assert saved == {"token": "new-tok", "device": "OTHERDEV"}

    def test_relogin_with_same_device_id_keeps_the_client(
        self, session, monkeypatch
    ):
        whoami = SimpleNamespace(status_code="M_UNKNOWN_TOKEN", message="bad")
        cleared, saved = self._prep(session, monkeypatch, whoami, password="hunter2")
        original = session.client
        monkeypatch.setattr(session.client, "store", object(), raising=False)

        async def fake_login(password, device_name=None):
            return SimpleNamespace(access_token="new-tok", device_id="DEV")

        monkeypatch.setattr(session.client, "login", fake_login)
        ok, msg = asyncio.run(session.connect())
        assert ok
        assert session.client is original
        assert saved == {"token": "new-tok", "device": "DEV"}

    def test_rejected_token_without_password_fails_with_hint(
        self, session, monkeypatch
    ):
        whoami = SimpleNamespace(status_code="M_UNKNOWN_TOKEN", message="bad")
        cleared, saved = self._prep(session, monkeypatch, whoami, password=None)
        ok, msg = asyncio.run(session.connect())
        assert not ok
        assert cleared
        assert "Keychain" in msg

    def test_unreadable_store_resets_and_relogins(self, session, monkeypatch):
        # restore_login (not a later load_store) is what raises when the store
        # was written under the previous pickle key; connect must catch it there.
        whoami = SimpleNamespace(user_id=ME)
        cleared, saved = self._prep(session, monkeypatch, whoami, password="hunter2")

        def boom(**kwargs):
            raise Exception("MAC tag mismatch")

        reset_called = []
        monkeypatch.setattr(session.client, "restore_login", boom)
        monkeypatch.setattr(
            session, "_reset_store", lambda: reset_called.append(True)
        )

        async def fake_login(password, device_name=None):
            return SimpleNamespace(access_token="new-tok", device_id="NEWDEV")

        monkeypatch.setattr(session.client, "login", fake_login)
        ok, msg = asyncio.run(session.connect())
        assert ok
        assert reset_called and cleared
        assert saved == {"token": "new-tok", "device": "NEWDEV"}
        assert "reset" in msg and "verify" in msg
        # The clean message must not echo the store key or any token.
        assert "pickle" not in msg.lower()

    def test_unreadable_store_on_no_token_login_resets_and_retries(
        self, session, monkeypatch
    ):
        # With no cached token, login() is what loads the store (nio sets the
        # access token, then load_store raises on the old pickle key), so the
        # raise comes out of login rather than restore_login; connect must
        # give it the same reset-and-retry treatment.
        from nio import AsyncClient

        monkeypatch.setattr(
            session.cfg, "get_password", lambda: "hunter2", raising=False
        )
        saved = {}
        monkeypatch.setattr(
            session.cfg,
            "save_token",
            lambda token, device: saved.update(token=token, device=device),
            raising=False,
        )
        monkeypatch.setattr(AsyncClient, "should_upload_keys", False)

        calls = []
        logged_out = []
        reset_called = []

        async def bad_login(password, device_name=None):
            calls.append("first")
            session.client.access_token = "half-tok"
            raise Exception("MAC tag mismatch")

        async def fake_logout(*args, **kwargs):
            logged_out.append(True)

        def fake_reset():
            reset_called.append(True)

            async def good_login(password, device_name=None):
                calls.append("retry")
                return SimpleNamespace(access_token="new-tok", device_id="NEWDEV")

            monkeypatch.setattr(session.client, "login", good_login)
            monkeypatch.setattr(session.client, "load_store", lambda: None)

        monkeypatch.setattr(session.client, "login", bad_login)
        monkeypatch.setattr(session.client, "logout", fake_logout)
        monkeypatch.setattr(session, "_reset_store", fake_reset)

        ok, msg = asyncio.run(session.connect())
        assert ok
        assert calls == ["first", "retry"]
        assert reset_called
        # The half-created session from the first login was signed out.
        assert logged_out
        assert saved == {"token": "new-tok", "device": "NEWDEV"}
        assert "reset" in msg and "verify" in msg

    def test_login_transport_raise_does_not_wipe_the_store(
        self, session, monkeypatch
    ):
        # A raise WITHOUT an access token means the request itself failed
        # (network), not the store load; resetting would destroy a good store.
        monkeypatch.setattr(
            session.cfg, "get_password", lambda: "hunter2", raising=False
        )
        reset_called = []
        monkeypatch.setattr(
            session, "_reset_store", lambda: reset_called.append(True)
        )

        async def bad_login(password, device_name=None):
            raise Exception("Cannot connect to host")

        monkeypatch.setattr(session.client, "login", bad_login)
        ok, msg = asyncio.run(session.connect())
        assert not ok
        assert not reset_called
        assert "login failed" in msg

    def test_unreadable_store_without_password_gives_clean_message(
        self, session, monkeypatch
    ):
        cleared, saved = self._prep(
            session, monkeypatch, SimpleNamespace(), password=None
        )
        monkeypatch.setattr(
            session.client, "restore_login", lambda **kw: (_ for _ in ()).throw(Exception("MAC"))
        )
        monkeypatch.setattr(session, "_reset_store", lambda: None)
        ok, msg = asyncio.run(session.connect())
        assert not ok
        assert "reset" in msg and "Keychain" in msg


class TestRefreshSpaceChildren:
    class FakeHttp:
        def __init__(self, payload, status=200):
            self.payload = payload
            self.status = status
            self.urls = []

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        def get(self, url, headers=None, **kwargs):
            self.urls.append(url)
            outer = self

            class Ctx:
                async def __aenter__(ctx):
                    return SimpleNamespace(
                        status=outer.status, json=outer._json
                    )

                async def __aexit__(ctx, *args):
                    return False

            return Ctx()

        async def _json(self):
            return self.payload

    def test_lenient_parse_of_child_state(self, session, fake_room, monkeypatch):
        session.client.rooms["!s:hs"] = fake_room("!s:hs", room_type="m.space")
        payload = [
            {"type": "m.space.child", "state_key": "!good:hs", "content": {"via": ["hs"]}},
            {"type": "m.space.child", "state_key": "!badvia:hs", "content": {"via": "hs"}},
            {"type": "m.space.child", "state_key": "!removed:hs", "content": {}},
            {"type": "m.space.child", "content": {"via": ["hs"]}},
            {"type": "m.room.member", "state_key": "@u:hs", "content": {"membership": "join"}},
        ]
        fake = self.FakeHttp(payload)
        monkeypatch.setattr(
            "matrixcli.client.aiohttp.ClientSession", lambda **kw: fake
        )
        asyncio.run(session.refresh_space_children())
        # A malformed via (plain string) still counts as a live link; an
        # emptied content or missing state_key does not.
        assert session.space_children["!s:hs"] == {"!good:hs", "!badvia:hs"}
        # The space id is percent-encoded into the path so a hostile id
        # containing "/../" or "?" cannot re-target the request.
        assert "/rooms/%21s%3Ahs/state" in fake.urls[0]
        # Mirrored into state.json, so the next launch can paint the space
        # columns without waiting for this fetch.
        assert session.state["space_children"]["!s:hs"] == ["!badvia:hs", "!good:hs"]

    def test_error_response_leaves_existing_map_alone(
        self, session, fake_room, monkeypatch
    ):
        session.client.rooms["!s:hs"] = fake_room("!s:hs", room_type="m.space")
        session.space_children["!s:hs"] = {"!kept:hs"}
        fake = self.FakeHttp([], status=403)
        monkeypatch.setattr(
            "matrixcli.client.aiohttp.ClientSession", lambda **kw: fake
        )
        asyncio.run(session.refresh_space_children())
        assert session.space_children["!s:hs"] == {"!kept:hs"}


class TestSpacesWithChildChanges:
    def resp(self, join):
        return SimpleNamespace(rooms=SimpleNamespace(join=join))

    def room(self, timeline=(), state=()):
        return SimpleNamespace(
            timeline=SimpleNamespace(events=list(timeline)), state=list(state)
        )

    def test_detects_child_events_in_timeline_and_state(self, session):
        child = SimpleNamespace(
            source={"type": "m.space.child", "state_key": "!r:hs", "content": {}}
        )
        message = SimpleNamespace(source={"type": "m.room.message", "content": {}})
        resp = self.resp(
            {
                "!s1:hs": self.room(timeline=[message, child]),
                "!s2:hs": self.room(state=[child]),
                "!plain:hs": self.room(timeline=[message]),
            }
        )
        assert session.spaces_with_child_changes(resp) == {"!s1:hs", "!s2:hs"}

    def test_matches_bad_events_without_typed_fields(self, session):
        # The malformed child events this whole path exists for arrive as
        # BadEvent, which keeps only the source dict.
        from nio.events.room_events import Event, RoomSpaceChildEvent

        bad = Event.parse_event(
            {
                "type": "m.space.child",
                "event_id": "$c",
                "sender": ALICE,
                "origin_server_ts": 100,
                "room_id": "!s:hs",
                "state_key": "!r:hs",
                "content": {"via": "hs"},
            }
        )
        assert not isinstance(bad, RoomSpaceChildEvent)
        resp = self.resp({"!s:hs": self.room(timeline=[bad])})
        assert session.spaces_with_child_changes(resp) == {"!s:hs"}

    def test_empty_response_is_handled(self, session):
        assert session.spaces_with_child_changes(SimpleNamespace()) == set()
        assert session.spaces_with_child_changes(self.resp({})) == set()


class TestDownloadMedia:
    def media_msg(self, **kw):
        defaults = dict(
            sender=ALICE,
            sender_name="A",
            body="caption",
            ts=1,
            event_id="$1",
            media_url="mxc://hs/abc",
            media_name="report.pdf",
        )
        defaults.update(kw)
        return Message(**defaults)

    def test_saves_dedups_and_sanitizes(self, session, tmp_path):
        from pathlib import Path

        async def fake_download(mxc=None, **kwargs):
            assert mxc == "mxc://hs/abc"
            return SimpleNamespace(body=b"data")

        session.client.download = fake_download
        m = self.media_msg(media_name="../report.pdf")
        ok, first = asyncio.run(session.download_media(m, tmp_path))
        assert ok
        assert first == str(tmp_path / "report.pdf")
        assert Path(first).read_bytes() == b"data"
        ok, second = asyncio.run(session.download_media(m, tmp_path))
        assert ok and second == str(tmp_path / "report (1).pdf")

    def test_error_response_reports_reason(self, session, tmp_path):
        async def fake_download(**kwargs):
            return SimpleNamespace(message="not found", status_code="M_NOT_FOUND")

        session.client.download = fake_download
        ok, err = asyncio.run(
            session.download_media(self.media_msg(), tmp_path)
        )
        assert not ok and "not found" in err

    def test_rejects_oversize_advertised(self, session, tmp_path):
        from matrixcli.client import MAX_DOWNLOAD_BYTES

        m = self.media_msg(media_size=MAX_DOWNLOAD_BYTES + 1)
        ok, err = asyncio.run(session.download_media(m, tmp_path))
        assert not ok and "too large" in err

    def test_rejects_oversize_body(self, session, tmp_path):
        from matrixcli.client import MAX_DOWNLOAD_BYTES

        async def fake_download(mxc=None, **kwargs):
            return SimpleNamespace(body=b"x" * (MAX_DOWNLOAD_BYTES + 1))

        session.client.download = fake_download
        ok, err = asyncio.run(session.download_media(self.media_msg(), tmp_path))
        assert not ok and "too large" in err

    def test_written_file_is_owner_only(self, session, tmp_path):
        import stat

        async def fake_download(mxc=None, **kwargs):
            return SimpleNamespace(body=b"data")

        session.client.download = fake_download
        ok, path = asyncio.run(session.download_media(self.media_msg(), tmp_path))
        assert ok
        mode = stat.S_IMODE(__import__("os").stat(path).st_mode)
        assert mode == 0o600

    def test_strips_control_chars_from_filename(self, session, tmp_path):
        from pathlib import Path

        async def fake_download(mxc=None, **kwargs):
            return SimpleNamespace(body=b"data")

        session.client.download = fake_download
        m = self.media_msg(media_name="a\x1b[31mb\x07.pdf")
        ok, path = asyncio.run(session.download_media(m, tmp_path))
        assert ok
        # Control bytes (ESC, BEL) are gone; the residual printable text is inert.
        name = Path(path).name
        assert "\x1b" not in name and "\x07" not in name
        assert name == "a[31mb.pdf"

    def test_dotdot_only_name_does_not_escape_directory(self, session, tmp_path):
        from pathlib import Path

        async def fake_download(mxc=None, **kwargs):
            return SimpleNamespace(body=b"data")

        session.client.download = fake_download
        m = self.media_msg(media_name="..")
        ok, path = asyncio.run(session.download_media(m, tmp_path))
        assert ok
        assert Path(path).parent == tmp_path
        assert Path(path).name == "download"

    def test_does_not_follow_symlink_at_target(self, session, tmp_path):
        import os
        from pathlib import Path

        async def fake_download(mxc=None, **kwargs):
            return SimpleNamespace(body=b"data")

        session.client.download = fake_download
        victim = tmp_path / "victim"
        victim.write_text("precious")
        (tmp_path / "report.pdf").symlink_to(victim)
        ok, path = asyncio.run(
            session.download_media(self.media_msg(media_name="report.pdf"), tmp_path)
        )
        assert ok
        assert Path(path).name == "report (1).pdf"  # skipped the symlink name
        assert victim.read_text() == "precious"  # not written through


class TestInsecureHomeserver:
    def test_http_rejected(self, session):
        session.cfg.homeserver = "http://evil.example"
        assert session._insecure_homeserver() is True

    def test_https_ok(self, session):
        session.cfg.homeserver = "https://matrix.example"
        assert session._insecure_homeserver() is False

    def test_localhost_http_allowed(self, session):
        session.cfg.homeserver = "http://localhost:8008"
        assert session._insecure_homeserver() is False

    def test_connect_refuses_insecure(self, session):
        session.cfg.homeserver = "http://evil.example"
        ok, msg = asyncio.run(session.connect())
        assert not ok and "insecure" in msg


class TestToggleReaction:
    def wire(self, session):
        """Record sends and redactions; each returns a fresh event id."""
        calls = {"sent": [], "redacted": []}

        async def fake_room_send(room_id, message_type, content, **kwargs):
            calls["sent"].append((room_id, message_type, content))
            return SimpleNamespace(event_id=f"$sent{len(calls['sent'])}")

        async def fake_room_redact(room_id, event_id):
            calls["redacted"].append((room_id, event_id))
            return SimpleNamespace(event_id=f"$redact{len(calls['redacted'])}")

        session.client.room_send = fake_room_send
        session.client.room_redact = fake_room_redact
        return calls

    def test_first_toggle_sends_an_annotation_and_shows_at_once(self, session):
        calls = self.wire(session)
        ok, event_id, added = asyncio.run(
            session.toggle_reaction("!a:hs", "$msg", "👍")
        )
        assert (ok, added, event_id) == (True, True, "$sent1")
        room_id, message_type, content = calls["sent"][0]
        assert message_type == "m.reaction"
        assert content["m.relates_to"] == {
            "rel_type": "m.annotation",
            "event_id": "$msg",
            "key": "👍",
        }
        # Noted locally right away: the badge and the my-reaction check must
        # not wait for the sync echo.
        assert session.reaction_summary("!a:hs", "$msg") == [("👍", 1)]
        assert session.my_reaction("!a:hs", "$msg", "👍") == "$sent1"
        assert session.last_event_id["!a:hs"] == "$sent1"

    def test_second_toggle_redacts_and_subtracts(self, session):
        calls = self.wire(session)
        asyncio.run(session.toggle_reaction("!a:hs", "$msg", "👍"))
        ok, event_id, added = asyncio.run(
            session.toggle_reaction("!a:hs", "$msg", "👍")
        )
        assert (ok, added) == (True, False)
        assert calls["redacted"] == [("!a:hs", "$sent1")]
        assert session.reaction_summary("!a:hs", "$msg") == []
        assert session.my_reaction("!a:hs", "$msg", "👍") is None

    def test_variation_selector_does_not_split_the_toggle(self, session):
        # Sending "👍️" (with the invisible variation selector) and toggling
        # with the bare "👍" must land in the same bucket and remove it.
        calls = self.wire(session)
        asyncio.run(session.toggle_reaction("!a:hs", "$msg", "👍️"))
        ok, _event_id, added = asyncio.run(
            session.toggle_reaction("!a:hs", "$msg", "👍")
        )
        assert (ok, added) == (True, False)
        assert len(calls["sent"]) == 1 and len(calls["redacted"]) == 1
        assert session.reaction_summary("!a:hs", "$msg") == []

    def test_someone_elses_reaction_is_not_ours_to_remove(self, session):
        calls = self.wire(session)
        session._note_reaction("!a:hs", "$their", ALICE, "$msg", "👍")
        ok, _event_id, added = asyncio.run(
            session.toggle_reaction("!a:hs", "$msg", "👍")
        )
        # Alice's vote stands; ours is added next to it, nothing redacted.
        assert (ok, added) == (True, True)
        assert calls["redacted"] == []
        assert session.reaction_summary("!a:hs", "$msg") == [("👍", 2)]


class TestTimelineCachePersistence:
    """The encrypted on-disk timeline cache: what a restart gets back."""

    def seed(self, session):
        session.timelines["!a:hs"].extend(
            [
                Message(
                    sender=ALICE,
                    sender_name="Alice",
                    body="hello",
                    ts=1000,
                    event_id="$1",
                ),
                Message(
                    sender=ME,
                    sender_name="Me",
                    body="a picture",
                    ts=2000,
                    event_id="$2",
                    media_url="mxc://hs/xyz",
                    media_name="pic.png",
                    media_crypt={"key": "k", "iv": "i", "sha256": "h"},
                ),
            ]
        )
        session.reactions["!a:hs"] = {"$1": {"👍": {ALICE, BOB}}}
        session._reaction_events["$r1"] = ("!a:hs", "$1", "👍", ALICE)
        session.last_event_id["!a:hs"] = "$2"
        session.client.next_batch = "tok"

    def restored(self, cfg, resume=True, token="tok"):
        fresh = MatrixSession(cfg)
        fresh.client.loaded_sync_token = token
        fresh._restore_timelines(resume)
        return fresh

    def test_roundtrip_across_sessions(self, cfg, session):
        self.seed(session)
        session._save_timelines()
        fresh = self.restored(cfg)
        assert list(fresh.timelines["!a:hs"]) == list(session.timelines["!a:hs"])
        assert fresh.reactions == {"!a:hs": {"$1": {"👍": {ALICE, BOB}}}}
        assert fresh._reaction_events["$r1"] == ("!a:hs", "$1", "👍", ALICE)
        assert fresh.last_event_id["!a:hs"] == "$2"
        # Token matched: the cache is gapless, rooms may serve without a fetch.
        assert fresh.gap_gen == {}

    def test_pending_local_echoes_are_not_persisted(self, cfg, session):
        self.seed(session)
        session.timelines["!a:hs"].append(
            Message(sender=ME, sender_name="Me", body="unsent", ts=3000, pending=True)
        )
        session._save_timelines()
        fresh = self.restored(cfg)
        assert [m.event_id for m in fresh.timelines["!a:hs"]] == ["$1", "$2"]

    def test_token_mismatch_marks_every_seeded_room_gapped(self, cfg, session):
        self.seed(session)
        session._save_timelines()
        fresh = self.restored(cfg, token="newer-tok")
        # Content is still seeded (decrypted bodies survive the next merge)...
        assert len(fresh.timelines["!a:hs"]) == 2
        # ...but the first open must refetch: events may hide in the gap.
        assert fresh.gap_gen == {"!a:hs": 1}

    def test_non_resume_launch_marks_rooms_gapped(self, cfg, session):
        self.seed(session)
        session._save_timelines()
        fresh = self.restored(cfg, resume=False)
        assert fresh.gap_gen == {"!a:hs": 1}

    def test_cache_of_another_account_is_ignored(self, cfg, session):
        cfg.save_timeline_cache(
            {
                "user_id": "@other:example.org",
                "next_batch": "tok",
                "timelines": {"!a:hs": [{"sender": ALICE, "sender_name": "Alice", "body": "x", "ts": 1}]},
            }
        )
        fresh = self.restored(cfg)
        assert not fresh.timelines

    def test_unknown_message_fields_are_dropped_not_fatal(self, cfg, session):
        cfg.save_timeline_cache(
            {
                "user_id": ME,
                "next_batch": "tok",
                "timelines": {
                    "!a:hs": [
                        {
                            "sender": ALICE,
                            "sender_name": "Alice",
                            "body": "old cache",
                            "ts": 1,
                            "field_from_the_future": True,
                        }
                    ]
                },
            }
        )
        fresh = self.restored(cfg)
        assert [m.body for m in fresh.timelines["!a:hs"]] == ["old cache"]

    def test_import_keys_clears_the_disk_cache(self, cfg, session, monkeypatch):
        self.seed(session)
        session._save_timelines()
        assert cfg._timeline_cache_path.exists()

        async def fake_import(infile, passphrase):
            return None

        monkeypatch.setattr(session.client, "import_keys", fake_import)
        asyncio.run(session.import_keys("keys.txt", "pass"))
        assert not session.timelines
        assert not cfg._timeline_cache_path.exists()

    def test_close_flushes_a_dirty_cache(self, cfg, session, monkeypatch):
        self.seed(session)
        session._cache_dirty = True

        async def fake_close():
            return None

        monkeypatch.setattr(session.client, "close", fake_close)
        asyncio.run(session.close())
        assert cfg.load_timeline_cache()["last_event_id"] == {"!a:hs": "$2"}


class TestBackfillArchive:
    """The background full-history download and the archive it fills."""

    def msg(self, event_id, ts, body="m"):
        return Message(
            sender=ALICE, sender_name="Alice", body=body, ts=ts, event_id=event_id
        )

    def wire_pages(self, session, fake_room, pages):
        """Serve /messages responses from a fixed list of (events, end)."""
        session.client.rooms["!a:hs"] = fake_room("!a:hs")
        calls = []

        async def fake_room_messages(room_id, start, direction, limit):
            calls.append(start)
            events, end = pages[len(calls) - 1]
            return SimpleNamespace(chunk=list(events), end=end)

        session.client.room_messages = fake_room_messages
        return calls

    def test_full_download_reaches_beginning(self, session, fake_room):
        self.wire_pages(
            session,
            fake_room,
            [
                ([text_event("$3", ALICE, 3000, "c"), text_event("$2", ALICE, 2000, "b")], "t1"),
                ([text_event("$1", ALICE, 1000, "a")], None),
            ],
        )
        asyncio.run(session._backfill("!a:hs"))
        assert set(session.archives["!a:hs"]) == {"$1", "$2", "$3"}
        assert "!a:hs" in session.archive_done
        assert "!a:hs" not in session.archive_stale
        assert session._cache_dirty

    def test_resumes_from_persisted_token(self, session, fake_room):
        session.archives["!a:hs"] = {"$9": self.msg("$9", 9000)}
        session.archive_tokens["!a:hs"] = "deep"
        calls = self.wire_pages(
            session, fake_room, [([text_event("$1", ALICE, 1000, "a")], None)]
        )
        asyncio.run(session._backfill("!a:hs"))
        assert calls == ["deep"]
        assert set(session.archives["!a:hs"]) == {"$1", "$9"}

    def test_recover_walk_stops_at_archived_territory(self, session, fake_room):
        session.archives["!a:hs"] = {
            "$1": self.msg("$1", 1000),
            "$2": self.msg("$2", 2000),
        }
        session.archive_done.add("!a:hs")
        session.archive_stale.add("!a:hs")
        calls = self.wire_pages(
            session,
            fake_room,
            [
                ([text_event("$5", ALICE, 5000, "e"), text_event("$4", ALICE, 4000, "d")], "t1"),
                ([text_event("$2", ALICE, 2000, "b"), text_event("$1", ALICE, 1000, "a")], "t2"),
                ([], None),  # must never be reached
            ],
        )
        asyncio.run(session._backfill("!a:hs"))
        # The hole ($4, $5) is filled; the walk stopped at known territory
        # instead of re-fetching the whole room.
        assert len(calls) == 2
        assert set(session.archives["!a:hs"]) == {"$1", "$2", "$4", "$5"}
        assert "!a:hs" in session.archive_done
        assert "!a:hs" not in session.archive_stale

    def test_load_older_serves_archive_without_network(self, session):
        session.archives["!a:hs"] = {
            f"${i}": self.msg(f"${i}", i * 1000) for i in range(1, 6)
        }
        session.archive_done.add("!a:hs")

        async def no_network(*a, **kw):
            raise AssertionError("archive-served pagination must not fetch")

        session.client.room_messages = no_network
        session.reset_pagination("!a:hs")
        assert [m.event_id for m in asyncio.run(session.load_older("!a:hs", limit=2))] == ["$4", "$5"]
        assert [m.event_id for m in asyncio.run(session.load_older("!a:hs", limit=2))] == ["$2", "$3"]
        assert [m.event_id for m in asyncio.run(session.load_older("!a:hs", limit=2))] == ["$1"]
        assert asyncio.run(session.load_older("!a:hs", limit=2)) == []
        assert session.pagination_done["!a:hs"]

    def test_load_older_partial_archive_continues_from_its_token(self, session, fake_room):
        session.archives["!a:hs"] = {
            "$2": self.msg("$2", 2000),
            "$3": self.msg("$3", 3000),
        }
        session.archive_tokens["!a:hs"] = "deep"
        calls = self.wire_pages(
            session, fake_room, [([text_event("$1", ALICE, 1000, "a")], "deeper")]
        )
        session.reset_pagination("!a:hs")
        served = asyncio.run(session.load_older("!a:hs", limit=5))
        assert [m.event_id for m in served] == ["$2", "$3"]
        older = asyncio.run(session.load_older("!a:hs", limit=5))
        # The wire continuation starts exactly where the download stopped.
        assert calls == ["deep"]
        assert [m.event_id for m in older] == ["$1"]

    def test_archive_roundtrips_and_mismatch_marks_stale(self, cfg, session):
        session.archives["!a:hs"] = {"$1": self.msg("$1", 1000)}
        session._archive_dirty.add("!a:hs")
        session.archive_tokens["!a:hs"] = "deep"
        session.archive_done.add("!a:hs")
        session.client.next_batch = "tok"
        session._save_timelines()
        # The archive went to its own per-room file, not the main cache.
        assert "archives" not in cfg.load_timeline_cache()
        assert cfg._room_archive_path("!a:hs").exists()

        fresh = MatrixSession(cfg)
        fresh.client.loaded_sync_token = "tok"
        fresh._restore_timelines(resume=True)
        assert set(fresh.archives["!a:hs"]) == {"$1"}
        assert fresh.archive_tokens["!a:hs"] == "deep"
        assert "!a:hs" in fresh.archive_done
        assert "!a:hs" not in fresh.archive_stale

        stale = MatrixSession(cfg)
        stale.client.loaded_sync_token = "other-tok"
        stale._restore_timelines(resume=True)
        assert "!a:hs" in stale.archive_stale

    def test_gappy_sync_marks_an_archived_room_stale(self, session):
        session.archives["!a:hs"] = {"$1": self.msg("$1", 1000)}
        resp = SimpleNamespace(
            rooms=SimpleNamespace(
                join={
                    "!a:hs": SimpleNamespace(
                        timeline=SimpleNamespace(limited=True, events=[])
                    )
                },
                leave={},
            )
        )
        session._record_room_timestamps(resp)
        assert "!a:hs" in session.archive_stale

    def test_start_backfill_is_a_noop_when_archived(self, session):
        async def main():
            session.archive_done.add("!a:hs")
            session.start_backfill("!a:hs")
            return dict(session._backfill_tasks)

        assert asyncio.run(main()) == {}

    def test_window_edits_fold_into_the_archive_on_save(self, cfg, session):
        session.archives["!a:hs"] = {"$1": self.msg("$1", 1000)}
        session.timelines["!a:hs"].append(
            replace(self.msg("$1", 1000), redacted_ts=5000, body="deleted text")
        )
        session._save_timelines()
        # The archive's disk copy inherited the redaction mark AND the text.
        saved = [
            p for p in cfg.load_room_archives() if p["room_id"] == "!a:hs"
        ][0]["messages"]
        assert saved[0]["redacted_ts"] == 5000
        assert saved[0]["body"] == "deleted text"


class TestCachePolicy:
    """The global and per-space switches governing what may touch disk."""

    def msg(self, event_id, ts):
        return Message(
            sender=ALICE, sender_name="Alice", body="m", ts=ts, event_id=event_id
        )

    def test_global_off_disallows_everything(self, session):
        session.cfg.cache_messages = False
        assert not session.cache_allowed("!a:hs")

    def test_space_opt_out_covers_its_rooms_and_itself(self, session):
        session.space_children["!s:hs"] = {"!a:hs", "!b:hs"}
        session.state["cache_spaces"] = {"!s:hs": False}
        assert not session.cache_allowed("!a:hs")
        assert not session.cache_allowed("!s:hs")
        assert session.cache_allowed("!elsewhere:hs")

    def test_any_opted_out_space_wins_for_multi_space_rooms(self, session):
        session.space_children["!on:hs"] = {"!a:hs"}
        session.space_children["!off:hs"] = {"!a:hs"}
        session.state["cache_spaces"] = {"!off:hs": False}
        assert not session.cache_allowed("!a:hs")

    def test_toggle_off_purges_disk_and_on_removes_the_override(
        self, cfg, session
    ):
        session.space_children["!s:hs"] = {"!a:hs"}
        session.archives["!a:hs"] = {"$1": self.msg("$1", 1000)}
        session._archive_dirty.add("!a:hs")
        session.timelines["!a:hs"].append(self.msg("$1", 1000))
        session._save_timelines()
        assert cfg._room_archive_path("!a:hs").exists()

        session.set_space_cache("!s:hs", False)
        assert not cfg._room_archive_path("!a:hs").exists()
        assert "!a:hs" not in cfg.load_timeline_cache()["timelines"]
        # In memory nothing is lost: the session cache is not the disk cache.
        assert len(session.timelines["!a:hs"]) == 1

        session.set_space_cache("!s:hs", True)
        assert session.state["cache_spaces"] == {}  # inherit, not True

    def test_restore_skips_and_purges_disallowed_rooms(self, cfg, session):
        session.space_children["!s:hs"] = {"!a:hs"}
        session.archives["!a:hs"] = {"$1": self.msg("$1", 1000)}
        session._archive_dirty.add("!a:hs")
        session.timelines["!a:hs"].append(self.msg("$1", 1000))
        session.client.next_batch = "tok"
        session._save_timelines()

        fresh = MatrixSession(cfg)
        fresh.space_children["!s:hs"] = {"!a:hs"}
        fresh.state["cache_spaces"] = {"!s:hs": False}
        fresh.client.loaded_sync_token = "tok"
        fresh._restore_timelines(resume=True)
        assert "!a:hs" not in fresh.timelines
        assert "!a:hs" not in fresh.archives
        assert not cfg._room_archive_path("!a:hs").exists()

    def test_global_off_never_writes_and_wipes_at_restore(self, cfg, session):
        session.timelines["!a:hs"].append(self.msg("$1", 1000))
        session._save_timelines()
        assert cfg._timeline_cache_path.exists()

        session.cfg.cache_messages = False
        session._cache_dirty = True
        session._save_timelines()  # must be a no-op now
        session._restore_timelines(resume=True)  # wipes the leftovers
        assert not cfg._timeline_cache_path.exists()
        assert cfg.load_room_archives() == []

    def test_backfill_refuses_disallowed_rooms(self, session):
        async def main():
            session.state["cache_spaces"] = {"!s:hs": False}
            session.space_children["!s:hs"] = {"!a:hs"}
            session.start_backfill("!a:hs")
            return dict(session._backfill_tasks)

        assert asyncio.run(main()) == {}

    def test_v1_inline_archives_migrate_to_room_files(self, cfg, session):
        cfg.save_timeline_cache(
            {
                "version": 1,
                "user_id": ME,
                "next_batch": "tok",
                "archives": {
                    "!a:hs": [
                        {
                            "sender": ALICE,
                            "sender_name": "Alice",
                            "body": "old",
                            "ts": 1,
                            "event_id": "$1",
                        }
                    ]
                },
                "archive_done": ["!a:hs"],
            }
        )
        fresh = MatrixSession(cfg)
        fresh.client.loaded_sync_token = "tok"
        fresh._restore_timelines(resume=True)
        assert set(fresh.archives["!a:hs"]) == {"$1"}
        fresh._save_timelines()
        saved = cfg.load_room_archives()
        assert [p["room_id"] for p in saved] == ["!a:hs"]
        assert "archives" not in cfg.load_timeline_cache()
