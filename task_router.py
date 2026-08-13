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
import threading
from typing import Dict, List, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    pass


# ═════════════════════════════════════════════════════════════════════════════
# KATEGORI & TOOL MAP
# ═════════════════════════════════════════════════════════════════════════════

# PATCH v2: threshold jumlah kategori/task yang dianggap "bakal lama" —
# begitu decision_graph (+ trigger_phrases safety-net) memutuskan >= angka
# ini kategori perlu di-Pass B/C, stall_callback dipicu. Tunable tanpa perlu
# bongkar logic run_task_router().
_STALL_MIN_CATEGORIES = 2

# PATCH v4: kategori built-in yang SIFATNYA internal/lokal — biasanya
# selesai dalam 1-2 percobaan tanpa panggilan jaringan eksternal (beda
# dengan state/event/social/custom/cloud yang sering perlu EXEC ke API
# luar). Kalau kategori yang dieksplor HANYA berisi ini, jangan trigger
# stall — root cause laporan "masih pakai interrupt buat percakapan biasa":
# chat roleplay/romantis (family='character' → categories=['self','meta'])
# ke-hitung "2 kategori" dan lolos threshold, padahal itu bukan proses
# yang lama — malah bikin immersion pecah gara-gara ada "tunggu sebentar"
# di tengah momen roleplay.
_STALL_FAST_CATEGORIES = {"self", "meta"}

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

def _estimate_confidence(
    fname: str,
    cat: str,
    user_input: str,
    compiled_meta: Dict,
    has_prior_rejection: bool = False,
) -> int:
    """
    PATCH: perkiraan confidence Pass B TANPA perlu ubah prompt/format output
    Pass B (jadi tidak mengganggu jalur yang sudah jalan).

    Baseline TURUN kalau pilihan sebelumnya di kategori ini SUDAH pernah
    ditolak Pass C (has_prior_rejection) — itu sinyal paling jujur bahwa
    kasus ini genuinely ambigu ("pusing"), bukan cuma confidence statis di
    attempt pertama (percobaan pertama awalnya base_conf tinggi supaya tetap
    baseline "execute"/"validate" seperti perilaku lama utk kasus normal;
    baseline RENDAH baru dipakai SETELAH ada penolakan, supaya tier 'skip'
    — yang men-trigger thinking mode — benar-benar bisa kesentuh, bukan
    cuma teori di atas kertas).

    Bonus tambahan dari 2 sumber sinyal keyword:
      1. gate.compute_confidence() — regex per KATEGORI built-in.
      2. trigger_phrases tool yang dipilih (kalau fname itu compiled/custom
         tool) — lebih presisi per-tool daripada per-kategori.
    """
    from gate import compute_confidence
    base_conf = 60 if has_prior_rejection else 85
    conf = compute_confidence(fname, cat, base_conf, user_input)

    meta = (compiled_meta or {}).get(fname)
    if meta:
        phrases = meta.get("trigger_phrases") or []
        text = user_input.lower()
        if any(p and str(p).lower() in text for p in phrases):
            conf = min(100, conf + 15)

    return conf


_THINK_SYS_SUFFIX = """

[THINK+PILIH ULANG]
Pilihan sebelumnya kurang meyakinkan. Pikirkan LEBIH TELITI sebelum putuskan:
1. Apa sebenarnya yang user butuhkan dari chat ini?
2. Dari daftar fungsi di atas, mana yang PALING pas? Atau memang tidak ada
   yang cocok sama sekali?
Output HANYA JSON satu baris: {"reasoning":"<max 15 kata>","f":"nama_fungsi_atau_null","a":{}}
Kalau memang tidak ada yang cocok, f harus null.
"""


