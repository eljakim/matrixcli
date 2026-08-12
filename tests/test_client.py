import asyncio
from types import SimpleNamespace

import aiohttp
from nio.events.room_events import RoomMessageText

from matrixcli.client import Message, fold_edits

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


class TestRefreshDirectMap:
    def run(self, session, payload, monkeypatch, status=200):
        fake = TestRefreshSpaceChildren.FakeHttp(payload, status=status)
        monkeypatch.setattr("matrixcli.client.aiohttp.ClientSession", lambda: fake)
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

    def run(self, session, monkeypatch, syncs, get_displayname=None):
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
        monkeypatch.setattr(session, "refresh_space_children", noop)
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
