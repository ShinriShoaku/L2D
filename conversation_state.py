"""
conversation_state.py — Conversation State Engine (standalone module).

Tujuan: Decision Graph / Router tidak lagi menebak makna dari SATU message
mentah. Sebelum routing, Conversation Analyzer membaca:
  - last N messages (immediate history)
  - state percakapan sebelumnya (persisten per user)
lalu menghasilkan ConversationState — representasi "apa yang sedang terjadi"
yang jauh lebih murah dibaca oleh Decision Graph dibanding raw text.

Contoh masalah yang diselesaikan:
  "yang tadi gimana?"     → tanpa state: ambigu. dengan state: referensi ke topic sebelumnya
  "iya"                   → tanpa state: tidak ada makna. dengan state: konfirmasi pending_action
  "jadi jangan simpan ya" → tanpa state: "simpan apa?". dengan state: batalkan pending cloud_save

Format persist: conversation_state.bin (folder state/) — sama pola dengan
router_cache.bin dan cloud_store.bin (struct header+index + msgpack/pickle).

State per-user, key = user_id. TIDAK menyimpan full chat history — hanya
representasi ringkas (state terakhir + pending_action), karena history
mentah sudah ada di ChatHistory (main.py).
"""

from __future__ import annotations

import json
import os
import re
import struct
import time
from dataclasses import dataclass, field, asdict
from typing import Any, Dict, List, Optional

# ── msgpack (cepat) → fallback pickle ────────────────────────────────────────
try:
    import msgpack as _pack
    def _dumps(obj) -> bytes: return _pack.packb(obj, use_bin_type=True)
    def _loads(b: bytes):     return _pack.unpackb(b, raw=False)
    _PACK_NAME = "msgpack"
except ImportError:
    import pickle as _pkl
    def _dumps(obj) -> bytes: return _pkl.dumps(obj, protocol=4)
    def _loads(b: bytes):     return _pkl.loads(b)
    _PACK_NAME = "pickle"


# ═════════════════════════════════════════════════════════════════════════════
# CONVERSATION FRAME — kategori abstrak kondisi percakapan
# ═════════════════════════════════════════════════════════════════════════════

FRAMES = {
    "CHAT",            # greeting/ekspresi ringan, tidak butuh apapun
    "FOLLOW_UP",       # melanjutkan/merujuk topik sebelumnya ("yang tadi", "terus?")
    "MEMORY_EDIT",      # user minta simpan/hapus/ubah sesuatu ke memory/cloud
    "EMOTIONAL",        # ekspresi emosi (sedih, marah, bingung, dst)
    "KNOWLEDGE",        # minta penjelasan/analisa/perbandingan
    "ROLEPLAY",          # roleplay panjang, "anggap kau", "jadi pacarku"
    "TASK",             # butuh tool eksternal jelas (cuaca, cek data, dll)
    "CONFIRMATION",     # jawaban pendek terhadap pending_action ("iya", "jangan", "batal")
}

# Frame → default routing hints (dipakai Decision Graph nanti)
# need_memory        = butuh data memory tentang USER (working/relationship/knowledge)
# need_self_memory   = butuh data memory tentang KARAKTER AI sendiri (identitas,
#                       tanggal lahir, suka/tidak suka, hobi, backstory, dll —
#                       lihat character_memory.py). Task terpisah dari need_memory
#                       supaya akurasinya jelas: pertanyaan "kamu ulang tahun kapan?"
#                       tidak butuh data USER sama sekali, tapi WAJIB butuh data diri
#                       karakter — dua kebutuhan yang berbeda, jadi dua flag berbeda.
FRAME_HINTS: Dict[str, Dict[str, bool]] = {
    "CHAT":         {"need_history": False, "need_tool": False, "need_memory": False, "need_self_memory": False, "soul_deep": False},
    "FOLLOW_UP":    {"need_history": True,  "need_tool": False, "need_memory": False, "need_self_memory": False, "soul_deep": False},
    "MEMORY_EDIT":  {"need_history": True,  "need_tool": True,  "need_memory": True,  "need_self_memory": False, "soul_deep": False},
    "EMOTIONAL":    {"need_history": False, "need_tool": False, "need_memory": False, "need_self_memory": False, "soul_deep": True},
    "KNOWLEDGE":    {"need_history": False, "need_tool": False, "need_memory": False, "need_self_memory": False, "soul_deep": True},
    "ROLEPLAY":     {"need_history": True,  "need_tool": False, "need_memory": False, "need_self_memory": True,  "soul_deep": True},
    "TASK":         {"need_history": False, "need_tool": True,  "need_memory": False, "need_self_memory": False, "soul_deep": False},
    "CONFIRMATION": {"need_history": True,  "need_tool": True,  "need_memory": False, "need_self_memory": False, "soul_deep": False},
}


