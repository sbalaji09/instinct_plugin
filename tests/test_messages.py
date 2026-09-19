import json
import sqlite3
from datetime import datetime, timedelta

import pytest

from chatdb_fixture import ChatDB, blob
from instinct.adapters.messages import ChatNotFound, MapResolver, MessagesError, MessagesStore, normalize_handle

NOW = datetime.now().astimezone()


@pytest.fixture
def db(tmp_path):
    d = ChatDB(tmp_path / "chat.db")
    alice = d.handle("+18055550100")
    bob = d.handle("bob@example.com")
    carol = d.handle("+14155550199")
    c_alice = d.chat("+18055550100", [alice])
    c_bob = d.chat("bob@example.com", [bob])
    c_group = d.chat("chat123456", [alice, carol], display_name="Study Group")
    d.message(c_alice, NOW - timedelta(days=400), text="ancient seconds row", handle=alice, seconds=True)
    d.message(c_alice, NOW - timedelta(days=3), text="hey are you coming", handle=alice)
    d.message(c_alice, NOW - timedelta(days=3, minutes=-1), text="yes!", from_me=True)
    d.message(c_alice, NOW - timedelta(hours=2), body=blob("emoji_non_ascii"), handle=alice)
    d.message(c_alice, NOW - timedelta(hours=2, minutes=-1), text='Loved “héllo”', handle=alice, assoc=2000)
    d.message(c_alice, NOW - timedelta(hours=1), text=None, body=None, handle=alice, item_type=2)  # rename event
    d.message(c_bob, NOW - timedelta(days=1), body=blob("mutable_multi_run"), handle=bob)
    d.message(c_group, NOW - timedelta(minutes=30), body=blob("attachment_with_caption"), handle=carol,
              attachments=True)
    d.checkpoint()
    return d


@pytest.fixture
def store(db, tmp_path):
    resolver = MapResolver({"(805) 555-0100": "Alice Smith", "bob@example.com": "Bob"})
    s = MessagesStore(db.path, resolver, cursor_path=tmp_path / "home" / "cursor.json")
    yield s
    s.snapshot.close()


def test_never_opens_live_db(store, db, monkeypatch):
    opened = []
    real = sqlite3.connect
    monkeypatch.setattr(sqlite3, "connect", lambda *a, **k: opened.append(a[0]) or real(*a, **k))
    store.list_chats()
    assert opened and all(str(db.path) not in str(p) and "mode=ro" in str(p) for p in opened)


def test_list_chats_resolves_names_and_orders_by_recency(store):
    chats = store.list_chats()
    assert [c["name"] for c in chats] == ["Study Group", "Alice Smith", "Bob"]
    group = chats[0]
    assert group["is_group"]
    assert {p["name"] for p in group["participants"]} == {"Alice Smith", None}
    assert group["last_message"]["text"] == "[attachment]look at this"
    assert group["last_message"]["sender"] == "+14155550199"  # unresolved -> raw handle


def test_list_chats_query(store):
    assert [c["name"] for c in store.list_chats(query="alice")] == ["Study Group", "Alice Smith"]
    assert [c["name"] for c in store.list_chats(query="805-555-0100")] == ["Study Group", "Alice Smith"]
    assert store.list_chats(query="nobody") == []


def test_read_thread_decodes_bodies_and_skips_events(store):
    t = store.read_thread("Alice Smith")
    assert t["chat"]["name"] == "Alice Smith"
    texts = [m["text"] for m in t["messages"]]
    assert texts == ["ancient seconds row", "hey are you coming", "yes!", "héllo 👋🏽 日本語 — ça va? 🇺🇸",
                     "Loved “héllo”"]
    assert t["messages"][2]["sender"] == "me"
    assert t["messages"][-1]["reaction"] == "loved"
    # seconds-era row got a sane date
    assert t["messages"][0]["time"].startswith(str((NOW - timedelta(days=400)).year))


def test_read_thread_since_and_limit(store):
    t = store.read_thread("Alice Smith", since="1d")
    assert [m["text"] for m in t["messages"]] == ["héllo 👋🏽 日本語 — ça va? 🇺🇸", "Loved “héllo”"]
    t = store.read_thread("Alice Smith", limit=1)
    assert [m["text"] for m in t["messages"]] == ["Loved “héllo”"]


def test_read_thread_by_id_and_errors(store):
    cid = store.list_chats(query="Bob")[0]["id"]
    assert store.read_thread(f"chat:{cid}")["messages"][0]["text"] == "Meet at 5pm today?"
    with pytest.raises(ChatNotFound):
        store.read_thread("nobody")
    with pytest.raises(ChatNotFound):
        store.read_thread("chat:999")


def test_one_to_one_preferred_over_group(store):
    assert store.read_thread("alice")["chat"]["name"] == "Alice Smith"


