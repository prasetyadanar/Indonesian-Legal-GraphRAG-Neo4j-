# GraphRAG for Indonesian Legislation

A GraphRAG prototype over Indonesian statutory law (*peraturan perundang-undangan*).
PDFs are parsed into a Neo4j property graph, then retrieved with a hybrid
vector + full-text + graph-traversal retriever and answered by an LLM.

Built as a baseline for a larger multi-hop / agentic retrieval project.

---

## Why a graph

Legal questions are rarely answered by one article in isolation:

- An article is **amended** by an omnibus law — the old text is misleading.
- An article **refers** to another article whose content you need.
- A government regulation (PP) **implements** a parent law (UU).

Plain vector RAG retrieves a single chunk and loses all of that. This project
attaches the relevant edges to every retrieved article, so one embedding lookup
carries one hop of legal context.

## Graph schema

```
(:UU {uu_id, nomor, tahun, judul, jenis})        jenis: murni | perubahan | penetapan | pp | rujukan
(:Pasal {pasal_id, nomor, jenis, text_full, ayat, penjelasan, bab, bagian, paragraf})
(:BAB {bab_key, nomor, uu_id})
(:Preamble {preamble_id, menimbang, mengingat, memutuskan})

(Pasal)-[:BAGIAN_DARI]->(UU)
(Pasal)-[:DALAM_BAB]->(BAB)-[:BAGIAN_DARI]->(UU)
(Pasal)-[:MERUJUK {tipe, ayat}]->(Pasal)         internal cross-reference
(Pasal)-[:MERUJUK_UU]->(UU)                      reference to another document
(UU)-[:PUNYA_PREAMBLE]->(Preamble)
(Preamble)-[:HAS_LEGAL_BASIS]->(UU)
(UU)-[:MELAKSANAKAN]->(UU)                       implementing regulation -> parent law

# amendments (omnibus path)
(Pasal)-[:MENGUBAH {aksi, uu_target, pasal_target, after, teks_baru}]->(Pasal)
(Pasal)-[:MENGHAPUS {...}]->(Pasal)
(Pasal)-[:MENYISIPKAN {...}]->(Pasal)
```

`pasal_id` format is `<uu>:<pasal>` for ordinary articles, and
`<uu>:<cluster>:<target>` for amendment operations — one node per operation,
because a single omnibus cluster (e.g. Cipta Kerja Pasal 81) contains dozens.

## Pipeline

```
PDF ──► script/parse_uu.py            (ordinary law)
   └──► script/parse_uu_perubahan.py  (amendment / omnibus law)
              │
              ▼
       script/graph.py  ──► Neo4j
              │
              ▼
       Neo4jVector (hybrid vector + fulltext indexes)
              │
              ▼
       rag.py  HybridCypherRetriever ──► LLM ──► Streamlit
```

**Parsing** — `pymupdf` extracts text, selecting the text layer when the page has
one and falling back to the OCR layer otherwise (scanned documents). Then:
page-header removal, line reflow, extraction of BAB/Bagian/Paragraf, articles,
paragraphs (`ayat`), lettered items, the official explanation section
(*Pasal demi pasal*), and the preamble (*Menimbang / Mengingat / Memutuskan*).

The omnibus parser additionally normalises OCR digit confusions
(`2O2O` → `2020`, `l`/`I`/`T` → `1`, …) inside numeric contexts, detects
amendment clusters, and splits each cluster into individual operations
(`diubah` / `dihapus` / `disisipkan`) with the replacement text.

**Retrieval** — a single Cypher expansion runs from each vector hit:

```cypher
WITH node AS anchor, score
OPTIONAL MATCH (anchor)-[:BAGIAN_DARI]->(u:UU)
OPTIONAL MATCH (u)-[:MELAKSANAKAN]->(induk:UU)
OPTIONAL MATCH (anchor)<-[e:MENGUBAH|MENGHAPUS|MENYISIPKAN]-(ubah:Pasal)
OPTIONAL MATCH (anchor)-[:MERUJUK]->(rujuk:Pasal)
    WHERE rujuk.pasal_id <> anchor.pasal_id
RETURN anchor.text_full AS teks, ...
       collect(DISTINCT {uu_id: induk.uu_id, judul: induk.judul}) AS melaksanakan,
       collect(DISTINCT {aksi: e.aksi, teks: e.teks_baru, ...}) AS diubah,
       collect(DISTINCT {pasal_id: rujuk.pasal_id, teks: rujuk.text_full})[0..3] AS rujukan
```

Each hit is rendered into a context block the LLM is instructed to prefer:

