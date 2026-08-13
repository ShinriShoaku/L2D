#!/usr/bin/env python3
"""
main.py — v10 pipeline: [TASK] → Execute → [SOUL] → [ANIM] → [TRANS] → Merge

Pipeline:
  Pass 1  [TASK]  : task router (local LM Studio) → function calls
  Pass 2          : execute calls → router_data string
  Pass 3  [SOUL]  : single-prompt dialog generation (online custom API)
  Pass 4  [ANIM]  : tentukan animasi + point (local LM Studio)
  Pass 5  [TRANS] : terjemahkan + segmentasi → [{id, jp}] (local LM Studio)
  Merge           : gabungkan SOUL+ANIM+TRANS → [{ind, jp, anim}]
"""

import json
import os
import random
import re
import time
from collections import OrderedDict
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple
import character_memory
from openai import OpenAI

from config import (
    DEBUG, LM_STUDIO_URL, MEMORY_DIR,
    MODEL_MEMORY_DIR, MODEL_NAME,
    SOUL_API_URL, SOUL_MODEL_NAME,
    get_client, get_model, get_extra_body,
)
from memory import UserMemory, UserMemoryManager
from model_memory import ModelMemory
from chat_history import ChatHistory
from mcp_tools import (
    RouterExecutor,
    parse_router_output,
    extract_tools_from_output,
    format_tool_results,
    ToolExecutor,
)
from task_router import run_task_router, precheck as _tr_precheck, execute_categories as _tr_execute_categories
from concurrency import get_executor as _get_pool
from tool_compiler import boot_check as _tool_compiler_boot
from settings_ui import open_settings, open_settings_async

# ─── Memory System (new architecture) ────────────────────────────────────────
try:
    from conversation_state import analyze as _cs_analyze, load_state as _cs_load, ConversationState
    from context_composer   import compose as _cx_compose, compose_summary_line as _cx_summary_line
    import working_memory   as _wm
    import reflection_engine as _reflect
    _MEMORY_SYSTEM_AVAILABLE = True
except ImportError:
    _MEMORY_SYSTEM_AVAILABLE = False

# ─── Client (local LM Studio) — legacy, masih dipakai beberapa helper ────────
client = OpenAI(base_url=LM_STUDIO_URL, api_key="lm-studio", timeout=60, max_retries=0)

# ─── Soul client — legacy alias; routing aktual via get_client("soul") ───────
_soul_client = OpenAI(base_url=SOUL_API_URL, api_key="local", timeout=60, max_retries=0)

# ─── Active character ─────────────────────────────────────────────────────────
CHARACTER:         Dict = {}
_ACTIVE_CHAR_NAME: str  = "default"
_ACTIVE_CHAR_DIR:  Optional[str] = None   # PATCH: folder karakter aktif (characters/<nama>/)
_model_mem:        Optional[ModelMemory]       = None
_chat_hist:        Optional[ChatHistory]       = None
_user_mgr:         Optional[UserMemoryManager] = None
_last_assistant_response: str = ""

# ─── Admin user ID — di-load dari character.json saat set_character() ────────
ADMIN_USER_ID: str = "shinri_shoaku"


# ═══════════════════════════════════════════════════════════════════════════════
# PROMPT LOADER — baca dari CHARACTER["prompts"]
# ═══════════════════════════════════════════════════════════════════════════════

def _get_prompt(key: str) -> str:
    """
    Ambil prompt dari CHARACTER["prompts"][key].
    Placeholder {admin_user_id} di-replace otomatis dari CHARACTER["admin_user_id"].
    Raise RuntimeError jika karakter belum di-load atau key tidak ditemukan.
    """
    prompts = CHARACTER.get("prompts", {})
    if not prompts:
        raise RuntimeError(
            f"[PROMPT] CHARACTER belum di-load atau field 'prompts' tidak ada. "
            f"Pastikan set_character() dipanggil sebelum generate."
        )
    text = prompts.get(key)
    if text is None:
        raise RuntimeError(
            f"[PROMPT] Key '{key}' tidak ditemukan di character.json → prompts. "
            f"Keys tersedia: {list(prompts.keys())}"
        )
    # Replace placeholder admin_user_id
    admin_id = CHARACTER.get("admin_user_id", ADMIN_USER_ID)
    return text.replace("{admin_user_id}", admin_id)


def _get_trans_examples() -> List[Dict]:
    """
    Ambil contoh few-shot translate ID->JP dari CHARACTER["trans_few_shot"].
    Dipakai sebagai turn user/assistant ASLI di messages (lihat _trans_single),
    BUKAN ditulis ulang sebagai teks panjang di system prompt — supaya model
    kecil membaca tiap contoh sebagai 1 percakapan singkat, bukan 1 blok besar.
    """
    return CHARACTER.get("trans_few_shot", [])

# ═══════════════════════════════════════════════════════════════════════════════
# CHARACTER SETUP
# ═══════════════════════════════════════════════════════════════════════════════

def set_character(name: str, data: Dict, storage_dir: str = None, char_dir: str = None):
    """
    char_dir: folder karakter (mis. characters/liana/), biasanya dari
    CharacterManager.char_dir. PATCH: disimpan ke _ACTIVE_CHAR_DIR supaya
    context_composer.compose() bisa meneruskannya ke character_memory.load()
    dan membaca file bin yang BENAR (per-karakter), bukan fallback global
    state/character_memory.bin yang selalu kosong.
    """
    global CHARACTER, _ACTIVE_CHAR_NAME, _ACTIVE_CHAR_DIR, _model_mem, _chat_hist, \
           _last_assistant_response, ADMIN_USER_ID
    dir_ = storage_dir or MODEL_MEMORY_DIR
    CHARACTER                = data
    _ACTIVE_CHAR_NAME        = name
    _ACTIVE_CHAR_DIR         = char_dir
    _model_mem               = ModelMemory(name, storage_dir=dir_)
    _chat_hist               = ChatHistory(name, storage_dir=dir_)
    _last_assistant_response = ""
    # Sync ADMIN_USER_ID dari karakter
    ADMIN_USER_ID = data.get("admin_user_id", ADMIN_USER_ID)
    if DEBUG:
        missing = [k for k in ("task_system", "cmd_interpret_system", "identity_context_system",
                                "soul_final_system", "anim_system", "trans_system")
                   if k not in data.get("prompts", {})]
        if missing:
            print(f"[MAIN] ⚠️ Prompt tidak lengkap di character.json: {missing}")
        print(f"[MAIN] Character={name} | admin={ADMIN_USER_ID} | model_mem={_model_mem.filepath} | char_dir={char_dir or '(none — character_self akan kosong!)'}")


def get_user_manager() -> UserMemoryManager:
    global _user_mgr
    if _user_mgr is None:
        _user_mgr = UserMemoryManager(storage_dir=MEMORY_DIR)
    return _user_mgr


def load_character(path: str) -> Dict:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


# ═══════════════════════════════════════════════════════════════════════════════
# DEBUG LOGGER
# ═══════════════════════════════════════════════════════════════════════════════

class DebugLogger:
    LOG_FILE = "debug_gen.log"
    _SEP     = "═" * 68

    def __init__(self, enabled: bool = True, log_file: str = None):
        self.enabled  = enabled
        self.log_file = log_file or self.LOG_FILE

    def _w(self, text: str):
        if not self.enabled:
            return
        print(text)
        try:
            with open(self.log_file, "a", encoding="utf-8") as f:
                f.write(text + "\n")
        except Exception:
            pass

    def section(self, title: str):
        ts = datetime.now().strftime("%H:%M:%S")
        self._w(f"\n{self._SEP}")
        self._w(f"  {title}  [{ts}]")
        self._w(self._SEP)

    def _hdr(self, label: str):
        pad = max(0, 60 - len(label))
        self._w(f"\n── {label} {'─' * pad}")

    def log_system(self, sys_txt: str):
        self._hdr("SYSTEM PROMPT")
        for ln in sys_txt.splitlines():
            self._w(f"  {ln}")

    def log_history(self, msgs: List[Dict]):
        self._hdr(f"CHAT HISTORY  ({len(msgs)} entries)")
        if not msgs:
            self._w("  (kosong)")
            return
        for m in msgs:
            role    = m.get("role", "?")
            content = (m.get("content") or "")[:120]
            label   = "[user]      " if role == "user" else "[assistant] "
            suffix  = "…" if len(m.get("content", "")) > 120 else ""
            self._w(f"  {label}{content}{suffix}")

    def log_ctx(self, ctx: str):
        self._hdr("CTX / INPUT")
        for ln in ctx.splitlines():
            self._w(f"  {ln}")

    def log_raw_response(self, label: str, raw: str):
        self._hdr(f"RAW → {label}")
        preview = raw[:600] + ("…" if len(raw) > 600 else "")
        for ln in preview.splitlines():
            self._w(f"  {ln}")

    def log_parsed(self, responses: List[Dict], points: int):
        self._hdr("FINAL SEGMENTS")
        for i, r in enumerate(responses, 1):
            self._w(f"  seg{i} ({r.get('anim','?')}): {r.get('ind','')}")
            self._w(f"       JP: {r.get('jp','')[:60]}")
        self._w(f"  points: {points:+d}")

    def log_timing(self, elapsed: float):
        self._hdr("TIMING")
        self._w(f"  elapsed: {elapsed:.2f}s")

    def log_error(self, label: str, exc: Exception):
        self._hdr(f"ERROR — {label}")
        self._w(f"  {type(exc).__name__}: {exc}")

    def log_step(self, step: int, label: str, detail: str = ""):
        icons = ["", "🔍", "⚙️ ", "🧠", "🎭", "🌐", "🔀", "✔️ ", "🛡️ "]
        icon = icons[step] if step < len(icons) else "•"
        self._w(f"\n  {icon} STEP {step}: {label}" + (f" — {detail}" if detail else ""))

    def log_router_summary(
        self,
        categories: list,
        cat: str,
        attempt: int,
        fname: str,
        accepted: bool,
        args: dict = None,
    ):
        """Log ringkas satu siklus Pass B+C."""
        status  = "✅ OK" if accepted else "❌ REJECT"
        arg_str = f" args={args}" if args else ""
        self._w(
            f"  [ROUTER] cats={categories} | [{cat}] attempt={attempt} | "
            f"f={fname}{arg_str} | {status}"
        )

    def log_exec(self, fname: str, args: dict, result: str, ok: bool):
        """Log satu tool execution."""
        status  = "✅" if ok else "⚠️ "
        preview = result[:100] + ("…" if len(result) > 100 else "")
        self._w(f"  [EXEC] {status} {fname}({args}) → {preview}")

    def log_pipeline_state(self, step_name: str, data: str):
        """Log ringkasan state antar pass."""
        self._w(f"\n  ┌─ {step_name}")
        for ln in data.splitlines():
            self._w(f"  │  {ln}")
        self._w("  └─")

    def tail_log(self, n: int = 60) -> str:
        if not os.path.exists(self.log_file):
            return "(log file belum ada)"
        with open(self.log_file, "r", encoding="utf-8") as f:
            lines = f.readlines()
        return "".join(lines[-n:])

    def line(self, text: str):
        self._w(text)


_dbg = DebugLogger(enabled=DEBUG)


# ═══════════════════════════════════════════════════════════════════════════════
# JSON UTILITIES
# ═══════════════════════════════════════════════════════════════════════════════

def safe_parse(text: str) -> Optional[Dict]:
    """Parse JSON object dari teks, toleran terhadap markdown fence."""
    if not text:
        return None
    stripped = re.sub(r"```(?:json)?\s*", "", text, flags=re.IGNORECASE)
    stripped = re.sub(r"```", "", stripped).strip()
    try:
        return json.loads(stripped)
    except Exception:
        pass
    start = stripped.find("{")
    if start == -1:
        return None
    depth = 0; in_str = False; escape = False
    for i, ch in enumerate(stripped[start:], start):
        if escape: escape = False; continue
        if ch == "\\" and in_str: escape = True; continue
        if ch == '"': in_str = not in_str; continue
        if in_str: continue
        if ch == "{": depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                try: return json.loads(stripped[start:i + 1])
                except Exception: break
    return None


def safe_parse_array(text: str) -> Optional[List]:
    """Parse JSON array dari teks, toleran terhadap markdown fence."""
    if not text:
        return None
    stripped = re.sub(r"```(?:json)?\s*", "", text, flags=re.IGNORECASE)
    stripped = re.sub(r"```", "", stripped).strip()
    try:
        data = json.loads(stripped)
        if isinstance(data, list):
            return data
    except Exception:
        pass
    start = stripped.find("[")
    if start == -1:
        return None
    depth = 0; in_str = False; escape = False
    for i, ch in enumerate(stripped[start:], start):
        if escape: escape = False; continue
        if ch == "\\" and in_str: escape = True; continue
        if ch == '"': in_str = not in_str; continue
        if in_str: continue
        if ch == "[": depth += 1
        elif ch == "]":
            depth -= 1
            if depth == 0:
                try:
                    data = json.loads(stripped[start:i + 1])
                    if isinstance(data, list):
                        return data
                except Exception:
                    break
    return None


def clean_jp_for_tts(text: str) -> str:
    text = re.sub(r"[\(\（][^\)\）]*[\)\）]", "", text)
    text = re.sub(r"[\[【][^\]】]*[\]】]",   "", text)
    text = re.sub(r"[a-zA-Z]+", "", text)
    text = re.sub(r"\s+", "", text).strip()
    return text or "えっと…"


# ═══════════════════════════════════════════════════════════════════════════════
# LLM CALL — FLATTEN (untuk LM Studio compatibility)
# ═══════════════════════════════════════════════════════════════════════════════

def _flatten_messages(messages: List[Dict]) -> List[Dict]:
    """Flatten multi-turn ke single user message untuk LM Studio + Qwen."""
    if len(messages) <= 1:
        return messages

    system_parts  = []
    conv_msgs     = []
    last_user_msg = None

    for i, m in enumerate(messages):
        role    = m.get("role", "")
        content = str(m.get("content", ""))
        is_last = (i == len(messages) - 1)

        if role == "system":
            system_parts.append(content)
        elif is_last and role == "user":
            last_user_msg = content
        else:
            conv_msgs.append({"role": role, "content": content})

    if len(conv_msgs) >= 2:
        last_exchange = conv_msgs[-2:]
        old_history   = conv_msgs[:-2]
    elif conv_msgs:
        last_exchange = conv_msgs[-1:]
        old_history   = []
    else:
        last_exchange = []
        old_history   = []

    parts = []
    if system_parts:
        parts.append("\n\n".join(system_parts))

    if old_history:
        old_lines = []
        for m in old_history:
            content = m["content"]
            preview = content[:180] + ("…" if len(content) > 180 else "")
            tag = "[USER]" if m["role"] == "user" else "[KARAKTER]"
            old_lines.append(f"{tag}: {preview}")
        parts.append("<RIWAYAT_LAMA>\n" + "\n".join(old_lines) + "\n</RIWAYAT_LAMA>")

    if last_exchange:
        last_lines = []
        for m in last_exchange:
            tag = "[USER]" if m["role"] == "user" else "[KARAKTER]"
            last_lines.append(f"{tag}: {m['content']}")
        parts.append(
            "<EXCHANGE_TERAKHIR>\n"
            + "\n".join(last_lines)
            + "\n</EXCHANGE_TERAKHIR>"
        )

    if last_user_msg:
        parts.append(f"<TUGAS_SEKARANG>\n{last_user_msg}\n</TUGAS_SEKARANG>")

    flat_content = "\n\n".join(parts)
    return [{"role": "user", "content": flat_content}]


