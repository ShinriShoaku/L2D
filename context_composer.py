"""
context_composer.py — Context Composer.

Sebelum Soul dijalankan, Composer memilih memory mana yang relevan
berdasarkan ConversationState saat ini. Tidak dump semua memory —
hanya yang relevan dengan topik + frame saat ini.

Output: string context yang siap dimasukkan ke Soul prompt.

--- PATCH: integrasi character_memory.py ---
Ditambahkan blok "character_self" yang menyisipkan profil diri karakter
(tanggal lahir, keinginan, hobi, dll) ke context, supaya kalau user
bertanya "apa keinginanmu?" AI otomatis punya datanya tanpa mengarang.

--- PATCH v2: fix "data bin karakter tidak terbaca" di mode nano ---
Root cause: banyak pertanyaan identitas singkat ("ulang tahunmu kapan?")
dapat skor CCS rendah → mode nano, dan blok character_self hanya ikut di
mode lite/normal/deep — jadi ke-skip total walau character_memory.bin
sudah ter-generate dan terisi benar.

Fix modular: conversation_state.analyze() sekarang punya task terpisah
`need_self_memory` (di samping `need_memory` yang sudah ada untuk data
USER) — analyzer secara eksplisit menandai apakah chat menyinggung
identitas/sifat KARAKTER. Composer di sini tinggal baca flag itu sebagai
override per mode. Dua kebutuhan (data user vs data karakter) sengaja
dipisah jadi dua flag supaya masing-masing gate tetap sederhana dan
akurasinya bisa diaudit sendiri-sendiri, tidak saling menutupi.
"""

from __future__ import annotations

from typing import Dict, Optional

from conversation_state import ConversationState, complexity_to_mode
import working_memory as wm_mod
import relationship_memory as rm_mod
import knowledge_memory as km_mod
import long_memory as lm_mod
import character_memory as cmem_mod  # PATCH

# Context budget per mode (dalam chars, approx):
# NOTE: nano dinaikkan 500→700 karena character_self sekarang SELALU
# disertakan (lihat PATCH di compose()), jadi budget nano perlu sedikit
# lebih longgar supaya tidak langsung ke-truncate begitu need_memory juga
# memicu blok lain di mode ini.
_BUDGET: Dict[str, int] = {
    "nano": 700,
    "lite": 800,
    "normal": 1500,
    "deep": 3000,
}

# --- PATCH v3.1: fix "general/sapaan tetap dump semua field" ---
# Kasus: pesan cuma sapaan ("halo liana siang") → analyzer benar mendeteksi
# self_memory_field="general" (memang bukan pertanyaan spesifik). Tapi
# fallback lama untuk kasus "tidak ada field spesifik" SELALU manggil
# cm.summary_for_context() PENUH (nama+lahir+zodiak+kepribadian+suka+dst),
# baru dipotong 300 char kalau mode nano — jadi tetap boros karena field-nya
# sudah ke-render duluan sebelum dipotong (cuma dipotong dari BELAKANG string,
# bukan dari JUMLAH field-nya).
#
# Fix: untuk kasus "general"/tidak spesifik, ukuran ditentukan oleh MODE dari
# AWAL (bukan truncate belakangan) — nano cuma dapat 1 field inti (nama),
# lite/normal dapat beberapa field inti, dan hanya "deep" yang benar-benar
# dapat ringkasan penuh (karena budget deep memang didesain besar & biasanya
# dipakai untuk EMOTIONAL/ROLEPLAY yang butuh konteks kaya).
_CORE_IDENTITY_FIELDS: Dict[str, list] = {
    "nano":   ["full_name"],
    "lite":   ["full_name", "personality"],
    "normal": ["full_name", "personality", "likes"],
}


