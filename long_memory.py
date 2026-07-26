"""
long_memory.py — Long Memory (Pengalaman Bersama + Conversation Summaries).

Menyimpan:
  - experiences : pengalaman signifikan (event penting, momen bersama)
  - summaries   : ringkasan sesi percakapan panjang (bukan full history)
  - milestones  : pencapaian relasi (first deep talk, first gift, dst)

Persist: state/long_memory.bin — per user_id.
Semua entry punya timestamp sehingga bisa di-sort kronologis.
"""
from __future__ import annotations
import hashlib, os, struct, time
from dataclasses import dataclass, field, asdict
from typing import Dict, List, Optional

try:
    import msgpack as _p
    def _dumps(o) -> bytes: return _p.packb(o, use_bin_type=True)
    def _loads(b: bytes):   return _p.unpackb(b, raw=False)
except ImportError:
    import pickle as _pk
    def _dumps(o) -> bytes: return _pk.dumps(o, protocol=4)
    def _loads(b: bytes):   return _pk.loads(b)

_FILE   = "long_memory.bin"
_MAGIC  = b"LNM\x01"
_H_FMT  = ">4sI";  _H_SZ  = struct.calcsize(_H_FMT)
_IX_FMT = ">Q";    _IX_SZ = struct.calcsize(_IX_FMT)
_L_FMT  = ">I";    _L_SZ  = struct.calcsize(_L_FMT)
_MAX_EXP  = 200   # max experience entries per user
_MAX_SUM  = 50    # max summary entries per user

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
                f.seek(off); lr = f.read(_L_SZ)
                if len(lr) < _L_SZ: continue
                plen = struct.unpack(_L_FMT, lr)[0]; pr = f.read(plen)
                try: rec = _loads(pr); out[rec["user_id"]] = rec
                except Exception: pass
        return out
    except Exception: return {}

def _write_all(data: Dict[str, Dict]):
    p = _path(); items = list(data.values())
    payloads = [None]*len(items)
    for i, r in enumerate(items):
        try: payloads[i] = _dumps(r)
        except Exception: pass
    valid = [(items[i], payloads[i]) for i in range(len(items)) if payloads[i]]
    count = len(valid); ds = _H_SZ + _IX_SZ*count
    offs, cur = [], ds
    for _, p2 in valid: offs.append(cur); cur += _L_SZ+len(p2)
    try:
        with open(p, "wb") as f:
            f.write(struct.pack(_H_FMT, _MAGIC, count))
            for o in offs: f.write(struct.pack(_IX_FMT, o))
            for _, p2 in valid: f.write(struct.pack(_L_FMT, len(p2))); f.write(p2)
    except Exception: pass


@dataclass
class Experience:
    exp_id:      str  = ""
    title:       str  = ""     # ringkasan singkat, mis. "membangun AI character Ayumi"
    description: str  = ""     # detail lebih panjang
    topics:      List[str] = field(default_factory=list)  # tag topik
    importance:  float = 0.5
    ts:          int  = 0

@dataclass
class ConversationSummary:
    sum_id:  str  = ""
    summary: str  = ""
    topics:  List[str] = field(default_factory=list)
    mood:    str  = "neutral"
    ts:      int  = 0
    n_messages: int = 0


@dataclass
class LongMemory:
    user_id:      str  = ""
    experiences:  List[Dict] = field(default_factory=list)
    summaries:    List[Dict] = field(default_factory=list)
    milestones:   List[str]  = field(default_factory=list)
    updated_ts:   int = 0

    def add_experience(self, title: str, description: str = "", topics: List[str] = None, importance: float = 0.5):
        eid = hashlib.md5(f"{self.user_id}{title}{int(time.time())}".encode()).hexdigest()[:8]
        exp = Experience(exp_id=eid, title=title, description=description,
                         topics=topics or [], importance=importance, ts=int(time.time()))
        self.experiences.append(asdict(exp))
        # Trim: pertahankan yang paling penting
        if len(self.experiences) > _MAX_EXP:
            self.experiences.sort(key=lambda x: (x.get("importance", 0), x.get("ts", 0)), reverse=True)
            self.experiences = self.experiences[:_MAX_EXP]

    def add_summary(self, summary: str, topics: List[str] = None, mood: str = "neutral", n_messages: int = 0):
        sid = hashlib.md5(f"{self.user_id}{summary[:30]}{int(time.time())}".encode()).hexdigest()[:8]
        s = ConversationSummary(sum_id=sid, summary=summary, topics=topics or [],
                                mood=mood, ts=int(time.time()), n_messages=n_messages)
        self.summaries.append(asdict(s))
        if len(self.summaries) > _MAX_SUM:
            self.summaries = self.summaries[-_MAX_SUM:]

    def add_milestone(self, milestone: str):
        if milestone and milestone not in self.milestones:
            self.milestones.append(milestone)

    def search_experiences(self, query: str, max_results: int = 5) -> List[Experience]:
        q = query.lower()
        results = []
        for e in self.experiences:
            text = f"{e.get('title','')} {e.get('description','')} {' '.join(e.get('topics',[]))}"
            if q in text.lower():
                results.append(Experience(**e))
        results.sort(key=lambda x: (x.importance, x.ts), reverse=True)
        return results[:max_results]

    def recent_summaries(self, n: int = 3) -> List[ConversationSummary]:
        sorted_s = sorted(self.summaries, key=lambda x: x.get("ts", 0), reverse=True)
        return [ConversationSummary(**s) for s in sorted_s[:n]]

    def summary_for_context(self, topic_hint: str = "") -> str:
        """Pilih konten relevan untuk dimasukkan ke Soul context."""
        lines = []
        # Recent summaries
        recent = self.recent_summaries(2)
        if recent:
            lines.append("[riwayat sesi terakhir]")
            for s in recent:
                lines.append(f"  - {s.summary[:100]}")
        # Relevant experiences
        exps = self.search_experiences(topic_hint) if topic_hint else []
        if not exps:
            exps_sorted = sorted(self.experiences, key=lambda x: (x.get("importance",0), x.get("ts",0)), reverse=True)
            exps = [Experience(**e) for e in exps_sorted[:3]]
        if exps:
            lines.append("[pengalaman bersama]")
            for e in exps:
                lines.append(f"  - {e.title}: {e.description[:80]}")
        if self.milestones:
            lines.append(f"[milestones] {self.milestones[-3:]}")
        return "\n".join(lines) if lines else "(belum ada long memory)"

    def to_dict(self) -> Dict: return asdict(self)

    @staticmethod
    def from_dict(d: Dict) -> "LongMemory":
        lm = LongMemory(user_id=d.get("user_id",""), updated_ts=d.get("updated_ts",0))
        lm.experiences = d.get("experiences", [])
        lm.summaries   = d.get("summaries", [])
        lm.milestones  = d.get("milestones", [])
        return lm


def load(user_id: str) -> LongMemory:
    raw = _read_all().get(user_id)
    if raw is None: return LongMemory(user_id=user_id, updated_ts=int(time.time()))
    try:    return LongMemory.from_dict(raw)
    except: return LongMemory(user_id=user_id)

def save(lm: LongMemory):
    lm.updated_ts = int(time.time())
    data = _read_all(); data[lm.user_id] = lm.to_dict(); _write_all(data)

def clear(user_id: str):
    data = _read_all()
    if user_id in data: del data[user_id]; _write_all(data)