def _llm_call(pass_name: str = "react", **kwargs):
    """
    Wrapper LLM call dengan routing per-pass.
    `pass_name` harus sesuai key di CALL_ROUTING (config.py).
    Client dan model dipilih otomatis berdasarkan config.
    Jika `model` tidak di-pass eksplisit, diambil dari get_model(pass_name).
    """
    messages  = kwargs.get("messages", [])
    flat_msgs = _flatten_messages(messages)
    clean_kwargs             = {k: v for k, v in kwargs.items() if k not in ("messages", "model")}
    clean_kwargs["messages"] = flat_msgs
    # Model: pakai yang di-pass eksplisit hanya jika caller memang set, else dari config
    clean_kwargs["model"]    = kwargs.get("model") or get_model(pass_name)
    routed_client = get_client(pass_name)
    # Merge extra_body dari config (mis. enable_thinking=False untuk endpoint tertentu)
    # tanpa menimpa extra_body yang sudah di-pass eksplisit oleh caller.
    cfg_extra_body = get_extra_body(pass_name)
    if cfg_extra_body:
        clean_kwargs["extra_body"] = {**cfg_extra_body, **kwargs.get("extra_body", {})}
    if DEBUG:
        from config import CALL_ROUTING, ENDPOINTS
        ep_key = CALL_ROUTING.get(pass_name, "?")
        ep_url = ENDPOINTS.get(ep_key, {}).get("url", "?")
        print(f"  [LLM_CALL] pass={pass_name} → endpoint={ep_key} ({ep_url}) model={clean_kwargs['model']} extra_body={clean_kwargs.get('extra_body', {})}")
    return routed_client.chat.completions.create(**clean_kwargs)


# ═══════════════════════════════════════════════════════════════════════════════
# HISTORY HELPERS
# ═══════════════════════════════════════════════════════════════════════════════

def _reorder_history_for_user(
    messages:         List[Dict],
    current_username: str,
) -> List[Dict]:
    """Pindah exchange user saat ini ke posisi paling akhir history."""
    if len(messages) < 2:
        return messages

    prefix = f"[{current_username}]:"
    last_user_idx = -1
    for i in range(len(messages) - 1, -1, -1):
        m = messages[i]
        if m.get("role") != "user":
            continue
        content = (m.get("content") or "").strip()
        if content.lower().startswith(prefix.lower()):
            last_user_idx = i
            break

    if last_user_idx == -1:
        return messages

    pair: List[Dict] = [messages[last_user_idx]]
    next_idx = last_user_idx + 1
    if next_idx < len(messages) and messages[next_idx].get("role") == "assistant":
        pair.append(messages[next_idx])
        end_idx = next_idx + 1
    else:
        end_idx = last_user_idx + 1

    remaining = messages[:last_user_idx] + messages[end_idx:]
    return remaining + pair


def _deduplicate_history(chat_hist: "ChatHistory"):
    try:
        msgs = chat_hist.get_messages()
        if len(msgs) < 3:
            return
        assistant_entries = [(i, m) for i, m in enumerate(msgs) if m.get("role") == "assistant"]
        if len(assistant_entries) < 2:
            return
        fp_last   = (assistant_entries[-1][1].get("content") or "")[:100]
        fp_second = (assistant_entries[-2][1].get("content") or "")[:100]
        if fp_last and fp_last == fp_second:
            _dbg.line("  [HISTORY] Duplikat terdeteksi, hapus entry terakhir")
            for attr in ("_messages", "messages"):
                if hasattr(chat_hist, attr):
                    lst = getattr(chat_hist, attr)
                    for i in range(len(lst) - 1, -1, -1):
                        if (lst[i].get("role") == "assistant"
                                and (lst[i].get("content") or "")[:100] == fp_last):
                            lst.pop(i)
                            break
                    chat_hist.save()
                    break
    except Exception as e:
        _dbg.line(f"  [HISTORY] Dedup error (non-fatal): {e}")


# ═══════════════════════════════════════════════════════════════════════════════
# STYLE COMMAND DETECTION
# ═══════════════════════════════════════════════════════════════════════════════

_STYLE_CMD_PATTERNS = [
    r"(?:coba\s+)?tambah(?:kan)?\s+akhiran\s+.+",
    r"(?:coba\s+)?pakai(?:kan)?\s+(?:kata|gaya|akhiran)\s+.+",
    r"(?:coba\s+)?gunakan\s+(?:kata|akhiran|gaya)\s+.+",
    r"setiap\s+(?:akhir\s+)?kalimat\s+(?:diakhiri|pakai|tambah)\s+.+",
    r"(?:coba\s+)?bicara(?:lah)?\s+(?:dengan\s+gaya|menggunakan)\s+.+",
    r"(?:coba\s+)?selalu\s+(?:tambah|pakai|gunakan)\s+.+",
    r"jadi\s+\w+",   # "jadi maid", "jadi yandere", "jadi onee-chan"
]
_STYLE_RESET_KEYWORDS = [
    "stop", "berhenti", "normal lagi", "biasa lagi", "reset gaya",
    "hapus akhiran", "hapus gaya", "kembali normal",
]


def _detect_style_command(user_input: str) -> Optional[str]:
    lower = user_input.lower().strip()
    if any(kw in lower for kw in _STYLE_RESET_KEYWORDS):
        return ""
    trigger_words = [
        "tambahkan", "tambah", "pakai", "gunakan", "akhiran",
        "gaya", "setiap kalimat", "setiap akhir", "coba", "selalu",
        "jadi maid", "jadi yandere", "jadi onee", "jadi tsundere",
    ]
    if not any(w in lower for w in trigger_words):
        return None
    for pattern in _STYLE_CMD_PATTERNS:
        if re.search(pattern, lower):
            return user_input.strip()
    if any(w in lower for w in ["akhiran", "setiap kalimat", "setiap akhir"]):
        return user_input.strip()
    return None


# ═══════════════════════════════════════════════════════════════════════════════
# STEP 1 — TASK ROUTER
# ═══════════════════════════════════════════════════════════════════════════════

def _task_router_pass(
    user_id:    str,
    username:   str,
    user_input: str,
    stall_callback=None,
) -> List[Dict]:
    """
    3-Pass Task Router (via task_router.py):
      Pass A → broad category (comma-separated, multi-select)
      Pass B → tool specific per kategori (prompt mini terfokus)
      Pass C → yes/no validation, retry max 2x per kategori jika reject

    Custom MCP tools otomatis diregistrasikan berdasarkan field "category"
    di mcp_custom_tools.json (Settings UI).

    stall_callback: PATCH v2 — diteruskan apa adanya ke run_task_router().
        Dipanggil sebagai stall_callback(user_input) begitu router tahu
        BERAPA BANYAK kategori/task yang bakal dieksplor sudah lewat
        threshold (lihat _STALL_MIN_CATEGORIES di task_router.py) — SEBELUM
        Pass B/C+EXEC yang makan waktu.
    """
    calls = run_task_router(
        user_id        = user_id,
        username       = username,
        user_input     = user_input,
        char_prompts   = CHARACTER.get("prompts", {}),
        llm_call       = _llm_call,
        dbg            = _dbg,
        stall_callback = stall_callback,
    )
    _dbg.log_step(1, "TASK done", f"{len(calls)} calls: {[c.get('f') for c in calls]}")
    return calls


def _set_pending_action_wm(user_id: str, tool: str, args: dict, description: str = "", char_id: str = None):
    """
    Simpan pending_action ke working memory + conversation_state.
    Dipanggil saat Soul mau tanya konfirmasi ke user sebelum execute tool.
    Turn berikutnya, CONFIRMATION handler di full_generate akan execute-nya.

    char_id: PATCH — WAJIB scoped ke karakter aktif (lihat working_memory.py),
        default ke _ACTIVE_CHAR_NAME kalau tidak diisi eksplisit.

    Contoh:
        _set_pending_action_wm(uid, "cs_write",
            {"ns":"user:local","key":"hobi","value":"kopi"},
            "simpan: user suka kopi")
    """
    if not _MEMORY_SYSTEM_AVAILABLE:
        return
    char_id = char_id or CHARACTER.get("id") or _ACTIVE_CHAR_NAME or "default"
    try:
        from conversation_state import set_pending_action as _spa
        _spa(user_id, tool, args, description)
        wm = _wm.load(user_id, char_id)
        wm.set("__pending_tool__", tool)
        wm.set("__pending_args__", args)
        wm.awaiting_reply = description or f"konfirmasi: {tool}"
        _wm.save(wm)
    except Exception as e:
        _dbg.log_error("_set_pending_action_wm", e)



# ═══════════════════════════════════════════════════════════════════════════════
# AGENT PIPELINE — Pass 1–6 + 8 (pendek, fokus, anti-halusinasi)
# ═══════════════════════════════════════════════════════════════════════════════

# ── Pass 1: Category Classify (3 kelompok besar) ────────────────────────────

def _history_snippet(history: List[Dict], username: str, char_name: str = "Alfa", n: int = 3) -> str:
    """
    Format N giliran terakhir jadi teks ringkas untuk dikirim ke tahap routing
    (category/subintent/tool-select), supaya referensi seperti 'itu', 'yang tadi',
    'simpan ke memory' (tanpa isi baru) bisa di-resolve dari history, bukan
    cuma dari user_input current yang berdiri sendiri.
    """
    if not history:
        return "(tidak ada history)"
    lines = []
    for m in history[-n:]:
        role    = m.get("role", "")
        content = (m.get("content") or "").strip().replace(" | ", " ")
        label   = char_name if role == "assistant" else username
        lines.append(f"{label}: {content[:160]}")
    return "\n".join(lines) if lines else "(tidak ada history)"



def _category_classify(user_id: str, username: str, user_input: str, history_text: str = "") -> str:
    """
    Pass 1: klasifikasi kategori besar saja — data_user / stream_state / command / none.
    Prompt pendek, fokus, murah. Default "none" jika gagal parse.
    history_text disertakan supaya referensi ke chat sebelumnya (mis. 'simpan ke
    memory' tanpa isi baru, merujuk ke pesan sebelumnya) bisa dikenali.
    """
    prompt = f"[HISTORY]\n{history_text or '(tidak ada history)'}\n\n[Chat]\n{user_id}:{username}: {user_input}"
    _dbg.log_step(1, "CATEGORY CLASSIFY", f"input={user_input[:60]}")
    try:
        resp = _llm_call(
            "category",
            messages=[
                {"role": "system", "content": _get_prompt("category_system")},
                {"role": "user",   "content": prompt},
            ],
            temperature=0.0,
            max_tokens=30,
        )
        raw = (resp.choices[0].message.content or "").strip()
        _dbg.line(f"  [CATEGORY RAW] {raw}")
        data = safe_parse(raw)
        if data and "cat" in data:
            cat = str(data["cat"])
            _dbg.line(f"  [CATEGORY] cat={cat}")
            return cat
    except Exception as e:
        _dbg.log_error("_category_classify", e)
    return "none"


# ── Pass 2: Sub-Intent Classify (hanya jalan untuk data_user / stream_state) ─

def _subintent_data_classify(user_id: str, username: str, user_input: str, history_text: str = "") -> str:
    """Tentukan read vs write untuk kategori data_user. Default 'read' jika gagal/ragu."""
    prompt = f"[HISTORY]\n{history_text or '(tidak ada history)'}\n\n[Chat]\n{user_id}:{username}: {user_input}"
    _dbg.log_step(2, "SUBINTENT DATA", f"input={user_input[:60]}")
    try:
        resp = _llm_call(
            "subintent_data",
            messages=[
                {"role": "system", "content": _get_prompt("subintent_data_system")},
                {"role": "user",   "content": prompt},
            ],
            temperature=0.0,
            max_tokens=30,
        )
        raw = (resp.choices[0].message.content or "").strip()
        _dbg.line(f"  [SUBINTENT DATA RAW] {raw}")
        data = safe_parse(raw)
        if data and "action" in data:
            action = str(data["action"])
            _dbg.line(f"  [SUBINTENT DATA] action={action}")
            return action
    except Exception as e:
        _dbg.log_error("_subintent_data_classify", e)
    return "read"


def _subintent_stream_classify(user_id: str, username: str, user_input: str) -> str:
    """Tentukan topic spesifik untuk kategori stream_state. Default 'info' jika gagal."""
    prompt = f"{user_id}:{username}: {user_input}"
    _dbg.log_step(2, "SUBINTENT STREAM", f"input={user_input[:60]}")
    try:
        resp = _llm_call(
            "subintent_stream",
            messages=[
                {"role": "system", "content": _get_prompt("subintent_stream_system")},
                {"role": "user",   "content": prompt},
            ],
            temperature=0.0,
            max_tokens=30,
        )
        raw = (resp.choices[0].message.content or "").strip()
        _dbg.line(f"  [SUBINTENT STREAM RAW] {raw}")
        data = safe_parse(raw)
        if data and "topic" in data:
            topic = str(data["topic"])
            _dbg.line(f"  [SUBINTENT STREAM] topic={topic}")
            return topic
    except Exception as e:
        _dbg.log_error("_subintent_stream_classify", e)
    return "info"


# ── Pass 3: Tool Selector (prompt mini per kategori/subintent) ─────────────

# Topic stream_state → tool langsung (tidak perlu LLM ke-3, sudah pasti 1:1)
_STREAM_TOPIC_TOOL: Dict[str, str] = {
    "viewer":   "gvc",
    "info":     "gsi",
    "history":  "grc",
    "activity": "gca",
    "time":     "gtc",
}

def _tool_select_generic(
    user_id: str, username: str, user_input: str,
    prompt_key: str, log_label: str, history_text: str = "",
) -> Optional[Dict]:
    """
    Helper: jalankan satu LLM call tool-select dengan prompt mini tertentu.
    PENTING: model HANYA diminta pilih nama fungsi (dan argumen non-id seperti
    'm' untuk uum, atau 'target'/'name' untuk sn). Parameter 'id' TIDAK PERNAH
    diminta dari model — model kecil sering halusinasi/salah-tafsir saat
    diminta menyalin balik user_id dari teks. 'id' selalu disuntik di Python
    dari variabel asli setelah parsing, sehingga selalu benar.
    history_text disertakan supaya untuk kasus tool_select_data_write, model bisa
    menyusun isi 'm' dari INFO YANG SEBENARNYA DISEBUT di chat sebelumnya, bukan
    cuma dari kalimat current ('simpan ke memory' saja tanpa isi -> harus ambil
    isi dari HISTORY, bukan ngarang/null).
    """
    prompt = (
        f"[HISTORY]\n{history_text or '(tidak ada history)'}\n\n"
        f"username: {username}\nchat: {user_input}"
    )
    _dbg.log_step(3, log_label, f"input={user_input[:60]}")
    try:
        resp = _llm_call(
            "tool_select",
            messages=[
                {"role": "system", "content": _get_prompt(prompt_key)},
                {"role": "user",   "content": prompt},
            ],
            temperature=0.0,
            max_tokens=80,
        )
        raw = (resp.choices[0].message.content or "").strip()
        _dbg.line(f"  [{log_label} RAW] {raw}")
        if raw.strip().lower() == "null":
            return None
        data = safe_parse(raw)
        if not data or "f" not in data:
            return None

        fname = str(data["f"]).strip()
        args  = dict(data.get("a", {}) or {})

        # Guard: nama fungsi harus salah satu kode 2-4 huruf yang valid,
        # bukan nama orang / teks bebas (mis. halusinasi "Shinri").
        _VALID_FUNCS = {
            "gum", "gus", "gugh", "uum", "bu",
            "gvc", "gsi", "gca", "grc", "gtc",
            "sn", "cmd", "rs",
        }
        if fname not in _VALID_FUNCS:
            _dbg.line(f"  [{log_label}] REJECTED — fname tidak valid: {fname!r}")
            return None

        # Suntik 'id' di Python — JANGAN PERNAH pakai nilai 'id' dari model.
        if fname in ("gum", "gus", "gugh", "uum", "bu"):
            args["id"] = user_id

        _dbg.line(f"  [{log_label}] f={fname} a={args}")
        return {"f": fname, "a": args}
    except Exception as e:
        _dbg.log_error(log_label, e)
    return None


