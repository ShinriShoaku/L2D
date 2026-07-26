"""
router_cache.py — Binary cache untuk task router.

Format file: router_cache.bin
┌─────────────────────────────────────────────────────────┐
│ HEADER  4B magic "RTC\x01" + 4B entry_count (uint32)   │
├─────────────────────────────────────────────────────────┤
│ INDEX   entry_count × 8B (uint64 offset per entry)      │
├─────────────────────────────────────────────────────────┤
│ DATA    entry_count × variable-length record            │
│   each record:                                          │
│     4B  payload_len (uint32)                            │
│     NB  msgpack/pickle payload:                         │
│       q         str  — original query                   │
│       tokens    list — tokenized query                  │
│       calls     list — [{"f":..,"a":..}]                │
│       category  str  — Pass A category (debug)          │
│       ts        int  — unix timestamp                   │
│       hit_count int  — berapa kali cache hit            │
└─────────────────────────────────────────────────────────┘
"""

from __future__ import annotations
import os, re, struct, time
from typing import Dict, List, Optional, Tuple

# ── msgpack (cepat) → fallback pickle ────────────────────────────────────────
try:
    import msgpack as _pack
    def _dumps(obj) -> bytes: return _pack.packb(obj, use_bin_type=True)
    def _loads(b: bytes):     return _pack.unpackb(b, raw=False)
    _PACK_NAME = "msgpack"
except ImportError:
    import pickle as _pkl
    def _dumps(obj) -> bytes: return _pkl.dumps(obj, protocol=4)
    def _loads(b: bytes):     return _pkl.loads(b)
    _PACK_NAME = "pickle"

# ─── Config ───────────────────────────────────────────────────────────────────
CACHE_FILE  = "router_cache.bin"
MAX_ENTRIES = 300
TOP_K       = 5
MIN_OVERLAP = 0.25

_MAGIC   = b"RTC\x01"
_HDR_FMT = ">4sI"   # magic + count        = 8B
_HDR_SZ  = struct.calcsize(_HDR_FMT)
_IDX_FMT = ">Q"     # uint64 offset         = 8B
_IDX_SZ  = struct.calcsize(_IDX_FMT)
_LEN_FMT = ">I"     # uint32 payload_len    = 4B
_LEN_SZ  = struct.calcsize(_LEN_FMT)

CONFIRM_SYS = (
    "Jawab HANYA: yes atau no\n"
    "yes = kedua kalimat meminta HAL YANG SAMA (tool/aksi identik)\n"
    "no  = berbeda tujuan atau butuh tool berbeda\n"
    "Abaikan perbedaan nama, angka, atau gaya bahasa."
)

_STOP = {
    "aku","kamu","dia","yang","dan","di","ke","dari","untuk","apa","ini","itu",
    "ya","ada","tidak","dengan","atau","juga","bisa","coba","dong","deh","sih",
    "lah","nih","bang","kak","please","tolong","boleh","mau","gimana","bagaimana",
}

# Sinonim: semua kata dalam grup dinormalisasi ke kata pertama
_SYN = {
    "cek":"cek","check":"cek","lihat":"cek","baca":"cek","tampilkan":"cek","show":"cek",
    "data":"memory","info":"memory","informasi":"memory","memory":"memory","memori":"memory",
    "simpan":"simpan","save":"simpan","catat":"simpan","store":"simpan","tulis":"simpan",
    "hapus":"hapus","delete":"hapus","remove":"hapus",
    "cuaca":"cuaca","weather":"cuaca","suhu":"cuaca",
    "gifter":"gifter","hadiah":"gifter","gift":"gifter",
    "waktu":"waktu","jam":"waktu","time":"waktu","tanggal":"waktu","date":"waktu",
}

def _tokenize(text: str) -> List[str]:
    words = re.findall(r"[a-zA-Z0-9]+", text.lower())
    result = []
    for w in words:
        if w in _STOP or len(w) <= 1:
            continue
        result.append(_SYN.get(w, w))
    return result

def _jaccard(a: List[str], b: List[str]) -> float:
    sa, sb = set(a), set(b)
    if not sa and not sb: return 1.0
    union = len(sa | sb)
    return len(sa & sb) / union if union else 0.0