def compose(
    user_id: str,
    char_id: str,
    state: ConversationState,
    tool_output: str = "",
    override_mode: Optional[str] = None,
    char_dir: Optional[str] = None,
    llm_call=None,
    user_input: str = "",
) -> Dict:
    """
    Build context dict berdasarkan ConversationState.

    char_dir: folder karakter aktif (mis. characters/liana/), didapat dari
        CharacterManager.char_dir. WAJIB diteruskan kalau kamu mau
        character_self terisi data yang benar — tanpa ini, character_memory
        akan fallback ke state/character_memory.bin (file global lama, BUKAN
        file per-karakter yang di-generate character_manager.py), sehingga
        selalu terbaca kosong walau bin karakter sudah ada.

    user_input: teks pesan user saat ini (opsional). PATCH v3: dipakai
        sebagai sinyal tambahan untuk guess_field()/resolve_relevant_fields()
        kalau state.self_memory_field dari analyzer kosong/"general" — supaya
        deteksi field karakter makin akurat. Kalau tidak diisi, fallback pakai
        state.topic (tetap jalan, cuma kurang presisi).

    Return:
    {
        "mode": "nano"|"lite"|"normal"|"deep",
        "max_tokens": int,
        "working_memory": str,
        "relationship": str,
        "knowledge": str,
        "long_memory": str,
        "character_self": str,   # PATCH: profil diri karakter (wants, birthday, dll)
        "tool_output": str,
        "full_context": str,     # gabungan siap pakai
    }
    """
    mode = override_mode or complexity_to_mode(state.complexity)
    budget = _BUDGET.get(mode, 1500)
    parts = []

    result = {
        "mode": mode,
        "max_tokens": _mode_to_max_tokens(mode),
        "working_memory": "",
        "relationship": "",
        "knowledge": "",
        "long_memory": "",
        "character_self": "",  # PATCH
        "tool_output": "",
        "full_context": "",
        "need_memory": False,       # PATCH: diisi di bawah, untuk debug/audit
        "need_self_memory": False,  # PATCH: diisi di bawah, untuk debug/audit
        "self_memory_field": getattr(state, "self_memory_field", "") or "",  # PATCH v3
    }

    # ── Tool output (selalu masuk jika ada) ──────────────────────────────────
    if tool_output:
        result["tool_output"] = tool_output
        parts.append(f"[Data]\n{tool_output[:400]}")

    # ── Memory-need flags ────────────────────────────────────────────────── PATCH
    # Dua task terpisah dari analyzer (conversation_state.analyze), masing-masing
    # dengan tanggung jawab jelas — supaya gating di sini tidak berbelit dan
    # akurasinya bisa diaudit per-flag:
    #   need_memory      → chat ini butuh data memory tentang USER (working/
    #                       relationship/knowledge memory)
    #   need_self_memory → chat ini butuh data memory tentang KARAKTER AI sendiri
    #                       (identitas: nama, tanggal lahir, suka, hobi, dst — lihat
    #                       character_memory.py)
    # Sebelumnya field ini dihitung analyzer tapi TIDAK DIPAKAI composer sama
    # sekali, jadi di mode "nano" (skor CCS rendah — termasuk pertanyaan identitas
    # singkat seperti "ulang tahunmu kapan?") kedua blok ini ke-skip walau
    # analyzer sudah tahu jelas dibutuhkan. Sekarang keduanya dipakai sebagai
    # override eksplisit per mode.
    need_memory      = bool(getattr(state, "need_memory", False))
    need_self_memory = bool(getattr(state, "need_self_memory", False))
    result["need_memory"]      = need_memory
    result["need_self_memory"] = need_self_memory

    # ── Character Self Memory ────────────────────────────────────────────── PATCH
    # Identitas dasar karakter (nama, tanggal lahir, keinginan, hobi, dll).
    # Masuk di mode lite+ (identitas dasar sering ditanya di request yang lebih
    # kompleks), ATAU kapan pun analyzer menandai need_self_memory=True — inilah
    # yang membuat pertanyaan identitas singkat di mode nano tetap kebagian data,
    # tanpa perlu memaksa SEMUA request mode nano membawa profil karakter.
    #
    # --- PATCH v3: targeted field retrieval (hemat token) ---
    # Sebelumnya blok ini SELALU tarik cm.summary_for_context() penuh (semua
    # field karakter) begitu kondisi di atas terpenuhi. Sekarang kita coba
    # persempit dulu ke field yang benar-benar relevan dengan pertanyaan,
    # baru fallback ke summary penuh kalau memang tidak bisa dipersempit:
    #   1. state.self_memory_field dari analyzer (sudah dari 1 LLM call yg
    #      sama, nol biaya tambahan) — kalau spesifik (bukan ""/"general"),
    #      langsung pakai.
    #   2. guess_field() lokal terhadap topic/user_input (tanpa LLM call).
    #   3. resolve_relevant_fields() — probe field satu-satu ke LLM murah,
    #      stop di match pertama (lihat character_memory.py). Cuma jalan
    #      kalau llm_call tersedia & mode bukan nano (biar tidak nambah
    #      latency di request paling ringan).
    #   4. Kalau semua gagal → fallback summary_for_context() penuh, SAMA
    #      seperti perilaku lama (tidak ada regresi).
    if mode in ("lite", "normal", "deep") or need_self_memory:
        cm = cmem_mod.load(char_id, char_dir=char_dir)
        probe_text = user_input or state.topic or ""

        field_keys: list = []
        detected = (getattr(state, "self_memory_field", "") or "").strip().lower()
        if detected and detected != "general":
            field_keys = [detected]

        cm_str = cm.field_summary_for_context(field_keys) if field_keys else "(belum ada data karakter)"

        if cm_str == "(belum ada data karakter)":
            guessed = cmem_mod.guess_field(probe_text)
            if guessed:
                cm_str = cm.field_summary_for_context([guessed])

        if cm_str == "(belum ada data karakter)" and need_self_memory and mode != "nano":
            probed_keys = cmem_mod.resolve_relevant_fields(
                cm, probe_text, llm_call=llm_call, dbg=None,
            )
            if probed_keys:
                cm_str = cm.field_summary_for_context(probed_keys)

        if cm_str == "(belum ada data karakter)":
            # Tier terakhir: dulu selalu cm.summary_for_context() PENUH lalu
            # dipotong belakangan di mode nano — itu penyebab "sapaan biasa
            # tetap kirim semua field" yang dilaporkan. Sekarang: kalau ini
            # memang kasus "general"/tidak spesifik (bukan gagal deteksi tapi
            # SENGAJA general, mis. sapaan), ukurannya proporsional ke mode.
            # Hanya mode "deep" yang dapat ringkasan penuh.
            if mode == "deep":
                cm_str = cm.summary_for_context()
            else:
                core_fields = _CORE_IDENTITY_FIELDS.get(mode, ["full_name"])
                cm_str = cm.field_summary_for_context(core_fields)

        if cm_str != "(belum ada data karakter)":
            if mode == "nano" and len(cm_str) > 300:
                cm_str = cm_str[:300].rstrip() + "\n...(dipotong, mode nano)"
            result["character_self"] = cm_str
            parts.append(cm_str)

    # ── Working Memory ────────────────────────────────────────────────────────
    # Masuk kecuali nano — kecuali analyzer eksplisit menandai need_memory=True
    # (chat menyinggung goal/aksi/data USER yang sedang berjalan).
    if mode != "nano" or need_memory:
        wm = wm_mod.load(user_id)
        wm_str = wm.summary()
        if wm_str != "(empty)":
            result["working_memory"] = wm_str
            parts.append(f"[Working Memory]\n{wm_str}")

    # ── Relationship Memory ────────────────────────────────────────────────────
    # Selalu masuk di lite+, atau di nano jika need_memory=True.
    if mode in ("lite", "normal", "deep") or (mode == "nano" and need_memory):
        rm = rm_mod.load(user_id, char_id)
        rm_str = rm.summary_for_context()
        if rm_str != "(belum ada data relasi)":
            result["relationship"] = rm_str
            parts.append(f"[Profil User]\n{rm_str}")

    # ── Knowledge Memory ──────────────────────────────────────────────────────
    # Masuk di normal+, ATAU jika frame TASK/KNOWLEDGE/FOLLOW_UP, ATAU
    # need_memory=True walau mode nano.
    if (
        mode in ("normal", "deep")
        or state.frame in ("TASK", "KNOWLEDGE", "FOLLOW_UP")
        or need_memory
    ):
        ks = km_mod.load(user_id)
        km_str = ks.summary_for_context(topic_hint=state.topic, max_items=5 if mode == "deep" else 3)
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
        f"char={'✓' if ctx['character_self'] else '✗'} "  # PATCH
        f"wm={'✓' if ctx['working_memory'] else '✗'} "
        f"rel={'✓' if ctx['relationship'] else '✗'} "
        f"know={'✓' if ctx['knowledge'] else '✗'} "
        f"long={'✓' if ctx['long_memory'] else '✗'} "
        f"tool={'✓' if ctx['tool_output'] else '✗'} "
        f"[need_mem={ctx.get('need_memory')} need_self_mem={ctx.get('need_self_memory')} "  # PATCH
        f"self_field={ctx.get('self_memory_field') or '-'}] "
        f"len={len(ctx['full_context'])}"
    )
