"""Standard tier: Gemini on Vertex AI via the google-genai SDK (pay-per-token,
zero idle cost). Mock reply when no GCP project is configured."""
from ..settings import GCP_PROJECT, GCP_REGION


def generate(prompt: str, model: str, max_output_tokens: int) -> dict:
    if not GCP_PROJECT:
        return {
            "text": f"[mock:{model}] This is a simulated standard-tier reply. "
                    "Set GCP_PROJECT to route to Vertex AI.",
            "input_tokens": len(prompt) // 4,
            "output_tokens": 24,
            "model": model,
        }

    from google import genai
    from google.genai import types

    client = genai.Client(vertexai=True, project=GCP_PROJECT, location=GCP_REGION)
    resp = client.models.generate_content(
        model=model,
        contents=prompt,
        config=types.GenerateContentConfig(max_output_tokens=max_output_tokens),
    )
    usage = resp.usage_metadata
    return {
        "text": resp.text or "",
        "input_tokens": usage.prompt_token_count or 0,
        "output_tokens": usage.candidates_token_count or 0,
        "model": model,
    }
