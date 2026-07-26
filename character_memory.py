"""
character_memory.py — Character Self-Memory (Identitas & Keinginan Karakter).

Berbeda dari long_memory.py / relationship_memory.py (yang menyimpan data
tentang USER), modul ini menyimpan data tentang KARAKTER itu sendiri:
tanggal lahir, keinginan, hobi, kepribadian, dst. Satu file bin menyimpan
memory untuk SEMUA karakter, di-index per character_id (nama folder di
character/<nama>/).

Menyimpan:
- identity     : nama lengkap, tanggal lahir, zodiak, umur (opsional)
- likes / dislikes / hobbies
- wants        : keinginan / cita-cita karakter (list, bisa prioritas)
- personality  : trait kepribadian singkat
- backstory    : latar belakang cerita
- fears        : ketakutan (opsional, buat roleplay lebih dalam)
- custom_facts : fakta bebas yang bisa ditambah AI/dev kapan saja

Auto-generate: saat CharacterManager.load(nama) dipanggil pertama kali dan
belum ada data character_memory untuk karakter tsb, modul ini otomatis
membuat entry baru dengan men-seed dari character.json (jika field terkait
ada di sana), lalu simpan permanen ke state/character_memory.bin.

Persist: state/character_memory.bin — satu file untuk semua karakter,
format sama seperti long_memory.py (header magic + index + payload
msgpack/pickle) supaya konsisten dengan modul memory lain di project ini.
"""

from __future__ import annotations

import hashlib
import json
import os
import struct
import time
from dataclasses import dataclass, field, asdict
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


def _path() -> str:
    d = os.path.join(os.path.dirname(os.path.abspath(__file__)), "state")
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, _FILE)


# ─── Low-level binary read/write (pola sama dengan long_memory.py) ───────────

def _read_all() -> Dict[str, Dict]:
    p = _path()
    if not os.path.exists(p):
        return {}
    try:
        with open(p, "rb") as f:
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


def _write_all(data: Dict[str, Dict]):
    p = _path()
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
        with open(p, "wb") as f:
            f.write(struct.pack(_H_FMT, _MAGIC, count))
            for o in offs:
                f.write(struct.pack(_IX_FMT, o))
            for _, p2 in valid:
                f.write(struct.pack(_L_FMT, len(p2)))
                f.write(p2)
    except Exception:
        pass


# ─── Data model ────────────────────────────────────────────────────────────

