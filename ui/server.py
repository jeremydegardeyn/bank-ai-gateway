"""Bank AI Gateway UI — serves the sign-in SPA and proxies chat traffic.

Auth (FinChat ADR-0016 pattern): the SPA obtains a Google Identity Services
ID token; every API call carries it as `Authorization: Bearer <token>`. This
server verifies the token (signature, audience = our OAuth client, expiry,
email_verified) and forwards the VERIFIED email to the private gateway as
user_id — the browser never talks to the gateway and never chooses its own
identity. Persona entitlements are resolved gateway-side.

Local dev: with GOOGLE_OAUTH_CLIENT_ID unset, sign-in is bypassed and the SPA
offers the demo users (the gateway's dev persona mapping handles them)."""
import os
import time
from pathlib import Path

import requests
from fastapi import FastAPI, Header, Request
from fastapi.responses import HTMLResponse, JSONResponse

GATEWAY_URL = os.environ.get("GATEWAY_URL", "http://localhost:8080")
OAUTH_CLIENT_ID = os.environ.get("GOOGLE_OAUTH_CLIENT_ID", "")
STATIC = Path(__file__).resolve().parent / "static"

app = FastAPI(title="Bank AI Gateway UI")

# ── Google sign-in verification (cached; GIS tokens live ~1h) ───────────────
_user_cache: dict[str, dict] = {}


def _verify(token: str) -> dict | None:
    """Returns {email, exp} for a valid GIS ID token, else None."""
    cached = _user_cache.get(token)
    if cached and cached["exp"] > time.time():
        return cached
    try:
        import google.auth.transport.requests
        from google.oauth2 import id_token as gid
        info = gid.verify_oauth2_token(
            token, google.auth.transport.requests.Request(), OAUTH_CLIENT_ID)
        if not info.get("email_verified"):
            return None
        user = {"email": (info.get("email") or "").lower(), "exp": info["exp"]}
        _user_cache[token] = user
        return user
    except Exception:
        return None


def _identity(authorization: str | None) -> str | None:
    """Resolve the caller's identity: verified email, or a demo id in dev mode."""
    if not OAUTH_CLIENT_ID:  # local dev — no sign-in configured
        return (authorization or "").removeprefix("Bearer dev:") or None
    if not authorization or not authorization.startswith("Bearer "):
        return None
    user = _verify(authorization.removeprefix("Bearer "))
    return user["email"] if user else None


# ── Gateway proxy (service-to-service auth via ID token) ────────────────────
def _gateway_headers() -> dict:
    if GATEWAY_URL.startswith("http://localhost"):
        return {}
    try:
        import google.auth.transport.requests
        import google.oauth2.id_token
        token = google.oauth2.id_token.fetch_id_token(
            google.auth.transport.requests.Request(), GATEWAY_URL)
        return {"Authorization": f"Bearer {token}"}
    except Exception:
        return {}


# ── Routes ───────────────────────────────────────────────────────────────────
@app.get("/", response_class=HTMLResponse)
def index():
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    return html.replace("{{CLIENT_ID}}", OAUTH_CLIENT_ID)


@app.get("/api/me")
def me(authorization: str | None = Header(default=None)):
    user = _identity(authorization)
    if user is None:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    r = requests.get(f"{GATEWAY_URL}/v1/me/{user}",
                     headers=_gateway_headers(), timeout=15)
    return JSONResponse({"email": user, **r.json()}, status_code=r.status_code)


@app.get("/api/history")
def get_history(authorization: str | None = Header(default=None)):
    user = _identity(authorization)
    if user is None:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    r = requests.get(f"{GATEWAY_URL}/v1/history/{user}",
                     headers=_gateway_headers(), timeout=15)
    return JSONResponse(r.json(), status_code=r.status_code)


@app.post("/api/chat")
async def chat(request: Request, authorization: str | None = Header(default=None)):
    user = _identity(authorization)
    if user is None:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    body = await request.json()
    payload = {"user_id": user, "message": body.get("message", "")}
    if body.get("tier") in ("standard", "premium"):
        payload["tier"] = body["tier"]
    r = requests.post(f"{GATEWAY_URL}/v1/chat", json=payload,
                      headers=_gateway_headers(), timeout=120)
    return JSONResponse(r.json(), status_code=r.status_code)
