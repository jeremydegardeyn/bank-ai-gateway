from . import claude_vertex, gemini

PROVIDERS = {
    "gemini": gemini.generate,
    "claude": claude_vertex.generate,
}
