#!/usr/bin/env python3
"""
task_router.py — 3-Pass Task Router Pipeline

Pipeline:
  Pass A : Broad Category  → comma-separated, multi-select, model-kecil-friendly
  Pass B : Tool Specific   → satu LLM call per kategori, prompt mini terfokus
  Pass C : Validation      → yes/no per tool, retry max 2x jika reject

Custom MCP tools diregistrasikan ke kategori via field "category" di JSON
(default "custom" jika tidak di-set). Kategori yang sama dengan built-in akan
otomatis dimasukkan ke Pass B prompt kategori tersebut.

Usage di main.py:
    from task_router import run_task_router

    calls = run_task_router(
        user_id      = user_mem.user_id,
        username     = uname,
        user_input   = user_input,
        char_prompts = CHARACTER.get("prompts", {}),
        llm_call     = _llm_call,
        dbg          = _dbg,
    )
    # calls → List[Dict[str,Any]]  e.g. [{"f":"gum","a":{"id":"..."}}, ...]
"""

from __future__ import annotations

import json
import re
from typing import Dict, List, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    pass


# ═════════════════════════════════════════════════════════════════════════════
# KATEGORI & TOOL MAP
# ═════════════════════════════════════════════════════════════════════════════

VALID_CATEGORIES = {
    "user", "chat", "state", "event", "game", "social", "self", "meta", "custom", "cloud"
}

# Tool yang valid per kategori (untuk guard halusinasi Pass B output).
# Diisi ulang setiap run_task_router() dipanggil dengan custom tools terbaru.
_CATEGORY_TOOLS: Dict[str, List[str]] = {
    "user":   ["gum", "gus", "guls", "gur", "gmu", "gugh", "guch", "uum", "bu", "sn", "rs"],
    "chat":   ["grc", "gts", "sc", "gcs"],
    "state":  ["gsi", "gsd", "gvc", "gav", "gsm", "gca"],
    "event":  ["gre", "gpg", "gns", "gms", "gri", "gpm"],
    "game":   ["gcg", "ggs", "gac"],
    "social": ["gfc", "gtg", "gnf", "gru", "gug"],
    "self":   ["gmm", "gme", "gcm", "grr", "gmos"],
    "meta":   ["gtc", "gpe", "gan", "cmd"],
    "custom": [],   # diisi dinamis
    "cloud":  ["cs_write", "cs_read", "cs_list", "cs_search", "cs_delete"],
}

# Fungsi yang butuh injeksi user_id dari Python (jangan percaya output model)
_USER_ID_INJECT_FUNCS = {
    "gum", "gus", "guls", "gur", "gugh", "guch", "uum", "bu", "rs"
}


# ═════════════════════════════════════════════════════════════════════════════
# PROMPT DEFAULTS — dipakai kalau character.json tidak punya key tersebut
# ═════════════════════════════════════════════════════════════════════════════

_PASS_A_DEFAULT = """\
[TASK ROUTER — PASS A]
Tentukan KATEGORI. Jawab HANYA nama kategori, pisah koma jika lebih dari satu.

Kategori:
- user   : data USER yang chat (romance, info, nickname, memory tentang dia)
- chat   : riwayat/topik percakapan
- state  : kondisi stream (viewer, mood)
- event  : event stream (raid, poll, milestone)
- game   : game/aktivitas berjalan
- social : follower, top gifter
- self   : kondisi KARAKTER AI (mood/mode karakter, bukan user)
- meta   : waktu, jadwal, command gaya bicara
- cloud  : simpan/baca/hapus/cari data di cloud/bin/storage
{custom_line}
"memory/data aku" → user | "kamu mood apa" → self | "simpan ke cloud" → cloud
Salam/greeting murni ("halo","hai","hi","halo-halo","pagi") → none
Jika tidak ada intent spesifik: none
"""

_PASS_A_CUSTOM_LINE = "- custom : tool kustom yang didefinisikan admin"

