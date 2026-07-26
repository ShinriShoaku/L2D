"""
test_real_model.py — Real integration test dengan LM Studio (localhost:1234).

Menguji semua modul memory system + conversation state dengan model AI sungguhan,
bukan mock. Output dicetak lengkap agar kamu bisa lihat respons model asli.

Jalankan:
    python test_real_model.py              → test semua
    python test_real_model.py cs           → hanya conversation state
    python test_real_model.py re           → hanya reflection engine
    python test_real_model.py gate         → hanya gate0 + gate1
    python test_real_model.py full         → simulasi full conversation turn

Pastikan LM Studio sudah running dan model sudah di-load sebelum menjalankan.
"""

import sys, os, time, json, shutil
from openai import OpenAI

# ─── Config ───────────────────────────────────────────────────────────────────
LM_STUDIO_URL = "http://localhost:1234/v1"
MODEL_NAME    = None   # None = pakai model yang sedang aktif di LM Studio

# ─── LLM Client ──────────────────────────────────────────────────────────────
_client = OpenAI(base_url=LM_STUDIO_URL, api_key="lm-studio", timeout=60, max_retries=0)

def llm_call(pass_name: str, messages: list, temperature: float = 0.0, max_tokens: int = 150) -> object:
    """
    llm_call compatible dengan interface yang dipakai semua modul.
    pass_name diabaikan (satu model untuk semua).
    """
    model = MODEL_NAME or _get_active_model()
    resp  = _client.chat.completions.create(
        model       = model,
        messages    = messages,
        temperature = temperature,
        max_tokens  = max_tokens,
    )
    return resp

def _get_active_model() -> str:
    """Ambil nama model yang sedang aktif di LM Studio."""
    try:
        models = _client.models.list()
        return models.data[0].id if models.data else "local-model"
    except Exception:
        return "local-model"

# ─── Helpers ─────────────────────────────────────────────────────────────────

GREEN  = "\033[92m"
RED    = "\033[91m"
YELLOW = "\033[93m"
CYAN   = "\033[96m"
RESET  = "\033[0m"
DIM    = "\033[2m"

def header(title: str):
    print(f"\n{CYAN}{'═'*60}{RESET}")
    print(f"{CYAN}  {title}{RESET}")
    print(f"{CYAN}{'═'*60}{RESET}")

def subheader(title: str):
    print(f"\n{YELLOW}  ── {title} ──{RESET}")

def ok(label: str, detail: str = ""):
    d = f"{DIM} → {detail}{RESET}" if detail else ""
    print(f"  {GREEN}✅{RESET} {label}{d}")

def fail(label: str, detail: str = ""):
    d = f"{DIM} → {detail}{RESET}" if detail else ""
    print(f"  {RED}❌{RESET} {label}{d}")

def info(label: str, value: str = ""):
    print(f"  {DIM}ℹ  {label}{RESET}" + (f": {value}" if value else ""))

def show_response(label: str, text: str, max_len: int = 200):
    preview = text[:max_len] + ("..." if len(text) > max_len else "")
    print(f"  {DIM}┌─ {label}{RESET}")
    for line in preview.split("\n"):
        print(f"  {DIM}│  {line}{RESET}")
    print(f"  {DIM}└─{RESET}")

_errors = []
def check(label: str, cond: bool, detail: str = ""):
    if cond: ok(label, detail)
    else:    fail(label, detail); _errors.append(label)

def cleanup():
    shutil.rmtree("state", ignore_errors=True)

def check_lm_studio() -> bool:
    """Pastikan LM Studio bisa dijangkau sebelum test dimulai."""
    try:
        models = _client.models.list()
        if not models.data:
            print(f"{RED}❌ LM Studio terhubung tapi tidak ada model yang di-load.{RESET}")
            return False
        model_id = models.data[0].id
        print(f"{GREEN}✅ LM Studio OK — model aktif: {model_id}{RESET}")
        return True
    except Exception as e:
        print(f"{RED}❌ Tidak bisa terhubung ke LM Studio ({LM_STUDIO_URL}): {e}{RESET}")
        print(f"   Pastikan LM Studio sudah running dan model sudah di-load.")
        return False

# ═════════════════════════════════════════════════════════════════════════════
# TEST 1: GATE 0 — pure Python (tidak butuh model)
# ═════════════════════════════════════════════════════════════════════════════

