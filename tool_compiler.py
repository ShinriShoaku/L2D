"""
tool_compiler.py — Compile custom MCP tools (dari mcp_custom_tools.json)
jadi metadata ringkas siap-pakai router: alias pendek, short_desc,
trigger_phrases, pass_b_line, dst.

--- PATCH: storage per-tool, bukan 1 file gabungan ---
MCP tools bisa terus bertambah dari waktu ke waktu (didaftarkan user lewat
Settings UI), jadi TIDAK di-hardcode di sini. Setiap tool yang berhasil
di-compile disimpan sebagai .bin TERPISAH di folder tools/ (mis.
tools/get_weather.bin, tools/check_server.bin) — bukan 1 file besar berisi
semua tool. Manfaatnya:
  - Gampang di-cek/di-maintenance per tool (buka 1 file kecil, bukan cari
    di tengah file gabungan yang bisa membesar terus).
  - Kalau 1 file korup, tool lain tidak ikut kena.
  - _tool_hash() per tool tetap dipakai buat cache-hit: tool yang definisinya
    tidak berubah TIDAK di-generate ulang ke model (hemat LLM call) walau
    tool lain di sekitarnya berubah.

Alias (kode pendek seperti "gw" utk get_weather, "cs" utk check_server)
JUGA tidak di-hardcode — di-generate oleh model tiap kali ada tool baru
didaftarkan/berubah (lihat _generate_tool_metadata), dengan fallback
deterministik (_fallback_metadata) kalau LLM tidak tersedia/gagal.

File index kecil (tools/_index.bin) cuma nyimpan bundle_hash + compiled_at
— dipakai load_compiled_tools() buat tahu kapan perlu re-scan folder tools/
dari disk vs kapan boleh reuse cache in-memory (biar precheck(), yang
dipanggil TIAP turn chat, tidak perlu buka banyak file tiap kali kalau
tidak ada compile baru).
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import time
from typing import Dict, List, Optional

try:
    import msgpack as _pack
    def _dumps(obj) -> bytes: return _pack.packb(obj, use_bin_type=True)
    def _loads(b: bytes):     return _pack.unpackb(b, raw=False)
except ImportError:
    import pickle as _pkl
    def _dumps(obj) -> bytes: return _pkl.dumps(obj, protocol=4)
    def _loads(b: bytes):     return _pkl.loads(b)


# ═════════════════════════════════════════════════════════════════════════════
# STORAGE — per-tool .bin file di folder tools/
# ═════════════════════════════════════════════════════════════════════════════

def _tools_dir() -> str:
    d = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tools")
    os.makedirs(d, exist_ok=True)
    return d

# Dipertahankan sebagai nama yang bisa di-import (dipakai settings_ui.py) —
# dulu jalur ke 1 file gabungan, sekarang jalur ke folder (banyak .bin).
COMPILED_BIN = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tools")

_INDEX_FILE = "_index.bin"


def _index_path() -> str:
    return os.path.join(_tools_dir(), _INDEX_FILE)


def _safe_filename(name: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_\-]", "_", str(name))[:80] or "tool"


def _tool_bin_path(name: str) -> str:
    return os.path.join(_tools_dir(), f"{_safe_filename(name)}.bin")


def _write_tool_bin(name: str, meta: Dict) -> None:
    try:
        with open(_tool_bin_path(name), "wb") as f:
            f.write(_dumps(meta))
    except Exception:
        pass


def _read_tool_bin(name: str) -> Optional[Dict]:
    path = _tool_bin_path(name)
    if not os.path.exists(path):
        return None
    try:
        with open(path, "rb") as f:
            return _loads(f.read())
    except Exception:
        return None


# ═════════════════════════════════════════════════════════════════════════════
# HASH — deteksi perubahan tool (per-tool & bundle)
# ═════════════════════════════════════════════════════════════════════════════

_HASH_FIELDS = ("name", "type", "url", "method", "headers", "body", "category", "description", "params")


def _tool_hash(tool: Dict) -> str:
    """Hash 1 definisi tool — berubah kalau field relevan berubah."""
    payload = json.dumps(
        {k: tool.get(k) for k in _HASH_FIELDS},
        sort_keys=True, ensure_ascii=False, default=str,
    )
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()[:16]


def _tools_bundle_hash(tools: List[Dict]) -> str:
    """Hash keseluruhan daftar tool — berubah kalau ADA tool ditambah/
    dihapus/diubah, dipakai load_compiled_tools() utk cache invalidation."""
    hashes = sorted(_tool_hash(t) for t in tools if isinstance(t, dict) and t.get("name"))
    return hashlib.sha1("|".join(hashes).encode("utf-8")).hexdigest()[:16]


# ═════════════════════════════════════════════════════════════════════════════
# READ/WRITE gabungan — kompatibel dgn caller lama (settings_ui.py) yang
# masih mikir "1 cache dict", padahal di baliknya sekarang banyak file.
# ═════════════════════════════════════════════════════════════════════════════

def _read_compiled() -> Optional[Dict]:
    """
    Rekonstruksi {"bundle_hash", "compiled_at", "tools": {name: meta}} dari
    tools/_index.bin (bundle_hash/compiled_at) + scan tools/*.bin (isi tiap
    tool). Key `tools` dict diambil dari FIELD "name" di dalam tiap file
    (bukan dari nama file) — supaya tetap benar walau nama tool mengandung
    karakter yang di-sanitize di nama file.
    Return None kalau belum pernah ada compile sama sekali.
    """
    idx_path = _index_path()
    bundle_hash, compiled_at = "", 0
    if os.path.exists(idx_path):
        try:
            with open(idx_path, "rb") as f:
                idx = _loads(f.read())
            bundle_hash = idx.get("bundle_hash", "")
            compiled_at = idx.get("compiled_at", 0)
        except Exception:
            pass

    tools: Dict[str, Dict] = {}
    d = _tools_dir()
    for fn in os.listdir(d):
        if not fn.endswith(".bin") or fn == _INDEX_FILE:
            continue
        try:
            with open(os.path.join(d, fn), "rb") as f:
                meta = _loads(f.read())
            tname = meta.get("name") or fn[:-4]
            tools[tname] = meta
        except Exception:
            continue

    if not tools and not os.path.exists(idx_path):
        return None
    return {"bundle_hash": bundle_hash, "compiled_at": compiled_at, "tools": tools}


def _write_compiled(cache: Dict) -> None:
    """
    cache: {"bundle_hash":..., "compiled_at":..., "tools": {name: meta}}
    Tulis SETIAP tool ke .bin sendiri-sendiri (bukan 1 file gabungan). Tool
    lama yang sudah tidak ada lagi di cache["tools"] (dihapus/di-rename)
    otomatis dibersihkan .bin-nya — tidak ada file "hantu" yang nyangkut.
    """
    new_tools = cache.get("tools", {}) or {}

    old = _read_compiled() or {}
    old_tools = old.get("tools", {}) or {}
    for stale_name in set(old_tools.keys()) - set(new_tools.keys()):
        try:
            os.remove(_tool_bin_path(stale_name))
        except Exception:
            pass

    for name, meta in new_tools.items():
        meta = dict(meta)
        meta.setdefault("name", name)
        _write_tool_bin(name, meta)

    try:
        with open(_index_path(), "wb") as f:
            f.write(_dumps({
                "bundle_hash": cache.get("bundle_hash", ""),
                "compiled_at": cache.get("compiled_at", time.time()),
            }))
    except Exception:
        pass


# ═════════════════════════════════════════════════════════════════════════════
# GENERATE METADATA — kirim tool baru/berubah ke model, TIDAK di-hardcode
# ═════════════════════════════════════════════════════════════════════════════

_TOOL_GEN_SYS = """\
Kamu generator metadata untuk tool/MCP baru yang didaftarkan ke sistem.
Diberi definisi mentah 1 tool, buat metadata ringkas untuk dipakai router.

Output HANYA JSON satu baris:
{"alias":"xx","short_desc":"...","trigger_phrases":["...","..."],"sim_result":"..."}

Aturan:
- alias: kode PENDEK 2-4 huruf kecil, berbasis inisial/singkatan nama tool
  (mis. get_weather -> "gw", check_server -> "cs"). Hindari alias yang sudah
  umum dipakai tool bawaan sistem (gw, gum, gus, gtc, dst) kecuali memang
  tool ini fungsinya identik.
- short_desc: 1 kalimat Indonesia, jelas apa fungsi tool & apa yang dikembalikan.
- trigger_phrases: 3-6 kata/frasa (boleh campur Indonesia/Inggris) yang kalau
  muncul di chat user, kemungkinan besar butuh tool ini. Singkat, lowercase.
- sim_result: contoh singkat hasil simulasi/dummy tool ini (buat testing).
Jangan tulis apapun selain JSON.
"""


def _build_args_schema(params: List[Dict]) -> List[Dict]:
    return [
        {
            "name":     p.get("name", ""),
            "example":  p.get("example", p.get("name", "")),
            "required": bool(p.get("required", False)),
        }
        for p in (params or []) if isinstance(p, dict) and p.get("name")
    ]


def _build_pass_b_line(alias: str, short_desc: str, args_schema: List[Dict]) -> str:
    param_str = ", ".join(f"{a['name']}={a['example']}" for a in args_schema) or "no params"
    return f"- {alias}: {short_desc} | params: {param_str}"


def _generate_tool_metadata(tool: Dict, llm_call) -> Optional[Dict]:
    """
    Kirim definisi tool ke model (llm_call), minta di-generate: alias pendek,
    short_desc, trigger_phrases, sim_result. INI yang dimaksud "harus
    di-generate manual, kirim ke model" — tidak ada daftar alias hardcode
    di sini, semua diputuskan model per tool yang didaftarkan.
    Return None kalau llm_call tidak tersedia atau parsing gagal (caller
    fallback ke _fallback_metadata).
    """
    if llm_call is None:
        return None

    name   = tool.get("name", "")
    params = tool.get("params", []) or []
    param_desc = ", ".join(
        f"{p.get('name','?')}({'required' if p.get('required') else 'optional'})"
        for p in params if isinstance(p, dict)
    ) or "(tidak ada param)"

    payload = (
        f"name: {name}\n"
        f"type: {tool.get('type','http')}\n"
        f"description: {tool.get('description','(tidak ada deskripsi)')}\n"
        f"params: {param_desc}\n"
        f"url: {tool.get('url','')}\n"
        f"category: {tool.get('category','custom')}"
    )
    try:
        resp = llm_call(
            "react",
            messages=[
                {"role": "system", "content": _TOOL_GEN_SYS},
                {"role": "user",   "content": payload},
            ],
            temperature=0.2,
            max_tokens=200,
        )
        raw = (resp.choices[0].message.content or "").strip()
        raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw, flags=re.I).strip()
        data = json.loads(raw)

        alias = re.sub(r"[^a-z0-9_]", "", str(data.get("alias", "")).strip().lower())[:6]
        if not alias:
            alias = re.sub(r"[^a-z0-9]", "", name.lower())[:3] or "tl"

        short_desc = str(data.get("short_desc", "")).strip() or f"Tool kustom: {name}"

        trigger_phrases = [
            str(p).strip().lower() for p in (data.get("trigger_phrases") or [])
            if str(p).strip()
        ]
        if not trigger_phrases:
            trigger_phrases = [name.replace("_", " ")]

        sim_result = str(data.get("sim_result", "")).strip() or f"(simulasi: hasil dari {name})"

        args_schema = _build_args_schema(params)
        pass_b_line = _build_pass_b_line(alias, short_desc, args_schema)

        return {
            "alias":           alias,
            "short_desc":      short_desc,
            "args_schema":     args_schema,
            "sim_result":      sim_result,
            "trigger_phrases": trigger_phrases,
            "pass_b_line":     pass_b_line,
            "fallback":        False,
        }
    except Exception:
        return None


def _fallback_metadata(tool: Dict) -> Dict:
    """Fallback deterministik (tanpa LLM) — dipakai kalau llm_call tidak
    tersedia atau _generate_tool_metadata gagal parse."""
    name        = tool.get("name", "tool")
    params      = tool.get("params", []) or []
    args_schema = _build_args_schema(params)
    alias       = re.sub(r"[^a-z0-9]", "", name.lower())[:3] or "tl"
    short_desc  = tool.get("description") or f"Tool kustom: {name}"
    pass_b_line = _build_pass_b_line(alias, short_desc, args_schema)
    return {
        "alias":           alias,
        "short_desc":      short_desc,
        "args_schema":     args_schema,
        "sim_result":      f"(simulasi: hasil dari {name})",
        "trigger_phrases": [name.replace("_", " ")],
        "pass_b_line":     pass_b_line,
        "fallback":        True,
    }


# ═════════════════════════════════════════════════════════════════════════════
# BATCH COMPILE
# ═════════════════════════════════════════════════════════════════════════════

def compile_tools(llm_call=None, force: bool = False, verbose: bool = False) -> Dict[str, Dict]:
    """
    Compile semua custom tool (dari mcp_tools.load_custom_tools()).
    Cache-hit (hash tool sama & bukan force) → reuse metadata lama, TIDAK
    generate ulang ke model (hemat LLM call untuk tool yang tidak berubah).
    llm_call=None → semua tool pakai _fallback_metadata (mode tanpa LLM).
    """
    from mcp_tools import load_custom_tools

    to_compile = [t for t in load_custom_tools(force_reload=True) if t.get("name")]
    old        = _read_compiled() or {}
    old_tools  = old.get("tools", {})

    used_aliases: set = set()
    new_tools: Dict[str, Dict] = {}

    for tool in to_compile:
        name  = tool["name"]
        thash = _tool_hash(tool)
        old_entry = old_tools.get(name, {})

        if not force and old_entry.get("hash") == thash:
            meta = dict(old_entry)
            if verbose:
                print(f"[tool_compiler] {name} — cache hit")
        else:
            meta = _generate_tool_metadata(tool, llm_call)
            if meta is None:
                meta = _fallback_metadata(tool)
                if verbose:
                    print(f"[tool_compiler] {name} — fallback (LLM gagal/tidak ada)")
            elif verbose:
                print(f"[tool_compiler] {name} — generated via LLM")

        alias  = meta.get("alias") or name[:3].lower()
        orig, suffix = alias, 2
        while alias in used_aliases:
            alias = f"{orig}{suffix}"
            suffix += 1
        meta["alias"]    = alias
        meta["hash"]     = thash
        meta["name"]     = name
        meta["category"] = tool.get("category", "custom")
        meta["enabled"]  = True
        used_aliases.add(alias)
        new_tools[name] = meta

    bundle_hash = _tools_bundle_hash(to_compile)
    _write_compiled({
        "bundle_hash": bundle_hash,
        "compiled_at": time.time(),
        "tools":       new_tools,
    })
    return new_tools


# ═════════════════════════════════════════════════════════════════════════════
# PUBLIC READ API — dipakai task_router.py, mcp_tools.py
# ═════════════════════════════════════════════════════════════════════════════

# Cache in-memory ringan — precheck() manggil load_compiled_tools() TIAP
# turn chat, jadi jangan scan folder tools/ dari disk tiap kali kalau tidak
# ada compile baru sejak load terakhir (dicek via bundle_hash di index).
_load_cache: Dict = {"bundle_hash": None, "tools": {}}


def boot_check(llm_call=None, verbose: bool = False) -> Dict:
    """
    PATCH: dipanggil sekali saat startup — pastikan compiled tools (folder
    tools/) konsisten dengan mcp_custom_tools.json SAAT INI. Kalau ada tool
    baru/berubah yang belum ter-compile sejak load terakhir, otomatis
    compile (pakai llm_call kalau disediakan, else fallback deterministik)
    — supaya sistem tidak mulai dengan tool yang "ke-detect" load_custom_tools()
    tapi metadatanya (alias, trigger_phrases, dst) belum ada sama sekali.

    compile_tools() di dalamnya sudah cache-hit per-tool sendiri, jadi
    boot_check TIDAK akan re-generate tool yang tidak berubah walau bundle
    hash keseluruhan berubah karena ada 1 tool baru — cuma tool yang baru/
    berubah itu saja yang kena generate.

    Return dict ringkas: {"total": jumlah tool, "recompiled": 0 kalau cache
    sudah up-to-date (tidak ada compile_tools() dipanggil), else jumlah
    tool hasil compile_tools(), "ok": True}.
    """
    from mcp_tools import load_custom_tools

    raw_tools = [t for t in load_custom_tools(force_reload=True) if t.get("name")]
    current_bundle = _tools_bundle_hash(raw_tools)

    cached = _read_compiled()
    cached_bundle = (cached or {}).get("bundle_hash", "")

    if cached is not None and cached_bundle == current_bundle:
        if verbose:
            print(f"[tool_compiler] boot_check: {len(raw_tools)} tool(s), cache sudah up-to-date")
        return {"total": len(raw_tools), "recompiled": 0, "ok": True}

    if verbose:
        print("[tool_compiler] boot_check: bundle tool berubah sejak compile terakhir → compile...")
    new_tools = compile_tools(llm_call=llm_call, force=False, verbose=verbose)
    return {"total": len(new_tools), "recompiled": len(new_tools), "ok": True}


def load_compiled_tools() -> Dict[str, Dict]:
    """Return {name: meta} semua tool yang sudah di-compile."""
    idx_path = _index_path()
    current_hash = None
    if os.path.exists(idx_path):
        try:
            with open(idx_path, "rb") as f:
                current_hash = _loads(f.read()).get("bundle_hash")
        except Exception:
            current_hash = None

    if current_hash is not None and current_hash == _load_cache["bundle_hash"]:
        return _load_cache["tools"]

    compiled = _read_compiled()
    tools = (compiled or {}).get("tools", {})
    _load_cache["bundle_hash"] = current_hash
    _load_cache["tools"] = tools
    return tools


def get_alias_map() -> Dict[str, str]:
    """{alias: real_name} — dipakai task_router._parse_pass_b() buat resolve
    alias pendek (mis. 'gw') balik ke nama tool asli ('get_weather')."""
    return {
        meta["alias"]: name
        for name, meta in load_compiled_tools().items()
        if meta.get("alias")
    }


def build_pass_b_addon(category: str, tools: Optional[List[Dict]] = None) -> str:
    """Blok teks pass_b_line per tool untuk 1 kategori — buat injeksi
    tambahan ke prompt Pass B kalau caller tidak pakai jalur compiled_meta
    langsung (lihat task_router._build_pass_b_prompt untuk jalur utama)."""
    compiled = load_compiled_tools()
    if tools is None:
        names = [n for n, m in compiled.items() if str(m.get("category", "custom")).lower() == category]
    else:
        names = [t["name"] for t in tools if isinstance(t, dict) and t.get("name")]
    lines = []
    for n in names:
        meta = compiled.get(n)
        if meta and meta.get("pass_b_line"):
            lines.append(meta["pass_b_line"])
    return "\n".join(lines)
