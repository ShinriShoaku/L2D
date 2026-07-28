"""
reflection_engine.py — Reflection Engine (Background Async).

Dijalankan SETELAH Soul mengirim respons (non-blocking, daemon thread).
Input : last N messages + current state + tool results
Output: update ke semua memory layer (working/relationship/knowledge/long)

Prompt kecil → JSON → dispatch ke masing-masing memory module.
Tidak menambah latency ke user karena berjalan di background.

--- PATCH: character_self_facts (konsistensi improvisasi karakter) ---
Kalau Soul ditanya soal dirinya sendiri (mis. "suka apa?") dan bin karakter
belum ada datanya, Soul akan MENGARANG jawaban spesifik (mis. "suka baca
novel judul X"). Tanpa disimpan, pertanyaan yang sama nanti bisa dijawab
BEDA (Soul ngarang lagi). Reflection sekarang juga mengekstrak detail
spesifik yang baru diimprovisasi karakter dan menuliskannya ke
character_memory.bin (via character_memory.set_field/add_want/add_fact),
supaya jawaban berikutnya konsisten. Fakta yang SUDAH ada di bin (dikirim
sebagai hint ke prompt reflect) TIDAK ditimpa — field skalar (birthday, dst)
hanya diisi kalau masih kosong, field list (likes, dst) di-append kalau
belum ada.
"""
from __future__ import annotations
import json, re, threading, time
from typing import Dict, List, Optional

import working_memory    as wm_mod
import relationship_memory as rm_mod
import knowledge_memory  as km_mod
import long_memory       as lm_mod
import character_memory  as cmem_mod  # PATCH: simpan fakta diri karakter yg diimprovisasi

# ── Prompt ────────────────────────────────────────────────────────────────────
_REFLECT_SYS = """\
Analisa percakapan ini dan ekstrak informasi yang perlu disimpan.
Jawab HANYA JSON satu baris.

Format:
{
  "new_facts":[{"category":"hardware|software|project|preference|other","key":"...","value":"...","confidence":0.0-1.0}],
  "new_lessons":["..."],
  "relationship_update":{"trust_delta":0,"romance_delta":0,"preferred_name":"","relation_note":""},
  "experience":{"title":"","description":"","topics":[],"importance":0.0-1.0},
  "summary":"ringkasan singkat sesi ini (max 100 kata)",
  "working_memory":{"current_goal":"","last_action":"","awaiting_reply":"","short_context":{}},
  "character_self_facts":[{"field":"likes|dislikes|hobbies|fears|wants|backstory|custom","text":"..."}]
}

Aturan:
- new_facts: hanya fakta konkret yang USER nyatakan (OS, GPU, framework, suka/tidak suka)
- new_lessons: hal yang AI harus ingat ("jangan panggil user bos", "user suka jawaban singkat")
- relationship_update: perubahan trust/romance dari sesi ini. 0 jika tidak ada perubahan
- experience: isi jika ada kejadian signifikan. title kosong jika tidak ada
- summary: wajib diisi. ringkasan netral apa yang dibicarakan
- working_memory: update jika ada goal baru, aksi terakhir, atau menunggu konfirmasi
- character_self_facts: array KOSONG [] kecuali karakter AI (BUKAN user) menyebutkan DETAIL
  SPESIFIK baru tentang DIRINYA SENDIRI di respons terakhirnya — mis. judul buku favorit, nama
  tempat, nama hewan peliharaan, atau detail konkret lain yang kalau ditanya lagi nanti HARUS
  konsisten (bukan diimprovisasi ulang jadi beda). JANGAN catat generalisasi yang sudah jelas
  dari kepribadian dasar karakter, dan JANGAN catat ulang fakta yang SUDAH ada di
  "[Fakta karakter yang sudah tercatat]" di bawah — itu daftar yang sudah tersimpan.
  field = kategori paling cocok (pakai "custom" kalau tidak ada yang cocok).
  text = detail SPESIFIK yang disebutkan, ringkas (max ~15 kata).
- Jangan tulis apapun selain JSON
"""

def _build_reflect_input(
    messages:    List[Dict],
    tool_output: str = "",
    n:           int = 10,
    known_char_facts: str = "",
) -> str:
    lines = []
    recent = messages[-n:]
    for m in recent:
        role    = "User" if m.get("role") == "user" else "AI"
        content = (m.get("content") or "")[:200]
        lines.append(f"{role}: {content}")
    if tool_output:
        lines.append(f"[Tool Output] {tool_output[:200]}")
    if known_char_facts:
        lines.append(f"\n[Fakta karakter yang sudah tercatat]\n{known_char_facts}")
    return "\n".join(lines)


_CHARMEM_LIST_FIELDS   = {"likes", "dislikes", "hobbies", "fears", "personality"}
_CHARMEM_SCALAR_FIELDS = {"full_name", "birthday", "zodiac", "age", "backstory"}

