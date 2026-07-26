"""
relationship_memory.py — Relationship Memory (Profil User).

Menyimpan preferensi, perilaku, dan relasi user dengan karakter AI.
Bukan history mentah — ini "apa yang AI ketahui tentang user".

Data disimpan per (user_id, char_id) agar tiap karakter punya relasi sendiri.
Persist ke state/relationship_memory.bin.
"""
from __future__ import annotations
import os, struct, time
from dataclasses import dataclass, field, asdict
from typing import Any, Dict, List, Optional

try:
    import msgpack as _p
    def _dumps(o) -> bytes: return _p.packb(o, use_bin_type=True)
    def _loads(b: bytes):   return _p.unpackb(b, raw=False)
except ImportError:
    import pickle as _pk
    def _dumps(o) -> bytes: return _pk.dumps(o, protocol=4)
    def _loads(b: bytes):   return _pk.loads(b)

_FILE   = "relationship_memory.bin"
_MAGIC  = b"RLM\x01"
_H_FMT  = ">4sI";  _H_SZ  = struct.calcsize(_H_FMT)
_IX_FMT = ">Q";    _IX_SZ = struct.calcsize(_IX_FMT)
_L_FMT  = ">I";    _L_SZ  = struct.calcsize(_L_FMT)

def _path() -> str:
    d = os.path.join(os.path.dirname(os.path.abspath(__file__)), "state")
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, _FILE)

def _key(user_id: str, char_id: str) -> str:
    return f"{user_id}::{char_id}"

def _read_all() -> Dict[str, Dict]:
    p = _path()
    if not os.path.exists(p): return {}
    try:
        with open(p, "rb") as f:
            hdr = f.read(_H_SZ)
            if len(hdr) < _H_SZ: return {}
            magic, count = struct.unpack(_H_FMT, hdr)
            if magic != _MAGIC or count == 0: return {}
            idx = [struct.unpack(_IX_FMT, f.read(_IX_SZ))[0] for _ in range(count)]
            out = {}
            for off in idx:
                f.seek(off)
                lr = f.read(_L_SZ)
                if len(lr) < _L_SZ: continue
                plen = struct.unpack(_L_FMT, lr)[0]
                pr = f.read(plen)
                try:
                    rec = _loads(pr); out[rec["_key"]] = rec
                except Exception: pass
        return out
    except Exception: return {}

def _write_all(data: Dict[str, Dict]):
    p = _path()
    items = list(data.values())
    payloads = [None] * len(items)
    for i, rec in enumerate(items):
        try: payloads[i] = _dumps(rec)
        except Exception: pass
    valid = [(items[i], payloads[i]) for i in range(len(items)) if payloads[i]]
    count = len(valid)
    ds = _H_SZ + _IX_SZ * count
    offs, cur = [], ds
    for _, p2 in valid: offs.append(cur); cur += _L_SZ + len(p2)
    try:
        with open(p, "wb") as f:
            f.write(struct.pack(_H_FMT, _MAGIC, count))
            for o in offs: f.write(struct.pack(_IX_FMT, o))
            for _, p2 in valid: f.write(struct.pack(_L_FMT, len(p2))); f.write(p2)
    except Exception: pass


@dataclass
class Preference:
    key:   str = ""     # mis. "answer_style", "nickname", "language"
    value: str = ""     # mis. "technical", "Shinri", "Indonesia"
    confidence: float = 0.7
    source: str = ""    # dari mana: "user_stated", "inferred", "corrected"
    ts:    int  = 0

@dataclass
class Behavior:
    trigger:  str = ""  # mis. "dame", "hm", "skip"
    meaning:  str = ""  # mis. "cancel", "thinking", "move on"
    confidence: float = 0.8
    ts: int = 0