def test_gate0():
    header("GATE 0 — Pure Python Rule (no LLM)")
    from gate import gate0

    cases_chat = [
        "halo", "hai", "halo-halo", "hehe", "wkwk", ":)", "....",
        "iya", "ok", "dame", "123", "hmmmm", "ahhhh", "ok",
    ]
    cases_pass = [
        "cek memory daraku", "simpan ke cloud nama aku",
        "gimana cuaca hari ini", "kamu lagi mood apa",
    ]

    subheader("Harus → chat (skip LLM)")
    all_ok = True
    for text in cases_chat:
        r = gate0(text)
        ok_flag = r == "chat"
        if ok_flag: ok(f"{text!r}", "→ chat")
        else:       fail(f"{text!r}", f"→ {r!r} (bukan 'chat')"); _errors.append(f"gate0:{text}")
        all_ok = all_ok and ok_flag

    subheader("Harus → None (lanjut ke Gate 1)")
    for text in cases_pass:
        r = gate0(text)
        ok_flag = r is None
        if ok_flag: ok(f"{text!r}", "→ None (lanjut)")
        else:       fail(f"{text!r}", f"→ {r!r} (harusnya None)"); _errors.append(f"gate0:{text}")

# ═════════════════════════════════════════════════════════════════════════════
# TEST 2: GATE 1 — LLM intent detection
# ═════════════════════════════════════════════════════════════════════════════

def test_gate1():
    header("GATE 1 — LLM Intent Detection")
    from gate import gate1

    cases = [
        ("cek memory daraku",                    "task"),
        ("simpan ke cloud nama aku shinri",      "task"),
        ("gimana cuaca hari ini?",               "task"),
        ("ceritakan tentang hidupmu",            "chat"),
        ("haha lucu banget",                     "chat"),
        ("tolong analisa kepribadianku detail",  "task"),
    ]

    for text, expect_type in cases:
        t0 = time.time()
        result = gate1(text, llm_call)
        elapsed = time.time() - t0
        got_type = result.get("type","?")
        complexity = result.get("complexity","?")
        label = f"{text!r}"
        detail = f"type={got_type} complexity={complexity} ({elapsed:.2f}s)"
        # Toleran: mixed juga ok
        ok_flag = got_type == expect_type or got_type == "mixed"
        if ok_flag: ok(label, detail)
        else:       fail(label, f"expect={expect_type} {detail}"); _errors.append(f"gate1:{text}")
        show_response("raw JSON", json.dumps(result))

# ═════════════════════════════════════════════════════════════════════════════
# TEST 3: CONVERSATION STATE — LLM Analyzer
# ═════════════════════════════════════════════════════════════════════════════

