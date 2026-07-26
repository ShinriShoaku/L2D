"""
cloud_store.py — Binary key-value store untuk data user & global.

Format file: cloud_store.bin
┌──────────────────────────────────────────────────────────────┐
│ HEADER   4B magic "CLS\x02" + 4B record_count (uint32)      │
├──────────────────────────────────────────────────────────────┤
│ INDEX    record_count × 28B per slot:                        │
│   20B  key_hash  (sha1, fixed-size untuk binary search)      │
│    8B  offset    (uint64, posisi record di DATA section)      │
├──────────────────────────────────────────────────────────────┤
│ DATA     variable-length records:                            │
│   4B  payload_len (uint32)                                   │
│   NB  msgpack/pickle payload:                                │
│     ns        str  — namespace: "user:{id}" atau "global"    │
│     key       str  — key string                              │
│     value     any  — nilai (str/int/list/dict)               │
│     label     str  — label/deskripsi singkat (untuk lookup)  │
│     ts        int  — unix timestamp (created/updated)        │
│     updated   int  — unix timestamp update terakhir          │
│     ttl       int  — 0 = permanent, >0 = expiry unix ts      │
└──────────────────────────────────────────────────────────────┘

Namespace:
  "global"     → data global, semua user bisa baca, admin yang nulis
  "user:{id}"  → data private per user, hanya user itu yang bisa akses

Key format: "{ns}::{key}"  → di-hash SHA1 untuk index
"""

from __future__ import annotations
import hashlib, os, struct, time
from typing import Any, Dict, List, Optional, Tuple

try:
    import msgpack as _pack
    def _dumps(obj) -> bytes: return _pack.packb(obj, use_bin_type=True)
    def _loads(b: bytes):     return _pack.unpackb(b, raw=False)
    _PACK = "msgpack"
except ImportError:
    import pickle as _pkl
    def _dumps(obj) -> bytes: return _pkl.dumps(obj, protocol=4)
    def _loads(b: bytes):     return _pkl.loads(b)
    _PACK = "pickle"

# ─── Konstanta format ─────────────────────────────────────────────────────────
STORE_FILE = "cloud_store.bin"
_MAGIC     = b"CLS\x02"
_HDR_FMT   = ">4sI"          # magic(4s) + count(I)  = 8B
_HDR_SZ    = struct.calcsize(_HDR_FMT)
_SLOT_FMT  = ">20sQ"         # sha1(20s) + offset(Q) = 28B
_SLOT_SZ   = struct.calcsize(_SLOT_FMT)
_LEN_FMT   = ">I"            # payload_len            = 4B
_LEN_SZ    = struct.calcsize(_LEN_FMT)

def _store_path() -> str:
    base = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cloud")
    os.makedirs(base, exist_ok=True)
    return os.path.join(base, STORE_FILE)

def _make_key(ns: str, key: str) -> str:
    return f"{ns}::{key}"

def _hash_key(composite_key: str) -> bytes:
    return hashlib.sha1(composite_key.encode()).digest()   # 20 bytes

# ─── Low-level I/O ────────────────────────────────────────────────────────────

def _read_all() -> Dict[str, Dict]:
    """Return dict {composite_key: record}."""
    path = _store_path()
    if not os.path.exists(path): return {}
    result = {}
    try:
        with open(path, "rb") as f:
            hdr = f.read(_HDR_SZ)
            if len(hdr) < _HDR_SZ: return {}
            magic, count = struct.unpack(_HDR_FMT, hdr)
            if magic != _MAGIC or count == 0: return {}
            index = []
            for _ in range(count):
                slot = f.read(_SLOT_SZ)
                if len(slot) < _SLOT_SZ: break
                sha1, offset = struct.unpack(_SLOT_FMT, slot)
                index.append((sha1, offset))
            for sha1, offset in index:
                f.seek(offset)
                lr = f.read(_LEN_SZ)
                if len(lr) < _LEN_SZ: continue
                plen = struct.unpack(_LEN_FMT, lr)[0]
                pr = f.read(plen)
                if len(pr) < plen: continue
                try:
                    rec = _loads(pr)
                    ck = _make_key(rec["ns"], rec["key"])
                    result[ck] = rec
                except Exception: pass
    except Exception: pass
    return result

def _write_all(records: Dict[str, Dict]):
    """Tulis ulang seluruh file dari dict records."""
    path = _store_path()
    items = list(records.values())
    payloads = []
    for rec in items:
        try: payloads.append(_dumps(rec))
        except Exception: payloads.append(None)

    count = sum(1 for p in payloads if p is not None)
    data_start = _HDR_SZ + _SLOT_SZ * count
    slots, cur = [], data_start
    valid_items = [(items[i], payloads[i]) for i in range(len(items)) if payloads[i] is not None]

    for rec, p in valid_items:
        ck = _make_key(rec["ns"], rec["key"])
        slots.append((_hash_key(ck), cur))
        cur += _LEN_SZ + len(p)

    try:
        with open(path, "wb") as f:
            f.write(struct.pack(_HDR_FMT, _MAGIC, len(slots)))
            for sha1, off in slots:
                f.write(struct.pack(_SLOT_FMT, sha1, off))
            for _, p in valid_items:
                f.write(struct.pack(_LEN_FMT, len(p)))
                f.write(p)
    except Exception: pass

