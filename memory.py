"""
memory.py — Per-user memory.

User memory file: memory/user_{safe_id}.json
Format:
{
  "user_id":             "12345",
  "username":            "Shinri",
  "info_user":           ["suka kopi", "kerja di IT"],
  "romance_status":      "teman",
  "romance_points":      120,
  "note":                [{"text": "...", "ts": "..."}],
  "gift_history":        [{"gift": "Rose", "count": 3, "ts": "..."}],
  "vip_user":            false,
  "last_chat_timestamp": "Sabtu, 12 Juli 2025, 09:18 AM",
  "_last_chat_iso":      "2025-07-12T02:18:00+00:00"
}
"""

import json
import os
import re
import threading
from collections import OrderedDict
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

# ─── Time helpers ─────────────────────────────────────────────────────────────

def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()

def format_ts_indonesian(dt: datetime) -> str:
    days   = ["Senin","Selasa","Rabu","Kamis","Jumat","Sabtu","Minggu"]
    months = ["Januari","Februari","Maret","April","Mei","Juni",
              "Juli","Agustus","September","Oktober","November","Desember"]
    return (
        f"{days[dt.weekday()]}, {dt.day} {months[dt.month - 1]} {dt.year}, "
        f"{dt.strftime('%I:%M %p')}"
    )

def time_ago(ts_iso: str) -> str:
    """Convert ISO timestamp → human-readable Indonesian relative time."""
    try:
        ts = datetime.fromisoformat(ts_iso)
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        diff = int((datetime.now(timezone.utc) - ts).total_seconds())
        if diff < 60:    return "baru saja"
        if diff < 3600:  return f"{diff // 60} mnt yang lalu"
        if diff < 86400: return f"{diff // 3600} jam yang lalu"
        return f"{diff // 86400} hari yang lalu"
    except Exception:
        return "lama"

# ─── File lock helpers ────────────────────────────────────────────────────────

_file_locks: Dict[str, threading.Lock] = {}
_flock_global = threading.Lock()

def _get_lock(path: str) -> threading.Lock:
    with _flock_global:
        if path not in _file_locks:
            _file_locks[path] = threading.Lock()
        return _file_locks[path]

def _safe_id(uid: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_\-]", "_", str(uid))[:64]

# ─── UserMemory ───────────────────────────────────────────────────────────────

