"""Parser UU perubahan/omnibus (mis. UU Cipta Kerja).

Dokumen = UU 6/2023 (penetapan Perppu 2/2022 Cipta Kerja). Struktur:
  - Bagian ketentuan umum  : pasal 1..16 (definisi, tujuan, ruang lingkup) —
                             substansi UU/perppu itu sendiri.
  - Bagian perubahan       : klaster "Pasal N\nBeberapa ketentuan dalam
                             Undang-Undang ... diubah sebagai berikut:",
                             tiap klaster berisi operasi yang mengubah pasal
                             UU sektor (diubah / dihapus / disisipkan).

Output tunggal `pasal`, tiap item punya `jenis`:
  {"jenis": "ketentuan_umum", "pasal_id": "2/2022:1", "nomor": "1",
   "ayat": [...], "text_full": "...", "refs": [...]}

  {"jenis": "perubahan", "pasal_id": "2/2022:81:156", "pasal_perubahan": "81",
   "uu_target": "13/2003", "pasal_target": "156", "aksi": "diubah",
   "text_full": "..."}   # text_full = teks pasal hasil perubahan

  pasal_id perubahan = <uu>:<pasal klaster>:<pasal target>, satu node per
  operasi. Satu klaster (mis. Pasal 81) memuat puluhan operasi; kalau id-nya
  hanya per klaster, semua teks operasi bertabrakan jadi satu node.

Bagian penulisan/parsing ayat reuse parse_uu.py (UU murni). Khusus Ciptaker:
normalisasi OCR agresif (scan 1127 hal; 2O2O->2020, 2Ol4->2014, 0->O, 1->l/I/T).
"""
import re
import pymupdf
import parse_uu as P


### NORMALISASI OCR ###

# OCR salah baca digit: 0->O/Q, 1->l/I/T/t/L/|, 2->Z, 5->S, 8->B, 9->g/q.
# Ganti HANYA dalam konteks angka (tahun 4-digit, nomor setelah Pasal/Nomor/ayat/huruf).
_REPL_DIGIT = str.maketrans({
    "O": "0", "o": "0", "D": "0", "Q": "0",
    "l": "1", "I": "1", "T": "1", "t": "1", "L": "1", "|": "1",
    "Z": "2", "z": "2",
    "S": "5", "s": "5",
    "B": "8",
    "g": "9", "q": "9",
})
RE_TAHUN_CTX = re.compile(r"((?:Tahun|Tal\w+)\s+)([\w]{4})(?![\w])", re.I)
RE_NOMOR_CTX = re.compile(r"\b(Pasal|Nomor|ayat|huruf)\s*([\dOolITZzSsB|gqtL]{1,4}[A-Z]?)\b")


def _fix_digit_ctx(m: re.Match) -> str:
    return f"{m.group(1)} {m.group(2).translate(_REPL_DIGIT)}"


def _fix_tahun_ctx(m: re.Match) -> str:
    """Paksa 4 char setelah 'Tahun' jadi digit (konteks pasti angka tahun)."""
    s = m.group(2).translate(_REPL_DIGIT)
    return m.group(1) + s


def normalisasi_ocr(text: str) -> str:
    """Koreksi digit yang salah baca OCR dalam konteks angka."""
    text = RE_TAHUN_CTX.sub(_fix_tahun_ctx, text)
    text = RE_NOMOR_CTX.sub(_fix_digit_ctx, text)
    # "Pasal24" -> "Pasal 24" (spasi hilang akibat OCR)
    text = re.sub(r"\b(Pasal|ayat|huruf)(\d)", r"\1 \2", text)
    # "Pasal 1 1 1" -> "Pasal 111" (spasi dalam nomor)
    text = re.sub(
        r"\b(Pasal|Nomor)\s+(\d)\s+(\d)(?:\s+(\d))?",
        lambda m: f"{m.group(1)} " + "".join(g for g in m.groups()[1:] if g),
        text,
    )
    return text


### REGEX KLASTER & OPERASI ###

