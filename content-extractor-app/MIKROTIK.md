# Running the Content Extractor in a MikroTik RouterOS container

This deploys the worker as an OCI/Docker container using RouterOS v7's built-in
**container** feature. It is written for the setup you have:

| | |
|---|---|
| RouterOS | **7.24** (container feature is mature here) |
| Arch | **x86 / amd64** (CHR or x86 hardware) — build images for `linux/amd64` |
| CPU | 2 cores — fine |
| Disk | ~40 GB free — plenty; containers need a **formatted disk** for storage (see step 2) |
| RAM | ~700 MB free — **the one real constraint.** Run **without** the Chromium fallback and cap the threads (step 5). The image in this repo already leaves the browser out. |

> **Two things trip everyone up. Do these first and the rest is easy:**
> 1. `container` package installed **and** `device-mode container=yes` (needs a reboot — step 1).
> 2. A **formatted disk** for the container `root-dir` — RouterOS will not store containers on the system partition (step 2).

---

## Step 0 — Build the image (not on the router)

RouterOS cannot build images. On any machine with Docker, from this
`content-extractor-app/` directory:

```bash
# Option A — push to a registry the router can reach (recommended for CHR):
./docker-build.sh push docker.io/YOURUSER/content-extractor:latest

# Option B — no registry: save a tarball to upload to the router:
./docker-build.sh save            # -> content-extractor.tar
```

The image is small and has **no browser** (see the top-level `Dockerfile`), so
it fits the RAM budget. Smoke-test it locally first with `docker compose up`
(see `docker-compose.yml`) before touching the router.

---

## Step 1 — Enable containers (one-time, needs a reboot)

Install the **container** extra package if it is not already there: download the
"Extra packages" bundle for your version/arch from mikrotik.com, upload
`container-7.24-*.npk` to **Files**, and reboot to install. Check:

```rsc
/system/package/print
```

Then turn on container support in device-mode and reboot:

```rsc
/system/device-mode/print                 ;# see current flags
/system/device-mode/update container=yes
```

RouterOS will say the change applies after confirmation.
- **On hardware:** press the reset/mode button (or power-cycle) within the grace period.
- **On CHR / a cloud VM:** there is no button — **power-cycle the VM** from your cloud console (a hard off/on). A plain `/system/reboot` sometimes does not count; if `device-mode/print` still shows `container: no` afterward, do a full power cycle.

Confirm after reboot:

```rsc
/system/device-mode/print                 ;# container: yes
```

> Security note: containers on RouterOS run with real access to the router.
> Only run images you built/trust, and keep the panel off the public internet
> (step 4).

---

## Step 2 — A disk for container storage

Containers, image layers and the pull tempdir must live on a **formatted disk**,
not the system partition. See what you have:

```rsc
/disk/print
```

**On CHR:** if you only see the system disk, add a second virtual disk to the VM
(e.g. 8–40 GB) in your cloud console, then format it in RouterOS. **Formatting
erases that disk** — make sure you pick the new/empty one:

```rsc
/disk/format-drive slot=<the-new-disk> file-system=ext4 label=cdisk
/disk/print                                ;# note the mount point, e.g. cdisk / disk1
```

Pick folders on that disk for the pull tempdir and image layers:

```rsc
/container/config/set \
    tmpdir=cdisk/pull \
    layer-dir=cdisk/layers \
    registry-url=https://registry-1.docker.io
/container/config/print
```

(`registry-url` only matters for Option A. For a **private** registry also set
`username=` and `password=`.)

---

## Step 3 — Network the container (veth + bridge + NAT)

The worker needs **outbound HTTPS** (to your n8n and to the news sites). Give it
a veth interface behind a NATed bridge.

```rsc
# 1) veth with a static address + gateway (the bridge IP below)
/interface/veth/add name=veth-ce address=172.19.0.2/24 gateway=172.19.0.1

# 2) a bridge to hold container veths, with the gateway IP
/interface/bridge/add name=containers
/ip/address/add address=172.19.0.1/24 interface=containers
/interface/bridge/port/add bridge=containers interface=veth-ce

# 3) NAT the container subnet out to the internet
#    (replace WAN with your real upstream interface, or use action=masquerade)
/ip/firewall/nat/add chain=srcnat action=masquerade src-address=172.19.0.0/24

# 4) make sure the container can resolve DNS
/ip/dns/set servers=1.1.1.1,8.8.8.8 allow-remote-requests=yes
```

> Pick a subnet that does not collide with your LAN. `172.19.0.0/24` is just an
> example.

---

## Step 4 — Configuration (environment variables)

The app is fully env-driven — no config file needed. Create an env list. **These
values are tuned for ~700 MB RAM** (fewer threads and a smaller batch than the
VPS defaults of 24/60):

```rsc
/container/envs/add name=ce  key=N8N_BASE_URL       value="https://n8n.3rfan.ir/webhook"
/container/envs/add name=ce  key=N8N_API_KEY        value="YOUR_N8N_API_KEY"
/container/envs/add name=ce  key=NODE_NAME          value="mikrotik-ce-1"

# --- memory budget: keep the worker small ---
/container/envs/add name=ce  key=THREADS            value="8"
/container/envs/add name=ce  key=BATCH_SIZE         value="20"
/container/envs/add name=ce  key=MAX_PAGE_BYTES     value="1500000"
/container/envs/add name=ce  key=RENDER_FALLBACK    value="false"

# --- control panel (reachable at the veth IP; keep it off the internet) ---
/container/envs/add name=ce  key=PANEL_HOST         value="0.0.0.0"
/container/envs/add name=ce  key=PANEL_PORT         value="8787"
/container/envs/add name=ce  key=PANEL_USER         value="admin"
/container/envs/add name=ce  key=PANEL_PASSWORD     value="PICK_A_STRONG_ONE"
```

