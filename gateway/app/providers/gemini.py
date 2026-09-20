"""Standard tier: Gemini on Vertex AI via the google-genai SDK (pay-per-token,
zero idle cost). Mock reply when no GCP project is configured."""
from ..settings import GCP_PROJECT, GCP_REGION


def generate(prompt: str, model: str, max_output_tokens: int,
             profile: dict | None = None) -> dict:
    """One completion. `profile` (see workloads.PROFILES) carries the generation settings a
    workload class is allowed to run under: thinking budget, temperature, response MIME
    type. None means the model's defaults — which for gemini-2.5-flash means it thinks
    first and bills that thinking against `max_output_tokens`."""
    profile = profile or {}
    if not GCP_PROJECT:
        mock = (f"[mock:{model}] This is a simulated standard-tier reply. "
                "Set GCP_PROJECT to route to Vertex AI.")
        if profile.get("response_mime_type") == "application/json":
            mock = f'{{"mock":true,"model":"{model}"}}'
        return {
            "text": mock,
            "input_tokens": len(prompt) // 4,
            "output_tokens": 24,
            "thoughts_tokens": 0,
            "finish_reason": "STOP",
            "model": model,
        }

    from google import genai
    from google.genai import types

    config = types.GenerateContentConfig(max_output_tokens=max_output_tokens)
    if profile.get("thinking_budget") is not None:
        # Load-bearing for classification, not a tweak: with the default budget the model
        # spent the whole allowance reasoning and returned 7 tokens of a 150-token JSON
        # verdict, or no parts at all. FinChat's ui/server.py `_via_vertex` records the
        # same finding for its intent router.
        config.thinking_config = types.ThinkingConfig(
            thinking_budget=int(profile["thinking_budget"]))
    if profile.get("temperature") is not None:
        config.temperature = float(profile["temperature"])
    if profile.get("response_mime_type"):
        config.response_mime_type = profile["response_mime_type"]

    client = genai.Client(vertexai=True, project=GCP_PROJECT, location=GCP_REGION)
    resp = client.models.generate_content(model=model, contents=prompt, config=config)
    usage = resp.usage_metadata
    cand = (resp.candidates or [None])[0]
    finish = getattr(cand, "finish_reason", None)
    return {
        "text": resp.text or "",
        "input_tokens": usage.prompt_token_count or 0,
        "output_tokens": usage.candidates_token_count or 0,
        # Reasoning tokens. Billed as output by Vertex and drawn from the same
        # max_output_tokens budget as the answer, so a caller seeing output_tokens=7 and
        # thoughts_tokens=249 knows exactly where its budget went. 0 when thinking is off.
        "thoughts_tokens": usage.thoughts_token_count or 0,
        # MAX_TOKENS here is the truncation signal a caller needs; a short `text` alone
        # cannot be told apart from a short answer.
        "finish_reason": getattr(finish, "name", None) or (str(finish) if finish else None),
        "model": model,
        # What actually served, as reported by the provider — distinct from `model`,
        # which is only what we asked for. Requesting an alias and recording the version
        # that answered is the difference between an intention and evidence of drift.
        # None when the surface does not report it; never back-filled from the request.
        "model_version": getattr(resp, "model_version", None),
    }
