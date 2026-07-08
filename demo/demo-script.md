# Demo script — Bank AI Gateway

Run through these in the UI, in order. Each one exercises a governance control.

## 1. Normal traffic → cheap tier
> What are the current FDIC insurance limits?

Expect: `tier: standard`, Gemini Flash, a few hundred tokens charged.

## 2. Analytical query → premium tier (auto-routing)
> Analyze the trade-offs between warehouse-native and lakehouse architectures
> for a regional bank's risk-reporting platform, step by step.

Expect: `tier: premium`, Claude on Vertex. Talking point: spend governance is
architectural (routing), not just a quota.

## 3. PII in the prompt → blocked
> Customer John Smith, SSN 123-45-6789, card 4111 1111 1111 1111, is disputing
> a charge. Draft a response email.

Expect: hard block, findings listed, incident written to the audit log.
Talking point: this is the exact "employee pastes customer data into a chatbot"
scenario the bank fears — caught at the boundary, before any model sees it.

## 4. Bank-internal identifier → custom infoType
> Look up account HB-20260708 and summarize recent activity.

Expect: block/redact on `BANK_INTERNAL_ACCOUNT`. Talking point: Model Armor
integrates with Sensitive Data Protection custom infoTypes, so the bank's own
account-number formats are first-class detectors.

## 5. Budget exhaustion
Switch user to **demo-intern** (2,000 tokens/day) and send 2–3 premium
queries.

Expect: `budget_exceeded` with a clean user-facing message. Talking point:
per-user/per-department budgets turn "excessive token spend" from a surprise
invoice into a policy decision.

## 6. The audit trail
Local mode: `logs/audit.jsonl`. GCP mode: BigQuery `ai_gateway.requests` —
point Looker Studio at it for spend-per-user, tokens-over-time, and
PII-incidents-by-type charts.

## Positioning (interview framing)
- Banks **buy** frontier models through their cloud tenancy (Gemini / Claude on
  Vertex): contractual no-training, data stays in-region, existing IAM.
- Banks **build** this gateway regardless of the model behind it.
- Self-hosted open models are a *tier* for niche workloads (sovereignty,
  high-volume batch), not the platform — and this gateway is where such a tier
  would plug in (one more entry in `config.yaml`).
