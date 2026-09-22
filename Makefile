COMPOSE      ?= docker compose
DIFY_COMPOSE ?= $(COMPOSE) -f docker-compose.yml -f docker-compose.dify.yml
PORT         ?= 8100
KEY          ?= mailrag-demo-key
Q            ?= What unit price was quoted for PO-48812?

.PHONY: help up dify-up down logs health ingest ask stats clean

help:
	@grep -E '^[a-z-]+:.*?##' $(MAKEFILE_LIST) | sed 's/:.*##/\t/' | expand -t22

up:  ## Start the retrieval API on its own (reachable at localhost:$(PORT))
	$(COMPOSE) up -d --build

dify-up:  ## Start it on Dify's network too, as http://mailrag:8000
	$(DIFY_COMPOSE) up -d --build

down:  ## Stop it
	$(COMPOSE) down

logs:  ## Follow the API log
	$(COMPOSE) logs -f mailrag

health:  ## How much mail is indexed
	@curl -s localhost:$(PORT)/health; echo

ingest:  ## Re-index the mounted mail (idempotent)
	$(COMPOSE) exec mailrag python -m rag.ingest /app/samples

ask:  ## Ask the retrieval API directly: make ask Q="which serial was RMA'd?"
	@curl -s -X POST localhost:$(PORT)/retrieval \
		-H "Authorization: Bearer $(KEY)" \
		-H 'Content-Type: application/json' \
		-d '{"knowledge_id":"emails","query":"$(Q)","retrieval_setting":{"top_k":3,"score_threshold":0}}' \
		| python3 -m json.tool

stats: health  ## Alias for health

clean:  ## Stop and drop the index volume
	$(COMPOSE) down -v