RE_PAGE_HEADER = re.compile(
    r"^(PRESIDEN|REPU[BE]L[IK]+\s+INDONES\w*|-?\s*\d+\s*-?|SALINAN)\s*$",
    re.I,
)
# Batas dua dokumen: cangkang UU penetapan | lampiran Perppu yang ditetapkan.
# Pasal sebelum penanda = UU 6/2023 (penetapan); sesudah = Perppu 2/2022 (isi).
RE_LAMPIRAN = re.compile(
    r"LAMPIRAN\s*\n.*?PERATURAN\s+PEMERINTAH\s+PENGGANT[I]?\s+UNDANG-?UNDANG\s+"
    r"REPUBLIK\s+INDONESIA\s*\n\s*NOMOR\s+(\d+)\s+TAHUN\s+(\d{4})",
    re.I | re.S,
)
# Header klaster UU sektor: "Pasal N" lalu (mungkin diantar preamble
# "Untuk memberikan kemudahan...") "beberapa ketentuan dalam Undang-Undang
# Nomor X Tahun YYYY". Preamble bisa beberapa baris — dibatasi ~400 char.
# Anchor klaster: frasa "beberapa ketentuan dalam Undang-Undang Nomor X Tahun Y"
# jauh lebih stabil daripada "Pasal N" (nomor pasal juga muncul di teks substansi).
RE_ANCHOR_KLASTER = re.compile(
    r"(?:beberapa ketentuan dalam|pengaturan baru(?: yang diatur)? dalam)\s+Undang-?\s*\n?"
    r"(?:Undang\s+)?(?:Republik Indonesia\s+)?(?:Nomor\s+)?(\d+)\s+(?:Tahun|Tal\w+)\s+(\d{4})",
    re.I,
)
# "Pasal N" di awal baris — dipakai untuk menemukan nomor pasal klaster ke belakang.
RE_PASAL_HEADER = re.compile(r"(?:^|\n)\s*Pasal\s*(\d+)\s*\n")
# Operasi perubahan. Nomor urut ("31. ") OPSIONAL — sebagian blok langsung
# "Ketentuan Pasal X diubah ..." tanpa nomor (mis. klaster Pasal 47, 82, 108).
_NOMOR_OPT = r"\A\s*(?:(\d{1,3})\.\s*)?"
RE_OP_UBAH = re.compile(
    _NOMOR_OPT + r"(?:Ketentuan\s+)?(?:beberapa\s+ketentuan\s+)?"
    r"Pasal\s+(\d+[A-Z]?)\s+diubah",
    re.I,
)
RE_OP_HAPUS = re.compile(
    _NOMOR_OPT + r"(?:Ketentuan\s+)?Pasal\s+(\d+[A-Z]?)\s+dihapus",
    re.I,
)
# sisip: "Di antara Pasal 4 dan Pasal 5 disisipkan 1 (satu) pasal, yakni
#        Pasal 4A sehingga berbunyi..." atau "Pasal N disisipkan".
# TANPA re.S: pembatas \\s+ saja, supaya match tidak lari menembus blok lain.
RE_OP_SISIP = re.compile(
    _NOMOR_OPT + r"(?:Ketentuan\s+)?"
    r"(?:di\s+antara\s+Pasal\s+(\d+[A-Z]?)\s+dan\s+Pasal\s+(\d+[A-Z]?)"
    r"[\s\S]{0,80}?disisipkan[\s\S]{0,80}?Pasal\s+(\d+[A-Z]?)"
    r"|Pasal\s+(\d+[A-Z]?)\s+disisipkan)",
    re.I,
)
# awal tiap operasi (utk batas blok teks_baru)
RE_OP_ANY = re.compile(
    r"(?:^|\n)\s*(?:(\d{1,3})\.\s+)?(?:Ketentuan\s+)?(?:beberapa\s+ketentuan\s+)?"
    r"(?:Pasal\s+\d+[A-Z]?\s+(?:diubah|dihapus|disisipkan)|di\s+antara\s+Pasal)",
    re.I,
)


def _deteksi_klaster(text: str) -> list[dict]:
    """Deteksi klaster UU sektor secara dua tahap.

    Tahap 1: temukan anchor "beberapa ketentuan dalam Undang-Undang ... Tahun Y".
    Tahap 2: mundur cari "Pasal N" (header pasal klaster) terdekat sebelumnya.
    Hasil: list {pasal, uu, tahun, start, body_start}.
    """
    out: list[dict] = []
    for a in RE_ANCHOR_KLASTER.finditer(text):
        pre = text[max(0, a.start() - 3000):a.start()]
        headers = list(RE_PASAL_HEADER.finditer(pre))
        if not headers:
            continue
        h = headers[-1]  # Pasal N terdekat sebelum anchor
        out.append({
            "pasal": h.group(1),
            "uu": f"{a.group(1)}/{a.group(2)}",
            "start": max(0, a.start() - 3000) + h.start(),
            "body_start": a.end(),
        })
    return out