def _route_and_select_tool(
    user_id: str, username: str, user_input: str, cat: str, history_text: str = "",
) -> Optional[Dict]:
    """
    Pass 2+3 gabungan: berdasarkan kategori dari Pass 1, jalankan sub-intent
    (jika perlu) lalu pilih tool dengan prompt mini yang sudah dipersempit.
    """
    if cat == "data_user":
        action = _subintent_data_classify(user_id, username, user_input, history_text)
        if action == "write":
            return _tool_select_generic(
                user_id, username, user_input,
                "tool_select_data_write", "TOOL SEL (data/write)", history_text,
            )
        return _tool_select_generic(
            user_id, username, user_input,
            "tool_select_data_read", "TOOL SEL (data/read)", history_text,
        )

    if cat == "stream_state":
        topic = _subintent_stream_classify(user_id, username, user_input)
        fname = _STREAM_TOPIC_TOOL.get(topic)
        if not fname:
            return None
        args = {"n": 5} if fname == "grc" else {}
        _dbg.line(f"  [TOOL SEL (stream)] topic={topic} → f={fname} a={args}")
        return {"f": fname, "a": args}

    if cat == "command":
        return _tool_select_generic(
            user_id, username, user_input,
            "tool_select_command_system", "TOOL SEL (command)", history_text,
        )

    return None


# ── Pass 3: Execute + Validate (Python only, no LLM) ────────────────────────

# Fallback tool jika hasil kosong — coba tool alternatif sekali
_TOOL_FALLBACK: Dict[str, str] = {
    "gum": "gus",   # memory gagal → coba stats
    "gus": "gum",   # stats gagal → coba memory
    "gvc": "gsi",   # viewer count gagal → coba stream info
    "gca": "gsi",
}

def _execute_with_retry(
    executor: "RouterExecutor",
    call:     Dict,
) -> str:
    """
    Pass 3: eksekusi satu tool call, retry dengan fallback jika kosong/error.
    Returns string hasil atau "" jika gagal semua.
    """
    fname = call.get("f", "")
    args  = call.get("a", {}) or {}
    _dbg.log_step(3, "EXECUTE TOOL", f"f={fname} a={args}")

    # Percobaan pertama
    try:
        result = executor._dispatch(fname, args)
        if result and result.strip() and result not in ("N/A", "no update"):
            _dbg.line(f"  [EXEC OK] {fname}: {str(result)[:80]}")
            return f"{fname}: {result}"
    except Exception as e:
        _dbg.log_error(f"_execute {fname}", e)

    # Fallback ke tool alternatif
    fallback = _TOOL_FALLBACK.get(fname)
    if fallback:
        _dbg.line(f"  [EXEC RETRY] {fname} gagal → coba {fallback}")
        try:
            result = executor._dispatch(fallback, args)
            if result and result.strip() and result not in ("N/A", "no update"):
                _dbg.line(f"  [EXEC FALLBACK OK] {fallback}: {str(result)[:80]}")
                return f"{fallback}: {result}"
        except Exception as e:
            _dbg.log_error(f"_execute_fallback {fallback}", e)

    _dbg.line(f"  [EXEC FAIL] {fname} dan fallback gagal semua")
    return ""


# ── Pass 4: Data Summarizer ────────────────────────────────────────────────

def _data_summarizer(raw_data: str, user_input: str) -> str:
    """
    Pass 4: ringkas data mentah dari tool menjadi 1-2 kalimat relevan.
    Ini mencegah soul menerima dump data panjang yang bikin halusinasi.
    """
    if not raw_data or not raw_data.strip():
        return ""
    _dbg.log_step(4, "DATA SUMMARIZER", f"len={len(raw_data)}")
    _dbg.line(f"  [SUMMARIZER IN] {raw_data[:200]}")
    prompt = f"Pertanyaan user: {user_input}\nData: {raw_data}"
    try:
        resp = _llm_call(
            "data_summary",
            messages=[
                {"role": "system", "content": _get_prompt("data_summary_system")},
                {"role": "user",   "content": prompt},
            ],
            temperature=0.1,
            max_tokens=120,
        )
        summary = (resp.choices[0].message.content or "").strip()
        _dbg.line(f"  [DATA SUMMARY] {summary[:80]}")
        # Marker relevansi: kalau model bilang data tidak nyambung dgn
        # pertanyaan, treat sebagai kosong — JANGAN dipaksa jadi must_use_data
        # di Pass berikutnya (itu sumber halusinasi "jawaban ngawur tapi pede").
        if re.sub(r"[().\s]", "", summary).lower() in ("tidakrelevan", "notrelevant", "irrelevant"):
            _dbg.line("  [DATA SUMMARY] ditandai tidak relevan → dikosongkan")
            return ""
        return summary or raw_data[:200]
    except Exception as e:
        _dbg.log_error("_data_summarizer", e)
        return raw_data[:200]


# ── Pass 5: Soul Think ───────────────────────────────────────────────────────

def _soul_think(
    user_input:   str,
    username:     str,
    data_summary: str,
    history_tail: List[Dict],
) -> Dict:
    """
    Pass 5: grounding — model menentukan intent, tone, dan key_point sebelum generate.
    Output: {"intent": str, "tone": str, "key_point": str}
    """
    hist_lines = []
    for m in history_tail[-2:]:  # hanya 2 terakhir untuk grounding
        role = m.get("role", "")
        content = (m.get("content") or "")[:120]
        hist_lines.append(f"{'user' if role=='user' else 'alfa'}: {content}")

    hist_text = "\n".join(hist_lines) if hist_lines else "(baru mulai)"
    ctx = (
        f"history terbaru:\n{hist_text}\n"
        f"data konteks: {data_summary or 'tidak ada'}\n"
        f"chat terbaru: {username}: {user_input}\n"
        f"PENTING: key_point harus respons BARU, jangan ulangi kalimat dari history."
    )
    _dbg.log_step(5, "SOUL THINK", f"input={user_input[:50]}")
    try:
        resp = _llm_call(
            "soul_think",
            messages=[
                {"role": "system", "content": _get_prompt("soul_think_system")},
                {"role": "user",   "content": ctx},
            ],
            temperature=0.1,
            max_tokens=80,
        )
        raw = (resp.choices[0].message.content or "").strip()
        _dbg.line(f"  [THINK RAW] {raw}")
        data = safe_parse(raw)
        if data and "intent" in data:
            result = {
                "intent":    str(data.get("intent", "")),
                "tone":      str(data.get("tone", "warm")),
                "key_point": str(data.get("key_point", "")),
            }
            _dbg.line(f"  [THINK] intent={result['intent']} tone={result['tone']}")
            return result
    except Exception as e:
        _dbg.log_error("_soul_think", e)
    return {"intent": user_input[:40], "tone": "warm", "key_point": ""}


# ── Pass 6: Soul Persona Lock ────────────────────────────────────────────────

def _soul_persona_lock(tone: str, cmd: str) -> Dict:
    """
    Pass 6: kunci gaya bicara sebelum generate — cegah OOC dan drift karakter.
    Output: {"style": str, "stammer": bool, "forbidden": str}
    """
    _dbg.log_step(6, "PERSONA LOCK", f"tone={tone} cmd={cmd or 'none'}")
    prompt = f"tone: {tone}\ncmd aktif: {cmd or 'none'}"
    try:
        resp = _llm_call(
            "soul_persona",
            messages=[
                {"role": "system", "content": _get_prompt("soul_persona_system")},
                {"role": "user",   "content": prompt},
            ],
            temperature=0.0,
            max_tokens=60,
        )
        raw = (resp.choices[0].message.content or "").strip()
        _dbg.line(f"  [PERSONA RAW] {raw}")
        data = safe_parse(raw)
        if data and "style" in data:
            result = {
                "style":     str(data.get("style", "malu-malu natural")),
                "stammer":   bool(data.get("stammer", True)),
                "forbidden": str(data.get("forbidden", "asterisk action, markdown")),
            }
            _dbg.line(f"  [PERSONA] style={result['style']} stammer={result['stammer']}")
            return result
    except Exception as e:
        _dbg.log_error("_soul_persona_lock", e)
    return {"style": "malu-malu natural", "stammer": True, "forbidden": "asterisk action, markdown"}


# ── Pass A: CMD Interpret — resolve mm.command mentah jadi directive kecil ───

_CMD_DIR_FALLBACK = {"address": "", "style_note": "", "sticky_suffix": ""}

# PATCH: cache exact-match untuk _cmd_interpret. cmd_active (mis. "bilang
# nyann 10x", "jawab dalam bahasa indonesia") biasanya SAMA PERSIS di banyak
# turn berturut-turut — user jarang ganti gaya bicara tiap chat — dan call
# ini temperature=0.0 (deterministic), jadi aman di-cache murni by exact
# string match. Hemat 1 LLM call di HAMPIR SETIAP turn selama cmd_active
# belum berubah. Dibatasi ukurannya (LRU sederhana via OrderedDict) supaya
# tidak numpuk tanpa batas kalau ternyata banyak variasi cmd.
_CMD_INTERPRET_CACHE: "OrderedDict[str, Dict]" = OrderedDict()
_CMD_INTERPRET_CACHE_MAX = 100


def _cmd_interpret(cmd: str) -> Dict:
    """
    Pass A: terjemahkan instruksi gaya bicara mentah (mm.command) jadi
    {"address", "style_note", "sticky_suffix"}. Skip LLM call kalau cmd kosong
    ATAU kalau cmd yang sama persis sudah pernah di-interpret sebelumnya.
    """
    cmd = (cmd or "").strip()
    if not cmd:
        return dict(_CMD_DIR_FALLBACK)

    if cmd in _CMD_INTERPRET_CACHE:
        cached = _CMD_INTERPRET_CACHE[cmd]
        _CMD_INTERPRET_CACHE.move_to_end(cmd)
        _dbg.log_step(5, "CMD INTERPRET (cache hit)", f"cmd={cmd!r}")
        _dbg.line(f"  [CMD DIR] (cache) {cached}")
        return dict(cached)

    _dbg.log_step(5, "CMD INTERPRET", f"cmd={cmd!r}")
    try:
        resp = _llm_call(
            "cmd_interpret",
            messages=[
                {"role": "system", "content": _get_prompt("cmd_interpret_system")},
                {"role": "user",   "content": cmd},
            ],
            temperature=0.0,
            max_tokens=60,
        )
        raw = (resp.choices[0].message.content or "").strip()
        _dbg.line(f"  [CMD DIR RAW] {raw}")
        data = safe_parse(raw)
        if data:
            result = {
                "address":       str(data.get("address", "")),
                "style_note":    str(data.get("style_note", "")),
                "sticky_suffix": str(data.get("sticky_suffix", "")),
            }
            _dbg.line(f"  [CMD DIR] {result}")
            _CMD_INTERPRET_CACHE[cmd] = dict(result)
            if len(_CMD_INTERPRET_CACHE) > _CMD_INTERPRET_CACHE_MAX:
                _CMD_INTERPRET_CACHE.popitem(last=False)
            return result
    except Exception as e:
        _dbg.log_error("_cmd_interpret", e)
    return dict(_CMD_DIR_FALLBACK)


# ── Pass B: Identity + Context Merge — gabung Identity/Relation/CONTEXT/CMD ──

# PATCH: cache exact-match untuk _identity_context_merge, keyed by
# (username, relation, cmd_dir, data_summary). Kalau semua 4 input ini
# SAMA PERSIS dengan turn sebelumnya (sangat umum di turn tanpa tool data —
# lihat "data_summary: (none)" yang sering berulang), hasil directive-nya
# pasti sama juga (temperature=0.0) — skip LLM call. Begitu salah satu
# berubah (mis. ada data_summary baru dari tool), otomatis re-compute,
# TIDAK ada data basi yang kepakai.
_IDENTITY_MERGE_CACHE: "OrderedDict[tuple, Dict]" = OrderedDict()
_IDENTITY_MERGE_CACHE_MAX = 200


def _identity_context_merge(
    username:     str,
    relation:     str,
    cmd_dir:      Dict,
    data_summary: str,
) -> Dict:
    """
    Pass B: gabungkan Target_User, Relation, cmd_directive, dan CONTEXT data
    jadi satu directive ringkas: {"address_user_as", "persona_note", "must_use_data"}.
    Cache exact-match kalau kombinasi (username, relation, cmd_dir, data_summary)
    sudah pernah diproses sebelumnya.
    """
    cache_key = (
        username, relation,
        json.dumps(cmd_dir, sort_keys=True, ensure_ascii=False),
        data_summary or "",
    )
    if cache_key in _IDENTITY_MERGE_CACHE:
        cached = _IDENTITY_MERGE_CACHE[cache_key]
        _IDENTITY_MERGE_CACHE.move_to_end(cache_key)
        _dbg.log_step(6, "IDENTITY+CONTEXT MERGE (cache hit)", f"user={username} relation={relation}")
        _dbg.line(f"  [DIRECTIVE] (cache) {cached}")
        return dict(cached)

    payload = (
        f"Target_User: {username}\n"
        f"Relation: {relation}\n"
        f"cmd_directive: {json.dumps(cmd_dir, ensure_ascii=False)}\n"
        f"CONTEXT: {data_summary or '(tidak ada data tambahan)'}"
    )
    _dbg.log_step(6, "IDENTITY+CONTEXT MERGE", payload[:80])
    try:
        resp = _llm_call(
            "identity_merge",
            messages=[
                {"role": "system", "content": _get_prompt("identity_context_system")},
                {"role": "user",   "content": payload},
            ],
            temperature=0.0,
            max_tokens=120,
        )
        raw = (resp.choices[0].message.content or "").strip()
        _dbg.line(f"  [DIRECTIVE RAW] {raw}")
        data = safe_parse(raw)
        if data and "address_user_as" in data:
            result = {
                "address_user_as": str(data.get("address_user_as", username)) or username,
                "persona_note":    str(data.get("persona_note", "")),
                "must_use_data":   str(data.get("must_use_data", "")),
            }
            _dbg.line(f"  [DIRECTIVE] {result}")
            _IDENTITY_MERGE_CACHE[cache_key] = dict(result)
            if len(_IDENTITY_MERGE_CACHE) > _IDENTITY_MERGE_CACHE_MAX:
                _IDENTITY_MERGE_CACHE.popitem(last=False)
            return result
    except Exception as e:
        _dbg.log_error("_identity_context_merge", e)
    return {
        "address_user_as": username,
        "persona_note":    "",
        "must_use_data":   data_summary or "",
    }


# ── Pass R: Agent ReAct Loop — generic toolbox, dipakai utk replace Pass 1-3 ──
#
# Loop ini menjalankan beberapa giliran [THINK]/[ACT] dengan agent_react_system,
# tiap [ACT] dieksekusi via `tool_bus` (objek apa pun yang punya method
# lookup/resolve/mutate/admin_action/done — di produksi ini wrapper di atas
# RouterExecutor, di test ini MockToolBus). Observasi hasil eksekusi disuntik
# balik sebagai [OBS] sebelum giliran [THINK] berikutnya.

_REACT_VALID_TOOLS = {"lookup", "resolve", "mutate", "admin_action", "done"}


def _parse_react_step(raw: str) -> Tuple[str, Dict]:
    """
    Parse satu giliran output model: ambil baris [ACT] {...} dan kembalikan
    (tool_name, args). Toleran terhadap [THINK] yang nempel di depan/sekitar,
    DAN toleran terhadap JSON yang ke-truncate (kehabisan max_tokens di
    tengah) — dalam kasus itu tetap coba selamatkan nama tool + args parsial
    lewat regex longgar, daripada langsung jatuh ke "done" dan kehilangan
    seluruh keputusan model.
    Default ke ("done", {}) HANYA kalau benar-benar tidak ada nama tool yang
    bisa diselamatkan — fail-safe ke no-op, bukan fail-safe ke asumsi
    tindakan berbahaya.
    """
    m = re.search(r"\[ACT\]\s*(\{.*?\})\s*(?:\[|$)", raw, flags=re.DOTALL)
    if not m:
        m = re.search(r"\{.*\}", raw, flags=re.DOTALL)
    if m:
        data = safe_parse(m.group(0))
        if data and "tool" in data:
            tool = str(data.get("tool", "done")).strip()
            args = data.get("args", {}) or {}
            if tool in _REACT_VALID_TOOLS:
                return tool, args

    # ── Fallback: JSON kemungkinan ke-truncate (max_tokens kehabisan). Coba
    # selamatkan minimal nama tool + beberapa argumen umum via regex longgar.
    tool_m = re.search(r'"tool"\s*:\s*"(\w+)"', raw)
    if tool_m and tool_m.group(1) in _REACT_VALID_TOOLS:
        tool = tool_m.group(1)
        args: Dict = {}
        for key in ("scope", "ref", "text_ref", "value", "mode", "action", "target", "reason"):
            km = re.search(rf'"{key}"\s*:\s*"([^"]*)"', raw)
            if km:
                args[key] = km.group(1)
        _dbg.line(f"  [REACT] JSON ke-truncate, diselamatkan via regex: tool={tool} args={args}")
        return tool, args

    return "done", {}


