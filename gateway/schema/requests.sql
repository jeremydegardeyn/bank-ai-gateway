-- Audit table schema for the AI Gateway (BigQuery `ai_gateway.requests`).
--
-- This file exists because of a specific failure. `audit.log_event` inserts with
-- `ignore_unknown_values=True`, which is the right call — an audit row is worth more
-- than its completeness, and a schema mismatch should not cost you the record that
-- something happened. But the consequence is that a field absent from the table is
-- **silently discarded** rather than raising.
--
-- That is exactly what happened on 2026-08-06: `/v1/complete` and `/v1/generate` had
-- been emitting agent_id, workload_class, session_id and on_behalf_of for a day while
-- the table still carried the original `/v1/chat` schema. Every one of those fields went
-- into the void. Nothing errored, nothing logged, and the only symptom was that the
-- unit-economics view came back empty — which reads as "no traffic yet", not as data loss.
--
-- So: **any field added to an audit event must be added here and applied**, or it does
-- not exist. `audit._warn_on_schema_drift` now warns once per process when the table is
-- missing an expected field, but a warning is a backstop, not a substitute for this.
--
--   bq query --project_id=<project> --use_legacy_sql=false < schema/requests.sql

ALTER TABLE `strongsville-city-schools.ai_gateway.requests`
  -- Attribution: who called, on whose behalf, and under which allowance.
  ADD COLUMN IF NOT EXISTS agent_id          STRING,  -- registered agent in the caller's registry
  ADD COLUMN IF NOT EXISTS workload_class    STRING,  -- classification | evaluation | reasoning | ...
  ADD COLUMN IF NOT EXISTS on_behalf_of      STRING,  -- verified end-user email, when a human asked
  ADD COLUMN IF NOT EXISTS owner             STRING,  -- accountable human for the agent
  ADD COLUMN IF NOT EXISTS persona           STRING,  -- human persona on the /v1/chat surface
  -- The join key. Without it, cost cannot be joined to quality and cost-per-successful-
  -- task silently degrades to cost-per-token.
  ADD COLUMN IF NOT EXISTS session_id        STRING,
  -- Which surface served: chat (human) | complete (agent) | generate (tool-calling agent).
  ADD COLUMN IF NOT EXISTS surface           STRING,
  -- What ACTUALLY answered, distinct from the requested model (ADR-0022).
  ADD COLUMN IF NOT EXISTS model_served      STRING,
  ADD COLUMN IF NOT EXISTS tier_clamped      BOOL,
  ADD COLUMN IF NOT EXISTS turns             INT64,
  ADD COLUMN IF NOT EXISTS compaction_tokens INT64,
  ADD COLUMN IF NOT EXISTS error             STRING;
