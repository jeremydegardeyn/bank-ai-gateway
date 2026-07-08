"""Premium tier: Claude on Vertex AI via the AnthropicVertex client.
Bare first-party model IDs (e.g. claude-opus-4-8), GCP ADC auth, no Anthropic
API key. Enable the model in Vertex Model Garden first. Mock reply when no
GCP project is configured."""
from ..settings import CLAUDE_VERTEX_REGION, GCP_PROJECT


def generate(prompt: str, model: str, max_output_tokens: int) -> dict:
    if not GCP_PROJECT:
        return {
            "text": f"[mock:{model}] This is a simulated premium-tier reply. "
                    "Set GCP_PROJECT to route to Claude on Vertex AI.",
            "input_tokens": len(prompt) // 4,
            "output_tokens": 24,
            "model": model,
        }

    from anthropic import AnthropicVertex

    client = AnthropicVertex(project_id=GCP_PROJECT, region=CLAUDE_VERTEX_REGION)
    response = client.messages.create(
        model=model,
        max_tokens=max_output_tokens,
        thinking={"type": "adaptive"},
        messages=[{"role": "user", "content": prompt}],
    )

    if response.stop_reason == "refusal":
        text = "The model declined this request under its safety policy."
    else:
        text = next((b.text for b in response.content if b.type == "text"), "")

    return {
        "text": text,
        "input_tokens": response.usage.input_tokens,
        "output_tokens": response.usage.output_tokens,
        "model": model,
    }