# Prompt Pass B per kategori (dipakai jika character.json tidak punya
# key "tool_select_<cat>_system")
_PASS_B_PROMPTS: Dict[str, str] = {
    "user": (
        "[TOOL SELECT: user]\n"
        "Pilih fungsi untuk data pengguna yang chat.\n\n"
        "Fungsi tersedia:\n"
        "- gum  : get_user_memory    — info lengkap user (romance, info, notes, memory tentang dia)\n"
        "- gus  : get_user_stats     — romance points/level, gift, last seen\n"
        "- guls : get_user_last_seen — kapan terakhir user chat\n"
        "- gur  : get_user_relationship — status & level romance\n"
        "- gmu  : get_multiple_users — beberapa user (a:{ids:[...]})\n"
        "- gugh : get_user_gift_history — riwayat hadiah\n"
        "- guch : get_user_chat_history — ringkasan chat\n"
        "- uum  : update_user_memory — simpan info/note/nickname (a:{m:\"teks\"})\n"
        "- bu   : banned_user        — HANYA admin\n"
        "- sn   : set_nickname       — ganti nickname (a:{target:\"self\",name:\"Nama\"})\n"
        "- rs   : romance_status     — cek status romance\n\n"
        "Output HANYA JSON: {\"f\":\"nama\",\"a\":{}}\n"
        "Jika tidak cocok: null"
    ),
    "chat": (
        "[TOOL SELECT: chat]\n"
        "Pilih satu fungsi yang paling sesuai untuk permintaan soal riwayat chat.\n\n"
        "Fungsi tersedia:\n"
        "- grc : get_recent_chat   — N chat terbaru (a:{n:5})\n"
        "- gts : get_topic_history — topik obrolan saat ini\n"
        "- sc  : search_chat       — cari kata kunci (a:{q:\"kata\"})\n"
        "- gcs : get_chat_since    — ringkasan chat sejak waktu tertentu\n\n"
        "Output HANYA JSON satu baris: {\"f\":\"nama\",\"a\":{}}\n"
        "Jika tidak ada yang cocok: null"
    ),
    "state": (
        "[TOOL SELECT: state]\n"
        "Pilih satu fungsi yang paling sesuai untuk permintaan soal kondisi stream.\n\n"
        "Fungsi tersedia:\n"
        "- gsi : get_stream_info  — topik/role/style stream saat ini\n"
        "- gvc : get_viewer_count — jumlah penonton\n"
        "- gsm : get_stream_mood  — mood/style stream\n"
        "- gca : get_chat_activity\n\n"
        "Output HANYA JSON satu baris: {\"f\":\"nama\",\"a\":{}}\n"
        "Jika tidak ada yang cocok: null"
    ),
    "event": (
        "[TOOL SELECT: event]\n"
        "Pilih satu fungsi yang paling sesuai untuk permintaan soal event stream.\n\n"
        "Fungsi tersedia:\n"
        "- gre : get_recent_events\n"
        "- gpg : get_pinned_goal\n"
        "- gns : get_notification_status\n"
        "- gms : get_milestone_status\n"
        "- gri : get_raid_info\n"
        "- gpm : get_poll_mode\n\n"
        "Output HANYA JSON satu baris: {\"f\":\"nama\",\"a\":{}}\n"
        "Jika tidak ada yang cocok: null"
    ),
    "game": (
        "[TOOL SELECT: game]\n"
        "Pilih satu fungsi yang paling sesuai untuk permintaan soal game/aktivitas.\n\n"
        "Fungsi tersedia:\n"
        "- gcg : get_current_game\n"
        "- ggs : get_game_status\n"
        "- gac : get_activity_context\n\n"
        "Output HANYA JSON satu baris: {\"f\":\"nama\",\"a\":{}}\n"
        "Jika tidak ada yang cocok: null"
    ),
    "social": (
        "[TOOL SELECT: social]\n"
        "Pilih satu fungsi yang paling sesuai untuk permintaan soal interaksi sosial.\n\n"
        "Fungsi tersedia:\n"
        "- gfc : get_follower_count\n"
        "- gtg : get_top_gifters\n"
        "- gnf : get_new_followers\n"
        "- gru : get_regular_users\n"
        "- gug : get_user_groups\n\n"
        "Output HANYA JSON satu baris: {\"f\":\"nama\",\"a\":{}}\n"
        "Jika tidak ada yang cocok: null"
    ),
    "self": (
        "[TOOL SELECT: self]\n"
        "Pilih fungsi untuk kondisi KARAKTER AI sendiri (bukan data user).\n\n"
        "Fungsi tersedia:\n"
        "- gmm  : get_character_mood    — mood karakter AI\n"
        "- gcm  : get_character_mode    — mode/role karakter AI\n"
        "- grr  : get_recent_responses  — respons terakhir karakter\n"
        "- gmos : get_session_memory    — topik/role/style/command sesi\n\n"
        "Output HANYA JSON: {\"f\":\"nama\",\"a\":{}}\n"
        "Jika tidak cocok: null"
    ),
    "meta": (
        "[TOOL SELECT: meta]\n"
        "Pilih satu fungsi yang paling sesuai untuk waktu, jadwal, atau command.\n\n"
        "Fungsi tersedia:\n"
        "- gtc : get_time_context  — hari/tanggal/jam saat ini\n"
        "- gpe : get_planned_events\n"
        "- gan : get_announcements\n"
        "- cmd : set_command       — set command gaya bicara (a:{c:\"instruksi\"})\n\n"
        "Output HANYA JSON satu baris: {\"f\":\"nama\",\"a\":{}}\n"
        "Jika tidak ada yang cocok: null"
    ),
    "custom": "",  # dibangun dinamis di _build_pass_b_prompt()
    "cloud": (
        "[TOOL SELECT: cloud]\n"
        "Pilih fungsi cloud storage yang sesuai.\n\n"
        "Fungsi:\n"
        "- cs_write  : simpan/update data (a:{ns,key,value,label,ttl})\n"
        "- cs_read   : baca data (a:{ns,key})\n"
        "- cs_list   : tampilkan semua data (a:{ns})\n"
        "- cs_search : cari data (a:{ns,q})\n"
        "- cs_delete : hapus data (a:{ns,key})\n\n"
        "ns: 'global' (semua user) atau 'user:{id}' (private). Default: user\n"
        "Output HANYA JSON: {\"f\":\"nama\",\"a\":{}}\n"
        "Jika tidak cocok: null"
    ),
}