def _clean_lines_ocr(doc) -> list[str]:
    """Ekstrak & bersihkan baris teks seluruh halaman, lalu normalisasi OCR."""
    out: list[str] = []
    for page in doc:
        for raw in page.get_text().split("\n"):
            line = raw.replace("ƒ", "").replace("�", "").rstrip()
            s = line.strip()
            if not s or RE_PAGE_HEADER.match(s):
                continue
            out.append(s)
    return normalisasi_ocr("\n".join(out)).split("\n")


def _parse_header(text: str) -> dict:
    m = re.search(
        r"UNDANG-UNDANG\s+REPUBLIK\s+INDONESIA\s*\n\s*NOMOR\s+(\d+)\s+TAHUN\s+(\d{4})",
        text, re.I,
    )
    if m:
        return {"nomor": m.group(1), "tahun": m.group(2), "uu_id": f"{m.group(1)}/{m.group(2)}"}
    return {"nomor": "", "tahun": "", "uu_id": ""}


# Judul dokumen: baris "TENTANG" diikuti nama, sampai marker berikutnya.
RE_TENTANG = re.compile(r"^TENTANG\s*$")
RE_STOP_JUDUL = re.compile(r"^(DENGAN RAHMAT|PRESIDEN|Menimbang|PERATURAN PEMERINTAH|UNDANG-UNDANG)", re.I)


def _parse_judul(lines: list[str]) -> list[str]:
    """Semua judul dari pasangan 'TENTANG\\n<judul>'. Urut sesuai dokumen.

    Satu PDF perubahan bisa punya >1 judul (cangkang penetapan + dokumen isi).
    """
    out: list[str] = []
    for i, l in enumerate(lines[:700]):
        if not RE_TENTANG.match(l):
            continue
        buf = []
        for nxt in lines[i + 1:i + 8]:
            if RE_STOP_JUDUL.match(nxt):
                break
            buf.append(nxt)
        judul = " ".join(buf).strip()
        judul = re.sub(r"\s+", " ", judul)
        if judul and judul not in out:
            out.append(judul)
    return out


def _judul_isi(lines: list[str], batas_lamp: int) -> str:
    """Judul dokumen isi = judul TENTANG terakhir (setelah lampiran), bukan penetapan."""
    setelah = _parse_judul(lines[batas_lamp:])
    if setelah:
        return setelah[-1]
    semua = _parse_judul(lines)
    return semua[-1] if semua else ""


def _ambil_teks_baru(blok: str, p_target: str) -> str:
    """Teks pasal hasil perubahan: blok setelah 'Pasal <target>'.

    Fallback ke setelah 'berbunyi ... berikut:' kalau header pasal tak ketemu
    (OCR rusak pada header, mis. '74'->'14', '228'->'22u'). Buang 'SK No ...'.
    """
    m = re.search(r"Pasal\s*" + re.escape(p_target) + r"\b\s*\n", blok)
    if m:
        teks = blok[m.end():]
    else:
        m2 = re.search(r"berikut\s*:?\s*\n", blok, re.I)
        if not m2:
            return ""
        teks = blok[m2.end():]
    teks = re.sub(r"SK\s+No\s+\S+\s+\w*\n?", "", teks)
    return re.sub(r"\n{2,}", "\n", teks.strip())


def _telusuri_operasi(seg: str) -> list[dict]:
    """Ekstrak operasi perubahan dari satu rentang klaster.

    Tiap blok diekstrak dari AWAL blok itu sendiri (match di-anchor `\\A`),
    supaya 'Pasal X diubah' di dalam badan teks blok lain tidak salah ambil.
    Nomor urut ("1. ") opsional.
    """
    starts = [m.start() for m in RE_OP_ANY.finditer(seg)]
    starts.append(len(seg))
    ops: list[dict] = []
    for i in range(len(starts) - 1):
        blok = seg[starts[i]:starts[i + 1]].strip()
        m_ubah, m_hapus, m_sisip = RE_OP_UBAH.match(blok), RE_OP_HAPUS.match(blok), RE_OP_SISIP.match(blok)
        if m_hapus:
            ops.append({"pasal_target": m_hapus.group(2), "aksi": "dihapus", "teks_baru": ""})
        elif m_ubah:
            ops.append({"pasal_target": m_ubah.group(2), "aksi": "diubah",
                        "teks_baru": _ambil_teks_baru(blok, m_ubah.group(2))})
        elif m_sisip:
            # grup: 1=nomor urut, 2=anchor (di antara Pasal N),
            #       3=pasal batas, 4=pasal baru, 5=pasal disisipkan polos
            target = m_sisip.group(4) or m_sisip.group(5) or ""
            anchor = m_sisip.group(2) or ""
            ops.append({"pasal_target": target, "aksi": "disisipkan",
                        "after": anchor, "teks_baru": _ambil_teks_baru(blok, target)})
    return ops


