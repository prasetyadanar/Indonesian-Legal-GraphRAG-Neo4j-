"""Insert hasil parse ke Neo4j — jalur UU murni dan UU perubahan.

Dua jalur, satu skema graph dasar:
    insert_uu_murni(driver, data)      # parse_uu.parse_uu
    insert_uu_perubahan(driver, data)  # parse_uu_perubahan.parse_uu_perubahan

Skema graph:
  (:UU {uu_id, nomor, tahun, judul, jenis})
  (:Pasal {pasal_id, nomor, jenis, text_full, ayat})
  (:Preamble {preamble_id, menimbang, mengingat, memutuskan})

  (Pasal)-[:BAGIAN_DARI]->(UU)
  (Pasal)-[:MERUJUK {tipe, ayat}]->(Pasal)        # rujukan internal
  (Pasal)-[:MERUJUK_UU]->(UU)                     # rujukan dokumen lain
  (UU)-[:PUNYA_PREAMBLE]->(Preamble)
  (Preamble)-[:HAS_LEGAL_BASIS]->(UU)

Khusus jalur perubahan (UU omnibus):
  (Pasal)-[:MENGUBAH   {aksi, uu_target}]->(Pasal target)
  (Pasal)-[:MENGHAPUS  {uu_target}]->(Pasal target)
  (Pasal)-[:MENYISIPKAN{after, uu_target}]->(Pasal target)

pasal_id jalur perubahan = <uu>:<pasal klaster>:<pasal target>, satu node per
operasi. Satu klaster Ciptaker (mis. Pasal 81) memuat puluhan operasi, jadi
id per klaster saja akan menabrakkan semua teks operasi dalam satu node.
"""
import os
import re

EDGE_BY_AKSI = {"diubah": "MENGUBAH", "dihapus": "MENGHAPUS", "disisipkan": "MENYISIPKAN"}

RE_UU_REF = re.compile(
    r"Undang[-\s]+Undang\s+(?:Republik Indonesia\s+)?Nomor\s+(\d+)\s+Tahun\s+(\d{4})", re.I)
RE_UUD = re.compile(r"Undang[-\s]+Undang Dasar", re.I)


def _run(driver, q, database="neo4j", **p):
    return driver.execute_query(q, database_=database, **p)


def _constraints(driver, database="neo4j"):
    _run(driver, "CREATE CONSTRAINT uu_id IF NOT EXISTS FOR (u:UU) REQUIRE u.uu_id IS UNIQUE", database)
    _run(driver, "CREATE CONSTRAINT pasal_id IF NOT EXISTS FOR (p:Pasal) REQUIRE p.pasal_id IS UNIQUE", database)
    _run(driver, "CREATE CONSTRAINT preamble_id IF NOT EXISTS FOR (p:Preamble) REQUIRE p.preamble_id IS UNIQUE", database)


def _upsert_uu(driver, uu_id, nomor, tahun, judul, jenis, database="neo4j"):
    _run(driver, """
    MERGE (u:UU {uu_id: $uu_id})
    SET u.nomor = $nomor, u.tahun = $tahun, u.judul = $judul, u.jenis = $jenis
    """, database, uu_id=uu_id, nomor=nomor, tahun=tahun, judul=judul, jenis=jenis)


def _upsert_struktur(driver, pasal: list[dict], database="neo4j"):
    """Node BAB + edge (Pasal)-[:DALAM_BAB]->(BAB), (BAB)-[:BAGIAN_DARI]->(UU).

    Bab/bagian/paragraf disimpan juga sebagai properti pasal supaya tetap
    bisa dibaca walau node BAB-nya belum tentu ada.
    """
    rows = []
    for p in pasal:
        bab = p.get("bab") or ""
        if not bab:
            continue
        uu_id = p["pasal_id"].split(":")[0]
        rows.append({
            "pasal_id": p["pasal_id"], "uu_id": uu_id, "bab": bab,
            "bab_key": f"{uu_id}:BAB:{bab}",
            "bagian": p.get("bagian", ""), "paragraf": p.get("paragraf", ""),
        })
    if not rows:
        return 0
    # properti bab/bagian/paragraf pada pasal
    _run(driver, """
    UNWIND $rows AS r
    MATCH (p:Pasal {pasal_id: r.pasal_id})
    SET p.bab = r.bab, p.bagian = r.bagian, p.paragraf = r.paragraf
    """, database, rows=rows)
    # node BAB unik
    bab_rows = [{"bab_key": r["bab_key"], "uu_id": r["uu_id"], "bab": r["bab"]}
                for r in ({x["bab_key"]: x for x in rows}).values()]
    _run(driver, """
    UNWIND $rows AS r
    MERGE (b:BAB {bab_key: r.bab_key})
    SET b.nomor = r.bab, b.uu_id = r.uu_id
    WITH b, r
    MERGE (u:UU {uu_id: r.uu_id})
    MERGE (b)-[:BAGIAN_DARI]->(u)
    """, database, rows=bab_rows)
    # edge Pasal -> BAB
    _run(driver, """
    UNWIND $rows AS r
    MATCH (p:Pasal {pasal_id: r.pasal_id})
    MATCH (b:BAB {bab_key: r.bab_key})
    MERGE (p)-[:DALAM_BAB]->(b)
    """, database, rows=rows)
    return len(bab_rows)


