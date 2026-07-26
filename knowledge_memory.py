"""
knowledge_memory.py — Knowledge Memory (Fakta + Confidence Scoring).

Menyimpan fakta yang diucapkan user: hardware, software, project, dst.
Setiap fakta punya confidence score yang naik/turun berdasarkan repetisi
atau kontradiksi. Confidence rendah = belum yakin. Confidence tinggi = established fact.

Persist: state/knowledge_memory.bin — per user_id.
Search: substring / keyword (tidak butuh embedding untuk model kecil).
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

_FILE   = "knowledge_memory.bin"
_MAGIC  = b"KNM\x01"
_H_FMT  = ">4sI";  _H_SZ  = struct.calcsize(_H_FMT)
_IX_FMT = ">Q";    _IX_SZ = struct.calcsize(_IX_FMT)
_L_FMT  = ">I";    _L_SZ  = struct.calcsize(_L_FMT)
_MAX_FACTS = 500    # per user

def _path() -> str:
    d = os.path.join(os.path.dirname(os.path.abspath(__file__)), "state")
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, _FILE)

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
                    rec = _loads(pr); out[rec["user_id"]] = rec
                except Exception: pass
        return out
    except Exception: return {}

def _write_all(data: Dict[str, Dict]):
    p = _path()
    items = list(data.values())
    payloads = [None]*len(items)
    for i, r in enumerate(items):
        try: payloads[i] = _dumps(r)
        except Exception: pass
    valid = [(items[i], payloads[i]) for i in range(len(items)) if payloads[i]]
    count = len(valid)
    ds = _H_SZ + _IX_SZ*count
    offs, cur = [], ds
    for _, p2 in valid: offs.append(cur); cur += _L_SZ+len(p2)
    try:
        with open(p, "wb") as f:
            f.write(struct.pack(_H_FMT, _MAGIC, count))
            for o in offs: f.write(struct.pack(_IX_FMT, o))
            for _, p2 in valid: f.write(struct.pack(_L_FMT, len(p2))); f.write(p2)
    except Exception: pass


@dataclass
class Fact:
    fact_id:    str   = ""
    category:   str   = ""     # hardware/software/project/preference/other
    key:        str   = ""     # mis. "os", "gpu", "framework"
    value:      str   = ""     # mis. "Arch Linux", "RTX 4070", "FastAPI"
    confidence: float = 0.5    # 0.0-1.0
    mentions:   int   = 1      # berapa kali diucapkan
    contradicted: bool = False  # pernah dikontradiksi?
    ts_created: int   = 0
    ts_updated: int   = 0

    def reinforce(self, delta: float = 0.15):
        """Confidence naik saat fact diulang."""
        self.confidence = min(1.0, self.confidence + delta)
        self.mentions  += 1
        self.ts_updated = int(time.time())

    def contradict(self, new_value: str) -> "Fact":
        """Return fact lama dengan confidence turun, plus hint untuk fact baru."""
        self.confidence   = max(0.0, self.confidence - 0.4)
        self.contradicted = True
        self.ts_updated   = int(time.time())
        return self


@dataclass
class KnowledgeStore:
    user_id: str  = ""
    facts:   List[Dict] = field(default_factory=list)
    updated_ts: int = 0

    def _find(self, category: str, key: str) -> Optional[int]:
        """Return index of matching fact or None."""
        for i, f in enumerate(self.facts):
            if f.get("category") == category and f.get("key") == key:
                return i
        return None

    def add_or_update(self, category: str, key: str, value: str, confidence: float = 0.6) -> Fact:
        idx = self._find(category, key)
        if idx is not None:
            existing = Fact(**self.facts[idx])
            if existing.value.lower() == value.lower():
                existing.reinforce()
                self.facts[idx] = asdict(existing)
                return existing
            else:
                # Kontradiksi: nilai berbeda
                existing.contradict(value)
                self.facts[idx] = asdict(existing)
                # Tambah fact baru dengan confidence lebih rendah (belum confirm)
                return self.add_new(category, key, value, confidence=0.45)
        return self.add_new(category, key, value, confidence)

    def add_new(self, category: str, key: str, value: str, confidence: float = 0.6) -> Fact:
        import hashlib
        fid = hashlib.md5(f"{self.user_id}{category}{key}{value}".encode()).hexdigest()[:8]
        now = int(time.time())
        f = Fact(fact_id=fid, category=category, key=key, value=value,
                 confidence=confidence, ts_created=now, ts_updated=now)
        self.facts.append(asdict(f))
        # Trim jika terlalu banyak (hapus yang confidence rendah)
        if len(self.facts) > _MAX_FACTS:
            self.facts.sort(key=lambda x: x.get("confidence", 0), reverse=True)
            self.facts = self.facts[:_MAX_FACTS]
        return f

    def get(self, category: str = "", key: str = "", min_confidence: float = 0.3) -> List[Fact]:
        result = []
        for f in self.facts:
            if f.get("confidence", 0) < min_confidence: continue
            if category and f.get("category") != category: continue
            if key and f.get("key") != key: continue
            result.append(Fact(**f))
        result.sort(key=lambda x: x.confidence, reverse=True)
        return result

    def search(self, query: str, min_confidence: float = 0.3) -> List[Fact]:
        q = query.lower()
        return [Fact(**f) for f in self.facts
                if f.get("confidence", 0) >= min_confidence and
                (q in f.get("key","").lower() or q in f.get("value","").lower() or q in f.get("category","").lower())]

    def summary_for_context(self, topic_hint: str = "", max_items: int = 8) -> str:
        """Pilih fakta relevan untuk dimasukkan ke Soul context."""
        candidates = self.search(topic_hint) if topic_hint else self.get()
        if not candidates: candidates = self.get()
        top = sorted(candidates, key=lambda x: x.confidence, reverse=True)[:max_items]
        if not top: return "(belum ada knowledge)"
        lines = [f"[knowledge/{self.user_id}]"]
        for f in top:
            lines.append(f"  {f.category}.{f.key}={f.value!r} (conf={f.confidence:.2f})")
        return "\n".join(lines)

    def to_dict(self) -> Dict: return asdict(self)

    @staticmethod
    def from_dict(d: Dict) -> "KnowledgeStore":
        ks = KnowledgeStore(user_id=d.get("user_id",""), updated_ts=d.get("updated_ts",0))
        ks.facts = d.get("facts", [])
        return ks


def load(user_id: str) -> KnowledgeStore:
    raw = _read_all().get(user_id)
    if raw is None: return KnowledgeStore(user_id=user_id, updated_ts=int(time.time()))
    try:    return KnowledgeStore.from_dict(raw)
    except: return KnowledgeStore(user_id=user_id)

def save(ks: KnowledgeStore):
    ks.updated_ts = int(time.time())
    data = _read_all(); data[ks.user_id] = ks.to_dict(); _write_all(data)

def clear(user_id: str):
    data = _read_all()
    if user_id in data: del data[user_id]; _write_all(data)

# Convenience
def add_fact(user_id: str, category: str, key: str, value: str, confidence: float = 0.6) -> Fact:
    ks = load(user_id); f = ks.add_or_update(category, key, value, confidence); save(ks); return f

def search_facts(user_id: str, query: str) -> List[Fact]:
    return load(user_id).search(query)
