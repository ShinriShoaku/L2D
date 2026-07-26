"""
test_memory_system.py — Test runner untuk semua modul memory system.

Jalankan dari folder project:
    python test_memory_system.py           → test semua
    python test_memory_system.py wm        → hanya working_memory
    python test_memory_system.py rm        → hanya relationship_memory
    python test_memory_system.py km        → hanya knowledge_memory
    python test_memory_system.py lm        → hanya long_memory
    python test_memory_system.py cs        → hanya conversation_state
    python test_memory_system.py re        → hanya reflection_engine
    python test_memory_system.py cx        → hanya context_composer
"""

import sys, shutil, os, json, time

# ── Mock LLM ─────────────────────────────────────────────────────────────────

class _FakeResp:
    def __init__(self, content):
        self.choices = [type("C", (), {"message": type("M", (), {"content": content})()})()]

def make_llm(response: str):
    """Factory mock llm_call yang selalu return response yang sama."""
    def _llm(*args, **kwargs):
        return _FakeResp(response)
    return _llm

# ── Helpers ───────────────────────────────────────────────────────────────────

PASS = "\033[92m✅ PASS\033[0m"
FAIL = "\033[91m❌ FAIL\033[0m"
_errors = []

def check(label: str, condition: bool, detail: str = ""):
    if condition:
        print(f"   {PASS}  {label}")
    else:
        msg = f"   {FAIL}  {label}" + (f" → {detail}" if detail else "")
        print(msg)
        _errors.append(label)

def header(title: str):
    print(f"\n{'═'*55}")
    print(f"  {title}")
    print(f"{'═'*55}")

def cleanup():
    shutil.rmtree("state", ignore_errors=True)

# ═════════════════════════════════════════════════════════════════════════════

def test_working_memory():
    header("WORKING MEMORY (wm)")
    import working_memory as wm
    wm.clear("t_u1")

    # Basic set/get
    m = wm.load("t_u1")
    m.current_goal = "buat project folder"
    m.add_task("rename abc.py")
    m.add_task("setup venv")
    m.set("editor", "neovim")
    m.add_fact("user pakai Arch Linux")
    wm.save(m)

    m2 = wm.load("t_u1")
    check("load goal dari disk",         m2.current_goal == "buat project folder")
    check("load tasks dari disk",         "rename abc.py" in m2.pending_tasks)
    check("short_context get()",          m2.get("editor") == "neovim")
    check("confirmed_facts tersimpan",    "user pakai Arch Linux" in m2.confirmed_facts)

    # complete task
    m2.complete_task("rename abc.py")
    wm.save(m2)
    m3 = wm.load("t_u1")
    check("complete_task hapus dari list", "rename abc.py" not in m3.pending_tasks)
    check("task lain masih ada",           "setup venv" in m3.pending_tasks)

    # update shortcut
    wm.update("t_u1", last_action="cloud_save", awaiting_reply="konfirmasi nama?")
    m4 = wm.load("t_u1")
    check("update() last_action",         m4.last_action == "cloud_save")
    check("update() awaiting_reply",      m4.awaiting_reply == "konfirmasi nama?")

    # clear_session
    m4.clear_session()
    check("clear_session() reset goal",   m4.current_goal == "")
    check("clear_session() reset tasks",  m4.pending_tasks == [])

    wm.clear("t_u1")
    print(f"\n  summary (before clear): {wm.load('t_u1').summary()}")


