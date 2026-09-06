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

## Governing tools, not just prompts

The pipeline above screens what a person types. It says nothing about what a *model*
reaches for — and once a model has tools, that is where the customer data actually
flows. `POST /v1/mcp/chat` closes that: the gateway is an [MCP](https://modelcontextprotocol.io)
client of a remote tool server, and runs the tool-calling loop itself so the controls
wrap every turn of it.

```
User → Chat UI ──► /v1/mcp/chat ──► Vertex (function calling)
                        │                    │
                        │            ┌───────┴────────┐
                        │            │  functionCall  │
                        ▼            ▼                │
                  persona + budget   MCP over HTTP ───┘
                  PII screen in      (OIDC id-token, aud = server URL)
                  PII screen on          │
                    every tool result    ▼
                  audit: tools_called  FinChat MCP server (private Cloud Run)
                                         └─► the bank's governed APIs
```

Four controls that have no equivalent on the prompt-only surface:

| Control | Why it does not carry over |
|---|---|
| **Tool results are screened** | They come from a service this gateway does not own, enter the model's context, and leave on someone's screen. A hit withholds the result and tells the model, rather than killing the exchange or passing it silently. |
| **The budget covers the loop** | A tool-calling answer is N model calls, and every turn re-sends the results accumulated so far. Charging the first understates it badly. |
| **The tool names are audited** | "Which model answered" is half the record. "What did it read" is the half a regulator asks about — `mcp_server` and `tools_called` in `ai_gateway.requests`. |
| **An unreachable server refuses** | A model asked for a balance will produce a plausible one. The model is not called at all, so there is nothing to leak by accident. |

Plus the one the protocol gives you free and clients routinely drop: the server's
**`instructions`**, which is the only place MCP lets a server constrain a client's
model. FinChat ships its refusal policy there; dropping it produces answers that
platform cannot stand behind.

**Authentication is Cloud Run IAM, not MCP.** The spec's answer is OAuth with dynamic
client registration; nothing here implements it. So the tool server is private, this
gateway holds `run.invoker` on it, and mints an OIDC id-token per call. The consequence
is worth stating rather than discovering: the remote server sees *this gateway's*
identity, not the signed-in human's. The person's own entitlements are enforced here —
persona, budget, PII — one hop before the tool is reached.

The gateway runs as its own `ai-gateway-sa`, not the project's default compute service
account. That was tolerable while it only called Vertex, and stopped being tolerable the
moment another team granted an identity access to their banking tools: the **public** UI
service runs as the default SA too, so granting "the gateway" would have granted the
internet-facing service in the same breath.

Register servers at deploy time — an empty registry is a working gateway with the tool
surface off, which is the right default:

```bash
MCP_SERVERS="finchat=https://finchat-dev-mcp-xxxxx-uc.a.run.app" ./infra/deploy.sh
```

`GET /v1/mcp/servers` lists what is reachable and what each offers; because discovery is
a live connect, it doubles as the health check for the whole path.

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
  mcp_client.py        MCP client: discovery, OIDC auth, JSON Schema → Vertex schema
  mcp_endpoint.py      the governed tool-calling loop (/v1/mcp/chat)
  test_mcp.py          17 offline tests; each pins a claim the modules make
  guards/pii.py        Model Armor client + local regex fallback
  guards/budget.py     Firestore daily quotas (in-memory fallback)
  routing.py           tier selection heuristics
  providers/           gemini.py (google-genai) · claude_vertex.py (AnthropicVertex)
  audit.py             BigQuery streaming inserts (JSONL fallback)
ui/app.py              Streamlit chat with tier badge / budget meter / PII banners
infra/                 setup.sh (one-time GCP) · deploy.sh (Cloud Run)
demo/demo-script.md    walkthrough with talking points
```