# Prompt Pass C (validation)
# PRINSIP: bias ke "yes" — hanya tolak jika JELAS salah kategori.
# Model kecil cenderung over-reject, jadi kita minta dia default ke yes
# dan hanya no jika benar-benar tidak nyambung sama sekali.
_PASS_C_SYSTEM = """\
[VALIDASI TOOL]
Jawab HANYA: yes atau no
yes = fungsi masuk akal untuk pertanyaan (meski tidak sempurna)
no  = fungsi SAMA SEKALI tidak nyambung

Contoh no: "memory/data aku" + gmos/gmm → no (itu untuk kondisi karakter AI)
Jawab yes kecuali JELAS salah.
"""


# ═════════════════════════════════════════════════════════════════════════════
# PASS A — BROAD CATEGORY PARSER
# ═════════════════════════════════════════════════════════════════════════════

def _build_pass_a_prompt(char_prompts: Dict, custom_tools: List[Dict]) -> str:
    """Ambil prompt Pass A dari character.json atau default."""
    if "task_router_a_system" in char_prompts:
        return char_prompts["task_router_a_system"]

    # Kelompokkan custom tools berdasarkan kategori
    from collections import defaultdict
    custom_by_cat = defaultdict(list)
    for t in custom_tools:
        cat = str(t.get("category", "custom")).lower().strip()
        custom_by_cat[cat].append(t)

    # Modifikasi deskripsi kategori default dengan custom tools
    cat_lines = []
    default_cats = [
        ("user",   "data USER yang chat (romance, info, nickname, memory tentang dia)"),
        ("chat",   "riwayat/topik percakapan"),
        ("state",  "kondisi stream (viewer, mood)"),
        ("event",  "event stream (raid, poll, milestone)"),
        ("game",   "game/aktivitas berjalan"),
        ("social", "follower, top gifter"),
        ("self",   "kondisi KARAKTER AI (mood/mode karakter, bukan data user)"),
        ("meta",   "waktu, jadwal, command gaya bicara"),
        ("cloud",  "simpan/baca/hapus/cari data di cloud/bin/storage"),
        ("custom", "tool kustom admin"),
    ]

    for cat_name, cat_desc in default_cats:
        if cat_name == "custom" and not custom_tools:
            continue
        line = f"- {cat_name:<8} : {cat_desc}"
        cat_lines.append(line)
        # Jika ada custom tools untuk kategori ini, cantumkan di bawahnya
        tools_in_cat = custom_by_cat.get(cat_name, [])
        for t in tools_in_cat:
            tname = t["name"]
            tdesc = t.get("description", "(no description)")
            cat_lines.append(f"    * {tname}: {tdesc}")

    prompt_lines = [
        "[TASK ROUTER — PASS A]",
        "Tentukan KATEGORI. Jawab HANYA nama kategori, pisah koma jika lebih dari satu.",
        "",
        "Kategori:"
    ]
    prompt_lines.extend(cat_lines)
    prompt_lines.extend([
        "",
        "\"memory/data aku\" → user | \"kamu mood apa\" → self | \"simpan ke cloud\" → cloud",
        "Salam/greeting murni (\"halo\",\"hai\",\"hi\",\"pagi\") → none",
        "Jika tidak ada intent spesifik: none",
    ])

    return "\n".join(prompt_lines).strip()