# ─── Public API ───────────────────────────────────────────────────────────────

def cs_set(
    ns:    str,
    key:   str,
    value: Any,
    label: str = "",
    ttl:   int = 0,          # 0 = permanent; >0 = detik dari sekarang
) -> bool:
    """Simpan/update record. Return True jika berhasil."""
    records = _read_all()
    ck      = _make_key(ns, key)
    now     = int(time.time())
    expiry  = now + ttl if ttl > 0 else 0
    records[ck] = {
        "ns":      ns,
        "key":     key,
        "value":   value,
        "label":   label,
        "ts":      records[ck]["ts"] if ck in records else now,
        "updated": now,
        "ttl":     expiry,
    }
    _write_all(records)
    return True


def cs_get(ns: str, key: str) -> Optional[Any]:
    """Ambil value. Return None jika tidak ada atau sudah expired."""
    records = _read_all()
    ck  = _make_key(ns, key)
    rec = records.get(ck)
    if rec is None: return None
    if rec.get("ttl", 0) > 0 and time.time() > rec["ttl"]:
        cs_delete(ns, key)   # auto-expire
        return None
    return rec.get("value")


def cs_get_record(ns: str, key: str) -> Optional[Dict]:
    """Ambil full record (termasuk metadata)."""
    records = _read_all()
    ck  = _make_key(ns, key)
    rec = records.get(ck)
    if rec is None: return None
    if rec.get("ttl", 0) > 0 and time.time() > rec["ttl"]:
        cs_delete(ns, key)
        return None
    return rec


def cs_delete(ns: str, key: str) -> bool:
    """Hapus record. Return True jika ada yang dihapus."""
    records = _read_all()
    ck = _make_key(ns, key)
    if ck not in records: return False
    del records[ck]
    _write_all(records)
    return True


def cs_list(ns: str) -> List[Dict]:
    """
    List semua record dalam namespace.
    Return list of {"key", "label", "value", "ts", "updated", "ttl"}.
    Expired entries di-filter dan dihapus otomatis.
    """
    records = _read_all()
    now     = int(time.time())
    result  = []
    expired = []
    for ck, rec in records.items():
        if not ck.startswith(f"{ns}::"): continue
        if rec.get("ttl", 0) > 0 and now > rec["ttl"]:
            expired.append(ck)
            continue
        result.append({
            "key":     rec["key"],
            "label":   rec.get("label", ""),
            "value":   rec.get("value"),
            "ts":      rec.get("ts", 0),
            "updated": rec.get("updated", 0),
            "ttl":     rec.get("ttl", 0),
        })
    if expired:
        for ck in expired: del records[ck]
        _write_all(records)
    result.sort(key=lambda r: r["updated"], reverse=True)
    return result


def cs_search(ns: str, query: str) -> List[Dict]:
    """
    Cari record berdasarkan key atau label (case-insensitive substring).
    Berguna untuk natural language lookup.
    """
    q = query.lower()
    return [
        r for r in cs_list(ns)
        if q in r["key"].lower() or q in r.get("label", "").lower()
    ]


def cs_summary(ns: str, max_items: int = 10) -> str:
    """
    Return string ringkasan isi namespace — untuk dimasukkan ke context LLM.
    Format: "key: label = value (updated: ...)"
    """
    items = cs_list(ns)[:max_items]
    if not items: return f"[cloud/{ns}] kosong"
    lines = [f"[cloud/{ns}] {len(items)} item(s):"]
    for r in items:
        val_preview = str(r["value"])[:60]
        label = f" ({r['label']})" if r.get("label") else ""
        lines.append(f"  • {r['key']}{label}: {val_preview}")
    return "\n".join(lines)


def stats() -> Dict:
    records = _read_all()
    now = int(time.time())
    namespaces: Dict[str, int] = {}
    expired = 0
    for rec in records.values():
        ns = rec.get("ns", "?")
        namespaces[ns] = namespaces.get(ns, 0) + 1
        if rec.get("ttl", 0) > 0 and now > rec["ttl"]:
            expired += 1
    path = _store_path()
    return {
        "total":      len(records),
        "expired":    expired,
        "namespaces": namespaces,
        "pack":       _PACK,
        "file_kb":    round(os.path.getsize(path) / 1024, 1) if os.path.exists(path) else 0,
    }