@dataclass
class CharacterMemory:
    character_id: str = ""
    full_name: str = ""
    birthday: str = ""              # contoh: "12 Januari" atau "2003-01-12"
    zodiac: str = ""
    age: Optional[int] = None
    likes: List[str] = field(default_factory=list)
    dislikes: List[str] = field(default_factory=list)
    hobbies: List[str] = field(default_factory=list)
    wants: List[Dict] = field(default_factory=list)      # [{text, priority, ts}]
    personality: List[str] = field(default_factory=list)
    backstory: str = ""
    fears: List[str] = field(default_factory=list)
    custom_facts: List[Dict] = field(default_factory=list)  # [{text, ts}]
    created_ts: int = 0
    updated_ts: int = 0

    # ── Mutators ──────────────────────────────────────────────────────────

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
        """Update field identitas dasar secara langsung (birthday, zodiac, dll)."""
        if hasattr(self, key):
            setattr(self, key, value)

    # ── Query untuk dijawab AI ───────────────────────────────────────────

    def get_wants(self, n: int = 5) -> List[str]:
        sorted_w = sorted(self.wants, key=lambda x: x.get("priority", 0), reverse=True)
        return [w["text"] for w in sorted_w[:n]]

    def summary_for_context(self) -> str:
        """Dipanggil context_composer.py untuk inject profil karakter ke prompt."""
        lines = ["[Profil Diri Karakter]"]
        if self.full_name:
            lines.append(f" - Nama: {self.full_name}")
        if self.birthday:
            lines.append(f" - Tanggal lahir: {self.birthday}" + (f" ({self.zodiac})" if self.zodiac else ""))
        if self.personality:
            lines.append(f" - Kepribadian: {', '.join(self.personality[:5])}")
        if self.likes:
            lines.append(f" - Suka: {', '.join(self.likes[:5])}")
        if self.dislikes:
            lines.append(f" - Tidak suka: {', '.join(self.dislikes[:5])}")
        if self.hobbies:
            lines.append(f" - Hobi: {', '.join(self.hobbies[:5])}")
        wants = self.get_wants(3)
        if wants:
            lines.append(f" - Keinginan: {'; '.join(wants)}")
        if self.fears:
            lines.append(f" - Takut akan: {', '.join(self.fears[:3])}")
        if self.backstory:
            lines.append(f" - Latar belakang: {self.backstory[:150]}")
        recent_facts = [f["text"] for f in self.custom_facts[-3:]]
        if recent_facts:
            lines.append(f" - Catatan tambahan: {'; '.join(recent_facts)}")
        return "\n".join(lines) if len(lines) > 1 else "(belum ada data karakter)"

    def to_dict(self) -> Dict:
        return asdict(self)

    @staticmethod
    def from_dict(d: Dict) -> "CharacterMemory":
        cm = CharacterMemory(character_id=d.get("character_id", ""))
        for k, v in d.items():
            if hasattr(cm, k):
                setattr(cm, k, v)
        return cm

    # ── Seed dari character.json ─────────────────────────────────────────

    @staticmethod
    def seed_from_character_json(character_id: str, char_json: Dict) -> "CharacterMemory":
        """
        Bangun CharacterMemory awal dari isi character.json.

        PRIORITAS SUMBER DATA:
        1. Blok "profile": {...} kalau ada di character.json — cara yang
           DIREKOMENDASIKAN, supaya data identitas (birthday, wants, dll)
           terpisah rapi dari blok "prompts" yang biasanya sudah besar/panjang.
        2. Kalau tidak ada blok "profile", fallback cek field yang sama
           persis di level atas character.json (top-level).

        Kalau character.json tidak punya field-field ini sama sekali,
        hasilnya CharacterMemory kosong (cuma full_name terisi dari "name")
        — kamu tinggal tambahkan blok "profile" ke character.json lalu
        jalankan `python character_memory.py generate <nama> --force`,
        atau isi manual lewat export_json()/import_json().

        Contoh blok yang perlu ditambahkan di character.json:
            "profile": {
                "full_name": "Liana Elcart",
                "birthday": "12 Januari",
                "zodiac": "Capricorn",
                "likes": ["kopi", "hujan"],
                "dislikes": ["keramaian"],
                "hobbies": ["menggambar", "bermain gitar"],
                "wants": ["ingin jalan-jalan ke Jepang", "ingin punya kucing"],
                "personality": ["anggun", "keibuan", "manja", "tegas saat perlu"],
                "fears": ["ketinggian"],
                "backstory": "Liana tumbuh di kota kecil dan suka menulis cerita."
            }
        """
        now = int(time.time())
        cm = CharacterMemory(character_id=character_id, created_ts=now, updated_ts=now)

        profile = char_json.get("profile")
        profile = profile if isinstance(profile, dict) else {}

        def pick(*keys, default=""):
            # 1) cek di blok "profile" dulu
            for k in keys:
                if k in profile and profile[k]:
                    return profile[k]
            # 2) fallback ke top-level character.json
            for k in keys:
                if k in char_json and char_json[k]:
                    return char_json[k]
            return default

        cm.full_name = pick("full_name", "name", "nama", default=character_id)
        cm.birthday = pick("birthday", "tanggal_lahir", "birth_date")
        cm.zodiac = pick("zodiac", "zodiak")
        age_val = pick("age", "umur", default=None)
        cm.age = int(age_val) if isinstance(age_val, (int, str)) and str(age_val).isdigit() else None

        def as_list(*keys):
            for src in (profile, char_json):
                for k in keys:
                    v = src.get(k)
                    if isinstance(v, list):
                        return list(v)
                    if isinstance(v, str) and v:
                        return [s.strip() for s in v.split(",") if s.strip()]
            return []

        cm.likes = as_list("likes", "suka")
        cm.dislikes = as_list("dislikes", "tidak_suka")
        cm.hobbies = as_list("hobbies", "hobi")
        cm.personality = as_list("personality", "personality_traits", "kepribadian")
        cm.fears = as_list("fears", "ketakutan")
        cm.backstory = pick("backstory", "bio", "latar_belakang", "description")

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


