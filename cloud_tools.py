"""
cloud_tools.py — Tool executor + NL parser untuk cloud_store.

Alur parse_cloud_command():
  1. Regex detect INTENT (write/read/list/search/delete) — cepat, tidak butuh LLM
  2. Untuk write/read/delete: kirim 1 request kecil ke model untuk ekstrak
     key/value/ns secara akurat dari teks natural user
  3. list/search: langsung dari regex (tidak butuh model)

llm_call di-inject dari task_router saat dipanggil.
"""

from __future__ import annotations
import json, re
from typing import Any, Dict, Optional
from cloud_store import cs_set, cs_get, cs_delete, cs_list, cs_search, cs_summary

# ─── Dispatcher ───────────────────────────────────────────────────────────────

def dispatch_cloud(f: str, args: Dict, user_id: str, is_admin: bool = False) -> str:
    ns_user   = f"user:{user_id}"
    ns_global = "global"

    if f == "cs_write":
        ns    = args.get("ns", ns_user)
        if ns == ns_global and not is_admin:
            return "[cloud] ❌ hanya admin yang bisa menulis ke namespace global"
        key   = str(args.get("key", "")).strip()
        value = args.get("value", "")
        label = str(args.get("label", ""))
        ttl   = int(args.get("ttl", 0))
        if not key:
            return "[cloud] ❌ key tidak boleh kosong"
        cs_set(ns, key, value, label=label, ttl=ttl)
        ttl_info = f", ttl={ttl}s" if ttl > 0 else ", permanent"
        return f"[cloud] ✅ tersimpan → {ns}::{key} = {str(value)[:80]}{ttl_info}"

    elif f == "cs_read":
        ns  = args.get("ns", ns_user)
        key = str(args.get("key", "")).strip()
        if not key:
            return "[cloud] ❌ key tidak boleh kosong"
        val = cs_get(ns, key)
        if val is None:
            return f"[cloud] key '{key}' tidak ditemukan di {ns}"
        return f"[cloud] {ns}::{key} = {val}"

    elif f == "cs_list":
        ns = args.get("ns", ns_user)
        return cs_summary(ns, max_items=15)

    elif f == "cs_search":
        ns    = args.get("ns", ns_user)
        query = str(args.get("q", ""))
        results = cs_search(ns, query)
        if not results:
            return f"[cloud] tidak ada hasil untuk '{query}' di {ns}"
        lines = [f"[cloud] hasil pencarian '{query}' di {ns}:"]
        for r in results[:10]:
            lines.append(f"  • {r['key']}: {str(r['value'])[:60]}")
        return "\n".join(lines)

    elif f == "cs_delete":
        ns  = args.get("ns", ns_user)
        if ns == ns_global and not is_admin:
            return "[cloud] ❌ hanya admin yang bisa hapus dari namespace global"
        key = str(args.get("key", "")).strip()
        if not key:
            return "[cloud] ❌ key tidak boleh kosong"
        ok = cs_delete(ns, key)
        return f"[cloud] {'✅ dihapus' if ok else '⚠️ tidak ditemukan'}: {ns}::{key}"

    return f"[cloud] ❌ fungsi tidak dikenal: {f}"


# ─── Intent detection (regex only, tidak perlu LLM) ──────────────────────────

_WRITE_PAT  = re.compile(r"\b(simpan|catat|tulis|tambah|store)\b.{0,80}(cloud|bin|storage|global)", re.I)
_READ_PAT   = re.compile(r"\b(ambil|baca|lihat|cek|tampilkan|show|get)\b.{0,60}(cloud|dari cloud|di cloud)", re.I)
_LIST_PAT   = re.compile(r"\b(list|tampilkan|isi|semua)\b.{0,40}\bcloud\b", re.I)
_DEL_PAT    = re.compile(r"\b(hapus|delete|remove)\b.{0,60}(cloud|dari cloud|di cloud)", re.I)
_SEARCH_PAT = re.compile(r"\b(cari|search)\b.{0,60}\bcloud\b", re.I)
_NS_PAT     = re.compile(r"\b(global)\b", re.I)   # default = user namespace


def _detect_intent(text: str) -> Optional[str]:
    """Return intent string atau None."""
    if _LIST_PAT.search(text):   return "list"
    if _SEARCH_PAT.search(text): return "search"
    if _DEL_PAT.search(text):    return "delete"
    if _READ_PAT.search(text):   return "read"
    if _WRITE_PAT.search(text):  return "write"
    return None


# ─── LLM extractor untuk write / read / delete ───────────────────────────────