```
### Pasal 66 — PP 35/2021 — Perjanjian Kerja Waktu Tertentu, Alih Daya, ...
(peraturan pelaksana dari UU 2/2022 (CIPTA KERJA))
<journal article text>

[VERSI TERBARU — Pasal 66 diubah oleh Ciptaker 2/2022:81:66]
<replacement text from the omnibus law>

[Kaitan: 13/2003:59, 13/2003:66]
[Isi pasal yang dirujuk — Pasal 59 (UU 13/2003)]
<text of the referenced article>
```

## Setup

Requirements: Python 3.12+, [uv](https://docs.astral.sh/uv/), Docker.

```bash
uv sync

# Neo4j
cd Neo4j && docker compose up -d
```

Create `.env`:

```env
NEO4J_URI=neo4j://localhost:7687
NEO4J_USERNAME=neo4j
NEO4J_PASSWORD=passwordneo4j

SUMOPOD_URI=https://...      # OpenAI-compatible embeddings endpoint
SUMOPOD_KEY=...

9ROUTER_URI=https://...      # OpenAI-compatible chat completions endpoint
9ROUTER_KEY=...
```

Embeddings: `text-embedding-3-large`. Chat model: `gemini-3.8-flash`, temperature 0.

## Ingest

Put PDFs in `Documents/`, then run `insert_graph.ipynb`:

```python
import parse_uu as P
import graph as G

d = P.parse_uu("Documents/KETENAGAKERJAAN NOMOR 13 TAHUN 2003.pdf")
G.insert_uu_murni(driver, d, judul="Ketenagakerjaan")

d = parse_uu_perubahan.parse_uu_perubahan("Documents/CIPTA KERJA NOMOR 2 TAHUN 2022.pdf")
G.insert_uu_perubahan(driver, d)

# implementing regulation, linked to its parent law explicitly
d = P.parse_uu("Documents/PERATURAN PEMERINTAH NOMOR 35 TAHUN 2021.pdf")
G.insert_uu_murni(driver, d, jenis="pp", melaksanakan="2/2022")
```

`MELAKSANAKAN` is passed manually: the PP cites the revoked UU 11/2020, so the
textual reference does not point at the law currently in force.

Then build the hybrid indexes (`insert_graph.ipynb`):

```python
from langchain_neo4j import Neo4jVector

Neo4jVector.from_existing_graph(
    embedding=embeddings,
    url=..., username=..., password=...,
    index_name="pasal_embedding",
    keyword_index_name="pasal_keyword",
    node_label="Embedded",
    text_node_properties=["text_full"],
    embedding_node_property="embedding",
    search_type="hybrid",
)
```

## Usage

```python
from rag import RAGChain

chain = RAGChain()
try:
    result = chain.ask("Apa dasar hukum PP 35 Tahun 2021?", top_k=3)
    print(result["answer"])
    for doc in result["docs"]:
        print(doc["pasal_id"], doc["score"])
finally:
    chain.close()
```

Streamlit UI:

```bash
uv run streamlit run app.py
```

## Evaluation notes

Questions this baseline handles:

| Question | Mechanism |
| --- | --- |
| Which law does PP 35/2021 implement? | `MELAKSANAKAN` |
| What is the current version of Pasal 66 UU 13/2003? | `MENGUBAH` + `teks_baru` |
| What does the article referenced by Pasal 166 say? | `MERUJUK` + target text |
| What is the severance entitlement? | hybrid retrieval |

Questions it does **not** handle: query decomposition, iterative retrieval,
comparison across three or more documents, and anything requiring two or more
graph hops beyond the fixed expansion above.

## Known limitations

- **Incomplete corpus.** Out of 2,876 `MERUJUK` edges, 2,073 point at articles
  with no text — the referenced documents have not been ingested yet. The
  `[Isi pasal yang dirujuk]` block only appears for ingested targets.
- **No validity status.** There is no `status` / `DICABUT_OLEH` modelling, so a
  revoked law and its replacement coexist without a machine-readable marker.
- **No provenance.** Page numbers and source offsets are not stored per article.
- **Per-article embeddings.** Long articles are a single vector; the `ayat`
  granularity exists in the parser output but is not indexed.
- **No retry or self-correction.** One retrieval pass per question.

## Repository layout

```
app.py                       Streamlit chat UI
rag.py                       retriever, prompt, chain
chatbot.ipynb                interactive experiments
insert_graph.ipynb           ingestion + index creation
script/parse_uu.py           ordinary law parser
script/parse_uu_perubahan.py omnibus / amendment parser
script/graph.py              Neo4j upserts
Documents/                   source PDFs (not tracked)
Neo4j/docker-compose.yml     local Neo4j
```
