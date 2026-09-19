"""Messages adapter (read-only) backed by a snapshot of ~/Library/Messages/chat.db.

Invariants:
- The live database is never opened. chat.db and its -wal / -shm sidecars are
  copied into a private temp dir and the copy is opened with a read-only URI.
  Uncheckpointed messages live in the -wal file, so copying it matters.
- Messages.app is never touched here (sending lives in imessage_send.py, behind the gate).
- Message text comes from `text`, falling back to decoding `attributedBody`
  (see typedstream.py).
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import sqlite3
import tempfile
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from pathlib import Path

from instinct.adapters.typedstream import message_text
from instinct.logs import redact
from instinct.timeutil import apple_to_datetime, datetime_to_apple_ns, datetime_to_apple_s, iso_local, parse_since

log = logging.getLogger("instinct.messages")

SIDECARS = ("-wal", "-shm")
# associated_message_type 2000-2006 are tapbacks, 3000-3006 their removals.
REACTIONS = {2000: "loved", 2001: "liked", 2002: "disliked", 2003: "laughed at",
             2004: "emphasized", 2005: "questioned", 2006: "reacted to"}
SEARCH_SCAN_CAP = 50_000


class MessagesError(RuntimeError):
    pass


class ChatNotFound(MessagesError):
    pass


# --------------------------------------------------------------------------- snapshot


def _clone_or_copy(src: Path, dst: Path) -> None:
    """APFS clonefile(2) (instant, copy-on-write) with a plain copy as fallback.

    chat.db is often hundreds of MB, and the bridge re-snapshots it every time it
    changes, so a real copy each time would be slow and churn the disk.
    """
    try:
        import ctypes

        libc = ctypes.CDLL(None, use_errno=True)
        if libc.clonefile(os.fsencode(src), os.fsencode(dst), 0) == 0:
            return
    except (OSError, AttributeError):
        pass
    shutil.copyfile(src, dst)


class Snapshot:
    """Private read-only copy of chat.db, refreshed only when the source changes."""

    def __init__(self, source: Path):
        self.source = Path(source)
        self._dir: Path | None = None
        self._sig: tuple | None = None
        self._lock = threading.Lock()

    def _signature(self) -> tuple:
        sig = []
        for suffix in ("", *SIDECARS):
            p = Path(f"{self.source}{suffix}")
            try:
                st = p.stat()
                sig.append((suffix, st.st_mtime_ns, st.st_size))
            except FileNotFoundError:
                sig.append((suffix, None, None))
        return tuple(sig)

    def path(self) -> Path:
        with self._lock:
            try:
                sig = self._signature()
                if not self.source.exists():
                    raise MessagesError(f"{self.source} not found")
            except PermissionError as e:
                raise MessagesError(
                    "Permission denied reading chat.db. Grant Full Disk Access to the app that "
                    "runs the server (see `uv run instinct doctor`)."
                ) from e
            if self._dir is None or sig != self._sig:
                self._copy()
                self._sig = sig
            return self._dir / "chat.db"

    def _copy(self) -> None:
        new = Path(tempfile.mkdtemp(prefix="instinct-chatdb-"))
        os.chmod(new, 0o700)
        try:
            for suffix in ("", *SIDECARS):
                src = Path(f"{self.source}{suffix}")
                if src.exists():
                    _clone_or_copy(src, new / f"chat.db{suffix}")
        except PermissionError as e:
            shutil.rmtree(new, ignore_errors=True)
            raise MessagesError(
                "Permission denied copying chat.db. Grant Full Disk Access to the app that runs "
                "the server (see `uv run instinct doctor`)."
            ) from e
        old, self._dir = self._dir, new
        if old:
            shutil.rmtree(old, ignore_errors=True)
        log.info("refreshed chat.db snapshot")

    def connect(self) -> sqlite3.Connection:
        db = self.path()
        # mode=ro: the copy is never written. The sidecars sit next to it and are
        # writable (they're ours), which SQLite needs to read a WAL database.
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, check_same_thread=False)
        con.row_factory = sqlite3.Row
        return con

    def close(self) -> None:
        if self._dir:
            shutil.rmtree(self._dir, ignore_errors=True)
            self._dir = None


# --------------------------------------------------------------------------- contacts


def normalize_handle(handle: str) -> str:
    """Canonical key for matching Messages handles to Contacts entries."""
    h = handle.strip().lower()
    if "@" in h:
        return h
    digits = re.sub(r"\D", "", h)
    # Compare on the last 10 digits so +1 (805) 555-0100 == 8055550100.
    return digits[-10:] if len(digits) >= 10 else digits


class ContactResolver:
    """Maps handles (phone/email) to names. Base class resolves nothing."""

    def name_for(self, handle: str | None) -> str | None:
        return None


class MapResolver(ContactResolver):
    def __init__(self, mapping: dict[str, str]):
        self._map = {normalize_handle(k): v for k, v in mapping.items()}

    def name_for(self, handle: str | None) -> str | None:
        return self._map.get(normalize_handle(handle)) if handle else None


class MacContactsResolver(ContactResolver):
    """Contacts.framework lookup; silently does nothing without permission."""

    TTL_S = 600

    def __init__(self):
        self._map: dict[str, str] | None = None
        self._loaded_at = 0.0
        self._lock = threading.Lock()

    @staticmethod
    def authorized() -> bool:
        try:
            import Contacts
        except ImportError:
            return False
        status = Contacts.CNContactStore.authorizationStatusForEntityType_(Contacts.CNEntityTypeContacts)
        return int(status) in (3, 4)  # authorized, limited

    def _load(self) -> dict[str, str]:
        import Contacts

        keys = [Contacts.CNContactGivenNameKey, Contacts.CNContactFamilyNameKey,
                Contacts.CNContactNicknameKey, Contacts.CNContactOrganizationNameKey,
                Contacts.CNContactPhoneNumbersKey, Contacts.CNContactEmailAddressesKey]
        req = Contacts.CNContactFetchRequest.alloc().initWithKeysToFetch_(keys)
        mapping: dict[str, str] = {}

        def visit(contact, _stop):
            name = " ".join(p for p in (contact.givenName(), contact.familyName()) if p).strip()
            name = name or contact.nickname() or contact.organizationName()
            if not name:
                return
            for pn in contact.phoneNumbers() or []:
                mapping.setdefault(normalize_handle(pn.value().stringValue()), name)
            for em in contact.emailAddresses() or []:
                mapping.setdefault(normalize_handle(str(em.value())), name)

        store = Contacts.CNContactStore.alloc().init()
        ok, err = store.enumerateContactsWithFetchRequest_error_usingBlock_(req, None, visit)
        if not ok:
            log.warning("contacts enumeration failed: %s", err)
        return mapping

    def name_for(self, handle: str | None) -> str | None:
        if not handle:
            return None
        with self._lock:
            if self._map is None or time.monotonic() - self._loaded_at > self.TTL_S:
                self._map = self._load() if self.authorized() else {}
                self._loaded_at = time.monotonic()
            return self._map.get(normalize_handle(handle))


class AddressBookDBResolver(ContactResolver):
    """Read names from the local Contacts database (needs only Full Disk Access).

    Command-line hosts often can't trigger the Contacts permission prompt, so this
    reads ~/Library/Application Support/AddressBook/**/AddressBook-v22.abcddb
    (private Core Data schema) from private read-only copies, like chat.db.
    """

    TTL_S = 600

    def __init__(self, root: Path | None = None):
        self.root = root or Path("~/Library/Application Support/AddressBook").expanduser()
        self._map: dict[str, str] | None = None
        self._loaded_at = 0.0
        self._lock = threading.Lock()

    def _databases(self) -> list[Path]:
        try:
            return sorted(self.root.rglob("AddressBook-v22.abcddb"))
        except OSError:
            return []

    @staticmethod
    def _read(db: Path, mapping: dict[str, str]) -> None:
        with tempfile.TemporaryDirectory(prefix="instinct-ab-") as tmp:
            for suffix in ("", *SIDECARS):
                src = Path(f"{db}{suffix}")
                if src.exists():
                    shutil.copyfile(src, Path(tmp) / f"ab.db{suffix}")
            con = sqlite3.connect(f"file:{Path(tmp) / 'ab.db'}?mode=ro", uri=True)
            try:
                cols = {r[1] for r in con.execute("PRAGMA table_info(ZABCDRECORD)")}
                if not {"Z_PK", "ZFIRSTNAME", "ZLASTNAME"} <= cols:
                    return
                extra = [c for c in ("ZNICKNAME", "ZORGANIZATION") if c in cols]
                names = {}
                for row in con.execute(f"SELECT Z_PK, ZFIRSTNAME, ZLASTNAME{''.join(', ' + c for c in extra)} "
                                       "FROM ZABCDRECORD"):
                    full = " ".join(p for p in row[1:3] if p).strip()
                    full = full or next((p for p in row[3:] if p), None)
                    if full:
                        names[row[0]] = full
                for table, col in (("ZABCDPHONENUMBER", "ZFULLNUMBER"), ("ZABCDEMAILADDRESS", "ZADDRESS")):
                    try:
                        for owner, value in con.execute(f"SELECT ZOWNER, {col} FROM {table}"):
                            if owner in names and value:
                                mapping.setdefault(normalize_handle(value), names[owner])
                    except sqlite3.Error:
                        continue
            finally:
                con.close()

    def name_for(self, handle: str | None) -> str | None:
        if not handle:
            return None
        with self._lock:
            if self._map is None or time.monotonic() - self._loaded_at > self.TTL_S:
                mapping: dict[str, str] = {}
                for db in self._databases():
                    try:
                        self._read(db, mapping)
                    except (OSError, sqlite3.Error) as e:
                        log.warning("address book read failed: %s", type(e).__name__)
                self._map, self._loaded_at = mapping, time.monotonic()
            return self._map.get(normalize_handle(handle))


class ChainResolver(ContactResolver):
    def __init__(self, *resolvers: ContactResolver):
        self.resolvers = resolvers

    def name_for(self, handle: str | None) -> str | None:
        for r in self.resolvers:
            if name := r.name_for(handle):
                return name
        return None


# --------------------------------------------------------------------------- store


@dataclass
class Message:
    id: int
    chat_id: int
    time: str | None
    sender: str
    sender_handle: str | None
    is_from_me: bool
    text: str | None
    has_attachments: bool
    reaction: str | None = None


_MSG_SELECT = """
SELECT m.ROWID AS rowid, cmj.chat_id AS chat_id, m.text AS text, m.attributedBody AS body,
       m.date AS date, m.is_from_me AS is_from_me, h.id AS handle,
       m.cache_has_attachments AS has_att, m.associated_message_type AS assoc