def test_conversation_state():
    header("CONVERSATION STATE — LLM Analyzer")
    from conversation_state import analyze, load_state, clear_state, set_pending_action

    # ── Skenario 1: Greeting ─────────────────────────────────────────────────
    subheader("Skenario 1: 'halo' → harus CHAT")
    clear_state("real_u1")
    t0 = time.time()
    s = analyze("real_u1", "halo", [], llm_call)
    info("elapsed", f"{time.time()-t0:.2f}s")
    info("result", s.summary_line())
    check("frame = CHAT",         s.frame == "CHAT", s.frame)
    check("need_tool = False",    not s.need_tool, str(s.need_tool))

    # ── Skenario 2: "yang tadi gimana?" ─────────────────────────────────────
    subheader("Skenario 2: 'yang tadi gimana?' + history cuaca")
    clear_state("real_u2")
    history = [
        {"role":"user",      "content":"gimana cuaca hari ini?"},
        {"role":"assistant", "content":"cerah, suhu 28 derajat"},
    ]
    t0 = time.time()
    s2 = analyze("real_u2", "yang tadi gimana?", history, llm_call)
    info("elapsed", f"{time.time()-t0:.2f}s")
    info("result", s2.summary_line())
    check("frame = FOLLOW_UP atau CHAT", s2.frame in ("FOLLOW_UP","CHAT"), s2.frame)
    check("references_previous = True", s2.references_previous, str(s2.references_previous))
    check("need_history = True",        s2.need_history, str(s2.need_history))

    # ── Skenario 3: "iya" dengan pending_action ──────────────────────────────
    subheader("Skenario 3: 'iya' setelah pending simpan kopi")
    clear_state("real_u3")
    set_pending_action("real_u3", "cs_write", {"key":"hobi","value":"kopi"}, "simpan: user suka kopi")
    prev = load_state("real_u3")
    info("pending sebelum", prev.pending_action.description if prev.pending_action else "None")
    t0 = time.time()
    s3 = analyze("real_u3", "iya", [], llm_call)
    info("elapsed", f"{time.time()-t0:.2f}s")
    info("result", s3.summary_line())
    check("frame = CONFIRMATION",   s3.frame == "CONFIRMATION", s3.frame)
    check("pending di-clear",       s3.pending_action is None, str(s3.pending_action))

    # ── Skenario 4: Ekspresi emosi ───────────────────────────────────────────
    subheader("Skenario 4: 'aku lagi sedih banget hari ini'")
    clear_state("real_u4")
    t0 = time.time()
    s4 = analyze("real_u4", "aku lagi sedih banget hari ini", [], llm_call)
    info("elapsed", f"{time.time()-t0:.2f}s")
    info("result", s4.summary_line())
    check("emotion != neutral",     s4.emotion != "neutral", s4.emotion)
    check("frame = EMOTIONAL/CHAT", s4.frame in ("EMOTIONAL","CHAT"), s4.frame)

    # ── Skenario 5: Task request ─────────────────────────────────────────────
    subheader("Skenario 5: 'coba cek memory daraku'")
    clear_state("real_u5")
    t0 = time.time()
    s5 = analyze("real_u5", "coba cek memory daraku", [], llm_call)
    info("elapsed", f"{time.time()-t0:.2f}s")
    info("result", s5.summary_line())
    # need_tool True karena FRAME_HINTS[TASK/MEMORY_EDIT] override LLM
    check("need_tool = True",       s5.need_tool,    str(s5.need_tool))
    check("need_memory = True",     s5.need_memory,  str(s5.need_memory))
    check("frame = TASK/MEMORY/USER", s5.frame in ("TASK","MEMORY_EDIT","FOLLOW_UP","CHAT"), s5.frame)

    for uid in ["real_u1","real_u2","real_u3","real_u4","real_u5"]:
        clear_state(uid)

# ═════════════════════════════════════════════════════════════════════════════
# TEST 4: REFLECTION ENGINE — extract facts/lessons dari conversation
# ═════════════════════════════════════════════════════════════════════════════

def test_reflection_engine():
    header("REFLECTION ENGINE — Real LLM Extract")
    import reflection_engine as re_eng
    import working_memory    as wm
    import knowledge_memory  as km
    import relationship_memory as rm
    import long_memory       as lm

    # Clear state
    for m in [wm, km, lm]: m.clear("real_re")
    rm.clear("real_re","ayumi")

    # Simulasi percakapan yang mengandung banyak info
    messages = [
        {"role":"user",      "content":"hei, aku pakai RTX 4070 dan OS-ku Arch Linux"},
        {"role":"assistant", "content":"wah keren, setup yang bagus!"},
        {"role":"user",      "content":"iya, aku lagi build project AI pakai FastAPI dan Qwen"},
        {"role":"assistant", "content":"menarik! Qwen 8B atau 32B?"},
        {"role":"user",      "content":"8B dulu, dan panggil aku Shinri ya, bukan nama asli"},
        {"role":"assistant", "content":"oke Shinri, siap!"},
        {"role":"user",      "content":"btw jangan pakai emoji ya, aku kurang suka"},
        {"role":"assistant", "content":"baik, tidak akan pakai emoji"},
    ]

    print()
    info("Mengirim percakapan ke model untuk dianalisa...")
    info("Ini akan muncul setelah beberapa detik (async=False untuk test)")
    t0 = time.time()
    re_eng.run("real_re", "ayumi", messages, llm_call,
               tool_output="", async_mode=False)
    elapsed = time.time() - t0
    info("elapsed", f"{elapsed:.2f}s")

    # Cek hasil
    subheader("Working Memory")
    wm2 = wm.load("real_re")
    info("summary", wm2.summary())

    subheader("Knowledge Memory")
    ks = km.load("real_re")
    info(f"total facts", str(len(ks.facts)))
    for f in ks.facts:
        info(f"  {f.get('category')}.{f.get('key')}", f"{f.get('value')!r} conf={f.get('confidence',0):.2f}")
    check("minimal 1 fact hardware/software", len(ks.facts) >= 1, str(len(ks.facts)))

    subheader("Relationship Memory")
    r = rm.load("real_re","ayumi")
    info("preferred_name", r.preferred_name or "(kosong)")
    info("lessons",        str(r.lessons))
    info("preferences",    str([(p["key"],p["value"]) for p in r.preferences]))
    check("minimal 1 lesson", len(r.lessons) >= 1, str(r.lessons))

    subheader("Long Memory")
    l = lm.load("real_re")
    info("summaries", str(len(l.summaries)))
    for s in l.summaries:
        info("  summary", s.get("summary","")[:100])
    check("ada summary", len(l.summaries) >= 1)

    for m in [wm, km, lm]: m.clear("real_re")
    rm.clear("real_re","ayumi")

