"""
context_composer.py — Context Composer.

Sebelum Soul dijalankan, Composer memilih memory mana yang relevan
berdasarkan ConversationState saat ini. Tidak dump semua memory —
hanya yang relevan dengan topik + frame saat ini.

Output: string context yang siap dimasukkan ke Soul prompt.
"""
from __future__ import annotations
from typing import Dict, Optional

from conversation_state import ConversationState, complexity_to_mode
import working_memory      as wm_mod
import relationship_memory as rm_mod
import knowledge_memory    as km_mod
import long_memory         as lm_mod


# Context budget per mode (dalam chars, approx):
_BUDGET: Dict[str, int] = {
    "nano":   500,
    "lite":   800,
    "normal": 1500,
    "deep":   3000,
}


def compose(
    user_id:     str,
    char_id:     str,
    state:       ConversationState,
    tool_output: str = "",
    override_mode: Optional[str] = None,
) -> Dict:
    """
    Build context dict berdasarkan ConversationState.
    Return:
      {
        "mode": "nano"|"lite"|"normal"|"deep",
        "max_tokens": int,
        "working_memory": str,
        "relationship": str,
        "knowledge": str,
        "long_memory": str,
        "tool_output": str,
        "full_context": str,   # gabungan siap pakai
      }
    """
    mode   = override_mode or complexity_to_mode(state.complexity)
    budget = _BUDGET.get(mode, 1500)

    parts  = []
    result = {
        "mode":           mode,
        "max_tokens":     _mode_to_max_tokens(mode),
        "working_memory": "",
        "relationship":   "",
        "knowledge":      "",
        "long_memory":    "",
        "tool_output":    "",
        "full_context":   "",
    }

    # ── Tool output (selalu masuk jika ada) ──────────────────────────────────
    if tool_output:
        result["tool_output"] = tool_output
        parts.append(f"[Data]\n{tool_output[:400]}")

    # ── Working Memory ────────────────────────────────────────────────────────
    # Selalu masuk kecuali nano (tidak ada context)
    if mode != "nano":
        wm = wm_mod.load(user_id)
        wm_str = wm.summary()
        if wm_str != "(empty)":
            result["working_memory"] = wm_str
            parts.append(f"[Working Memory]\n{wm_str}")

    # ── Relationship Memory ────────────────────────────────────────────────────
    # Selalu masuk di lite+ (karena nickname / preferensi dasar penting)
    if mode in ("lite", "normal", "deep"):
        rm = rm_mod.load(user_id, char_id)
        rm_str = rm.summary_for_context()
        if rm_str != "(belum ada data relasi)":
            result["relationship"] = rm_str
            parts.append(f"[Profil User]\n{rm_str}")

    # ── Knowledge Memory ──────────────────────────────────────────────────────
    # Masuk di normal+ ATAU jika frame adalah TASK/KNOWLEDGE/FOLLOW_UP
    if mode in ("normal", "deep") or state.frame in ("TASK", "KNOWLEDGE", "FOLLOW_UP"):
        ks = km_mod.load(user_id)
        km_str = ks.summary_for_context(topic_hint=state.topic, max_items=5 if mode=="deep" else 3)
        if km_str != "(belum ada knowledge)":
            result["knowledge"] = km_str
            parts.append(km_str)

    # ── Long Memory ────────────────────────────────────────────────────────────
    # Hanya deep, atau EMOTIONAL/ROLEPLAY (butuh konteks panjang)
    if mode == "deep" or state.frame in ("EMOTIONAL", "ROLEPLAY"):
        lm = lm_mod.load(user_id)
        lm_str = lm.summary_for_context(topic_hint=state.topic)
        if lm_str != "(belum ada long memory)":
            result["long_memory"] = lm_str
            parts.append(lm_str)

    # ── Pending action hint ────────────────────────────────────────────────────
    if state.pending_action and not state.pending_action.is_empty():
        pa_hint = f"[Menunggu Konfirmasi] {state.pending_action.description}"
        parts.append(pa_hint)

    # ── Gabungkan + trim sesuai budget ────────────────────────────────────────
    full = "\n\n".join(parts)
    if len(full) > budget:
        full = full[:budget] + "\n...(truncated)"
    result["full_context"] = full

    return result


def _mode_to_max_tokens(mode: str) -> int:
    return {"nano": 100, "lite": 300, "normal": 800, "deep": 1208}.get(mode, 800)


def compose_summary_line(ctx: Dict) -> str:
    """Satu baris untuk debug log."""
    return (
        f"mode={ctx['mode']} max_tok={ctx['max_tokens']} "
        f"wm={'✓' if ctx['working_memory'] else '✗'} "
        f"rel={'✓' if ctx['relationship'] else '✗'} "
        f"know={'✓' if ctx['knowledge'] else '✗'} "
        f"long={'✓' if ctx['long_memory'] else '✗'} "
        f"tool={'✓' if ctx['tool_output'] else '✗'} "
        f"len={len(ctx['full_context'])}"
    )