def _cache_path() -> str:
    base = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cache")
    os.makedirs(base, exist_ok=True)
    return os.path.join(base, CACHE_FILE)

# ─── Binary I/O ───────────────────────────────────────────────────────────────

def _read_all_entries() -> List[Dict]:
    path = _cache_path()
    if not os.path.exists(path): return []
    try:
        with open(path, "rb") as f:
            hdr = f.read(_HDR_SZ)
            if len(hdr) < _HDR_SZ: return []
            magic, count = struct.unpack(_HDR_FMT, hdr)
            if magic != _MAGIC or count == 0: return []
            idx_raw = f.read(_IDX_SZ * count)
            offsets = [struct.unpack(_IDX_FMT, idx_raw[i*_IDX_SZ:(i+1)*_IDX_SZ])[0] for i in range(count)]
            entries = []
            for off in offsets:
                f.seek(off)
                lr = f.read(_LEN_SZ)
                if len(lr) < _LEN_SZ: continue
                plen = struct.unpack(_LEN_FMT, lr)[0]
                pr = f.read(plen)
                if len(pr) < plen: continue
                try: entries.append(_loads(pr))
                except Exception: pass
        return entries
    except Exception: return []

def _write_all_entries(entries: List[Dict]):
    path = _cache_path()
    payloads = []
    for e in entries:
        try: payloads.append(_dumps(e))
        except Exception: pass
    count = len(payloads)
    data_start = _HDR_SZ + _IDX_SZ * count
    offsets, cur = [], data_start
    for p in payloads:
        offsets.append(cur)
        cur += _LEN_SZ + len(p)
    try:
        with open(path, "wb") as f:
            f.write(struct.pack(_HDR_FMT, _MAGIC, count))
            for off in offsets: f.write(struct.pack(_IDX_FMT, off))
            for p in payloads:
                f.write(struct.pack(_LEN_FMT, len(p)))
                f.write(p)
    except Exception: pass

def _append_entry(entry: Dict):
    entries = _read_all_entries()
    for e in entries:
        if e.get("q") == entry.get("q"):
            e["hit_count"] = e.get("hit_count", 0) + 1
            e["ts"] = entry.get("ts", int(time.time()))
            _write_all_entries(entries)
            return
    entries.append(entry)
    if len(entries) > MAX_ENTRIES:
        entries = entries[-MAX_ENTRIES:]
    _write_all_entries(entries)

# ─── Public API ───────────────────────────────────────────────────────────────

def find_candidates(user_input: str) -> List[Tuple[float, Dict]]:
    q_tok = _tokenize(user_input)
    scored = [((_j := _jaccard(q_tok, e.get("tokens", []))), e)
              for e in _read_all_entries() if _jaccard(q_tok, e.get("tokens", [])) >= MIN_OVERLAP]
    scored.sort(key=lambda x: x[0], reverse=True)
    return scored[:TOP_K]

def confirm_match(user_input: str, candidate_q: str, llm_call) -> bool:
    try:
        resp = llm_call("react",
            messages=[{"role":"system","content":CONFIRM_SYS},
                      {"role":"user","content":f'A: "{user_input}"\nB: "{candidate_q}"'}],
            temperature=0.0, max_tokens=5)
        raw = (resp.choices[0].message.content or "").strip().lower()
        return raw.startswith("yes") or raw.startswith("ya")
    except Exception: return False

# Tool yang bersifat "always-on" / default context — tidak merepresentasikan
# intent spesifik user, jadi tidak layak di-cache sendiri.
# Cache hanya berguna kalau ada tool dengan intent jelas (gum, get_weather, gtg, dll).
# cs_* juga di-blacklist karena value-nya unik per request — tidak bisa di-replay.
# Tool yang TIDAK boleh di-cache:
# - "always-on" context tools: dipanggil hampir setiap request, bukan intent spesifik
# - cs_* cloud tools: value-nya unik per request, tidak bisa di-replay
_CACHE_BLACKLIST = {
    # always-on context tools
    "grc", "gmos", "gme", "grr",
    "gsm", "gsi", "gsd", "gvc", "gca", "gav",  # state tools
    "gts", "gcs", "sc",                           # chat summary tools
    "gtc", "gpe", "gan",                          # time/event tools (selalu berubah)
    # cloud tools (value unik per request)
    "cs_write", "cs_read", "cs_list", "cs_search", "cs_delete",
}