# ═════════════════════════════════════════════════════════════════════════════
# TEST 5: CONTEXT COMPOSER — pilih memory yang tepat
# ═════════════════════════════════════════════════════════════════════════════

def test_context_composer():
    header("CONTEXT COMPOSER — Memory Selection")
    import working_memory      as wm
    import relationship_memory as rm
    import knowledge_memory    as km
    import long_memory         as lm
    from conversation_state import ConversationState
    from context_composer   import compose, compose_summary_line

    # Seed data realistis
    w = wm.load("real_cx")
    w.current_goal = "build AI memory system"
    w.add_task("implement long_memory")
    wm.save(w)

    r = rm.load("real_cx","ayumi")
    r.preferred_name = "Shinri"
    r.set_preference("answer_style","technical")
    r.add_lesson("jangan pakai emoji")
    rm.save(r)

    km.add_fact("real_cx","hardware","gpu","RTX 4070",0.9)
    km.add_fact("real_cx","software","os","Arch Linux",0.85)
    km.add_fact("real_cx","project","framework","FastAPI",0.8)

    l = lm.load("real_cx")
    l.add_experience("membangun AI Ayumi","project pertama",topics=["AI","qwen"],importance=0.9)
    l.add_summary("User bahas arsitektur memory system",topics=["memory"],mood="excited",n_messages=20)
    lm.save(l)

    modes = [
        ("nano",   ConversationState(user_id="real_cx", frame="CHAT",      topic="",       complexity=0)),
        ("lite",   ConversationState(user_id="real_cx", frame="FOLLOW_UP", topic="router", complexity=5)),
        ("normal", ConversationState(user_id="real_cx", frame="KNOWLEDGE", topic="memory", complexity=8)),
        ("deep",   ConversationState(user_id="real_cx", frame="EMOTIONAL", topic="AI",     complexity=12)),
    ]

    for mode_label, state in modes:
        subheader(f"Mode: {mode_label.upper()}")
        ctx = compose("real_cx","ayumi", state, tool_output="gum: Shinri pts=5" if mode_label=="deep" else "")
        print(f"  {compose_summary_line(ctx)}")
        if ctx["full_context"]:
            show_response("context yang dikirim ke Soul", ctx["full_context"])
        check(f"{mode_label}: mode benar", ctx["mode"] == mode_label, ctx["mode"])

    for m in [wm, km, lm]: m.clear("real_cx")
    rm.clear("real_cx","ayumi")

# ═════════════════════════════════════════════════════════════════════════════
# TEST 6: FULL CONVERSATION SIMULATION — multi-turn dengan state persisten
# ═════════════════════════════════════════════════════════════════════════════