_EXTRACT_SYS = """\
Ekstrak informasi dari perintah cloud user. Jawab HANYA JSON satu baris, tidak ada teks lain.

Format output:
{"key": "<snake_case_max_40_char>", "value": "<isi data, kosong jika bukan write>", "ns": "user|global", "label": "<deskripsi singkat, boleh kosong>", "ttl": 0}

Aturan:
- key: ringkas, snake_case, maksimal 40 karakter. Buat dari topik/isi data.
- value: isi data LENGKAP yang ingin disimpan (untuk write). Kosong untuk read/delete.
- ns: "global" hanya jika user EKSPLISIT sebut "global". Default "user".
- label: deskripsi singkat opsional (boleh kosong "").
- ttl: detik expiry (0 = permanent). Isi jika user sebut durasi.
- JANGAN tambahkan komentar atau teks di luar JSON.
"""

def _llm_extract(text: str, intent: str, llm_call) -> Optional[Dict]:
    """
    Kirim 1 request ke model untuk ekstrak key/value/ns dari teks user.
    Return dict hasil parsing atau None jika gagal.
    """
    user_msg = f'Intent: {intent}\nTeks user: "{text}"'
    try:
        resp = llm_call(
            "react",
            messages=[
                {"role": "system", "content": _EXTRACT_SYS},
                {"role": "user",   "content": user_msg},
            ],
            temperature=0.0,
            max_tokens=120,
        )
        raw = (resp.choices[0].message.content or "").strip()
        # Strip markdown fence kalau ada
        raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw, flags=re.I).strip()
        return json.loads(raw)
    except Exception:
        return None


# ─── Public: parse_cloud_command ─────────────────────────────────────────────

def parse_cloud_command(
    text:     str,
    user_id:  str,
    llm_call  = None,   # inject dari task_router; None → fallback regex
    is_admin: bool = False,
) -> Optional[Dict]:
    """
    Detect intent via regex, lalu ekstrak data via LLM (jika llm_call tersedia).
    Return {"f": "cs_*", "a": {...}} atau None jika bukan perintah cloud.
    """
    intent = _detect_intent(text)
    if not intent:
        return None

    ns_user = f"user:{user_id}"

    # ── list: tidak perlu LLM ────────────────────────────────────────────────
    if intent == "list":
        ns = "global" if _NS_PAT.search(text) else ns_user
        return {"f": "cs_list", "a": {"ns": ns}}

    # ── search: ambil query setelah "cari/search", tidak perlu LLM ──────────
    if intent == "search":
        ns = "global" if _NS_PAT.search(text) else ns_user
        q  = re.sub(r"^.*(cari|search)\s+", "", text, flags=re.I)
        q  = re.sub(r"\s*\bdi\s+cloud\b.*", "", q, flags=re.I).strip()
        return {"f": "cs_search", "a": {"ns": ns, "q": q}}

    # ── write / read / delete: gunakan LLM untuk akurasi ───────────────────
    if llm_call:
        extracted = _llm_extract(text, intent, llm_call)
    else:
        extracted = None

    # Fallback regex jika LLM gagal
    if not extracted:
        extracted = _regex_fallback(text, intent)

    if not extracted:
        return None

    # Resolve namespace
    raw_ns = str(extracted.get("ns", "user")).lower()
    ns = "global" if raw_ns == "global" else ns_user

    if intent == "write":
        key   = str(extracted.get("key", "")).strip()
        value = extracted.get("value", "")
        label = str(extracted.get("label", ""))
        ttl   = int(extracted.get("ttl", 0))
        if not key and value:
            key = re.sub(r"[^a-zA-Z0-9]+", "_", str(value).lower())[:40].strip("_")
        return {"f": "cs_write", "a": {"ns": ns, "key": key, "value": value, "label": label, "ttl": ttl}}

    elif intent == "read":
        key = str(extracted.get("key", "")).strip()
        return {"f": "cs_read", "a": {"ns": ns, "key": key}}

    elif intent == "delete":
        key = str(extracted.get("key", "")).strip()
        return {"f": "cs_delete", "a": {"ns": ns, "key": key}}

    return None


def _regex_fallback(text: str, intent: str) -> Optional[Dict]:
    """Fallback jika LLM tidak tersedia atau gagal parse."""
    quoted = re.search(r'["\u201c\u201d](.+?)["\u201c\u201d]', text)
    val    = quoted.group(1).strip() if quoted else ""

    if not val:
        # Ambil semua setelah kata kerja & preposisi & 'cloud'
        cleaned = re.sub(r"^.*(simpan|catat|tulis|tambah|baca|ambil|cek|hapus)\s+", "", text, flags=re.I)
        cleaned = re.sub(r"\b(ke|di|dari|dalam)\s+(cloud|bin)\b", "", cleaned, flags=re.I).strip()
        cleaned = re.sub(r"\bcloud\b", "", cleaned, flags=re.I).strip()
        val = cleaned

    key = re.sub(r"[^a-zA-Z0-9]+", "_", val.lower())[:40].strip("_") if val else ""
    return {"key": key, "value": val if intent == "write" else "", "ns": "user", "label": "", "ttl": 0}