def _parse_pass_a(raw: str, has_custom: bool) -> List[str]:
    """
    Parse output Pass A: comma/whitespace-separated nama kategori.
    Return list unik kategori valid. [] jika none/invalid.
    """
    if not raw:
        return []
    # Bersihkan semua kecuali huruf kecil, koma, spasi
    cleaned = re.sub(r"[^a-zA-Z,\s]", "", raw).lower().strip()
    tokens = re.split(r"[,\s]+", cleaned)
    tokens = [t.strip() for t in tokens if t.strip()]

    if not tokens or tokens == ["none"]:
        return []

    allowed = set(VALID_CATEGORIES)
    if not has_custom:
        allowed.discard("custom")

    result: List[str] = []
    seen: set = set()
    for t in tokens:
        if t in allowed and t not in seen and t != "none":
            result.append(t)
            seen.add(t)
    return result


# ═════════════════════════════════════════════════════════════════════════════
# PASS B — TOOL SPECIFIC BUILDER & PARSER
# ═════════════════════════════════════════════════════════════════════════════

def _build_pass_b_prompt(
    cat: str,
    char_prompts: Dict,
    custom_tools: List[Dict],
    extra_tools: List[str],       # custom tools yg category-nya = cat ini
    compiled_meta: Dict = None,   # hasil load_compiled_tools()
) -> str:
    """
    Ambil prompt Pass B untuk kategori tertentu.
    Priority: character.json key → _PASS_B_PROMPTS default.
    Custom tools diinjeksi menggunakan compiled pass_b_line jika tersedia,
    fallback ke deskripsi mentah dari JSON.
    """
    compiled_meta = compiled_meta or {}

    def _compiled_line(tname: str) -> str:
        """Ambil pass_b_line dari compiled metadata, atau buat sederhana."""
        meta = compiled_meta.get(tname)
        if meta and meta.get("pass_b_line"):
            return meta["pass_b_line"]
        # Fallback: cari dari custom_tools list
        raw = next((t for t in custom_tools if t.get("name") == tname), None)
        if raw:
            params = raw.get("params", [])
            param_str = ", ".join(
                f"{p['name']}={'req' if p.get('required') else 'opt'}"
                for p in params
            ) or "no params"
            return f"- {tname}: {raw.get('description','(no desc)')} | params: {param_str}"
        return f"- {tname}: tool kustom"

    char_key = f"tool_select_{cat}_system"
    if char_key in char_prompts:
        base = char_prompts[char_key]
        if extra_tools and cat != "custom":
            extras = "\n".join(_compiled_line(n) for n in extra_tools)
            base = base.rstrip() + f"\n\nTool tambahan (kustom):\n{extras}"
        return base

    if cat == "custom":
        if not custom_tools:
            return ""
        lines = [
            "[TOOL SELECT: custom]",
            "Pilih satu fungsi kustom yang paling sesuai.\n",
            "Fungsi tersedia:",
        ]
        for t in custom_tools:
            lines.append(_compiled_line(t["name"]))
        lines += [
            "\nOutput HANYA JSON satu baris: {\"f\":\"nama\",\"a\":{}}",
            "Jika tidak ada yang cocok: null",
        ]
        return "\n".join(lines)

    base = _PASS_B_PROMPTS.get(cat, "")
    if extra_tools and base:
        extras = "\n".join(_compiled_line(n) for n in extra_tools)
        base = base.rstrip() + f"\n\nTool tambahan (kustom):\n{extras}"
    return base