FROM message m
JOIN chat_message_join cmj ON cmj.message_id = m.ROWID
LEFT JOIN handle h ON h.ROWID = m.handle_id
WHERE m.item_type = 0
"""


def _since_clause(since: datetime | None) -> tuple[str, list]:
    if since is None:
        return "", []
    # Rows are nanoseconds on modern macOS, seconds on very old ones; match both.
    return (" AND ((m.date > 10000000000 AND m.date >= ?) OR (m.date <= 10000000000 AND m.date >= ?))",
            [datetime_to_apple_ns(since), datetime_to_apple_s(since)])


class MessagesStore:
    def __init__(self, db_path: Path, resolver: ContactResolver | None = None,
                 cursor_path: Path | None = None):
        self.snapshot = Snapshot(db_path)
        self.resolver = resolver or ContactResolver()
        self.cursor_path = cursor_path

    # ---------------------------------------------------------------- helpers

    @contextmanager
    def _con(self) -> Iterator[sqlite3.Connection]:
        con = self.snapshot.connect()
        try:
            yield con
        finally:
            con.close()

    def _display(self, handle: str | None) -> str:
        if not handle:
            return "unknown"
        return self.resolver.name_for(handle) or handle

    def _participants(self, con, chat_id: int) -> list[str]:
        rows = con.execute(
            "SELECT h.id FROM chat_handle_join chj JOIN handle h ON h.ROWID = chj.handle_id "
            "WHERE chj.chat_id = ? ORDER BY h.id", (chat_id,)).fetchall()
        return [r[0] for r in rows]

    def _chat_name(self, row, participants: list[str]) -> str:
        if row["display_name"]:
            return row["display_name"]
        if participants:
            return ", ".join(self._display(h) for h in participants)
        return self._display(row["chat_identifier"])

    def _message(self, row) -> Message:
        assoc = row["assoc"] or 0
        reaction = None
        if 2000 <= assoc < 2100:
            reaction = REACTIONS.get(assoc, "reacted to")
        elif 3000 <= assoc < 3100:
            reaction = "removed reaction"
        from_me = bool(row["is_from_me"])
        return Message(
            id=row["rowid"],
            chat_id=row["chat_id"],
            time=iso_local(apple_to_datetime(row["date"])),
            sender="me" if from_me else self._display(row["handle"]),
            sender_handle=None if from_me else row["handle"],
            is_from_me=from_me,
            text=message_text(row["text"], row["body"]),
            has_attachments=bool(row["has_att"]),
            reaction=reaction,
        )

    def _chats(self, con) -> Iterator[dict]:
        rows = con.execute("""
            SELECT c.ROWID AS id, c.guid, c.chat_identifier, c.display_name, c.service_name,
                   (SELECT MAX(m.date) FROM chat_message_join cmj JOIN message m ON m.ROWID = cmj.message_id
                     WHERE cmj.chat_id = c.ROWID) AS last_date
            FROM chat c ORDER BY last_date IS NULL, last_date DESC
        """).fetchall()
        for r in rows:
            parts = self._participants(con, r["id"])
            yield {
                "id": r["id"],
                "guid": r["guid"],
                "name": self._chat_name(r, parts),
                "identifier": r["chat_identifier"],
                "service": r["service_name"],
                "is_group": len(parts) > 1,
                "participants": [{"handle": h, "name": self.resolver.name_for(h)} for h in parts],
                "last_message_at": iso_local(apple_to_datetime(r["last_date"])),
            }

    @staticmethod
    def _chat_matches(chat: dict, query: str) -> bool:
        q = query.strip().lower()
        hay = [chat["name"], chat["identifier"] or ""]
        for p in chat["participants"]:
            hay += [p["handle"], p["name"] or ""]
        if any(q in h.lower() for h in hay if h):
            return True
        # phone numbers typed with different punctuation
        qd = re.sub(r"\D", "", q)
        return len(qd) >= 7 and any(qd[-10:] in re.sub(r"\D", "", p["handle"]) for p in chat["participants"])

    def resolve_chat(self, con, chat: str | int) -> dict:
        chats = list(self._chats(con))
        s = str(chat).strip()
        if s.removeprefix("chat:").isdigit():
            cid = int(s.removeprefix("chat:"))
            for c in chats:
                if c["id"] == cid:
                    return c
            raise ChatNotFound(f"no chat with id {cid}")
        exact = [c for c in chats if s.lower() in {c["name"].lower(), (c["identifier"] or "").lower()}]
        matches = exact or [c for c in chats if self._chat_matches(c, s)]
        if not matches:
            raise ChatNotFound(f"no chat matching {s!r}; try list_chats(query=...)")
        # Several chats with one person (SMS + iMessage, or 1:1 + groups): prefer the
        # most recently active 1:1 chat, else the most recent match.
        one_to_one = [c for c in matches if not c["is_group"]]
        return (one_to_one or matches)[0]

    # ---------------------------------------------------------------- tools

    def list_chats(self, query: str | None = None, limit: int = 20) -> list[dict]:
        with self._con() as con:
            out = []
            for c in self._chats(con):
                if query and not self._chat_matches(c, query):
                    continue
                last = con.execute(_MSG_SELECT + " AND cmj.chat_id = ? ORDER BY m.date DESC LIMIT 1",
                                   (c["id"],)).fetchone()
                c["last_message"] = asdict(self._message(last)) if last else None
                out.append(c)
                if len(out) >= limit:
                    break
            return out

    def read_thread(self, chat: str | int, since: str | None = None, limit: int = 50) -> dict:
        since_dt = parse_since(since)
        with self._con() as con:
            c = self.resolve_chat(con, chat)
            clause, params = _since_clause(since_dt)
            rows = con.execute(
                _MSG_SELECT + " AND cmj.chat_id = ?" + clause + " ORDER BY m.date DESC, m.ROWID DESC LIMIT ?",
                [c["id"], *params, limit]).fetchall()
        msgs = [asdict(self._message(r)) for r in reversed(rows)]
        log.info("read_thread chat=%s -> %d messages", c["id"], len(msgs))
        return {"chat": {k: c[k] for k in ("id", "name", "is_group", "participants")}, "messages": msgs}

    def search(self, text: str, chat: str | int | None = None, since: str | None = None,
               limit: int = 20) -> list[dict]:
        if not text.strip():
            raise ValueError("search text must not be empty")
        needle = text.casefold()
        since_dt = parse_since(since)
        with self._con() as con:
            sql, params = _MSG_SELECT, []
            names = {}
            if chat is not None:
                c = self.resolve_chat(con, chat)
                sql += " AND cmj.chat_id = ?"
                params.append(c["id"])
                names[c["id"]] = c["name"]
            clause, p2 = _since_clause(since_dt)
            sql += clause + " ORDER BY m.date DESC LIMIT ?"
            # Text is often only in attributedBody, which SQL can't search reliably,
            # so decode and match in Python over a bounded window of recent rows.
            params += [*p2, SEARCH_SCAN_CAP]
            hits = []
            for row in con.execute(sql, params):
                t = message_text(row["text"], row["body"])
                if t and needle in t.casefold():
                    hits.append(self._message(row))
                    if len(hits) >= limit:
                        break
            if not names:
                names = {c["id"]: c["name"] for c in self._chats(con)}
        log.info("search %s -> %d hits", redact(text), len(hits))
        return [{**asdict(m), "chat_name": names.get(m.chat_id)} for m in hits]

    def max_rowid(self) -> int:
        with self._con() as con:
            return con.execute("SELECT COALESCE(MAX(ROWID), 0) FROM message").fetchone()[0]

    def messages_after(self, rowid: int, chat_ids: list[int], limit: int = 200) -> list[Message]:
        """Messages (from anyone, including me) in `chat_ids` with ROWID > rowid, oldest first."""
        if not chat_ids:
            return []
        marks = ",".join("?" * len(chat_ids))
        with self._con() as con:
            rows = con.execute(_MSG_SELECT + f" AND m.ROWID > ? AND cmj.chat_id IN ({marks})"
                               " ORDER BY m.ROWID ASC LIMIT ?", [rowid, *chat_ids, limit]).fetchall()
            return [self._message(r) for r in rows]

    def find_chat(self, chat: str | int) -> dict:
        with self._con() as con:
            return self.resolve_chat(con, chat)

    def chats_with(self, handle_or_name: str) -> list[dict]:
        """Every 1:1 chat with one person (a phone number, email, or contact name), newest first."""
        with self._con() as con:
            return [c for c in self._chats(con) if not c["is_group"] and self._chat_matches(c, handle_or_name)]

    def _read_cursor(self) -> int | None:
        if not self.cursor_path or not self.cursor_path.exists():
            return None
        try:
            return int(json.loads(self.cursor_path.read_text())["last_rowid"])
        except (ValueError, KeyError, json.JSONDecodeError):
            return None

    def _write_cursor(self, rowid: int) -> None:
        if not self.cursor_path:
            return
        self.cursor_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        tmp = self.cursor_path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"last_rowid": rowid, "updated": datetime.now().astimezone().isoformat()}))
        tmp.replace(self.cursor_path)

    def whats_new(self, mark_seen: bool = True, limit: int = 100,
                  first_run_window: timedelta = timedelta(hours=24)) -> dict:
        """Incoming messages with ROWID above the saved cursor, grouped by chat.

        First run (no cursor): messages from the last 24h. Returned oldest-first; if
        more than `limit` are pending, `truncated` is true and the cursor only
        advances past what was returned, so calling again pages forward.
        """
        cursor = self._read_cursor()
        with self._con() as con:
            max_rowid = con.execute("SELECT COALESCE(MAX(ROWID), 0) FROM message").fetchone()[0]
            if cursor is None:
                clause, params = _since_clause(datetime.now().astimezone() - first_run_window)
                sql, params = _MSG_SELECT + " AND m.is_from_me = 0" + clause, params
            else:
                sql, params = _MSG_SELECT + " AND m.is_from_me = 0 AND m.ROWID > ?", [cursor]
            rows = con.execute(sql + " ORDER BY m.ROWID ASC LIMIT ?", [*params, limit + 1]).fetchall()
            truncated = len(rows) > limit
            rows = rows[:limit]
            new_cursor = rows[-1]["rowid"] if truncated else max_rowid
            chat_ids = {r["chat_id"] for r in rows}
            chats = {c["id"]: c for c in self._chats(con) if c["id"] in chat_ids}
        grouped: dict[int, dict] = {}
        for r in rows:
            m = self._message(r)
            g = grouped.setdefault(m.chat_id, {"chat_id": m.chat_id,
                                               "chat_name": chats.get(m.chat_id, {}).get("name"),
                                               "messages": []})
            g["messages"].append(asdict(m))
        if mark_seen:
            self._write_cursor(new_cursor)
        log.info("whats_new cursor=%s -> %d messages in %d chats (mark_seen=%s)",
                 cursor, len(rows), len(grouped), mark_seen)
        return {
            "since_cursor": cursor,
            "new_cursor": new_cursor if mark_seen else cursor,
            "count": len(rows),
            "truncated": truncated,
            "chats": sorted(grouped.values(), key=lambda g: g["messages"][-1]["time"] or "", reverse=True),
        }


def default_store(cfg) -> MessagesStore:
    import atexit

    resolver: ContactResolver = (ChainResolver(MacContactsResolver(), AddressBookDBResolver())
                                 if cfg.messages.resolve_contacts else ContactResolver())
    store = MessagesStore(cfg.messages.db_path, resolver, cursor_path=cfg.home / "messages_cursor.json")
    atexit.register(store.snapshot.close)  # remove the private copy of chat.db on exit
    return store


__all__ = ["MessagesStore", "MessagesError", "ChatNotFound", "Snapshot", "MapResolver",
           "ContactResolver", "MacContactsResolver", "normalize_handle", "default_store"]
