#!/usr/bin/env python3
"""
agent_core.py — Stateful Core & Tool Domains Agent  (v2)

v2: disesuaikan dengan pipeline Soul (no jp) + Translate pass terpisah.

Arsitektur ReAct (Reasoning + Acting):
  Trigger → [Thought] → [Action: tool call] → [Observation] → (repeat) → done

─────────────────────────────────────────────────────────────────
Komponen:
  AgentState      live in-memory state (mood, topic, active_user, uptime)
  ToolDomain      enum domain: MEMORY | ACTION | COGNITIVE | STREAM
  AgentTool       satu tool: domain, nama, deskripsi, fungsi
  ToolRegistry    registry semua tool, bisa generate schema prompt
  ReActLoop       Thought → Action → Observation (max MAX_STEPS)
  StatefulAgent   orchestrator utama — entry point dari LiveServer
  IdleScheduler   proaktif: trigger handle_idle jika stream sepi

─────────────────────────────────────────────────────────────────
Cara pakai di LiveServer.__init__:

    self.agent = StatefulAgent(
        user_mgr   = self.user_mgr,
        model_mem  = _main._model_mem,
        chat_hist  = _main._chat_hist,
        l2d        = self.l2d,
        tracker    = self.tracker,
        char_name  = self.char_mgr.active,
        char_data  = CHARACTER,
    )
    self.idle_sched = IdleScheduler(
        agent          = self.agent,
        idle_threshold = 600,
        on_response    = lambda r, d: play_segments(r, d, self.l2d),
        is_busy_fn     = self.pipeline.has_work,
    )

Di LiveServer.run():
    self.idle_sched.start()

Di ResponsePipeline._start_gen():
    # Ganti full_generate(...) dengan:
    segs, expr = self.agent.handle_chat(user_id, username, model_input)

Di setiap event handler (_on_comment, _on_gift, dll):
    self.idle_sched.reset()

─────────────────────────────────────────────────────────────────
Desain keputusan:
  - ReAct loop jalan SEBELUM full_generate (baca konteks, set L2D, update state)
  - full_generate tetap dipakai beserta router-nya — TIDAK digantikan
  - Untuk idle: generate_from_prompt dengan idle prompt berbasis state
  - NO modifikasi ke main.py / mcp_tools.py / model_memory.py
"""

from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Tuple

from config import client, MODEL_NAME, DEBUG   # ← pakai client yang sudah ada
from memory import UserMemory, UserMemoryManager
from model_memory import ModelMemory


# ═════════════════════════════════════════════════════════════════════════════
# 1.  DOMAIN ENUM
# ═════════════════════════════════════════════════════════════════════════════

class ToolDomain(str, Enum):
    MEMORY    = "memory"    # baca/tulis user/model memory
    ACTION    = "action"    # Live2D anim, model state
    COGNITIVE = "cognitive" # analisis teks, waktu, konteks
    STREAM    = "stream"    # statistik sesi live


# ═════════════════════════════════════════════════════════════════════════════
# 2.  AGENT STATE
# ═════════════════════════════════════════════════════════════════════════════