# ═════════════════════════════════════════════════════════════════════════════
# DATA STRUCTURES
# ═════════════════════════════════════════════════════════════════════════════

@dataclass
class PendingAction:
    """Aksi yang menunggu konfirmasi user (mis. mau simpan ke cloud, perlu 'iya')."""
    tool:        str = ""             # nama tool yang akan dieksekusi, mis. "cs_write"
    args:        Dict = field(default_factory=dict)
    description: str = ""             # ringkasan untuk log/debug, mis. "simpan: user suka kopi"
    created_ts:  int = 0

    def is_empty(self) -> bool:
        return not self.tool


@dataclass
class ConversationState:
    """
    Representasi ringkas "apa yang sedang terjadi" dalam percakapan.
    Di-generate oleh analyze() setiap turn, dipakai Decision Graph berikutnya.
    """
    user_id:             str  = ""
    topic:               str  = ""
    frame:                str  = "CHAT"        # salah satu dari FRAMES
    intent:              str  = ""
    stage:                str  = "new_topic"    # new_topic | follow_up | continuation
    emotion:              str  = "neutral"
    references_previous:  bool = False
    continuation:          bool = False
    new_topic:             bool = True
    need_history:          bool = False
    need_memory:           bool = False
    need_self_memory:      bool = False   # PATCH: butuh data memori diri KARAKTER (bukan user)
    self_memory_field:     str  = ""      # PATCH v3: field SPESIFIK yg ditanya, mis. "birthday".
                                           # "" / "general" = tidak spesifik (fallback ke ringkasan).
                                           # Dipakai composer supaya cuma kirim 1 field, bukan semua.
    need_tool:             bool = False
    importance:            float = 0.5
    complexity:            int   = 1            # 0-17 CCS sederhana (lihat compute_ccs)
    confidence:            float = 0.8
    awaiting_confirmation:  bool = False
    pending_action:         Optional[PendingAction] = None
    last_topic:             str  = ""           # topic dari state SEBELUMNYA (untuk follow-up)
    updated_ts:             int  = 0

    def to_dict(self) -> Dict:
        d = asdict(self)
        if self.pending_action is not None:
            d["pending_action"] = asdict(self.pending_action)
        return d

    @staticmethod
    def from_dict(d: Dict) -> "ConversationState":
        pa_raw = d.get("pending_action")
        pa = PendingAction(**pa_raw) if pa_raw else None
        kwargs = {k: v for k, v in d.items() if k != "pending_action"}
        return ConversationState(pending_action=pa, **kwargs)

    def summary_line(self) -> str:
        """Ringkasan satu baris untuk debug log."""
        pa = f" pending={self.pending_action.tool}" if self.pending_action and not self.pending_action.is_empty() else ""
        smf = f" self_field={self.self_memory_field}" if self.self_memory_field else ""
        return (
            f"frame={self.frame} topic={self.topic!r} stage={self.stage} "
            f"emotion={self.emotion} conf={self.confidence:.2f} "
            f"need_mem={self.need_memory} need_self_mem={self.need_self_memory}{smf}{pa}"
        )


# ═════════════════════════════════════════════════════════════════════════════
# BINARY PERSIST — state/conversation_state.bin
# ═════════════════════════════════════════════════════════════════════════════

_STATE_FILE = "conversation_state.bin"
_MAGIC      = b"CVS\x01"
_HDR_FMT    = ">4sI"
_HDR_SZ     = struct.calcsize(_HDR_FMT)
_IDX_FMT    = ">Q"
_IDX_SZ     = struct.calcsize(_IDX_FMT)
_LEN_FMT    = ">I"
_LEN_SZ     = struct.calcsize(_LEN_FMT)


def _state_path() -> str:
    base = os.path.join(os.path.dirname(os.path.abspath(__file__)), "state")
    os.makedirs(base, exist_ok=True)
    return os.path.join(base, _STATE_FILE)