- `NODE_NAME` **must be unique** across all your workers.
- Leave `PANEL_PASSWORD` set — otherwise the app generates a random one each
  start (you would have to read it from the log).
- Full list of keys and defaults: `config.example.env`.

---

## Step 5 — Create and start the container

### Option A — pull from a registry

```rsc
/container/add \
    remote-image=docker.io/YOURUSER/content-extractor:latest \
    interface=veth-ce \
    envlist=ce \
    root-dir=cdisk/containers/ce \
    hostname=mikrotik-ce-1 \
    logging=yes \
    start-on-boot=yes
```

### Option B — offline `.tar` (no registry)

Upload `content-extractor.tar` to **Files**, then:

```rsc
/container/add \
    file=content-extractor.tar \
    interface=veth-ce \
    envlist=ce \
    root-dir=cdisk/containers/ce \
    hostname=mikrotik-ce-1 \
    logging=yes \
    start-on-boot=yes
```

Then extract/pull finishes in the background — watch the status, and start it
once it is `stopped` (ready):

```rsc
/container/print                    ;# wait until status: stopped (extracted)
/container/start 0                  ;# use the number/id from print
/container/print                    ;# status: running
```

> If your 7.24 build's `/container` exposes a memory cap (e.g. a `ram-high`
> option on `/container/config` or the container itself), set it around
> `500M`–`600M` as a backstop. If it does not, the THREADS/BATCH_SIZE/MAX_PAGE_BYTES
> tuning in step 4 is what keeps the process within budget.

---

## Step 6 — Verify

```rsc
/log/print where topics~"container"           ;# startup + worker logs
```

Within a minute you should see the worker banner and cycle lines like:

```
content-extractor 1.0.0 starting (node_name=mikrotik-ce-1, config=defaults+env, ...)
cycle 1 | ok 18 | fail 2 | 6.1s | saved=18
```

Get a shell in the container to run the built-in pre-flight check — it verifies
Python, every dependency, DNS, outbound HTTPS, and that your n8n API answers and
accepts the key, naming the fix for anything that fails:

```rsc
/container/shell 0
```
```sh
python -m extractor doctor          # full check (uses the heartbeat webhook; consumes no queue rows)
python -m extractor config          # the effective configuration
python -m extractor test https://example.com/some-article   # extract one page, saves nothing
exit
```

**Reach the panel** from a machine that can route to the bridge subnet:
`http://172.19.0.2:8787` (user/password from step 4). To reach it from your LAN
without exposing it publicly, add a dst-nat from the router's LAN IP, or use a
WireGuard/SSH tunnel — **do not** dst-nat it from your WAN in plain HTTP.

---

## Day-two operations

```rsc
# stop / start / restart
/container/stop 0
/container/start 0

# change a setting: edit the env, then restart the container
/container/envs/set [find name=ce key=THREADS] value="12"
/container/stop 0 ; /container/start 0

# update to a new image build (Option A): remove and re-add, root-dir/envs are reusable
/container/stop 0
/container/remove 0
/container/add remote-image=docker.io/YOURUSER/content-extractor:latest \
    interface=veth-ce envlist=ce root-dir=cdisk/containers/ce \
    hostname=mikrotik-ce-1 logging=yes start-on-boot=yes
/container/start 0
```

`start-on-boot=yes` (set above) brings it back after a router reboot.

---

## Memory & tuning notes (the 700 MB budget)

- **No browser.** Keep `RENDER_FALLBACK=false`. The Chromium fallback alone
  wants far more than you have free; this image does not even include it. Pages
  that need JavaScript will be reported as `bot_challenge` / `too_short`, which
  is the correct outcome on a small box.
- **Threads are the main memory dial.** Each thread can hold a page in memory
  while `trafilatura` parses it. Start at `THREADS=8`, `BATCH_SIZE=20`. If the
  log is healthy and free RAM is comfortable, raise threads a few at a time
  (you can do it live from the panel). If memory gets tight, lower them and/or
  drop `MAX_PAGE_BYTES` (e.g. `1000000`).
- **Watch it settle** for a few cycles before walking away — `trafilatura`'s
  peak is on large/complex pages, not the average one.

---

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| `device-mode/print` still shows `container: no` | The reboot did not confirm it. On CHR do a **full power-cycle** from the cloud console, not a soft reboot. |
| `/container/add` fails with no space / cannot create | `root-dir` (and `tmpdir`/`layer-dir`) must be on a **formatted disk** — redo step 2. |
| Container stuck at `status: extracting` | Still unpacking the image; wait. For Option A, check `registry-url` and that the router has DNS + internet (step 3). |
| Log shows `401 unauthorized` | `N8N_API_KEY` ≠ the key in the n8n **Auth** nodes. |
| Log shows `404 … is the workflow Active?` | The n8n workflow is in test mode — activate it. |
| Container restarts / OOMs | Lower `THREADS`, `BATCH_SIZE`, `MAX_PAGE_BYTES`; confirm `RENDER_FALLBACK=false`. |
| No outbound / DNS errors in `doctor` | Fix NAT + DNS in step 3; the container subnet must masquerade out and resolve names. |
| Panel unreachable | `PANEL_HOST` must be `0.0.0.0` (step 4); reach it at the **veth IP**, not `127.0.0.1`. |
| Lots of `bot_challenge` / `too_short` | Sites that need JavaScript. Expected here — there is no browser fallback on this box. |

For what the worker does and every configuration key, see `README.md`.
