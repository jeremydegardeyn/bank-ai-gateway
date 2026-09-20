"""Premium tier: Claude on Vertex AI via the AnthropicVertex client.
Bare first-party model IDs (e.g. claude-opus-4-8), GCP ADC auth, no Anthropic
API key. Enable the model in Vertex Model Garden first. Mock reply when no
GCP project is configured."""
from ..settings import CLAUDE_VERTEX_REGION, GCP_PROJECT


def generate(prompt: str, model: str, max_output_tokens: int,
             profile: dict | None = None) -> dict:
    """`profile` mirrors gemini.generate. Only `thinking_budget` and `temperature` have a
    Claude equivalent: budget 0 drops adaptive thinking so the whole `max_tokens` is the
    answer's. There is no response-MIME switch on this API; JSON shape is the prompt's job
    and the gateway's structured PII pass still keeps the document parseable."""
    profile = profile or {}
    if not GCP_PROJECT:
        return {
            "text": f"[mock:{model}] This is a simulated premium-tier reply. "
                    "Set GCP_PROJECT to route to Claude on Vertex AI.",
            "input_tokens": len(prompt) // 4,
            "output_tokens": 24,
            "thoughts_tokens": 0,
            "finish_reason": "end_turn",
            "model": model,
        }

    from anthropic import AnthropicVertex

    kwargs = {}
    if profile.get("thinking_budget") != 0:
        kwargs["thinking"] = {"type": "adaptive"}
    if profile.get("temperature") is not None:
        kwargs["temperature"] = float(profile["temperature"])
    client = AnthropicVertex(project_id=GCP_PROJECT, region=CLAUDE_VERTEX_REGION)
    response = client.messages.create(
        model=model,
        max_tokens=max_output_tokens,
        messages=[{"role": "user", "content": prompt}],
        **kwargs,
    )

    if response.stop_reason == "refusal":
        text = "The model declined this request under its safety policy."
    else:
        text = next((b.text for b in response.content if b.type == "text"), "")

    return {
        "text": text,
        "input_tokens": response.usage.input_tokens,
        # Anthropic bills thinking inside output_tokens and does not break it out.
        "output_tokens": response.usage.output_tokens,
        "thoughts_tokens": 0,
        "finish_reason": response.stop_reason,
        "model": model,
    }
