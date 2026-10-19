import streamlit as st
from rag import RAGChain


@st.cache_resource
def get_chain():
    return RAGChain()

top_k = st.sidebar.selectbox("Jumlah sumber", range(1, 11), index=2)

if "messages" not in st.session_state:
    st.session_state.messages = []


def tampilkan_sumber(docs):
    for d in docs:
        with st.expander(f"{d['pasal_id']} (score {d['score']:.3f})"):
            st.markdown(d["content"])

    
for m in st.session_state.messages:
    with st.chat_message(m["role"]):
        st.markdown(m["content"])
        if m.get("docs"):
            tampilkan_sumber(m["docs"])

if q := st.chat_input("Tanya soal UU..."):
    st.session_state.messages.append({"role": "user", "content": q})
    with st.chat_message("user"):
        st.markdown(q)

    with st.chat_message("assistant"):
        with st.spinner("Mencari..."):
            result = get_chain().ask(q, top_k=top_k)
        st.markdown(result["answer"])
        tampilkan_sumber(result["docs"])

    st.session_state.messages.append({
        "role": "assistant",
        "content": result["answer"],
        "docs": result["docs"],
    })