@dataclass
class AgentState:
    """
    Status agen yang dipertahankan selama sesi (in-memory).
    Direset saat LiveServer di-restart.
    """
    current_mood:        str   = "neutral"
    current_topic:       str   = "general"
    active_user:         str   = ""
    stream_start:        float = field(default_factory=time.monotonic)
    idle_since:          float = field(default_factory=time.monotonic)
    recent_observations: List[str] = field(default_factory=list)  # max 5
    last_tool_calls:     List[str] = field(default_factory=list)  # max 8
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    # ── helpers ───────────────────────────────────────────────────────────────

    def uptime_str(self) -> str:
        secs = int(time.monotonic() - self.stream_start)
        h, m = divmod(secs // 60, 60)
        s    = secs % 60
        if h:
            return f"{h}j {m}m"
        if m:
            return f"{m}m {s}s"
        return f"{s}s"

    def idle_str(self) -> str:
        secs = int(time.monotonic() - self.idle_since)
        if secs < 60:
            return f"{secs}s"
        return f"{secs // 60}m {secs % 60}s"

    def set_mood(self, mood: str):
        with self._lock:
            self.current_mood = mood.lower().strip() or "neutral"

    def set_topic(self, topic: str):
        with self._lock:
            self.current_topic = topic.strip() or "general"

    def set_active_user(self, username: str):
        with self._lock:
            self.active_user = username
            self.idle_since  = time.monotonic()

    def add_observation(self, obs: str):
        with self._lock:
            self.recent_observations.append(obs)
            if len(self.recent_observations) > 5:
                self.recent_observations.pop(0)

    def add_tool_call(self, name: str):
        with self._lock:
            self.last_tool_calls.append(name)
            if len(self.last_tool_calls) > 8:
                self.last_tool_calls.pop(0)

    def summary(self) -> str:
        return (
            f"mood={self.current_mood} topic={self.current_topic} "
            f"user={self.active_user or 'none'} "
            f"uptime={self.uptime_str()} idle={self.idle_str()}"
        )

    def last_context(self) -> str:
        """Ringkas observasi terbaru — untuk idle prompt."""
        return " | ".join(self.recent_observations[-3:]) if self.recent_observations else ""


# ═════════════════════════════════════════════════════════════════════════════
# 3.  TOOL DEFINITION
# ═════════════════════════════════════════════════════════════════════════════

@dataclass
class AgentTool:
    name:        str
    domain:      ToolDomain
    description: str                      # ≤ 60 char — masuk prompt router
    func:        Callable[..., str]       # selalu return str (observation)
    args_schema: Dict[str, str] = field(default_factory=dict)  # {"arg": "type"}


# ═════════════════════════════════════════════════════════════════════════════
# 4.  TOOL REGISTRY
# ═════════════════════════════════════════════════════════════════════════════

class ToolRegistry:
    def __init__(self):
        self._tools: Dict[str, AgentTool] = {}

    def register(self, tool: AgentTool):
        self._tools[tool.name] = tool

    def get(self, name: str) -> Optional[AgentTool]:
        return self._tools.get(name)

    def call(self, name: str, args: Dict[str, Any]) -> str:
        tool = self._tools.get(name)
        if not tool:
            return f"[ERROR] Tool '{name}' tidak ditemukan"
        try:
            return tool.func(**args)
        except TypeError as e:
            return f"[ERROR] Args salah '{name}': {e}"
        except Exception as e:
            return f"[ERROR] '{name}' gagal: {e}"

    def schema_prompt(self) -> str:
        """Semua tools per domain → diembed ke ReAct system prompt."""
        by_domain: Dict[ToolDomain, List[str]] = {}
        for t in self._tools.values():
            sig = f"{t.name}({','.join(t.args_schema)}) — {t.description}"
            by_domain.setdefault(t.domain, []).append(sig)
        lines = []
        for domain in ToolDomain:
            items = by_domain.get(domain, [])
            if items:
                lines.append(f"[{domain.value.upper()}]")
                lines.extend(f"  {it}" for it in items)
        return "\n".join(lines)


# ═════════════════════════════════════════════════════════════════════════════
# 5.  REACT LOOP
# ═════════════════════════════════════════════════════════════════════════════

_REACT_SYS = """\
Kamu adalah reasoning core untuk AI VTuber bernama {char_name}.
State saat ini: {state_summary}

Tools tersedia:
{tool_schema}

Loop: Thought → Action → Observation → (ulangi maks {max_steps}x) → done.

Output WAJIB salah satu JSON berikut:
  {{"thought":"...", "action":{{"tool":"nama","args":{{}}}}}}
  {{"thought":"...", "done":true, "context":"ringkasan temuan"}}

Aturan:
- done=true jika tidak perlu tool lagi ATAU sudah cukup konteks.
- Jangan panggil tool yang sama dua kali.
- Output HANYA JSON. Tanpa penjelasan, tanpa markdown backtick."""


class ReActLoop:
    MAX_STEPS = 3

    def __init__(
        self,
        registry:  ToolRegistry,
        state:     AgentState,
        char_name: str = "karakter",
    ):
        self.registry  = registry
        self.state     = state
        self.char_name = char_name

    def run(self, trigger: str) -> None:
        """
        Jalankan loop ReAct. Tools berjalan sebagai efek samping
        (update AgentState, L2D, UserMemory, dll).
        Tidak mengembalikan nilai — perubahan tersimpan di state.
        """
        system = _REACT_SYS.format(
            char_name    = self.char_name,
            state_summary= self.state.summary(),
            tool_schema  = self.registry.schema_prompt(),
            max_steps    = self.MAX_STEPS,
        )
        messages: List[Dict] = [
            {"role": "user", "content": f"Trigger: {trigger}"}
        ]
        tools_called: List[str] = []

        for step in range(self.MAX_STEPS):
            raw = self._call_llm(system, messages)
            if not raw:
                break

            parsed = self._parse_json(raw)
            if not parsed:
                break

            thought = parsed.get("thought", "")
            if DEBUG:
                print(f"[AGENT] step{step+1} thought: {thought[:80]}")

            # done → keluar
            if parsed.get("done"):
                ctx = parsed.get("context", "")
                if ctx:
                    self.state.add_observation(f"[REASONING] {ctx}")
                break

            # action → eksekusi tool
            action    = parsed.get("action", {})
            tool_name = (action.get("tool") or "").strip()
            tool_args = action.get("args") or {}
            if not isinstance(tool_args, dict):
                tool_args = {}

            if not tool_name:
                break
            if tool_name in tools_called:
                if DEBUG:
                    print(f"[AGENT] skip duplicate: {tool_name}")
                break

            obs = self.registry.call(tool_name, tool_args)
            tools_called.append(tool_name)
            self.state.add_tool_call(tool_name)
            self.state.add_observation(f"{tool_name}: {obs}")

            if DEBUG:
                print(f"[AGENT] {tool_name}({tool_args}) → {obs[:80]}")

            messages.append({"role": "assistant", "content": raw})
            messages.append({
                "role": "user",
                "content": f"Observation: {obs}\nLanjutkan atau done."
            })

    # ── internals ─────────────────────────────────────────────────────────────

    def _call_llm(self, system: str, messages: List[Dict]) -> str:
        try:
            resp = client.chat.completions.create(
                model       = MODEL_NAME,
                messages    = [{"role": "system", "content": system}] + messages,
                max_tokens  = 256,
                temperature = 0.3,
            )
            return (resp.choices[0].message.content or "").strip()
        except Exception as e:
            if DEBUG:
                print(f"[AGENT] LLM error: {e}")
            return ""

    @staticmethod
    def _parse_json(raw: str) -> Optional[Dict]:
        text = raw.strip()
        # strip markdown fence
        if text.startswith("```"):
            parts = text.split("```")
            text  = parts[1] if len(parts) > 1 else text
            if text.lower().startswith("json"):
                text = text[4:]
        try:
            return json.loads(text.strip())
        except Exception:
            s = text.find("{")
            e = text.rfind("}")
            if s != -1 and e > s:
                try:
                    return json.loads(text[s:e + 1])
                except Exception:
                    pass
        return None


# ═════════════════════════════════════════════════════════════════════════════
# 6.  STATEFUL AGENT
# ═════════════════════════════════════════════════════════════════════════════

class StatefulAgent:
    """
    Entry point tunggal untuk LiveServer dan DesktopClient.

    Alur handle_chat:
      1. ReAct loop → tools jalan (set L2D, update state/notes)
      2. full_generate (Soul model → ind+anim) + _translate_segments (jp)
      3. Return (responses, dominant) dengan {ind, jp, anim}

    Alur handle_idle:
      1. ReAct loop → tools jalan (play_motion, set_expression, dll)
      2. generate_from_prompt dengan prompt idle berbasis state
      3. Return (responses, dominant)
    """

    def __init__(
        self,
        user_mgr:   UserMemoryManager,
        model_mem:  ModelMemory,
        chat_hist,                         # ChatHistory — boleh None
        l2d,                               # Live2DClient — boleh None
        tracker     = None,                # LiveTracker  — boleh None
        char_name:  str  = "default",
        char_data:  Dict = None,
    ):
        self.user_mgr  = user_mgr
        self.model_mem = model_mem
        self.chat_hist = chat_hist
        self.l2d       = l2d
        self.tracker   = tracker
        self.char_name = char_name
        self.char_data = char_data or {}

        # State agen
        self.state = AgentState()
        self.state.set_topic(model_mem.topik)

        # Pakai client yang sudah diinisialisasi di config.py (hindari duplikasi)
        self.client   = client
        self.registry = ToolRegistry()
        self._register_all_tools()

        self.react = ReActLoop(
            self.registry, self.state, char_name
        )

        if DEBUG:
            print(f"[AGENT] StatefulAgent ready — char={char_name}")

    # ═════════════════════════════════════════════════════════════════════════
    # PUBLIC API
    # ═════════════════════════════════════════════════════════════════════════

    def handle_chat(
        self,
        user_id:  str,
        username: str,
        content:  str,
    ) -> Tuple[List[Dict], str]:
        """
        Proses chat masuk.
        Panggil ini dari ResponsePipeline._start_gen sebagai pengganti
        full_generate() langsung.

        Alur:
          ReAct (set mood/topic, L2D, baca user notes)
          → full_generate (pipeline normal dengan router-nya)
        """
        user_mem = self.user_mgr.get(user_id, username)
        self.state.set_active_user(username)

        # Phase 1: ReAct — set efek samping sebelum generate
        trigger = f"user={username} chat={content!r}"
        self.react.run(trigger)

        # Sync topic jika tools mengubahnya
        if self.state.current_topic != self.model_mem.topik:
            self.model_mem.update_topic(self.state.current_topic)

        # Phase 2: generate normal
        return self._run_full_generate(content, user_mem, username)

    def handle_idle(self, reason: str = "idle") -> Tuple[List[Dict], str]:
        """
        Dipanggil IdleScheduler saat stream sepi.
        Agent memilih ekspresi & animasi, lalu generate monolog spontan.
        """
        trigger = (
            f"IDLE — tidak ada chat {self.state.idle_str()} | "
            f"uptime={self.state.uptime_str()} | mood={self.state.current_mood}"
        )
        self.react.run(trigger)    # tools: play_motion, set_expression, dll

        idle_prompt = self._build_idle_prompt()
        return self._run_generate_from_prompt(idle_prompt)

    def handle_event(
        self,
        event_type: str,
        data:       Dict,
    ) -> Tuple[List[Dict], str]:
        """
        Event trigger opsional (gift/follow/share/raid).
        Bisa dipakai sebagai alternatif pipeline.push jika ingin
        respons lebih cepat tanpa antrian.
        """
        username = data.get("username", "someone")
        user_id  = data.get("user_id",  "_unknown")

        trigger_map = {
            "gift":  lambda: (
                f"EVENT gift — {username} beri "
                f"{data.get('gift_name','?')} ×{data.get('count',1)}"
            ),
            "follow":    lambda: f"EVENT follow — {username} baru follow",
            "share":     lambda: f"EVENT share — {username} share stream",
            "raid":      lambda: (
                f"EVENT raid — {username} bawa "
                f"{data.get('viewer_count','?')} viewer"
            ),
            "milestone": lambda: f"EVENT milestone — {data}",
        }
        trigger = trigger_map.get(event_type, lambda: f"EVENT {event_type} — {data}")()
        self.react.run(trigger)

        user_mem  = self.user_mgr.get(user_id, username)
        event_msg = trigger.replace("EVENT ", "")
        return self._run_full_generate(event_msg, user_mem, username)

    # ═════════════════════════════════════════════════════════════════════════
    # STATE ACCESSORS — untuk LiveServer / BanterManager
    # ═════════════════════════════════════════════════════════════════════════

    @property
    def mood(self) -> str:
        return self.state.current_mood

    @property
    def topic(self) -> str:
        return self.state.current_topic

    def set_mood(self, mood: str):
        self.state.set_mood(mood)

    def set_topic(self, topic: str):
        self.state.set_topic(topic)
        self.model_mem.update_topic(topic)

    def get_state_summary(self) -> str:
        return self.state.summary()

    def notify_activity(self):
        """Panggil setiap ada aktivitas untuk reset idle_since di state."""
        self.state.idle_since = time.monotonic()

    # ═════════════════════════════════════════════════════════════════════════
    # INTERNAL GENERATION HELPERS
    # ═════════════════════════════════════════════════════════════════════════

    def _run_full_generate(
        self,
        content:  str,
        user_mem: UserMemory,
        username: str,
    ) -> Tuple[List[Dict], str]:
        import main as _main
        try:
            return _main.full_generate(
                content,
                user_mem,
                char_name = self.char_name,
                char_data = self.char_data or _main.CHARACTER,
                username  = username,
            )
        except Exception as e:
            if DEBUG:
                print(f"[AGENT] full_generate error: {e}")
            return self._fallback()

    def _run_generate_from_prompt(self, prompt: str) -> Tuple[List[Dict], str]:
        import main as _main
        try:
            char = self.char_data or _main.CHARACTER
            return _main.generate_from_prompt(
                prompt, char, char_name=self.char_name
            )
        except Exception as e:
            if DEBUG:
                print(f"[AGENT] generate_from_prompt error: {e}")
            return self._fallback()

    def _build_idle_prompt(self) -> str:
        uptime = self.state.uptime_str()
        idle   = self.state.idle_str()
        ctx    = self.state.last_context()
        mood   = self.state.current_mood
        topic  = self.state.current_topic

        prompt = (
            f"[IDLE — sepi {idle} | stream {uptime} | "
            f"mood={mood} | topik={topic}]\n"
        )
        if ctx:
            prompt += f"Konteks agent: {ctx}\n"
        prompt += (
            "Buat reaksi spontan alami sesuai kepribadian: bisa mengeluh sepi, "
            "komentar soal waktu stream, ajak penonton berinteraksi, atau lakukan "
            "sesuatu kreatif. Minimal 2 segmen. Output JSON sesuai format."
        )
        return prompt

    @staticmethod
    def _fallback() -> Tuple[List[Dict], str]:
        # jp hardcoded — fallback tidak melalui _translate_segments pipeline
        return [{"ind": "...", "jp": "えっと…", "anim": "default"}], "default"

    # ═════════════════════════════════════════════════════════════════════════
    # TOOL REGISTRATION
    # ═════════════════════════════════════════════════════════════════════════

    def _register_all_tools(self):
        self._reg_memory()
        self._reg_action()
        self._reg_cognitive()
        self._reg_stream()

    # ── MEMORY ────────────────────────────────────────────────────────────────

    def _reg_memory(self):
        R = self.registry.register

        R(AgentTool("get_user_stats", ToolDomain.MEMORY,
                    "Stats user: romance, VIP, info",
                    self._t_user_stats, {"username": "str"}))

        R(AgentTool("check_romance", ToolDomain.MEMORY,
                    "Level dan status romance user",
                    self._t_romance, {"username": "str"}))

        R(AgentTool("get_user_notes", ToolDomain.MEMORY,
                    "Catatan penting tentang user",
                    self._t_notes, {"username": "str"}))

        R(AgentTool("get_gift_history", ToolDomain.MEMORY,
                    "Riwayat hadiah user (top 3)",
                    self._t_gifts, {"username": "str"}))

        R(AgentTool("get_last_seen", ToolDomain.MEMORY,
                    "Kapan user terakhir chat",
                    self._t_last_seen, {"username": "str"}))

        R(AgentTool("save_user_note", ToolDomain.MEMORY,
                    "Simpan catatan baru tentang user",
                    self._t_save_note, {"username": "str", "note": "str"}))

    # ── ACTION ────────────────────────────────────────────────────────────────

    def _reg_action(self):
        R = self.registry.register

        R(AgentTool("set_expression", ToolDomain.ACTION,
                    "Set ekspresi Live2D karakter",
                    self._t_expression, {"expression": "str"}))

        R(AgentTool("play_motion", ToolDomain.ACTION,
                    "Trigger animasi/motion Live2D",
                    self._t_motion, {"name": "str"}))

        R(AgentTool("set_mood", ToolDomain.ACTION,
                    "Update mood agen (neutral/happy/teasing/sad/annoyed)",
                    self._t_set_mood, {"mood": "str"}))

        R(AgentTool("set_topic", ToolDomain.ACTION,
                    "Update topik pembicaraan aktif ke ModelMemory",
                    self._t_set_topic, {"topic": "str"}))

        R(AgentTool("show_notification", ToolDomain.ACTION,
                    "Tampilkan notifikasi di overlay Live2D",
                    self._t_notification, {"text": "str"}))

    # ── COGNITIVE ─────────────────────────────────────────────────────────────

    def _reg_cognitive(self):
        R = self.registry.register

        R(AgentTool("analyze_sentiment", ToolDomain.COGNITIVE,
                    "Analisis sentimen teks: positif/negatif/netral",
                    self._t_sentiment, {"text": "str"}))

        R(AgentTool("get_time_context", ToolDomain.COGNITIVE,
                    "Waktu lokal, periode hari, dan uptime stream",
                    self._t_time, {}))

        R(AgentTool("get_character_state", ToolDomain.COGNITIVE,
                    "State karakter: topic/role/style dari ModelMemory",
                    self._t_char_state, {}))

    # ── STREAM ────────────────────────────────────────────────────────────────

    def _reg_stream(self):
        R = self.registry.register

        R(AgentTool("get_stream_uptime", ToolDomain.STREAM,
                    "Berapa lama stream sudah berjalan",
                    lambda: self.state.uptime_str(), {}))

        R(AgentTool("get_idle_duration", ToolDomain.STREAM,
                    "Sudah berapa lama tidak ada interaksi",
                    lambda: self.state.idle_str(), {}))

        R(AgentTool("get_tracker_summary", ToolDomain.STREAM,
                    "Statistik sesi: chat/like/gift/follow/share",
                    self._t_tracker, {}))

        R(AgentTool("get_recent_observations", ToolDomain.STREAM,
                    "Hasil observasi agent dari loop sebelumnya",
                    lambda: ("; ".join(self.state.recent_observations[-3:])
                             or "kosong"), {}))

    # ═════════════════════════════════════════════════════════════════════════
    # TOOL IMPLEMENTATIONS
    # ═════════════════════════════════════════════════════════════════════════

    def _resolve_mem(self, username: str) -> Optional[UserMemory]:
        target = (username or "").strip() or self.state.active_user
        if target:
            return self.user_mgr.find_by_username(target)
        return None

    # memory ───────────────────────────────────────────────────────────────────

    def _t_user_stats(self, username: str = "") -> str:
        m = self._resolve_mem(username)
        if not m:
            return f"user '{username}' tidak ditemukan"
        d    = m.data
        info = "; ".join(d.get("info_user", [])[-3:]) or "-"
        return (
            f"user={m.get_display_name()} "
            f"romance={m.romance_points}pts({m.get_romance_level()}) "
            f"VIP={d.get('vip_user', False)} info=[{info}]"
        )

    def _t_romance(self, username: str = "") -> str:
        m = self._resolve_mem(username)
        if not m:
            return "user tidak ditemukan"
        return (
            f"{m.get_display_name()}: {m.romance_points}pts "
            f"level={m.get_romance_level()} "
            f"status={m.get_romance_status() or 'belum_diset'}"
        )

    def _t_notes(self, username: str = "") -> str:
        m = self._resolve_mem(username)
        if not m:
            return "user tidak ditemukan"
        notes = m.data.get("note", [])
        if not notes:
            return f"{m.get_display_name()}: belum ada catatan"
        items = [f"[{n['ts'][:10]}] {n['text']}" for n in notes[-2:]]
        return f"{m.get_display_name()}: {' | '.join(items)}"

    def _t_gifts(self, username: str = "") -> str:
        m = self._resolve_mem(username)
        if not m:
            return "user tidak ditemukan"
        gifts = m.data.get("gift_history", [])
        if not gifts:
            return f"{m.get_display_name()}: belum ada gift"
        items = [f"{g.get('gift','?')}×{g.get('count',1)}" for g in gifts[-3:]]
        return f"{m.get_display_name()} gifts: {', '.join(items)}"

    def _t_last_seen(self, username: str = "") -> str:
        m = self._resolve_mem(username)
        if not m:
            return "user tidak ditemukan"
        ago = m.get_last_chat_ago() if hasattr(m, "get_last_chat_ago") else "-"
        return f"{m.get_display_name()} last_seen={ago}"

    def _t_save_note(self, username: str = "", note: str = "") -> str:
        m = self._resolve_mem(username)
        if not m or not note.strip():
            return "gagal: user atau note kosong"
        m.add_note(note.strip())
        return f"note disimpan untuk {m.get_display_name()}: {note[:50]}"

    # action ───────────────────────────────────────────────────────────────────

    def _t_expression(self, expression: str = "neutral") -> str:
        if self.l2d:
            try:
                self.l2d.set_expression(expression)
                return f"ekspresi → {expression}"
            except Exception as e:
                return f"L2D error: {e}"
        return f"(offline) ekspresi → {expression}"

    def _t_motion(self, name: str = "response") -> str:
        if self.l2d:
            try:
                self.l2d.send_random_motions(name=name)
                return f"motion → {name}"
            except Exception as e:
                return f"L2D error: {e}"
        return f"(offline) motion → {name}"

    def _t_set_mood(self, mood: str = "neutral") -> str:
        valid = {"neutral", "happy", "teasing", "sad", "annoyed", "excited", "tired"}
        m     = mood.lower().strip()
        if m not in valid:
            m = "neutral"
        self.state.set_mood(m)
        return f"mood → {m}"

    def _t_set_topic(self, topic: str = "general") -> str:
        t = topic.strip() or "general"
        self.state.set_topic(t)
        self.model_mem.update_topic(t)
        return f"topic → {t}"

    def _t_notification(self, text: str = "") -> str:
        if not text.strip():
            return "notifikasi kosong, skip"
        if self.l2d:
            try:
                self.l2d.show_notification(text=text)
                return f"notifikasi: {text[:40]}"
            except Exception as e:
                return f"L2D error: {e}"
        return f"(offline) notifikasi: {text[:40]}"

    # cognitive ────────────────────────────────────────────────────────────────

    _POS = {"senang","suka","cinta","bagus","mantap","keren","asik","gajian",
            "hadiah","lucu","haha","wkwk","sayang","seru","good","nice","love"}
    _NEG = {"sedih","marah","kesel","benci","lelah","capek","bosan","jelek",
            "buruk","nyebelin","bete","kecewa","gagal","susah","bad","hate"}

    def _t_sentiment(self, text: str = "") -> str:
        words = set(text.lower().split())
        pos   = len(words & self._POS)
        neg   = len(words & self._NEG)
        result = "positif" if pos > neg else ("negatif" if neg > pos else "netral")
        return f"sentimen={result} (pos={pos} neg={neg})"

    def _t_time(self) -> str:
        now  = datetime.now()
        h    = now.hour
        period = ("dini_hari" if h < 5 else
                  "pagi"      if h < 12 else
                  "siang"     if h < 15 else
                  "sore"      if h < 18 else
                  "malam")
        return (
            f"waktu={period}({now.strftime('%H:%M')}) "
            f"hari={now.strftime('%A')} uptime={self.state.uptime_str()}"
        )

    def _t_char_state(self) -> str:
        return (
            f"topic={self.model_mem.topik} role={self.model_mem.role} "
            f"style={self.model_mem.style} cmd={self.model_mem.command or '-'}"
        )

    # stream ───────────────────────────────────────────────────────────────────

    def _t_tracker(self) -> str:
        if not self.tracker:
            return "tracker tidak tersedia"
        try:
            s = self.tracker.get_summary()
            return (
                f"chat={s.get('total_chats',0)} like={s.get('total_likes',0)} "
                f"gift={s.get('total_gifts',0)} follow={s.get('total_follows',0)} "
                f"share={s.get('total_shares',0)}"
            )
        except Exception as e:
            return f"tracker error: {e}"


# ═════════════════════════════════════════════════════════════════════════════
# 7.  IDLE SCHEDULER
# ═════════════════════════════════════════════════════════════════════════════

class IdleScheduler:
    """
    Scheduler ringan — jika tidak ada aktivitas ≥ idle_threshold detik,
    panggil agent.handle_idle() lalu kirim hasil ke on_response callback.

    Parameters
    ----------
    agent          : StatefulAgent
    idle_threshold : detik sebelum fire (default 600 = 10 menit)
    on_response    : callback(responses, dominant) — biasanya play_segments
    is_busy_fn     : callable() → bool; jika True, tunda firing
                     (misal: pipeline.has_work)
    """

    def __init__(
        self,
        agent:          StatefulAgent,
        idle_threshold: int      = 600,
        on_response:    Callable = None,
        is_busy_fn:     Callable = None,
    ):
        self.agent          = agent
        self.idle_threshold = idle_threshold
        self.on_response    = on_response
        self.is_busy_fn     = is_busy_fn
        self._stop_evt      = threading.Event()
        self._last_activity = time.monotonic()
        self._thread: Optional[threading.Thread] = None

    def start(self):
        """Mulai background thread scheduler."""
        self._stop_evt.clear()
        self._last_activity = time.monotonic()
        self._thread = threading.Thread(
            target=self._loop, daemon=True, name="idle-sched"
        )
        self._thread.start()
        if DEBUG:
            print(f"[IDLE] Scheduler started (threshold={self.idle_threshold}s)")

    def stop(self):
        """Hentikan scheduler (saat live selesai)."""
        self._stop_evt.set()

    def reset(self):
        """
        Panggil setiap kali ada aktivitas apa pun
        (chat, like, gift, share, follow).
        """
        self._last_activity = time.monotonic()
        self.agent.notify_activity()

    def _loop(self):
        while not self._stop_evt.is_set():
            time.sleep(10)

            idle = time.monotonic() - self._last_activity
            if idle < self.idle_threshold:
                continue

            # Tunda jika pipeline masih sibuk
            if self.is_busy_fn and self.is_busy_fn():
                if DEBUG:
                    print("[IDLE] Pipeline busy — tunda idle fire")
                continue

            if DEBUG:
                print(f"[IDLE] Fire setelah {idle:.0f}s idle")

            try:
                responses, dominant = self.agent.handle_idle(
                    reason=f"idle_{int(idle)}s"
                )
                if self.on_response:
                    self.on_response(responses, dominant)
            except Exception as e:
                if DEBUG:
                    print(f"[IDLE] Error saat handle_idle: {e}")
            finally:
                # Reset timer setelah fire — cegah spam idle
                self._last_activity = time.monotonic()


# ═════════════════════════════════════════════════════════════════════════════
# 8.  SELF TEST  (python agent_core.py)
# ═════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import sys, os
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

    from memory import UserMemoryManager

    print("=" * 60)
    print("  AGENT CORE — Self Test (tool registry, no LLM needed)")
    print("=" * 60)

    mgr   = UserMemoryManager(storage_dir="memory")
    mem   = ModelMemory("test", storage_dir=".")
    agent = StatefulAgent(mgr, mem, None, None, char_name="TestChar")

    print(f"\n[STATE]  {agent.get_state_summary()}")
    print(f"\n[SCHEMA]\n{agent.registry.schema_prompt()}")

    tests = [
        ("get_time_context",  {}),
        ("analyze_sentiment", {"text": "aku senang banget gajian hari ini!"}),
        ("set_mood",          {"mood": "happy"}),
        ("set_topic",         {"topic": "gajian"}),
        ("get_stream_uptime", {}),
        ("get_idle_duration", {}),
    ]
    print("\n[TOOL TESTS]")
    for name, args in tests:
        res = agent.registry.call(name, args)
        print(f"  {name}({args}) → {res}")

    print(f"\n[STATE AFTER] {agent.get_state_summary()}")
    print("\n✅ Self-test selesai. Untuk ReAct loop penuh, pastikan LM Studio aktif.")