def parse_uu_perubahan(path: str) -> dict:
    doc = pymupdf.open(path)
    try:
        lines = _clean_lines_ocr(doc)
    finally:
        doc.close()

    text = "\n".join(lines)
    meta = _parse_header(text)
    meta["sumber"] = path
    meta["jenis"] = "perubahan"

    # Pisahkan cangkang UU penetapan (mis. 6/2023) dari lampiran Perppu (isi).
    # Semua batas dihitung dalam indeks baris (lines), bukan indeks karakter.
    m_lamp = RE_LAMPIRAN.search(text)
    if m_lamp:
        batas_lamp = text[:m_lamp.start()].count("\n")
        meta["uu_penetapan"] = meta["uu_id"]
        meta["uu_id"] = f"{m_lamp.group(1)}/{m_lamp.group(2)}"  # 2/2022
    else:
        batas_lamp = len(lines)

    # Judul dari PDF (jangan hardcode di insert).
    meta["judul"] = _judul_isi(lines, batas_lamp)
    meta["judul_penetapan"] = (_parse_judul(lines[:batas_lamp]) or [""])[0]

    klaster = _deteksi_klaster(text)
    batas_umum = text[: klaster[0]["start"]].count("\n") if klaster else len(lines)

    # 1a. Pasal cangkang UU penetapan (sebelum penanda LAMPIRAN).
    #     Buang blok PENJELASAN dulu agar 'Cukup jelas' tak jadi pasal palsu.
    body_pen, _ = P._split_body_and_penjelasan(lines[:batas_lamp])
    pasal_penetapan = P._parse_body(body_pen)
    for p in pasal_penetapan:
        p.pasal_id = f"{meta['uu_penetapan']}:{p.nomor}"
        p.refs = P._extract_refs(p.text_full, meta["uu_penetapan"], p.nomor)

    # 1b. Pasal ketentuan umum Perppu (lampiran s/d klaster pertama).
    pasal_umum = P._parse_body(lines[batas_lamp:batas_umum])
    for p in pasal_umum:
        p.pasal_id = f"{meta['uu_id']}:{p.nomor}"
        p.refs = P._extract_refs(p.text_full, meta["uu_id"], p.nomor)

    # 1c. Preamble tiap dokumen: UU penetapan dan lampiran Perppu punya sendiri.
    preamble = {
        "penetapan": P._extract_preamble(lines[:batas_lamp]),
        "perppu": P._extract_preamble(lines[batas_lamp:batas_umum]),
    }

    # 2. Bagian perubahan = operasi per klaster.
    # Batasi sampai awal PENJELASAN: blok penjelasan juga memuat frasa
    # "Pasal N diubah/dihapus" (kutipan) yang bukan operasi amendemen.
    i_pen = text.find("PENJELASAN", klaster[0]["start"] if klaster else 0)
    batas_ubah = i_pen if i_pen > 0 else len(text)
    pasal_ubah: list[dict] = []
    for i, kl in enumerate(klaster):
        awal = kl["body_start"]
        akhir = klaster[i + 1]["start"] if i + 1 < len(klaster) else batas_ubah
        for op in _telusuri_operasi(text[awal:akhir]):
            op["pasal_perubahan"] = kl["pasal"]
            op["uu_target"] = kl["uu"]
            pasal_ubah.append(op)

    # 3. Satukan jadi satu list pasal.
    # Pasal ketentuan umum Perppu punya struktur BAB/Bagian/Paragraf (mis. BAB I
    # KETENTUAN UMUM), jadi field itu dipertahankan. Penetapan (cangkang) tidak.
    pasal: list[dict] = []
    for p, jenis in [(p, "penetapan") for p in pasal_penetapan] + \
                    [(p, "ketentuan_umum") for p in pasal_umum]:
        d = P.asdict(p)
        d.pop("_mode", None)
        d["jenis"] = jenis
        for k in ("penjelasan", "urutan"):
            d.pop(k, None)
        if jenis == "penetapan":
            for k in ("bab", "bagian", "paragraf"):
                d.pop(k, None)
        pasal.append(d)
    for op in pasal_ubah:
        # Rujukan di teks hasil perubahan menunjuk ke pasal di UU TARGET,
        # bukan ke Ciptaker sendiri (mis. 'Pasal 8 ayat (2)' -> 26/2007:8).
        refs = [P.asdict(r) for r in P._extract_refs(op["teks_baru"], op["uu_target"], op["pasal_target"])]
        pasal.append({
            "jenis": "perubahan",
            "pasal_id": f"{meta['uu_id']}:{op['pasal_perubahan']}:{op['pasal_target']}",
            "nomor": op["pasal_perubahan"],
            "pasal_perubahan": op["pasal_perubahan"],
            "uu_target": op["uu_target"],
            "pasal_target": op["pasal_target"],
            "aksi": op["aksi"],
            "after": op.get("after", ""),  # utk disisipkan: disisipkan setelah pasal ini
            "text_full": op["teks_baru"],
            "ayat": [],
            "refs": refs,
        })

    meta["jumlah_klaster"] = len(klaster)
    meta["jumlah_penetapan"] = len(pasal_penetapan)
    meta["jumlah_ketentuan_umum"] = len(pasal_umum)
    meta["jumlah_perubahan"] = len(pasal_ubah)
    return {"meta": meta, "preamble": preamble, "pasal": pasal}