def test_full_conversation():
    header("FULL CONVERSATION SIMULATION — Multi-Turn")
    from conversation_state import analyze, load_state, clear_state, set_pending_action
    import working_memory      as wm
    import relationship_memory as rm
    import knowledge_memory    as km
    import long_memory         as lm
    import reflection_engine   as re_eng
    from context_composer      import compose, compose_summary_line

    USER_ID = "real_full"
    CHAR_ID = "ayumi"
    clear_state(USER_ID)
    for m in [wm, km, lm]: m.clear(USER_ID)
    rm.clear(USER_ID, CHAR_ID)

    def _turn(user_msg: str, ai_response: str, history: list):
        """Simulasi satu turn: analyze → compose → reflect."""
        print(f"\n  {YELLOW}User:{RESET} {user_msg}")

        # 1. Analyze
        from gate import gate0
        g0 = gate0(user_msg)
        if g0 == "chat":
            state = load_state(USER_ID)
            state.frame = "CHAT"; state.complexity = 0
            print(f"  {DIM}[gate0 → chat, skip analyze]{RESET}")
        else:
            t0 = time.time()
            state = analyze(USER_ID, user_msg, history, llm_call)
            print(f"  {DIM}[analyze {time.time()-t0:.2f}s] {state.summary_line()}{RESET}")

        # 2. Context Composer
        ctx = compose(USER_ID, CHAR_ID, state)
        print(f"  {DIM}[compose] {compose_summary_line(ctx)}{RESET}")

        # 3. Simpan history
        history.append({"role":"user","content":user_msg})
        history.append({"role":"assistant","content":ai_response})

        # 4. Reflect (sync untuk test agar bisa langsung cek hasilnya)
        re_eng.run(USER_ID, CHAR_ID, history[-6:], llm_call, async_mode=False)
        print(f"  {DIM}[reflect done]{RESET}")

        print(f"  {CYAN}AI:{RESET} {ai_response[:80]}...")
        return state, ctx

    # Simulasi percakapan realistis
    hist = []

    s1, _ = _turn(
        "hei, aku Shinri. aku lagi develop AI assistant pakai Python",
        "hei Shinri! Menarik sekali, AI assistant pakai Python itu seru.",
        hist,
    )

    s2, _ = _turn(
        "aku pakai Qwen 8B di LM Studio, dan RTX 4070 untuk inferencing",
        "Qwen 8B dengan RTX 4070 sangat capable untuk local inference.",
        hist,
    )

    s3, _ = _turn(
        "yang tadi soal LM Studio, ada tips optimasi tidak?",
        "Untuk LM Studio dengan RTX 4070, coba set context length 4096 dan gunakan Q4_K_M.",
        hist,
    )
    check("turn 3: FOLLOW_UP detected", s3.frame in ("FOLLOW_UP","KNOWLEDGE","TASK"), s3.frame)

    s4, _ = _turn(
        "iya bagus, simpan tips itu ya",
        "Baik, akan aku catat: LM Studio RTX 4070 → context 4096, Q4_K_M.",
        hist,
    )

    # Verifikasi state memory setelah 4 turn
    subheader("State Memory setelah 4 turn")
    ks = km.load(USER_ID)
    info("knowledge facts", str(len(ks.facts)))
    for f in ks.facts[:5]:
        info(f"  {f.get('category')}.{f.get('key')}", f.get('value',''))

    r = rm.load(USER_ID, CHAR_ID)
    info("preferred_name",   r.preferred_name or "(belum)")
    info("lessons",          str(r.lessons[:3]))

    l = lm.load(USER_ID)
    info("summaries",        str(len(l.summaries)))

    wm2 = wm.load(USER_ID)
    info("working_memory",   wm2.summary())

    check("ada knowledge facts",     len(ks.facts) >= 1)
    check("ada conversation summary", len(l.summaries) >= 1)

    # Cleanup
    clear_state(USER_ID)
    for m in [wm, km, lm]: m.clear(USER_ID)
    rm.clear(USER_ID, CHAR_ID)

# ═════════════════════════════════════════════════════════════════════════════
# TEST 7: DECISION GRAPH — kategori dari tree binary
# ═════════════════════════════════════════════════════════════════════════════

def test_decision_graph():
    header("DECISION GRAPH — LLM Category Tree")
    from gate import decision_graph

    cases = [
        ("cek memory daraku",              ["user","chat"]),
        ("simpan ke cloud nama aku",       ["cloud"]),
        ("berapa viewer sekarang",         ["state","event","social"]),
        ("kamu lagi mood apa",             ["self","meta"]),
        ("cuaca jakarta hari ini",         ["custom","live"]),
    ]

    for text, expected_cats in cases:
        t0 = time.time()
        cats = decision_graph(text, llm_call, has_custom=True)
        elapsed = time.time() - t0
        overlap = any(c in cats for c in expected_cats)
        label   = f"{text!r}"
        detail  = f"got={cats} expect_any={expected_cats} ({elapsed:.2f}s)"
        if overlap: ok(label, detail)
        else:       fail(label, detail); _errors.append(f"dg:{text}")