class UserMemory:
    """
    Per-user persistent memory.
    Stores info, notes, gifts, romance status, romance points, VIP flag, timestamps.
    """
    MAX_INFO  = 20
    MAX_NOTES = 30
    MAX_GIFTS = 50

    # Romance level thresholds (romance_points → label)
    ROMANCE_LEVELS = [
        (500, "sangat_dekat"),
        (300, "dekat"),
        (150, "akrab"),
        (50,  "kenal"),
        (0,   "baru_kenal"),
    ]

    def __init__(self, user_id: str, username: str, storage_dir: str = "memory"):
        self.user_id      = user_id
        self.username     = username
        self.storage_dir  = storage_dir
        os.makedirs(storage_dir, exist_ok=True)
        self.filepath = os.path.join(storage_dir, f"user_{_safe_id(user_id)}.json")
        self._lock    = threading.Lock()
        self.data     = self._load()

    # ── I/O ──────────────────────────────────────────────────────────────────

    def _default(self) -> Dict:
        now = datetime.now(timezone.utc)
        return {
            "user_id":             self.user_id,
            "username":            self.username,
            "info_user":           [],
            "romance_status":      "",
            "romance_points":      0,
            "note":                [],
            "gift_history":        [],
            "vip_user":            False,
            "last_chat_timestamp": format_ts_indonesian(now),
            "_last_chat_iso":      now_iso(),
        }

    def _load(self) -> Dict:
        if os.path.exists(self.filepath):
            try:
                with open(self.filepath, "r", encoding="utf-8") as f:
                    d = json.load(f)
                # Patch missing keys (handles legacy files without romance_points)
                for k, v in self._default().items():
                    d.setdefault(k, v)
                return d
            except Exception:
                pass
        return self._default()

    def save(self):
        lock = _get_lock(self.filepath)
        with lock:
            with open(self.filepath, "w", encoding="utf-8") as f:
                json.dump(self.data, f, indent=2, ensure_ascii=False)

    # ── Timestamp ────────────────────────────────────────────────────────────

    def touch(self):
        """Update last chat timestamp to now."""
        now = datetime.now(timezone.utc)
        self.data["last_chat_timestamp"] = format_ts_indonesian(now)
        self.data["_last_chat_iso"]      = now_iso()
        self.save()

    def get_last_chat_ago(self) -> str:
        return time_ago(self.data.get("_last_chat_iso", now_iso()))

    # ── Info ─────────────────────────────────────────────────────────────────

    def add_info(self, info: str):
        """Add a user info item (deduped)."""
        info = info.strip()
        if not info or info in self.data["info_user"]:
            return
        self.data["info_user"].append(info)
        if len(self.data["info_user"]) > self.MAX_INFO:
            self.data["info_user"] = self.data["info_user"][-self.MAX_INFO:]
        self.save()

    def get_recent_info(self, n: int = 3) -> List[str]:
        return self.data.get("info_user", [])[-n:]

    # ── Notes ─────────────────────────────────────────────────────────────────

    def add_note(self, note: str):
        note = note.strip()
        if not note:
            return
        self.data["note"].append({"text": note, "ts": now_iso()})
        if len(self.data["note"]) > self.MAX_NOTES:
            self.data["note"] = self.data["note"][-self.MAX_NOTES:]
        self.save()

    def get_recent_notes(self, n: int = 2) -> List[str]:
        return [item["text"] for item in self.data.get("note", [])[-n:]]

    # ── Gifts ─────────────────────────────────────────────────────────────────

    def add_gift(self, gift_name: str, count: int = 1):
        self.data["gift_history"].append({
            "gift": gift_name, "count": count, "ts": now_iso()
        })
        if len(self.data["gift_history"]) > self.MAX_GIFTS:
            self.data["gift_history"] = self.data["gift_history"][-self.MAX_GIFTS:]
        self.save()

    def get_gift_summary(self) -> str:
        gifts: Dict[str, int] = {}
        for g in self.data.get("gift_history", []):
            gifts[g["gift"]] = gifts.get(g["gift"], 0) + g["count"]
        if not gifts:
            return ""
        top = sorted(gifts.items(), key=lambda x: x[1], reverse=True)[:3]
        return ", ".join(f"{g}×{c}" for g, c in top)

    # ── Romance Points ────────────────────────────────────────────────────────

    @property
    def romance_points(self) -> int:
        return int(self.data.get("romance_points", 0))

    def add_romance_points(self, delta: int):
        """
        Add or subtract romance points (clamped 0–9999).
        Positive delta = add, negative delta = subtract.
        """
        with self._lock:
            current = int(self.data.get("romance_points", 0))
            self.data["romance_points"] = max(0, min(9999, current + delta))
        self.save()

    def get_romance_level(self) -> str:
        pts = self.romance_points
        for threshold, label in self.ROMANCE_LEVELS:
            if pts >= threshold:
                return label
        return "baru_kenal"

    # ── Romance Status / VIP ──────────────────────────────────────────────────

    def set_romance_status(self, status: str):
        self.data["romance_status"] = status.strip()
        self.save()

    def get_romance_status(self) -> str:
        return self.data.get("romance_status", "")

    def set_vip(self, flag: bool):
        self.data["vip_user"] = flag
        self.save()

    # ── Display name ──────────────────────────────────────────────────────────

    def get_display_name(self) -> str:
        """Return current username (which may have been updated from nickname)."""
        return self.data.get("username", self.username)

    def update_username(self, new_username: str):
        """Update username field directly. Replaces old username/nickname."""
        new_username = new_username.strip()
        if not new_username:
            return
        self.username = new_username
        self.data["username"] = new_username
        self.save()

    # ── Context lines for CTX injection ───────────────────────────────────────

    def get_context_lines(self) -> List[str]:
        """Extra context lines (non-romance, non-timestamp)."""
        lines = []
        if self.data.get("vip_user"):
            lines.append("vip=true")
        recent_info = self.get_recent_info(3)
        if recent_info:
            lines.append(f"info={'; '.join(recent_info)}")
        recent_notes = self.get_recent_notes(1)
        if recent_notes:
            lines.append(f"note={recent_notes[0]}")
        return lines


# ─── UserMemoryManager (LRU cache) ────────────────────────────────────────────

class UserMemoryManager:
    """Thread-safe LRU cache for UserMemory objects."""

    def __init__(self, storage_dir: str = "memory", max_cache: int = 200):
        self.storage_dir = storage_dir
        self.max_cache   = max_cache
        self._cache: OrderedDict[str, UserMemory] = OrderedDict()
        self._lock  = threading.Lock()
        os.makedirs(storage_dir, exist_ok=True)

    def get(self, user_id: str, username: str) -> "UserMemory":
        with self._lock:
            if user_id in self._cache:
                self._cache.move_to_end(user_id)
                return self._cache[user_id]
            mem = UserMemory(user_id, username, self.storage_dir)
            self._cache[user_id] = mem
            while len(self._cache) > self.max_cache:
                _, evict = self._cache.popitem(last=False)
                try:
                    evict.save()
                except Exception:
                    pass
            return mem

    def size(self) -> int:
        with self._lock:
            return len(self._cache)

    def find_by_username(self, username: str) -> Optional["UserMemory"]:
        """Cari user memory berdasarkan username (case-insensitive)."""
        with self._lock:
            for mem in self._cache.values():
                if mem.username.lower() == username.lower():
                    return mem
        # Coba scan dari disk jika tidak di cache
        if not os.path.isdir(self.storage_dir):
            return None
        for fname in os.listdir(self.storage_dir):
            if not fname.startswith("user_") or not fname.endswith(".json"):
                continue
            try:
                with open(os.path.join(self.storage_dir, fname), "r", encoding="utf-8") as f:
                    d = json.load(f)
                if d.get("username", "").lower() == username.lower():
                    uid = d.get("user_id", fname[5:-5])
                    mem = UserMemory(uid, username, self.storage_dir)
                    mem.data = d
                    self._cache[uid] = mem
                    return mem
            except Exception:
                continue
        return None

    def get_or_create_by_username(self, username: str, user_id: str = None) -> "UserMemory":
        """Cari by username, kalau tidak ada buat baru."""
        found = self.find_by_username(username)
        if found:
            return found
        uid = user_id or f"u_{username.lower().replace(' ', '_')}"
        return self.get(uid, username)