def test_relationship_memory():
    header("RELATIONSHIP MEMORY (rm)")
    import relationship_memory as rm
    rm.clear("t_u1", "ayumi")

    r = rm.load("t_u1", "ayumi")
    r.preferred_name = "Shinri"
    r.set_preference("answer_style", "technical", source="user_stated")
    r.set_preference("language", "Indonesia")
    r.add_behavior("dame", "cancel", confidence=0.95)
    r.add_lesson("jangan panggil user bos")
    r.update_trust(10)
    r.update_romance(5)
    rm.save(r)

    r2 = rm.load("t_u1", "ayumi")
    check("preferred_name tersimpan",      r2.preferred_name == "Shinri")
    check("get_preference answer_style",   r2.get_preference("answer_style") == "technical")
    check("get_preference language",       r2.get_preference("language") == "Indonesia")
    check("get_behavior 'dame'",           r2.get_behavior("dame") == "cancel")
    check("lessons tersimpan",             "jangan panggil user bos" in r2.lessons)
    check("trust_level +10",               r2.trust_level == 60)
    check("romance_level +5",              r2.romance_level == 5)

    # update preference (reinforce)
    r2.set_preference("answer_style", "technical", confidence=0.95)
    check("preference update (tidak duplikat)", len([p for p in r2.preferences if p["key"]=="answer_style"]) == 1)

    # trust cap
    r2.update_trust(999)
    check("trust_level max 100",           r2.trust_level == 100)
    r2.update_trust(-999)
    check("trust_level min 0",             r2.trust_level == 0)

    print(f"\n  context preview:\n{r2.summary_for_context()[:200]}")
    rm.clear("t_u1", "ayumi")


def test_knowledge_memory():
    header("KNOWLEDGE MEMORY (km)")
    import knowledge_memory as km
    km.clear("t_u1")

    # Add facts
    km.add_fact("t_u1", "hardware", "gpu",       "RTX 4070",   0.9)
    km.add_fact("t_u1", "software", "os",        "Arch Linux", 0.85)
    km.add_fact("t_u1", "project",  "framework", "FastAPI",    0.8)

    ks = km.load("t_u1")
    check("add 3 facts",                   len(ks.facts) == 3)

    # Reinforce
    km.add_fact("t_u1", "hardware", "gpu", "RTX 4070", 0.9)
    ks2 = km.load("t_u1")
    gpu = ks2.get("hardware", "gpu")
    check("reinforce: mentions naik",      gpu[0].mentions == 2)
    check("reinforce: confidence naik",    gpu[0].confidence > 0.9)

    # Contradict
    km.add_fact("t_u1", "software", "os", "Ubuntu", 0.7)
    ks3 = km.load("t_u1")
    os_facts = ks3.search("os")
    check("contradict: 2 os facts",        len(os_facts) == 2)
    old_os = next((f for f in os_facts if f.value == "Arch Linux"), None)
    check("contradict: old confidence turun", old_os is not None and old_os.confidence < 0.85)

    # Search
    results = ks3.search("fastapi")
    check("search 'fastapi'",              len(results) > 0 and results[0].value == "FastAPI")
    results2 = ks3.search("4070")
    check("search '4070'",                 len(results2) > 0)

    print(f"\n  knowledge context:\n{ks3.summary_for_context(topic_hint='gpu')[:200]}")
    km.clear("t_u1")


def test_long_memory():
    header("LONG MEMORY (lm)")
    import long_memory as lm
    lm.clear("t_u1")

    l = lm.load("t_u1")
    l.add_experience("membangun AI character Ayumi", "project pertama dengan Qwen 8B",
                     topics=["AI","qwen"], importance=0.9)
    l.add_experience("migrasi Electron ke Tauri", "refactor besar untuk performa",
                     topics=["electron","tauri"], importance=0.7)
    l.add_summary("User membahas arsitektur router dan memory system",
                  topics=["router","memory"], mood="excited", n_messages=25)
    l.add_milestone("first deep discussion about AI architecture")
    lm.save(l)

    l2 = lm.load("t_u1")
    check("2 experiences tersimpan",       len(l2.experiences) == 2)
    check("summary tersimpan",             len(l2.summaries) == 1)
    check("milestone tersimpan",           "first deep discussion about AI architecture" in l2.milestones)

    # Search
    exps = l2.search_experiences("ayumi")
    check("search 'ayumi' → experience",   len(exps) > 0 and "Ayumi" in exps[0].title)
    exps2 = l2.search_experiences("tauri")
    check("search 'tauri' → experience",   len(exps2) > 0 and "Tauri" in exps2[0].title)

    # Summary sorted by recency
    sums = l2.recent_summaries(1)
    check("recent_summaries(1)",           len(sums) == 1)

    print(f"\n  long memory context:\n{l2.summary_for_context(topic_hint='router')[:300]}")
    lm.clear("t_u1")