def test_search_finds_text_only_in_attributed_body(store):
    hits = store.search("5PM")
    assert [h["text"] for h in hits] == ["Meet at 5pm today?"]
    assert hits[0]["chat_name"] == "Bob"
    assert [h["text"] for h in store.search("日本")] == ["héllo 👋🏽 日本語 — ça va? 🇺🇸"]
    assert store.search("héllo", chat="Bob") == []
    assert len(store.search("e", since="2d")) >= 2
    with pytest.raises(ValueError):
        store.search("  ")


def test_whats_new_cursor(store, db, tmp_path):
    first = store.whats_new(mark_seen=True)
    got = [m["text"] for g in first["chats"] for m in g["messages"]]
    # first run: incoming messages from the last 24h only
    assert sorted(got) == sorted(["héllo 👋🏽 日本語 — ça va? 🇺🇸", "Loved “héllo”", "[attachment]look at this"])
    assert json.loads((tmp_path / "home" / "cursor.json").read_text())["last_rowid"] == first["new_cursor"]

    assert store.whats_new()["count"] == 0

    alice_chat = store.list_chats(query="Alice Smith")[-1]["id"]
    db.message(alice_chat, NOW, text="new one", handle=1)
    db.message(alice_chat, NOW, text="my own reply", from_me=True)
    db.commit()  # left in the WAL, not checkpointed

    peek = store.whats_new(mark_seen=False)
    assert [m["text"] for g in peek["chats"] for m in g["messages"]] == ["new one"]
    assert store.whats_new(mark_seen=True)["count"] == 1
    assert store.whats_new()["count"] == 0


def test_whats_new_truncation_pages_forward(store, db):
    store.whats_new()  # set cursor
    cid = store.list_chats(query="Bob")[0]["id"]
    for i in range(5):
        db.message(cid, NOW, text=f"m{i}", handle=2)
    db.commit()
    a = store.whats_new(limit=3)
    assert a["truncated"] and [m["text"] for m in a["chats"][0]["messages"]] == ["m0", "m1", "m2"]
    b = store.whats_new(limit=3)
    assert not b["truncated"] and [m["text"] for m in b["chats"][0]["messages"]] == ["m3", "m4"]


def test_snapshot_sees_wal_only_rows_and_refreshes(store, db):
    before = len(store.read_thread("Bob")["messages"])
    cid = store.list_chats(query="Bob")[0]["id"]
    db.message(cid, NOW, text="only in wal", handle=2)
    db.commit()
    msgs = store.read_thread("Bob")["messages"]
    assert len(msgs) == before + 1 and msgs[-1]["text"] == "only in wal"


def test_missing_db(tmp_path):
    s = MessagesStore(tmp_path / "nope.db")
    with pytest.raises(MessagesError):
        s.list_chats()


def test_normalize_handle():
    assert normalize_handle("+1 (805) 555-0100") == normalize_handle("8055550100")
    assert normalize_handle("Bob@Example.com") == "bob@example.com"


def test_addressbook_db_resolver(tmp_path):
    from instinct.adapters.messages import AddressBookDBResolver, ChainResolver

    src = tmp_path / "AddressBook" / "Sources" / "ABC"
    src.mkdir(parents=True)
    con = sqlite3.connect(src / "AddressBook-v22.abcddb")
    con.executescript("""
        CREATE TABLE ZABCDRECORD (Z_PK INTEGER PRIMARY KEY, ZFIRSTNAME TEXT, ZLASTNAME TEXT, ZNICKNAME TEXT,
                                  ZORGANIZATION TEXT);
        CREATE TABLE ZABCDPHONENUMBER (Z_PK INTEGER PRIMARY KEY, ZOWNER INTEGER, ZFULLNUMBER TEXT);
        CREATE TABLE ZABCDEMAILADDRESS (Z_PK INTEGER PRIMARY KEY, ZOWNER INTEGER, ZADDRESS TEXT);
        INSERT INTO ZABCDRECORD VALUES (1, 'Priya', 'Rao', NULL, NULL), (2, NULL, NULL, NULL, 'Cal Poly IT');
        INSERT INTO ZABCDPHONENUMBER VALUES (1, 1, '(805) 555-0100'), (2, 2, '+1 805 756 0000');
        INSERT INTO ZABCDEMAILADDRESS VALUES (1, 1, 'Priya@Example.com');
    """)
    con.commit()
    con.close()
    r = ChainResolver(MapResolver({}), AddressBookDBResolver(tmp_path / "AddressBook"))
    assert r.name_for("+18055550100") == "Priya Rao"
    assert r.name_for("priya@example.com") == "Priya Rao"
    assert r.name_for("8057560000") == "Cal Poly IT"
    assert r.name_for("+15550000000") is None
    assert AddressBookDBResolver(tmp_path / "missing").name_for("+18055550100") is None
