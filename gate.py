"""
gate.py — Hierarchical Gating untuk task router.

Gate 0  : Pure Python — HANYA rule yang 100% deterministik.
           Tidak ada hardcode greeting/expression list.
           Yang di-catch: kosong, ≤2 char, emoji only, punct only,
           number only, repeat char, repeat message.
           Semua yang ambigu → lanjut ke Gate 1.

Gate 1  : 1 LLM call kecil (~0.3s) — klasifikasi intent.
           Output: {"type": "chat"|"task"|"mixed", "complexity": "lite"|"deep"}
           Jauh lebih akurat dari hardcode list.

Decision Graph : 2-level binary tree — ganti Pass A monolitik.
Confidence Voting : model confidence + regex keyword signal.
"""

from __future__ import annotations

import json
import re
import unicodedata
from typing import Dict, List, Optional, Tuple


# ═════════════════════════════════════════════════════════════════════════════
# GATE 0 — Pure Python, HANYA rule deterministik 100%
# ═════════════════════════════════════════════════════════════════════════════

_EMOJI_RANGES = (
    (0x1F300, 0x1FAFF),
    (0x2600,  0x27BF),
    (0x2300,  0x23FF),
)
_PUNCT_ONLY_RE  = re.compile(r"^[^\w\s]+$")
_NUMBER_ONLY_RE = re.compile(r"^\d+(\.\d+)?$")
_REPEAT_CHAR_RE = re.compile(r"^(.)\1{2,}$")
_REPEAT_RUN_RE  = re.compile(r"(.)\1{2,}")


def _is_emoji_only(text: str) -> bool:
    for ch in text:
        if ch.isspace():
            continue
        code = ord(ch)
        cat  = unicodedata.category(ch)
        if not any(lo <= code <= hi for lo, hi in _EMOJI_RANGES) \
                and cat not in ("So", "Sm", "Sk"):
            return False
    return len(text.strip()) > 0


def _is_mostly_repeated_char(text: str, threshold: float = 0.7) -> bool:
    if len(text) < 3 or not _REPEAT_RUN_RE.search(text):
        return False
    from collections import Counter
    counts = Counter(text.lower())
    return counts.most_common(1)[0][1] / len(text) >= threshold


def gate0(text: str, prev_text: str = "") -> Optional[str]:
    """
    Pure Python gate — hanya catch yang 100% pasti bukan task.
    Return "chat" atau None (lanjut ke Gate 1).

    Rule:
      1. Kosong / ≤2 char non-spasi
      2. Emoji only
      3. Punctuation only  (...  ???  !!!)
      4. Number only       (123  3.14)
      5. Repeat char       (aaaa  hmmmm  ???)
      6. Repeat message    (sama persis dengan pesan sebelumnya)

    TIDAK ada hardcode greeting/expression list — semua itu ke Gate 1.
    """
    stripped = text.strip()
    no_space = stripped.replace(" ", "")

    # 1. Kosong / ≤2 char
    if len(no_space) <= 2:
        return "chat"

    # 2. Emoji only
    if _is_emoji_only(stripped):
        return "chat"

    # 3. Punctuation only
    if _PUNCT_ONLY_RE.match(stripped):
        return "chat"

    # 4. Number only
    if _NUMBER_ONLY_RE.match(stripped):
        return "chat"

    # 5. Repeat char (aaaa, hmmmm, ????)
    if _REPEAT_CHAR_RE.match(no_space) or _is_mostly_repeated_char(no_space):
        return "chat"

    # 6. Repeat message exact
    if prev_text and stripped.lower() == prev_text.strip().lower():
        return "chat"

    return None   # lanjut ke Gate 1


# ═════════════════════════════════════════════════════════════════════════════
# GATE 1 — Tiny LLM Intent Gate (~0.3s)
# ═════════════════════════════════════════════════════════════════════════════

_GATE1_SYS = """\
Klasifikasikan apakah user meminta AKSI/INFORMASI atau hanya mengobrol.
Jawab HANYA JSON satu baris, tanpa penjelasan.

Output format:
{"type":"chat","complexity":"lite"}
{"type":"task","complexity":"lite"}
{"type":"task","complexity":"deep"}
{"type":"mixed","complexity":"deep"}

type:
  chat = greeting, ekspresi, bercanda, kata acak, obrolan ringan, tidak butuh tool/data apapun
  task = butuh data atau tool (cek memory/romance, cuaca, simpan ke cloud, info stream, dll)
  mixed = ada chat sekaligus ada task

complexity:
  lite = cukup jawab singkat (≤3 kalimat)
  deep = perlu jawaban panjang, emosi mendalam, roleplay, analisa, diskusi

Contoh chat: "halo", "gimana hari ini", "wkwk", "dame", "test test", "hehe", "ok deh", "ya ampun", "lagi apa"
Contoh task: "cek memory aku", "cuaca jakarta", "berapa romance points aku", "simpan ke cloud", "siapa top gifter"

Kata pendek atau acak tanpa konteks → chat.
Jangan tulis apapun selain JSON.
"""