def _valid_funcs_for_cat(cat: str, custom_tools_for_cat: List[str]) -> set:
    """Set nama fungsi valid untuk satu kategori (built-in + custom)."""
    base = set(_CATEGORY_TOOLS.get(cat, []))
    base.update(custom_tools_for_cat)
    return base


def _parse_json_tolerant(raw: str) -> Optional[Dict]:
    """Parse JSON object, toleran terhadap markdown fence dan teks sekitar."""
    if not raw or raw.strip().lower() == "null":
        return None
    stripped = re.sub(r"```(?:json)?", "", raw, flags=re.IGNORECASE)
    stripped = re.sub(r"```", "", stripped).strip()

    # Coba langsung
    try:
        data = json.loads(stripped)
        if isinstance(data, dict):
            return data
    except Exception:
        pass

    # Cari object JSON dalam teks
    start = stripped.find("{")
    if start == -1:
        return None
    depth = 0
    in_str = False
    escape = False
    for i, ch in enumerate(stripped[start:], start):
        if escape:
            escape = False
            continue
        if ch == "\\" and in_str:
            escape = True
            continue
        if ch == '"':
            in_str = not in_str
            continue
        if in_str:
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                try:
                    data = json.loads(stripped[start:i + 1])
                    if isinstance(data, dict):
                        return data
                except Exception:
                    pass
                break
    return None


def _parse_pass_b(
    raw: str,
    cat: str,
    user_id: str,
    valid_funcs: set,
    alias_map: Dict = None,
) -> Optional[Dict]:
    """
    Parse output Pass B → {"f": fname, "a": args} atau None.
    Resolve alias pendek (gw → get_weather) dari compiled metadata.
    Injeksi 'id' dari Python untuk fungsi yang butuhnya.
    """
    data = _parse_json_tolerant(raw)
    if not data or "f" not in data:
        return None

    fname = str(data["f"]).strip()
    args  = dict(data.get("a", {}) or {})

    # Resolve alias pendek ke nama asli jika perlu
    alias_map = alias_map or {}
    if fname in alias_map:
        resolved = alias_map[fname]
        fname = resolved

    # Guard: fname harus valid untuk kategori ini
    if valid_funcs and fname not in valid_funcs:
        # Coba juga valid_funcs dengan semua alias resolved
        if fname not in (alias_map.get(v, v) for v in valid_funcs):
            return None

    # Injeksi user_id dari Python — JANGAN percaya output model
    if fname in _USER_ID_INJECT_FUNCS:
        args["id"] = user_id

    return {"f": fname, "a": args}


# ═════════════════════════════════════════════════════════════════════════════
# PASS C — VALIDATION PARSER
# ═════════════════════════════════════════════════════════════════════════════

def _parse_pass_c(raw: str) -> bool:
    """
    Return True (accept) jika model jawab 'yes'.
    Default ke True jika output tidak jelas — bias ke accept untuk hindari
    over-rejection oleh model kecil yang sering ragu-ragu.
    """
    if not raw:
        return True   # error/timeout → accept by default
    cleaned = raw.strip().lower()
    tokens  = re.split(r"[\s,.\n]+", cleaned)
    first   = next((t for t in tokens if t), "")
    # Hanya tolak jika JELAS bilang no/tidak/nope/false
    if first in ("no", "tidak", "nope", "false", "n"):
        return False
    # Semua lainnya (yes, ya, benar, unclear, dll) → accept
    return True


# ═════════════════════════════════════════════════════════════════════════════
# MAIN ENTRY POINT
# ═════════════════════════════════════════════════════════════════════════════

