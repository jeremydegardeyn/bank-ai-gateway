"""Streamlit chat UI for the Bank AI Gateway demo. Shows the governance layer
working: tier badges, live budget meter, and PII-block banners."""
import os

import requests
import streamlit as st

GATEWAY_URL = os.environ.get("GATEWAY_URL", "http://localhost:8080")


def _auth_headers() -> dict:
    """On Cloud Run, mint an ID token for the private gateway (service-to-service
    auth via the metadata server). Locally, no auth is needed."""
    if GATEWAY_URL.startswith("http://localhost"):
        return {}
    try:
        import google.auth.transport.requests
        import google.oauth2.id_token
        token = google.oauth2.id_token.fetch_id_token(
            google.auth.transport.requests.Request(), GATEWAY_URL
        )
        return {"Authorization": f"Bearer {token}"}
    except Exception:
        return {}

st.set_page_config(page_title="Bank AI Gateway", page_icon="🏦", layout="centered")
st.title("🏦 Bank AI Assistant")
st.caption("All traffic passes through the enterprise AI gateway: PII screening, "
           "per-user token budgets, tiered model routing, full audit trail.")

with st.sidebar:
    st.header("Session")
    user_id = st.selectbox("User", ["demo-analyst", "demo-intern", "demo-manager"])
    tier = st.selectbox("Model tier", ["auto", "standard", "premium"])
    try:
        b = requests.get(f"{GATEWAY_URL}/v1/budget/{user_id}",
                         headers=_auth_headers(), timeout=5).json()
        st.metric("Daily token budget", f"{b['remaining']:,} left",
                  delta=f"-{b['tokens_used']:,} used")
        st.progress(min(1.0, b["tokens_used"] / max(1, b["daily_limit"])))
    except Exception:
        st.warning("Gateway unreachable — start it with `uvicorn app.main:app`.")

if "history" not in st.session_state:
    st.session_state.history = []

for msg in st.session_state.history:
    with st.chat_message(msg["role"]):
        st.markdown(msg["content"])
        if msg.get("meta"):
            st.caption(msg["meta"])

if prompt := st.chat_input("Ask something…"):
    st.session_state.history.append({"role": "user", "content": prompt})
    with st.chat_message("user"):
        st.markdown(prompt)

    payload = {"user_id": user_id, "message": prompt}
    if tier != "auto":
        payload["tier"] = tier
    r = requests.post(f"{GATEWAY_URL}/v1/chat", json=payload,
                      headers=_auth_headers(), timeout=120).json()

    with st.chat_message("assistant"):
        if r["outcome"] == "pii_blocked":
            st.error(r["reply"])
            meta = f"🛑 blocked by {r['pii']['engine']} · findings: {', '.join(r['pii']['findings'])}"
        elif r["outcome"] == "budget_exceeded":
            st.warning(r["reply"])
            meta = "💸 daily budget exhausted"
        else:
            st.markdown(r["reply"])
            u = r["usage"]
            meta = (f"tier: **{r['tier']}** · model: `{r['model']}` · "
                    f"{u['input_tokens']}→{u['output_tokens']} tokens · "
                    f"budget left: {r['budget']['remaining']:,}")
            if r["pii"]["prompt_redacted"]:
                meta += " · ✂️ prompt PII redacted"
        st.caption(meta)
    st.session_state.history.append({"role": "assistant", "content": r["reply"], "meta": meta})