def _pass_b_think(
    cat:            str,
    user_input:     str,
    prev_fname:     str,
    valid_funcs:    set,
    rejected_funcs: List[str],
    prompt_b:       str,
    llm_call,
    dbg=None,
) -> Optional[Dict]:
    """
    "Thinking mode" — dipanggil HANYA saat confidence Pass B rendah (tier
    'skip'). Beda dari retry Pass B biasa: prompt-nya membawa instruksi
    reasoning eksplisit (mempertimbangkan ulang pilihan sebelumnya + alasan),
    bukan cuma "coba lagi" tanpa konteks kenapa attempt sebelumnya lemah.
    Token budget lebih besar dari Pass B normal supaya ada ruang buat
    reasoning singkat sebelum keputusan akhir.
    Return {"f":..,"a":..} atau None kalau setelah dipikir ulang tetap
    tidak ada yang cocok.
    """
    def _log(msg: str):
        if dbg:
            dbg.line(msg)

    input_think = (
        f"username/chat: {user_input}\n"
        f"Pilihan sebelumnya (confidence rendah): {prev_fname}\n"
        f"Sudah ditolak validator: {rejected_funcs}\n"
        f"Fungsi valid utk kategori '{cat}': {sorted(valid_funcs)}"
    )
    try:
        resp = llm_call(
            "react",
            messages=[
                {"role": "system", "content": prompt_b + _THINK_SYS_SUFFIX},
                {"role": "user",   "content": input_think},
            ],
            temperature=0.0,
            max_tokens=100,
        )
        raw = (resp.choices[0].message.content or "").strip()
        _log(f"  [THINK RAW] {raw!r}")
        data = _parse_json_tolerant(raw)
        if not data or not data.get("f") or str(data["f"]).strip().lower() == "null":
            return None
        return {"f": str(data["f"]).strip(), "a": dict(data.get("a", {}) or {})}
    except Exception as e:
        _log(f"  [THINK ERROR] {e}")
        return None


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
            # PATCH: dulu langsung `break` (nyerah total) begitu output null,
            # walau max_retry masih ada budget. Itu bug — model kecil kadang
            # cuma "hiccup" 1x, retry berikutnya sering berhasil (lihat kasus
            # "cuaca jakarta" yang null di attempt 1 tapi tool-nya jelas ada).
            # Sekarang retry seperti Pass C reject, tetap dibatasi max_retry.
            _log(f"  └── PASS B: null/invalid output → retry (masih ada budget)")
            attempt += 1
            continue

        fname = parsed["f"]

        if fname in already_funcs:
            _log(f"  [PASS B] {fname} sudah ada di hasil, skip")
            break

        # ── PATCH: confidence voting — "thinking mode" utk kasus ambigu ────
        # compute_confidence()/route_by_confidence() sudah ada di gate.py
        # dari awal tapi belum pernah dipakai di manapun. Sekarang disambung:
        #   - confidence TINGGI (>=95): tool ini jelas banget cocok (model
        #     pilih + regex/trigger_phrase juga match) → skip Pass C,
        #     langsung terima. Lebih cepat utk kasus yang sudah jelas.
        #   - confidence SEDANG (75-94): jalur normal, tetap lewat Pass C
        #     seperti biasa (yes/no cepat).
        #   - confidence RENDAH (<75): BARU di sini masuk "thinking mode" —
        #     bukan retry buta, tapi 1 langkah reasoning eksplisit yang
        #     mempertimbangkan ulang pilihan sebelumnya sebelum diputuskan.
        #     Jadi biaya ekstra HANYA muncul di kasus yang beneran ambigu,
        #     bukan di semua request.
        from gate import compute_confidence, route_by_confidence
        conf = _estimate_confidence(
            fname, cat, user_input, compiled_meta,
            has_prior_rejection=bool(rejected_funcs),
        )
        tier = route_by_confidence(conf)
        _log(f"  │   confidence={conf} → tier={tier}")

        if tier == "execute":
            _log(f"  ⚡ confidence tinggi → skip Pass C, langsung terima f={fname}")
            call = parsed
            break

        if tier == "skip":
            _log(f"  🧠 confidence rendah → thinking mode (reasoning ulang)")
            thought = _pass_b_think(
                cat, user_input, fname, valid_funcs, rejected_funcs,
                prompt_b, llm_call, dbg,
            )
            if thought is None:
                _log(f"  └── THINK: tidak ada yang cocok setelah dipikir ulang → retry")
                rejected_funcs.append(fname)
                attempt += 1
                continue
            parsed = thought
            fname  = parsed["f"]
            _log(f"  │   THINK hasil baru: f={fname} a={parsed.get('a',{})}")
            # lanjut ke Pass C normal di bawah dgn pilihan hasil thinking —
            # tetap divalidasi, thinking mode menambah akurasi, bukan
            # menggantikan validasi akhir.

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


