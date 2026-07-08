# Bank AI Gateway

Enterprise AI governance reference for a bank on GCP: **the gateway every bank
builds** (PII screening, token budgets, tiered routing, audit) in front of
**the models banks actually buy** (Gemini and Claude served pay-per-token
inside the bank's Vertex AI tenancy).

Near-zero cost: every component scales to zero or is pay-per-token. Demo-month
spend is single-digit dollars; idle spend is $0.

```
User → Chat UI (Cloud Run) → AI Gateway (Cloud Run, FastAPI)
         1. Budget check      Firestore per-user daily token quotas
         2. PII screen        Model Armor (2M tokens/mo free) + custom infoTypes
         3. Tier routing      standard → Gemini Flash · premium → Claude on Vertex
         4. Response screen   Model Armor on the reply
         5. Audit             BigQuery → Looker Studio dashboard
```

## Why this architecture (the recommendation)

| Question | Answer |
|---|---|
| Which model? | **Buy, don't host**: Gemini / Claude via Vertex AI — frontier capability, no-training contractual terms, data stays in the bank's cloud tenancy and region, behind existing IAM. |
| What do we build? | **This gateway.** It's model-agnostic and it's where the bank's actual obligations live: DLP, spend control, auditability. |
| When self-host? | Niche tiers only: data-sovereignty/air-gap mandates, high-volume batch where per-token cost dominates, vendor-independence hedging (SR 11-7 model risk). Plugs in as one more `config.yaml` tier. |

## Run locally (zero GCP, zero cost)

Everything degrades gracefully with no GCP project: mock models, regex PII
screening, in-memory budgets, JSONL audit log. The full governance flow works.

```powershell
cd gateway; pip install fastapi "uvicorn[standard]" pydantic pyyaml requests
uvicorn app.main:app --port 8080

# second terminal
cd ui; pip install streamlit requests
streamlit run app.py
```

Then walk [demo/demo-script.md](demo/demo-script.md).

## Deploy to GCP

```bash
PROJECT_ID=<your-project> ./infra/setup.sh     # APIs, Model Armor template, Firestore, BigQuery
# Vertex Model Garden → enable Claude Opus 4.8 (Gemini needs no enablement)
PROJECT_ID=<your-project> MODEL_ARMOR_TEMPLATE=projects/<p>/locations/us-central1/templates/bank-pii-guard \
  ./infra/deploy.sh
```

Env vars the gateway reads: `GCP_PROJECT`, `GCP_REGION`, `MODEL_ARMOR_TEMPLATE`
(full resource name), `BQ_DATASET`, `CLAUDE_VERTEX_REGION` (default `global`),
`USE_FIRESTORE`.

## Cost model (demo volume)

| Component | Monthly |
|---|---|
| Cloud Run gateway + UI (scale-to-zero, CPU only) | ~$0 |
| Gemini 2.5 Flash (standard tier, pay-per-token) | pennies |
| Claude Opus 4.8 on Vertex (premium tier, pay-per-token) | ~$1–5 at demo volume |
| Model Armor (first 2M tokens/mo free) | $0 |
| Firestore / BigQuery / Looker Studio (free tiers) | ~$0 |
| **Total** | **< $10/mo, $0 idle** |

Contrast with the self-hosted variant (Cloud Run GPU, L4): ~$0.67/hr while
active, ~$550/mo if 24/7 — the number that motivates "buy the model, build the
gateway."

## Repo layout

```
gateway/config.yaml    tiers, routing rules, budgets, PII policy
gateway/app/
  main.py              FastAPI pipeline (budget → PII → route → model → screen → audit)
  guards/pii.py        Model Armor client + local regex fallback
  guards/budget.py     Firestore daily quotas (in-memory fallback)
  routing.py           tier selection heuristics
  providers/           gemini.py (google-genai) · claude_vertex.py (AnthropicVertex)
  audit.py             BigQuery streaming inserts (JSONL fallback)
ui/app.py              Streamlit chat with tier badge / budget meter / PII banners
infra/                 setup.sh (one-time GCP) · deploy.sh (Cloud Run)
demo/demo-script.md    walkthrough with talking points
```
