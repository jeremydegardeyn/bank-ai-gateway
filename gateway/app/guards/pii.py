"""PII screening. Primary engine: Google Cloud Model Armor (sanitizeUserPrompt /
sanitizeModelResponse). Fallback engine: local regex detectors, so the demo runs
with zero GCP setup and the block/redact flow is identical either way."""
import re
from dataclasses import dataclass, field

import requests

from ..settings import MODEL_ARMOR_TEMPLATE, GCP_REGION


@dataclass
class PiiVerdict:
    match: bool
    findings: list[str] = field(default_factory=list)
    redacted_text: str | None = None
    engine: str = "local-regex"


# Local fallback detectors. HB-XXXXXXXX simulates a bank-internal account format —
# the same thing you'd register as a custom SDP infoType in Model Armor.
_LOCAL_PATTERNS = {
    "US_SOCIAL_SECURITY_NUMBER": re.compile(r"\b\d{3}-\d{2}-\d{4}\b"),
    "CREDIT_CARD_NUMBER": re.compile(r"\b(?:\d[ -]?){13,16}\b"),
    "EMAIL_ADDRESS": re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.]+\b"),
    "PHONE_NUMBER": re.compile(r"\b\(?\d{3}\)?[ .-]?\d{3}[ .-]?\d{4}\b"),
    "BANK_INTERNAL_ACCOUNT": re.compile(r"\bHB-\d{8}\b"),
}


def _luhn_ok(digits: str) -> bool:
    ds = [int(c) for c in digits if c.isdigit()]
    if not 13 <= len(ds) <= 16:
        return False
    checksum, parity = 0, len(ds) % 2
    for i, d in enumerate(ds):
        if i % 2 == parity:
            d *= 2
            if d > 9:
                d -= 9
        checksum += d
    return checksum % 10 == 0


def _screen_local(text: str) -> PiiVerdict:
    findings, redacted = [], text
    for name, pattern in _LOCAL_PATTERNS.items():
        for m in pattern.finditer(text):
            if name == "CREDIT_CARD_NUMBER" and not _luhn_ok(m.group()):
                continue
            findings.append(name)
            redacted = redacted.replace(m.group(), f"[{name}]")
    return PiiVerdict(match=bool(findings), findings=sorted(set(findings)),
                      redacted_text=redacted if findings else None, engine="local-regex")


def _screen_model_armor(text: str, kind: str) -> PiiVerdict:
    import google.auth
    import google.auth.transport.requests

    creds, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
    creds.refresh(google.auth.transport.requests.Request())

    verb = "sanitizeUserPrompt" if kind == "prompt" else "sanitizeModelResponse"
    payload_key = "userPromptData" if kind == "prompt" else "modelResponseData"
    url = f"https://modelarmor.{GCP_REGION}.rep.googleapis.com/v1/{MODEL_ARMOR_TEMPLATE}:{verb}"

    resp = requests.post(
        url,
        headers={"Authorization": f"Bearer {creds.token}"},
        json={payload_key: {"text": text}},
        timeout=15,
    )
    resp.raise_for_status()
    result = resp.json().get("sanitizationResult", {})
    match = result.get("filterMatchState") == "MATCH_FOUND"
    findings = [
        name for name, fr in result.get("filterResults", {}).items()
        if "MATCH_FOUND" in str(fr)
    ]
    return PiiVerdict(match=match, findings=findings, engine="model-armor")


def screen(text: str, kind: str = "prompt") -> PiiVerdict:
    """kind: 'prompt' or 'response'."""
    if MODEL_ARMOR_TEMPLATE:
        try:
            verdict = _screen_model_armor(text, kind)
            # Model Armor flags, local regex supplies the redacted rendering.
            if verdict.match:
                verdict.redacted_text = _screen_local(text).redacted_text or text
            return verdict
        except Exception:
            pass  # degrade to local screening rather than failing open with no screen
    return _screen_local(text)
