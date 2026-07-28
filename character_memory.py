"""
character_memory.py — Character Self-Memory (Identitas & Keinginan Karakter).

PERUBAHAN BARU (per-character bin):
- Tiap karakter sekarang punya file bin SENDIRI di folder karakternya
  masing-masing: characters/<nama>/character_memory.bin
- Kalau dipanggil tanpa char_dir (backward compat), tetap pakai
  state/character_memory.bin (multi-karakter lama).
- Migrasi otomatis: kalau load dari char_dir dan file belum ada,
  tapi ada data di file global lama, data dipindahkan ke file karakter.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import struct
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime
from typing import Any, Dict, List, Optional

try:
    import msgpack as _p
    def _dumps(o) -> bytes: return _p.packb(o, use_bin_type=True)
    def _loads(b: bytes): return _p.unpackb(b, raw=False)
except ImportError:
    import pickle as _pk
    def _dumps(o) -> bytes: return _pk.dumps(o, protocol=4)
    def _loads(b: bytes): return _pk.loads(b)

_FILE = "character_memory.bin"
_MAGIC = b"CHM\x01"
_H_FMT = ">4sI"; _H_SZ = struct.calcsize(_H_FMT)
_IX_FMT = ">Q"; _IX_SZ = struct.calcsize(_IX_FMT)
_L_FMT = ">I"; _L_SZ = struct.calcsize(_L_FMT)

_MAX_WANTS = 30
_MAX_FACTS = 100
_DEBUG_LOG_FILE = "character_memory_debug.log"
_DBG_SEP = "═" * 68

# ─── Debug: lihat prompt asli tiap kali generate jalan ───────────────────────

def _resolve_debug(debug: Optional[bool]) -> bool:
    """Tentukan apakah debug logging aktif. Prioritas: argumen eksplisit ->
    env var CHARMEM_DEBUG -> config.DEBUG (kalau config.py ada) -> False."""
    if debug is not None:
        return bool(debug)
    env = os.environ.get("CHARMEM_DEBUG")
    if env is not None:
        return env.strip().lower() in ("1", "true", "yes", "on")
    try:
        import config
        return bool(getattr(config, "DEBUG", False))
    except Exception:
        return False

def _debug_log_path() -> str:
    d = os.path.join(os.path.dirname(os.path.abspath(__file__)), "state")
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, _DEBUG_LOG_FILE)

def _dbg_write(text: str):
    print(text)
    try:
        with open(_debug_log_path(), "a", encoding="utf-8") as f:
            f.write(text + "\n")
    except Exception:
        pass

def _dbg_log_prompt(character_id: str, system: str, user: str):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    _dbg_write(f"\n{_DBG_SEP}\n[CHAR MEMORY][{ts}] generate_via_ai → '{character_id}'\n{_DBG_SEP}")
    _dbg_write("── SYSTEM PROMPT (dikirim ke local model) " + "─" * 24)
    _dbg_write(system)
    _dbg_write("── USER PROMPT / PERSONA (sudah dibersihkan dari bagian teknis) " + "─" * 3)
    _dbg_write(user)

def _dbg_log_raw_response(raw: str):
    _dbg_write("── RAW RESPONSE dari model " + "─" * 40)
    _dbg_write(raw)

def _dbg_log_result(cm: "CharacterMemory"):
    _dbg_write("── HASIL AKHIR (disimpan ke bin) " + "─" * 34)
    _dbg_write(json.dumps(cm.to_dict(), ensure_ascii=False, indent=2))

# ─── Pembersih persona: buang bagian teknis dari blok prompts.soul_* ─────────

_TECHNICAL_HEADER_KEYWORDS = (
    "LOGIKA", "KONDISIONAL", "PERILAKU", "BATASAN", "FORMAT INPUT",
    "FORMAT OUTPUT", "INPUT PROMPT", "OUTPUT FORMAT", "MASUKAN DATA",
    "PANDUAN OVERRIDE", "TASK_RESULT", "CMD", "OVERRIDE", "IDENTITY",
    "HISTORY", "CHAT", "OUTPUT", "CONTEXT", "TASK", "RULES", "CONSTRAINT",
)
_IDENTITY_HEADER_KEYWORDS = ("SYSTEM CORE", "IDENTITAS", "PERSONA", "KEPRIBADIAN", "CHARACTER")

_SECTION_HEADER_RE = re.compile(r'^\[([^\]]+)\]\s*$', re.MULTILINE)

def _strip_technical_sections(text: str) -> str:
    """Buang section prompt yang teknis (logika kondisional, batasan output,
    format input, dst), sisakan cuma section identitas/kepribadian."""
    if not text:
        return ""
    matches = list(_SECTION_HEADER_RE.finditer(text))
    if not matches:
        return text.strip()

    kept: List[str] = []
    preamble = text[:matches[0].start()].strip()
    if preamble:
        kept.append(preamble)

    for i, m in enumerate(matches):
        header = m.group(1).strip().upper()
        start = m.start()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        block = text[start:end].strip()

        is_technical = any(kw in header for kw in _TECHNICAL_HEADER_KEYWORDS)
        is_identity = any(kw in header for kw in _IDENTITY_HEADER_KEYWORDS)

        if i == 0 and not is_technical:
            kept.append(block)
        elif is_identity and not is_technical:
            kept.append(block)

    cleaned = "\n\n".join(kept).strip()
    return cleaned or text.strip()

# ─── Path resolution (global vs per-character) ─────────────────────────────

def _global_path() -> str:
    """Path file bin lama (multi-karakter, backward compat)."""
    d = os.path.join(os.path.dirname(os.path.abspath(__file__)), "state")
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, _FILE)

def _auto_discover_char_dir(character_id: str) -> Optional[str]:
    """
    PATCH: Fallback auto-discovery kalau caller TIDAK memberikan char_dir.
    Meniru logika character_manager._find_character_dir(): cari
    characters/<character_id>/ atau character/<character_id>/ relatif ke
    lokasi module ini, yang sudah punya character.json dan/atau
    character_memory.bin.

    Ini defense-in-depth: idealnya setiap caller (context_composer.py, dll)
    selalu meneruskan char_dir eksplisit dari CharacterManager.char_dir.
    Tapi kalau ada satu titik pemanggilan yang lupa (atau file belum
    di-deploy), fallback ini mencegah data karakter "hilang" begitu saja ke
    file global kosong — root cause paling umum dari "bin sudah digenerate
    tapi tidak kebaca".
    """
    base = os.path.dirname(os.path.abspath(__file__))
    for parent in ("characters", "character"):
        candidate = os.path.join(base, parent, character_id)
        if os.path.isdir(candidate) and (
            os.path.isfile(os.path.join(candidate, "character.json"))
            or os.path.isfile(os.path.join(candidate, _FILE))
        ):
            return candidate
    return None

def _char_path(character_id: str, char_dir: Optional[str] = None) -> str:
    """Path file bin untuk satu karakter.
    Kalau char_dir diberikan: characters/<nama>/character_memory.bin
    Kalau tidak: coba auto-discover folder characters/<nama>/ dulu (PATCH).
    Kalau tetap tidak ketemu: fallback ke state/character_memory.bin
    (global, lama — sengaja dipertahankan untuk backward compat).
    """
    if char_dir:
        return os.path.join(char_dir, _FILE)
    auto = _auto_discover_char_dir(character_id)
    if auto:
        return os.path.join(auto, _FILE)
    return _global_path()

# ─── Low-level binary read/write ─────────────────────────────────────────────

def _read_file(path: str) -> Dict[str, Dict]:
    """Baca file bin (bisa multi-record). Balik dict {character_id: record}."""
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "rb") as f:
            hdr = f.read(_H_SZ)
            if len(hdr) < _H_SZ:
                return {}
            magic, count = struct.unpack(_H_FMT, hdr)
            if magic != _MAGIC or count == 0:
                return {}
            idx = [struct.unpack(_IX_FMT, f.read(_IX_SZ))[0] for _ in range(count)]
            out = {}
            for off in idx:
                f.seek(off)
                lr = f.read(_L_SZ)
                if len(lr) < _L_SZ:
                    continue
                plen = struct.unpack(_L_FMT, lr)[0]
                pr = f.read(plen)
                try:
                    rec = _loads(pr)
                    out[rec["character_id"]] = rec
                except Exception:
                    pass
            return out
    except Exception:
        return {}

def _write_file(path: str, data: Dict[str, Dict]):
    """Tulis dict {character_id: record} ke file bin."""
    items = list(data.values())
    payloads = [None] * len(items)
    for i, r in enumerate(items):
        try:
            payloads[i] = _dumps(r)
        except Exception:
            pass
    valid = [(items[i], payloads[i]) for i in range(len(items)) if payloads[i]]
    count = len(valid)
    ds = _H_SZ + _IX_SZ * count
    offs, cur = [], ds
    for _, p2 in valid:
        offs.append(cur)
        cur += _L_SZ + len(p2)
    try:
        d = os.path.dirname(path)
        if d:
            os.makedirs(d, exist_ok=True)
        with open(path, "wb") as f:
            f.write(struct.pack(_H_FMT, _MAGIC, count))
            for o in offs:
                f.write(struct.pack(_IX_FMT, o))
            for _, p2 in valid:
                f.write(struct.pack(_L_FMT, len(p2)))
                f.write(p2)
    except Exception:
        pass

def _read_char(character_id: str, char_dir: Optional[str] = None) -> Optional[Dict]:
    """Baca record satu karakter dari file bin-nya (global atau per-character)."""
    path = _char_path(character_id, char_dir)
    data = _read_file(path)
    return data.get(character_id)

def _write_char(cm: "CharacterMemory", char_dir: Optional[str] = None):
    """Tulis record satu karakter ke file bin-nya."""
    path = _char_path(cm.character_id, char_dir)
    data = _read_file(path)
    data[cm.character_id] = cm.to_dict()
    _write_file(path, data)

# ─── Migrasi otomatis dari file global lama ke per-character ─────────────────

def _migrate_from_global(character_id: str, char_dir: Optional[str] = None) -> bool:
    """Kalau file per-character belum ada tapi ada data di file global lama,
    pindahkan data karakter tsb ke file per-character-nya. Return True kalau
    berhasil dimigrasi."""
    if not char_dir:
        return False
    char_path = _char_path(character_id, char_dir)
    if os.path.exists(char_path):
        return False
    global_path = _global_path()
    if not os.path.exists(global_path):
        return False
    global_data = _read_file(global_path)
    if character_id not in global_data:
        return False
    _write_file(char_path, {character_id: global_data[character_id]})
    del global_data[character_id]
    if global_data:
        _write_file(global_path, global_data)
    else:
        try:
            os.remove(global_path)
        except Exception:
            pass
    print(f"[CHAR MEMORY] Migrasi otomatis: '{character_id}' dipindahkan dari state/{_FILE} ke {char_dir}/{_FILE}")
    return True

# ─── Data model ────────────────────────────────────────────────────────────

_LABELS = {
    "full_name": "Nama",
    "birthday": "Tanggal lahir",
    "zodiac": "Zodiak",
    "age": "Umur",
    "personality": "Kepribadian",
    "likes": "Suka",
    "dislikes": "Tidak suka",
    "hobbies": "Hobi",
    "fears": "Takut akan",
    "backstory": "Latar belakang",
}
_SUMMARY_PRIORITY = ["full_name", "birthday", "zodiac", "age", "personality", "likes", "dislikes", "hobbies"]

_LEGACY_TOP_LEVEL_KEYS = (
    "full_name", "birthday", "zodiac", "age", "likes", "dislikes",
    "hobbies", "personality", "backstory", "fears",
)

def _fmt_value(v: Any, max_len: int = 180) -> str:
    if isinstance(v, list):
        s = ", ".join(str(x) for x in v[:6])
    elif isinstance(v, dict):
        s = json.dumps(v, ensure_ascii=False)
    else:
        s = str(v)
    return s if len(s) <= max_len else s[:max_len].rstrip() + "…"

@dataclass
class CharacterMemory:
    character_id: str = ""
    attributes: Dict[str, Any] = field(default_factory=dict)
    wants: List[Dict] = field(default_factory=list)
    custom_facts: List[Dict] = field(default_factory=list)
    created_ts: int = 0
    updated_ts: int = 0

    def add_want(self, text: str, priority: float = 0.5):
        text = text.strip()
        if not text:
            return
        wid = hashlib.md5(f"{self.character_id}{text}".encode()).hexdigest()[:8]
        if any(w.get("id") == wid for w in self.wants):
            return
        self.wants.append({"id": wid, "text": text, "priority": priority, "ts": int(time.time())})
        if len(self.wants) > _MAX_WANTS:
            self.wants.sort(key=lambda x: x.get("priority", 0), reverse=True)
            self.wants = self.wants[:_MAX_WANTS]

    def add_fact(self, text: str):
        text = text.strip()
        if not text:
            return
        self.custom_facts.append({"text": text, "ts": int(time.time())})
        if len(self.custom_facts) > _MAX_FACTS:
            self.custom_facts = self.custom_facts[-_MAX_FACTS:]

    def set_field(self, key: str, value: Any):
        key = key.strip()
        if not key:
            return
        if value in (None, "", [], {}):
            self.attributes.pop(key, None)
        else:
            self.attributes[key] = value

    def remove_field(self, key: str):
        self.attributes.pop(key, None)

    def get_field(self, key: str, default: Any = None) -> Any:
        return self.attributes.get(key, default)

    def field_keys(self) -> List[str]:
        return list(self.attributes.keys())

    def get_wants(self, n: int = 5) -> List[str]:
        sorted_w = sorted(self.wants, key=lambda x: x.get("priority", 0), reverse=True)
        return [w["text"] for w in sorted_w[:n]]

    def field_summary_for_context(self, keys: List[str]) -> str:
        """
        PATCH v3: versi TARGETED dari summary_for_context() — cuma keluarkan
        field yang diminta (mis. ["birthday"]), bukan seluruh profil.
        Ini yang bikin context hemat token: kalau user cuma nanya 1 hal
        spesifik, kita nggak perlu kirim nama+zodiak+suka+tidak_suka+hobi+dst
        sekaligus, cukup baris yang relevan.

        keys boleh berisi:
          - key attribute biasa ("birthday", "likes", dst — lihat _LABELS)
          - "wants"        → keinginan/goals karakter
          - "custom_facts" → catatan tambahan
          - "general"      → sinyal "tidak spesifik", diabaikan di sini
                              (caller yang decide fallback ke summary penuh)

        Return "(belum ada data karakter)" kalau semua key yang diminta kosong,
        supaya caller tahu harus fallback (mis. ke summary_for_context() penuh
        atau ke tier validasi berikutnya).
        """
        lines: List[str] = []
        for k in keys:
            k = (k or "").strip().lower()
            if not k or k == "general":
                continue
            if k == "wants":
                wants = self.get_wants(5)
                if wants:
                    lines.append(f" - Keinginan: {'; '.join(wants)}")
                continue
            if k == "custom_facts":
                recent = [f["text"] for f in self.custom_facts[-3:]]
                if recent:
                    lines.append(f" - Catatan tambahan: {'; '.join(recent)}")
                continue
            v = self.attributes.get(k)
            if v in (None, "", [], {}):
                continue
            label = _LABELS.get(k) or k.replace("_", " ").strip().capitalize()
            lines.append(f" - {label}: {_fmt_value(v)}")

        if not lines:
            return "(belum ada data karakter)"
        return "\n".join(["[Profil Diri Karakter]"] + lines)

    def summary_for_context(self) -> str:
        if not self.attributes and not self.wants and not self.custom_facts:
            return "(belum ada data karakter)"

        lines = ["[Profil Diri Karakter]"]
        ordered_keys = [k for k in _SUMMARY_PRIORITY if k in self.attributes]
        ordered_keys += [k for k in self.attributes.keys() if k not in ordered_keys]
        for k in ordered_keys:
            v = self.attributes.get(k)
            if v in (None, "", [], {}):
                continue
            label = _LABELS.get(k) or k.replace("_", " ").strip().capitalize()
            lines.append(f" - {label}: {_fmt_value(v)}")

        wants = self.get_wants(3)
        if wants:
            lines.append(f" - Keinginan: {'; '.join(wants)}")
        recent_facts = [f["text"] for f in self.custom_facts[-3:]]
        if recent_facts:
            lines.append(f" - Catatan tambahan: {'; '.join(recent_facts)}")
        return "\n".join(lines) if len(lines) > 1 else "(belum ada data karakter)"

    def to_dict(self) -> Dict:
        return asdict(self)

    @staticmethod
    def from_dict(d: Dict) -> "CharacterMemory":
        cm = CharacterMemory(character_id=d.get("character_id", ""))
        cm.created_ts = d.get("created_ts", 0) or 0
        cm.updated_ts = d.get("updated_ts", 0) or 0
        cm.wants = list(d.get("wants") or [])
        cm.custom_facts = list(d.get("custom_facts") or [])
        if isinstance(d.get("attributes"), dict):
            cm.attributes = dict(d["attributes"])
        else:
            migrated = {}
            for k in _LEGACY_TOP_LEVEL_KEYS:
                v = d.get(k)
                if v not in (None, "", [], {}):
                    migrated[k] = v
            cm.attributes = migrated
        return cm

    @staticmethod
    def seed_from_character_json(character_id: str, char_json: Dict) -> "CharacterMemory":
        now = int(time.time())
        cm = CharacterMemory(character_id=character_id, created_ts=now, updated_ts=now)

        profile = char_json.get("profile")
        profile = profile if isinstance(profile, dict) else {}

        if profile:
            skip = {"wants", "keinginan", "goals"}
            cm.attributes = {
                k: v for k, v in profile.items()
                if k not in skip and v not in (None, "", [], {})
            }
            cm.attributes.setdefault("full_name", char_json.get("name", character_id))
        else:
            aliases = {
                "full_name": ("full_name", "name", "nama"),
                "birthday": ("birthday", "tanggal_lahir", "birth_date"),
                "zodiac": ("zodiac", "zodiak"),
                "age": ("age", "umur"),
                "likes": ("likes", "suka"),
                "dislikes": ("dislikes", "tidak_suka"),
                "hobbies": ("hobbies", "hobi"),
                "personality": ("personality", "personality_traits", "kepribadian"),
                "fears": ("fears", "ketakutan"),
                "backstory": ("backstory", "bio", "latar_belakang", "description"),
            }
            list_fields = {"likes", "dislikes", "hobbies", "personality", "fears"}
            attrs: Dict[str, Any] = {}
            for canon, keys in aliases.items():
                for k in keys:
                    if k in char_json and char_json[k] not in (None, "", [], {}):
                        v = char_json[k]
                        if canon in list_fields and isinstance(v, str):
                            v = [s.strip() for s in v.split(",") if s.strip()]
                        attrs[canon] = v
                        break
            attrs.setdefault("full_name", char_json.get("name", character_id))
            cm.attributes = attrs

        wants_raw = (
            profile.get("wants") or profile.get("keinginan") or profile.get("goals")
            or char_json.get("wants") or char_json.get("keinginan") or char_json.get("goals")
            or []
        )
        if isinstance(wants_raw, str):
            wants_raw = [wants_raw]
        for i, w in enumerate(wants_raw):
            if isinstance(w, dict):
                cm.add_want(w.get("text", ""), w.get("priority", 0.5))
            else:
                cm.add_want(str(w), priority=1.0 - i * 0.1)

        return cm

# ─── PATCH v3: Field detection (hemat token) ─────────────────────────────────
#
# Tujuan: waktu user tanya sesuatu yang spesifik tentang karakter (mis. "kapan
# ulang tahunmu?"), composer TIDAK perlu kirim seluruh character_memory.bin
# (nama+zodiak+suka+tidak_suka+hobi+takut+dst) ke Soul — cukup 1 baris yang
# relevan. Ada 2 tingkat deteksi, dari yang paling murah ke yang paling mahal:
#
#   Tier 1 — guess_field()            : keyword matching lokal, TANPA LLM call
#                                        sama sekali. Nangkep >90% kasus umum.
#   Tier 2 — resolve_relevant_fields() : kalau Tier 1 gagal & need_self_memory
#                                        tetap true, probe field SATU PER SATU
#                                        ke LLM murah (bukan kirim semua field
#                                        sekaligus) dan STOP begitu dapat match
#                                        pertama — ini bagian "validasi
#                                        bertahap, kirim sedikit-sedikit" yang
#                                        diminta, bukan dump semua isi bin.
#
# Kalau kedua tier gagal, caller (context_composer.py) fallback ke
# summary_for_context() penuh seperti perilaku lama — jadi tidak ada regresi.

_FIELD_KEYWORDS: Dict[str, tuple] = {
    "birthday":    ("ulang tahun", "ultah", "tanggal lahir", "lahir", "birthday", "bday"),
    "zodiac":      ("zodiak", "zodiac", "rasi bintang", "bintang"),
    "age":         ("umur", "usia", "berapa tahun", "age"),
    "full_name":   ("siapa namamu", "nama kamu", "nama lengkap", "your name"),
    "likes":       ("suka apa", "kesukaan", "hal yang disukai", "favorit", "likes"),
    "dislikes":    ("tidak suka", "gak suka", "benci", "dislikes", "hindari"),
    "hobbies":     ("hobi", "kegiatan favorit", "hobby", "hobbies"),
    "fears":       ("takut", "ketakutan", "phobia", "fears"),
    "personality": ("sifat", "kepribadian", "karaktermu", "personality"),
    "backstory":   ("masa lalu", "latar belakang", "cerita hidup", "backstory", "bio"),
    "wants":       ("keinginan", "cita-cita", "mau apa", "goals", "impian"),
}

def guess_field(text: str) -> Optional[str]:
    """
    Tier 1: cocokkan teks (biasanya state.topic atau user_input) ke salah satu
    canonical field via keyword sederhana. TANPA LLM call — nol biaya token
    tambahan. Return None kalau tidak ada yang cocok (caller lanjut ke tier
    berikutnya atau fallback summary penuh).
    """
    if not text:
        return None
    t = text.strip().lower()
    for canon, keywords in _FIELD_KEYWORDS.items():
        for kw in keywords:
            if kw in t:
                return canon
    return None

_FIELD_PROBE_SYS = (
    "Jawab HANYA: yes atau no\n"
    "yes = pertanyaan user MEMBUTUHKAN data field ini untuk dijawab\n"
    "no  = field ini tidak relevan dengan pertanyaan user\n"
)

def _probe_field_relevant(user_text: str, label: str, value_preview: str, llm_call) -> bool:
    """Satu probe kecil: kirim SATU field (label + preview singkat) ke LLM
    murah, tanya relevan atau tidak. Dipakai iteratif oleh
    resolve_relevant_fields() supaya tidak pernah kirim semua field sekaligus."""
    try:
        resp = llm_call(
            "react",
            messages=[
                {"role": "system", "content": _FIELD_PROBE_SYS},
                {"role": "user", "content": (
                    f'Pertanyaan user: "{user_text}"\n'
                    f'Field: {label} = {value_preview[:120]}'
                )},
            ],
            temperature=0.0,
            max_tokens=5,
        )
        raw = (resp.choices[0].message.content or "").strip().lower()
        return raw.startswith("yes") or raw.startswith("ya")
    except Exception:
        return False

def resolve_relevant_fields(
    cm: "CharacterMemory",
    query_text: str,
    llm_call=None,
    dbg=None,
    max_probe: int = 6,
) -> List[str]:
    """
    Cari field mana yang relevan dengan query_text, dari yang termurah:
      1. guess_field() lokal (tanpa LLM) — kalau match & field itu ada isinya
         di cm, langsung return.
      2. Kalau tidak match & llm_call tersedia: probe field SATU PER SATU
         (urut _SUMMARY_PRIORITY dulu, baru sisanya) dan STOP di probe
         pertama yang "yes" — jadi paling banyak `max_probe` field kecil
         terkirim ke LLM, bukan seluruh bin sekaligus.
    Return list kosong kalau tidak ada yang ketemu → caller fallback ke
    summary_for_context() penuh (perilaku lama, tetap aman).
    """
    def _log(msg):
        if dbg: dbg.line(msg)

    guessed = guess_field(query_text)
    if guessed:
        has_value = (
            (guessed == "wants" and cm.wants)
            or (guessed != "wants" and cm.attributes.get(guessed) not in (None, "", [], {}))
        )
        if has_value:
            _log(f"  [CHARMEM] tier1 keyword match → field={guessed}")
            return [guessed]

    if not llm_call:
        return []

    ordered = [k for k in _SUMMARY_PRIORITY if k in cm.attributes]
    ordered += [k for k in cm.attributes.keys() if k not in ordered]
    if cm.wants:
        ordered.append("wants")

    probed = 0
    for k in ordered:
        if probed >= max_probe:
            break
        if k == "wants":
            label = "Keinginan"
            preview = "; ".join(cm.get_wants(3))
        else:
            label = _LABELS.get(k) or k.replace("_", " ").capitalize()
            preview = _fmt_value(cm.attributes.get(k))
        probed += 1
        _log(f"  [CHARMEM] tier2 probe {probed}/{max_probe} → field={k}")
        if _probe_field_relevant(query_text, label, preview, llm_call):
            _log(f"  [CHARMEM] tier2 match → field={k}")
            return [k]

    _log("  [CHARMEM] tidak ada field relevan ditemukan (fallback ke summary penuh)")
    return []

# ─── AI-based auto-generate ─────────────────────────────────────────────────

_PROFILE_SCHEMA_HINT = """{
  "full_name": "...",
  "birthday": "...",
  "zodiac": "...",
  "likes": ["...", "..."],
  "dislikes": ["...", "..."],
  "hobbies": ["...", "..."],
  "wants": ["...", "..."],
  "personality": ["...", "..."],
  "fears": ["...", "..."],
  "backstory": "..."
}"""

_PROFILE_SYSTEM_PROMPT = (
    "[PROFILE EXTRACT] Kamu membaca deskripsi kepribadian sebuah karakter AI VTuber, "
    "lalu MELENGKAPI data identitas pribadi karakter tsb secara detail dan KONSISTEN "
    "dengan kepribadiannya. Data ini akan jadi memory permanen karakter, jadi buat "
    "masuk akal, spesifik, dan JANGAN generic/template.\n"
    "Field TIDAK dibatasi baku. Contoh struktur dasar di bawah ini cuma titik awal — "
    "kamu BEBAS menambah field baru apa pun yang relevan dan spesifik untuk karakter "
    "ini (misal: 'makanan_favorit', 'kebiasaan_unik', 'problem_today', 'trauma_kecil', "
    "'kutipan_andalan', dst). Tiap karakter boleh punya set field yang beda-beda, "
    "tidak harus semua karakter sama persis.\n"
    "Kalau di 'Data yang sudah diketahui' sudah ada isinya, JANGAN diubah/dihapus, "
    "cukup lengkapi atau tambah field lain yang relevan.\n"
    f"Contoh struktur dasar (boleh ditambah field lain di luar ini):\n{_PROFILE_SCHEMA_HINT}\n"
    "Output HANYA JSON object valid (flat, key snake_case), tanpa teks/markdown lain."
)

def _extract_persona_text(char_json: Dict) -> str:
    name = char_json.get("name", "")
    prompts = char_json.get("prompts", {})
    prompts = prompts if isinstance(prompts, dict) else {}
    raw_persona = prompts.get("soul_final_system") or prompts.get("soul_system") or ""
    persona = _strip_technical_sections(raw_persona)

    known = char_json.get("profile", {})
    known = known if isinstance(known, dict) else {}

    parts = [f"Nama karakter: {name}"]
    if persona:
        parts.append(f"Deskripsi kepribadian:\n{persona[:2500]}")
    if known:
        parts.append(f"Data yang sudah diketahui (jangan diubah):\n{json.dumps(known, ensure_ascii=False)}")
    return "\n\n".join(parts)

def _default_llm_call(system: str, user: str, pass_name: str = "profile_extract") -> str:
    import config
    client = config.get_client(pass_name)
    model = config.get_model(pass_name)
    extra_body = config.get_extra_body(pass_name)
    resp = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        max_tokens=2040,
        temperature=0.8,
        extra_body=extra_body or None,
    )
    return (resp.choices[0].message.content or "").strip()

def _parse_json_loose(raw: str) -> Optional[Dict]:
    text = raw.strip()
    if text.startswith("```"):
        chunks = text.split("```")
        text = chunks[1] if len(chunks) > 1 else text
        if text.lower().startswith("json"):
            text = text[4:]
    try:
        return json.loads(text.strip())
    except Exception:
        s, e = text.find("{"), text.rfind("}")
        if s != -1 and e > s:
            try:
                return json.loads(text[s:e + 1])
            except Exception:
                pass
    return None

def generate_via_ai(
    character_id: str,
    char_json: Dict,
    llm_call=None,
    debug: Optional[bool] = None,
) -> "CharacterMemory":
    caller = llm_call or _default_llm_call
    user_prompt = _extract_persona_text(char_json)
    dbg = _resolve_debug(debug)

    if dbg:
        _dbg_log_prompt(character_id, _PROFILE_SYSTEM_PROMPT, user_prompt)

    try:
        raw = caller(_PROFILE_SYSTEM_PROMPT, user_prompt)
        if dbg:
            _dbg_log_raw_response(raw)
        data = _parse_json_loose(raw)
        if not isinstance(data, dict) or not data:
            raise ValueError(f"output bukan JSON object valid: {raw[:200]!r}")
    except Exception as e:
        print(f"[CHAR MEMORY] AI-generate gagal untuk '{character_id}' ({e}) — fallback ke character.json biasa.")
        cm = CharacterMemory.seed_from_character_json(character_id, char_json)
        if dbg:
            _dbg_log_result(cm)
        return cm

    now = int(time.time())
    cm = CharacterMemory(character_id=character_id, created_ts=now, updated_ts=now)

    wants_raw = data.pop("wants", None) or data.pop("keinginan", None) or []
    if isinstance(wants_raw, str):
        wants_raw = [wants_raw]
    if isinstance(wants_raw, list):
        for i, w in enumerate(wants_raw):
            if isinstance(w, dict):
                cm.add_want(str(w.get("text", "")), w.get("priority", 1.0 - i * 0.1))
            else:
                cm.add_want(str(w), priority=1.0 - i * 0.1)

    for k, v in data.items():
        if v in (None, "", [], {}):
            continue
        cm.attributes[str(k)] = v
    cm.attributes.setdefault("full_name", char_json.get("name", character_id))

    if dbg:
        _dbg_log_result(cm)
    return cm

# ─── Public API (dengan support char_dir opsional) ─────────────────────────

def load(
    character_id: str,
    char_json: Optional[Dict] = None,
    use_ai: bool = True,
    llm_call=None,
    debug: Optional[bool] = None,
    char_dir: Optional[str] = None,
) -> CharacterMemory:
    """
    Ambil CharacterMemory untuk karakter tsb.
    
    char_dir: folder karakter (mis. characters/liana). Kalau diberikan,
    file bin disimpan di folder karakter tersebut. Kalau None, fallback
    ke state/character_memory.bin (backward compat).
    """
    _migrate_from_global(character_id, char_dir)

    raw = _read_char(character_id, char_dir)
    if raw is not None:
        try:
            return CharacterMemory.from_dict(raw)
        except Exception:
            pass

    if not char_json:
        cm = CharacterMemory(character_id=character_id, created_ts=int(time.time()))
    elif use_ai:
        cm = generate_via_ai(character_id, char_json, llm_call, debug=debug)
    else:
        cm = CharacterMemory.seed_from_character_json(character_id, char_json)

    save(cm, char_dir=char_dir)
    return cm

def save(cm: CharacterMemory, char_dir: Optional[str] = None):
    cm.updated_ts = int(time.time())
    _write_char(cm, char_dir)

def exists(character_id: str, char_dir: Optional[str] = None) -> bool:
    _migrate_from_global(character_id, char_dir)
    return _read_char(character_id, char_dir) is not None

def clear(character_id: str, char_dir: Optional[str] = None):
    path = _char_path(character_id, char_dir)
    data = _read_file(path)
    if character_id in data:
        del data[character_id]
        if data:
            _write_file(path, data)
        else:
            try:
                os.remove(path)
            except Exception:
                pass

def list_characters(char_dir: Optional[str] = None) -> List[str]:
    if char_dir:
        path = os.path.join(char_dir, _FILE)
        if os.path.exists(path):
            return sorted(_read_file(path).keys())
        return []
    return sorted(_read_file(_global_path()).keys())

def _read_character_json(char_manager, name: str) -> Dict:
    char_dir = os.path.join(char_manager.dir, name)
    char_json_path = os.path.join(char_dir, "character.json")
    char_json = {}
    if os.path.isfile(char_json_path):
        try:
            with open(char_json_path, "r", encoding="utf-8") as f:
                char_json = json.load(f)
        except Exception:
            pass
    return char_json

def ensure_all(
    char_manager,
    force: bool = False,
    use_ai: bool = True,
    llm_call=None,
    debug: Optional[bool] = None,
) -> List[str]:
    generated = []
    for name in char_manager.list_characters():
        char_dir = os.path.join(char_manager.dir, name)
        if exists(name, char_dir=char_dir) and not force:
            continue
        char_json = _read_character_json(char_manager, name)
        cm = (
            generate_via_ai(name, char_json, llm_call, debug=debug)
            if use_ai else
            CharacterMemory.seed_from_character_json(name, char_json)
        )
        save(cm, char_dir=char_dir)
        generated.append(name)
    return generated

def regenerate(
    char_manager,
    character_id: str,
    use_ai: bool = True,
    llm_call=None,
    debug: Optional[bool] = None,
) -> CharacterMemory:
    char_dir = os.path.join(char_manager.dir, character_id)
    char_json = _read_character_json(char_manager, character_id)
    cm = (
        generate_via_ai(character_id, char_json, llm_call, debug=debug)
        if use_ai else
        CharacterMemory.seed_from_character_json(character_id, char_json)
    )
    save(cm, char_dir=char_dir)
    return cm

# ─── Utility: export/import ke JSON ──────────────────────────────────────────

def export_json(character_id: str, out_path: Optional[str] = None, char_dir: Optional[str] = None) -> str:
    cm = load(character_id, char_dir=char_dir)
    if out_path:
        pass
    elif char_dir:
        out_path = os.path.join(char_dir, f"{character_id}_memory_export.json")
    else:
        out_path = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "state", f"{character_id}_memory_export.json"
        )
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(cm.to_dict(), f, indent=2, ensure_ascii=False)
    return out_path

def import_json(character_id: str, json_path: str, char_dir: Optional[str] = None):
    with open(json_path, "r", encoding="utf-8") as f:
        d = json.load(f)
    d["character_id"] = character_id
    cm = CharacterMemory.from_dict(d)
    save(cm, char_dir=char_dir)

# ─── CLI kecil buat testing & regenerate manual ─────────────────────────────

def _get_char_manager():
    from character_manager import CharacterManager
    return CharacterManager()

if __name__ == "__main__":
    import sys

    args = sys.argv[1:]
    force = "--force" in args
    use_ai = "--no-ai" not in args
    debug_flag = True if "--debug" in args else None
    args = [a for a in args if a not in ("--force", "--no-ai", "--debug")]

    if not args:
        print(
            "Usage:\n"
            "  python character_memory.py list\n"
            "  python character_memory.py show <name>\n"
            "  python character_memory.py generate <name> [--force] [--no-ai] [--debug]\n"
            "  python character_memory.py generate-all [--force] [--no-ai] [--debug]\n"
            "  python character_memory.py export <name>\n"
            "  python character_memory.py import <name> <json_path>\n"
            "  python character_memory.py clear <name>\n"
            "\n"
            "Default: generate/generate-all pakai AI. Tambah --no-ai untuk cuma\n"
            "pakai blok 'profile' manual di character.json tanpa manggil model.\n"
            "Tambah --debug untuk lihat prompt asli & raw response dari model."
        )
        sys.exit(0)

    cmd = args[0]
    mgr = _get_char_manager() if cmd in ("generate", "generate-all", "show", "export", "clear") else None

    if cmd == "list":
        if mgr:
            for name in mgr.list_characters():
                char_dir = os.path.join(mgr.dir, name)
                has_mem = exists(name, char_dir=char_dir)
                print(f"  {name} {'[ada]' if has_mem else '[belum ada]'}")
        else:
            print(list_characters())

    elif cmd == "show" and len(args) > 1:
        name = args[1]
        char_dir = os.path.join(mgr.dir, name) if mgr else None
        print(load(name, char_dir=char_dir).summary_for_context())

    elif cmd == "generate" and len(args) > 1:
        name = args[1]
        if name not in mgr.list_characters():
            print(f"[!] Character '{name}' tidak ditemukan di folder {mgr.dir}")
            sys.exit(1)
        char_dir = os.path.join(mgr.dir, name)
        if exists(name, char_dir=char_dir) and not force:
            print(f"[i] Memory '{name}' sudah ada. Pakai --force untuk menimpa ulang.")
        else:
            mode = "AI (local model)" if use_ai else "character.json manual"
            print(f"[i] Generating '{name}' via {mode}...")
            regenerate(mgr, name, use_ai=use_ai, debug=debug_flag)
            print(f"[OK] Memory '{name}' berhasil di-generate di {char_dir}/{_FILE}")

    elif cmd == "generate-all":
        mode = "AI (local model)" if use_ai else "character.json manual"
        print(f"[i] Generating semua karakter via {mode}...")
        generated = ensure_all(mgr, force=force, use_ai=use_ai, debug=debug_flag)
        if generated:
            for name in generated:
                char_dir = os.path.join(mgr.dir, name)
                print(f"  [OK] {name} -> {char_dir}/{_FILE}")
        else:
            print("[i] Tidak ada yang di-generate (semua sudah ada). Pakai --force untuk paksa regenerate semua.")

    elif cmd == "export" and len(args) > 1:
        name = args[1]
        char_dir = os.path.join(mgr.dir, name) if mgr else None
        print("Exported to:", export_json(name, char_dir=char_dir))

    elif cmd == "import" and len(args) > 2:
        name = args[1]
        char_dir = os.path.join(mgr.dir, name) if mgr else None
        import_json(name, args[2], char_dir=char_dir)
        print(f"[OK] Memory '{name}' berhasil di-import dari {args[2]}")

    elif cmd == "clear" and len(args) > 1:
        name = args[1]
        char_dir = os.path.join(mgr.dir, name) if mgr else None
        clear(name, char_dir=char_dir)
        print(f"[OK] Cleared memory for '{name}'")

    else:
        print("Command tidak dikenali atau argumen kurang. Jalankan tanpa argumen untuk lihat usage.")