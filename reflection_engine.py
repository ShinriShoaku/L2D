"""
reflection_engine.py — Reflection Engine (Background Async).

Dijalankan SETELAH Soul mengirim respons (non-blocking, daemon thread).
Input : last N messages + current state + tool results
Output: update ke semua memory layer (working/relationship/knowledge/long)

Prompt kecil → JSON → dispatch ke masing-masing memory module.
Tidak menambah latency ke user karena berjalan di background.
"""
from __future__ import annotations
import json, re, threading, time
from typing import Dict, List, Optional

import working_memory    as wm_mod
import relationship_memory as rm_mod
import knowledge_memory  as km_mod
import long_memory       as lm_mod

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
  "working_memory":{"current_goal":"","last_action":"","awaiting_reply":"","short_context":{}}
}

Aturan:
- new_facts: hanya fakta konkret yang USER nyatakan (OS, GPU, framework, suka/tidak suka)
- new_lessons: hal yang AI harus ingat ("jangan panggil user bos", "user suka jawaban singkat")
- relationship_update: perubahan trust/romance dari sesi ini. 0 jika tidak ada perubahan
- experience: isi jika ada kejadian signifikan. title kosong jika tidak ada
- summary: wajib diisi. ringkasan netral apa yang dibicarakan
- working_memory: update jika ada goal baru, aksi terakhir, atau menunggu konfirmasi
- Jangan tulis apapun selain JSON
"""

def _build_reflect_input(
    messages:    List[Dict],
    tool_output: str = "",
    n:           int = 10,
) -> str:
    lines = []
    recent = messages[-n:]
    for m in recent:
        role    = "User" if m.get("role") == "user" else "AI"
        content = (m.get("content") or "")[:200]
        lines.append(f"{role}: {content}")
    if tool_output:
        lines.append(f"[Tool Output] {tool_output[:200]}")
    return "\n".join(lines)


def _dispatch_reflection(
    user_id:  str,
    char_id:  str,
    result:   Dict,
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


def run(
    user_id:     str,
    char_id:     str,
    messages:    List[Dict],
    llm_call,
    tool_output: str = "",
    async_mode:  bool = True,
    dbg=None,
):
    """
    Entry point. Jalankan reflection.
    async_mode=True → daemon thread (tidak block main flow).
    async_mode=False → blocking (untuk testing).
    """
    def _log(msg):
        if dbg: dbg.line(msg)

    def _do_reflect():
        try:
            reflect_input = _build_reflect_input(messages, tool_output)
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
            _dispatch_reflection(user_id, char_id, result)
            _log(f"  [REFLECT] ✅ done — facts={len(result.get('new_facts',[]))} lessons={len(result.get('new_lessons',[]))}")
        except json.JSONDecodeError as e:
            _log(f"  [REFLECT] ⚠️ JSON parse error: {e}")
        except Exception as e:
            _log(f"  [REFLECT] ⚠️ error: {e}")

    if async_mode:
        t = threading.Thread(target=_do_reflect, daemon=True, name="reflection")
        t.start()
    else:
        _do_reflect()