def _react_agent_loop(
    user_input:   str,
    history_text: str,
    current_user_id: str,
    is_admin:     bool,
    tool_bus,                       # objek dgn method lookup/resolve/mutate/admin_action/done
    max_steps:    int = 4,
    active_command: str = "",
) -> List[Dict]:
    """
    Pass R: jalankan loop ReAct, kembalikan log semua tool call yang terjadi
    (dalam urutan), supaya caller (full_generate ATAU test runner) bisa
    menyusun data_summary dari situ.

    tool_bus HARUS punya method:
      .lookup(scope, ref) -> str (observation)
      .resolve(text_ref)  -> str (observation, biasanya JSON kandidat)
      .mutate(scope, ref, value, mode) -> str
      .admin_action(action, target, reason) -> str
      .done() -> str
    Semua method ini WAJIB melakukan guard-nya sendiri (cek is_admin dsb di
    level eksekusi) — prompt cuma lapisan pertama, bukan satu-satunya.

    active_command: gaya bicara aktif (mm.command mentah) — dikirim sebagai
    field [CONTEXT] terpisah, BUKAN ditaruh di [HISTORY], supaya kasus
    "udah balik normal aja" / "ganti gaya bicara" bisa dideteksi tanpa
    bergantung pada history chat yang kebetulan menyinggungnya.
    """
    calls_log: List[Dict] = []
    seen_signatures: set = set()
    transcript = (
        f"[CONTEXT]\ncurrent_user_id: {current_user_id}\nis_admin: {is_admin}\n"
        f"active_command: {active_command or '(tidak ada)'}\n\n"
        f"[HISTORY]\n{history_text or '(tidak ada history)'}\n\n"
        f"[Chat]\n{user_input}"
    )

    for step in range(max_steps):
        _dbg.log_step(0, f"REACT STEP {step+1}/{max_steps}", "")
        _dbg.log_pipeline_state("TRANSCRIPT TAIL", transcript[-300:])
        try:
            resp = _llm_call(
                "react",
                messages=[
                    {"role": "system", "content": _get_prompt("agent_react_system")},
                    {"role": "user",   "content": transcript},
                ],
                temperature=0.0,
                max_tokens=250,
            )
            raw = (resp.choices[0].message.content or "").strip()
        except Exception as e:
            _dbg.log_error("_react_agent_loop", e)
            break

        _dbg.line(f"  [REACT RAW] {raw[:150]}")
        tool, args = _parse_react_step(raw)

        if tool == "done":
            calls_log.append({"tool": "done"})
            break

        # ── Anti-loop guard (Python-level, tidak bergantung disiplin prompt) ──
        # Kalau model mengulang tool+args PERSIS SAMA yang sudah pernah dipanggil
        # di giliran ini, paksa stop — observation-nya sudah ada di transcript,
        # mengulang lagi cuma buang token tanpa progres.
        sig = (tool, json.dumps(args, sort_keys=True))
        if sig in seen_signatures:
            _dbg.line(f"  [REACT] anti-loop guard: '{tool}' args sama diulang → force stop")
            calls_log.append({"tool": "done", "note": "anti_loop_forced"})
            break
        seen_signatures.add(sig)

        try:
            if tool == "lookup":
                scope = str(args.get("scope", ""))
                ref   = str(args.get("ref", ""))
                obs   = tool_bus.lookup(scope, ref)
                calls_log.append({"tool": "lookup", "scope": scope, "ref": ref, "obs": obs})
            elif tool == "resolve":
                text_ref = str(args.get("text_ref", ""))
                obs      = tool_bus.resolve(text_ref)
                calls_log.append({"tool": "resolve", "text_ref": text_ref, "obs": obs})
            elif tool == "mutate":
                scope = str(args.get("scope", ""))
                ref   = str(args.get("ref", ""))
                value = str(args.get("value", ""))
                mode  = str(args.get("mode", "append"))
                obs   = tool_bus.mutate(scope, ref, value, mode)
                calls_log.append({"tool": "mutate", "scope": scope, "ref": ref, "value": value, "mode": mode, "obs": obs})
            elif tool == "admin_action":
                action = str(args.get("action", ""))
                target = str(args.get("target", ""))
                reason = str(args.get("reason", ""))
                obs    = tool_bus.admin_action(action, target, reason)
                calls_log.append({"tool": "admin_action", "action": action, "target": target, "reason": reason, "obs": obs})
            else:
                obs = "DITOLAK: tool tidak dikenal."
        except Exception as e:
            obs = f"ERROR: {e}"
            _dbg.log_error(f"_react_agent_loop.{tool}", e)

        _dbg.line(f"  [OBS] {obs[:200]}")
        transcript += f"\n\n[OBS]\n{obs}"

    else:
        # max_steps tercapai tanpa done() — paksa stop, jangan loop lebih jauh.
        _dbg.line("  [REACT] max_steps tercapai, paksa stop")

    return calls_log


# ── Production ToolBus — wrapper RouterExecutor utk dipakai _react_agent_loop ─

_REACT_SCOPE_TO_FNAME = {
    "self_memory":  "gum",
    "self_stats":   "gus",
    "self_gifts":   "gugh",
    "other_user":   "gum",      # ref WAJIB user_id hasil resolve(), bukan username
    "stream_info":  "gsi",
    "viewer_count": "gvc",      # NB: saat ini RouterExecutor._dispatch return "N/A" utk gvc
    "chat_history": "grc",
    "time":         "gtc",
    "mood":         "gmm",
    "activity":     "gac",
}


class RouterExecutorToolBus:
    """
    Adapter produksi: mengubah panggilan generik (lookup/resolve/mutate/
    admin_action/done) dari _react_agent_loop jadi panggilan ke RouterExecutor
    asli (gum/gus/uum/bu/dst). Semua guard keamanan (admin check, dsb) tetap
    ditegakkan oleh RouterExecutor sendiri (lihat _banned_user) — bus ini
    TIDAK menambah trust apa pun ke argumen yang datang dari model.
    """

    def __init__(self, executor: "RouterExecutor", current_user_id: str, is_admin: bool):
        self.executor   = executor
        self.user_id    = current_user_id
        self.is_admin   = is_admin

    def lookup(self, scope: str, ref: str = "") -> str:
        fname = _REACT_SCOPE_TO_FNAME.get(scope)
        if not fname:
            return f"DITOLAK: scope '{scope}' tidak dikenal."
        args: Dict = {}
        if scope == "other_user":
            if not ref:
                return "DITOLAK: scope other_user butuh ref user_id (panggil resolve() dulu)."
            args["id"] = ref
        elif scope == "chat_history":
            args["n"] = 5
        # scope self_* sengaja TIDAK kirim id -> RouterExecutor default ke current user
        return self.executor._dispatch(fname, args)

    def resolve(self, text_ref: str) -> str:
        text_ref = (text_ref or "").strip()
        if not text_ref:
            return json.dumps({"candidates": []})
        mem = self.executor.user_mgr.find_by_username(text_ref)
        if mem:
            return json.dumps({"candidates": [{"user_id": mem.user_id, "display_name": mem.get_display_name()}]})
        return json.dumps({"candidates": []})

    def mutate(self, scope: str, ref: str, value: str, mode: str = "append") -> str:
        if scope != "self_memory":
            return "DITOLAK: scope mutate selain self_memory belum didukung di sini."
        if mode == "delete":
            return "DITOLAK: mode delete belum didukung sistem memory saat ini."
        # ref diabaikan utk self_memory (selalu target diri sendiri / current_user_id)
        return self.executor._dispatch("uum", {"m": value})

    def admin_action(self, action: str, target: str = "", reason: str = "") -> str:
        if action == "ban":
            return self.executor._dispatch("bu", {"id": target, "r": reason})
        if action == "cmd":
            return self.executor._dispatch("cmd", {"c": reason or target})
        return f"DITOLAK: admin action '{action}' tidak dikenal."

    def done(self) -> str:
        return "OK"


# ── Pass C: Soul Generate v3 — leaner ctx, pakai soul_final_system ──────────

def build_soul_ctx_v3(
    directive:      Dict,
    history:        List[Dict],
    username:       str,
    user_input:     str,
    char_name:      str,
    memory_context: str = "",   # dari Context Composer full_context
) -> str:
    """
    Build input untuk Soul Generate v3.
    Kirim: [Directive], [Memory Context] (opsional), [HISTORY] (max 3), [Chat].
    """
    lines: List[str] = []

    lines.append("[Directive]")
    lines.append(f"address_user_as: {directive.get('address_user_as', username)}")
    lines.append(f"persona_note: {directive.get('persona_note', '')}")
    lines.append(f"must_use_data: {directive.get('must_use_data', '')}")
    lines.append("")

    # Inject memory context dari Context Composer jika ada dan bukan hanya tool data
    # full_context dari composer TIDAK mengandung tool_output (sudah difix di atas)
    if memory_context and memory_context.strip():
        # Filter: skip jika hanya berisi [Data] section (tool output) tanpa memory
        _mc_clean = memory_context.strip()
        _has_real_memory = any(
            section in _mc_clean
            for section in ("[Working Memory]", "[Profil User]", "[knowledge/", "[riwayat sesi", "[pengalaman","[Profil Diri Karakter]")
        )
        if _has_real_memory:
            lines.append("[Memory Context]")
            lines.append(_mc_clean)
            lines.append("")

    lines.append("[HISTORY]")
    if history:
        recent = history[-3:]
        for m in recent:
            role        = m.get("role", "")
            raw_content = (m.get("content") or "").strip()
            raw_content = raw_content.replace(" | ", " ").replace("| ", "").strip()
            first_sent  = re.split(r'(?<=[.!?~])\s+', raw_content)
            content     = first_sent[0].strip()[:120] if first_sent else raw_content[:120]
            if role == "user":
                if content.startswith("[") and "]:" in content:
                    actual_user = content[1:content.find("]:")]
                    actual_msg  = content[content.find("]:") + 2:].strip()
                    lines.append(f"{actual_user}: {actual_msg}")
                else:
                    lines.append(f"{username}: {content}")
            elif role == "assistant":
                lines.append(f"{char_name}: {content}")
    else:
        lines.append("(none)")
    lines.append("")

    clean_input = user_input.strip()
    prefix = f"{username}:"
    if clean_input.lower().startswith(prefix.lower()):
        clean_input = clean_input[len(prefix):].strip()
    lines.append("[Chat]")
    lines.append(f"{username}: {clean_input}")

    return "\n".join(lines)


def _soul_pass_v3(ctx: str, complexity: str = "deep") -> str:
    """
    Pass C: Generate dialog menggunakan online custom API + soul_final_system.

    complexity → Soul mode (dari Context Composer / LAST_GATE_INFO):
      nano   → max_tokens=100  (greeting singkat, tidak butuh respons panjang)
      lite   → max_tokens=300
      normal → max_tokens=600
      deep   → max_tokens=1208 (default, respons penuh)
    """
    _MAX_TOK_MAP = {"nano": 100, "lite": 300, "normal": 600, "deep": 1208}
    max_tok = _MAX_TOK_MAP.get(complexity, 1208)

    messages = [
        {"role": "system", "content": _get_prompt("soul_final_system")},
        {"role": "user",   "content": ctx},
    ]
    _soul_routed = get_client("soul")
    _soul_model  = get_model("soul")
    _soul_extra_body = get_extra_body("soul")
    _dbg.log_step(3, "SOUL PASS v3", f"endpoint={_soul_model or 'default'} mode={complexity} max_tok={max_tok}")
    _dbg.log_ctx(ctx)
    try:
        resp = _soul_routed.chat.completions.create(
            model=_soul_model,
            messages=messages,
            temperature=0.8,
            max_tokens=max_tok,
            **({"extra_body": _soul_extra_body} if _soul_extra_body else {}),
        )
        raw = (resp.choices[0].message.content or "").strip()
        _dbg.log_raw_response("SOUL", raw)
        if raw.startswith("{") or raw.startswith("["):
            try:
                data = json.loads(raw)
                if isinstance(data, dict):
                    for key in ("ind", "text", "response", "output"):
                        if key in data:
                            return str(data[key]).strip() or _SOUL_FALLBACK()
                    resps = data.get("responses", [])
                    if resps and isinstance(resps[0], dict):
                        texts = [r.get("ind", "") for r in resps if r.get("ind")]
                        if texts:
                            return " ".join(texts)
            except Exception:
                pass
        return raw or _SOUL_FALLBACK()
    except Exception as e:
        _dbg.log_error("_soul_pass_v3", e)
        return _SOUL_FALLBACK()


# ── Pass 7 (lama — masih dipakai opening/closing/banter via build_soul_ctx_v2) ─

def build_soul_ctx_v2(
    user_mem:     "UserMemory",
    model_mem:    "ModelMemory",
    user_input:   str,
    username:     str,
    data_summary: str,
    think:        Dict,
    persona:      Dict,
    history:      List[Dict],
) -> str:
    """
    Build context untuk Soul Generate (Pass 7) — jauh lebih ringkas dari v1.
    Hanya kirim: identity, CMD, 3 history terakhir, data ringkas, intent+persona, chat.
    """
    romance_status = user_mem.get_romance_status() or "stranger"
    cmd            = (model_mem.command or "").strip()
    char_name      = CHARACTER.get("name") or _ACTIVE_CHAR_NAME

    lines: List[str] = []

    # Identity ringkas
    lines.append("[Identity]")
    lines.append(f"Target_User: {username}")
    lines.append(f"Relation: {romance_status}")
    lines.append("")

    # CMD
    lines.append("[CMD]")
    lines.append(cmd if cmd else "none")
    lines.append("")

    # History — hanya 3 terakhir, pipe dibersihkan, max 1 kalimat per entry
    lines.append("[HISTORY]")
    if history:
        recent = history[-3:]
        for m in recent:
            role    = m.get("role", "")
            raw_content = (m.get("content") or "").strip()
            # Strip pipe lama dari history sebelum perbaikan
            raw_content = raw_content.replace(" | ", " ").replace("| ", "").strip()
            # Ambil hanya kalimat pertama (sebelum . ! ?) max 120 char
            # Ini cegah model "belajar" respons panjang dari history
            first_sent = re.split(r'(?<=[.!?~])\s+', raw_content)
            content = first_sent[0].strip()[:120] if first_sent else raw_content[:120]
            if role == "user":
                if content.startswith("[") and "]:" in content:
                    actual_user = content[1:content.find("]:")]
                    actual_msg  = content[content.find("]:") + 2:].strip()
                    lines.append(f"{actual_user}: {actual_msg}")
                else:
                    lines.append(f"{username}: {content}")
            elif role == "assistant":
                lines.append(f"{char_name}: {content}")
    else:
        lines.append("(none)")
    lines.append("")

    # Data ringkas dari tool (bukan raw dump)
    lines.append("[CONTEXT]")
    if data_summary:
        lines.append(data_summary)
    else:
        lines.append("(tidak ada data tambahan)")
    lines.append("")

    # Think result — grounding intent
    lines.append("[TASK]")
    lines.append(f"intent: {think.get('intent', '')}")
    lines.append(f"key_point: {think.get('key_point', '')}")
    lines.append(f"gaya: {persona.get('style', 'malu-malu natural')}")
    stammer_hint = "Sisipkan 'E-eh...' atau 'Um...' di awal jika cocok." if persona.get("stammer") else ""
    if stammer_hint:
        lines.append(stammer_hint)
    lines.append(f"DILARANG: {persona.get('forbidden', 'asterisk action, markdown')}")
    lines.append("")

    # Chat
    clean_input = user_input.strip()
    prefix = f"{username}:"
    if clean_input.lower().startswith(prefix.lower()):
        clean_input = clean_input[len(prefix):].strip()
    lines.append("[Chat]")
    lines.append(f"{username}: {clean_input}")

    return "\n".join(lines)