def _run_pass_bc_for_category(
    cat:            str,
    user_id:        str,
    username:       str,
    user_input:     str,
    char_prompts:   Dict,
    llm_call,
    custom_for_cat: List[Dict],
    extra_for_cat:  List[str],
    compiled_meta:  Dict,
    alias_map:      Dict,
    already_funcs:  set,
    max_retry:      int,
    dbg=None,
) -> Optional[Dict]:
    """
    Jalankan Pass B (tool select) + Pass C (validate) untuk satu kategori.
    Return call dict {"f":..,"a":..} jika confirmed, None jika tidak ada yang valid.
    Di-extract dari run_task_router() agar reusable oleh decision graph path.
    """
    def _log(msg: str):
        if dbg: dbg.line(msg)

    prompt_b = _build_pass_b_prompt(cat, char_prompts, custom_for_cat, extra_for_cat, compiled_meta)
    if not prompt_b:
        _log(f"  [PASS B] no prompt for cat={cat}, skip")
        return None

    # ── CLOUD short-circuit: skip LLM, pakai NL parser langsung ──────────────
    if cat == "cloud":
        try:
            from cloud_tools import parse_cloud_command as _parse_cloud
            cloud_call = _parse_cloud(user_input, user_id, llm_call=llm_call)
            if cloud_call:
                f = cloud_call["f"]
                a = cloud_call.get("a", {})
                _log(f"  [CLOUD NL] → f={f} a={a}")
                if f not in already_funcs:
                    return {"f": f, "a": a}
            else:
                _log(f"  [CLOUD NL] tidak bisa parse → skip")
        except Exception as e:
            _log(f"  [CLOUD NL ERROR] {e}")
        return None

    valid_funcs = _valid_funcs_for_cat(cat, extra_for_cat)

    call: Optional[Dict]     = None
    rejected_funcs: List[str] = []
    attempt = 0

    while attempt <= max_retry:
        label = f"attempt {attempt+1}/{max_retry+1}"
        _log(f"\n  ┌── PASS B [{label}]: pilih tool untuk kategori '{cat}'")
        if rejected_funcs:
            _log(f"  │   rejected sebelumnya: {rejected_funcs}")
        _log(f"  │   valid_funcs: {sorted(valid_funcs)}")

        exclusion = ""
        if rejected_funcs:
            exclusion = (
                f"\nJANGAN pilih fungsi berikut (sudah ditolak validator): "
                + ", ".join(rejected_funcs)
            )

        input_b = f"username: {username}\nchat: {user_input}{exclusion}"

        try:
            resp_b = llm_call(
                "tool_select",
                messages=[
                    {"role": "system", "content": prompt_b},
                    {"role": "user",   "content": input_b},
                ],
                temperature=0.0,
                max_tokens=80,
            )
            raw_b = (resp_b.choices[0].message.content or "").strip()
            _log(f"  [PASS B RAW {label}] {raw_b!r}")
        except Exception as e:
            _log(f"  [PASS B ERROR {label}] {e}")
            break

        parsed = _parse_pass_b(raw_b, cat, user_id, valid_funcs, alias_map)
        _log(f"  │   parsed → {parsed}")

        if parsed is None:
            _log(f"  └── PASS B: null/invalid output — stop retry untuk '{cat}'")
            break

        fname = parsed["f"]

        if fname in already_funcs:
            _log(f"  [PASS B] {fname} sudah ada di hasil, skip")
            break

        _log(f"  │   akan validate: f={fname} a={parsed.get('a',{})}")
        _log(f"  ├── PASS C: VALIDATE f={fname}")

        if "task_router_c_system" in char_prompts:
            prompt_c_sys = char_prompts["task_router_c_system"]
        else:
            prompt_c_sys = _PASS_C_SYSTEM

        input_c = (
            f"Pertanyaan user: {user_input}\n"
            f"Fungsi dipilih: {fname}\n"
            f"Argumen: {json.dumps(parsed.get('a', {}), ensure_ascii=False)}\n"
            f"Kategori: {cat}"
        )

        try:
            resp_c = llm_call(
                "react",
                messages=[
                    {"role": "system", "content": prompt_c_sys},
                    {"role": "user",   "content": input_c},
                ],
                temperature=0.0,
                max_tokens=8,
            )
            raw_c = (resp_c.choices[0].message.content or "").strip()
            _log(f"  │   [PASS C input] user={user_input[:50]!r} f={fname}")
            _log(f"  │   [PASS C raw]   {raw_c!r}")
        except Exception as e:
            _log(f"  [PASS C ERROR] {e} → accept by default")
            raw_c = "yes"

        accepted = _parse_pass_c(raw_c)
        if accepted:
            _log(f"  └── PASS C: ✅ ACCEPTED  f={fname}")
            call = parsed
            break
        else:
            _log(f"  └── PASS C: ❌ REJECTED  f={fname} → retry Pass B")
            rejected_funcs.append(fname)
            attempt += 1

    return call