# ─── AI-based auto-generate (kirim persona ke local model) ─────────────────
#
# Bukan seed statis dari field yang harus ditulis manual — ini mengirim
# deskripsi kepribadian karakter (blok prompts.soul_system di character.json)
# ke local model kamu (lihat config.py: ENDPOINTS + CALL_ROUTING), lalu model
# itu sendiri yang "mengarang" data identitas (tanggal lahir, keinginan, dll)
# yang KONSISTEN dengan kepribadiannya. Hasilnya baru disimpan ke bin.
#
# WAJIB: tambahkan satu entry baru di config.py punyamu, di CALL_ROUTING:
#   CALL_ROUTING = {
#       ...
#       "profile_extract": "local_lm_studio",   # <- tambahkan baris ini
#   }
# (boleh pakai endpoint lain kalau mau, tinggal ganti value-nya)

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
    "dengan kepribadiannya (tanggal lahir, zodiak, suka/tidak suka, hobi, keinginan, "
    "sifat, ketakutan, latar belakang singkat). Data ini akan jadi memory permanen "
    "karakter, jadi buat masuk akal, spesifik, dan JANGAN generic/template. "
    "Kalau di 'Data yang sudah diketahui' sudah ada isinya, JANGAN diubah, cukup "
    "lengkapi field yang masih kosong.\n"
    f"Output HANYA JSON valid, struktur PERSIS seperti ini, tanpa teks/markdown lain:\n{_PROFILE_SCHEMA_HINT}"
)


def _extract_persona_text(char_json: Dict) -> str:
    """Ambil teks deskripsi kepribadian karakter dari character.json."""
    name = char_json.get("name", "")
    prompts = char_json.get("prompts", {})
    prompts = prompts if isinstance(prompts, dict) else {}
    persona = prompts.get("soul_system") or prompts.get("soul_final_system") or ""

    known = char_json.get("profile", {})
    known = known if isinstance(known, dict) else {}

    parts = [f"Nama karakter: {name}"]
    if persona:
        parts.append(f"Deskripsi kepribadian:\n{persona[:2500]}")
    if known:
        parts.append(f"Data yang sudah diketahui (jangan diubah):\n{json.dumps(known, ensure_ascii=False)}")
    return "\n\n".join(parts)


def _default_llm_call(system: str, user: str, pass_name: str = "profile_extract") -> str:
    """
    Default caller ke local model, pakai config.py (get_client/get_model/get_extra_body)
    milikmu. Kalau config.py belum punya routing 'profile_extract', atau model lokal
    belum jalan, ini akan raise — dan generate_via_ai() akan fallback dengan aman.
    """
    import config  # lazy import — modul ini tetap bisa dipakai berdiri sendiri tanpa config.py
    client = config.get_client(pass_name)
    model = config.get_model(pass_name)
    extra_body = config.get_extra_body(pass_name)
    resp = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        max_tokens=700,
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


def generate_via_ai(character_id: str, char_json: Dict, llm_call=None) -> "CharacterMemory":
    """
    Auto-generate CharacterMemory dengan mengirim persona karakter ke local model,
    minta model MELENGKAPI data identitasnya sendiri (bukan ditulis manual).

    llm_call: callable(system, user) -> str raw text. Default pakai config.py.
    Kalau gagal (model belum jalan / routing belum ada / output bukan JSON valid),
    otomatis fallback ke seed_from_character_json() biasa (tidak pernah crash).
    """
    caller = llm_call or _default_llm_call
    user_prompt = _extract_persona_text(char_json)
    try:
        raw = caller(_PROFILE_SYSTEM_PROMPT, user_prompt)
        data = _parse_json_loose(raw)
        if not data:
            raise ValueError(f"output bukan JSON valid: {raw[:200]!r}")
    except Exception as e:
        print(f"[CHAR MEMORY] AI-generate gagal untuk '{character_id}' ({e}) — fallback ke character.json biasa.")
        return CharacterMemory.seed_from_character_json(character_id, char_json)

    now = int(time.time())
    cm = CharacterMemory(character_id=character_id, created_ts=now, updated_ts=now)
    cm.full_name = data.get("full_name") or char_json.get("name", character_id)
    cm.birthday = data.get("birthday", "") or ""
    cm.zodiac = data.get("zodiac", "") or ""
    cm.likes = data.get("likes") if isinstance(data.get("likes"), list) else []
    cm.dislikes = data.get("dislikes") if isinstance(data.get("dislikes"), list) else []
    cm.hobbies = data.get("hobbies") if isinstance(data.get("hobbies"), list) else []
    cm.personality = data.get("personality") if isinstance(data.get("personality"), list) else []
    cm.fears = data.get("fears") if isinstance(data.get("fears"), list) else []
    cm.backstory = data.get("backstory") if isinstance(data.get("backstory"), str) else ""
    wants_raw = data.get("wants", [])
    if isinstance(wants_raw, list):
        for i, w in enumerate(wants_raw):
            cm.add_want(str(w), priority=1.0 - i * 0.1)
    return cm


