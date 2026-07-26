"""
model_memory.py — Per-character model memory.

File: {char_name}.memory.json  (e.g.  liana.memory.json)
Format:
{
  "topik":   "morning_greeting",
  "role":    "default",
  "command": "nyann~",
  "style":   "teasing"
}

NOTE: romance_points is now stored per-user in UserMemory, not here.
"""

import json
import os
import threading
from typing import Dict, Optional


class ModelMemory:
    """
    Persistent per-character state shared across all users.
    Tracks current topic, role, command word, and style.
    romance_points has been moved to UserMemory (per-user).
    """

    def __init__(self, model_name: str, storage_dir: str = "."):
        self.model_name = model_name.lower()
        self.filepath   = os.path.join(storage_dir, f"{self.model_name}.memory.json")
        self._lock      = threading.Lock()
        self.data       = self._load()

    # ── I/O ──────────────────────────────────────────────────────────────────

    def _default(self) -> Dict:
        return {
            "topik":   "general",
            "role":    "default",
            "command": "",
            "style":   "normal",
        }

    def _load(self) -> Dict:
        if os.path.exists(self.filepath):
            try:
                with open(self.filepath, "r", encoding="utf-8") as f:
                    d = json.load(f)
                for k, v in self._default().items():
                    d.setdefault(k, v)
                # Remove legacy romance_points if present
                d.pop("romance_points", None)
                return d
            except Exception:
                pass
        d = self._default()
        self._write(d)
        return d

    def _write(self, data: Dict = None):
        target = data or self.data
        with open(self.filepath, "w", encoding="utf-8") as f:
            json.dump(target, f, indent=2, ensure_ascii=False)

    def save(self):
        with self._lock:
            self._write()

    # ── Properties ────────────────────────────────────────────────────────────

    @property
    def topik(self) -> str:
        return self.data.get("topik", "general")

    @topik.setter
    def topik(self, value: str):
        self.update_topic(value)

    @property
    def role(self) -> str:
        return self.data.get("role", "default")

    @role.setter
    def role(self, value: str):
        self.update_role(value)

    @property
    def command(self) -> str:
        return self.data.get("command", "")

    @command.setter
    def command(self, value: str):
        self.update_command(value)

    @property
    def style(self) -> str:
        return self.data.get("style", "normal")

    @style.setter
    def style(self, value: str):
        self.update_style(value)

    # ── Setters ───────────────────────────────────────────────────────────────

    def update_topic(self, topic: str):
        t = (topic or "").strip()
        if t:
            self.data["topik"] = t
            self.save()

    def update_style(self, style: str):
        s = (style or "").strip()
        if s:
            self.data["style"] = s
            self.save()

    def update_command(self, cmd: str):
        self.data["command"] = (cmd or "").strip()
        self.save()

    def update_role(self, role: str):
        r = (role or "").strip()
        if r:
            self.data["role"] = r
            self.save()
