import os
import re
from dotenv import load_dotenv
from neo4j import GraphDatabase
from langchain_openai import OpenAIEmbeddings, ChatOpenAI
from neo4j_graphrag.retrievers import HybridCypherRetriever
from neo4j_graphrag.types import RetrieverResultItem
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.output_parsers import StrOutputParser
from langchain_core.runnables import RunnableLambda, RunnablePassthrough

load_dotenv()


class RAGChain:
    def __init__(self):
        self.driver = GraphDatabase.driver(
            os.environ["NEO4J_URI"],
            auth=(os.environ["NEO4J_USERNAME"], os.environ["NEO4J_PASSWORD"]),
        )
        self.embeddings = OpenAIEmbeddings(
            model="text-embedding-3-large",
            base_url=os.environ["SUMOPOD_URI"] + "/v1",
            api_key=os.environ["SUMOPOD_KEY"],
        )
        self.llm = ChatOpenAI(
            model="gemini-3.8-flash",
            base_url=os.environ["9ROUTER_URI"] + "/v1",
            api_key=os.environ["9ROUTER_KEY"],
            temperature=0,
        )
        self._retriever = self._build_retriever()
        self._chain = self._build_chain()

    def _fmt(self, record):
        pasal_id = record.get("pasal_id") or ""
        nomor = record.get("nomor") or pasal_id.split(":")[-1]
        uu = record.get("uu") or ""
        jenis = record.get("jenis") or ""
        uu_id = record.get("uu_id") or pasal_id.split(":")[0]

        prefix = "PP" if jenis == "pp" else "UU"
        label = f"{prefix} {uu_id}" + (f" — {uu}" if uu else "")

        mel = [m for m in (record.get("melaksanakan") or []) if m and m.get("uu_id")]
        dasar = ", ".join(
            f"UU {m['uu_id']}" + (f" ({m['judul']})" if m.get("judul") else "")
            for m in mel
        ) if mel else ""

        header = f"### Pasal {nomor} — {label}"
        if dasar:
            header += f" (peraturan pelaksana dari {dasar})"
        bagian = [header]

        teks = (record.get("teks") or "").strip()
        bagian.append(teks)

        penj = (record.get("penjelasan") or "").strip()
        if penj and penj.lower() != "cukup jelas":
            bagian.append(f"[Penjelasan resmi] {penj}")

        for d in (record.get("diubah") or []):
            if not d or not d.get("teks"):
                continue
            bagian.append(
                f"[VERSI TERBARU — Pasal {d.get('target') or nomor} "
                f"{d.get('aksi')} oleh Ciptaker {d.get('dari')}]\n{d['teks'].strip()}"
            )

        rujuk = [r for r in (record.get("rujukan") or []) if r and r.get("pasal_id")]
        if rujuk:
            bagian.append(f"[Kaitan: {', '.join(r['pasal_id'] for r in rujuk)}]")
            for r in rujuk:
                isi = (r.get("teks") or "").strip()
                if not isi:
                    continue
                uu_r = r["pasal_id"].split(":")[0]
                bagian.append(
                    f"[Isi pasal yang dirujuk — Pasal {r['pasal_id'].split(':')[-1]} "
                    f"(UU {uu_r})]\n{isi}"
                )

        content = "\n".join(bagian)

        metadata = {
            "pasal_id": pasal_id,
            "nomor": nomor,
            "uu": uu,
            "jenis": jenis,
            "score": record.get("score"),
            "ada_perubahan": bool([d for d in (record.get("diubah") or []) if d and d.get("teks")]),
        }
        return RetrieverResultItem(content=content, metadata=metadata)



    def _build_retriever(self):
        query = """
            WITH node AS anchor, score
            OPTIONAL MATCH (anchor)-[:BAGIAN_DARI]->(u:UU)
            OPTIONAL MATCH (u)-[:MELAKSANAKAN]->(induk:UU)
            OPTIONAL MATCH (anchor)<-[e:MENGUBAH|MENGHAPUS|MENYISIPKAN]-(ubah:Pasal)
            OPTIONAL MATCH (anchor)-[:MERUJUK]->(rujuk:Pasal)
            WHERE rujuk.pasal_id <> anchor.pasal_id
            RETURN anchor.text_full AS teks,
                anchor.penjelasan AS penjelasan,
                anchor.pasal_id AS pasal_id,
                anchor.nomor AS nomor,
                anchor.uu_target AS uu_target,
                score,
                coalesce(u.judul,'') AS uu,
                u.jenis AS jenis,
                u.uu_id AS uu_id,
                collect(DISTINCT {
                    aksi: e.aksi,
                    teks: e.teks_baru,
                    dari: ubah.pasal_id,
                    target: e.pasal_target
                }) AS diubah,
                collect(DISTINCT {uu_id: induk.uu_id, judul: induk.judul}) AS melaksanakan,
                collect(DISTINCT {
                    pasal_id: rujuk.pasal_id,
                    teks: rujuk.text_full
                })[0..3] AS rujukan
        """
        return HybridCypherRetriever(
            self.driver,
            "pasal_embedding",
            "pasal_keyword",
            retrieval_query=query,
            embedder=self.embeddings,
            result_formatter=self._fmt,
        )

    def _get_docs(self, payload):
        question = payload["question"]
        # Lucene query parser crash pada karakter khusus ("/" di "PP 35/2021")
        aman = re.sub(r'[+\-&|!(){}\[\]^"~*?:\\/]', " ", question)
        raw = self._retriever.search(query_text=aman, top_k=payload.get("top_k", 3))
        return {
            "question": question,
            "docs": [
                { 
                    "pasal_id": i.metadata["pasal_id"],
                    "score": i.metadata["score"],
                    "content": i.content,
                }
                for i in raw.items
            ],
            "context": "\n\n---\n\n".join(i.content for i in raw.items),
        }

    def _build_chain(self):
        system_prompt = """Jawab HANYA dari konteks di bawah. Kalau konteks tak memuat jawabannya,
        bilang tidak tahu. Bila ada blok [VERSI TERBARU], itu hukum yang berlaku —
        pakai itu, dan sebut pasal lama hanya sebagai perbandingan.
        Jawab dalam prosa atau daftar bernomor biasa. Jangan menulis blok kode.
        """
        human_prompt = """Pertanyaan:
        {question}
        Konteks:
        {context}
        """

        prompt = ChatPromptTemplate.from_messages([
            ("system", system_prompt),
            ("human", human_prompt),
        ])

        return (
            RunnableLambda(self._get_docs)
            | RunnablePassthrough.assign(
                answer=prompt | self.llm | StrOutputParser()
            )
        )

    def ask(self, question, top_k=3):
        return self._chain.invoke({"question": question, "top_k": top_k})

    def close(self):
        self.driver.close()


if __name__ == "__main__":
    chain = RAGChain()
    try:
        result = chain.ask("regulasi BPJS")
        print(result["answer"])
        print("\n=== Docs ===")
        for d in result["docs"]:
            print(f"\n{d['score']:.3f} {d['pasal_id']}")
            print(d["content"][:200])
    finally:
        chain.close()