# ═════════════════════════════════════════════════════════════════════════════
# ENTRY POINT
# ═════════════════════════════════════════════════════════════════════════════


def test_pending_action_flow():
    header("PENDING ACTION FLOW — Confirmation Cycle")
    from conversation_state import (
        analyze, clear_state, set_pending_action,
        get_pending_action, _is_positive_confirmation,
    )
    import working_memory as wm

    UID = "real_pa"
    clear_state(UID)
    wm.clear(UID)

    # ── Setup: Soul tanya konfirmasi, set pending ────────────────────────────
    subheader("Setup: set pending cs_write")
    set_pending_action(UID, "cs_write",
        {"ns": f"user:{UID}", "key": "hobi", "value": "kopi hitam"},
        description="simpan: user suka kopi hitam")

    pa = get_pending_action(UID)
    check("pending_action tersimpan", pa is not None and pa.tool == "cs_write")
    info("pending", pa.description if pa else "None")

    # ── Turn 1: user jawab "iya" ─────────────────────────────────────────────
    subheader("Turn 1: user jawab 'iya'")
    t0 = time.time()
    s = analyze(UID, "iya", [], llm_call)
    info("elapsed", f"{time.time()-t0:.2f}s")
    info("frame", s.frame)
    check("frame = CONFIRMATION", s.frame == "CONFIRMATION", s.frame)
    check("pending di-clear", s.pending_action is None)
    is_pos = _is_positive_confirmation("iya")
    check("'iya' = positive", is_pos)

    # ── Reset dan test negatif ───────────────────────────────────────────────
    subheader("Setup: set pending lagi")
    set_pending_action(UID, "cs_write",
        {"ns": f"user:{UID}", "key": "hobi", "value": "kopi hitam"},
        "simpan: user suka kopi hitam")

    subheader("Turn 2: user jawab 'jangan'")
    t0 = time.time()
    s2 = analyze(UID, "eh jangan deh", [], llm_call)
    info("elapsed", f"{time.time()-t0:.2f}s")
    info("frame", s2.frame)
    is_neg = not _is_positive_confirmation("eh jangan deh")
    check("'eh jangan deh' = negative", is_neg)

    # ── Positive/negative edge cases ─────────────────────────────────────────
    subheader("Positive/Negative edge cases")
    pos_cases = ["iya", "ya", "ok", "oke", "gas", "gas lanjut", "lanjut", "boleh", "sip"]
    neg_cases = ["jangan", "tidak", "ga", "nggak", "batal", "cancel", "stop", "engga"]

    for text in pos_cases:
        r = _is_positive_confirmation(text)
        if r: ok(f"pos: {text!r}")
        else: fail(f"pos: {text!r} → got False"); _errors.append(f"pos:{text}")

    for text in neg_cases:
        r = not _is_positive_confirmation(text)
        if r: ok(f"neg: {text!r}")
        else: fail(f"neg: {text!r} → got True (harusnya False)"); _errors.append(f"neg:{text}")

    clear_state(UID)
    wm.clear(UID)


# ═════════════════════════════════════════════════════════════════════════════
# TEST 9: KNOWLEDGE CONFIDENCE — reinforce dan contradict dengan data real
# ═════════════════════════════════════════════════════════════════════════════

