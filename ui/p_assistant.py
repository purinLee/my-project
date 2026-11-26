import streamlit as st

from catalog_intel import ai
from catalog_intel.assistant import EXAMPLES, answer
from catalog_intel.db import query_df
from ui.common import cached_audit, data_version, db, now, page_header, pill


def _flush_llm_log() -> None:
    if ai.pending_calls():
        with db() as wc:
            ai.flush_call_log(wc)


def _llm_panel() -> None:
    with st.expander("LLM usage, latency and estimated cost log", expanded=False):
        if not ai.is_configured():
            st.info("No OPENAI_API_KEY is configured: classification and answers use the deterministic rules engine, and no LLM calls are made or logged.")
        else:
            st.caption(f"Model: `{ai.model_name()}` - daily cap {ai.max_calls_per_day()} calls per server process. Costs are estimates from public per-token prices.")
            if st.button("Run live connectivity test (1 tiny call)", key="llm_ping"):
                call = ai.ping()
                _flush_llm_log()
                (st.success if call and call.status == "ok" else st.error)(
                    f"status={call.status if call else 'n/a'} latency={call.latency_ms if call else '-'} ms tokens={call.prompt_tokens if call else '-'}+{call.completion_tokens if call else '-'} "
                    f"estimated cost={call.cost_usd if call else '-'}" + (f" error={call.error}" if call and call.error else "")
                )
        with db(readonly=True) as c:
            log = query_df(c, "SELECT created_at, purpose, model, status, latency_ms, prompt_tokens, completion_tokens, cost_usd, error FROM llm_call_log ORDER BY call_id DESC LIMIT 50")
            tot = c.execute("SELECT COUNT(*), COALESCE(SUM(status='ok'), 0), COALESCE(SUM(prompt_tokens), 0), COALESCE(SUM(completion_tokens), 0), SUM(cost_usd) FROM llm_call_log").fetchone()
        m1, m2, m3, m4 = st.columns(4)
        m1.metric("Calls logged", tot[0])
        m2.metric("Successful", tot[1])
        m3.metric("Tokens (in + out)", f"{tot[2] + tot[3]:,}")
        m4.metric("Estimated cost (USD)", "n/a" if tot[4] is None else f"${tot[4]:.4f}")
        if len(log):
            st.dataframe(log, hide_index=True, width="stretch")
        else:
            st.caption("No LLM calls recorded yet.")


def _run(question: str) -> None:
    try:
        with db(readonly=True) as c:
            reply = answer(question, c, cached_audit(data_version(), 3.5, 0.90), now())
    except Exception as exc:  # noqa: BLE001 - never crash the chat
        st.session_state.chat.append({"role": "assistant", "text": f"Sorry, I could not answer that: {exc}", "table": None, "meta": ""})
        return
    finally:
        _flush_llm_log()
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
    _llm_panel()