# ── Pass 8: Soul Validate ────────────────────────────────────────────────────

# Regex cepat — cek tanpa LLM dulu
import re as _re
_VALIDATE_PATTERNS = [
    (_re.compile(r"\*[^\*]+\*"),         "asterisk"),
    (_re.compile(r"\*\*[^\*]+\*\*"),       "markdown"),
    (_re.compile(r"^#{1,3}\s", _re.M),      "markdown"),
    (_re.compile(r"_{1,2}[^_]+_{1,2}"),     "markdown"),
    (_re.compile(r"\|"),                    "pipe"),
]

_OOC_PHRASES = ("sebagai ai", "sebagai asisten", "saya adalah")

def _soul_validate(soul_text: str, char_name: str) -> Dict:
    """
    Pass 8: validasi output soul — MURNI regex/rule-based, TIDAK ADA LLM
    call sama sekali.

    PATCH: dulu ada cabang LLM ("soul_validate" pass, prompt
    soul_validate_system) yang dipanggil kalau teks kelihatan "suspicious"
    (nyebut "sebagai AI"/"sebagai asisten"/dst) — tujuannya nangkep OOC/nama
    salah yang regex gak bisa cek. Tapi deteksi "suspicious"-nya SENDIRI
    sudah cukup jadi sinyal OOC yang valid (substring match, bukan
    ambigu) — jadi kirim ke model lagi buat "konfirmasi ulang" itu langkah
    ekstra yang gak perlu. Sekarang begitu salah satu frasa itu ketemu,
    langsung ditandai issue 'ooc' tanpa validasi model tambahan.
    Returns: {"ok": bool, "issues": [str]}
    """
    _dbg.log_step(8, "SOUL VALIDATE", f"len={len(soul_text)} chars")
    _dbg.line(f"  [SOUL PREVIEW] {soul_text[:100]}")
    issues: List[str] = []

    # Cek regex (asterisk/markdown/pipe)
    for pattern, label in _VALIDATE_PATTERNS:
        if pattern.search(soul_text):
            issues.append(label)

    if not soul_text or len(soul_text.strip()) < 10:
        issues.append("too_short")

    # Cek OOC via substring — dulu cuma jadi trigger buat LLM call, sekarang
    # langsung jadi issue final (tidak perlu model buat mengonfirmasi ulang
    # sesuatu yang sudah jelas dari kata-katanya sendiri).
    text_lower = soul_text.lower()
    if any(p in text_lower for p in _OOC_PHRASES):
        issues.append("ooc")

    ok = len(issues) == 0
    _dbg.line(f"  [VALIDATE] ok={ok} issues={issues}")
    return {"ok": ok, "issues": issues}


def _soul_fix(soul_text: str, issues: List[str]) -> str:
    """
    Fix ringan tanpa LLM — hapus asterisk dan markdown dari teks.
    Jika perlu regen, caller yang memutuskan.
    """
    fixed = soul_text
    # Hapus *action* dan **bold**
    fixed = _re.sub(r"\*{1,2}([^\*]+)\*{1,2}", r"\1", fixed)
    # Hapus # heading
    fixed = _re.sub(r"^#{1,3}\s+", "", fixed, flags=_re.M)
    # Hapus _italic_
    fixed = _re.sub(r"_{1,2}([^_]+)_{1,2}", r"\1", fixed)
    # Hapus pipe | yang mungkin terbawa dari history lama
    fixed = _re.sub(r"\s*\|\s*", " ", fixed)
    fixed = fixed.strip()
    _dbg.line(f"  [SOUL FIX] fixed len={len(fixed)}")
    return fixed


# ═══════════════════════════════════════════════════════════════════════════════
# CTX BUILDERS — format sesuai training dataset
# ═══════════════════════════════════════════════════════════════════════════════

def build_soul_ctx(
    user_mem:    "UserMemory",
    model_mem:   ModelMemory,
    user_input:  str,
    username:    str,
    router_data: str = "",
    history:     List[Dict] = None,
) -> str:
    """
    Build input untuk [SOUL] (single-prompt format).
    Format: [Identity] [CMD] [HISTORY] [TASK_RESULT] [Chat]
    """
    romance_status = user_mem.get_romance_status() or "stranger"
    cmd = (model_mem.command or "").strip()

    lines: List[str] = []

    # ── Identity ──────────────────────────────────────────────────────────
    lines.append("[Identity]")
    lines.append(f"Target_User: {username}")
    lines.append(f"Relation: {romance_status}")
    lines.append("")

    # ── CMD ───────────────────────────────────────────────────────────────
    lines.append("[CMD]")
    lines.append(cmd if cmd else "none")
    lines.append("")

    # ── History (max 7 entri) ─────────────────────────────────────────────
    lines.append("[HISTORY CHAT]")
    if history:
        recent = history[-7:]  # maks MAX_HISTORY = 7
        for m in recent:
            role    = m.get("role", "")
            content = (m.get("content") or "").strip()
            
            if role == "user":
                if content.startswith("[") and "]:" in content:
                    actual_user = content[1:content.find("]:")]
                    actual_msg = content[content.find("]:") + 2:].strip()
                    lines.append(f"{actual_user}: {actual_msg}")
                else:
                    lines.append(f"{username}: {content}")
            elif role == "assistant":
                char_label = CHARACTER.get("name") or _ACTIVE_CHAR_NAME
                lines.append(f"{char_label}: {content}")
    else:
        lines.append("(none)")
    lines.append("")

    # ── TASK_RESULT ───────────────────────────────────────────────────────
    lines.append("[TASK_RESULT]")
    if router_data and router_data.strip():
        data = router_data.strip()
        if data.startswith("[ROUTER]"):
            data = data[len("[ROUTER]"):].strip()
        lines.append("status: success")
        lines.append(f"result: {data}")
    else:
        lines.append("status: null")
        lines.append("result: null")
    lines.append("")

    # ── Chat ──────────────────────────────────────────────────────────────
    # FIX: Strip username prefix if user_input already contains it
    clean_input = user_input.strip()
    prefix = f"{username}:"
    if clean_input.lower().startswith(prefix.lower()):
        clean_input = clean_input[len(prefix):].strip()
    
    lines.append("[Chat]")
    lines.append(f"{username}: {clean_input}")

    return "\n".join(lines)

def build_anim_ctx(
    user_mem:    "UserMemory",
    model_mem:   ModelMemory,
    user_input:  str,
    username:    str,
    soul_text:   str,
    router_data: str = "",
) -> str:
    """
    Build input untuk [ANIM] model.
    Format: Context: ..., [INPUT], [TASK_RESULT], Output: {soul_text}
    """
    romance_status = user_mem.get_romance_status() or "stranger"
    cmd = (model_mem.command or "").strip()

    lines = ["Context:"]
    lines.append(f"romance-status: {romance_status}")
    if cmd:
        lines.append(f"cmd: {cmd}")
    lines.append("")
    lines.append("[INPUT]")
    lines.append(f"{username}: {user_input.strip()}")
    lines.append("")
    lines.append("[TASK_RESULT]")
    if router_data and router_data.strip():
        data = router_data.strip()
        if data.startswith("[ROUTER]"):
            data = data[len("[ROUTER]"):].strip()
        lines.append(data)
    # Kalau null, tidak ditulis apa-apa
    lines.append("")
    lines.append("Output:")
    lines.append(soul_text.strip())

    return "\n".join(lines)

_INNER_FALLBACK = ""  # kept for compat — not used


def _get_soul_fallback() -> str:
    """Fallback dialog memakai nama karakter aktif, bukan hardcoded."""
    char_name = CHARACTER.get("name") or _ACTIVE_CHAR_NAME or "Aku"
    return f"A-ah... maaf, {char_name} sepertinya nggak bisa jawab saat ini..."

_SOUL_FALLBACK = _get_soul_fallback  # callable — panggil saat dibutuhkan

def _soul_pass(ctx: str) -> str:
    """
    Pass 3: Generate dialog Alfa menggunakan online custom API.
    History sudah di-embed di dalam ctx — tidak perlu dikirim sebagai messages terpisah.
    Model: SOUL_MODEL_NAME di SOUL_API_URL.
    """
    messages = [
        {"role": "system", "content": _get_prompt("soul_system")},
        {"role": "user",   "content": ctx},
    ]
    _opening_client = get_client("opening")
    _opening_model  = get_model("opening")
    _opening_extra_body = get_extra_body("opening")
    _dbg.log_step(3, "SOUL PASS (opening/closing)", f"model={_opening_model or 'default'}")
    _dbg.log_ctx(ctx)
    try:
        resp = _opening_client.chat.completions.create(
            model=_opening_model,
            messages=messages,
            temperature=0.8,
            max_tokens=1208,
            **({"extra_body": _opening_extra_body} if _opening_extra_body else {}),
        )
        raw = (resp.choices[0].message.content or "").strip()
        _dbg.log_raw_response("SOUL", raw)
        # Kalau model masih output JSON, coba ekstrak teks
        if raw.startswith("{") or raw.startswith("["):
            try:
                data = json.loads(raw)
                if isinstance(data, dict):
                    for key in ("ind", "text", "response", "output"):
                        if key in data:
                            return str(data[key]).strip() or _SOUL_FALLBACK()
                    resps = data.get("responses", [])
                    if resps and isinstance(resps[0], dict):
                        texts = [r.get("ind", "") for r in resps if r.get("ind")]
                        if texts:
                            return " ".join(texts)
            except Exception:
                pass
        return raw or _SOUL_FALLBACK()
    except Exception as e:
        _dbg.log_error("_soul_pass", e)
        return _SOUL_FALLBACK()

_ANIM_FALLBACK = {"anims": [{"anim": "neutral", "pos": 0}], "point": 0}


