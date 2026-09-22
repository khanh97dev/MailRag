"""Ingest: .eml files (or a maildir) -> chunks in SQLite.

Standard library only -- Python already parses MIME better than anything I would
write. Ingest is idempotent: each file is fingerprinted with its sha1, unchanged
files are skipped and changed ones have their chunks rebuilt, so this is safe to
run from cron against a live maildir.

    python -m rag.ingest samples/
"""

from __future__ import annotations

import hashlib
import html
import os
import re
import sys
from email import policy
from email.parser import BytesParser
from email.utils import parsedate_to_datetime
from pathlib import Path

from .store import connect, stats

CHUNK_SIZE = int(os.getenv("MAILRAG_CHUNK_SIZE", "1200"))
CHUNK_OVERLAP = int(os.getenv("MAILRAG_CHUNK_OVERLAP", "200"))
SKIP_DIRS = {".Trash", ".Drafts", ".Templates", "cur.bak"}


def html_to_text(raw: str) -> str:
    """Enough HTML stripping for mail bodies: structure kept as line breaks."""
    text = re.sub(r"(?is)<(script|style|head)[^>]*>.*?</\1>", " ", raw)
    text = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</tr>", "\n", text)
    text = re.sub(r"(?i)</td>", " | ", text)
    text = re.sub(r"<[^>]+>", " ", text)
    text = html.unescape(text)
    text = text.replace("\xa0", " ")
    text = re.sub(r"[ \t]+", " ", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def parse_email(path: Path) -> dict:
    with path.open("rb") as handle:
        message = BytesParser(policy=policy.default).parse(handle)

    body = ""
    plain = message.get_body(preferencelist=("plain",))
    if plain is not None:
        body = str(plain.get_content()).strip()
    if not body:
        rich = message.get_body(preferencelist=("html",))
        if rich is not None:
            body = html_to_text(str(rich.get_content()))

    sent_at = None
    if message["Date"]:
        try:
            sent_at = parsedate_to_datetime(message["Date"]).isoformat()
        except (TypeError, ValueError):
            sent_at = None

    attachments = [
        part.get_filename()
        for part in message.iter_attachments()
        if part.get_filename()
    ]
    if attachments:
        # File names are often the answer ("which quote did they attach?"), so they
        # belong in the searchable text even though the bytes are dropped.
        body += "\n\nAttachments: " + ", ".join(attachments)

    return {
        "subject": (message["Subject"] or "").strip(),
        "sender": (message["From"] or "").strip(),
        "recipient": (message["To"] or "").strip(),
        "sent_at": sent_at,
        "body": body,
    }


def chunk(text: str, header: str) -> list[str]:
    """Paragraph-aligned chunks with a little overlap, each carrying `header`.

    The overlap keeps facts that straddle a boundary intact (a part number on one
    line, its price on the next). The header -- subject / sender / date -- is
    repeated in every chunk because chunks are retrieved in isolation: without it
    only the first chunk of a thread can answer "who sent this, and when?".
    """
    paragraphs: list[str] = []
    for block in (p.strip() for p in re.split(r"\n\s*\n", text)):
        while len(block) > CHUNK_SIZE:              # a pasted table, typically
            cut = block.rfind(" ", CHUNK_SIZE // 2, CHUNK_SIZE)
            cut = cut if cut > 0 else CHUNK_SIZE
            paragraphs.append(block[:cut].strip())
            block = block[cut:].strip()
        if block:
            paragraphs.append(block)

    chunks: list[str] = []
    current = ""

    for paragraph in paragraphs:
        if not current:
            current = paragraph
        elif len(current) + len(paragraph) + 2 <= CHUNK_SIZE:
            current += "\n\n" + paragraph
        else:
            chunks.append(current)
            current = current[-CHUNK_OVERLAP:].split(" ", 1)[-1] + "\n\n" + paragraph

    if current.strip():
        chunks.append(current)

    return [f"{header}\n\n{c.strip()}" for c in chunks] or [header]


def discover(root: Path) -> list[Path]:
    if root.is_file():
        return [root]
    files = [
        path
        for path in sorted(root.rglob("*"))
        if path.is_file()
        and not path.name.startswith(".")
        and not SKIP_DIRS & set(path.parts)
        and path.suffix.lower() in {".eml", ""}
    ]
    return files


def ingest(db_path: str, roots: list[str]) -> dict:
    conn = connect(db_path)
    counts = {"added": 0, "updated": 0, "skipped": 0, "chunks": 0}

    for root in roots:
        for path in discover(Path(root)):
            source = str(path.resolve())
            fingerprint = hashlib.sha1(path.read_bytes()).hexdigest()

            row = conn.execute("SELECT id, fingerprint FROM emails WHERE source = ?", (source,)).fetchone()
            if row and row["fingerprint"] == fingerprint:
                counts["skipped"] += 1
                continue

            mail = parse_email(path)
            header = " | ".join(
                part
                for part in (
                    f"Subject: {mail['subject']}" if mail["subject"] else "",
                    f"From: {mail['sender']}" if mail["sender"] else "",
                    f"To: {mail['recipient']}" if mail["recipient"] else "",
                    f"Date: {mail['sent_at'][:10]}" if mail["sent_at"] else "",
                )
                if part
            )
            pieces = chunk(mail["body"], header)

            with conn:
                if row:
                    # Rebuilt wholesale, not diffed: chunk boundaries move when the
                    # body changes, so a partial update leaves stale text indexed.
                    conn.execute("DELETE FROM chunks WHERE email_id = ?", (row["id"],))
                    conn.execute(
                        "UPDATE emails SET fingerprint=?, subject=?, sender=?, recipient=?, sent_at=?, body=? WHERE id=?",
                        (fingerprint, mail["subject"], mail["sender"], mail["recipient"], mail["sent_at"], mail["body"], row["id"]),
                    )
                    email_id = row["id"]
                    counts["updated"] += 1
                else:
                    cursor = conn.execute(
                        "INSERT INTO emails (source, fingerprint, subject, sender, recipient, sent_at, body)"
                        " VALUES (?,?,?,?,?,?,?)",
                        (source, fingerprint, mail["subject"], mail["sender"], mail["recipient"], mail["sent_at"], mail["body"]),
                    )
                    email_id = int(cursor.lastrowid or 0)
                    counts["added"] += 1

                conn.executemany(
                    "INSERT INTO chunks (email_id, ordinal, text) VALUES (?,?,?)",
                    [(email_id, i, piece) for i, piece in enumerate(pieces)],
                )
            counts["chunks"] += len(pieces)

    counts.update(stats(conn))
    conn.close()
    return counts


if __name__ == "__main__":
    targets = sys.argv[1:] or ["samples"]
    result = ingest(os.getenv("MAILRAG_DB", "data/mailrag.sqlite"), targets)
    print(
        f"added={result['added']} updated={result['updated']} skipped={result['skipped']} "
        f"-> {result['emails']} emails, {result['chunks']} chunks indexed"
    )