def _match_trigger_categories(user_input: str, compiled_meta: Dict) -> List[str]:
    """
    PATCH: safety-net keyword matching supaya compiled/custom tool (mis.
    get_weather, category="meta") TIDAK silent ke-skip kalau decision_graph
    (LLM classifier di gate.py) salah menebak family/kategori untuk input
    tsb — root cause bug "cuaca ga kepanggil": DG menebak family='live' →
    categories=['state','event','social'], padahal get_weather terdaftar di
    kategori 'meta', jadi Pass B+C TIDAK PERNAH dijalankan untuk 'meta' sama
    sekali, apa pun isi Pass B-nya.

    Cek trigger_phrases tiap compiled tool terhadap user_input secara
    harfiah (substring match, tanpa LLM, jadi nol biaya tambahan). Kalau
    match, kategori tool itu WAJIB ikut di-explore di Pass B — di luar
    kendali/kesalahan decision_graph.

    Return list kategori (unik) yang wajib ditambahkan, [] kalau tidak ada.
    """
    text = (user_input or "").lower()
    if not text:
        return []
    hit_categories: List[str] = []
    for tname, meta in (compiled_meta or {}).items():
        if not isinstance(meta, dict) or not meta.get("enabled", True):
            continue
        phrases = meta.get("trigger_phrases") or []
        if not any(p and str(p).lower() in text for p in phrases):
            continue
        cat = str(meta.get("category", "custom")).lower().strip()
        if cat not in VALID_CATEGORIES:
            cat = "custom"
        if cat not in hit_categories:
            hit_categories.append(cat)
    return hit_categories


# PATCH v6: deskripsi singkat tiap kategori — dipakai HANYA untuk prompt
# validasi kategori (_validate_categories), bukan ditampilkan ke user.
_CATEGORY_DESC: Dict[str, str] = {
    "user":   "data/preferensi USER (bukan karakter)",
    "chat":   "riwayat/rangkuman percakapan",
    "state":  "data eksternal real-time: cuaca, kondisi saat ini",
    "event":  "jadwal, planner, event terjadwal",
    "game":   "hadiah, gift, poin romance",
    "social": "hubungan sosial, follow, relasi user lain",
    "self":   "identitas/kepribadian KARAKTER: nama, suka, sesi role, mood",
    "meta":   "waktu/tanggal saat ini, command aktif, animasi sistem",
    "custom": "tool custom buatan user",
    "cloud":  "baca/simpan/hapus data cloud",
}