# ═════════════════════════════════════════════════════════════════════════════
# MAIN ENTRY POINT
# ═════════════════════════════════════════════════════════════════════════════

# Tracker complexity dari panggilan run_task_router() terakhir.
# Dibaca oleh main.py setelah memanggil run_task_router() untuk menentukan
# Soul Lite vs Soul Deep. Module-level karena tidak ingin ubah return
# signature run_task_router() (tetap List[Dict] demi backward compat).
LAST_GATE_INFO: Dict = {"gate0": None, "gate1_type": "task", "complexity": "lite"}


def run_task_router(
    user_id:      str,
    username:     str,
    user_input:   str,
    char_prompts: Dict,
    llm_call,               # _llm_call(pass_name, messages=..., temperature=..., max_tokens=...)
    dbg=None,               # DebugLogger optional
    max_retry:    int = 2,  # max retry Pass B per kategori jika Pass C reject
) -> List[Dict]:
    """
    3-Pass Task Router Pipeline.
    Menggantikan _task_router_pass() di main.py.

    Returns:
        List[Dict] berisi {"f": fname, "a": args} yang sudah divalidasi,
        siap dieksekusi oleh RouterExecutor.
    """
    from mcp_tools import load_custom_tools  # import di sini untuk hindari circular
    from tool_compiler import load_compiled_tools, build_pass_b_addon, get_alias_map

    def _log(msg: str):
        if dbg:
            dbg.line(msg)

    # Load compiled metadata duluan — "sudah di-compile" (punya .bin di
    # folder tools/) sekarang yang menentukan aktif/tidaknya custom tool,
    # menggantikan toggle enable/disable yang dulu ada di Settings UI.
    compiled_meta = load_compiled_tools()
    alias_map     = get_alias_map()  # alias pendek → nama tool asli

    # ── Load & kategorisasi custom tools ────────────────────────────────────
    all_custom: List[Dict] = [
        t for t in load_custom_tools()
        if isinstance(t, dict) and t.get("name") and t["name"] in compiled_meta
    ]

    # Reset dan isi ulang _CATEGORY_TOOLS untuk custom
    _CATEGORY_TOOLS["custom"] = []
    # Map: cat_name → list nama custom tool untuk kategori itu
    _custom_by_cat: Dict[str, List[str]] = {cat: [] for cat in VALID_CATEGORIES}

    for t in all_custom:
        tname = t["name"]
        tcat  = str(t.get("category", "custom")).lower().strip()
        if tcat not in VALID_CATEGORIES:
            tcat = "custom"
        _custom_by_cat[tcat].append(tname)
        if tname not in _CATEGORY_TOOLS.get(tcat, []):
            _CATEGORY_TOOLS.setdefault(tcat, []).append(tname)
        if tname not in _CATEGORY_TOOLS["custom"]:
            _CATEGORY_TOOLS["custom"].append(tname)

    has_custom = bool(all_custom)

    _log("\n══════════════════════════════════════════════════════════════")
    _log(f"  HIERARCHICAL ROUTER")
    _log(f"  user       = {username} ({user_id})")
    _log(f"  input      = {user_input[:80]}")
    _log(f"  custom     = {len(all_custom)} tool(s): {[t['name'] for t in all_custom]}")
    _log("══════════════════════════════════════════════════════════════")

    # ══════════════════════════════════════════════════════════════════════
    # GATE 0 — Pure Python, zero LLM
    # ══════════════════════════════════════════════════════════════════════
    from gate import gate0, gate1, decision_graph

    g0 = gate0(user_input)
    if g0 == "chat":
        _log("  [GATE 0] → chat (pure rule match) — skip semua LLM routing")
        LAST_GATE_INFO.update({"gate0": "chat", "gate1_type": "chat", "complexity": "lite"})
        return []

    # ══════════════════════════════════════════════════════════════════════
    # CACHE LOOKUP — cek dulu sebelum jalankan gating lanjutan
    # ══════════════════════════════════════════════════════════════════════
    try:
        from router_cache import lookup as _cache_lookup, save as _cache_save, purge_invalid as _cache_purge
        _cache_available = True
        try:
            removed = _cache_purge()
            if removed > 0:
                _log(f"  [CACHE] 🧹 purged {removed} invalid entries")
        except Exception:
            pass
    except ImportError:
        _cache_lookup    = None
        _cache_save      = None
        _cache_available = False

    if _cache_available:
        _log("\n── CACHE LOOKUP ────────────────────────────────────────────")
        cached = _cache_lookup(user_input, llm_call, dbg=dbg)
        if cached is not None:
            for c in cached:
                if c.get("f") in _USER_ID_INJECT_FUNCS:
                    c.setdefault("a", {})["id"] = user_id
            _log(f"  [CACHE] ⚡ HIT — skip router | calls={[c['f'] for c in cached]}")
            _log("══════════════════════════════════════════════════════════════")
            LAST_GATE_INFO.update({"gate0": None, "gate1_type": "task", "complexity": "lite"})
            return cached

    # ══════════════════════════════════════════════════════════════════════
    # GATE 1 — Tiny LLM intent gate
    # ══════════════════════════════════════════════════════════════════════
    _log("\n── GATE 1: INTENT ──────────────────────────────────────────")
    g1 = gate1(user_input, llm_call)
    _log(f"  [GATE 1] type={g1['type']} complexity={g1['complexity']}")
    LAST_GATE_INFO.update({"gate0": None, "gate1_type": g1["type"], "complexity": g1["complexity"]})

    if g1["type"] == "chat":
        _log("  [GATE 1] → chat — skip semua tool routing")
        return []

    # ══════════════════════════════════════════════════════════════════════
    # DECISION GRAPH — ganti Pass A monolitik
    # ══════════════════════════════════════════════════════════════════════
    _log("\n── DECISION GRAPH ───────────────────────────────────────────")
    categories = decision_graph(user_input, llm_call, has_custom=has_custom, dbg=dbg)

    if not categories:
        _log("  [DG] ⚠️  tidak ada kategori terdeteksi, skip tool routing")
        return []

    # ══════════════════════════════════════════════════════════════════════
    # PASS B + C — per kategori (reuse existing logic)
    # ══════════════════════════════════════════════════════════════════════
    final_calls: List[Dict] = []
    already_funcs: set      = set()

    for cat_idx, cat in enumerate(categories, 1):
        _log(f"\n── PASS B+C [{cat_idx}/{len(categories)}]: [{cat.upper()}] {'─'*40}")
        _log(f"  built-in tools : {_CATEGORY_TOOLS.get(cat, [])}")

        extra_for_cat   = _custom_by_cat.get(cat, [])
        custom_for_cat  = (
            [t for t in all_custom if t["name"] in extra_for_cat]
            if cat == "custom"
            else [t for t in all_custom if str(t.get("category","custom")).lower() == cat]
        )

        call = _run_pass_bc_for_category(
            cat, user_id, username, user_input, char_prompts, llm_call,
            custom_for_cat, extra_for_cat, compiled_meta, alias_map,
            already_funcs, max_retry, dbg,
        )

        if call is not None:
            fname = call["f"]
            if fname not in already_funcs:
                final_calls.append(call)
                already_funcs.add(fname)
                _log(f"  ★  CONFIRMED [{cat}]: f={fname} a={call.get('a',{})}")
            else:
                _log(f"  ⚠️  SKIP [{cat}]: f={fname} sudah ada di hasil lain")
        else:
            _log(f"  ✗  SKIP [{cat}] — tidak ada tool valid")

    _log(f"\n══════════════════════════════════════════════════════════════")
    _log(f"  HIERARCHICAL ROUTER DONE")
    _log(f"  gate0=miss gate1={g1} categories={categories}")
    _log(f"  confirmed calls : {len(final_calls)}")
    for i, c in enumerate(final_calls, 1):
        _log(f"    [{i}] f={c['f']} a={c.get('a',{})}")
    _log(f"══════════════════════════════════════════════════════════════")

    if _cache_available and final_calls and _cache_save:
        calls_to_cache = []
        for c in final_calls:
            a_clean = {k: v for k, v in c.get("a", {}).items() if k != "id"}
            calls_to_cache.append({"f": c["f"], "a": a_clean})
        _cache_save(user_input, calls_to_cache)
        _log(f"  [CACHE] 💾 saved — {[c['f'] for c in calls_to_cache]}")

    return final_calls
