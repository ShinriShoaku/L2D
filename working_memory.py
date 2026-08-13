"""
working_memory.py — Working Memory (RAM AI).

Hidup selama sesi percakapan, bisa persist opsional.
Menyimpan:
  - current_goal      : tujuan user saat ini ("buat folder", "rencanakan trip")
  - pending_tasks     : list task yang belum selesai
  - last_action       : aksi terakhir yang dilakukan
  - current_file      : file/objek yang sedang dikerjakan (untuk konteks)
  - awaiting_reply    : menunggu jawaban user untuk apa
  - short_context     : key-value bebas, hidup dalam sesi ini saja
  - confirmed_facts   : fakta yang baru diucapkan user sesi ini (belum masuk long memory)

Format persist: state/working_memory.bin — per user_id.
Di-clear otomatis setelah sesi selesai (opsional, bisa juga dibiarkan persist).
"""
from __future__ import annotations
import os, re, struct, time
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

_FILE   = "working_memory.bin"
_MAGIC  = b"WKM\x01"
_H_FMT  = ">4sI";  _H_SZ  = struct.calcsize(_H_FMT)
_IX_FMT = ">Q";    _IX_SZ = struct.calcsize(_IX_FMT)
_L_FMT  = ">I";    _L_SZ  = struct.calcsize(_L_FMT)

def _path(char_id: str) -> str:
    """
    PATCH: folder TERPISAH per karakter — state/{char_id}/working_memory.bin
    — bukan lagi 1 file flat state/working_memory.bin dibagi semua karakter.

    Root cause bug yang difix: sebelumnya load(user_id) TIDAK tahu karakter
    mana yang aktif, jadi kalau user_id yang sama ngobrol sama karakter A
    lalu pindah ke karakter B, working memory karakter A (goal, last_action,
    short_context, dst) ikut kebawa/ke-load lagi di sesi karakter B —
    ke-lihat jelas di log: ctx masih nyebut karakter lama padahal sudah
    switch karakter.
    """
    d = os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "state", _safe_char_id(char_id),
    )
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, _FILE)

def _safe_char_id(char_id: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_\-]", "_", str(char_id or "_default"))[:64]

def _read_all(char_id: str) -> Dict[str, Dict]:
    p = _path(char_id)
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

def _write_all(char_id: str, data: Dict[str, Dict]):
    p = _path(char_id)
    items = list(data.values())
    payloads = []
    for rec in items:
        try: payloads.append(_dumps(rec))
        except Exception: payloads.append(None)
    valid = [(items[i], payloads[i]) for i in range(len(items)) if payloads[i]]
    count = len(valid)
    ds = _H_SZ + _IX_SZ * count
    offs, cur = [], ds
    for _, p2 in valid:
        offs.append(cur); cur += _L_SZ + len(p2)
    try:
        with open(p, "wb") as f:
            f.write(struct.pack(_H_FMT, _MAGIC, count))
            for o in offs: f.write(struct.pack(_IX_FMT, o))
            for _, p2 in valid:
                f.write(struct.pack(_L_FMT, len(p2))); f.write(p2)
    except Exception: pass


@dataclass
class WorkingMemory:
    user_id:         str  = ""
    char_id:         str  = ""          # PATCH: karakter aktif pemilik memory ini
    current_goal:    str  = ""          # "buat project folder", "rencanakan liburan"
    last_action:     str  = ""          # aksi terakhir ("cloud_save", "weather_check")
    last_tool:       str  = ""
    current_file:    str  = ""          # file/objek konteks ("config.py", "folder X")
    awaiting_reply:  str  = ""          # menunggu jawaban untuk apa ("konfirmasi nama?")
    pending_tasks:   List[str]  = field(default_factory=list)
    short_context:   Dict[str, Any] = field(default_factory=dict)  # key-val bebas sesi ini
    confirmed_facts: List[str]  = field(default_factory=list)      # fakta baru sesi ini
    updated_ts:      int  = 0

    def set(self, key: str, value: Any):
        """Set nilai di short_context."""
        self.short_context[key] = value

    def get(self, key: str, default: Any = None) -> Any:
        return self.short_context.get(key, default)

    def add_task(self, task: str):
        if task and task not in self.pending_tasks:
            self.pending_tasks.append(task)

    def complete_task(self, task: str):
        self.pending_tasks = [t for t in self.pending_tasks if t != task]

    def add_fact(self, fact: str):
        if fact and fact not in self.confirmed_facts:
            self.confirmed_facts.append(fact)

    def clear_session(self):
        """Reset state sementara (keep user_id)."""
        self.current_goal   = ""
        self.last_action    = ""
        self.last_tool      = ""
        self.current_file   = ""
        self.awaiting_reply = ""
        self.pending_tasks  = []
        self.short_context  = {}
        self.confirmed_facts = []

    def summary(self) -> str:
        parts = []
        if self.current_goal:   parts.append(f"goal: {self.current_goal}")
        if self.pending_tasks:  parts.append(f"tasks: {self.pending_tasks}")
        if self.last_action:    parts.append(f"last_action: {self.last_action}")
        if self.awaiting_reply: parts.append(f"awaiting: {self.awaiting_reply}")
        if self.short_context:  parts.append(f"ctx: {dict(list(self.short_context.items())[:3])}")
        return " | ".join(parts) if parts else "(empty)"

    def to_dict(self) -> Dict: return asdict(self)

    @staticmethod
    def from_dict(d: Dict) -> "WorkingMemory":
        return WorkingMemory(**{k: v for k, v in d.items() if k in WorkingMemory.__dataclass_fields__})


def load(user_id: str, char_id: str) -> WorkingMemory:
    """
    PATCH: char_id WAJIB — working memory sekarang di-load dari folder
    milik karakter itu sendiri (state/{char_id}/working_memory.bin), jadi
    switch karakter untuk user_id yang sama TIDAK bakal ke-bawa data
    karakter lain lagi.
    """
    raw = _read_all(char_id).get(user_id)
    if raw is None:
        return WorkingMemory(user_id=user_id, char_id=char_id, updated_ts=int(time.time()))
    try:
        wm = WorkingMemory.from_dict(raw)
        wm.char_id = char_id  # jaga-jaga record lama (pra-patch) belum punya char_id
        return wm
    except Exception:
        return WorkingMemory(user_id=user_id, char_id=char_id, updated_ts=int(time.time()))

def save(wm: WorkingMemory):
    wm.updated_ts = int(time.time())
    data = _read_all(wm.char_id)
    data[wm.user_id] = wm.to_dict()
    _write_all(wm.char_id, data)

def clear(user_id: str, char_id: str):
    data = _read_all(char_id)
    if user_id in data:
        del data[user_id]
        _write_all(char_id, data)

def update(user_id: str, char_id: str, **kwargs) -> WorkingMemory:
    """Convenience: load → update fields → save → return."""
    wm = load(user_id, char_id)
    for k, v in kwargs.items():
        if hasattr(wm, k): setattr(wm, k, v)
    save(wm)
    return wm
