# Content Extractor API — n8n workflow + remote workers

Replaces the old **content extractor History** workflow (which fetched every URL
from inside n8n, one execution at a time) with a **queue API**: n8n only hands
out URLs and stores results, while any number of remote workers - Kaggle
notebooks, your own servers, or both at once - do the downloading in parallel.

```
[ worker 1 ]─┐                                      ┌─ claim 60 URLs  (pending → fetching)
[ worker 2 ]─┼──►  n8n "Content Extractor API"  ────┤
[ worker N ]─┘        3 webhooks, 1 workflow        └─ save bodies    (→ news_content)
                               │
                               └──► PostgreSQL   FOR UPDATE SKIP LOCKED = no duplicates
```

---

## Install (once)

1. n8n → **Workflows** → **Import from File** → `n8n/content-extractor-api.json`
2. Open the imported workflow. If the Postgres nodes show a credential warning,
   pick your **Postgres account** credential on any one node.
3. Click the **Setup Schema (run once)** node → **Test step**.
   It adds `news.claimed_at`, the queue indexes, and the `worker_status` table.
   Everything is `IF NOT EXISTS`, so re-running it is safe.
4. **Activate** the workflow (toggle, top right).
5. **Deactivate** the old *content extractor History* workflow — otherwise both
   compete for the same `pending` rows.

> The API key lives in the three **Auth -** IF nodes (`mer30kehasti` by default).
> Change it in all three, then update `N8N_API_KEY` in every worker.

---

## Endpoints

All three require the header `X-API-Key: <your key>` and return `401` without it.

### `POST /webhook/content-get-batch` — claim work

```jsonc
// request
{ "batch_size": 60, "node_name": "kaggle-extractor-1" }

// response
{
  "records": [
    { "id": 81234, "rss_id": 12, "title": "…", "date": "2026-09-11T08:00:00Z",
      "url": "https://example.com/article" }
  ],
  "count": 1
}
```

Flips the returned rows `pending → fetching` and stamps `node_name` +
`claimed_at` in the same statement. `batch_size` is clamped to **1–500**.
An empty queue returns `{"records": [], "count": 0}` — never an error.

### `POST /webhook/content-save` — return extracted content

```jsonc
// request
{
  "node_name": "kaggle-extractor-1",
  "results": [
    { "id": 81234, "status": "content", "content": "the article body …" },
    { "id": 81235, "status": "error",   "error": "bot_challenge:datadome" }
  ]
}

// response
{ "ok": true, "saved": 1, "failed": 1 }
```

The whole batch is written in **one** SQL statement:

| result | effect |
|---|---|
| `status: "content"`, ≥ 150 chars | upsert `news_content.description`, `news.status = 'content'` |
| `status: "error"` or too short | `news.status = 'error'` |

Bodies are truncated to 10 000 chars and NUL bytes stripped before writing.
Unknown ids, duplicate ids and non-numeric ids are dropped by the
*Normalize Results* node, so a buggy client cannot corrupt the table.

### `POST /webhook/content-status` — heartbeat

Any JSON body; `node_name`, `worker_type` and `status` are lifted into columns
and the whole payload is kept as `jsonb`, so the notebook can add new telemetry
fields without a schema change.

```sql
SELECT node_name, status,
       payload->>'urls_extracted' AS extracted,
       payload->>'success_rate'   AS success_pct,
       payload->>'avg_per_hour'   AS per_hour,
       updated_at
FROM worker_status
ORDER BY updated_at DESC;
```

---

## Crash recovery

A worker that dies mid-batch leaves rows stuck in `fetching`. The
**Every 5 min → Reclaim Stale Claims** branch returns any claim older than
**20 minutes** to `pending`. Raise that interval if you run a very large
`BATCH_SIZE`.

The same safety net covers a failed save: if `content-save` cannot be reached
after three attempts, the worker moves on and those URLs are simply re-queued.

---

## Running many workers

Two kinds of worker speak this API, and they mix freely:

| Worker | Where it runs | Good for |
|---|---|---|
| [`content-extractor.ipynb`](../content-extractor.ipynb) | Kaggle / Colab | free capacity, zero setup |
| [`content-extractor-app/`](../content-extractor-app/) | your own Linux server | clean IP, runs 24/7 under systemd, web panel, optional browser rendering |

Each worker needs only a **unique `NODE_NAME`** — same URLs, same API key.
Postgres row locking guarantees no two workers ever receive the same URL.

| Workers | Threads each | ≈ URLs/hour* |
|---|---|---|
| 1 | 24 | 4 000 – 8 000 |
| 4 | 24 | 16 000 – 32 000 |
| 10 | 24 | 40 000 – 80 000 |

\* Network-bound and site-dependent; `PER_DOMAIN_DELAY = 1.0` caps throughput
per domain, so a queue spread over many sites is much faster than one
concentrated on a few.

Tune before adding workers: raise `THREADS` (16–32 is the sweet spot on Kaggle)
and `BATCH_SIZE` first — one worker at 32 threads beats two at 8.

---

## Status values in `news`

| status | meaning |
|---|---|
| `pending` / `NULL` | waiting in the queue |
| `fetching` | claimed by a worker (`node_name`, `claimed_at` say who and when) |
| `content` | body extracted and stored in `news_content` |
| `error` | fetch failed, or the page was a bot wall / too short |

Retry everything that failed:

```sql
UPDATE news SET status = 'pending', node_name = NULL, claimed_at = NULL
WHERE status = 'error';
```

---

## Troubleshooting

| Symptom | Cause |
|---|---|
| Worker prints `401 unauthorized` | `N8N_API_KEY` ≠ the value in the **Auth -** nodes |
| `GET error: Expecting value` | workflow is not **Active** (test mode only answers one call) |
| Every batch returns 0 records | queue empty, or the old workflow is still active and draining it |
| Many `bot_challenge` errors | those sites front their pages with Cloudflare/DataDome; the worker deliberately skips them instead of trying to break through |
| Rows stay in `fetching` | the reclaim branch needs `news.claimed_at` — run the setup node |