def _read_all_states() -> Dict[str, Dict]:
    """Return {user_id: state_dict}."""
    path = _state_path()
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "rb") as f:
            hdr = f.read(_HDR_SZ)
            if len(hdr) < _HDR_SZ: return {}
            magic, count = struct.unpack(_HDR_FMT, hdr)
            if magic != _MAGIC or count == 0: return {}
            idx_raw = f.read(_IDX_SZ * count)
            offsets = [struct.unpack(_IDX_FMT, idx_raw[i*_IDX_SZ:(i+1)*_IDX_SZ])[0] for i in range(count)]
            result = {}
            for off in offsets:
                f.seek(off)
                lr = f.read(_LEN_SZ)
                if len(lr) < _LEN_SZ: continue
                plen = struct.unpack(_LEN_FMT, lr)[0]
                pr = f.read(plen)
                if len(pr) < plen: continue
                try:
                    rec = _loads(pr)
                    result[rec["user_id"]] = rec
                except Exception: pass
            return result
    except Exception:
        return {}


def _write_all_states(states: Dict[str, Dict]):
    path = _state_path()
    items = list(states.values())
    payloads = []
    for rec in items:
        try: payloads.append(_dumps(rec))
        except Exception: payloads.append(None)

    valid = [(items[i], payloads[i]) for i in range(len(items)) if payloads[i] is not None]
    count = len(valid)
    data_start = _HDR_SZ + _IDX_SZ * count
    offsets, cur = [], data_start
    for _, p in valid:
        offsets.append(cur)
        cur += _LEN_SZ + len(p)

    try:
        with open(path, "wb") as f:
            f.write(struct.pack(_HDR_FMT, _MAGIC, count))
            for off in offsets:
                f.write(struct.pack(_IDX_FMT, off))
            for _, p in valid:
                f.write(struct.pack(_LEN_FMT, len(p)))
                f.write(p)
    except Exception:
        pass


def load_state(user_id: str) -> ConversationState:
    """Load state user dari disk. Return state default (CHAT) jika belum ada."""
    states = _read_all_states()
    raw = states.get(user_id)
    if raw is None:
        return ConversationState(user_id=user_id, updated_ts=int(time.time()))
    try:
        return ConversationState.from_dict(raw)
    except Exception:
        return ConversationState(user_id=user_id, updated_ts=int(time.time()))


def save_state(state: ConversationState):
    """Simpan state user ke disk (overwrite state lama user tsb)."""
    states = _read_all_states()
    state.updated_ts = int(time.time())
    states[state.user_id] = state.to_dict()
    _write_all_states(states)


def clear_state(user_id: str) -> bool:
    """Hapus state user (reset percakapan). Return True jika ada yang dihapus."""
    states = _read_all_states()
    if user_id not in states:
        return False
    del states[user_id]
    _write_all_states(states)
    return True


# ═════════════════════════════════════════════════════════════════════════════
# ANALYZER — 1 LLM call: baca last N messages + state lama → state baru
# ═════════════════════════════════════════════════════════════════════════════

# Enum valid untuk self_memory_field — HARUS sinkron dengan _LABELS di
# character_memory.py (+ "wants" & "general" yang bukan attribute biasa).
_SELF_MEMORY_FIELDS = {
    "full_name", "birthday", "zodiac", "age", "personality",
    "likes", "dislikes", "hobbies", "fears", "backstory",
    "wants", "general",
}

