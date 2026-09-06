"""Run the UI on a laptop against a DEPLOYED gateway.

The two other ways to run this are all-local (mock models, no GCP) and all-deployed. The
useful third is a local UI against the real gateway: you get the actual models, the
actual Model Armor screen, the actual budgets and the actual remote MCP server, while
editing the page and reloading.

It works because of two deliberate fallbacks:

  * `server.py::_gateway_headers` falls back to `gcloud auth print-identity-token` when
    it cannot mint a workload token. Cloud Run accepts that from anyone holding
    `run.invoker`, so the call is attributable to *you* rather than to a shared identity.
  * With no `GOOGLE_OAUTH_CLIENT_ID` the page drops into dev mode. The three demo
    buttons resolve to personas only when the gateway has no `PERSONA_EMAILS`; against a
    deployed gateway they will come back unprovisioned, which is correct. Sign in as
    yourself from the browser console instead:

        devLogin('you@yourdomain.com')

Usage:

    python ui/run_local.py https://ai-gateway-xxxxx-uc.a.run.app
    GATEWAY_URL=https://... python ui/run_local.py      # same thing

The URL is accepted as an argument as well as an env var because launcher configs
(.claude/launch.json, IDE run configurations) can pass arguments and often cannot pass
environment.
"""
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent

GATEWAY = (sys.argv[1] if len(sys.argv) > 1 else os.getenv("GATEWAY_URL", "")).rstrip("/")
os.environ["GATEWAY_URL"] = GATEWAY

if __name__ == "__main__":
    if not GATEWAY:
        print("Set GATEWAY_URL to the deployed gateway (or http://localhost:8080 for "
              "a local one).", file=sys.stderr)
        raise SystemExit(2)

    # Explicitly cleared, not merely unset: an inherited client id from another shell is
    # how you end up staring at a sign-in button that cannot work against localhost.
    os.environ.pop("GOOGLE_OAUTH_CLIENT_ID", None)
    sys.path.insert(0, str(HERE))

    import uvicorn

    print(f"UI on http://localhost:{os.getenv('PORT', '8090')} -> {GATEWAY}")
    uvicorn.run("server:app", host="127.0.0.1", port=int(os.getenv("PORT", "8090")))