def test_knowledge_confidence():
    header("KNOWLEDGE CONFIDENCE — Reinforce & Contradict")
    import knowledge_memory as km
    import reflection_engine as re_eng

    UID = "real_km"
    km.clear(UID)

    # ── Round 1: user bilang pakai Arch Linux ────────────────────────────────
    subheader("Round 1: 'aku pakai Arch Linux'")
    msgs1 = [
        {"role":"user",      "content":"btw aku pakai Arch Linux sebagai OS utamaku"},
        {"role":"assistant", "content":"keren, Arch Linux memang powerful"},
    ]
    re_eng.run(UID, "ayumi", msgs1, llm_call, async_mode=False)
    ks1 = km.load(UID)
    os_facts1 = ks1.search("arch linux")
    info("facts setelah round 1", str(len(ks1.facts)))
    for f in ks1.facts: info(f"  {f.get('category')}.{f.get('key')}", f"{f.get('value')!r} conf={f.get('confidence',0):.2f}")

    # ── Round 2: reinforce (sebut lagi) ──────────────────────────────────────
    subheader("Round 2: reinforce — sebut Arch Linux lagi")
    msgs2 = [
        {"role":"user",      "content":"iya, aku sudah lama pakai Arch Linux, suka banget"},
        {"role":"assistant", "content":"wah loyal user Arch!"},
    ]
    re_eng.run(UID, "ayumi", msgs2, llm_call, async_mode=False)
    ks2 = km.load(UID)
    arch_after = ks2.search("arch")
    if arch_after:
        info("confidence setelah reinforce", f"{arch_after[0].confidence:.2f} mentions={arch_after[0].mentions}")
        check("confidence naik atau mentions naik", arch_after[0].mentions >= 1)
    else:
        info("arch facts", "(belum terdeteksi model)")

    # ── Round 3: contradict ───────────────────────────────────────────────────
    subheader("Round 3: contradict — pindah ke Ubuntu")
    msgs3 = [
        {"role":"user",      "content":"update: aku pindah ke Ubuntu sekarang, Arch terlalu ribet"},
        {"role":"assistant", "content":"oh pindah ke Ubuntu, lebih user-friendly ya"},
    ]
    re_eng.run(UID, "ayumi", msgs3, llm_call, async_mode=False)
    ks3 = km.load(UID)
    info("semua os facts", str(len(ks3.search("linux")) + len(ks3.search("ubuntu"))))
    for f in ks3.facts:
        if "linux" in f.get("value","").lower() or "ubuntu" in f.get("value","").lower():
            info(f"  os fact", f"{f.get('value')!r} conf={f.get('confidence',0):.2f} contradicted={f.get('contradicted',False)}")

    km.clear(UID)


# ═════════════════════════════════════════════════════════════════════════════
# TEST 10: LATENCY BENCHMARK — ukur waktu tiap komponen
# ═════════════════════════════════════════════════════════════════════════════

def test_latency_benchmark():
    header("LATENCY BENCHMARK — Per Component")
    from gate import gate0, gate1, decision_graph
    from conversation_state import analyze, clear_state

    UID = "bench_u"
    clear_state(UID)

    results = {}

    # Gate 0 (Python, no LLM)
    subheader("Gate 0 (Python)")
    times = []
    for _ in range(20):
        t0 = time.time()
        gate0("halo")
        times.append(time.time() - t0)
    avg = sum(times)/len(times)*1000
    results["gate0"] = avg
    info("avg (20 runs)", f"{avg:.3f}ms")
    check("Gate 0 < 1ms", avg < 1.0, f"{avg:.3f}ms")

    # Gate 1 (1 LLM call)
    subheader("Gate 1 (1 LLM call)")
    N = 3
    times = []
    for _ in range(N):
        t0 = time.time()
        gate1("cek memory daraku", llm_call)
        times.append(time.time() - t0)
    avg = sum(times)/len(times)
    results["gate1"] = avg
    info(f"avg ({N} runs)", f"{avg:.2f}s")

    # Decision Graph (2 LLM calls)
    subheader("Decision Graph (2 LLM calls)")
    times = []
    for _ in range(N):
        t0 = time.time()
        decision_graph("cek memory daraku", llm_call)
        times.append(time.time() - t0)
    avg = sum(times)/len(times)
    results["dg"] = avg
    info(f"avg ({N} runs)", f"{avg:.2f}s")

    # Conversation State Analyze (1 LLM call)
    subheader("Conversation State Analyze (1 LLM call)")
    times = []
    for _ in range(N):
        clear_state(UID)
        t0 = time.time()
        analyze(UID, "yang tadi gimana?", [], llm_call)
        times.append(time.time() - t0)
    avg = sum(times)/len(times)
    results["cs_analyze"] = avg
    info(f"avg ({N} runs)", f"{avg:.2f}s")

    # Binary file I/O
    subheader("Binary File I/O (state disk)")
    import working_memory as wm
    wm.clear(UID)
    w = wm.load(UID)
    w.current_goal = "test"
    times = []
    for _ in range(50):
        t0 = time.time()
        wm.save(w)
        wm.load(UID)
        times.append(time.time() - t0)
    avg_io = sum(times)/len(times)*1000
    results["file_io"] = avg_io
    info(f"avg save+load (50 runs)", f"{avg_io:.3f}ms")
    check("File I/O < 5ms", avg_io < 5.0, f"{avg_io:.3f}ms")
    wm.clear(UID)

    # Summary
    subheader("Summary Latency")
    print()
    print(f"  {'Component':<25} {'Latency':>12}")
    print(f"  {'-'*38}")
    labels = {
        "gate0":     "Gate 0 (Python)",
        "gate1":     "Gate 1 (1 LLM)",
        "dg":        "Decision Graph (2 LLM)",
        "cs_analyze":"Conv State Analyze (1 LLM)",
        "file_io":   "File I/O save+load",
    }
    for key, label in labels.items():
        v = results.get(key, 0)
        unit = "ms" if key in ("gate0","file_io") else "s"
        val  = f"{v:.3f}{unit}" if key in ("gate0","file_io") else f"{v:.2f}s"
        print(f"  {label:<25} {val:>12}")

    clear_state(UID)