_ANALYZER_SYS = """\
Analisa kondisi percakapan saat ini. Jawab HANYA JSON satu baris.

Format output:
{"topic":"...","frame":"FRAME","stage":"new_topic|follow_up|continuation","emotion":"...","references_previous":true/false,"need_history":true/false,"need_memory":true/false,"need_self_memory":true/false,"self_memory_field":"FIELD","need_tool":true/false,"importance":0.0-1.0,"confidence":0.0-1.0}

FRAME (pilih salah satu):
  CHAT          = greeting/ekspresi ringan, tidak butuh apapun
  FOLLOW_UP     = merujuk/melanjutkan topik sebelumnya ("yang tadi","terus?","itu")
  MEMORY_EDIT   = minta simpan/hapus/ubah sesuatu ke memory/cloud
  EMOTIONAL     = ekspresi emosi (sedih/marah/bingung/takut/kecewa)
  KNOWLEDGE     = minta penjelasan/analisa/perbandingan ("jelaskan","kenapa","bandingkan")
  ROLEPLAY      = roleplay panjang ("anggap kau","jadi pacarku","berpura-pura")
  TASK          = butuh tool eksternal jelas (cuaca, cek data, dll)
  CONFIRMATION  = jawaban pendek thd pertanyaan/aksi sebelumnya ("iya","jangan","batal","ok")

Gunakan STATE SEBELUMNYA untuk menentukan apakah ini follow_up/continuation.
Jika pesan pendek ("iya","ya","jangan") dan state sebelumnya punya pending_action,
frame HARUS "CONFIRMATION" dan references_previous=true.

need_memory vs need_self_memory (DUA HAL BERBEDA, jangan disamakan):
  need_memory      = true jika butuh data tentang USER yang chat (romance, info,
                      preferensi, riwayat obrolan dia). Contoh: "aku pernah cerita apa?"
  need_self_memory = true jika pertanyaan/topik menyinggung identitas atau sifat
                      KARAKTER AI itu sendiri — nama, tanggal lahir, umur, zodiak,
                      suka/tidak suka, hobi, kepribadian, latar belakang, keinginan.
                      Contoh: "kamu ulang tahun kapan?", "kesukaanmu apa?",
                      "ceritain masa lalumu". Kedua flag BOLEH true bersamaan
                      jika pertanyaannya menyinggung user DAN karakter sekaligus.

self_memory_field (HANYA diisi kalau need_self_memory=true — hemat token,
supaya composer tidak perlu kirim SELURUH profil karakter, cukup 1 field):
  Pilih SATU yang paling cocok dengan pertanyaan:
    full_name | birthday | zodiac | age | personality | likes | dislikes |
    hobbies | fears | backstory | wants | general
  - "birthday"    = tanggal lahir / ulang tahun
  - "zodiac"      = zodiak / rasi bintang
  - "likes"       = kesukaan / hal yang disukai
  - "dislikes"    = ketidaksukaan / hal yang dibenci
  - "hobbies"     = hobi / kegiatan favorit
  - "fears"       = ketakutan / hal yang ditakuti
  - "personality" = sifat / kepribadian
  - "backstory"   = masa lalu / latar belakang cerita
  - "wants"       = keinginan / cita-cita / goals
  - "general"     = pertanyaan identitas KARAKTER yang luas/tidak spesifik,
                     atau kalau tidak yakin field mana yang cocok
  Kalau need_self_memory=false, isi dengan string kosong "".

Jangan tulis apapun selain JSON.
"""

def _build_analyzer_input(
    user_input:    str,
    history:       List[Dict],   # [{"role":"user"/"assistant","content":"..."}]
    prev_state:    ConversationState,
    n_history:     int = 5,
) -> str:
    lines = []
    if prev_state.frame != "CHAT" or not prev_state.pending_action is None:
        lines.append("[STATE SEBELUMNYA]")
        lines.append(prev_state.summary_line())
        if prev_state.pending_action and not prev_state.pending_action.is_empty():
            lines.append(f"pending_action: {prev_state.pending_action.tool} — {prev_state.pending_action.description}")
        lines.append("")

    recent = history[-n_history:] if history else []
    if recent:
        lines.append(f"[LAST {len(recent)} MESSAGES]")
        for m in recent:
            role = "User" if m.get("role") == "user" else "AI"
            content = (m.get("content") or "").strip()[:150]
            lines.append(f"{role}: {content}")
        lines.append("")

    lines.append("[PESAN SEKARANG]")
    lines.append(user_input)
    return "\n".join(lines)


