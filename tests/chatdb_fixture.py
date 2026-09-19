"""Build a synthetic chat.db with the subset of Apple's schema the adapter reads."""

from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path

from instinct.timeutil import datetime_to_apple_ns, datetime_to_apple_s

FIX = Path(__file__).parent / "fixtures" / "attributed_body"

SCHEMA = """
CREATE TABLE handle (ROWID INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT NOT NULL, country TEXT,
    service TEXT NOT NULL, uncanonicalized_id TEXT, person_centric_id TEXT);
CREATE TABLE chat (ROWID INTEGER PRIMARY KEY AUTOINCREMENT, guid TEXT UNIQUE NOT NULL, style INTEGER,
    state INTEGER, account_id TEXT, properties BLOB, chat_identifier TEXT, service_name TEXT,
    room_name TEXT, account_login TEXT, is_archived INTEGER DEFAULT 0, last_addressed_handle TEXT,
    display_name TEXT, group_id TEXT, is_filtered INTEGER DEFAULT 0);
CREATE TABLE message (ROWID INTEGER PRIMARY KEY AUTOINCREMENT, guid TEXT UNIQUE NOT NULL, text TEXT,
    replace INTEGER DEFAULT 0, service_center TEXT, handle_id INTEGER DEFAULT 0, subject TEXT,
    country TEXT, attributedBody BLOB, version INTEGER DEFAULT 0, type INTEGER DEFAULT 0,
    service TEXT, account TEXT, date INTEGER, date_read INTEGER, date_delivered INTEGER,
    is_from_me INTEGER DEFAULT 0, item_type INTEGER DEFAULT 0, cache_has_attachments INTEGER DEFAULT 0,
    associated_message_guid TEXT, associated_message_type INTEGER DEFAULT 0);
CREATE TABLE chat_handle_join (chat_id INTEGER REFERENCES chat (ROWID) ON DELETE CASCADE,
    handle_id INTEGER REFERENCES handle (ROWID) ON DELETE CASCADE, UNIQUE(chat_id, handle_id));
CREATE TABLE chat_message_join (chat_id INTEGER REFERENCES chat (ROWID) ON DELETE CASCADE,
    message_id INTEGER REFERENCES message (ROWID) ON DELETE CASCADE, message_date INTEGER DEFAULT 0,
    PRIMARY KEY (chat_id, message_id));
CREATE INDEX chat_message_join_idx_chat_id ON chat_message_join(chat_id);
"""


def blob(name: str) -> bytes:
    return (FIX / f"{name}.bin").read_bytes()


class ChatDB:
    """Writer for a fake chat.db; keep it open to leave rows in the -wal file."""

    def __init__(self, path: Path):
        self.path = path
        self.con = sqlite3.connect(path, check_same_thread=False)  # the bridge tests write from a worker thread
        self.con.execute("PRAGMA journal_mode=WAL")
        self.con.execute("PRAGMA wal_autocheckpoint=0")
        self.con.executescript(SCHEMA)
        self._n = 0

    def handle(self, ident: str, service: str = "iMessage") -> int:
        return self.con.execute("INSERT INTO handle (id, service) VALUES (?, ?)", (ident, service)).lastrowid

    def chat(self, identifier: str, handles: list[int], display_name: str | None = None) -> int:
        cid = self.con.execute(
            "INSERT INTO chat (guid, chat_identifier, service_name, display_name) VALUES (?, ?, 'iMessage', ?)",
            (f"iMessage;-;{identifier}", identifier, display_name)).lastrowid
        for h in handles:
            self.con.execute("INSERT INTO chat_handle_join VALUES (?, ?)", (cid, h))
        return cid

    def message(self, chat_id: int, when: datetime, *, text: str | None = None, body: bytes | None = None,
                handle: int = 0, from_me: bool = False, seconds: bool = False, item_type: int = 0,
                assoc: int = 0, attachments: bool = False) -> int:
        self._n += 1
        date = datetime_to_apple_s(when) if seconds else datetime_to_apple_ns(when)
        mid = self.con.execute(
            "INSERT INTO message (guid, text, attributedBody, handle_id, date, is_from_me, item_type, "
            "associated_message_type, cache_has_attachments, service) VALUES (?,?,?,?,?,?,?,?,?, 'iMessage')",
            (f"guid-{self._n}", text, body, handle, date, int(from_me), item_type, assoc, int(attachments)),
        ).lastrowid
        self.con.execute("INSERT INTO chat_message_join VALUES (?, ?, ?)", (chat_id, mid, date))
        return mid

    def commit(self) -> None:
        self.con.commit()

    def checkpoint(self) -> None:
        self.con.commit()
        self.con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