def gate1(text: str, llm_call) -> Dict:
    """
    1 LLM call kecil untuk klasifikasi intent.
    Return {"type": "chat"|"task"|"mixed", "complexity": "lite"|"deep"}.
    Default ke {"type":"task","complexity":"lite"} jika gagal (safe fallback).
    """
    try:
        resp = llm_call(
            "react",
            messages=[
                {"role": "system", "content": _GATE1_SYS},
                {"role": "user",   "content": text},
            ],
            temperature=0.0,
            max_tokens=30,
        )
        raw = (resp.choices[0].message.content or "").strip()
        raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw, flags=re.I).strip()
        result = json.loads(raw)
        t = result.get("type", "task")
        c = result.get("complexity", "lite")
        if t not in ("chat", "task", "mixed"): t = "task"
        if c not in ("lite", "deep"):          c = "lite"
        return {"type": t, "complexity": c}
    except Exception:
        return {"type": "task", "complexity": "lite"}


# ═════════════════════════════════════════════════════════════════════════════
# DECISION GRAPH — ganti Pass A
# ═════════════════════════════════════════════════════════════════════════════

_DG_L1_SYS = """\
Apakah permintaan ini tentang data INTERNAL (user/karakter/cloud/chat/waktu) atau EKSTERNAL (stream/game/cuaca/tool luar)?
Jawab HANYA satu kata: internal atau external

Contoh internal: "cek memory aku", "kamu mood apa", "jam berapa", "tanggal hari ini", "simpan ke cloud", "riwayat chat"
Contoh external: "berapa viewer", "cuaca jakarta", "top gifter", "ada event apa", "lagi game apa"
"""

_DG_L2_INTERNAL_SYS = """\
Tentukan sub-kategori internal. Jawab HANYA satu kata.

memory    = data user, memory, romance, nickname, riwayat chat
character = kondisi karakter AI, mood, mode, sesi, command, waktu, jam, tanggal, jadwal
cloud     = simpan/baca/hapus data di cloud/bin/storage

Contoh:
  "cek memory aku" → memory
  "kamu lagi mood apa" → character
  "jam berapa sekarang" → character
  "tanggal hari ini" → character
  "simpan ke cloud" → cloud
"""

_DG_L2_EXTERNAL_SYS = """\
Tentukan sub-kategori eksternal. Jawab HANYA satu kata.

live     = kondisi stream, viewer, event, follower, gifter
activity = game atau aktivitas yang sedang berjalan
custom   = tool kustom admin (cuaca, musik, dll)
"""

_DG_FAMILY_MAP = {
    "memory":    ["user", "chat"],
    "character": ["self", "meta"],
    "cloud":     ["cloud"],
    "live":      ["state", "event", "social"],
    "activity":  ["game"],
    "custom":    ["custom"],
}


def decision_graph(text: str, llm_call, has_custom: bool = False, dbg=None) -> List[str]:
    """
    2-level decision graph → list kategori untuk Pass B+C.
    """
    def _log(msg):
        if dbg: dbg.line(msg)

    def _call(sys_prompt: str, max_tok: int = 15) -> str:
        try:
            resp = llm_call("react",
                messages=[{"role": "system", "content": sys_prompt},
                          {"role": "user",   "content": text}],
                temperature=0.0, max_tokens=max_tok)
            return (resp.choices[0].message.content or "").strip().lower()
        except Exception:
            return ""

    l1_raw = _call(_DG_L1_SYS)
    _log(f"  [DG L1] raw={l1_raw!r}")

    if "internal" in l1_raw:
        l2_raw = _call(_DG_L2_INTERNAL_SYS)
        _log(f"  [DG L2-internal] raw={l2_raw!r}")
        family = "memory"
        for f in ("memory", "character", "cloud"):
            if f in l2_raw:
                family = f
                break
    else:
        l2_raw = _call(_DG_L2_EXTERNAL_SYS)
        _log(f"  [DG L2-external] raw={l2_raw!r}")
        family = "live"
        for f in ("live", "activity", "custom"):
            if f in l2_raw:
                family = f
                break
        if family == "custom" and not has_custom:
            family = "live"

    categories = _DG_FAMILY_MAP.get(family, ["user"])
    _log(f"  [DG] family={family!r} → categories={categories}")
    return categories


# ═════════════════════════════════════════════════════════════════════════════
# CONFIDENCE VOTING
# ═════════════════════════════════════════════════════════════════════════════

_KEYWORD_SIGNALS: Dict[str, List[str]] = {
    "user":   [r"\b(memory|data|info|romance|nickname|points|gift)\b"],
    "cloud":  [r"\b(simpan|catat|cloud|bin|storage|store)\b"],
    "self":   [r"\b(mood|mode|karakter|kamu lagi|kondisi)\b"],
    "meta":   [r"\b(jam|waktu|tanggal|hari ini|jadwal)\b"],
    "state":  [r"\b(viewer|penonton|stream|siaran)\b"],
    "social": [r"\b(gifter|follower|subscriber|top)\b"],
    "event":  [r"\b(raid|goal|poll|milestone|notif)\b"],
    "game":   [r"\b(game|main|level|score|quest)\b"],
}

CONF_EXECUTE_DIRECT = 95
CONF_NEED_VALIDATOR = 75
CONF_SKIP_TOOL      = 74


def compute_confidence(tool_name: str, category: str, model_conf: int, user_text: str) -> int:
    regex_bonus = 0
    for pat in _KEYWORD_SIGNALS.get(category, []):
        if re.search(pat, user_text, re.I):
            regex_bonus = 15
            break
    return min(100, model_conf + regex_bonus)


def route_by_confidence(confidence: int) -> str:
    if confidence >= CONF_EXECUTE_DIRECT: return "execute"
    if confidence >= CONF_NEED_VALIDATOR: return "validate"
    return "skip"