def analyze(
    user_id:    str,
    user_input: str,
    history:    List[Dict],
    llm_call,
    n_history:  int = 5,
    dbg=None,
) -> ConversationState:
    """
    Main entry: analisa percakapan, update & persist state, return state baru.

    1. Load state lama dari disk
    2. Build prompt dengan last N history + state lama
    3. 1 LLM call → JSON
    4. Resolve pending_action (apakah pesan ini konfirmasi?)
    5. Save state baru ke disk
    6. Return ConversationState
    """
    def _log(msg):
        if dbg: dbg.line(msg)

    prev_state = load_state(user_id)
    analyzer_input = _build_analyzer_input(user_input, history, prev_state, n_history)

    try:
        resp = llm_call(
            "react",
            messages=[
                {"role": "system", "content": _ANALYZER_SYS},
                {"role": "user",   "content": analyzer_input},
            ],
            temperature=0.0,
            max_tokens=180,
        )
        raw = (resp.choices[0].message.content or "").strip()
        raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw, flags=re.I).strip()
        parsed = json.loads(raw)
        _log(f"  [ANALYZER RAW] {raw!r}")
    except Exception as e:
        _log(f"  [ANALYZER ERROR] {e} → fallback CHAT state")
        parsed = {
            "topic": "", "frame": "CHAT", "stage": "new_topic", "emotion": "neutral",
            "references_previous": False, "need_history": False, "need_memory": False,
            "need_self_memory": False, "self_memory_field": "", "need_tool": False,
            "importance": 0.3, "confidence": 0.5,
        }

    frame = str(parsed.get("frame", "CHAT")).upper()
    if frame not in FRAMES:
        frame = "CHAT"

    # ── Resolve pending_action: apakah ini konfirmasi? ───────────────────────
    new_pending = prev_state.pending_action
    if frame == "CONFIRMATION" and prev_state.pending_action and not prev_state.pending_action.is_empty():
        is_positive = _is_positive_confirmation(user_input)
        _log(f"  [ANALYZER] confirmation detected → positive={is_positive} pending={prev_state.pending_action.tool}")
        # Pending action di-clear setelah resolve (positif atau negatif sama-sama clear)
        # Caller (task_router/main) yang decide eksekusi tool berdasarkan is_positive
        new_pending = None
    elif frame != "CONFIRMATION":
        # Frame baru bukan confirmation — pending_action lama jadi stale, clear
        if prev_state.pending_action and not prev_state.pending_action.is_empty():
            _log(f"  [ANALYZER] frame baru ({frame}) bukan CONFIRMATION → clear pending lama")
        new_pending = None

    # FRAME_HINTS dipakai sebagai floor — kalau frame bilang butuh sesuatu,
    # selalu True meski model return False. Model boleh upgrade (False→True)
    # tapi tidak boleh downgrade (True→False) dari apa yang FRAME_HINTS bilang.
    _hints = FRAME_HINTS.get(frame, {})
    _llm_need_history      = bool(parsed.get("need_history", False))
    _llm_need_memory       = bool(parsed.get("need_memory",  False))
    _llm_need_self_memory  = bool(parsed.get("need_self_memory", False))
    _llm_need_tool         = bool(parsed.get("need_tool",    False))

    _self_field_raw = str(parsed.get("self_memory_field", "") or "").strip().lower()
    _self_field = _self_field_raw if _self_field_raw in _SELF_MEMORY_FIELDS else (
        "general" if _self_field_raw else ""
    )
    # need_self_memory boleh di-upgrade ke True kalau model kasih field spesifik
    # tapi lupa set flagnya sendiri — field spesifik = sinyal kuat butuh data karakter.
    if _self_field and _self_field != "":
        _llm_need_self_memory = True

    new_state = ConversationState(
        user_id              = user_id,
        topic                = str(parsed.get("topic", "")),
        frame                = frame,
        intent               = str(parsed.get("intent", "")),
        stage                = str(parsed.get("stage", "new_topic")),
        emotion              = str(parsed.get("emotion", "neutral")),
        references_previous  = bool(parsed.get("references_previous", False)),
        continuation         = parsed.get("stage") == "continuation",
        new_topic            = parsed.get("stage") == "new_topic",
        need_history         = _llm_need_history      or _hints.get("need_history", False),
        need_memory          = _llm_need_memory       or _hints.get("need_memory",  False),
        need_self_memory     = _llm_need_self_memory  or _hints.get("need_self_memory", False),
        self_memory_field    = _self_field,
        need_tool            = _llm_need_tool         or _hints.get("need_tool",    False),
        importance           = float(parsed.get("importance", 0.5)),
        complexity           = compute_simple_complexity(user_input, frame),
        confidence           = float(parsed.get("confidence", 0.7)),
        awaiting_confirmation= False,   # di-set True oleh caller saat set_pending_action()
        pending_action       = new_pending,
        last_topic           = prev_state.topic,
        updated_ts           = int(time.time()),
    )

    save_state(new_state)
    _log(f"  [ANALYZER] → {new_state.summary_line()}")
    return new_state