def test_conversation_state():
    header("CONVERSATION STATE (cs)")
    from conversation_state import (
        analyze, load_state, clear_state,
        set_pending_action, get_pending_action,
        compute_simple_complexity, complexity_to_mode,
        _is_positive_confirmation,
    )
    clear_state("t_u1")

    # ── Skenario 1: "yang tadi gimana?" ──────────────────────────────────────
    llm = make_llm('{"topic":"weather","frame":"FOLLOW_UP","stage":"follow_up","emotion":"neutral","references_previous":true,"need_history":true,"need_memory":false,"need_tool":false,"importance":0.6,"confidence":0.85}')
    history = [
        {"role":"user","content":"gimana cuaca hari ini"},
        {"role":"assistant","content":"cerah, 28 derajat"},
    ]
    s = analyze("t_u1", "yang tadi gimana?", history, llm)
    check("FOLLOW_UP: frame benar",        s.frame == "FOLLOW_UP")
    check("FOLLOW_UP: need_history=True",  s.need_history == True)
    check("FOLLOW_UP: references_previous", s.references_previous == True)

    # ── Skenario 2: pending_action + "iya" ───────────────────────────────────
    clear_state("t_u2")
    set_pending_action("t_u2", "cs_write", {"key":"hobi","value":"kopi"}, "simpan: user suka kopi")
    pa = get_pending_action("t_u2")
    check("pending_action tersimpan",      pa is not None and pa.tool == "cs_write")

    llm2 = make_llm('{"topic":"memory","frame":"CONFIRMATION","stage":"continuation","emotion":"neutral","references_previous":true,"need_history":true,"need_memory":true,"need_tool":true,"importance":0.7,"confidence":0.9}')
    s2 = analyze("t_u2", "iya", [], llm2)
    check("CONFIRMATION: frame benar",     s2.frame == "CONFIRMATION")
    check("CONFIRMATION: pending di-clear", s2.pending_action is None)

    # ── Skenario 3: "jadi jangan simpan ya" ──────────────────────────────────
    check("negative: 'jangan'",            _is_positive_confirmation("jangan") == False)
    check("negative: 'jadi jangan simpan'",_is_positive_confirmation("jadi jangan simpan ya") == False)
    check("positive: 'iya'",               _is_positive_confirmation("iya") == True)
    check("positive: 'gas lanjut'",        _is_positive_confirmation("gas lanjut") == True)

    # ── Skenario 4: LLM gagal → fallback ─────────────────────────────────────
    clear_state("t_u3")
    def broken(*a,**kw): raise ConnectionError("timeout")
    s3 = analyze("t_u3", "test", [], broken)
    check("LLM error → fallback CHAT",     s3.frame == "CHAT")

    # ── Skenario 5: Complexity scoring ───────────────────────────────────────
    cases = [
        ("hai",                                      "CHAT",      "nano"),
        ("dame",                                     "CHAT",      "nano"),
        ("aku lagi sedih banget",                    "EMOTIONAL", "normal"),   # score=7
        ("jelaskan perbedaan LSTM dan Transformer",  "KNOWLEDGE", "lite"),     # score=6
        ("aku depresi dan bingung harus gimana jelaskan detail", "EMOTIONAL", "normal"),  # score=10
    ]
    for text, frame, expected_mode in cases:
        score = compute_simple_complexity(text, frame)
        mode  = complexity_to_mode(score)
        check(f"complexity '{text[:30]}' → {expected_mode}", mode == expected_mode,
              f"got {mode} (score={score})")

    # ── Persist check ─────────────────────────────────────────────────────────
    clear_state("t_u4")
    llm3 = make_llm('{"topic":"coding","frame":"KNOWLEDGE","stage":"new_topic","emotion":"neutral","references_previous":false,"need_history":false,"need_memory":false,"need_tool":false,"importance":0.6,"confidence":0.8}')
    analyze("t_u4", "jelaskan decorator python", [], llm3)
    loaded = load_state("t_u4")
    check("state persist ke disk",         loaded.frame == "KNOWLEDGE" and loaded.topic == "coding")

    for uid in ["t_u1","t_u2","t_u3","t_u4"]: clear_state(uid)


