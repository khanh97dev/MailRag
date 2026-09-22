"""MailRag retrieval API.

Speaks Dify's **External Knowledge API** contract, so the same endpoint serves
both ways of wiring the demo:

  * an HTTP Request node in a workflow calling POST /retrieval directly, and
  * a Dify External Knowledge Base, which Dify itself calls at POST /retrieval.

Contract (Dify 1.x):

    POST /retrieval
    Authorization: Bearer <MAILRAG_API_KEY>
    {"knowledge_id": "emails", "query": "...",
     "retrieval_setting": {"top_k": 5, "score_threshold": 0.0},
     "metadata_condition": null}

    200 {"records": [{"content": "...", "title": "...", "score": 0.0-1.0,
                      "metadata": {...}}]}

`metadata` must be an object: Dify writes the score and title into it before
handing the record to the LLM node, and a null there loses both.
"""

from __future__ import annotations

import os

from fastapi import FastAPI, Header
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from .ingest import ingest
from .store import connect, search, stats

DB_PATH = os.getenv("MAILRAG_DB", "data/mailrag.sqlite")
API_KEY = os.getenv("MAILRAG_API_KEY", "mailrag-demo-key")
KNOWLEDGE_ID = os.getenv("MAILRAG_KNOWLEDGE_ID", "emails")
MAIL_DIR = os.getenv("MAILRAG_MAIL_DIR", "samples")

app = FastAPI(title="MailRag retrieval API", version="1.0.0")
db = connect(DB_PATH)


class RetrievalSetting(BaseModel):
    top_k: int = Field(default=5, ge=1, le=50)
    score_threshold: float = 0.0


class RetrievalRequest(BaseModel):
    knowledge_id: str
    query: str
    retrieval_setting: RetrievalSetting = RetrievalSetting()
    metadata_condition: dict | None = None      # accepted and ignored: no metadata filters yet


def _error(status: int, code: int, message: str) -> JSONResponse:
    """Dify's documented error envelope -- it surfaces error_msg in the UI."""
    return JSONResponse(status_code=status, content={"error_code": code, "error_msg": message})


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "knowledge_id": KNOWLEDGE_ID, **stats(db)}


@app.post("/retrieval")
def retrieval(request: RetrievalRequest, authorization: str = Header(default="")):
    if not authorization.startswith("Bearer "):
        return _error(403, 1001, "Invalid Authorization header format. Expected 'Bearer <api-key>'.")
    if authorization.removeprefix("Bearer ").strip() != API_KEY:
        return _error(403, 1002, "Authorization failed.")
    if request.knowledge_id != KNOWLEDGE_ID:
        return _error(404, 2001, f"Knowledge '{request.knowledge_id}' does not exist.")

    hits = search(db, request.query, top_k=request.retrieval_setting.top_k)
    threshold = request.retrieval_setting.score_threshold or 0.0

    return {
        "records": [
            {
                "content": hit["text"],
                "title": hit["subject"] or os.path.basename(hit["source"]),
                "score": hit["score"],
                "metadata": {
                    "source": os.path.basename(hit["source"]),
                    "from": hit["sender"],
                    "to": hit["recipient"],
                    "sent_at": hit["sent_at"],
                    "chunk": hit["ordinal"],
                },
            }
            for hit in hits
            if hit["score"] >= threshold
        ]
    }


@app.post("/reindex")
def reindex(authorization: str = Header(default="")):
    """Re-run ingest over MAILRAG_MAIL_DIR. Convenience for the demo, not a
    production ingest path -- a real archive is fed by cron or a queue."""
    if authorization.removeprefix("Bearer ").strip() != API_KEY:
        return _error(403, 1002, "Authorization failed.")
    return ingest(DB_PATH, [MAIL_DIR])