def _validate_categories(
    user_input: str,
    categories: List[str],
    llm_call,
    dbg=None,
) -> List[str]:
    """
    PATCH v6: validasi kategori SEBELUM Pass B/C — 1 call kecil (yes/no per
    kategori kandidat, bukan konten panjang) buat nyaring kategori yang
    decision_graph sertakan tapi sebenarnya tidak relevan untuk pertanyaan
    spesifik ini.

    Root cause yang difix: decision_graph sering nge-pair kategori secara
    KAKU per family (mis. family='character' hampir selalu → ['self','meta']),
    padahal belum tentu dua-duanya relevan — contoh nyata: "info hari ini"
    cuma butuh 'meta' (waktu), TIDAK butuh 'self' (identitas). Tanpa filter
    ini, 'self' tetap di-Pass B/C penuh: pilih tool → Pass C reject → retry
    Pass B → baru nyerah — 2-3 LLM call kebuang percuma tiap turn yang
    "kebetulan" masuk kategori 'character'.

    HANYA jalan kalau len(categories) > 1 — kalau cuma 1 kandidat, tidak ada
    yang perlu disaring (mubazir manggil validate buat 1 opsi doang). 1 call
    kecil ini jauh lebih murah daripada 2-4 call yang biasanya kebuang di
    Pass B/C untuk kategori yang keliru.

    Fail-safe: kalau parsing gagal / error / semua kategori ke-drop, balik
    ke daftar kategori ASLI (lebih baik sedikit boros daripada kehilangan
    tool yang sebenarnya perlu).
    """
    if len(categories) <= 1:
        return categories

    def _log(msg: str):
        if dbg:
            dbg.line(msg)

    desc_lines = "\n".join(f'- {c}: {_CATEGORY_DESC.get(c, c)}' for c in categories)
    sys_txt = (
        "Untuk tiap kategori tool di bawah, tentukan apakah BENERAN dibutuhkan "
        "buat menjawab pesan user ini secara SPESIFIK (bukan sekadar masih "
        "berhubungan tema besar).\n"
        f"{desc_lines}\n"
        'Jawab HANYA JSON satu baris: {"nama_kategori": true/false, ...} '
        "untuk SEMUA kategori di atas, tanpa teks lain."
    )
    try:
        resp = llm_call(
            "react",
            messages=[
                {"role": "system", "content": sys_txt},
                {"role": "user", "content": f'Pesan user: "{user_input}"'},
            ],
            temperature=0.0,
            max_tokens=60,
        )
        raw = (resp.choices[0].message.content or "").strip()
        raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw, flags=re.I).strip()
        parsed = json.loads(raw)

        # default True kalau model lupa sebut salah satu kategori — fail-safe
        # per-kategori, lebih aman drop cuma yang eksplisit false.
        kept = [c for c in categories if bool(parsed.get(c, True))]
        dropped = [c for c in categories if c not in kept]

        if dropped:
            _log(f"  [CAT VALIDATE] drop {dropped} (tidak relevan) → lanjut hanya {kept}")
        if not kept:
            _log("  [CAT VALIDATE] ⚠️  semua kategori ke-drop → fail-safe, tetap pakai semua")
            return categories
        return kept
    except Exception as e:
        _log(f"  [CAT VALIDATE] ⚠️ error: {e} → fail-safe, tetap pakai semua kategori")
        return categories