_POSITIVE_WORDS = {"iya","ya","yes","ok","oke","okay","yaudah","ya udah","gas","lanjut","boleh","silakan","setuju","yoi","yo","sip","siap","jalankan","simpan"}
_NEGATIVE_WORDS = {"jangan","tidak","ga","gak","nggak","ngga","no","batal","cancel","stop","udah ga usah","gausah","engga"}

def _is_positive_confirmation(text: str) -> bool:
    """Heuristik: apakah teks konfirmasi positif atau negatif.
    
    Negatif: cek sebagai WHOLE WORD (bukan substring) agar 'gas' tidak match 'ga'.
    Positif: cek whole word juga, atau default True jika tidak ada sinyal negatif.
    """
    lower = text.strip().lower()
    words = set(lower.split())
    # Cek negatif sebagai whole word
    for w in _NEGATIVE_WORDS:
        if w in words or lower == w:
            return False
    # Cek positif
    for w in _POSITIVE_WORDS:
        if w in words or lower == w:
            return True
    # Default: tidak ada sinyal negatif eksplisit → positif
    return True


# ═════════════════════════════════════════════════════════════════════════════
# PENDING ACTION HELPERS
# ═════════════════════════════════════════════════════════════════════════════

def set_pending_action(user_id: str, tool: str, args: Dict, description: str = ""):
    """
    Dipanggil ketika ada aksi yang butuh konfirmasi user sebelum dieksekusi.
    Mis: Soul bilang "mau aku simpan ya?" → set_pending_action sebelum kirim respons.
    """
    state = load_state(user_id)
    state.pending_action = PendingAction(
        tool=tool, args=args, description=description, created_ts=int(time.time())
    )
    state.awaiting_confirmation = True
    save_state(state)


def get_pending_action(user_id: str) -> Optional[PendingAction]:
    state = load_state(user_id)
    if state.pending_action and not state.pending_action.is_empty():
        return state.pending_action
    return None


def clear_pending_action(user_id: str):
    state = load_state(user_id)
    state.pending_action = None
    state.awaiting_confirmation = False
    save_state(state)


# ═════════════════════════════════════════════════════════════════════════════
# COMPLEXITY SCORE — versi sederhana (dipakai sementara, bisa di-extend ke CCS penuh)
# ═════════════════════════════════════════════════════════════════════════════

_EMOTION_KEYWORDS = {"sedih","marah","kecewa","takut","bingung","putus asa","galau","stress","capek","lelah","depresi","hancur","nangis","menangis","susah","menderita","trauma"}
_KNOWLEDGE_KEYWORDS = {"jelaskan","mengapa","kenapa","bagaimana","analisa","bandingkan","apa bedanya","perbedaan","perbandingan","definisi","cara kerja","maksud","artinya"}
_PERSONA_KEYWORDS = {"roleplay","berpura-pura","anggap kau","jadi pacarku","jadi kau adalah"}

def compute_simple_complexity(text: str, frame: str) -> int:
    """
    Skor kompleksitas sederhana 0-17 (subset dari CCS proposal).
    Dipakai sebagai sinyal awal untuk Soul Nano/Lite/Normal/Deep selection.
    """
    lower = text.lower()
    score = 0

    # Message complexity (panjang kasar)
    word_count = len(text.split())
    if word_count <= 2:        score += 0
    elif word_count <= 8:      score += 1
    elif word_count <= 20:     score += 2
    else:                      score += 3

    # Emotional weight
    if any(k in lower for k in _EMOTION_KEYWORDS):
        score += 3

    # Knowledge weight
    if any(k in lower for k in _KNOWLEDGE_KEYWORDS):
        score += 3

    # Persona weight
    if any(k in lower for k in _PERSONA_KEYWORDS):
        score += 2

    # Frame bonus
    if frame in ("EMOTIONAL", "ROLEPLAY"):
        score += 3
    elif frame == "KNOWLEDGE":
        score += 2
    elif frame in ("FOLLOW_UP", "MEMORY_EDIT", "TASK", "CONFIRMATION"):
        score += 1

    return min(17, score)


def complexity_to_mode(score: int) -> str:
    """Map CCS score → Soul mode (nano/lite/normal/deep)."""
    if score <= 2:  return "nano"
    if score <= 6:  return "lite"
    if score <= 10: return "normal"
    return "deep"