def _upsert_pasal(driver, pasal: list[dict], database="neo4j"):
    """MERGE semua pasal + edge BAGIAN_DARI ke UU induknya."""
    rows = [{
        "pasal_id": p["pasal_id"], "nomor": str(p["nomor"]), "jenis": p.get("jenis", "substansi"),
        "text_full": p.get("text_full", ""), "ayat": [a if isinstance(a, str) else a.get("text", "")
                                                        for a in p.get("ayat", [])],
        "uu_id": p["pasal_id"].split(":")[0],
    } for p in pasal]
    dedup: dict[str, dict] = {}
    for r in rows:
        dedup.setdefault(r["pasal_id"], r)
    _run(driver, """
    UNWIND $rows AS r
    MERGE (p:Pasal {pasal_id: r.pasal_id})
    SET p.nomor = r.nomor, p.jenis = r.jenis, p.text_full = r.text_full, p.ayat = r.ayat
    WITH p, r
    MERGE (u:UU {uu_id: r.uu_id})
    MERGE (p)-[:BAGIAN_DARI]->(u)
    """, database, rows=list(dedup.values()))
    return len(dedup)


def _upsert_refs(driver, pasal: list[dict], database="neo4j"):
    """Edge MERUJUK (pasal->pasal) dan MERUJUK_UU (pasal->UU) dari p['refs']."""
    pasal_refs, uu_refs = [], []
    for p in pasal:
        for r in p.get("refs", []):
            tipe = r.get("tipe")
            if tipe in ("pasal", "ayat") and r.get("pasal_id"):
                pasal_refs.append({"src": p["pasal_id"], "dst": r["pasal_id"],
                                   "tipe": tipe, "ayat": r.get("ayat", "")})
            elif tipe == "uu" and r.get("uu"):
                uu_refs.append({"src": p["pasal_id"], "dst_uu": r["uu"]})
    if pasal_refs:
        _run(driver, """
        UNWIND $rows AS r
        MERGE (t:Pasal {pasal_id: r.dst})
        ON CREATE SET t.jenis = 'target', t.nomor = split(r.dst, ':')[1]
        WITH t, r
        MATCH (s:Pasal {pasal_id: r.src})
        MERGE (s)-[e:MERUJUK {tipe: r.tipe, ayat: r.ayat}]->(t)
        """, database, rows=pasal_refs)
    if uu_refs:
        _run(driver, """
        UNWIND $rows AS r
        MERGE (t:UU {uu_id: r.dst_uu})
        ON CREATE SET t.jenis = 'rujukan'
        WITH t, r
        MATCH (s:Pasal {pasal_id: r.src})
        MERGE (s)-[:MERUJUK_UU]->(t)
        """, database, rows=uu_refs)
    return len(pasal_refs), len(uu_refs)


def _legal_basis(items: list[str]) -> list[str]:
    out: list[str] = []
    for s in items:
        for m in RE_UU_REF.finditer(s):
            out.append(f"{m.group(1)}/{m.group(2)}")
        if RE_UUD.search(s) and not RE_UU_REF.search(s):
            out.append("UUD/1945")
    seen, uniq = set(), []
    for u in out:
        if u not in seen:
            seen.add(u); uniq.append(u)
    return uniq


def _upsert_preamble(driver, pre_rows: list[dict], database="neo4j"):
    """pre_rows: [{preamble_id, uu_id, menimbang, mengingat, memutuskan}]."""
    if not pre_rows:
        return 0
    rows = [{**r, "legal_basis": _legal_basis(r["mengingat"])} for r in pre_rows]
    _run(driver, """
    UNWIND $rows AS r
    MERGE (pr:Preamble {preamble_id: r.preamble_id})
    SET pr.menimbang = r.menimbang, pr.mengingat = r.mengingat, pr.memutuskan = r.memutuskan
    WITH pr, r
    MERGE (u:UU {uu_id: r.uu_id})
    MERGE (u)-[:PUNYA_PREAMBLE]->(pr)
    """, database, rows=rows)
    lb = [{"preamble_id": r["preamble_id"], "tgt": t} for r in rows for t in r["legal_basis"]]
    if lb:
        _run(driver, """
        UNWIND $rows AS r
        MERGE (t:UU {uu_id: r.tgt})
        ON CREATE SET t.jenis = 'rujukan'
        WITH t, r
        MATCH (pr:Preamble {preamble_id: r.preamble_id})
        MERGE (pr)-[:HAS_LEGAL_BASIS]->(t)
        """, database, rows=lb)
    return len(rows)


