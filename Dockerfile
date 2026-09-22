FROM python:3.12-slim

WORKDIR /app

COPY rag/requirements.txt rag/requirements.txt
RUN pip install --no-cache-dir -r rag/requirements.txt

COPY rag/ rag/
COPY samples/ samples/

ENV MAILRAG_DB=/data/mailrag.sqlite \
    MAILRAG_MAIL_DIR=/app/samples \
    PYTHONUNBUFFERED=1

EXPOSE 8000

# Ingest on boot (idempotent: unchanged files are skipped by fingerprint), then serve.
CMD ["sh", "-c", "python -m rag.ingest \"$MAILRAG_MAIL_DIR\" && exec uvicorn rag.app:app --host 0.0.0.0 --port 8000"]
