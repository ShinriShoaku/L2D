#!/usr/bin/env python3
"""
tool_compiler.py — Auto-compile custom MCP tools menjadi cached prompt.

Flow:
  1. Baca mcp_custom_tools.json
  2. Hitung hash setiap tool (name+desc+url+params)
  3. Jika hash berubah dari cache → generate ulang via LLM
  4. Simpan ke mcp_tools_compiled.bin (msgpack)

Isi compiled per tool:
  - alias     : nama pendek (1-3 huruf) untuk Pass B
  - short_desc: deskripsi 1 baris yang model tulis sendiri
  - args_schema: parameter dengan contoh nilai konkret
  - sim_result : simulasi output (dry-run, model berimajinasi hasilnya)
  - pass_b_line: 1 baris siap pakai untuk diinjeksi ke prompt Pass B

Dipanggil dari:
  - settings_ui.py → _save_all() (on-save)
  - main.py        → startup check (on-boot, jika hash berubah)
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from typing import Dict, List, Optional

try:
    import msgpack
    _HAS_MSGPACK = True
except ImportError:
    _HAS_MSGPACK = False

# ── Path defaults (bisa di-override) ─────────────────────────────────────────
_DIR             = os.path.dirname(os.path.abspath(__file__))
TOOLS_JSON_PATH  = os.path.join(_DIR, "mcp_custom_tools.json")
COMPILED_BIN     = os.path.join(_DIR, "mcp_tools_compiled.bin")
COMPILED_JSON_FB = os.path.join(_DIR, "mcp_tools_compiled.json")  # fallback jika no msgpack

# Model endpoint — dipakai untuk compile (bisa yang kecil/cepat)
_COMPILE_MODEL_KEY = "react"   # pakai key yang sama dengan _llm_call di main.py


# ═════════════════════════════════════════════════════════════════════════════
# HASH UTILITIES
# ═════════════════════════════════════════════════════════════════════════════

def _tool_hash(tool: Dict) -> str:
    """Hash deterministik dari field yang relevan di satu tool."""
    key = json.dumps({
        "name":   tool.get("name", ""),
        "desc":   tool.get("description", ""),
        "url":    tool.get("url", ""),
        "method": tool.get("method", "GET"),
        "params": tool.get("params", []),
        "type":   tool.get("type", "http"),
    }, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(key.encode()).hexdigest()[:16]


def _tools_bundle_hash(tools: List[Dict]) -> str:
    """Hash seluruh bundle tools (untuk cek apakah perlu re-compile semua)."""
    combined = "".join(_tool_hash(t) for t in tools)
    return hashlib.sha256(combined.encode()).hexdigest()[:16]


# ═════════════════════════════════════════════════════════════════════════════
# READ / WRITE COMPILED CACHE
# ═════════════════════════════════════════════════════════════════════════════

def save_individual_tool_bins(tools_dict: Dict[str, Dict]) -> None:
    """Simpan setiap tool compiled metadata ke file bin individu di folder tools."""
    tools_dir = os.path.join(_DIR, "tools")
    os.makedirs(tools_dir, exist_ok=True)
    
    # Hapus file bin/json lama di folder tools yang tidak ada di tools_dict lagi
    if os.path.isdir(tools_dir):
        for f in os.listdir(tools_dir):
            if f.endswith(".bin") or f.endswith(".json"):
                base_name = f.rsplit(".", 1)[0]
                if base_name not in tools_dict:
                    try:
                        os.remove(os.path.join(tools_dir, f))
                    except Exception:
                        pass

    for name, meta in tools_dict.items():
        bin_path = os.path.join(tools_dir, f"{name}.bin")
        if _HAS_MSGPACK:
            try:
                with open(bin_path, "wb") as f:
                    msgpack.pack(meta, f, use_bin_type=True)
            except Exception as e:
                print(f"  [COMPILE ERROR] Gagal menulis individual bin {name}: {e}")
        else:
            try:
                with open(bin_path, "w", encoding="utf-8") as f:
                    json.dump(meta, f, ensure_ascii=False, indent=2)
            except Exception as e:
                print(f"  [COMPILE ERROR] Gagal menulis individual bin (json fallback) {name}: {e}")


def _write_compiled(data: Dict) -> None:
    """Tulis compiled cache ke .bin (msgpack) atau .json (fallback)."""
    # Tulis file bundel utama
    if _HAS_MSGPACK:
        with open(COMPILED_BIN, "wb") as f:
            msgpack.pack(data, f, use_bin_type=True)
    else:
        with open(COMPILED_JSON_FB, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            
    # Tulis file bin individu per tool di folder tools
    save_individual_tool_bins(data.get("tools", {}))


def _read_compiled() -> Optional[Dict]:
    """Baca compiled cache. Return None jika tidak ada / corrupt."""
    # Coba .bin dulu
    if _HAS_MSGPACK and os.path.exists(COMPILED_BIN):
        try:
            with open(COMPILED_BIN, "rb") as f:
                return msgpack.unpack(f, raw=False)
        except Exception:
            pass
    # Fallback .json
    if os.path.exists(COMPILED_JSON_FB):
        try:
            with open(COMPILED_JSON_FB, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return None


def load_tools_from_folder() -> Dict[str, Dict]:
    """
    Baca langsung semua file .bin (atau .json fallback) di folder tools/.

    Ini adalah sumber data utama saat runtime: selama sebuah tool sudah
    pernah berhasil di-compile (punya file di folder tools/), dia akan
    tetap terbaca di sini — bahkan jika mcp_custom_tools.json kosong,
    hilang, atau entry-nya sudah dihapus dari JSON. "Sudah di-compile"
    inilah yang sekarang jadi penentu aktif/tidaknya sebuah custom tool
    (menggantikan toggle enable/disable yang dulu ada di Settings UI).
    """
    tools_dir = os.path.join(_DIR, "tools")
    result: Dict[str, Dict] = {}
    if not os.path.isdir(tools_dir):
        return result

    for fname in sorted(os.listdir(tools_dir)):
        if not (fname.endswith(".bin") or fname.endswith(".json")):
            continue
        name = fname.rsplit(".", 1)[0]
        path = os.path.join(tools_dir, fname)
        meta = None

        if fname.endswith(".bin"):
            if not _HAS_MSGPACK:
                continue
            try:
                with open(path, "rb") as f:
                    meta = msgpack.unpack(f, raw=False)
            except Exception:
                meta = None
        else:  # .json fallback
            try:
                with open(path, encoding="utf-8") as f:
                    meta = json.load(f)
            except Exception:
                meta = None

        if isinstance(meta, dict):
            # .bin diprioritaskan dibanding .json kalau dua-duanya ada
            if name not in result or fname.endswith(".bin"):
                meta.setdefault("name", name)
                result[name] = meta

    return result


def load_compiled_tools() -> Dict[str, Dict]:
    """
    Return dict: tool_name → compiled_data.
    Dipakai oleh task_router.py untuk inject ke Pass B prompt.

    Sumber utama adalah file .bin individu di folder tools/ (lihat
    load_tools_from_folder). Bundle cache (mcp_tools_compiled.bin/json)
    dipakai hanya sebagai fallback kalau folder tools/ kosong/tidak ada —
    misalnya sebelum proses individual-bin pernah berjalan sekali.
    """
    from_folder = load_tools_from_folder()
    if from_folder:
        return from_folder

    cache = _read_compiled()
    if not cache:
        return {}
    return cache.get("tools", {})


# ═════════════════════════════════════════════════════════════════════════════
# LLM GENERATION PER TOOL
# ═════════════════════════════════════════════════════════════════════════════

_COMPILE_SYSTEM = """\
Kamu adalah system yang menganalisis definisi API tool dan menghasilkan metadata ringkas.
Output HANYA JSON satu objek, tidak ada teks lain, tidak ada markdown fence.