# ─── Public API (mengikuti pola load()/save() seperti long_memory.py) ───────

def load(
    character_id: str,
    char_json: Optional[Dict] = None,
    use_ai: bool = True,
    llm_call=None,
) -> CharacterMemory:
    """
    Ambil CharacterMemory untuk karakter tsb. Kalau belum pernah ada,
    otomatis dibuat (auto-generate):
      - use_ai=True (default)  -> kirim persona ke local model, model yang
        mengisi datanya sendiri (lihat generate_via_ai()).
      - use_ai=False            -> hanya seed dari field character.json
        (butuh blok "profile" ditulis manual, lihat seed_from_character_json()).
    """
    raw = _read_all().get(character_id)
    if raw is not None:
        try:
            return CharacterMemory.from_dict(raw)
        except Exception:
            pass

    if not char_json:
        cm = CharacterMemory(character_id=character_id, created_ts=int(time.time()))
    elif use_ai:
        cm = generate_via_ai(character_id, char_json, llm_call)
    else:
        cm = CharacterMemory.seed_from_character_json(character_id, char_json)

    save(cm)
    return cm


def save(cm: CharacterMemory):
    cm.updated_ts = int(time.time())
    data = _read_all()
    data[cm.character_id] = cm.to_dict()
    _write_all(data)


def exists(character_id: str) -> bool:
    return character_id in _read_all()


def clear(character_id: str):
    data = _read_all()
    if character_id in data:
        del data[character_id]
        _write_all(data)


def list_characters() -> List[str]:
    return sorted(_read_all().keys())


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


def ensure_all(char_manager, force: bool = False, use_ai: bool = True, llm_call=None) -> List[str]:
    """
    Dipanggil sekali saat app start (auto-generate). Untuk setiap karakter
    yang terdeteksi oleh CharacterManager:
      - kalau bin belum ada -> generate baru
          - use_ai=True (default): kirim persona ke local model, model isi
            datanya sendiri (generate_via_ai). Kalau model belum jalan,
            otomatis fallback ke seed dari character.json (tidak crash).
          - use_ai=False: hanya seed dari field character.json (butuh blok
            "profile" ditulis manual).
      - kalau bin sudah ada dan force=False -> dibiarkan (tidak ditimpa)
      - kalau force=True -> selalu di-regenerate ulang
    Return: list nama karakter yang di-generate/regenerate.
    """
    generated = []
    for name in char_manager.list_characters():
        if exists(name) and not force:
            continue
        char_json = _read_character_json(char_manager, name)
        cm = (
            generate_via_ai(name, char_json, llm_call)
            if use_ai else
            CharacterMemory.seed_from_character_json(name, char_json)
        )
        save(cm)
        generated.append(name)
    return generated


def regenerate(char_manager, character_id: str, use_ai: bool = True, llm_call=None) -> CharacterMemory:
    """
    Regenerate ulang SATU karakter secara paksa, menimpa data lama.
    Dipakai buat command manual: python character_memory.py generate <nama> --force
    """
    char_json = _read_character_json(char_manager, character_id)
    cm = (
        generate_via_ai(character_id, char_json, llm_call)
        if use_ai else
        CharacterMemory.seed_from_character_json(character_id, char_json)
    )
    save(cm)
    return cm


