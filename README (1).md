# 📰 Kaggle News Embedding Worker
### Embed news articles using Qwen3-Embedding-8B — free T4 GPU on Kaggle

This notebook fetches unembedded news articles from your PostgreSQL database
(via n8n webhooks), embeds them using Qwen3-Embedding-8B, and saves the
4096-dimensional vectors back to the database.

---

## What You Need Before Starting

| Requirement | Details |
|---|---|
| Kaggle account | Free at kaggle.com |
| HuggingFace account | Free at huggingface.co |
| n8n instance | Running and accessible publicly |
| n8n GET batch webhook URL | From your n8n embed-get-batch workflow |
| n8n SAVE vectors webhook URL | From your n8n embed-save-vectors workflow |
| n8n API key | The X-API-Key secret set in your n8n workflows |

---

## Part 1 — One-Time Setup (Do This Once Only)

### Step 1 — Create Kaggle Account & Enable GPU

1. Go to **kaggle.com** → Sign up or Log in
2. Click your profile picture → **Settings**
3. Scroll to **Phone Verification** → verify your number (required for GPU)

---

### Step 2 — Create HuggingFace Account & Token

1. Go to **huggingface.co** → Sign up or Log in
2. Go to **huggingface.co/Qwen/Qwen3-Embedding-8B**
3. Click **Agree and access repository** (accept the license)
4. Click your profile → **Settings** → **Access Tokens**
5. Click **New Token** → Name: `kaggle` → Role: **Read** → **Create**
6. Copy the token — looks like `hf_AbCdEfGhIjKlMnOpQrStUv...`

---

### Step 3 — Add HuggingFace Token to Kaggle Secrets

1. In Kaggle → click your profile → **Settings** → **Secrets**
2. Click **Add a new secret**
3. Name: `HF_TOKEN` (exactly, capitals)
4. Value: paste your `hf_...` token
5. Click **Save**

---

### Step 4 — Download and Cache the Model (One-Time, ~30 min)

This downloads the 14 GB model once and saves it to your Kaggle storage
so future sessions never re-download it.

**4a.** In Kaggle → **Create** → **New Notebook**

**4b.** Right panel:
- Accelerator → **GPU T4 x2**
- Internet → **On**

**4c.** Add this secret: right panel → **Add-ons** → **Secrets** → toggle `HF_TOKEN` **On**

**4d.** Paste and run these cells:

```python
# Cell 1
!pip install -q huggingface_hub
```

```python
# Cell 2 — Download model
from kaggle_secrets import UserSecretsClient
from huggingface_hub import snapshot_download, login

secrets = UserSecretsClient()
hf_token = secrets.get_secret("HF_TOKEN")
login(token=hf_token)

snapshot_download(
    repo_id="Qwen/Qwen3-Embedding-8B",
    local_dir="/kaggle/working/qwen3-embedding",
    token=hf_token,
    ignore_patterns=["*.msgpack", "*.h5", "flax_*", "rust_model.ot"]
)

print("✅ Download complete")

import os
print(f"Files: {os.listdir('/kaggle/working/qwen3-embedding')}")
```

**4e.** After it finishes:
- Right panel → **Output** tab
- Click **⋮** next to `qwen3-embedding` folder → **New Dataset**
- Name: `qwen3-embedding-8b-model`
- Visibility: **Private**
- Click **Create**
- Wait 15–30 minutes for upload to complete

**4f.** Check it's ready at **kaggle.com/datasets** — status must show **Ready**.

---

## Part 2 — Running the Embedding Worker

### Step 5 — Upload This Notebook to Kaggle

1. Go to **kaggle.com** → **Create** → **New Notebook**
2. Top menu → **File** → **Import Notebook**
3. Upload `news_embedding_worker.ipynb`

---

### Step 6 — Configure the Notebook

Right panel settings:
- **Accelerator** → **GPU T4 x2**
- **Internet** → **On**
- **Persistence** → **Files only**

**Attach your model dataset:**
- Right panel → **Input** → **+ Add Input**
- Click **Your Datasets** tab
- Find `qwen3-embedding-8b-model` → click **Add**

**Edit Cell 2** — fill in your credentials:

```python
N8N_GET_URL  = "https://n8n.yourdomain.com/webhook/YOUR-GET-ID"
N8N_SAVE_URL = "https://n8n.yourdomain.com/webhook/YOUR-SAVE-ID"
N8N_API_KEY  = "your-api-key-here"
NODE_NAME    = "kaggle-t4-worker"   # change if running multiple workers
```

---

