import re
import pymupdf
from dataclasses import asdict, dataclass, field


### REGEX PATTERN ###

RE_PAGE_HEADER = re.compile(
    r"^(PRESIDEN|REPUBLIK INDONESIA|-\s*\d+\s*-|LEMBARAN NEGARA.*)$"
)

RE_ELLIPSIS_TAIL = re.compile(r"(?:\.\.\.|…)\s*$")
RE_BAB = re.compile(r"^BAB\s+([IVXLC]+)\s*\.*\s*(.*)$")
RE_BAGIAN = re.compile(r"^Bagian\s+(\w+)\s*\.*\s*(.*)$")
RE_PARAGRAF = re.compile(r"^Paragraf\s+(\w+)\s*\.*\s*(.*)$")
RE_PASAL = re.compile(r"^Pasal\s+(\d+[A-Z]?)\s*$")
RE_PASAL_MAYBE = re.compile(r"^Pasal\s+(\d+[A-Z]?)\b\s*(.*)$")
RE_AYAT = re.compile(r"^\((\d+)\)\s*(.*)$")
RE_HURUF = re.compile(r"^([a-z])\.\s+(.*)$")
RE_ANGKA_ITEM = re.compile(r"^(\d+)\.\s*(.*)$")
RE_PENJELASAN = re.compile(r"^PENJELASAN", re.I)
RE_II_PDP = re.compile(r"^II\.\s*PASAL DEMI PASAL", re.I)
RE_TAIL = re.compile(r"^(TAMBAHAN LEMBARAN NEGARA)", re.I)

# Penanda preamble (mengikuti struktur LexID: consideration / legal basis / dictum).
# Hati-hati: watermark OCR seperti "Mengingat: . . ." atau "MEMUTUSKAN: . . ."
# bukan penanda asli — tolak baris yang isinya titik-titik saja.
RE_MENIMBANG = re.compile(r"^Menimbang\s*:?\s*\.*\s*$", re.I)
RE_MENGINGAT = re.compile(r"^(Mengingat|Memperhatikan)\s*:?\s*\.*\s*$", re.I)
RE_MEMUTUSKAN = re.compile(r"^MEMUTUSKAN\s*:?\s*\.*\s*$", re.I)
# item huruf (a., b., c.) yang berdiri sendiri di barisnya sendiri,
# atau tanpa titik karena OCR ('a' di baris terpisah)
RE_HURUF_ITEM = re.compile(r"^([a-z])\.?\s*$", re.I)
RE_REF_PASAL = re.compile(r"[Pp]asal\s+(\d+[A-Z]?)(?:\s+ayat\s+\((\d+[a-z]?)\))?")
RE_REF_UU = re.compile(
    r"Undang[- ]Undang\s+(?:Republik Indonesia\s+)?Nomor\s+(\d+)\s+Tahun\s+(\d{4})",
    re.I,
)

def _is_para_continue(prev: str, cur: str) -> bool:
    if not prev or not cur:
        return False
    if prev.endswith((".", ":", ";", "?", "!")):
        return False
    if RE_HURUF.match(cur) or RE_AYAT.match(cur) or RE_ANGKA_ITEM.match(cur):
        return False
    if cur[0].isupper():
        return False
    return True


def _flush_paragraph(buf: list[str]) -> str:
    """Gabungkan potongan baris jadi paragraf; kontraksi line-break."""
    if not buf:
        return ""
    txt = " ".join(buf)
    txt = re.sub(r"(\w)-\s+(\w)", r"\1-\2", txt)
    txt = re.sub(r"\s+", " ", txt)
    return txt.strip()


def _clean_lines(pages: list[str]) -> list[str]:
    """Buang header/footer halaman berulang, rapikan whitespace."""
    out: list[str] = []
    for page in pages:
        for raw in page.split("\n"):
            line = raw.replace("", "-").replace("�", "").strip()
            line = re.sub(r"\s+", " ", line)
            if not line:
                continue
            if RE_PAGE_HEADER.match(line):
                continue
            if RE_ELLIPSIS_TAIL.search(line):
                continue
            out.append(line)
    return out

@dataclass
class Ayat:
    nomor: str
    text: str


@dataclass
class Ref:
    """Rujukan terstruktur ke pasal/ayat/UU lain.

    tipe: "pasal"   -> pasal lain dalam UU yang sama
          "ayat"    -> ayat di pasal itu sendiri (self-reference)
          "uu"      -> dokumen UU lain
    """
    tipe: str
    pasal: str = ""      # nomor pasal target ("" utk tipe "uu")
    ayat: str = ""       # nomor ayat target (opsional)
    uu: str = ""         # "13/2003" utk tipe "uu"
    pasal_id: str = ""   # ID global target, mis. "13/2003:5" (kosong jika uu belum dikenal)


