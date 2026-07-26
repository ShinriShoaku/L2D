"""
chat_history.py — Unified shared chat history (cross-user, max 7 entries).

File: {char_name}.history.json
Each entry: {"role": "user"|"assistant", "content": "...", "username": "...", "user_id": "..."}

History sekarang track user_id. Saat build messages untuk LLM:
- Assistant entries: tanpa prefix (karena karakter tidak perlu tahu siapa yang diajari)
- User entries: prefix dengan [username] SAJA untuk user yang sedang chat sekarang
- User entries dari user lain: prefix [username] tetap ada untuk konteks grup
"""

import json
import os
import threading
from typing import Dict, List, Optional

MAX_HISTORY = 7


class ChatHistory:
    """
    Shared conversation history, unified across all users.
    Max 7 entries sliding window.
    """

    def __init__(self, model_name: str, storage_dir: str = "."):
        self.filepath = os.path.join(storage_dir, f"{model_name}.history.json")
        self._lock    = threading.Lock()
        self._history: List[Dict] = self._load()

    # ── I/O ──────────────────────────────────────────────────────────────────

    def _load(self) -> List[Dict]:
        if os.path.exists(self.filepath):
            try:
                with open(self.filepath, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, list):
                    return data[-MAX_HISTORY:]
            except Exception:
                pass
        return []

    def _save(self):
        """Must be called inside _lock."""
        try:
            with open(self.filepath, "w", encoding="utf-8") as f:
                json.dump(self._history, f, indent=2, ensure_ascii=False)
        except Exception:
            pass

    # ── Mutation ─────────────────────────────────────────────────────────────

    def add(self, role: str, content: str, username: Optional[str] = None, user_id: Optional[str] = None):
        """Add one entry. Track user_id untuk filtering context."""
        content = (content or "").strip()
        if not content:
            return
        with self._lock:
            entry: Dict = {"role": role, "content": content}
            if username and role == "user":
                entry["username"] = username
            if user_id and role == "user":
                entry["user_id"] = user_id
            self._history.append(entry)
            if len(self._history) > MAX_HISTORY:
                self._history = self._history[-MAX_HISTORY:]
            self._save()

    def clear(self):
        with self._lock:
            self._history = []
            self._save()

    # ── Query ─────────────────────────────────────────────────────────────────

    def get_messages(self, current_user_id: str = None, current_username: str = None) -> List[Dict]:
        """
        Return history as OpenAI-style messages.

        Strategi username prefix:
        - Assistant: tanpa prefix (karakter tidak perlu tahu siapa yang diajari)
        - User (current user): prefix [username] untuk memperkuat identitas
        - User (user lain): prefix [username] untuk konteks grup
        """
        with self._lock:
            result = []
            for h in self._history:
                content = h["content"]
                if h["role"] == "user":
                    uname = h.get("username", "")
                    uid = h.get("user_id", "")
                    # Selalu prefix dengan username asli dari history
                    # Jangan pernah ganti dengan current_username!
                    if uname:
                        content = f"[{uname}]: {content}"
                    else:
                        content = f"[user]: {content}"
                result.append({"role": h["role"], "content": content})
            return result

    def get_messages_for_user(self, current_user_id: str, current_username: str) -> List[Dict]:
        """
        Return history yang di-filter untuk user tertentu.
        Hanya tampilkan:
        - Semua assistant responses (tanpa prefix username)
        - User messages dari user ini saja (dengan prefix [username])
        - User messages dari user lain: tetap tampil tapi dengan username asli mereka

        Ini mencegah model "menggeneralisasi" nama user dari history.
        """
        with self._lock:
            result = []
            for h in self._history:
                content = h["content"]
                if h["role"] == "user":
                    uname = h.get("username", "")
                    uid = h.get("user_id", "")
                    # SELALU gunakan username yang tersimpan di history, bukan current_username
                    if uname:
                        content = f"[{uname}]: {content}"
                    else:
                        content = f"[user]: {content}"
                result.append({"role": h["role"], "content": content})
            return result

    def get_recent_summary(self, n: int = 3) -> str:
        """Return last n exchanges as readable summary (for banter context)."""
        with self._lock:
            recent = self._history[-n * 2:]
        lines = []
        for h in recent:
            role = h["role"]
            uname = h.get("username", "")
            content = h["content"][:80]
            if role == "user":
                label = f"[{uname}]" if uname else "[user]"
            else:
                label = "[char]"
            lines.append(f"{label}: {content}")
        return "\n".join(lines)

    def get_last_topic_hint(self) -> str:
        """Extract last few exchanges for banter topic context."""
        with self._lock:
            last = self._history[-4:] if len(self._history) >= 4 else self._history[:]
        return " | ".join(
            h["content"][:50] for h in last if h.get("content")
        )