@dataclass
class RelationshipMemory:
    user_id:     str  = ""
    char_id:     str  = ""
    _key:        str  = ""       # user_id::char_id

    # Profil
    preferred_name:   str  = ""
    romance_level:    int  = 0    # 0-100
    trust_level:      int  = 50   # 0-100
    relation_status:  str  = "stranger"  # stranger/friend/close/romantic

    # Preferensi terstruktur
    preferences:  List[Dict] = field(default_factory=list)  # list of Preference.asdict()
    behaviors:    List[Dict] = field(default_factory=list)   # list of Behavior.asdict()
    lessons:      List[str]  = field(default_factory=list)   # "never call user bos"

    # Stats
    total_sessions:   int = 0
    total_messages:   int = 0
    last_seen_ts:     int = 0
    first_seen_ts:    int = 0
    updated_ts:       int = 0

    def __post_init__(self):
        if not self._key:
            self._key = _key(self.user_id, self.char_id)

    def get_preference(self, key: str) -> Optional[str]:
        for p in self.preferences:
            if p.get("key") == key:
                return p.get("value")
        return None

    def set_preference(self, key: str, value: str, confidence: float = 0.8, source: str = "user_stated"):
        for p in self.preferences:
            if p.get("key") == key:
                p["value"] = value; p["confidence"] = confidence
                p["source"] = source; p["ts"] = int(time.time())
                return
        self.preferences.append(asdict(Preference(key=key, value=value, confidence=confidence, source=source, ts=int(time.time()))))

    def add_behavior(self, trigger: str, meaning: str, confidence: float = 0.8):
        for b in self.behaviors:
            if b.get("trigger") == trigger:
                b["meaning"] = meaning; b["confidence"] = confidence
                b["ts"] = int(time.time()); return
        self.behaviors.append(asdict(Behavior(trigger=trigger, meaning=meaning, confidence=confidence, ts=int(time.time()))))

    def get_behavior(self, trigger: str) -> Optional[str]:
        for b in self.behaviors:
            if b.get("trigger") == trigger:
                return b.get("meaning")
        return None

    def add_lesson(self, lesson: str):
        if lesson and lesson not in self.lessons:
            self.lessons.append(lesson)

    def update_romance(self, delta: int):
        self.romance_level = max(0, min(100, self.romance_level + delta))

    def update_trust(self, delta: int):
        self.trust_level = max(0, min(100, self.trust_level + delta))

    def summary_for_context(self) -> str:
        """Ringkasan untuk dimasukkan ke Soul context."""
        lines = []
        if self.preferred_name: lines.append(f"dipanggil: {self.preferred_name}")
        if self.relation_status: lines.append(f"relasi: {self.relation_status} (trust={self.trust_level})")
        prefs = [(p["key"], p["value"]) for p in self.preferences[:5]]
        if prefs: lines.append(f"preferensi: {', '.join(f'{k}={v}' for k,v in prefs)}")
        if self.behaviors:
            beh_str = [b["trigger"] + "→" + b["meaning"] for b in self.behaviors[:3]]
            lines.append(f"behavior: {beh_str}")
        if self.lessons: lines.append(f"lessons: {self.lessons[:3]}")
        return "\n".join(lines) if lines else "(belum ada data relasi)"

    def to_dict(self) -> Dict:
        d = asdict(self)
        d["_key"] = self._key
        return d

    @staticmethod
    def from_dict(d: Dict) -> "RelationshipMemory":
        rm = RelationshipMemory()
        for k, v in d.items():
            if hasattr(rm, k): setattr(rm, k, v)
        return rm


def load(user_id: str, char_id: str) -> RelationshipMemory:
    k = _key(user_id, char_id)
    raw = _read_all().get(k)
    if raw is None:
        now = int(time.time())
        return RelationshipMemory(user_id=user_id, char_id=char_id, first_seen_ts=now, last_seen_ts=now, updated_ts=now)
    try:    return RelationshipMemory.from_dict(raw)
    except: return RelationshipMemory(user_id=user_id, char_id=char_id)

def save(rm: RelationshipMemory):
    rm.updated_ts  = int(time.time())
    rm.last_seen_ts = int(time.time())
    if not rm._key: rm._key = _key(rm.user_id, rm.char_id)
    data = _read_all()
    data[rm._key] = rm.to_dict()
    _write_all(data)

def clear(user_id: str, char_id: str):
    data = _read_all()
    k = _key(user_id, char_id)
    if k in data: del data[k]; _write_all(data)
