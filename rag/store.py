"""SQLite storage + BM25 search for the mail archive.

One file, no vector database, no server to operate: SQLite's FTS5 module gives
BM25 ranking out of the box, and a mail archive is exactly the corpus where
keyword search is strong -- questions come with exact tokens in them (invoice
numbers, part numbers, PO references) that a dense embedding blurs together.

Swapping in embeddings later means replacing `search()` alone; everything above
it (the Dify contract) does not change.
"""

from __future__ import annotations

import sqlite3
import unicodedata
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS emails (
    id          INTEGER PRIMARY KEY,
    source      TEXT UNIQUE NOT NULL,   -- the .eml file this came from
    fingerprint TEXT NOT NULL,          -- sha1 of the file: re-ingest only on change
    subject     TEXT NOT NULL DEFAULT '',
    sender      TEXT NOT NULL DEFAULT '',
    recipient   TEXT NOT NULL DEFAULT '',
    sent_at     TEXT,
    body        TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS chunks (
    id       INTEGER PRIMARY KEY,
    email_id INTEGER NOT NULL REFERENCES emails(id) ON DELETE CASCADE,
    ordinal  INTEGER NOT NULL,
    text     TEXT NOT NULL
);

-- External-content FTS5 index: it indexes chunks.text without storing a second
-- copy, and `remove_diacritics 2` makes "cong ty" match "công ty", because
-- Vietnamese mail is typed both ways.
CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
    text,
    content='chunks',
    content_rowid='id',
    tokenize="unicode61 remove_diacritics 2"
);

CREATE TRIGGER IF NOT EXISTS chunks_ai AFTER INSERT ON chunks BEGIN
    INSERT INTO chunks_fts(rowid, text) VALUES (new.id, new.text);
END;

CREATE TRIGGER IF NOT EXISTS chunks_ad AFTER DELETE ON chunks BEGIN
    INSERT INTO chunks_fts(chunks_fts, rowid, text) VALUES ('delete', old.id, old.text);
END;
"""


def connect(db_path: str | Path) -> sqlite3.Connection:
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")      # reader (API) + writer (ingest) at once
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(SCHEMA)
    return conn


# Question words carry no signal but do carry IDF weight on a small archive, where
# a handful of documents is not enough for BM25 to discount them on its own.
_STOPWORDS_RAW = """
the what which who whom when where why how was were is are did does do for from and
any all with that this there have has had can could would should about on in at to of
it we you me my our your please need
là gì nào không có của cho với và thì được bao nhiêu ngày số ai đâu sao tại trong
"""


def fold(word: str) -> str:
    """Lowercase and strip Vietnamese tone marks, the way FTS5's `remove_diacritics 2`
    does on the index side -- so "công ty" and "cong ty" are the same token here too.
    `đ` has no decomposition, so it is mapped by hand."""
    decomposed = unicodedata.normalize("NFD", word.lower().replace("đ", "d"))
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch))


STOPWORDS = {fold(word) for word in _STOPWORDS_RAW.split()}


def fts_query(question: str) -> str:
    """A human question -> an FTS5 MATCH expression.

    Every token is quoted so "PO-48812" cannot be parsed as an FTS5 operator, and
    the tokens are OR-ed: a question is a bag of hints, and requiring all of them
    returns nothing for anything longer than a few words. BM25 does the
    discriminating instead -- a rare token like an invoice number dominates the
    ranking, common words barely move it.
    """
    words = [w for w in "".join(c if c.isalnum() else " " for c in question).split() if len(w) > 1]
    kept = [w.lower() for w in words if fold(w) not in STOPWORDS]
    unique = list(dict.fromkeys(kept or [w.lower() for w in words]))[:40]
    return " OR ".join(f'"{w}"' for w in unique)


def search(conn: sqlite3.Connection, question: str, top_k: int = 5, per_email: int = 2) -> list[dict]:
    """Best chunks for a question, at most `per_email` from any one message.

    The cap matters: retrieval returns several chunks of the same thread
    constantly, and uncapped, one chatty thread fills every slot while the
    message that actually holds the answer never reaches the model.
    """
    match = fts_query(question)
    if not match:
        return []

    rows = conn.execute(
        """
        SELECT c.id, c.ordinal, c.text, bm25(chunks_fts) AS rank,
               e.subject, e.sender, e.recipient, e.sent_at, e.source
          FROM chunks_fts
          JOIN chunks c ON c.id = chunks_fts.rowid
          JOIN emails e ON e.id = c.email_id
         WHERE chunks_fts MATCH ?
         ORDER BY rank            -- bm25() is negative; more negative is better
         LIMIT ?
        """,
        (match, max(top_k * 6, 30)),
    ).fetchall()

    best = max((-float(row["rank"]) for row in rows), default=0.0)

    hits, seen = [], {}
    for row in rows:
        if seen.get(row["source"], 0) >= per_email:
            continue
        seen[row["source"]] = seen.get(row["source"], 0) + 1
        hits.append({**dict(row), "score": _score(row["rank"], best)})
        if len(hits) >= top_k:
            break
    return hits


def _score(bm25_rank: float, best: float) -> float:
    """BM25 -> the 0..1 number Dify's `score_threshold` compares against.

    BM25 is unbounded and is not a probability, so this is strength *relative to
    the best hit for this query*, not a calibrated confidence: the top hit always
    scores 1.0. That makes the threshold mean something operators can reason about
    -- 0.2 drops anything less than a fifth as strong as the best hit -- instead of
    a number whose scale shifts with corpus size and query length.
    """
    strength = max(0.0, -float(bm25_rank))
    return round(strength / best, 4) if best > 0 else 0.0


def stats(conn: sqlite3.Connection) -> dict:
    emails = conn.execute("SELECT COUNT(*) FROM emails").fetchone()[0]
    chunks = conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
    return {"emails": emails, "chunks": chunks}