Format output WAJIB:
{
  "alias": "2-3 huruf unik, singkatan dari nama tool, huruf kecil",
  "short_desc": "deskripsi 1 baris max 80 karakter, bahasa Indonesia",
  "args_schema": [
    {"name": "param_name", "example": "contoh_nilai_konkret", "required": true/false}
  ],
  "sim_result": "contoh output realistis dari tool ini, max 120 karakter",
  "trigger_phrases": ["frasa user yang akan memicu tool ini", "contoh lain"],
  "pass_b_line": "- {alias}: {short_desc} | params: param1=contoh, param2=contoh"
}

Aturan alias:
- Ambil 2-3 huruf dari nama tool (misal get_weather → gw, get_random_joke → grj)
- Harus unik dan tidak bentrok dengan alias built-in:
  gum,gus,guls,gur,gmu,gugh,guch,uum,bu,sn,rs (user)
  grc,gts,sc,gcs (chat)
  gsi,gvc,gsm,gca (state)
  gre,gpg,gns,gms,gri,gpm (event)
  gcg,ggs,gac (game)
  gfc,gtg,gnf,gru,gug (social)
  gmm,gcm,grr,gmos (self)
  gtc,gpe,gan,cmd (meta)
"""

def _generate_tool_metadata(tool: Dict, llm_call) -> Optional[Dict]:
    """
    Panggil LLM untuk generate metadata satu tool.
    Return dict hasil generate, atau None jika gagal.
    """
    params_desc = []
    for p in tool.get("params", []):
        req = "wajib" if p.get("required") else "opsional"
        params_desc.append(f"  - {p['name']} ({p.get('type','string')}, {req}): {p.get('description','')}")

    user_prompt = f"""Analisis tool berikut dan generate metadata:

Nama    : {tool['name']}
Deskripsi: {tool.get('description', '(tidak ada)')}
URL     : {tool.get('url', '')}
Method  : {tool.get('method', 'GET')}
Params  :
{chr(10).join(params_desc) if params_desc else '  (tidak ada)'}
Kategori: {tool.get('category', 'custom')}

Generate metadata JSON sesuai format yang diminta."""

    try:
        resp = llm_call(
            _COMPILE_MODEL_KEY,
            messages=[
                {"role": "system", "content": _COMPILE_SYSTEM},
                {"role": "user",   "content": user_prompt},
            ],
            temperature=0.1,
            max_tokens=400,
        )
        raw = (resp.choices[0].message.content or "").strip()

        # Strip markdown fence
        import re
        raw = re.sub(r"```(?:json)?", "", raw, flags=re.IGNORECASE)
        raw = re.sub(r"```", "", raw).strip()

        data = json.loads(raw)
        if not isinstance(data, dict) or "alias" not in data:
            return None

        # Pastikan pass_b_line ada dan benar
        alias = data.get("alias", tool["name"][:3])
        if "pass_b_line" not in data or not data["pass_b_line"]:
            short = data.get("short_desc", tool.get("description", "")[:60])
            args_ex = ", ".join(
                f"{a['name']}={a.get('example','...')}"
                for a in data.get("args_schema", [])
                if a.get("required")
            )
            data["pass_b_line"] = f"- {alias}: {short} | params: {args_ex or 'no params'}"

        return data

    except Exception as e:
        print(f"  [COMPILE ERROR] {tool['name']}: {e}")
        return None


def _fallback_metadata(tool: Dict) -> Dict:
    """
    Generate metadata tanpa LLM (fallback jika LLM tidak tersedia).
    Lebih sederhana tapi tetap fungsional.
    """
    name  = tool["name"]
    # Buat alias dari inisial kata-kata
    words = name.replace("_", " ").split()
    alias = "".join(w[0] for w in words)[:4].lower()
    # Jika single word, ambil 3 huruf pertama
    if len(words) == 1:
        alias = name[:3].lower()

    params = tool.get("params", [])
    args_schema = [
        {
            "name": p["name"],
            "example": p.get("description", p["name"])[:20],
            "required": p.get("required", False),
        }
        for p in params
    ]
    args_ex = ", ".join(
        f"{a['name']}={a['example']}"
        for a in args_schema if a["required"]
    ) or "no params"

    short_desc = (tool.get("description", "") or name)[:80]
    pass_b_line = f"- {alias}: {short_desc} | params: {args_ex}"

    # Trigger phrases sederhana dari nama & deskripsi
    triggers = [name.replace("_", " ")]
    if "weather" in name or "cuaca" in (tool.get("description","")).lower():
        triggers += ["cuaca", "suhu", "iklim", "weather"]
    if "joke" in name or "lelucon" in (tool.get("description","")).lower():
        triggers += ["lelucon", "joke", "lucu"]

    return {
        "alias":          alias,
        "short_desc":     short_desc,
        "args_schema":    args_schema,
        "sim_result":     f"(simulasi: hasil dari {name})",
        "trigger_phrases": triggers,
        "pass_b_line":    pass_b_line,
        "fallback":       True,
    }


# ═════════════════════════════════════════════════════════════════════════════
# MAIN COMPILE FUNCTION
# ═════════════════════════════════════════════════════════════════════════════

def compile_tools(
    llm_call=None,
    force: bool = False,
    verbose: bool = True,
) -> Dict[str, Dict]:
    """
    Entry point utama. Compile semua tool yang punya "name" di JSON ke cache
    (tidak ada lagi filter enabled/disable — lihat catatan di atas).

    Args:
        llm_call : fungsi _llm_call dari main.py. None → pakai fallback.
        force    : paksa re-compile semua meski hash sama.
        verbose  : print progress.

    Returns:
        dict tool_name → compiled metadata (semua tool yang berhasil di-compile).
    """
    def _log(msg: str):
        if verbose:
            print(msg)

    # Load tools JSON
    # Catatan: tidak ada lagi gate enable/disable di sini — semua tool yang
    # punya "name" otomatis ikut di-compile. Aktif/tidaknya sebuah tool di
    # runtime sekarang murni ditentukan oleh apakah dia sudah berhasil
    # di-compile (punya file .bin di folder tools/) atau belum.
    if not os.path.exists(TOOLS_JSON_PATH):
        _log("[COMPILE] mcp_custom_tools.json tidak ditemukan — pakai bin yang sudah ada di folder tools/.")
        return load_tools_from_folder()

    try:
        with open(TOOLS_JSON_PATH, encoding="utf-8") as f:
            raw = json.load(f)
        all_tools: List[Dict] = raw.get("tools", []) if isinstance(raw, dict) else raw
        enabled = [t for t in all_tools if isinstance(t, dict) and t.get("name")]
    except Exception as e:
        _log(f"[COMPILE] Error baca tools JSON: {e}")
        # Jangan hapus bin yang sudah ada hanya karena JSON-nya bermasalah.
        return load_tools_from_folder()

    if not enabled:
        _log("[COMPILE] mcp_custom_tools.json kosong — pakai bin yang sudah ada di folder tools/.")
        # JSON kosong bukan berarti tool yang sudah di-compile harus hilang;
        # bin di folder tools/ tetap jadi sumber data yang dipakai runtime.
        return load_tools_from_folder()

    # Load cache lama
    old_cache = _read_compiled() or {}
    old_tools: Dict[str, Dict] = old_cache.get("tools", {})
    old_bundle_hash = old_cache.get("bundle_hash", "")
    new_bundle_hash = _tools_bundle_hash(enabled)

    if not force and old_bundle_hash == new_bundle_hash:
        _log(f"[COMPILE] Bundle hash sama ({new_bundle_hash}) — tidak ada perubahan, pakai cache.")
        return old_tools

    _log(f"\n{'═'*58}")
    _log(f"  TOOL COMPILER — {len(enabled)} tool(s) akan di-proses")
    _log(f"  old_hash={old_bundle_hash or 'none'}  new_hash={new_bundle_hash}")
    _log(f"{'═'*58}")

    # Ensure alias uniqueness across tools in this session
    used_aliases: set = set()
    new_tools: Dict[str, Dict] = {}

    for tool in enabled:
        name      = tool["name"]
        tool_hash = _tool_hash(tool)

        # Cek apakah tool ini berubah
        old_entry = old_tools.get(name, {})
        if not force and old_entry.get("hash") == tool_hash:
            _log(f"  ✓  {name} — tidak berubah, pakai cache")
            meta = old_entry
        else:
            _log(f"  ⟳  {name} — compile {'(LLM)' if llm_call else '(fallback)'} ...")

            if llm_call:
                meta = _generate_tool_metadata(tool, llm_call)
                if meta is None:
                    _log(f"     LLM gagal, pakai fallback")
                    meta = _fallback_metadata(tool)
            else:
                meta = _fallback_metadata(tool)

            # Resolve alias conflict
            alias = meta.get("alias", name[:3].lower())
            original_alias = alias
            suffix = 2
            while alias in used_aliases:
                alias = f"{original_alias}{suffix}"
                suffix += 1
            meta["alias"] = alias
            _log(f"     alias={alias!r}  sim={meta.get('sim_result','')[:60]}")

        used_aliases.add(meta.get("alias", ""))
        meta["hash"]     = tool_hash
        meta["name"]     = name
        meta["category"] = tool.get("category", "custom")
        meta["enabled"]  = True
        new_tools[name]  = meta

    # Simpan ke cache
    payload = {
        "bundle_hash": new_bundle_hash,
        "compiled_at": time.time(),
        "tools":       new_tools,
    }
    _write_compiled(payload)
    _log(f"\n  ✅ Compiled {len(new_tools)} tool(s) → {COMPILED_BIN if _HAS_MSGPACK else COMPILED_JSON_FB}")
    _log(f"{'═'*58}\n")

    return new_tools


# ═════════════════════════════════════════════════════════════════════════════
# BOOT CHECK (dipanggil dari main.py saat startup)
# ═════════════════════════════════════════════════════════════════════════════

def boot_check(llm_call=None, verbose: bool = True) -> Dict[str, Dict]:
    """
    Dipanggil sekali saat app start. Re-compile jika ada perubahan.
    Return compiled tools dict.
    """
    return compile_tools(llm_call=llm_call, force=False, verbose=verbose)


# ═════════════════════════════════════════════════════════════════════════════
# PASS B PROMPT BUILDER (dipakai oleh task_router.py)
# ═════════════════════════════════════════════════════════════════════════════

def build_pass_b_addon(cat: str) -> str:
    """
    Kembalikan baris-baris tool compiled untuk kategori tertentu,
    siap diappend ke prompt Pass B.

    Format per baris: "- {alias}: {short_desc} | params: ..."
    """
    compiled = load_compiled_tools()
    if not compiled:
        return ""

    lines = []
    for name, meta in compiled.items():
        if not meta.get("enabled", True):
            continue
        tool_cat = meta.get("category", "custom")
        if tool_cat != cat:
            continue
        line = meta.get("pass_b_line", "")
        if line:
            lines.append(line)

    return "\n".join(lines)


def get_alias_map() -> Dict[str, str]:
    """
    Return dict: alias → tool_name.
    Dipakai di Pass B parser untuk resolve alias pendek ke nama asli.
    """
    compiled = load_compiled_tools()
    result: Dict[str, str] = {}
    for name, meta in compiled.items():
        alias = meta.get("alias", "")
        if alias:
            result[alias] = name
    return result


# ═════════════════════════════════════════════════════════════════════════════
# CLI — test langsung tanpa LLM
# ═════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import sys
    force = "--force" in sys.argv

    print("Running tool_compiler (fallback mode — no LLM)\n")
    result = compile_tools(llm_call=None, force=force, verbose=True)

    if result:
        print("\nCompiled tools summary:")
        for name, meta in result.items():
            print(f"\n  [{name}]")
            print(f"    alias    : {meta.get('alias')}")
            print(f"    short    : {meta.get('short_desc','')[:70]}")
            print(f"    sim      : {meta.get('sim_result','')[:70]}")
            print(f"    triggers : {meta.get('trigger_phrases', [])}")
            print(f"    pass_b   : {meta.get('pass_b_line','')}")

        print("\nalias map:", get_alias_map())
        print("\nPass B addon (meta):", repr(build_pass_b_addon("meta")))
        print("Pass B addon (self):", repr(build_pass_b_addon("self")))
        print("Pass B addon (chat):", repr(build_pass_b_addon("chat")))