# ─── Utility: export/import ke JSON supaya gampang diedit manual ────────────

def export_json(character_id: str, out_path: Optional[str] = None) -> str:
    """Ekspor isi bin ke file .json yang bisa diedit tangan, lalu di-import lagi."""
    cm = load(character_id)
    out_path = out_path or os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "state", f"{character_id}_memory_export.json"
    )
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(cm.to_dict(), f, indent=2, ensure_ascii=False)
    return out_path


def import_json(character_id: str, json_path: str):
    """Baca file json hasil edit manual, lalu tulis ulang ke bin."""
    with open(json_path, "r", encoding="utf-8") as f:
        d = json.load(f)
    d["character_id"] = character_id
    cm = CharacterMemory.from_dict(d)
    save(cm)


# ─── CLI kecil buat testing & regenerate manual ─────────────────────────────
#
# Contoh pemakaian:
#   python character_memory.py list
#   python character_memory.py show liana
#   python character_memory.py generate liana --force            # AI-generate (default)
#   python character_memory.py generate liana --force --no-ai    # tanpa AI, cuma dari blok "profile" manual
#   python character_memory.py generate-all --force
#   python character_memory.py export liana
#   python character_memory.py clear liana

def _get_char_manager():
    """Import CharacterManager secara lazy (hanya dibutuhkan untuk CLI)."""
    from character_manager import CharacterManager
    return CharacterManager()


if __name__ == "__main__":
    import sys

    args = sys.argv[1:]
    force = "--force" in args
    use_ai = "--no-ai" not in args
    args = [a for a in args if a not in ("--force", "--no-ai")]

    if not args:
        print(
            "Usage:\n"
            "  python character_memory.py list\n"
            "  python character_memory.py show <character_id>\n"
            "  python character_memory.py generate <character_id> [--force] [--no-ai]\n"
            "  python character_memory.py generate-all [--force] [--no-ai]\n"
            "  python character_memory.py export <character_id>\n"
            "  python character_memory.py import <character_id> <json_path>\n"
            "  python character_memory.py clear <character_id>\n"
            "\n"
            "Default: generate/generate-all pakai AI (kirim persona ke local model\n"
            "dari config.py). Tambah --no-ai untuk cuma pakai blok 'profile' manual\n"
            "di character.json tanpa manggil model."
        )
        sys.exit(0)

    cmd = args[0]

    if cmd == "list":
        print(list_characters())

    elif cmd == "show" and len(args) > 1:
        print(load(args[1]).summary_for_context())

    elif cmd == "generate" and len(args) > 1:
        name = args[1]
        mgr = _get_char_manager()
        if name not in mgr.list_characters():
            print(f"[!] Character '{name}' tidak ditemukan di folder {mgr.dir}")
            sys.exit(1)
        if exists(name) and not force:
            print(f"[i] Memory '{name}' sudah ada. Pakai --force untuk menimpa ulang.")
        else:
            mode = "AI (local model)" if use_ai else "character.json manual"
            print(f"[i] Generating '{name}' via {mode}...")
            regenerate(mgr, name, use_ai=use_ai)
            print(f"[OK] Memory '{name}' berhasil di-generate.")

    elif cmd == "generate-all":
        mgr = _get_char_manager()
        mode = "AI (local model)" if use_ai else "character.json manual"
        print(f"[i] Generating semua karakter via {mode}...")
        generated = ensure_all(mgr, force=force, use_ai=use_ai)
        if generated:
            print(f"[OK] Generated/regenerated untuk: {generated}")
        else:
            print("[i] Tidak ada yang di-generate (semua sudah ada). Pakai --force untuk paksa regenerate semua.")

    elif cmd == "export" and len(args) > 1:
        print("Exported to:", export_json(args[1]))

    elif cmd == "import" and len(args) > 2:
        import_json(args[1], args[2])
        print(f"[OK] Memory '{args[1]}' berhasil di-import dari {args[2]}")

    elif cmd == "clear" and len(args) > 1:
        clear(args[1])
        print(f"[OK] Cleared memory for '{args[1]}'")

    else:
        print("Command tidak dikenali atau argumen kurang. Jalankan tanpa argumen untuk lihat usage.")