def precheck(
    user_id:      str,
    username:     str,
    user_input:   str,
    llm_call,
    dbg=None,
    stall_callback=None,
) -> Dict:
    """
    PATCH v5: bagian gate0 → cache lookup → gate1 → decision_graph →
    trigger_phrases safety-net → stall trigger. DIPISAH dari execute_categories()
    supaya caller (main.py) bisa menjalankan fungsi ini BERSAMAAN dengan
    conversation_state.analyze() lewat concurrency.get_executor() — dua-duanya
    cuma butuh (user_input, history/llm_call), tidak saling bergantung, tapi
    sebelumnya dipanggil berurutan.

    Return dict:
      {"done": True,  "calls": [...]}
          → sudah final, TIDAK perlu Pass B/C sama sekali (gate0/gate1=chat,
            atau cache hit). Caller langsung pakai calls ini.
      {"done": False, "categories": [...], "compiled_meta":.., "alias_map":..,
       "all_custom":.., "custom_by_cat":.., "has_custom":.., "g1":..}
          → lanjut ke execute_categories(pre, ...) untuk Pass B/C+EXEC.
    """
    from mcp_tools import load_custom_tools
    from tool_compiler import load_compiled_tools, build_pass_b_addon, get_alias_map
    from gate import gate0, gate1, decision_graph

    def _log(msg: str):
        if dbg:
            dbg.line(msg)

    compiled_meta = load_compiled_tools()
    alias_map     = get_alias_map()

    all_custom: List[Dict] = [
        t for t in load_custom_tools()
        if isinstance(t, dict) and t.get("name") and t["name"] in compiled_meta
    ]

    _CATEGORY_TOOLS["custom"] = []
    custom_by_cat: Dict[str, List[str]] = {cat: [] for cat in VALID_CATEGORIES}
    for t in all_custom:
        tname = t["name"]
        tcat  = str(t.get("category", "custom")).lower().strip()
        if tcat not in VALID_CATEGORIES:
            tcat = "custom"
        custom_by_cat[tcat].append(tname)
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
    g0 = gate0(user_input)
    if g0 == "chat":
        _log("  [GATE 0] → chat (pure rule match) — skip semua LLM routing")
        LAST_GATE_INFO.update({"gate0": "chat", "gate1_type": "chat", "complexity": "lite"})
        return {"done": True, "calls": []}

    # ══════════════════════════════════════════════════════════════════════
    # CACHE LOOKUP
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
            return {"done": True, "calls": cached}

    # ══════════════════════════════════════════════════════════════════════
    # GATE 1 — Tiny LLM intent gate
    # ══════════════════════════════════════════════════════════════════════
    # PATCH v7 revert: SEMPAT dicoba fan-out gate1+L1 paralel lewat shared
    # pool (concurrency.get_executor()) di sini. Ternyata BERMASALAH begitu
    # jalan lewat alur asli (full_generate() di main.py) — precheck() ITU
    # SENDIRI sudah dipanggil sebagai task di dalam pool yang sama
    # (dibarengin dengan _cs_analyze, lihat main.py full_generate() PATCH
    # v5), jadi kedua slot MAX_PARALLEL_LLM sudah penuh SEBELUM precheck()
    # sempat nge-submit L1 sebagai task ke-3 — L1 jadi antre nunggu slot
    # yang gak akan bebas (precheck() sendiri lagi nunggu hasilnya), jadi
    # paralelismenya gak kepakai (worst case malah nunggu sampai timeout
    # baru fallback). Paralelisme yang BENERAN efektif itu sudah ada di
    # level LUAR (full_generate(): Analyzer || precheck — sudah ada dari
    # PATCH v5 lama), jadi di SINI baliknya sequential biasa saja — jangan
    # nested submit ke pool yang sama dari dalam task yang sudah jalan
    # di pool itu.
    _log("\n── GATE 1: INTENT ──────────────────────────────────────────")
    g1 = gate1(user_input, llm_call)
    _log(f"  [GATE 1] type={g1['type']} complexity={g1['complexity']}")
    LAST_GATE_INFO.update({"gate0": None, "gate1_type": g1["type"], "complexity": g1["complexity"]})

    if g1["type"] == "chat":
        _log("  [GATE 1] → chat — skip semua tool routing")
        return {"done": True, "calls": []}

    # ══════════════════════════════════════════════════════════════════════
    # DECISION GRAPH
    # ══════════════════════════════════════════════════════════════════════
    _log("\n── DECISION GRAPH ───────────────────────────────────────────")
    categories = decision_graph(user_input, llm_call, has_custom=has_custom, dbg=dbg)

    forced_categories = _match_trigger_categories(user_input, compiled_meta)
    if forced_categories:
        missing = [c for c in forced_categories if c not in categories]
        if missing:
            _log(
                f"  [DG] ⚠️  trigger_phrases match tool di kategori {missing} "
                f"tapi decision_graph tidak menyertakannya → dipaksa ditambahkan"
            )
            categories = list(categories) + missing

    if not categories:
        _log("  [DG] ⚠️  tidak ada kategori terdeteksi, skip tool routing")
        return {"done": True, "calls": []}

    # ── PATCH v6: validasi kategori — saring yang tidak relevan SEBELUM
    # Pass B/C (lihat docstring _validate_categories). Stall trigger di
    # bawah SENGAJA pakai `categories` versi SUDAH disaring ini, supaya
    # hitungan "berapa kategori" juga akurat (kategori yang di-drop tidak
    # ikut menyumbang ke threshold stall).
    categories = _validate_categories(user_input, categories, llm_call, dbg=dbg)
    if not categories:
        _log("  [CAT VALIDATE] ⚠️  tidak ada kategori tersisa, skip tool routing")
        return {"done": True, "calls": []}

    all_fast_categories = bool(categories) and all(c in _STALL_FAST_CATEGORIES for c in categories)

    if (
        stall_callback is not None
        and len(categories) >= _STALL_MIN_CATEGORIES
        and not all_fast_categories
    ):
        _log(
            f"  [STALL] {len(categories)} kategori terdeteksi "
            f"(>={_STALL_MIN_CATEGORIES}) → trigger pesan tunggu (thread terpisah, non-blocking)"
        )

        def _run_stall():
            try:
                stall_callback(user_input)
            except Exception as e:
                _log(f"  [STALL] ⚠️ error (background thread): {e}")

        threading.Thread(target=_run_stall, daemon=True, name="stall-message").start()
    elif stall_callback is not None and len(categories) >= _STALL_MIN_CATEGORIES and all_fast_categories:
        _log(
            f"  [STALL] {len(categories)} kategori ({categories}) tapi semuanya "
            f"internal/cepat ({sorted(_STALL_FAST_CATEGORIES)}) → skip, tidak trigger pesan tunggu"
        )

    return {
        "done":          False,
        "categories":    categories,
        "compiled_meta": compiled_meta,
        "alias_map":     alias_map,
        "all_custom":    all_custom,
        "custom_by_cat": custom_by_cat,
        "has_custom":    has_custom,
        "g1":            g1,
        "cache_save":    _cache_save if _cache_available else None,
    }


