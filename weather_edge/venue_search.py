"""Narrow selective substring searches against the two-venue SQLite catalog."""
from __future__ import annotations

import sqlite3


FIELDS = ("instrument_id", "event_id", "series_id", "title", "event_title", "label", "station", "region")
TABLE = "market_text_index"
SELECTIVE_LIMIT = 50_000


def _present(db):
    return db.execute("SELECT 1 FROM sqlite_schema WHERE type='table' AND name=?", (TABLE,)).fetchone() is not None


def ensure(db):
    if _present(db):
        return True
    columns = ",".join(FIELDS)
    old = ",".join(f"old.{name}" for name in FIELDS)
    new = ",".join(f"new.{name}" for name in FIELDS)
    changed = " OR ".join(f"old.{name} IS NOT new.{name}" for name in FIELDS)
    remove = f"INSERT INTO {TABLE}({TABLE},rowid,{columns}) VALUES('delete',old.rowid,{old});"
    append = f"INSERT INTO {TABLE}(rowid,{columns}) VALUES(new.rowid,{new});"
    try:
        with db:
            db.execute("BEGIN IMMEDIATE")
            if _present(db):
                return True
            db.execute(f"CREATE VIRTUAL TABLE {TABLE} USING fts5({columns}, content='markets', content_rowid='rowid', tokenize='trigram')")
            db.execute(f"INSERT INTO {TABLE}({TABLE}) VALUES('rebuild')")
            db.execute(f"CREATE TRIGGER market_text_add AFTER INSERT ON markets BEGIN {append} END")
            db.execute(f"CREATE TRIGGER market_text_remove AFTER DELETE ON markets BEGIN {remove} END")
            db.execute(f"CREATE TRIGGER market_text_change AFTER UPDATE ON markets WHEN old.rowid IS NOT new.rowid OR {changed} "
                       f"BEGIN {remove} {append} END")
    except sqlite3.OperationalError as issue:
        if "no such module" in str(issue) or "no such tokenizer" in str(issue):
            return False
        raise
    return True


def narrow(db, where, values, text):
    if not text or len(text) < 3 or not text.isascii() or not text.isprintable() or not _present(db):
        return where, values
    phrase = '"' + text.replace('"', '""') + '"'
    matches = f"SELECT rowid FROM {TABLE} WHERE {TABLE} MATCH ?"
    try:
        count = db.execute(f"SELECT count(*) FROM ({matches} LIMIT ?)", (phrase, SELECTIVE_LIMIT + 1)).fetchone()[0]
    except sqlite3.OperationalError as issue:
        if "no such module" in str(issue) or "no such tokenizer" in str(issue):
            return where, values
        raise
    if count > SELECTIVE_LIMIT:
        return where, values
    # The catalog's ordinary literal predicates remain the authority for every match.
    return f"{where} AND rowid IN ({matches})", [*values, phrase]