def demo() -> None:
    """Self-check: jalankan dan assert invarian hasil parse."""
    import collections

    d = parse_uu_perubahan("Documents/CIPTA KERJA NOMOR 2 TAHUN 2022.pdf")
    m, pasal = d["meta"], d["pasal"]

    assert m["uu_id"] == "2/2022", m          # isi = Perppu 2/2022
    assert m["uu_penetapan"] == "6/2023", m   # cangkang = UU 6/2023
    assert m["jumlah_klaster"] >= 60, m
    assert m["jumlah_ketentuan_umum"] == 16, m
    assert m["jumlah_penetapan"] == 2, m

    # semua pasal punya pasal_id unik (aman utk MERGE Neo4j). Operasi perubahan
    # satu klaster bertabrakan kalau id-nya per klaster, bukan per operasi.
    ids = [p["pasal_id"] for p in pasal]
    assert len(ids) == len(set(ids)), f"pasal_id duplikat: {len(ids)} vs {len(set(ids))}"

    per = [p for p in pasal if p["jenis"] == "perubahan"]
    assert per and all(p["aksi"] in ("diubah", "dihapus", "disisipkan") for p in per)
    assert all(p["uu_target"].count("/") == 1 for p in per)
    assert not [p for p in per if p["aksi"] == "diubah" and not p["text_full"]], "operasi 'diubah' tanpa teks"

    # tiap (klaster, pasal_target) unik — tidak ada operasi ganda
    seen = collections.Counter((p["pasal_perubahan"], p["pasal_target"]) for p in per)
    dup = {k: v for k, v in seen.items() if v > 1}
    assert not dup, f"operasi duplikat: {dup}"

    # preamble kedua dokumen terekstrak
    pr = d["preamble"]
    assert pr["penetapan"]["mengingat"], "penetapan tanpa legal basis"
    assert pr["perppu"]["mengingat"], "perppu tanpa legal basis"
    assert pr["perppu"]["menimbang"], "perppu tanpa menimbang"
    print("demo OK:", m)


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "demo":
        demo()
    else:
        import json
        from collections import Counter
        d = parse_uu_perubahan("Documents/CIPTA KERJA NOMOR 2 TAHUN 2022.pdf")
        print(json.dumps(d["meta"], indent=2, ensure_ascii=False))
        u = [p for p in d["pasal"] if p["jenis"] == "ketentuan_umum"]
        print("ketentuan umum:", len(u), "->", [p["nomor"] for p in u])
        print("aksi:", Counter(p["aksi"] for p in d["pasal"] if p["jenis"] == "perubahan"))