def execute_categories(
    pre:          Dict,
    user_id:      str,
    username:     str,
    user_input:   str,
    char_prompts: Dict,
    llm_call,
    dbg=None,
    max_retry:    int = 2,
) -> List[Dict]:
    """
    PATCH v5: Pass B+C per kategori — dijalankan PARALEL lewat shared bounded
    executor (concurrency.py), dibatasi MAX_PARALLEL_LLM slot (samakan dengan
    setting "max concurrent" di server inference lokal kamu — LM Studio: 2).
    Sebelumnya kategori dieksplor satu-satu berurutan (state → event → social
    → meta); sekarang ditembak sekaligus, ke-antre otomatis di level pool
    kalau jumlah kategori > slot yang tersedia.

    Catatan thread-safety: `already_funcs` dibaca oleh tiap kategori yang
    jalan (mis. buat cloud short-circuit) tapi HANYA di-update SETELAH semua
    future selesai (lihat loop as_completed di bawah) — jadi selama fase
    paralel, kategori yang jalan bersamaan tidak saling tahu satu sama lain
    baru pilih tool apa. Kalau 2 kategori kebetulan pilih tool yang SAMA,
    duplikatnya tetap ke-filter di loop pengumpulan hasil (sama seperti
    perilaku lama) — cuma sedikit lebih boros (tool ke-2 nya kepilih sia-sia)
    dibanding versi sekuensial yang bisa saling cegah dari awal. Trade-off
    ini sepadan dengan speedup dari paralelisasi.
    """
    from concurrency import get_executor
    import concurrent.futures as _cf

    def _log(msg: str):
        if dbg:
            dbg.line(msg)

    categories    = pre["categories"]
    compiled_meta = pre["compiled_meta"]
    alias_map     = pre["alias_map"]
    all_custom    = pre["all_custom"]
    custom_by_cat = pre["custom_by_cat"]
    g1            = pre["g1"]
    _cache_save   = pre.get("cache_save")

    already_funcs: set = set()
    ex = get_executor()

    _log(
        f"\n── PASS B+C — {len(categories)} kategori, paralel "
        f"(maks {ex._max_workers} slot bersamaan) ──────────────"
    )

    futures = {}
    for cat in categories:
        extra_for_cat  = custom_by_cat.get(cat, [])
        custom_for_cat = (
            [t for t in all_custom if t["name"] in extra_for_cat]
            if cat == "custom"
            else [t for t in all_custom if str(t.get("category", "custom")).lower() == cat]
        )
        fut = ex.submit(
            _run_pass_bc_for_category,
            cat, user_id, username, user_input, char_prompts, llm_call,
            custom_for_cat, extra_for_cat, compiled_meta, alias_map,
            already_funcs, max_retry, dbg,
        )
        futures[fut] = cat

    final_calls: List[Dict] = []
    for fut in _cf.as_completed(futures):
        cat = futures[fut]
        try:
            call = fut.result()
        except Exception as e:
            _log(f"  ⚠️  [{cat}] error saat Pass B/C: {e}")
            continue

        if call is not None:
            fname = call["f"]
            if fname not in already_funcs:
                final_calls.append(call)
                already_funcs.add(fname)
                _log(f"  ★  CONFIRMED [{cat}]: f={fname} a={call.get('a',{})}")
            else:
                _log(f"  ⚠️  SKIP [{cat}]: f={fname} sudah ada di hasil lain (duplikat lintas-kategori)")
        else:
            _log(f"  ✗  SKIP [{cat}] — tidak ada tool valid")

    _log(f"\n══════════════════════════════════════════════════════════════")
    _log(f"  HIERARCHICAL ROUTER DONE")
    _log(f"  gate0=miss gate1={g1} categories={categories}")
    _log(f"  confirmed calls : {len(final_calls)}")
    for i, c in enumerate(final_calls, 1):
        _log(f"    [{i}] f={c['f']} a={c.get('a',{})}")
    _log(f"══════════════════════════════════════════════════════════════")

    if _cache_save and final_calls:
        calls_to_cache = []
        for c in final_calls:
            a_clean = {k: v for k, v in c.get("a", {}).items() if k != "id"}
            calls_to_cache.append({"f": c["f"], "a": a_clean})
        _cache_save(user_input, calls_to_cache, dbg=dbg)
        _log(f"  [CACHE] 💾 saved — {[c['f'] for c in calls_to_cache]}")

    return final_calls


def run_task_router(
    user_id:      str,
    username:     str,
    user_input:   str,
    char_prompts: Dict,
    llm_call,               # _llm_call(pass_name, messages=..., temperature=..., max_tokens=...)
    dbg=None,               # DebugLogger optional
    max_retry:    int = 2,  # max retry Pass B per kategori jika Pass C reject
    stall_callback=None,    # callable(user_input: str) — lihat docstring precheck()
) -> List[Dict]:
    """
    Compat wrapper — precheck() lalu execute_categories() berurutan.

    Caller yang mau fan-out Analyzer+precheck() secara paralel (lihat
    concurrency.py) sebaiknya panggil precheck()+execute_categories() secara
    terpisah, bukan lewat fungsi ini. Fungsi ini dipertahankan supaya
    caller lama yang belum pindah ke pola paralel tetap jalan tanpa ubahan.
    """
    pre = precheck(user_id, username, user_input, llm_call, dbg=dbg, stall_callback=stall_callback)
    if pre["done"]:
        return pre["calls"]
    return execute_categories(pre, user_id, username, user_input, char_prompts, llm_call, dbg=dbg, max_retry=max_retry)