def test_reflection_engine():
    header("REFLECTION ENGINE (re)")
    import working_memory as wm, knowledge_memory as km
    import relationship_memory as rm, long_memory as lm
    import reflection_engine as re_eng

    # Clear semua
    for m in [wm, km, lm]: m.clear("t_u1")
    rm.clear("t_u1", "ayumi")

    RJSON = json.dumps({
        "new_facts": [
            {"category":"hardware","key":"cpu","value":"Ryzen 7 5800X","confidence":0.85},
            {"category":"software","key":"editor","value":"neovim","confidence":0.9},
        ],
        "new_lessons": ["user suka jawaban teknis tanpa basa-basi", "jangan pakai emoji"],
        "relationship_update": {"trust_delta": 8, "romance_delta": 2, "preferred_name": "Shinri", "relation_note": ""},
        "experience": {"title": "diskusi memory system AI", "description": "user minta penjelasan long memory", "topics": ["memory","AI"], "importance": 0.75},
        "summary": "User bertanya tentang arsitektur memory system AI dan long memory",
        "working_memory": {"current_goal": "bangun memory module", "last_action": "explain", "awaiting_reply": "", "short_context": {"topic": "memory", "mood": "excited"}},
    })

    messages = [
        {"role":"user",      "content":"jelaskan dong long memory itu apa"},
        {"role":"assistant", "content":"long memory menyimpan pengalaman bersama..."},
    ]
    re_eng.run("t_u1", "ayumi", messages, make_llm(RJSON), tool_output="gum: ok", async_mode=False)

    wm2 = wm.load("t_u1")
    check("wm: current_goal",             wm2.current_goal == "bangun memory module")
    check("wm: short_context topic",      wm2.get("topic") == "memory")
    check("wm: short_context mood",       wm2.get("mood") == "excited")

    ks  = km.load("t_u1")
    cpu = ks.get("hardware","cpu")
    edt = ks.get("software","editor")
    check("km: cpu fact tersimpan",        len(cpu) > 0 and "5800X" in cpu[0].value)
    check("km: editor fact tersimpan",     len(edt) > 0 and edt[0].value == "neovim")

    r = rm.load("t_u1","ayumi")
    check("rm: preferred_name = Shinri",   r.preferred_name == "Shinri")
    check("rm: trust +8",                  r.trust_level == 58)
    check("rm: romance +2",                r.romance_level == 2)
    check("rm: lessons[0] tersimpan",      "user suka jawaban teknis" in r.lessons[0])
    check("rm: lessons[1] tersimpan",      "jangan pakai emoji" in r.lessons[1])

    l = lm.load("t_u1")
    exps = l.search_experiences("diskusi")
    check("lm: experience tersimpan",      len(exps) > 0 and "diskusi" in exps[0].title)
    check("lm: summary tersimpan",         len(l.summaries) > 0)

    for m in [wm, km, lm]: m.clear("t_u1")
    rm.clear("t_u1","ayumi")


