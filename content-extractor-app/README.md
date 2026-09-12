# Content Extractor — self-hosted worker

A long-running Python service that does what `content-extractor.ipynb` does, but
on your own Linux server: claims news URLs from the n8n **Content Extractor
API**, downloads and extracts the article body, and posts the text back.

Built because Kaggle's outbound IP ranges are widely blocklisted — news sites
answer a shared datacenter IP with a challenge page far more often than they
answer a normal server. Running from your own VPS fixes most of that, and this
app adds the things a notebook cannot do: it survives reboots, throttles itself
per domain, backs off hosts that push back, and can run pages that need
JavaScript through a real browser.

```
┌─────────────────────┐        ┌──────────────────┐        ┌────────────┐
│  your Linux server  │        │       n8n        │        │ PostgreSQL │
│  ┌───────────────┐  │ claim  │  Content         │        │            │
│  │ worker threads│──┼───────►│  Extractor API   ├───────►│ news       │
│  │ + web panel   │◄─┼────────┤  (3 webhooks)    │        │ news_content
│  └───────────────┘  │  save  └──────────────────┘        └────────────┘
└─────────────────────┘
```

---

## Install on a fresh server

Debian 11/12 or Ubuntu 20.04+, 1 vCPU and 1 GB RAM is enough.

```bash
git clone https://github.com/theosophy88/embedding-worker-Notebook.git
cd embedding-worker-Notebook/content-extractor-app

sudo bash install.sh \
  --n8n-url https://n8n.3rfan.ir/webhook \
  --api-key YOUR_N8N_API_KEY \
  --node-name vps-extractor-1
```

That single command installs the system packages, creates the `extractor`
system user, builds a virtualenv, writes `/etc/content-extractor/config.env`,
installs and starts a systemd service, runs the pre-flight checks and prints
your panel URL and password. Leave out `--api-key` and it asks for it.

Add `--with-browser` to also install Chromium for the JavaScript fallback
(a few hundred MB — skip it on a tiny VPS and turn it on later).

Re-running the installer is safe: it upgrades the code and keeps every config
value you do not pass again.

### Verify it

```bash
systemctl status content-extractor      # should be active (running)
journalctl -fu content-extractor        # live log
```

Within a minute the log shows lines like:

```
cycle 1 | ok 54 | fail 6 | 7.4s | saved=54
```

### Updating

The service runs from `/opt/content-extractor`, **not** from your git clone, so
pulling alone changes nothing:

```bash
cd ~/embedding-worker-Notebook && git pull
cd content-extractor-app && sudo bash install.sh   # copies the new code, restarts
```

The installer is idempotent and keeps your config. If you ever wonder which copy
is actually running, the startup log line says so:
`content-extractor 1.0.0 starting (… app=/opt/content-extractor/app/extractor)`.

---

## The panel

By default the panel binds to `127.0.0.1` — safe, because plain-HTTP Basic auth
over the open internet would leak the password. Reach it from your own machine:

```bash
ssh -N -L 8787:127.0.0.1:8787 you@your-server
# then open http://127.0.0.1:8787
```

To expose it on the network instead: `sudo bash install.sh --panel-public`
(put a TLS reverse proxy in front of it if it faces the internet).

It shows live throughput per minute, a failure-reason breakdown, per-domain
state, and the last results with their timings. You can pause/resume/stop the
worker, retune threads and batch size without a restart, clear a domain
cool-off, re-run the pre-flight checks, and extract any single URL to see
exactly what the extractor gets — that last one writes nothing to the database,
so it is the fastest way to work out why a site fails.

---

## Commands

```bash
sudo content-extractor doctor        # check machine, config and the n8n API
sudo content-extractor test URL      # extract one page, writes nothing
sudo content-extractor config        # the effective configuration
```

`install.sh` puts that wrapper in `/usr/local/bin`; it runs the CLI as the
service user from the right directory, so it works from anywhere. Without it
(older install, or a manual setup) the equivalent is:

```bash
cd /opt/content-extractor/app
sudo -u extractor /opt/content-extractor/venv/bin/python -m extractor doctor
```

`doctor` verifies Python, every dependency, the config, DNS, outbound HTTPS,
that the n8n API answers and accepts your key, that the panel port is free, and
that Chromium launches when the fallback is on. Each failure prints the fix.
(It uses the heartbeat webhook, so it never consumes queue rows.)

---

## Configuration

Everything lives in `/etc/content-extractor/config.env` (root-owned, mode 640).
Edit, then `sudo systemctl restart content-extractor`.

| Key | Default | What it does |
|---|---|---|
| `N8N_BASE_URL` | — | webhook base, e.g. `https://n8n.example.com/webhook` |
| `N8N_API_KEY` | — | the `X-API-Key` from the n8n Auth nodes |
| `NODE_NAME` | hostname | **must differ per worker** |
| `THREADS` | 24 | parallel downloads |
| `BATCH_SIZE` | 60 | URLs claimed per cycle (max 500) |
| `PER_DOMAIN_DELAY` | 1.0 | min seconds between two hits on one host |
| `DOMAIN_FAILURE_THRESHOLD` | 5 | consecutive blocks before a host is cooled off |
| `DOMAIN_COOLOFF_SECONDS` | 900 | how long that cool-off lasts |
| `RENDER_FALLBACK` | false | retry JS-only pages in headless Chromium |
| `PROXY_URL` | — | outbound proxy for page downloads (not for n8n calls) |
| `STRIP_PUNCTUATION` | true | same text cleaning as the original n8n extractor |
| `MAX_HOURS` | 0 | 0 = run forever |
| `PANEL_HOST` / `PANEL_PORT` | 127.0.0.1 / 8787 | panel bind address |