@dataclass
class Pasal:
    nomor: str
    bab: str = ""
    bagian: str = ""
    paragraf: str = ""
    ayat: list[Ayat] = field(default_factory=list)
    text_full: str = ""
    penjelasan: str = ""
    refs: list[Ref] = field(default_factory=list)
    urutan: int = 0
    pasal_id: str = ""
    _mode: str = ""

def _split_body_and_penjelasan(lines: list[str]) -> tuple[list[str], list[str]]:
    """Pisah badan UU dan bagian PENJELASAN (PASAL DEMI PASAL)."""
    start = next((i for i, l in enumerate(lines) if RE_PENJELASAN.match(l)), None)
    if start is None:
        return lines, []
    body = lines[:start]
    pen = lines[start:]
    i_pdp = next((i for i, l in enumerate(pen) if RE_II_PDP.match(l)), None)
    pen_body = pen[i_pdp + 1:] if i_pdp is not None else pen
    pen_body = [l for l in pen_body if not RE_TAIL.match(l)]
    return body, pen_body


def _parse_penjelasan(lines: list[str]) -> dict[str, str]:
    """Map {'1': 'Cukup jelas', '5': '...'} dari bagian PASAL DEMI PASAL."""
    result: dict[str, str] = {}
    cur: str | None = None
    buf: list[str] = []

    def flush() -> None:
        if cur is not None:
            result[cur] = _flush_paragraph(buf)

    for line in lines:
        m = RE_PASAL.match(line) or RE_PASAL_MAYBE.match(line)
        if m:
            flush()
            cur = m.group(1)
            rest = m.group(2) if m.re.groups >= 2 else ""
            buf = [rest] if rest.strip() else []
            continue
        if cur is None:
            continue
        buf.append(line)
    flush()
    return result


def _parse_body(lines: list[str]) -> list[Pasal]:
    """Turunkan daftar Pasal dari badan UU."""
    pasal_list: list[Pasal] = []
    cur: Pasal | None = None
    cur_bab = cur_bagian = cur_paragraf = ""
    buf: list[str] = []
    ayat_idx: int | None = None
    urutan = 0

    def flush_buf() -> None:
        nonlocal buf, ayat_idx
        if cur is None:
            buf = []
            return
        txt = _flush_paragraph(buf)
        buf = []
        if not txt:
            return
        if ayat_idx is not None:
            cur.ayat[ayat_idx].text = (cur.ayat[ayat_idx].text + " " + txt).strip()
        else:
            cur.text_full = (cur.text_full + " " + txt).strip()

    for line in lines:
        m_bab = RE_BAB.match(line)
        m_bag = RE_BAGIAN.match(line)
        m_par = RE_PARAGRAF.match(line)
        m_pas = RE_PASAL.match(line)

        if m_pas:  # pasal baru
            flush_buf()
            if cur:
                pasal_list.append(cur)
            urutan += 1
            cur = Pasal(
                nomor=m_pas.group(1),
                bab=cur_bab,
                bagian=cur_bagian,
                paragraf=cur_paragraf,
                urutan=urutan,
            )
            ayat_idx = None
            continue

        if m_bab:
            flush_buf()
            cur_bab, cur_bagian, cur_paragraf = m_bab.group(1), "", ""
            continue
        if m_bag:
            flush_buf()
            cur_bagian, cur_paragraf = m_bag.group(1), ""
            continue
        if m_par:
            flush_buf()
            cur_paragraf = m_par.group(1)
            continue

        if cur is None:
            continue  # masih preamble

        m_ay = RE_AYAT.match(line)
        # "(N)" = ayat baru, KECUALI didahului baris berakhir "ayat"/"huruf"
        # ("...dimaksud dalam ayat (1)"). Hanya ~9/412 kasus seperti ini di
        # UU 13/2003; sisanya ayat asli.
        prev = buf[-1].strip() if buf else ""
        is_ref = prev.lower().endswith(("ayat", "huruf"))
        if m_ay and not is_ref:
            flush_buf()
            cur.ayat.append(Ayat(nomor=m_ay.group(1), text=m_ay.group(2).strip()))
            ayat_idx = len(cur.ayat) - 1
            cur._mode = "paren"
            continue

        m_num = RE_ANGKA_ITEM.match(line)
        if m_num and cur._mode != "paren":
            flush_buf()
            cur._mode = "dot"
            cur.ayat.append(Ayat(nomor=m_num.group(1), text=m_num.group(2).strip()))
            ayat_idx = len(cur.ayat) - 1
            continue

        buf.append(line)

    flush_buf()
    if cur:
        pasal_list.append(cur)

    for p in pasal_list:
        if p.ayat:
            joined = " ".join(f"({a.nomor}) {a.text}" for a in p.ayat).strip()
            p.text_full = joined if not p.text_full else (joined + " " + p.text_full).strip()
        for a in p.ayat:
            a.text = re.sub(r"\s+", " ", a.text).strip()
        p.text_full = re.sub(r"\s+", " ", p.text_full).strip()
    return pasal_list