def _random_anim_data(animations, soul_text=""):
    """Pengganti _anim_pass() — random, tidak pakai LLM."""
    if not animations:
        return _ANIM_FALLBACK
    first_anim = random.choice(animations)
    anims = [{"anim": first_anim, "pos": 0}]
    if soul_text and len(soul_text) > 60:
        anims.append({"anim": random.choice(animations), "pos": len(soul_text) // 2})
    return {"anims": anims, "point": 0}

def _anim_pass(ctx: str, animations: List[str]) -> Dict:
    """
    Pass 5: Tentukan animasi (character-position based) dan skor kualitas.
    Input: anim_ctx (Context + soul_text)
    Output: {"anims": [{"anim": X, "pos": N}], "point": N}
    """
    _dbg.log_step(5, "ANIM PASS")
    try:
        resp = _llm_call(
            "anim",
            messages=[
                {"role": "system", "content": _get_prompt("anim_system")},
                {"role": "user",   "content": ctx},
            ],
            temperature=0.1,
            max_tokens=200,
        )
        raw = (resp.choices[0].message.content or "").strip()
        _dbg.log_raw_response("ANIM", raw)
        data = safe_parse(raw)
        if not data or "anims" not in data:
            return _ANIM_FALLBACK

        # Validasi anims
        valid_anims = []
        for a in data.get("anims", []):
            anim_name = a.get("anim", "neutral")
            anim_pos  = int(a.get("pos", 0))
            if anim_name not in animations:
                anim_name = animations[0] if animations else "neutral"
            valid_anims.append({"anim": anim_name, "pos": max(0, anim_pos)})

        if not valid_anims:
            valid_anims = [{"anim": animations[0] if animations else "neutral", "pos": 0}]

        # Sort by position
        valid_anims.sort(key=lambda x: x["pos"])

        point = max(-10, min(10, int(data.get("point", 0))))
        _dbg.line(f"  [ANIM] {valid_anims} | point={point:+d}")
        return {"anims": valid_anims, "point": point}

    except Exception as e:
        _dbg.log_error("_anim_pass", e)
        return _ANIM_FALLBACK

def _get_anim_at_pos(anims: List[Dict], char_pos: int) -> str:
    """Dapatkan animasi yang aktif pada posisi karakter tertentu."""
    current = anims[0]["anim"] if anims else "neutral"
    for a in anims:
        if a.get("pos", 0) <= char_pos:
            current = a["anim"]
        else:
            break
    return current


# ═══════════════════════════════════════════════════════════════════════════════
# STEP 5 — TRANS PASS (split di Python → translate 1 kalimat per request)
# ═══════════════════════════════════════════════════════════════════════════════

def split_sentences(text: str, min_chars: int = 30) -> List[str]:
    """
    Split teks Mailin menjadi kalimat-kalimat di Python — tanpa LLM.
    Menangani delimiter ojousama: desuwa~, ufufu~, ~, !, ?, .
    Delimiter dipertahankan di akhir kalimat.

    Kalimat pendek (< min_chars karakter) digabung ke kalimat berikutnya;
    jika tidak ada kalimat berikutnya, digabung ke kalimat sebelumnya.
    Ini mencegah potongan super-pendek seperti "hah...", "eh?", dll.
    Kata-kata seperti "desuwa~", "ufufu~" tetap dikirim ke LLM apa adanya.
    """
    if not text:
        return []
    text = text.strip()
    if not text:
        return []

    # Split setelah . ! ? atau ~ yang diikuti satu atau lebih spasi
    parts = re.split(r'(?<=[.!?~])\s+', text)
    parts = [p.strip() for p in parts if p.strip()]
    if not parts:
        return [text]

    # Merge kalimat pendek ke kalimat berikutnya (atau sebelumnya jika terakhir)
    merged: List[str] = []
    pending: str = ""

    for part in parts:
        if pending:
            # Gabung pending ke part ini
            part = pending + " " + part
            pending = ""

        if len(part) < min_chars:
            # Terlalu pendek — tahan dulu, gabung ke kalimat berikutnya
            pending = part
        else:
            merged.append(part)

    # Sisa pending (kalimat terakhir yang pendek) → gabung ke kalimat sebelumnya
    if pending:
        if merged:
            merged[-1] = merged[-1] + " " + pending
        else:
            # Hanya ada satu potongan dan pendek — kembalikan apa adanya
            merged.append(pending)

    return merged if merged else [text]


def _build_trans_messages(sentence: str) -> List[Dict]:
    """
    Susun messages untuk _trans_single: system prompt singkat + contoh
    few-shot sebagai turn user/assistant ASLI (bukan ditulis ulang jadi
    teks panjang di system prompt) + kalimat yang mau diterjemahkan.

    _flatten_messages() (lihat di atas) akan merapikan turn-turn contoh ini
    jadi blok <RIWAYAT_LAMA>/<EXCHANGE_TERAKHIR> sebelum dikirim ke model
    lokal — jauh lebih gampang dibaca model kecil dibanding 1 wall of text
    di system prompt, sekaligus prompt utama tetap pendek.
    """
    messages: List[Dict] = [
        {"role": "system", "content": _get_prompt("trans_system")},
    ]
    for ex in _get_trans_examples():
        ex_id = ex.get("ind") or ex.get("id")
        ex_jp = ex.get("jp")
        if ex_id and ex_jp:
            messages.append({"role": "user",      "content": ex_id})
            messages.append({"role": "assistant", "content": ex_jp})
    messages.append({"role": "user", "content": sentence})
    return messages


def _build_stall_messages(user_input: str, char_name: str) -> List[Dict]:
    """
    PATCH v2: prompt kecil buat generate pesan "tunggu sebentar" via model
    LOCAL — beda kata-kata tiap kali dipanggil (bukan pilih dari list statis).
    Sengaja pendek + max_tokens kecil biar cepat (ini dipanggil PAS di tengah
    jalan, jadi harus ringan).
    """
    sys_txt = (
        f"Kamu berperan sebagai {char_name}. User baru saja mengirim pesan yang "
        f"butuh beberapa langkah untuk diproses (bukan jawaban instan).\n"
        f"Balas dengan SATU kalimat pendek (maksimal 12 kata), bergaya {char_name}, "
        f"untuk bilang 'tunggu sebentar' ke user.\n"
        f"Aturan:\n"
        f"- Variasikan kata-katanya — JANGAN kalimat template yang sama tiap kali.\n"
        f"- JANGAN sebut alasan teknis (API, server, sistem, proses, loading, dst).\n"
        f"- Jawab HANYA kalimatnya saja, tanpa tanda kutip, tanpa penjelasan lain."
    )
    return [
        {"role": "system", "content": sys_txt},
        {"role": "user",   "content": f"Pesan user: {user_input}"},
    ]


def _generate_stall_text(user_input: str, char_name: str) -> Optional[str]:
    """
    PATCH v2: panggil model LOCAL (pass_name="react", endpoint yang sama
    dipakai untuk ReAct/reflection/router_cache) buat generate kalimat
    "tunggu sebentar" yang bervariasi. temperature dinaikkan supaya tidak
    monoton. Return None kalau gagal (caller cukup skip, tidak perlu
    fallback teks statis).
    """
    try:
        resp = _llm_call(
            "react",
            messages=_build_stall_messages(user_input, char_name),
            temperature=0.9,
            max_tokens=40,
        )
        raw = (resp.choices[0].message.content or "").strip()
        raw = raw.strip("\"'“”‘’")
        return raw or None
    except Exception as e:
        _dbg.log_error("_generate_stall_text", e)
        return None


def _trans_single(sentence: str) -> Dict:
    """
    Terjemahkan SATU kalimat Indonesia → {id, jp}.
    'id' SELALU diisi dari `sentence` lokal (input asli), TIDAK PERNAH dari
    output model — jadi model tidak perlu (dan tidak diminta) menulis ulang
    teks Indonesia-nya, cukup output teks Jepang polos. Ini menghemat token
    dan menghilangkan risiko model mengubah/menghalusinasi ulang teks ID.
    """
    if not sentence:
        return {"id": sentence, "jp": "えっと…"}
    try:
        resp = _llm_call(
            "trans",
            messages=_build_trans_messages(sentence),
            temperature=0.2,
            max_tokens=300,
        )
        raw = (resp.choices[0].message.content or "").strip()

        # Output yang diharapkan: teks Jepang polos. Tapi tetap toleran kalau
        # model lama kebiasaan masih balas JSON (mis. belum di-restart) —
        # coba ekstrak field "jp" dulu sebelum treat sebagai raw text.
        obj = safe_parse(raw)
        if obj and isinstance(obj, dict) and obj.get("jp"):
            jp = clean_jp_for_tts(str(obj["jp"]))
            return {"id": sentence, "jp": jp or "えっと…"}
        arr = safe_parse_array(raw)
        if arr and isinstance(arr, list):
            for item in arr:
                if isinstance(item, dict) and item.get("jp"):
                    jp = clean_jp_for_tts(str(item["jp"]))
                    return {"id": sentence, "jp": jp or "えっと…"}

        jp = clean_jp_for_tts(raw) if raw else "えっと…"
        return {"id": sentence, "jp": jp or "えっと…"}

    except Exception as e:
        _dbg.log_error("_trans_single", e)
        return {"id": sentence, "jp": "えっと…"}


def _trans_pass(soul_text: str) -> List[Dict]:
    """
    Pass 6: Terjemahkan plain text → [{id, jp}].
    BARU: Split kalimat di Python, lalu translate 1 per 1.
    Lebih cepat, token lebih kecil, jarang error dibanding kirim semua sekaligus.
    """
    if not soul_text or soul_text == _SOUL_FALLBACK():
        return [{"id": soul_text or "...", "jp": "えっと…"}]

    sentences = split_sentences(soul_text)
    _dbg.log_step(6, "TRANS PASS (per-sentence)", f"{len(sentences)} kalimat")

    result: List[Dict] = []
    for i, sent in enumerate(sentences):
        seg = _trans_single(sent)
        result.append(seg)
        _dbg.line(f"  [TRANS] {i+1}/{len(sentences)}: {seg.get('jp','')[:50]}")

    return result if result else [{"id": soul_text, "jp": "えっと…"}]

def _merge_segments(
    soul_text:  str,
    anim_data:  Dict,
    trans_segs: List[Dict],
    animations: List[str],
) -> Tuple[List[Dict], str, int]:
    """
    Gabungkan output SOUL + ANIM + TRANS menjadi format final [{ind, jp, anim}].
    Animasi ditentukan berdasarkan posisi karakter setiap segmen di soul_text.
    Returns: (responses, dominant_anim, points)
    """
    anims  = anim_data.get("anims", [{"anim": animations[0] if animations else "neutral", "pos": 0}])
    points = anim_data.get("point", 0)

    result: List[Dict] = []
    for seg in trans_segs:
        ind = seg.get("id", "").strip()
        jp  = seg.get("jp", "えっと…")
        if not ind:
            continue

        # Cari posisi segmen ini di soul_text
        char_pos = soul_text.find(ind)
        if char_pos == -1:
            # Partial match: cari kata pertama
            first_words = " ".join(ind.split()[:3])
            char_pos = soul_text.find(first_words)
            if char_pos == -1:
                char_pos = 0

        anim = _get_anim_at_pos(anims, char_pos)
        if anim not in animations:
            anim = animations[0] if animations else "neutral"

        result.append({"ind": ind, "jp": jp, "anim": anim})

    if not result:
        fallback_anim = animations[0] if animations else "neutral"
        result = [{"ind": soul_text, "jp": "えっと…", "anim": fallback_anim}]

    # Dominant = animasi yang paling sering muncul
    anim_counts: Dict[str, int] = {}
    for r in result:
        anim_counts[r["anim"]] = anim_counts.get(r["anim"], 0) + 1
    dominant = max(anim_counts, key=anim_counts.get)

    return result, dominant, points


_FALLBACK_MARKER = "A-ah... maaf, ada gangguan sebentar."

def _make_fallback(animations: List[str]) -> Tuple[List[Dict], str]:
    anim = animations[0] if animations else "neutral"
    return ([{"ind": _FALLBACK_MARKER, "jp": "あ、ちょっと問題が……", "anim": anim}], anim)


def full_generate(
    user_input:       str,
    user_mem:         "UserMemory",
    char_name:        str      = None,
    char_data:        Dict     = None,
    username:         str      = None,
    segment_callback: Optional[callable] = None,
    stall_callback:   Optional[callable] = None,
) -> Tuple[List[Dict], str]:
    """
    segment_callback(seg: Dict) — dipanggil untuk setiap segmen {ind, jp, anim}
    segera setelah kalimat tersebut selesai ditranslasi. Berguna untuk streaming
    play: TTS bisa mulai sebelum semua kalimat selesai ditranslasi.
    Jika None, perilaku sama seperti sebelumnya (batch, return setelah semua selesai).

    stall_callback(seg: Dict) — PATCH v2: dipanggil PALING BANYAK SEKALI per giliran,
    dipicu dari task_router.run_task_router() begitu jumlah kategori/task yang
    bakal dieksplor terdeteksi >= _STALL_MIN_CATEGORIES (lihat task_router.py) —
    yaitu SEBELUM Pass B/C+EXEC (bagian yang bisa makan waktu beberapa detik)
    benar-benar jalan. Isinya di-generate LANGSUNG ke model lokal (pass "react")
    tiap kali dipicu — jadi kata-katanya bervariasi, BUKAN dipilih dari list
    statis — lalu diterjemahkan (_trans_single) supaya seg berbentuk sama
    seperti segment_callback ({"ind", "jp", "anim"}) dan bisa langsung
    di-play/tampilkan sebagai TTS interrupt oleh caller. Setelah task selesai,
    jawaban final tetap menyusul lewat segment_callback/return seperti biasa
    — jadi alurnya: [deteksi lama] → generate+TTS interrupt → proses jalan →
    [selesai] → generate+TTS jawaban final. Kalau None atau generate gagal,
    tidak ada efek (perilaku sama seperti sebelum patch ini).
    """
    global _last_assistant_response

    t_start    = time.monotonic()
    char       = char_data or CHARACTER or {}
    char_name  = char_name or _ACTIVE_CHAR_NAME
    # PATCH: char_id dihitung SEKALI di awal (dulu baru dihitung jauh di
    # bawah, ~STEP 6) — root cause bug "working memory karakter lama masih
    # kebawa pas ganti karakter": CONFIRMATION handler & beberapa titik lain
    # di atas situ manggil working_memory TANPA char_id sama sekali (belum
    # ada variabelnya), jadi keynya cuma user_id → data karakter lain nyampur.
    char_id    = char.get("id") or char_name or "default"
    animations = char.get("animations", ["shy", "neutral", "happy", "blush", "panicked", "focused", "shock"])

    # ── PATCH v2: stall/filler message — generate ke model lokal ─────────────
    # Dipicu (paling banyak 1x per giliran) dari dalam _task_router_pass →
    # run_task_router() begitu JUMLAH kategori/task yang bakal dieksplor
    # terdeteksi banyak (lihat _STALL_MIN_CATEGORIES di task_router.py) —
    # yaitu SEBELUM Pass B/C + EXEC tool eksternal (bagian yang bisa makan
    # waktu beberapa detik) mulai jalan.
    _stall_fired = {"done": False}

    def _fire_stall_once(stall_input: str):
        if _stall_fired["done"]:
            return
        if stall_callback is None:
            # PATCH: sebelumnya silent return tanpa jejak log sama sekali —
            # ini yang bikin susah didiagnosa waktu caller (mis. main() CLI)
            # lupa/tidak wire parameter stall_callback= ke full_generate().
            # Router (task_router.py) tetap akan lapor "[STALL] N kategori
            # terdeteksi → trigger" karena DIA terima closure ini (bukan
            # None) — tapi kalau baris di bawah ini yang muncul, artinya
            # caller full_generate() sendiri belum kasih stall_callback.
            _dbg.line("  [STALL] skip — stall_callback tidak di-set oleh caller full_generate() (None)")
            _stall_fired["done"] = True
            return
        _stall_fired["done"] = True
        try:
            text = _generate_stall_text(stall_input, char_name)
            if not text:
                _dbg.line("  [STALL] skip — generate gagal/kosong")
                return
            trans = _trans_single(text)
            expr  = random.choice(animations) if animations else "neutral"
            seg   = {"ind": trans.get("id", text), "jp": trans.get("jp", "えっと…"), "anim": expr}
            _dbg.line(f"  [STALL] mengirim filler duluan: {seg['ind'][:60]}")
            stall_callback(seg)
        except Exception as e:
            _dbg.log_error("stall_callback", e)

    mm    = _model_mem or ModelMemory(char_name, storage_dir=MODEL_MEMORY_DIR)
    ch    = _chat_hist or ChatHistory(char_name, storage_dir=MODEL_MEMORY_DIR)
    # PENTING: prioritaskan nickname yang SUDAH TERSIMPAN (get_display_name)
    # di atas username mentah dari platform chat. Sebelumnya logikanya
    # `username or get_display_name()` — karena `username` dari platform
    # SELALU ada isinya, nickname yang baru disimpan via mutate() TIDAK
    # PERNAH terpakai lagi di turn-turn berikutnya (bug: nickname tersimpan
    # tapi tidak pernah dipakai utk menyapa). get_display_name() sendiri
    # sudah fallback ke username asli kalau user belum punya nickname.
    uname = user_mem.get_display_name() or username

    _dbg.section(f"GENERATE v11 (agent pipeline)  user={uname}  char={char_name}")
    _dbg.line(f"  input       : {user_input[:100]}")
    _dbg.line(f"  user_id     : {user_mem.user_id}")
    _dbg.line(f"  admin       : {user_mem.user_id.lower() == ADMIN_USER_ID.lower()}")
    _dbg.line(f"  romance_pts : {user_mem.romance_points} ({user_mem.get_romance_level()})")
    _dbg.line(f"  cmd_active  : {(_model_mem or mm).command or '(none)'}")

    # ── Style command detection ────────────────────────────────────────────
    style_cmd = _detect_style_command(user_input)
    if style_cmd is not None:
        if style_cmd == "":
            if mm.command:
                _dbg.line(f"  [CMD] Reset style command (was: '{mm.command}')")
                mm.command = ""
                mm.save()
        else:
            _dbg.line(f"  [CMD] Style command → '{style_cmd}'")
            mm.command = style_cmd
            mm.save()

    # ── Ambil history (maks 7 entri — untuk grounding & ctx) ──────────────
    raw_history = ch.get_messages(
        current_user_id=user_mem.user_id,
        current_username=uname,
    )
    history = _reorder_history_for_user(raw_history, current_username=uname)
    _dbg.log_history(history)

    # ── Conversation State Analysis (new memory system) ───────────────────
    # Dilakukan SETELAH history di-load agar analyzer bisa baca last-N messages.
    # Gate 0 check dulu: jika greeting/ekspresi murni → skip LLM analyze call.
    #
    # PATCH v5: Analyzer (di sini) dan task_router.precheck() (gate0/cache/
    # gate1/decision_graph) sama-sama cuma butuh (user_input, history/llm_call)
    # — TIDAK saling bergantung, tapi sebelumnya dipanggil berurutan (Analyzer
    # selesai dulu, baru nanti task router mulai). Sekarang ditembak BARENGAN
    # lewat shared executor (concurrency.py, dibatasi slot sesuai setting
    # server lokal kamu). Hasil precheck disimpan di _precheck_result, dipakai
    # nanti di titik pemanggilan router (skip _task_router_pass yang lama).
    #
    # Kalau style_cmd sudah literal (skip total tool routing) atau turn ini
    # ternyata CONFIRMATION (pending action, jalur lain), _precheck_result
    # cukup dibuang — sedikit kerja sia-sia untuk kasus yang relatif jarang,
    # demi latency lebih baik di kasus umum.
    _conv_state: Optional["ConversationState"] = None
    _precheck_result: Optional[Dict] = None
    if _MEMORY_SYSTEM_AVAILABLE:
        try:
            from gate import gate0 as _gate0
            _g0 = _gate0(user_input)
            if _g0 == "chat":
                # Greeting/ekspresi murni → pakai state lama dari disk, tidak perlu LLM
                _conv_state = _cs_load(user_mem.user_id)
                _conv_state.frame = "CHAT"
                _conv_state.complexity = 0
                _dbg.line(f"  [CONV STATE] gate0=chat → skip LLM analyze, frame=CHAT")
                if style_cmd is None:
                    _precheck_result = _tr_precheck(
                        user_id    = user_mem.user_id,
                        username   = uname,
                        user_input = user_input,
                        llm_call   = _llm_call,
                        dbg        = _dbg,
                        stall_callback = _fire_stall_once,
                    )
            elif style_cmd is None:
                _pool = _get_pool()
                _fut_analyzer = _pool.submit(
                    _cs_analyze,
                    user_id    = user_mem.user_id,
                    user_input = user_input,
                    history    = history,
                    llm_call   = _llm_call,
                    n_history  = 5,
                    dbg        = _dbg,
                )
                _fut_precheck = _pool.submit(
                    _tr_precheck,
                    user_id    = user_mem.user_id,
                    username   = uname,
                    user_input = user_input,
                    llm_call   = _llm_call,
                    dbg        = _dbg,
                    stall_callback = _fire_stall_once,
                )
                _conv_state      = _fut_analyzer.result()
                _precheck_result = _fut_precheck.result()
                _dbg.line(f"  [CONV STATE] {_conv_state.summary_line()}")
            else:
                # style_cmd literal sudah ada → router bakal di-skip total nanti,
                # gak perlu fan-out precheck() (hemat 1 slot buat Analyzer aja).
                _conv_state = _cs_analyze(
                    user_id    = user_mem.user_id,
                    user_input = user_input,
                    history    = history,
                    llm_call   = _llm_call,
                    n_history  = 5,
                    dbg        = _dbg,
                )
                _dbg.line(f"  [CONV STATE] {_conv_state.summary_line()}")
        except Exception as e:
            _dbg.log_error("conversation_state.analyze", e)

    # ── Setup executor & admin flag ───────────────────────────────────────────
    data_summary       = ""
    char_name_for_hist = CHARACTER.get("name") or char_name or "Alfa"
    history_text       = _history_snippet(history, uname, char_name_for_hist, n=4)
    is_admin           = user_mem.user_id.lower() == ADMIN_USER_ID.lower()

    executor = RouterExecutor(
        user_id       = user_mem.user_id,
        username      = uname,
        user_mgr      = get_user_manager(),
        chat_hist     = ch,
        model_mem     = mm,
        last_response = _last_assistant_response,
    )

    raw_tool_data = ""

    if style_cmd is not None:
        # Style command sudah ditangkap langsung (cmd literal),
        # skip seluruh tool routing — tidak perlu reasoning tambahan.
        _dbg.log_step(1, "TASK ROUTER (skip)", "style command literal terdeteksi")

    else:
        # ── CONFIRMATION: handle pending_action jika ada ─────────────────────
        # Jika ConvState mendeteksi frame=CONFIRMATION (user jawab "iya"/"jangan"),
        # ambil pending_action dari state sebelumnya dan execute/skip langsung.
        _pending_executed = False
        if (_MEMORY_SYSTEM_AVAILABLE and _conv_state is not None
                and _conv_state.frame == "CONFIRMATION"):
            from conversation_state import get_pending_action as _get_pa, _is_positive_confirmation
            # Ambil state SEBELUM analyze (pending sudah di-clear oleh analyze)
            # → kita simpan pending di _conv_state.pending_action sebelum analyze clear-nya
            # Lihat: conversation_state.analyze() menyimpan last_pending sebelum clear
            # Tapi kita perlu cara lain: cek working_memory untuk pending_action hint
            _pa = _wm.load(user_mem.user_id, char_id)
            _pa_tool = _pa.get("__pending_tool__")
            _pa_args = _pa.get("__pending_args__")
            if _pa_tool and _pa_args is not None:
                is_positive = _is_positive_confirmation(user_input)
                _dbg.line(f"  [CONFIRMATION] tool={_pa_tool} positive={is_positive}")
                if is_positive:
                    # Execute pending tool langsung
                    try:
                        _res = executor._dispatch(_pa_tool, _pa_args)
                        raw_tool_data = f"{_pa_tool}: {_res}"
                        _dbg.line(f"  [CONFIRMATION] executed → {raw_tool_data[:100]}")
                        _pending_executed = True
                    except Exception as e:
                        _dbg.log_error("confirmation_execute", e)
                else:
                    _dbg.line(f"  [CONFIRMATION] dibatalkan user → skip tool")
                    _pending_executed = True  # skip router juga
                # Clear pending dari working memory
                _pa.set("__pending_tool__", None)
                _pa.set("__pending_args__", None)
                _wm.save(_pa)

        if not _pending_executed:
            # ── PASS 1: Task Router — pakai hasil precheck() yang sudah
            # dijalankan PARALEL dengan Analyzer di atas (lihat blok
            # "Conversation State Analysis"). Kalau karena alasan tertentu
            # precheck belum sempat jalan (mis. style_cmd berubah di tengah,
            # exception, dsb), fallback ke jalur lama (sekuensial) supaya
            # tetap aman.
            if _precheck_result is not None:
                if _precheck_result["done"]:
                    router_calls = _precheck_result["calls"]
                    _dbg.line(
                        f"  [TASK ROUTER] (precheck paralel) → {len(router_calls)} "
                        f"call(s): {[c.get('f') for c in router_calls]}"
                    )
                elif _conv_state is not None and _conv_state.need_tool is False:
                    # PATCH: cross-check GRATIS — Analyzer & precheck (Gate1+DG)
                    # sudah jalan PARALEL di atas, jadi dua-duanya sudah selesai
                    # di titik ini tanpa biaya tambahan sama sekali. Kalau
                    # Analyzer eksplisit bilang need_tool=False tapi Gate1/DG
                    # tetap mau explore kategori (dua model kecil tidak setuju),
                    # percaya Analyzer dan skip Pass B/C sepenuhnya — ini
                    # menyaring kasus seperti "kabarmu hari ini apa?" yang
                    # Gate1 salah klasifikasi sebagai type=task, padahal
                    # Analyzer (yang baca frame+history lebih luas) sudah tahu
                    # ini murni CHAT. Menghemat 3-5 LLM call Pass B/C yang
                    # ujung-ujungnya SKIP semua juga (lihat log sebelumnya).
                    router_calls = []
                    _dbg.line(
                        f"  [TASK ROUTER] ⚠️  skip Pass B/C — Analyzer bilang "
                        f"need_tool=False tapi Gate1/DG mau explore "
                        f"{_precheck_result.get('categories')} (disagreement, "
                        f"percaya Analyzer — cross-check gratis, sudah resolve paralel)"
                    )
                else:
                    router_calls = _tr_execute_categories(
                        _precheck_result,
                        user_mem.user_id, uname, user_input,
                        CHARACTER.get("prompts", {}), _llm_call, dbg=_dbg,
                    )
                    _dbg.line(f"  [TASK ROUTER] → {len(router_calls)} call(s): {[c.get('f') for c in router_calls]}")
            else:
                router_calls = _task_router_pass(
                    user_id    = user_mem.user_id,
                    username   = uname,
                    user_input = user_input,
                    stall_callback = _fire_stall_once,
                )
                _dbg.line(f"  [TASK ROUTER] (fallback sekuensial) → {len(router_calls)} call(s): {[c.get('f') for c in router_calls]}")

            if router_calls:
                # ── PASS 2: Execute hasil router ──────────────────────────────
                _dbg.log_step(2, "EXECUTE ROUTER CALLS", f"{len(router_calls)} tool(s)")
                exec_parts: List[str] = []
                for call in router_calls:
                    fname = call.get("f", "")
                    args  = call.get("a", {}) or {}
                    _dbg.line(f"  [EXEC] f={fname} a={args}")
                    try:
                        result = executor._dispatch(fname, args)
                        _dbg.line(f"  [EXEC OK] {fname} → {str(result)[:120]}")
                        if result and str(result).strip() not in ("", "N/A", "no update"):
                            exec_parts.append(f"{fname}: {result}")
                    except Exception as e:
                        _dbg.log_error(f"execute {fname}", e)
                raw_tool_data = " | ".join(exec_parts)
                _dbg.line(f"  [EXEC RESULT] {raw_tool_data[:200]}")

            else:
                # ── Tidak ada tool dari router → fallback ke ReAct loop ────────
                _dbg.log_step(0, "REACT LOOP (fallback)", "router tidak pilih tool → ReAct reasoning")
                tool_bus  = RouterExecutorToolBus(executor, user_mem.user_id, is_admin)
                calls_log = _react_agent_loop(
                    user_input       = user_input,
                    history_text     = history_text,
                    current_user_id  = user_mem.user_id,
                    is_admin         = is_admin,
                    tool_bus         = tool_bus,
                    max_steps        = 4,
                    active_command   = mm.command or "",
                )
                obs_parts = [
                    c["obs"] for c in calls_log
                    if c["tool"] in ("lookup", "mutate", "admin_action") and c.get("obs")
                ]
                raw_tool_data = " | ".join(obs_parts)
                _dbg.line(f"  [REACT CALLS] {calls_log}")
                _dbg.line(f"  [REACT RESULT] {raw_tool_data[:200]}")

        # ── Sync user_mem dari cache setelah tool execution ───────────────
        _cached = get_user_manager()._cache.get(user_mem.user_id)
        if _cached is not None and _cached is not user_mem:
            user_mem.data     = _cached.data
            user_mem.username = _cached.username
            _dbg.line(f"  [SYNC] user_mem di-sync dari cache: username={user_mem.username}")

        # ── Update Working Memory: last_tool ──────────────────────────────
        if _MEMORY_SYSTEM_AVAILABLE:
            try:
                _rc = locals().get("router_calls", [])
                if _rc:
                    _wm.update(user_mem.user_id, char_id,
                               last_tool=_rc[0].get("f",""),
                               last_action="tool_execute")
            except Exception:
                pass

    # ── PASS 4: Data Summarizer ────────────────────────────────────────────
    if raw_tool_data:
        data_summary = _data_summarizer(raw_tool_data, user_input)
    else:
        data_summary = ""

    # ── Refresh uname setelah sync — supaya Soul & history turn ini
    # langsung pakai nickname baru (bukan baru turn berikutnya). ─────────
    uname = user_mem.get_display_name() or uname

    # ── PASS 5: CMD Interpret — resolve mm.command mentah jadi directive ──
    cmd_dir = _cmd_interpret(mm.command or "")

    # ── PASS 6: Identity + Context Merge ───────────────────────────────────
    relation  = user_mem.get_romance_status() or "stranger"
    directive = _identity_context_merge(uname, relation, cmd_dir, data_summary)

    # ── PASS 7: Soul Generate v3 ────────────────────────────────────────────
    char_name_str = CHARACTER.get("name") or char_name or "Alfa"
    _dbg.log_pipeline_state(
        "PRE-SOUL STATE",
        f"user        : {uname}\n"
        f"relation    : {relation}\n"
        f"cmd_active  : {mm.command or '(none)'}\n"
        f"cmd_dir     : {cmd_dir}\n"
        f"data_summary: {data_summary or '(none)'}\n"
        f"directive   : {directive}\n"
        f"raw_tool    : {raw_tool_data[:120] or '(none)'}"
    )

    # ── Context Composer: pilih memory relevan berdasarkan ConvState ────────
    _cx_ctx    = None
    _soul_mode = "lite"  # default lite (bukan deep) — model kecil butuh token efisien
    if _MEMORY_SYSTEM_AVAILABLE and _conv_state is not None:
        try:
            char_id    = CHARACTER.get("id") or char_name or "default"
            _cx_ctx    = _cx_compose(user_mem.user_id, char_id, _conv_state,
                                     tool_output="", char_dir=_ACTIVE_CHAR_DIR,
                                     llm_call=_llm_call, user_input=user_input)
            _soul_mode = _cx_ctx["mode"]
            _dbg.line(f"  [CTX COMPOSER] {_cx_summary_line(_cx_ctx)}")
        except Exception as e:
            _dbg.log_error("context_composer.compose", e)

    soul_ctx  = build_soul_ctx_v3(
        directive, history, uname, user_input, char_name_str,
        memory_context=_cx_ctx["full_context"] if _cx_ctx else "",
    )
    soul_text = _soul_pass_v3(soul_ctx, complexity=_soul_mode)
    _dbg.line(f"  [SOUL TEXT] {soul_text[:120]}")

    # ── PASS 8: Soul Validate + Auto-fix ─────────────────────────────────
    validate = _soul_validate(soul_text, char_name_str)
    if not validate["ok"]:
        issues = validate["issues"]
        # Coba fix ringan dulu (hapus asterisk/markdown tanpa regen)
        fixable = all(i in ("asterisk", "markdown") for i in issues)
        if fixable:
            soul_text = _soul_fix(soul_text, issues)
            _dbg.line(f"  [PASS 8] Auto-fixed: {issues}")
        elif "too_short" not in issues:
            # Regen sekali dengan hint eksplisit ditempel ke must_use_data
            _dbg.line(f"  [PASS 8] Issues tidak bisa di-fix → regen dengan hint")
            hint_note = ", ".join(issues)
            directive_retry = dict(directive)
            directive_retry["must_use_data"] = (
                f"{directive.get('must_use_data', '')} (JANGAN: {hint_note})".strip()
            )
            soul_ctx_retry  = build_soul_ctx_v3(
                directive_retry, history, uname, user_input, char_name_str,
                memory_context=_cx_ctx["full_context"] if _cx_ctx else "",
            )
            soul_text_retry = _soul_pass_v3(soul_ctx_retry, complexity=_soul_mode)
            if soul_text_retry and len(soul_text_retry.strip()) >= 10:
                soul_text = soul_text_retry
                _dbg.line(f"  [PASS 8] Regen OK: {soul_text[:60]}")
        else:
            _dbg.line(f"  [PASS 8] too_short — pakai fallback")
            soul_text = _SOUL_FALLBACK()

    # ── ANIM (random — anim_system disabled) ──────────────────────────────
    anim_data = _random_anim_data(animations, soul_text)

    # ── STEP 5 + 6: TRANS pass + Merge (dengan streaming callback) ───────────
    anims_list = anim_data.get("anims", [{"anim": animations[0] if animations else "neutral", "pos": 0}])

    if segment_callback is not None:
        # ── Streaming path: split di Python → translate 1/1 → callback per seg ──
        sentences   = split_sentences(soul_text)
        total_sents = len(sentences)
        trans_segs: List[Dict] = []
        _dbg.log_step(5, "TRANS STREAMING", f"{total_sents} kalimat")

        for i, sent in enumerate(sentences):
            seg = _trans_single(sent)
            trans_segs.append(seg)

            ind      = seg.get("id", "").strip()
            jp       = seg.get("jp", "えっと…")
            char_pos = soul_text.find(ind)
            if char_pos == -1:
                first_words = " ".join(ind.split()[:3])
                char_pos = soul_text.find(first_words)
            if char_pos == -1:
                char_pos = 0
            anim = _get_anim_at_pos(anims_list, char_pos)
            if anim not in animations:
                anim = animations[0] if animations else "neutral"

            merged_seg = {"ind": ind, "jp": jp, "anim": anim}
            _dbg.line(f"  [TRANS] {i+1}/{total_sents} → {jp[:40]}")
            # Kirim total ke callback agar bisa tentukan kapan mulai play
            segment_callback(merged_seg, total=total_sents)

        # Merge final untuk dominant + romance points
        responses, dominant, points = _merge_segments(soul_text, anim_data, trans_segs, animations)

    else:
        # ── Batch path: backward compat (opening, closing, banter, CLI) ──────────
        trans_segs             = _trans_pass(soul_text)
        responses, dominant, points = _merge_segments(soul_text, anim_data, trans_segs, animations)

    _dbg.log_parsed(responses, points)

    # ── Update romance points ─────────────────────────────────────────────
    if points != 0:
        user_mem.add_romance_points(points)
        _dbg.line(
            f"  romance_pts {'+' if points > 0 else ''}{points} "
            f"→ {user_mem.romance_points} ({user_mem.get_romance_level()})"
        )

    # ── Simpan ke history ─────────────────────────────────────────────────
    # Pipe " | " hanya untuk fallback check — history disimpan dengan spasi biasa
    # supaya model tidak meniru "|" sebagai gaya bicara saat baca history
    ind_text      = " | ".join(r["ind"] for r in responses)
    ind_text_hist = " ".join(r["ind"] for r in responses)
    if _FALLBACK_MARKER not in ind_text:
        ch.add("user",      user_input, username=uname, user_id=user_mem.user_id)
        ch.add("assistant", ind_text_hist)
        _deduplicate_history(ch)
        _last_assistant_response = ind_text_hist
    else:
        _dbg.line("  [HISTORY] Skip — fallback tidak disimpan")

    # ── Reflection Engine (background async) ─────────────────────────────────
    # Dijalankan SETELAH response dikirim — tidak menambah latency.
    # Update: working_memory, knowledge_memory, relationship_memory, long_memory.
    if _MEMORY_SYSTEM_AVAILABLE and _FALLBACK_MARKER not in ind_text:
        try:
            char_id   = CHARACTER.get("id") or char_name or "default"
            full_hist = ch.get_messages(current_user_id=user_mem.user_id,
                                        current_username=uname)
            _reflect.run(
                user_id     = user_mem.user_id,
                char_id     = char_id,
                messages    = full_hist[-10:],
                llm_call    = _llm_call,
                tool_output = raw_tool_data,
                char_dir    = _ACTIVE_CHAR_DIR,
                async_mode  = True,   # daemon thread, tidak block
            )
        except Exception as e:
            _dbg.log_error("reflection_engine.run", e)

    user_mem.touch()
    total_elapsed = time.monotonic() - t_start
    _dbg.log_timing(total_elapsed)
    _dbg.line(
        f"\n  ✅ TOTAL: {total_elapsed:.2f}s | "
        f"tool={'yes' if raw_tool_data else 'no'} | "
        f"persona={directive.get('persona_note') or 'default'} | "
        f"soul={len(soul_text)}c | segs={len(responses)}"
    )

    return responses, dominant

def build_opening_prompt(char_name: str, char: Dict) -> str:
    """Build soul ctx for stream opening — new single-prompt format."""
    return (
        "[Identity]\n"
        "Target_User: SYSTEM\n"
        "Relation: stranger\n"
        "\n"
        "[CMD]\n"
        "none\n"
        "\n"
        "[HISTORY]\n"
        "(none)\n"
        "\n"
        "[TASK_RESULT]\n"
        "status: null\n"
        "result: null\n"
        "\n"
        "[Chat]\n"
        "SYSTEM: Kamu baru memulai live streaming TikTok. "
        "Buat sambutan hangat dan semangat yang mengajak penonton berinteraksi. "
        "Minimal 3 kalimat, sesuai kepribadian malu-malu kamu."
    )

def build_closing_prompt(char_name: str, char: Dict, tracker_summary: str = "") -> str:
    """Build soul ctx for stream closing — new single-prompt format."""
    summary_part = f"\nData sesi:\n{tracker_summary}" if tracker_summary else ""
    return (
        "[Identity]\n"
        "Target_User: SYSTEM\n"
        "Relation: close_friend\n"
        "\n"
        "[CMD]\n"
        "none\n"
        "\n"
        "[HISTORY]\n"
        "(none)\n"
        "\n"
        "[TASK_RESULT]\n"
        "status: null\n"
        "result: null\n"
        "\n"
        "[Chat]\n"
        f"SYSTEM: Kamu akan mengakhiri live streaming.{summary_part} "
        "Buat penutupan tulus dan hangat, ucapkan terima kasih kepada penonton. "
        "Minimal 3 kalimat dengan nuansa malu-malu tapi tulus."
    )

def generate_from_prompt(
    prompt:    str,
    char:      Dict,
    char_name: str        = None,
    history:   List[Dict] = None,
) -> Tuple[List[Dict], str]:
    """
    Generate dari prompt khusus (opening/closing/idle).
    Menggunakan SOUL (online) → ANIM (local) → TRANS (local) pipeline.
    """
    animations = char.get("animations", ["shy", "neutral", "happy", "blush", "panicked", "focused", "shock"])
    char_name  = char_name or _ACTIVE_CHAR_NAME

    _dbg.section(f"GENERATE_PROMPT  char={char_name}")

    # SOUL pass — prompt sudah dalam format [Identity]/[CMD]/[HISTORY]/[TASK_RESULT]/[Chat]
    soul_text = _soul_pass(prompt)
    _dbg.line(f"  [SOUL TEXT] {soul_text[:100]}")

    # ANIM (random — anim_system disabled)
    anim_data = _random_anim_data(animations, soul_text)

    # TRANS pass (local)
    trans_segs = _trans_pass(soul_text)

    # Merge
    responses, dominant, _ = _merge_segments(soul_text, anim_data, trans_segs, animations)
    return responses, dominant

def generate_simple(prompt: str, max_tokens: int = 600, schema: Dict = None) -> Optional[str]:
    """Simple generate untuk banter / non-pipeline tasks."""
    try:
        resp = _llm_call(
            "react",
            messages=[{"role": "user", "content": prompt}],
            temperature=0.85,
            max_tokens=max_tokens,
        )
        return resp.choices[0].message.content
    except Exception as e:
        _dbg.log_error("generate_simple", e)
        return None


# ═══════════════════════════════════════════════════════════════════════════════
# BANTER SCHEMA + PIPELINE
# (local model generate id+text saja → ANIM → TRANS → Merge, sama seperti soul)
# ═══════════════════════════════════════════════════════════════════════════════

def make_banter_schema() -> Dict:
    """Schema sederhana: local model hanya generate id + plain Indonesian text.
    ANIM + TRANS dijalankan setelahnya via generate_banter_pipeline()."""
    return {
        "type": "json_schema",
        "json_schema": {
            "name":   "banter_generation",
            "strict": True,
            "schema": {
                "type":  "array",
                "items": {
                    "type":     "object",
                    "required": ["id", "text"],
                    "properties": {
                        "id":   {"type": "string"},
                        "text": {"type": "string"},
                    },
                    "additionalProperties": False,
                },
            },
        },
    }


def generate_banter_pipeline(
    raw_items:  List[Dict],
    char:       Dict = None,
    char_name:  str  = None,
) -> List[Dict]:
    """
    Proses list [{id, text}] dari local model melalui ANIM → TRANS → Merge,
    menghasilkan format final [{id, expression, segments: [{ind, jp, anim}]}]
    — pipeline identik dengan soul flow di full_generate.
    """
    char       = char or CHARACTER or {}
    char_name  = char_name or _ACTIVE_CHAR_NAME
    animations = char.get("animations", ["shy", "neutral", "happy", "blush", "panicked", "focused", "shock"])

    results: List[Dict] = []
    for item in raw_items:
        banter_id = item.get("id", f"b_{len(results):03d}")
        soul_text = (item.get("text") or "").strip()
        if not soul_text:
            continue

        _dbg.line(f"  [BANTER PIPELINE] id={banter_id} text={soul_text[:60]}")

        # ANIM (random — anim_system disabled)
        anim_data  = _random_anim_data(animations, soul_text)
        trans_segs = _trans_pass(soul_text)
        segments, dominant, _ = _merge_segments(soul_text, anim_data, trans_segs, animations)

        results.append({
            "id":         banter_id,
            "expression": dominant,
            "segments":   segments,
        })

    return results


# ═══════════════════════════════════════════════════════════════════════════════
# DEBUG HELPERS
# ═══════════════════════════════════════════════════════════════════════════════

def debug_show_user(user_mem: "UserMemory"):
    d = user_mem.data
    print("\n+- USER MEMORY ---------------------------------------------------")
    print(f"|  user_id      : {d.get('user_id')}")
    print(f"|  username     : {d.get('username')}")
    print(f"|  display_name : {user_mem.get_display_name()}")
    print(f"|  romance_pts  : {user_mem.romance_points}  ({user_mem.get_romance_level()})")
    print(f"|  romance_stat : {user_mem.get_romance_status() or '(kosong)'}")
    print(f"|  vip          : {d.get('vip_user', False)}")
    print(f"|  last_chat    : {user_mem.get_last_chat_ago()}")
    info = d.get("info_user", [])
    if info:
        print(f"|  info_user ({len(info)}):")
        for item in info:
            print(f"|    * {item}")
    else:
        print("|  info_user    : (kosong)")
    notes = d.get("note", [])
    if notes:
        print(f"|  notes ({len(notes)}):")
        for n in notes[-3:]:
            print(f"|    * {n['text'][:70]}  [{n['ts'][:10]}]")
    gifts = user_mem.get_gift_summary()
    if gifts:
        print(f"|  gifts        : {gifts}")
    print("+-----------------------------------------------------------------")


def debug_show_model(model_mem: ModelMemory):
    print("\n+- MODEL MEMORY --------------------------------------------------")
    print(f"|  topik   : {model_mem.topik}")
    print(f"|  role    : {model_mem.role}")
    print(f"|  style   : {model_mem.style}")
    print(f"|  command : {model_mem.command or '(kosong)'}")
    print("+-----------------------------------------------------------------")


def debug_show_history(chat_hist: ChatHistory):
    msgs = chat_hist.get_messages()
    print(f"\n+- CHAT HISTORY  ({len(msgs)} entries) ---------------------------------")
    if not msgs:
        print("|  (kosong)")
    for i, m in enumerate(msgs):
        role    = m.get("role", "?")
        content = (m.get("content") or "")[:80]
        label   = "[user]      " if role == "user" else "[assistant] "
        suffix  = "..." if len(m.get("content", "")) > 80 else ""
        print(f"|  {i+1}. {label}{content}{suffix}")
    print("+-----------------------------------------------------------------")


# ═══════════════════════════════════════════════════════════════════════════════
# CLI
# ═══════════════════════════════════════════════════════════════════════════════

def main():
    from character_manager import CharacterManager

    global _dbg
    _dbg = DebugLogger(enabled=DEBUG)

    # ── Auto-buka Settings UI (non-blocking, jalan di thread terpisah) ────────
    open_settings_async()

    mgr   = CharacterManager()
    generated = character_memory.ensure_all(mgr)
    if generated:
        print(f"[CHAR MEMORY] Auto-generated bin untuk: {generated}")
        
    chars = mgr.list_characters()
    print(f"Available: {chars}")
    if not chars:
        print("No characters found.")
        return

    name = input(f"Character [{chars[0]}]: ").strip() or chars[0]
    mgr.load(name)
    set_character(mgr.active, mgr.character, char_dir=mgr.char_dir)

    user_id  = "local_debug"
    username = input("Username [Shinri]: ").strip() or "Shinri"
    user_mem = UserMemory(user_id, username, storage_dir=MEMORY_DIR)

    print(f"\n{'='*58}")
    print(f"  {_ACTIVE_CHAR_NAME.upper()} — DEBUG CLI  (v11 · agent pipeline)")
    print(f"{'='*58}")
    print("Commands:")
    print("  #op                     opening live")
    print("  #end                    closing live + exit")
    print("  /user                   tampilkan user memory")
    print("  /status                 tampilkan model memory")
    print("  /history                tampilkan chat history")
    print("  /debug on|off           toggle debug")
    print("  /log [N]                tail N baris log")
    print("  /clearlog               hapus debug_gen.log")
    print("  #chl                    hapus chat history (clear history)")
    print("  /clearhist              hapus chat history")
    print("  /setmem nickname:Shin   set nickname")
    print("  /setmem info:suka kopi  tambah info")
    print("  /setmem note:...        tambah note")
    print("  /setmem romance:teman   set romance status")
    print("  /setmem vip:true|false  set VIP")
    print("  exit\n")

    while True:
        try:
            raw = input(f"[{username}]: ").strip()
            if not raw:
                continue

            if raw.lower() == "exit":
                break

            if raw.lower() == "#chl":
                if _chat_hist:
                    try:
                        for attr in ("_messages", "messages", "_history"):
                            if hasattr(_chat_hist, attr):
                                setattr(_chat_hist, attr, [])
                                break
                        _chat_hist.save()
                        _last_assistant_response = ""
                        print("✅ Chat history dihapus.")
                    except Exception as e:
                        print(f"Error clear history: {e}")
                continue

            if raw.lower() == "#op":
                prompt = build_opening_prompt(_ACTIVE_CHAR_NAME, CHARACTER)
                r, d   = generate_from_prompt(prompt, CHARACTER, char_name=_ACTIVE_CHAR_NAME)
                print(f"\n[{_ACTIVE_CHAR_NAME}] OPENING:")
                for i, seg in enumerate(r, 1):
                    print(f"  seg{i} ({seg['anim']}): {seg['ind']}")
                    print(f"          JP: {seg['jp']}")
                continue

            if raw.lower() == "#end":
                prompt = build_closing_prompt(_ACTIVE_CHAR_NAME, CHARACTER)
                r, d   = generate_from_prompt(prompt, CHARACTER, char_name=_ACTIVE_CHAR_NAME)
                print(f"\n[{_ACTIVE_CHAR_NAME}] CLOSING:")
                for i, seg in enumerate(r, 1):
                    print(f"  seg{i} ({seg['anim']}): {seg['ind']}")
                    print(f"          JP: {seg['jp']}")
                break

            if raw.lower() == "/user":
                debug_show_user(user_mem); continue
            if raw.lower() == "/status":
                if _model_mem: debug_show_model(_model_mem); continue
            if raw.lower() == "/history":
                if _chat_hist: debug_show_history(_chat_hist); continue
            if raw.lower() == "/debug on":
                _dbg.enabled = True;  print("Debug: ON"); continue
            if raw.lower() == "/debug off":
                _dbg.enabled = False; print("Debug: OFF"); continue
            if raw.lower().startswith("/log"):
                parts = raw.split()
                n     = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 60
                print(_dbg.tail_log(n)); continue
            if raw.lower() == "/clearlog":
                if os.path.exists(_dbg.log_file):
                    os.remove(_dbg.log_file)
                    print(f"Deleted: {_dbg.log_file}")
                continue
            if raw.lower() == "/clearhist":
                if _chat_hist:
                    try:
                        for attr in ("_messages", "messages", "_history"):
                            if hasattr(_chat_hist, attr):
                                setattr(_chat_hist, attr, [])
                                break
                        _chat_hist.save()
                        #global _last_assistant_response
                        _last_assistant_response = ""
                        print("Chat history & last response dihapus.")
                    except Exception as e:
                        print(f"Error clear history: {e}")
                continue
            if raw.lower().startswith("/setmem "):
                parts = raw[len("/setmem "):].strip()
                if ":" not in parts:
                    print("Format: /setmem <type>:<value>"); continue
                key, val = parts.split(":", 1)
                key = key.strip().lower(); val = val.strip()
                if not val:
                    print("Value tidak boleh kosong."); continue
                if   key == "nickname": user_mem.update_username(val); print(f"[OK] nickname → '{val}'")
                elif key == "info":     user_mem.add_info(val);               print(f"[OK] info +'{val}'")
                elif key == "note":     user_mem.add_note(val);               print(f"[OK] note +'{val}'")
                elif key == "romance":  user_mem.set_romance_status(val);     print(f"[OK] romance → '{val}'")
                elif key == "vip":
                    flag = val.lower() in ("true", "1", "yes")
                    user_mem.set_vip(flag)
                    print(f"[OK] vip → {flag}")
                else:
                    print(f"Key tidak dikenal: '{key}'")
                continue

            print("Berpikir...", end="\r", flush=True)

            def _cli_stall_print(seg: Dict):
                # PATCH: caller CLI ini sekarang benar-benar wire stall_callback
                # ke full_generate() — sebelumnya parameter ini tidak pernah
                # diisi sama sekali, jadi filler message selalu no-op meskipun
                # task_router sudah trigger duluan (root cause laporan "gak
                # kepanggil"). Cetak segera, jangan tunggu jawaban final.
                print(f"\n[{_ACTIVE_CHAR_NAME}] (mohon tunggu...)")
                print(f"  {seg.get('anim','neutral')}: {seg.get('ind','')}")
                print(f"       JP: {seg.get('jp','')}")
                print("Memproses...", end="\r", flush=True)

            responses, dominant = full_generate(
                raw, user_mem, username=username,
                stall_callback=_cli_stall_print,
            )

            print(f"\n[{_ACTIVE_CHAR_NAME}]")
            for i, r in enumerate(responses, 1):
                print(f"  seg{i} ({r['anim']}): {r['ind']}")
                print(f"          JP: {r['jp']}")
            print(f"  dominant: {dominant}")
            if _model_mem:
                print(f"  topic   : {_model_mem.topik}")
                if _model_mem.command:
                    print(f"  cmd     : {_model_mem.command}")
            print(f"  romance : {user_mem.romance_points}pts ({user_mem.get_romance_level()})")

        except KeyboardInterrupt:
            break
        except Exception as e:
            print(f"\n[ERROR] {e}")
            if DEBUG:
                import traceback; traceback.print_exc()


if __name__ == "__main__":
    main()
    