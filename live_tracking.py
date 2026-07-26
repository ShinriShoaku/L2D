"""
live_tracking.py — Per-session live event tracker.

Tracks chatters, likers, sharers, gifters, followers for:
  - Closing/thanks generation
  - Gift triggers
  - VIP detection

File: live_track.json  (overwritten each session start, persisted live)
"""

import json
import os
import threading
from collections import defaultdict
from datetime import datetime, timezone
from typing import Dict, List, Optional

_lock = threading.Lock()


class LiveTracker:
    """Track all interaction events during a live session."""

    def __init__(self, filepath: str = "live_track.json"):
        self.filepath    = filepath
        self._chatters:  Dict[str, int]         = defaultdict(int)   # username → msg count
        self._likers:    Dict[str, int]         = defaultdict(int)   # username → like count
        self._sharers:   set                    = set()
        self._gifters:   Dict[str, List[Dict]] = defaultdict(list)   # username → [{gift, count}]
        self._followers: List[str]              = []
        self._start_ts   = datetime.now(timezone.utc).isoformat()

    # ── Track events ──────────────────────────────────────────────────────────

    def track_chat(self, username: str):
        with _lock:
            self._chatters[username] += 1
            self._flush_unsafe()

    def track_like(self, username: str, count: int = 1):
        with _lock:
            self._likers[username] += count
            self._flush_unsafe()

    def track_share(self, username: str):
        with _lock:
            self._sharers.add(username)
            self._flush_unsafe()

    def track_gift(self, username: str, gift_name: str, count: int = 1):
        with _lock:
            gifts    = self._gifters[username]
            existing = next((g for g in gifts if g["gift"] == gift_name), None)
            if existing:
                existing["count"] += count
            else:
                gifts.append({"gift": gift_name, "count": count})
            self._flush_unsafe()

    def track_follow(self, username: str):
        with _lock:
            if username not in self._followers:
                self._followers.append(username)
            self._flush_unsafe()

    # ── Summary ───────────────────────────────────────────────────────────────

    def get_summary(self) -> Dict:
        with _lock:
            top_chatters = sorted(
                self._chatters.items(), key=lambda x: x[1], reverse=True
            )[:5]
            top_likers = sorted(
                self._likers.items(), key=lambda x: x[1], reverse=True
            )[:5]
            top_gifters = sorted(
                self._gifters.items(),
                key=lambda x: sum(g["count"] for g in x[1]),
                reverse=True
            )[:5]
            return {
                "total_chatters": len(self._chatters),
                "total_messages": sum(self._chatters.values()),
                "total_likes":    sum(self._likers.values()),
                "total_shares":   len(self._sharers),
                "top_chatters":   [f"{u} ({c}x chat)" for u, c in top_chatters],
                "top_likers":     [f"{u} ({c}x like)" for u, c in top_likers],
                "sharers":        sorted(self._sharers)[:5],
                "top_gifters": [
                    f"{u}: " + ", ".join(f"{g['gift']}×{g['count']}" for g in gs)
                    for u, gs in top_gifters
                ],
                "followers":   self._followers[:5],
                "start_ts":    self._start_ts,
            }

    def build_thanks_prompt(self, char_name: str, char_cfg: Dict) -> str:
        """
        Build a NEW prompt to send to the model for special thanks.
        Uses live tracking data to personalize the message.
        """
        s = self.get_summary()
        animations = char_cfg.get("animations", ["smile", "angry", "shy", "default"])
        anim_list  = str(animations).replace("'", '"')

        parts = [
            f"Kamu adalah {char_name}.",
            "Buat UCAPAN TERIMA KASIH SPESIAL kepada semua yang menemani live hari ini.",
            f"Data sesi live:",
            f"- Total penonton yang chat: {s['total_chatters']} orang",
            f"- Total pesan masuk: {s['total_messages']}",
            f"- Total like: {s['total_likes']}",
            f"- Total share: {s['total_shares']}",
        ]
        if s["top_chatters"]:
            parts.append(f"- Paling sering chat: {', '.join(s['top_chatters'])}")
        if s["top_likers"]:
            parts.append(f"- Paling banyak like: {', '.join(s['top_likers'])}")
        if s["sharers"]:
            parts.append(f"- Yang share stream: {', '.join(s['sharers'])}")
        if s["top_gifters"]:
            parts.append(f"- Gifter spesial: {', '.join(s['top_gifters'])}")
        if s["followers"]:
            parts.append(f"- Follower baru: {', '.join(s['followers'])}")
        parts += [
            "",
            "Instruksi:",
            "- Sebutkan nama-nama yang berjasa jika ada",
            "- Tulus, hangat, berkesan — sesuai kepribadian karakter",
            f"- Gaya bicara karakter ({char_name})",
            "- MINIMUM 3 segments, maksimum 5 segments",
            "",
            f"Output JSON valid:",
            f'{{"responses": [{{"ind": "...", "jp": "...", "anim": "..."}}], '
            f'"points": 0, "topic": "thanks", "info": "", "note": "", "command": ""}}',
            f"anim pilih dari: {', '.join(animations)}",
            "jp HANYA karakter Jepang (hiragana/katakana/kanji), ZERO huruf Latin.",
        ]
        return "\n".join(parts)

    # ── Gift trigger check ────────────────────────────────────────────────────

    def get_gifter_total(self, username: str) -> int:
        """Total gift count for a specific user this session."""
        with _lock:
            return sum(g["count"] for g in self._gifters.get(username, []))

    # ── Persistence ───────────────────────────────────────────────────────────

    def _flush_unsafe(self):
        """Save without acquiring lock (must be called inside _lock)."""
        try:
            with open(self.filepath, "w", encoding="utf-8") as f:
                json.dump(self._to_dict_unsafe(), f, indent=2, ensure_ascii=False)
        except Exception:
            pass

    def _to_dict_unsafe(self) -> Dict:
        return {
            "chatters":  dict(self._chatters),
            "likers":    dict(self._likers),
            "sharers":   list(self._sharers),
            "gifters":   {u: list(gs) for u, gs in self._gifters.items()},
            "followers": list(self._followers),
            "start_ts":  self._start_ts,
        }

    def reset(self):
        """Reset for new session."""
        with _lock:
            self._chatters  = defaultdict(int)
            self._likers    = defaultdict(int)
            self._sharers   = set()
            self._gifters   = defaultdict(list)
            self._followers = []
            self._start_ts  = datetime.now(timezone.utc).isoformat()
            self._flush_unsafe()

    def load(self):
        """Resume tracking from saved file."""
        if not os.path.exists(self.filepath):
            return
        try:
            with open(self.filepath, "r", encoding="utf-8") as f:
                d = json.load(f)
            with _lock:
                self._chatters  = defaultdict(int,  d.get("chatters",  {}))
                self._likers    = defaultdict(int,  d.get("likers",    {}))
                self._sharers   = set(d.get("sharers", []))
                raw_gifters     = d.get("gifters", {})
                self._gifters   = defaultdict(list)
                for u, gs in raw_gifters.items():
                    self._gifters[u] = list(gs)
                self._followers = d.get("followers", [])
                self._start_ts  = d.get("start_ts", self._start_ts)
        except Exception:
            pass
