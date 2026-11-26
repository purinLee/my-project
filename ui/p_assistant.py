import streamlit as st

from catalog_intel import ai
from catalog_intel.assistant import EXAMPLES, answer
from ui.common import cached_audit, data_version, db, now, page_header, pill


def _run(question: str) -> None:
    try:
        with db(readonly=True) as c:
            reply = answer(question, c, cached_audit(data_version(), 3.5, 0.90), now())
    except Exception as exc:  # noqa: BLE001 - never crash the chat
        st.session_state.chat.append({"role": "assistant", "text": f"Sorry, I could not answer that: {exc}", "table": None, "meta": ""})
        return
    st.session_state.chat.append({"role": "assistant", "text": reply.text, "table": reply.table, "meta": f"intent: {reply.intent} | mode: {reply.mode} | sources: {', '.join(reply.sources) or 'n/a'}"})


def render() -> None:
    page_header("AI Operations Assistant", "Ask your operations data", "Questions are answered from live SQL analytics and the catalog audit. Prefix with 'Classify:' to triage a new request text.")
    st.markdown(
        pill("LLM-assisted (with fallback)" if ai.is_configured() else "Deterministic rules engine", "good" if ai.is_configured() else "neutral")
        + " " + pill("Grounded in database", "info"), unsafe_allow_html=True,
    )
    st.session_state.setdefault("chat", [{"role": "assistant", "text": "Hi - I'm your catalog operations analyst. Ask about SLA risk, backlog, rework, processing time, catalog quality, or paste a request to classify.", "table": None, "meta": ""}])
    st.write("")
    cols = st.columns(3)
    for i, ex in enumerate(EXAMPLES):
        if cols[i % 3].button(ex, key=f"ex_{i}", width="stretch"):
            st.session_state.chat.append({"role": "user", "text": ex})
            _run(ex)
    for msg in st.session_state.chat:
        with st.chat_message(msg["role"]):
            st.markdown(msg["text"])
            if msg.get("table") is not None and len(msg["table"]):
                st.dataframe(msg["table"], hide_index=True, width="stretch")
            if msg.get("meta"):
                st.caption(msg["meta"])
    if prompt := st.chat_input("Ask about SLA, backlog, rework, duplicates... or 'Classify: <request text>'"):
        st.session_state.chat.append({"role": "user", "text": prompt})
        _run(prompt)
        st.rerun()
    if st.button("Clear conversation"):
        del st.session_state["chat"]
        st.rerun()