RE_REF_AYAT_SELF = re.compile(r"ayat\s+\((\d+[a-z]?)\)", re.I)
RE_REF_PASAL_AYAT = re.compile(
    r"[Pp]asal\s+(\d+[A-Z]?)\s+ayat\s+\((\d+[a-z]?)\)"
)


def _extract_refs(text: str, uu_id: str, nomor: str) -> list[Ref]:
    """Ekstrak rujukan terstruktur: pasal, ayat (self), dan UU lain.

    Deterministik (regex), tanpa LLM.
    """
    refs: list[Ref] = []

    # 1. UU lain (level dokumen)
    for m in RE_REF_UU.finditer(text):
        target = f"{m.group(1)}/{m.group(2)}"
        refs.append(Ref(tipe="uu", uu=target))

    # 2. Pasal + ayat: "Pasal 5 ayat (2)"
    for m in RE_REF_PASAL_AYAT.finditer(text):
        pn, an = m.group(1), m.group(2)
        if pn == nomor:  # self-reference ke pasal sendiri
            refs.append(Ref(tipe="ayat", pasal=pn, ayat=an, pasal_id=f"{uu_id}:{pn}"))
        else:
            refs.append(
                Ref(tipe="pasal", pasal=pn, ayat=an, pasal_id=f"{uu_id}:{pn}")
            )

    # 3. Pasal saja (yang belum tercakup pola pasal+ayat)
    for m in RE_REF_PASAL.finditer(text):
        pn = m.group(1)
        if m.group(2):  # sudah ditangani regex pasal+ayat
            continue
        if pn == nomor:
            continue
        refs.append(Ref(tipe="pasal", pasal=pn, pasal_id=f"{uu_id}:{pn}"))

    # 4. Ayat self tanpa nomor pasal: "sebagaimana dimaksud dalam ayat (1)"
    for m in RE_REF_AYAT_SELF.finditer(text):
        an = m.group(1)
        refs.append(Ref(tipe="ayat", pasal=nomor, ayat=an, pasal_id=f"{uu_id}:{nomor}"))

    # dedupe, jaga urutan
    seen: set[tuple] = set()
    out: list[Ref] = []
    for r in refs:
        key = (r.tipe, r.pasal, r.ayat, r.uu)
        if key in seen:
            continue
        seen.add(key)
        out.append(r)
    return out

def _parse_judul(lines: list[str]) -> str:
    """Judul dokumen: baris setelah 'TENTANG' sampai marker berikutnya."""
    RE_TENTANG = re.compile(r"^TENTANG\s*$")
    RE_STOP = re.compile(r"^(DENGAN RAHMAT|PRESIDEN|Menimbang|MEMUTUSKAN|BAB)", re.I)
    for i, l in enumerate(lines[:60]):
        if not RE_TENTANG.match(l):
            continue
        buf = []
        for nxt in lines[i + 1:i + 8]:
            if RE_STOP.match(nxt):
                break
            buf.append(nxt)
        judul = re.sub(r"\s+", " ", " ".join(buf)).strip()
        if judul:
            return judul
    return ""