def test_context_composer():
    header("CONTEXT COMPOSER (cx)")
    from conversation_state import ConversationState
    from context_composer import compose, compose_summary_line
    import working_memory as wm, relationship_memory as rm
    import knowledge_memory as km, long_memory as lm

    # Seed data
    w = wm.load("t_u1"); w.current_goal = "build memory system"; wm.save(w)
    r = rm.load("t_u1","ayumi"); r.preferred_name = "Shinri"; r.set_preference("style","technical"); rm.save(r)
    km.add_fact("t_u1","hardware","gpu","RTX 4070",0.9)
    l = lm.load("t_u1"); l.add_experience("bangun Ayumi","project AI",topics=["AI"],importance=0.9); lm.save(l)

    # nano: CHAT complexity=0
    s_nano = ConversationState(user_id="t_u1", frame="CHAT", topic="", complexity=0)
    c = compose("t_u1","ayumi", s_nano)
    check("nano: mode benar",              c["mode"] == "nano")
    check("nano: max_tokens=100",          c["max_tokens"] == 100)
    check("nano: context kosong",          c["full_context"] == "")
    check("nano: tidak ada wm",            c["working_memory"] == "")
    print(f"   nano  → {compose_summary_line(c)}")

    # lite: FOLLOW_UP complexity=5
    s_lite = ConversationState(user_id="t_u1", frame="FOLLOW_UP", topic="router", complexity=5)
    c2 = compose("t_u1","ayumi", s_lite)
    check("lite: mode benar",              c2["mode"] == "lite")
    check("lite: ada working_memory",      c2["working_memory"] != "")
    check("lite: ada relationship",        c2["relationship"] != "")
    check("lite: tidak ada long_memory",   c2["long_memory"] == "")
    print(f"   lite  → {compose_summary_line(c2)}")

    # normal: KNOWLEDGE complexity=8
    s_norm = ConversationState(user_id="t_u1", frame="KNOWLEDGE", topic="gpu", complexity=8)
    c3 = compose("t_u1","ayumi", s_norm)
    check("normal: mode benar",            c3["mode"] == "normal")
    check("normal: ada knowledge",         c3["knowledge"] != "")
    print(f"   norm  → {compose_summary_line(c3)}")

    # deep: EMOTIONAL complexity=12, dengan tool output
    s_deep = ConversationState(user_id="t_u1", frame="EMOTIONAL", topic="AI architecture", complexity=12)
    c4 = compose("t_u1","ayumi", s_deep, tool_output="gum: Shinri, romance=5pts")
    check("deep: mode benar",              c4["mode"] == "deep")
    check("deep: ada long_memory",         c4["long_memory"] != "")
    check("deep: ada tool_output",         c4["tool_output"] != "")
    check("deep: budget tidak exceeded",   len(c4["full_context"]) <= 3020)
    print(f"   deep  → {compose_summary_line(c4)}")

    # override mode
    c5 = compose("t_u1","ayumi", s_nano, override_mode="deep")
    check("override_mode='deep'",          c5["mode"] == "deep" and c5["long_memory"] != "")

    for m in [wm, km, lm]: m.clear("t_u1")
    rm.clear("t_u1","ayumi")


# ═════════════════════════════════════════════════════════════════════════════
# ENTRY POINT
# ═════════════════════════════════════════════════════════════════════════════

_ALL_TESTS = {
    "wm": ("working_memory",      test_working_memory),
    "rm": ("relationship_memory", test_relationship_memory),
    "km": ("knowledge_memory",    test_knowledge_memory),
    "lm": ("long_memory",         test_long_memory),
    "cs": ("conversation_state",  test_conversation_state),
    "re": ("reflection_engine",   test_reflection_engine),
    "cx": ("context_composer",    test_context_composer),
}

if __name__ == "__main__":
    cleanup()  # mulai fresh

    args = sys.argv[1:]
    if args:
        for a in args:
            if a in _ALL_TESTS:
                name, fn = _ALL_TESTS[a]
                try: fn()
                except Exception as e:
                    print(f"\n\033[91m⚠ Exception di {name}: {e}\033[0m")
                    import traceback; traceback.print_exc()
            else:
                print(f"Unknown module: {a}. Pilihan: {list(_ALL_TESTS.keys())}")
    else:
        for key, (name, fn) in _ALL_TESTS.items():
            try: fn()
            except Exception as e:
                print(f"\n\033[91m⚠ Exception di {name}: {e}\033[0m")
                import traceback; traceback.print_exc()

    cleanup()

    print(f"\n{'═'*55}")
    if _errors:
        print(f"\033[91m  FAILED: {len(_errors)} test(s)\033[0m")
        for e in _errors: print(f"    - {e}")
    else:
        print(f"\033[92m  ALL TESTS PASSED ✅\033[0m")
    print(f"{'═'*55}\n")