### Step 7 — Run the Notebook

Click **Save & Run All** (top right).

> ⚠️ Use **Save & Run All**, NOT the regular Run button.
> This runs the notebook in the background even if you close your browser.

The notebook will:
1. Install dependencies (~1 min)
2. Load the model (~2–3 min)
3. Start embedding and printing progress

---

### Step 8 — Monitor Progress

**From your server**, check progress any time:

```sql
-- Overall status
SELECT status, COUNT(*)
FROM news
GROUP BY status
ORDER BY count DESC;

-- This worker specifically
SELECT node_name, COUNT(*)
FROM news
WHERE node_name = 'kaggle-t4-worker'
GROUP BY node_name;

-- Articles embedded in last 10 minutes (speed check)
SELECT COUNT(*)
FROM news
WHERE status = 'done'
  AND node_name = 'kaggle-t4-worker';
```

---

## Part 3 — Running Multiple Sessions

The worker automatically stops after `MAX_HOURS` (default 8.5h).
To continue embedding, just run the notebook again — it picks up exactly
where it left off. No data is lost or duplicated.

**Weekly schedule** (30h free GPU per week):

| Session | Duration | Est. articles embedded |
|---|---|---|
| Session 1 | 8.5h | ~90,000–150,000 |
| Session 2 | 8.5h | ~90,000–150,000 |
| Session 3 | 8.5h | ~90,000–150,000 |
| **Week total** | **25.5h** | **~270,000–450,000** |

---

## Running Multiple Workers in Parallel

If you have friends helping, each person runs the notebook on their own
Kaggle account. They will not conflict — PostgreSQL ensures each article
is only claimed by one worker at a time.

Each friend must:
1. Complete Part 1 (one-time setup) on their own Kaggle account
2. Use a **different `NODE_NAME`** in Cell 2:
   - Friend 1: `NODE_NAME = "kaggle-worker-friend1"`
   - Friend 2: `NODE_NAME = "kaggle-worker-friend2"`
3. Use the **same** n8n URLs and API key

---

## Troubleshooting

### ⚠️ GET error: Expecting value (empty response)
Your n8n workflow is in **test mode** instead of active mode.
Go to n8n → your embed-get-batch workflow → click **Activate** (toggle top right).

### ⚠️ Model not found / path error
Run this in a new cell to find the correct path:
```python
import os
for root, dirs, files in os.walk("/kaggle/input"):
    for f in files[:1]:
        print(root)
    break
```
Then update `MODEL_PATH` in Cell 2 manually.

### ⚠️ Maximum GPU session count reached
You have another Kaggle notebook running with GPU.
Go to **kaggle.com** → **Your Work** → **Notebooks** → stop the other session.

### ⚠️ Articles stuck (claimed but not embedded)
Run this SQL on your server to reset them:
```sql
UPDATE news
SET status = 'pending', node_name = NULL
WHERE status = 'kaggle-t4-worker'
  AND vector IS NULL;
```
Replace `'kaggle-t4-worker'` with the NODE_NAME of the stuck worker.

### ⚠️ 401 Unauthorized on HuggingFace download
- Make sure you accepted the model license at huggingface.co/Qwen/Qwen3-Embedding-8B
- Check your HF_TOKEN secret is correctly set in Kaggle Secrets
- Wait 2 minutes after accepting the license and retry

---

## How It Works

```
[Kaggle T4 GPU]                    [n8n - public webhook]    [PostgreSQL]
  Load Qwen3-Embedding-8B   ──►  GET /embed-get-batch   ──►  Claim batch
  Embed article text         ──►  POST /embed-save-vectors ──►  Save vector
  Loop until done / timeout  ◄──  Return {id, description} ◄──
```

- Articles in DB with `status='pending'` are waiting to be embedded
- Worker claims a batch → sets `status=worker_name` (in progress)
- After embedding → sets `status='done'`, saves vector, sets `node_name`
- If worker crashes → reset with the SQL above → articles go back to pending

---

## Expected Output

```
Worker  : kaggle-t4-worker
Batch   : 1
Max time: 8.5h
------------------------------------------------------------
✅        1 embedded |   4.2 art/sec |    0.2 min | 0.00h / 8.5h | errors: 0
✅        2 embedded |   4.8 art/sec |    0.4 min | 0.01h / 8.5h | errors: 0
✅        3 embedded |   5.1 art/sec |    0.6 min | 0.01h / 8.5h | errors: 0
...
⏱️  Time limit reached (8.5h). Stopping cleanly.
Total embedded this session: 148,320
```