def _dispatch_reflection(
    user_id:  str,
    char_id:  str,
    result:   Dict,
    char_dir: Optional[str] = None,
):
    """Apply reflection result ke semua memory modules."""
    # ── Working Memory ────────────────────────────────────────────────────────
    wm_update = result.get("working_memory", {})
    if wm_update:
        wm = wm_mod.load(user_id)
        if wm_update.get("current_goal"):  wm.current_goal   = wm_update["current_goal"]
        if wm_update.get("last_action"):   wm.last_action    = wm_update["last_action"]
        if wm_update.get("awaiting_reply"): wm.awaiting_reply = wm_update["awaiting_reply"]
        ctx_update = wm_update.get("short_context", {})
        if ctx_update: wm.short_context.update(ctx_update)
        wm_mod.save(wm)

    # ── Knowledge Memory ──────────────────────────────────────────────────────
    for fact in result.get("new_facts", []):
        cat  = str(fact.get("category","other"))
        key  = str(fact.get("key",""))
        val  = str(fact.get("value",""))
        conf = float(fact.get("confidence", 0.6))
        if key and val:
            km_mod.add_fact(user_id, cat, key, val, conf)

    # ── Relationship Memory ───────────────────────────────────────────────────
    ru  = result.get("relationship_update", {})
    rm  = rm_mod.load(user_id, char_id)
    td  = int(ru.get("trust_delta", 0))
    rod = int(ru.get("romance_delta", 0))
    if td:  rm.update_trust(td)
    if rod: rm.update_romance(rod)
    if ru.get("preferred_name"):    rm.preferred_name = ru["preferred_name"]
    for lesson in result.get("new_lessons", []):
        rm.add_lesson(lesson)
    rm_mod.save(rm)

    # ── Long Memory ───────────────────────────────────────────────────────────
    lm = lm_mod.load(user_id)
    exp = result.get("experience", {})
    if exp.get("title"):
        lm.add_experience(
            title       = exp["title"],
            description = exp.get("description",""),
            topics      = exp.get("topics",[]),
            importance  = float(exp.get("importance", 0.5)),
        )
    summary = result.get("summary","")
    if summary:
        lm.add_summary(summary)
    lm_mod.save(lm)

    # ── Character Self Memory ─────────────────────────────────────────────── PATCH
    # Kalau Soul mengimprovisasi detail spesifik soal DIRINYA (mis. ditanya
    # "suka apa" dan bin belum ada isinya → Soul ngarang "baca novel judul
    # X"), simpan detail itu ke character_memory.bin supaya kalau ditanya
    # lagi nanti jawabannya TETAP SAMA, bukan improvisasi baru tiap kali.
    char_facts = result.get("character_self_facts", [])
    if char_facts:
        cm = cmem_mod.load(char_id, char_dir=char_dir)
        changed = False
        for cf in char_facts:
            fkey = str(cf.get("field", "custom") or "custom").strip().lower()
            text = str(cf.get("text", "") or "").strip()
            if not text:
                continue

            if fkey == "wants":
                cm.add_want(text)
                changed = True
            elif fkey in _CHARMEM_LIST_FIELDS:
                existing = cm.get_field(fkey, [])
                if isinstance(existing, str):
                    existing = [existing] if existing else []
                elif not isinstance(existing, list):
                    existing = []
                if text not in existing:
                    existing.append(text)
                    cm.set_field(fkey, existing)
                    changed = True
            elif fkey in _CHARMEM_SCALAR_FIELDS:
                # Field "canon" (mis. tanggal lahir) HANYA diisi kalau masih
                # kosong — jangan overwrite fakta yang sudah ditetapkan.
                if not cm.get_field(fkey):
                    cm.set_field(fkey, text)
                    changed = True
            else:
                cm.add_fact(text)  # custom_facts
                changed = True

        if changed:
            cmem_mod.save(cm, char_dir=char_dir)


def run(
    user_id:     str,
    char_id:     str,
    messages:    List[Dict],
    llm_call,
    tool_output: str = "",
    async_mode:  bool = True,
    char_dir:    Optional[str] = None,
    dbg=None,
):
    """
    Entry point. Jalankan reflection.
    async_mode=True → daemon thread (tidak block main flow).
    async_mode=False → blocking (untuk testing).
    char_dir: folder karakter aktif — WAJIB diteruskan (mis. dari
        CharacterManager.char_dir) supaya character_self_facts baru
        (PATCH) ditulis ke character_memory.bin milik karakter yang benar,
        bukan file global lama. Tanpa ini, fallback ke path lama
        (lihat character_memory._char_path()).
    """
    def _log(msg):
        if dbg: dbg.line(msg)

    def _do_reflect():
        try:
            known_char_facts = ""
            try:
                cm_snapshot = cmem_mod.load(char_id, char_dir=char_dir)
                known_char_facts = cm_snapshot.summary_for_context()
                if known_char_facts == "(belum ada data karakter)":
                    known_char_facts = ""
            except Exception:
                known_char_facts = ""

            reflect_input = _build_reflect_input(
                messages, tool_output, known_char_facts=known_char_facts,
            )
            resp = llm_call(
                "react",
                messages=[
                    {"role":"system","content":_REFLECT_SYS},
                    {"role":"user","content":reflect_input},
                ],
                temperature=0.1,
                max_tokens=400,
            )
            raw = (resp.choices[0].message.content or "").strip()
            raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw, flags=re.I).strip()
            result = json.loads(raw)
            _dispatch_reflection(user_id, char_id, result, char_dir=char_dir)
            _log(
                f"  [REFLECT] ✅ done — facts={len(result.get('new_facts',[]))} "
                f"lessons={len(result.get('new_lessons',[]))} "
                f"char_facts={len(result.get('character_self_facts',[]))}"
            )
        except json.JSONDecodeError as e:
            _log(f"  [REFLECT] ⚠️ JSON parse error: {e}")
        except Exception as e:
            _log(f"  [REFLECT] ⚠️ error: {e}")

    if async_mode:
        t = threading.Thread(target=_do_reflect, daemon=True, name="reflection")
        t.start()
    else:
        _do_reflect()