`THREADS`, `BATCH_SIZE`, `PER_DOMAIN_DELAY`, `IDLE_SLEEP_SECONDS`,
`MIN_CONTENT_CHARS` and `RENDER_FALLBACK` are also changeable live from the
panel (runtime only — the file is what survives a restart).

---

## How it handles sites that block

The worker never tries to defeat a protection system. It distinguishes three
cases, because they need different answers:

| The site says | What happens |
|---|---|
| "you need JavaScript" (Cloudflare interstitial, empty shell) | reported as `bot_challenge` / `too_short`. With `RENDER_FALLBACK=true`, retried once in a real browser that runs the page's own scripts |
| "no" (403, 429, repeated) | taken at face value — never retried in the browser. After `DOMAIN_FAILURE_THRESHOLD` consecutive blocks the host is left alone for 15 minutes |
| nothing (timeout, DNS, TLS) | reported with the specific reason so the panel can group it |

URLs skipped because their host is cooling off are **left claimed, not marked
failed** — n8n's reclaim job returns them to `pending` after 20 minutes and a
later cycle picks them up when the site is calm again.

Other reasons throughput improves over Kaggle: HTTP/2 with pooled keep-alive
connections per thread, one request per host per second no matter how many
threads run, non-HTML and oversized responses rejected before they are
downloaded in full, and charset detection from headers and `<meta>` rather than
guesswork.

---

## Running several workers

Install on each machine with a different `--node-name`. They share the queue
safely — Postgres hands each claim to exactly one worker via
`FOR UPDATE SKIP LOCKED`. Watch them all from your database:

```sql
SELECT node_name, status,
       payload->>'urls_extracted' AS extracted,
       payload->>'success_rate'   AS success_pct,
       payload->>'avg_per_hour'   AS per_hour,
       updated_at
FROM worker_status
ORDER BY updated_at DESC;
```

Tune one worker before adding another: raise `THREADS` (16–32 is a good range)
and `BATCH_SIZE` first. A queue spread over many domains is much faster than one
concentrated on a few, because of the per-domain pacing.

---

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| `401 unauthorized` in the log | `N8N_API_KEY` ≠ the key in the n8n **Auth** nodes |
| `404 … is the workflow Active?` | the n8n workflow is in test mode; activate it |
| Every batch returns 0 records | queue is empty, or the old *content extractor History* workflow is draining it — deactivate that one |
| Service restarts in a loop | `journalctl -u content-extractor -n 50`; usually a config error, which `doctor` names exactly |
| `No module named extractor` | run `sudo content-extractor doctor`, or `cd /opt/content-extractor/app` first — the package lives there, not in `/opt/content-extractor` |
| Panel does not load | check `PANEL_HOST`; on `127.0.0.1` you need the SSH tunnel above |
| Lots of `bot_challenge` | try `RENDER_FALLBACK=true` (needs `--with-browser`); some sites stay closed, and that is the correct outcome |
| Lots of `too_short` | the site builds its article with JavaScript — same answer |
| Rows stuck in `fetching` | that is normal for up to 20 minutes; n8n's reclaim job returns them |

Remove the service with `sudo bash uninstall.sh` (add `--purge` to delete
`/opt/content-extractor`, the config and the user).

---

## Manual install (no systemd, other distros, Docker)

```bash
python3 -m venv venv
venv/bin/pip install -r requirements.txt
cp config.example.env config.env      # edit it
EXTRACTOR_CONFIG=config.env venv/bin/python -m extractor run
```

Any setting can come from the environment instead of the file, which is all a
container needs. `--no-panel` runs the worker alone; `--no-worker` brings up the
panel without starting extraction.

### Docker / containers (incl. MikroTik RouterOS)

A `Dockerfile` builds a small, env-configured image (no headless browser, so it
stays light). Test it locally first:

```bash
cp config.example.env .env      # set N8N_BASE_URL, N8N_API_KEY, PANEL_PASSWORD
docker compose up --build       # panel at http://127.0.0.1:8787
```

`docker-build.sh` builds the image for an amd64 host and either pushes it to a
registry or saves a `.tar` for offline import.

To run it inside a **MikroTik RouterOS v7 container** (x86/CHR), see
**[MIKROTIK.md](MIKROTIK.md)** — full step-by-step for device-mode, a container
disk, veth/bridge/NAT networking, and env tuning for a small-RAM router.

---

## Layout

```
install.sh                 one-command install, 9 verified steps
bin/content-extractor      CLI wrapper installed to /usr/local/bin
uninstall.sh               stop, disable, optionally purge
config.example.env         every setting, documented
systemd/                   the unit template install.sh fills in
extractor/
  cli.py                   run | doctor | test | config
  config.py                settings, validation, live retuning
  n8n.py                   the three API calls, with retries
  fetcher.py               HTTP/2 client pool, per-domain pacing + breaker
  extract.py               trafilatura + the n8n regex cleaner fallback
  renderer.py              optional serial headless-Chromium fallback
  worker.py                claim → fetch in parallel → save loop
  stats.py                 live counters and the heartbeat payload
  doctor.py                pre-flight checks, each with a fix
  panel.py / panel.html    the control panel
```