def save(user_input: str, calls: List[Dict], category: str = ""):
    if not calls: return

    # Hanya simpan jika minimal 1 call bukan dari blacklist
    # Artinya: ada tool yang benar-benar di-trigger oleh intent user
    meaningful = [c for c in calls if c.get("f") not in _CACHE_BLACKLIST]
    if not meaningful:
        return   # semua tool adalah "context default" — tidak perlu di-cache

    # Simpan hanya calls yang meaningful (strip args kontekstual yg bisa basi)
    calls_clean = []
    for c in meaningful:
        a_clean = {k: v for k, v in c.get("a", {}).items()
                   if k not in ("username", "chat", "id")}
        calls_clean.append({"f": c["f"], "a": a_clean})

    _append_entry({
        "q":         user_input,
        "tokens":    _tokenize(user_input),
        "calls":     calls_clean,
        "category":  category,
        "ts":        int(time.time()),
        "hit_count": 0,
    })

def _is_valid_entry(entry: Dict) -> bool:
    """
    Validasi entry sebelum di-return dari lookup.
    Entry invalid jika:
    - Semua calls masuk blacklist (cs_*, gmos, grc, dst)
    - cs_write dengan key/value kosong (entry corrupt dari sebelum fix)
    - Tidak ada calls sama sekali
    """
    calls = entry.get("calls", [])
    if not calls:
        return False
    for c in calls:
        f = c.get("f", "")
        if f in _CACHE_BLACKLIST:
            return False
        # cs_write khusus: key dan value harus ada
        if f == "cs_write":
            a = c.get("a", {})
            if not a.get("key") or not a.get("value"):
                return False
    return True


def purge_invalid() -> int:
    """Hapus semua entry invalid/corrupt dari cache. Return jumlah yang dihapus."""
    entries = _read_all_entries()
    valid   = [e for e in entries if _is_valid_entry(e)]
    removed = len(entries) - len(valid)
    if removed > 0:
        _write_all_entries(valid)
    return removed


def lookup(user_input: str, llm_call, dbg=None) -> Optional[List[Dict]]:
    def _log(msg):
        if dbg: dbg.line(msg)
    candidates = find_candidates(user_input)
    if not candidates:
        _log("  [CACHE] miss")
        return None
    _log(f"  [CACHE] {len(candidates)} kandidat | pack={_PACK_NAME}")
    for score, entry in candidates:
        cq = entry.get("q", "")

        # Skip entry yang invalid/corrupt (blacklisted atau args kosong)
        if not _is_valid_entry(entry):
            _log(f"  [CACHE] ⚠️  skip invalid entry q={cq[:40]!r}")
            continue

        _log(f"  [CACHE] score={score:.2f} hit={entry.get('hit_count',0)} cat={entry.get('category','?')} q={cq[:50]!r}")
        if confirm_match(user_input, cq, llm_call):
            all_e = _read_all_entries()
            for e in all_e:
                if e.get("q") == cq:
                    e["hit_count"] = e.get("hit_count", 0) + 1
                    break
            _write_all_entries(all_e)
            calls = entry.get("calls", [])
            _log(f"  [CACHE] ✅ HIT — calls={[c['f'] for c in calls]}")
            return calls
    _log("  [CACHE] ❌ tidak ada match")
    return None

def stats() -> Dict:
    entries = _read_all_entries()
    if not entries: return {"count": 0, "pack": _PACK_NAME}
    path = _cache_path()
    return {
        "count":      len(entries),
        "max":        MAX_ENTRIES,
        "pack":       _PACK_NAME,
        "file_kb":    round(os.path.getsize(path) / 1024, 1) if os.path.exists(path) else 0,
        "top_hits":   sorted(entries, key=lambda e: e.get("hit_count", 0), reverse=True)[:3],
        "categories": list({e.get("category","?") for e in entries}),
    }