def insert_uu_murni(driver, data: dict, database: str = "neo4j",
                    judul: str = "") -> dict:
    """Insert satu UU murni (parse_uu.parse_uu).

    judul: override opsional; kalau kosong pakai meta['judul'] dari parser.
    """
    meta, pasal = data["meta"], data["pasal"]
    judul = judul or meta.get("judul", "") or ""
    _constraints(driver, database)
    _upsert_uu(driver, meta["uu_id"], meta["nomor"], meta["tahun"], judul, "murni", database)
    n_pasal = _upsert_pasal(driver, pasal, database)
    n_bab = _upsert_struktur(driver, pasal, database)
    n_ref, n_refuu = _upsert_refs(driver, pasal, database)
    pr = data.get("preamble") or {}
    n_pre = _upsert_preamble(driver, [{
        "preamble_id": f"{meta['uu_id']}:preamble", "uu_id": meta["uu_id"],
        "menimbang": pr.get("menimbang", []), "mengingat": pr.get("mengingat", []),
        "memutuskan": pr.get("memutuskan", ""),
    }] if pr else [], database)
    return {"uu": meta["uu_id"], "pasal": n_pasal, "bab": n_bab,
            "merujuk": n_ref, "merujuk_uu": n_refuu, "preamble": n_pre}


def insert_uu_perubahan(driver, data: dict, database: str = "neo4j") -> dict:
    """Insert satu UU perubahan/omnibus (parse_uu_perubahan.parse_uu_perubahan)."""
    meta, pasal, preamble = data["meta"], data["pasal"], data["preamble"]
    _constraints(driver, database)

    judul = meta.get("judul", "") or "UU Perubahan"
    _upsert_uu(driver, meta["uu_id"], meta["nomor"], meta["tahun"], judul, "perubahan", database)
    if meta.get("uu_penetapan"):
        nom, thn = meta["uu_penetapan"].split("/")
        judul_pen = meta.get("judul_penetapan", "") or "Penetapan UU"
        _upsert_uu(driver, meta["uu_penetapan"], nom, thn, judul_pen, "penetapan", database)

    n_pasal = _upsert_pasal(driver, pasal, database)
    n_bab = _upsert_struktur(driver, pasal, database)
    n_ref, n_refuu = _upsert_refs(driver, pasal, database)

    ops = [p for p in pasal if p.get("jenis") == "perubahan"]
    edge_rows = [{"src": p["pasal_id"], "dst": f"{p['uu_target']}:{p['pasal_target']}",
                "aksi": p["aksi"], "uu_target": p["uu_target"],
                "after": p.get("after", ""),
                "teks_baru": p.get("text_full", ""),      # ← tambah
                "pasal_target": p["pasal_target"]}         # ← tambah
                for p in ops]
    for aksi, edge in EDGE_BY_AKSI.items():
        sub = [r for r in edge_rows if r["aksi"] == aksi]
        if not sub:
            continue
        _run(driver, f"""
        UNWIND $rows AS r
        MERGE (t:Pasal {{pasal_id: r.dst}})
        ON CREATE SET t.jenis = 'target', t.nomor = split(r.dst, ':')[1]
        WITH t, r
        MATCH (s:Pasal {{pasal_id: r.src}})
        MERGE (s)-[e:{edge} {{pasal_target: r.pasal_target}}]->(t)   // ← kunci pasal_target
        SET e.aksi = r.aksi, e.after = r.after,
            e.uu_target = r.uu_target, e.teks_baru = r.teks_baru
        """, database, rows=sub)

    pre_rows = []
    for kunci, jns in (("penetapan", meta.get("uu_penetapan")), ("perppu", meta["uu_id"])):
        pr = preamble.get(kunci) if isinstance(preamble, dict) else None
        if not pr or not jns:
            continue
        pre_rows.append({
            "preamble_id": f"{jns}:preamble", "uu_id": jns,
            "menimbang": pr.get("menimbang", []), "mengingat": pr.get("mengingat", []),
            "memutuskan": pr.get("memutuskan", ""),
        })
    n_pre = _upsert_preamble(driver, pre_rows, database)
    total_pasal = _run(driver, """
    MATCH (p:Pasal) WHERE split(p.pasal_id, ':')[0] IN [$uu, $pen]
    RETURN count(p) AS c
    """, database, uu=meta["uu_id"], pen=meta.get("uu_penetapan", ""))[0][0]["c"]
    return {"uu": meta["uu_id"], "pasal_eksplisit": n_pasal, "pasal_total": total_pasal,
            "bab": n_bab, "operasi": len(ops), "merujuk": n_ref,
            "merujuk_uu": n_refuu, "preamble": n_pre}