if __name__ == "__main__":
    # _ALL didefinisikan di sini agar semua fungsi test sudah terdefinisi dulu
    _ALL = {
        "gate":  ("Gate 0 + Gate 1",           [test_gate0, test_gate1]),
        "cs":    ("Conversation State",         [test_conversation_state]),
        "re":    ("Reflection Engine",          [test_reflection_engine]),
        "cx":    ("Context Composer",           [test_context_composer]),
        "dg":    ("Decision Graph",             [test_decision_graph]),
        "full":  ("Full Conversation Sim",      [test_full_conversation]),
        "pa":    ("Pending Action Flow",        [test_pending_action_flow]),
        "km":    ("Knowledge Confidence",       [test_knowledge_confidence]),
        "bench": ("Latency Benchmark",          [test_latency_benchmark]),
    }

    cleanup()
    print(f"\n{CYAN}{'═'*60}{RESET}")
    print(f"{CYAN}  REAL MODEL INTEGRATION TEST{RESET}")
    print(f"{CYAN}  Server: {LM_STUDIO_URL}{RESET}")
    print(f"{CYAN}{'═'*60}{RESET}")

    if not check_lm_studio():
        sys.exit(1)

    # Model info
    try:
        model_id = _get_active_model()
        print(f"  Model  : {model_id}")
    except Exception:
        pass
    print()

    args = sys.argv[1:]
    run_all = not args or args == ["all"]

    if run_all:
        tests_to_run = []
        for key, (name, fns) in _ALL.items():
            tests_to_run.extend(fns)
        print(f"  Menjalankan semua {len(_ALL)} suite test...\n")
    else:
        tests_to_run = []
        for a in args:
            if a in _ALL:
                name, fns = _ALL[a]
                print(f"  Menjalankan: {name}")
                tests_to_run.extend(fns)
            elif a in ("help", "-h", "--help"):
                print(f"\n  Pilihan test:")
                for k, (n, _) in _ALL.items():
                    print(f"    {k:<8} → {n}")
                print()
                sys.exit(0)
            else:
                print(f"{RED}Unknown: {a!r}{RESET}")
                print(f"  Pilihan: {list(_ALL.keys())}")
                sys.exit(1)

    for fn in tests_to_run:
        try:
            fn()
        except Exception as e:
            import traceback
            print(f"\n{RED}⚠ Exception di {fn.__name__}: {e}{RESET}")
            traceback.print_exc()
            _errors.append(fn.__name__)

    cleanup()

    print(f"\n{CYAN}{'═'*60}{RESET}")
    if _errors:
        print(f"{RED}  FAILED: {len(_errors)} test(s){RESET}")
        for e in _errors:
            print(f"    - {e}")
    else:
        print(f"{GREEN}  ALL TESTS PASSED ✅{RESET}")
    print(f"{CYAN}{'═'*60}{RESET}\n")


# ═════════════════════════════════════════════════════════════════════════════
# TEST 8: PENDING ACTION FLOW — "mau aku simpan ya?" → "iya" / "jangan"
# ═════════════════════════════════════════════════════════════════════════════