def _extract_preamble(lines: list[str]) -> dict:
    """Ekstrak preamble: consideration (Menimbang), legal basis (Mengingat), dictum.

    Struktur mengikuti LexID: preamble = Menimbang + Mengingat + MEMUTUSKAN.
    Item dipisah per huruf (a., b., c.) lalu tiap item dijoin jadi satu paragraf.

    Catatan OCR: sebagian PDF (mis. Ciptaker) punya urutan halaman kacau sehingga
    baris 'Mengingat' bisa muncul di tengah-tengah daftar Menimbang, diikuti
    header halaman ('REPUBLIK INDONESIA'). Marker seperti itu ditolak; legal basis
    jatuh ke pencarian pola ('Pasal N ... UUD 1945', 'UU Nomor X Tahun Y').
    """
    i_men = i_ingat = i_putus = None
    for i, l in enumerate(lines[:500]):
        if i_men is None and RE_MENIMBANG.match(l):
            i_men = i
            continue
        if i_men is not None and RE_MENGINGAT.match(l):
            # tolak marker yang diikuti header halaman (bukan pasangan sejati)
            nxt = lines[i + 1] if i + 1 < len(lines) else ""
            if re.match(r"^(REPU[BE]L|PRESIDEN|REPUELIK|TNDONESIA|INDON)", nxt, re.I):
                continue
            if i_ingat is None:
                i_ingat = i
            continue
        if i_putus is None and RE_MEMUTUSKAN.match(l):
            i_putus = i
            break

    def _items(seg: list[str]) -> list[str]:
        parts: list[str] = []
        buf: list[str] = []
        for l in seg:
            if RE_HURUF_ITEM.match(l):
                if buf:
                    parts.append(_flush_paragraph(buf)); buf = []
                continue
            buf.append(l)
        if buf:
            parts.append(_flush_paragraph(buf))
        out: list[str] = []
        for p in parts:
            for piece in re.split(r";\s*", p):
                piece = piece.strip()
                if piece:
                    out.append(piece)
        return out

    # Batas atas Menimbang: marker Mengingat kalau ada, kalau tidak sampai MEMUTUSKAN.
    batas_men = i_ingat if i_ingat is not None else (i_putus if i_putus is not None else i_men)
    men_rows = lines[i_men + 1:batas_men] if i_men is not None and batas_men > i_men else []
    # buang baris header halaman yang nyangkut di awal (OCR halaman campur)
    men_rows = [l for l in men_rows
                if not re.match(r"^(REPU[BE]L|PRESIDEN|REPUELIK|TNDONESIA|INDON\w*)\b", l, re.I)]
    menimbang = _items(men_rows)

    # Legal basis: dari marker Mengingat (jika ada) atau dari seluruh rentang
    # Menimbang→MEMUTUSKAN. Ambil hanya frasa yang memang rujukan pasal/UU.
    RE_LEGAL = re.compile(
        r"(Pasal\s+\d+[A-Z]?(?:[^;]{0,120}?)(?:Undang[-\s]+Undang Dasar|UUD)[^;]{0,80}"
        r"|Undang[-\s]+Undang\s+(?:Republik Indonesia\s+)?Nomor\s+\d+\s+Tahun\s+\d{4}[^;]{0,60})",
        re.I,
    )
    lo = i_ingat + 1 if i_ingat is not None else (i_men + 1 if i_men is not None else 0)
    hi = i_putus if i_putus is not None else min(len(lines), lo + 200)
    seg_legal = " ".join(lines[lo:hi])
    mengingat: list[str] = []
    seen_norm: set[str] = set()
    for m in RE_LEGAL.finditer(seg_legal):
        s = re.sub(r"\s+", " ", m.group(1)).strip(" :,;")
        norm = re.sub(r"[-\s]+", "", s).lower()  # normalisasi hyphen/spasi utk dedup
        if norm and norm not in seen_norm:
            seen_norm.add(norm)
            mengingat.append(s)

    # dictum: hanya sampai baris BAB / Pasal pertama (bukan ikut body)
    dictum = ""
    if i_putus is not None:
        buf = []
        for l in lines[i_putus + 1:i_putus + 14]:
            if RE_BAB.match(l) or RE_PASAL.match(l):
                break
            if re.match(r"^(SK\s+No|Menetapkan|MEMUTUSKAN)", l, re.I):
                continue
            buf.append(l)
        dictum = _flush_paragraph(buf)
    return {"menimbang": menimbang, "mengingat": mengingat, "memutuskan": dictum}


def parse_uu(path: str) -> dict:
    doc = pymupdf.open(path)
    try:
        pages = [p.get_text() for p in doc]
    finally:
        doc.close()

    lines = _clean_lines(pages)
    body, pen_lines = _split_body_and_penjelasan(lines)
    pasal_list = _parse_body(body)
    penjelasan = _parse_penjelasan(pen_lines)

    meta: dict = {"sumber": path, "jumlah_pasal": len(pasal_list)}
    for l in lines[:60]:
        m = re.search(r"NOMOR\s+(\d+)\s+TAHUN\s+(\d{4})", l, re.I)
        if m:
            meta["nomor"], meta["tahun"] = m.group(1), m.group(2)
            break
    meta["uu_id"] = f"{meta.get('nomor')}/{meta.get('tahun')}"
    meta["judul"] = _parse_judul(lines)

    for p in pasal_list:
        p.penjelasan = penjelasan.get(p.nomor, "")
        p.pasal_id = f"{meta['uu_id']}:{p.nomor}"
        src = p.text_full + " " + p.penjelasan
        p.refs = _extract_refs(src, meta["uu_id"], p.nomor)

    pasal_json = []
    for p in pasal_list:
        d = asdict(p)
        d.pop("_mode", None)
        pasal_json.append(d)
    return {"meta": meta, "preamble": _extract_preamble(lines), "pasal": pasal_json}

if __name__ == "__main__":
    parse_uu()