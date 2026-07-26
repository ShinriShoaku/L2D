"""
mcp_tools.py — Tool calling layer + Task Router executor.

"""

import json
import os
import re
import threading
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

from memory import UserMemory, UserMemoryManager

try:
    from cloud_tools import dispatch_cloud as _dispatch_cloud
    _CLOUD_AVAILABLE = True
except ImportError:
    _CLOUD_AVAILABLE = False
    def _dispatch_cloud(f, args, user_id, is_admin=False): return "[cloud] tidak tersedia"

try:
    import requests  # only needed for "http"-type custom MCP tools
except ImportError:
    requests = None


# ═════════════════════════════════════════════════════════════════════════════
# CUSTOM MCP TOOLS  (user-defined via Settings UI → mcp_custom_tools.json)
#
# Format file: {"tools": [ {...tool def...}, ... ]}
# Setiap tool minimal punya "name". Tipe yang didukung sekarang: "http" —
# memanggil endpoint eksternal, dengan {param} di-substitusi dari args
# router-call. Tipe lain bisa ditambah belakangan (mis. "python", "static").
# ═════════════════════════════════════════════════════════════════════════════

_CUSTOM_TOOLS_FILE = "mcp_custom_tools.json"
_custom_tools_cache: Optional[List[Dict]] = None


def _custom_tools_path() -> str:
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), _CUSTOM_TOOLS_FILE)


def load_custom_tools(force_reload: bool = False) -> List[Dict]:
    """Load tools defined via Settings UI. Cached after first call —
    pass force_reload=True to re-read from disk (e.g. after editing tools
    in Settings UI while this process is still running)."""
    global _custom_tools_cache
    if _custom_tools_cache is not None and not force_reload:
        return _custom_tools_cache
    path = _custom_tools_path()
    tools: List[Dict] = []
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            raw = data.get("tools") if isinstance(data, dict) else data
            if isinstance(raw, list):
                tools = [t for t in raw if isinstance(t, dict) and t.get("name")]
        except Exception:
            tools = []
    _custom_tools_cache = tools
    return tools


def _find_custom_tool(fname: str) -> Optional[Dict]:
    """
    Cari definisi eksekusi (url/method/headers/dst) untuk custom tool `fname`
    di mcp_custom_tools.json — TAPI hanya dianggap aktif kalau tool itu sudah
    berhasil di-compile (punya .bin di folder tools/, lihat tool_compiler.py).

    Tidak ada lagi toggle enable/disable manual: "sudah di-compile" itulah
    yang sekarang menentukan aktif/tidaknya sebuah custom tool saat runtime.
    """
    try:
        from tool_compiler import load_compiled_tools
        compiled = load_compiled_tools()
    except Exception:
        compiled = {}

    for t in load_custom_tools():
        if t.get("name") == fname:
            if fname not in compiled:
                # Sudah didefinisikan di JSON tapi belum (atau tidak lagi)
                # di-compile → belum aktif.
                return None
            return t
    return None


def _fill_template(value, args: Dict):
    """Recursively substitute {param} placeholders in strings with args."""
    if isinstance(value, str):
        try:
            return value.format(**{k: ("" if v is None else v) for k, v in args.items()})
        except Exception:
            return value
    if isinstance(value, dict):
        return {k: _fill_template(v, args) for k, v in value.items()}
    if isinstance(value, list):
        return [_fill_template(v, args) for v in value]
    return value


def _run_custom_tool(tool: Dict, args: Dict) -> str:
    ttype = tool.get("type", "http")
    if ttype != "http":
        return f"(unsupported custom tool type '{ttype}')"
    if requests is None:
        return "(custom http tool unavailable: 'requests' package not installed)"

    method  = (tool.get("method") or "GET").upper()
    url     = _fill_template(tool.get("url", ""), args)
    headers = _fill_template(tool.get("headers") or {}, args)
    body    = _fill_template(tool.get("body"), args)
    try:
        resp = requests.request(
            method, url, headers=headers,
            json=body if isinstance(body, (dict, list)) else None,
            data=body if isinstance(body, str) else None,
            timeout=10,
        )
        text = resp.text.strip()
        return text[:500] if text else f"(status {resp.status_code}, no body)"
    except Exception as e:
        return f"(request failed: {e})"


# ═════════════════════════════════════════════════════════════════════════════
# ROUTER SYSTEM PROMPT
# Ini adalah versi ringkas untuk referensi; _TASK_SYSTEM di main.py adalah
# yang dipakai untuk model call (format sesuai training dataset).
# ═════════════════════════════════════════════════════════════════════════════

# Admin user ID — sinkron dengan ADMIN_USER_ID di main.py
_ADMIN_USER_ID = "shinri_shoaku"


# ═════════════════════════════════════════════════════════════════════════════
# ROUTER EXECUTOR
# ═════════════════════════════════════════════════════════════════════════════

class RouterExecutor:
    """
    Eksekusi router function calls dari [TASK] model.
    Fungsi yang didukung (sesuai training dataset):
      User   : gum, gus, guls, gur, gmu, gugh, guch, uum, bu
      Chat   : grc, gts, sc, gcs
      State  : gsi, gsd, gvc, gav, gsm, gca
      Events : gre, gpg, gns, gms, gri, gpm
      Game   : gcg, ggs, gac
      Social : gfc, gtg, gnf, gru, gug
      Self   : gmm, gme, gcm, grr, gmos
      Meta   : gtc, gpe, gan
      NEW v8 : sn (set_nickname), rs (romance_status), cmd (set_command)
    """

    def __init__(
        self,
        user_id:       str,
        username:      str,
        user_mgr:      "UserMemoryManager",
        chat_hist,                           # ChatHistory instance
        model_mem,                           # ModelMemory instance
        last_response: str = "",
        is_admin:      bool = False,
    ):
        self.user_id       = user_id
        self.username      = username
        self.user_mgr      = user_mgr
        self.chat_hist     = chat_hist
        self.model_mem     = model_mem
        self.last_response = last_response
        self.is_admin      = is_admin

    # ── Public API ────────────────────────────────────────────────────────────

    def execute_all(self, calls: List[Dict]) -> str:
        """
        Eksekusi semua router calls, kembalikan string konteks [ROUTER].
        Return "" jika tidak ada calls.
        """
        if not calls:
            return ""
        lines = []
        for call in calls:
            fname = call.get("f", "")
            args  = call.get("a", {}) or {}
            if not fname:
                continue
            try:
                result = self._dispatch(fname, args)
                if result:
                    lines.append(f"{fname}: {result}")
            except Exception as e:
                lines.append(f"{fname}: error ({e})")
        if not lines:
            return ""
        return "[ROUTER]\n" + "\n".join(lines)

    # ── Dispatcher ────────────────────────────────────────────────────────────

    def _dispatch(self, fname: str, args: Dict) -> str:
        # ── User functions ────────────────────────────────────────────────────
        if fname == "gum":   return self._get_user_memory(args)
        if fname == "gus":   return self._get_user_stats(args)
        if fname == "guls":  return self._get_user_last_seen(args)
        if fname == "gur":   return self._get_user_relationship(args)
        if fname == "gmu":   return self._get_multiple_users(args)
        if fname == "gugh":  return self._get_user_gift_history(args)
        if fname == "guch":  return self._get_user_chat_history(args)
        if fname == "uum":   return self._update_user_memory(args)
        if fname == "bu":    return self._banned_user(args)

        # ── Chat functions ────────────────────────────────────────────────────
        if fname == "grc":   return self._get_recent_chat(args)
        if fname == "gts":   return self._get_topic_history(args)
        if fname == "sc":    return self._search_chat(args)
        if fname == "gcs":   return self._get_chat_since(args)

        # ── State functions ───────────────────────────────────────────────────
        if fname == "gsi":   return self._get_stream_info()
        if fname == "gsd":   return "N/A"
        if fname == "gvc":   return "N/A"
        if fname == "gav":   return "N/A"
        if fname == "gsm":   return self._get_stream_mood()
        if fname == "gca":   return "N/A"

        # ── Events ───────────────────────────────────────────────────────────
        if fname in ("gre", "gpg", "gns", "gms", "gri", "gpm"):
            return "N/A"

        # ── Game ─────────────────────────────────────────────────────────────
        if fname in ("gcg", "ggs"):  return "N/A"
        if fname == "gac":           return self._get_activity_context()

        # ── Social ───────────────────────────────────────────────────────────
        if fname in ("gfc", "gtg", "gnf", "gru", "gug"):
            return "N/A"

        # ── Self ─────────────────────────────────────────────────────────────
        if fname == "gmm":   return self._get_alfa_mood()
        if fname == "gme":   return "normal"
        if fname == "gcm":   return self._get_character_mode()
        if fname == "grr":   return self._get_recent_responses()
        if fname == "gmos":  return self._get_alfa_memory_of_session()

        # ── Meta ─────────────────────────────────────────────────────────────
        if fname == "gtc":   return self._get_time_context()
        if fname in ("gpe", "gan"):  return "N/A"

        # ── NEW v8: Nickname, Romance Status, Command ─────────────────────────
        if fname == "sn":    return self._set_nickname(args)
        if fname == "rs":    return self._romance_status(args)
        if fname == "cmd":   return self._set_command(args)

        # ── Cloud store (cs_*) ────────────────────────────────────────────────
        if fname.startswith("cs_"):
            is_admin = getattr(self, "is_admin", False)
            return _dispatch_cloud(fname, args, self.user_id, is_admin)

        # ── Custom MCP tools (user-defined via Settings UI) ──────────────────
        custom = _find_custom_tool(fname)
        if custom is not None:
            return _run_custom_tool(custom, args)

        return f"unknown function '{fname}'"

    # ── User helpers ──────────────────────────────────────────────────────────

    def _resolve_user(self, args: Dict) -> Optional["UserMemory"]:
        """Resolve target user dari args, default ke current user."""
        uid = args.get("id", "").strip()
        if not uid or uid.lower() in (self.username.lower(), self.user_id.lower()):
            return self.user_mgr.get(self.user_id, self.username)
        mem = self.user_mgr.find_by_username(uid)
        if mem:
            return mem
        return self.user_mgr.get(uid, uid)

    def _get_user_memory(self, args: Dict) -> str:
        mem = self._resolve_user(args)
        if not mem:
            return "(not found)"
        d     = mem.data
        name  = mem.get_display_name()
        info  = "; ".join(d.get("info_user", [])[-5:]) or "-"
        notes = "; ".join(n["text"] for n in d.get("note", [])[-2:]) or "-"
        return (
            f"user={name} | romance={d.get('romance_status','-')} "
            f"({mem.romance_points}pts/{mem.get_romance_level()}) | "
            f"vip={d.get('vip_user',False)} | info={info} | notes={notes}"
        )

    def _get_user_stats(self, args: Dict) -> str:
        mem = self._resolve_user(args)
        if not mem:
            return "(not found)"
        gifts = mem.get_gift_summary() or "-"
        return (
            f"romance_pts={mem.romance_points} level={mem.get_romance_level()} "
            f"gifts={gifts} last_seen={mem.get_last_chat_ago()}"
        )

    def _get_user_last_seen(self, args: Dict) -> str:
        mem = self._resolve_user(args)
        if not mem:
            return "(not found)"
        return mem.get_last_chat_ago()

    def _get_user_relationship(self, args: Dict) -> str:
        mem = self._resolve_user(args)
        if not mem:
            return "(not found)"
        status = mem.get_romance_status() or "belum_diset"
        return (
            f"status={status} pts={mem.romance_points} "
            f"level={mem.get_romance_level()}"
        )

    def _get_multiple_users(self, args: Dict) -> str:
        ids = args.get("ids", [])
        if not ids:
            return "(no ids)"
        parts = []
        for uid in ids[:5]:
            mem = self.user_mgr.find_by_username(uid) or self.user_mgr.get(uid, uid)
            if mem:
                parts.append(f"{mem.get_display_name()}={mem.get_romance_level()}")
        return " | ".join(parts) or "(none found)"

    def _get_user_gift_history(self, args: Dict) -> str:
        mem = self._resolve_user(args)
        if not mem:
            return "(not found)"
        summary = mem.get_gift_summary()
        return summary if summary else "(belum ada gift)"

    def _get_user_chat_history(self, args: Dict) -> str:
        if not self.chat_hist:
            return "(no chat history)"
        n = int(args.get("n", 3))
        return self.chat_hist.get_recent_summary(n)

    def _update_user_memory(self, args: Dict) -> str:
        """
        Update user memory.
        Format 1: m=raw memory string (e.g. "suka kopi", "nickname:Shin")
        Format 2: k=key, v=value (e.g. k="romance_status", v="teman")
        """
        mem = self._resolve_user(args)
        if not mem:
            return "error: user not found"
        results = []

        # Guard: tolak nilai yang merupakan teks placeholder/template literal
        # dari prompt (mis. model copy-paste contoh format alih-alih isi asli).
        _PLACEHOLDER_VALUES = {
            "ringkasan singkat", "ringkasan", "info", "data",
            "teks", "value", "nilai", "...", "n/a", "none",
        }

        # Format 1: m=raw
        m = args.get("m", "").strip()
        if m and m.lower() not in _PLACEHOLDER_VALUES:
            lower_m = m.lower()
            if lower_m.startswith("nickname:"):
                nick = m.split(":", 1)[1].strip()
                # FIX: replace username field directly, not info_user
                mem.update_username(nick)
                results.append(f"nickname→{nick}")
            elif lower_m.startswith("romance_status:"):
                status = m.split(":", 1)[1].strip()
                mem.set_romance_status(status)
                results.append(f"romance_status→{status}")
            elif lower_m.startswith("note:"):
                note = m.split(":", 1)[1].strip()
                mem.add_note(note)
                results.append(f"note+{note[:30]}")
            else:
                mem.add_info(m)
                results.append(f"info+{m[:30]}")

        # Format 2: k=key, v=value
        k = args.get("k", "").strip()
        v = args.get("v", "").strip()
        if k and v:
            if k == "nickname":
                # FIX: replace username field directly
                mem.update_username(v)
                results.append(f"nickname→{v}")
            elif k == "romance_status":
                mem.set_romance_status(v)
                results.append(f"romance_status→{v}")
            elif k == "romance_points":
                try:
                    mem.add_romance_points(int(v))
                    results.append(f"romance_points+{v}")
                except ValueError:
                    pass
            elif k == "info":
                mem.add_info(v)
                results.append(f"info+{v[:30]}")
            elif k == "note":
                mem.add_note(v)
                results.append(f"note+{v[:30]}")
            elif k == "vip":
                flag = v.lower() in ("true", "1", "yes")
                mem.set_vip(flag)
                results.append(f"vip→{flag}")

        return ", ".join(results) if results else "no update"

    def _banned_user(self, args: Dict) -> str:
        # Validasi admin di level Python — jangan andalkan prompt LLM saja.
        if self.user_id.lower() != _ADMIN_USER_ID.lower():
            return "error: unauthorized — hanya admin yang bisa menggunakan fungsi ini"
        uid    = args.get("id", "")
        reason = args.get("r", "")
        return f"banned: {uid} reason={reason or 'unspecified'} (logged)"

    # ── Chat helpers ──────────────────────────────────────────────────────────

    def _get_recent_chat(self, args: Dict) -> str:
        if not self.chat_hist:
            return "(no chat history)"
        n = int(args.get("n", 5))
        return self.chat_hist.get_recent_summary(n)

    def _get_topic_history(self, args: Dict) -> str:
        if not self.model_mem:
            return "(no model memory)"
        return f"topik={self.model_mem.topik}"

    def _search_chat(self, args: Dict) -> str:
        if not self.chat_hist:
            return "(no chat history)"
        q = args.get("q", "").strip().lower()
        if not q:
            return "(no query)"
        msgs = self.chat_hist.get_messages()
        hits = [
            m["content"][:80]
            for m in msgs
            if q in (m.get("content") or "").lower()
        ]
        return " | ".join(hits[-3:]) if hits else f"'{q}' tidak ditemukan"

    def _get_chat_since(self, args: Dict) -> str:
        if not self.chat_hist:
            return "(no chat history)"
        return self.chat_hist.get_recent_summary(3)

    # ── State helpers ─────────────────────────────────────────────────────────

    def _get_stream_info(self) -> str:
        if not self.model_mem:
            return "mode=local"
        return (
            f"topik={self.model_mem.topik} "
            f"role={self.model_mem.role} "
            f"style={self.model_mem.style}"
        )

    def _get_stream_mood(self) -> str:
        if not self.model_mem:
            return "normal"
        return self.model_mem.style or "normal"

    # ── Game / activity helpers ───────────────────────────────────────────────

    def _get_activity_context(self) -> str:
        if not self.model_mem:
            return "N/A"
        return self.model_mem.role or "default"

    # ── Self helpers ──────────────────────────────────────────────────────────

    def _get_alfa_mood(self) -> str:
        if not self.model_mem:
            return "normal"
        return self.model_mem.style or "normal"

    def _get_character_mode(self) -> str:
        if not self.model_mem:
            return "default"
        return self.model_mem.role or "default"

    def _get_recent_responses(self) -> str:
        if not self.last_response:
            return "(belum ada respons)"
        return self.last_response[:120]

    def _get_alfa_memory_of_session(self) -> str:
        if not self.model_mem:
            return "N/A"
        return (
            f"topik={self.model_mem.topik} "
            f"role={self.model_mem.role} "
            f"style={self.model_mem.style} "
            f"command={self.model_mem.command or '(none)'}"
        )

    # ── Meta helpers ──────────────────────────────────────────────────────────

    def _get_time_context(self) -> str:
        now = datetime.now()
        days   = ["Senin","Selasa","Rabu","Kamis","Jumat","Sabtu","Minggu"]
        months = ["Januari","Februari","Maret","April","Mei","Juni",
                  "Juli","Agustus","September","Oktober","November","Desember"]
        day_name = days[now.weekday()]
        month    = months[now.month - 1]
        hour_str = now.strftime("%H:%M")
        hour     = now.hour
        if hour < 5:    waktu = "dini hari"
        elif hour < 12: waktu = "pagi"
        elif hour < 15: waktu = "siang"
        elif hour < 18: waktu = "sore"
        else:           waktu = "malam"
        return (
            f"{day_name}, {now.day} {month} {now.year} | "
            f"jam {hour_str} ({waktu})"
        )

    # ── NEW v8: Nickname, Romance Status, Command handlers ────────────────────

    def _set_nickname(self, args: Dict) -> str:
        """
        sn: set_nickname
        args: target="self"|user_id, name="nickname"
        """
        target = args.get("target", "self").strip()
        name   = args.get("name", "").strip()
        if not name:
            return "error: name kosong"

        # Resolve target user
        if target.lower() == "self":
            mem = self.user_mgr.get(self.user_id, self.username)
        else:
            mem = (self.user_mgr.find_by_username(target)
                   or self.user_mgr.get(target, target))

        if not mem:
            return f"error: user '{target}' tidak ditemukan"

        # FIX: replace username field directly
        mem.update_username(name)
        return f"nickname set: {mem.get_display_name()} → '{name}'"

    def _romance_status(self, args: Dict) -> str:
        """
        rs: romance_status
        args: id=user_id (optional; kosong = current user)
        """
        uid = args.get("id", "").strip()
        if not uid:
            mem = self.user_mgr.get(self.user_id, self.username)
        else:
            mem = (self.user_mgr.find_by_username(uid)
                   or self.user_mgr.get(uid, uid))

        if not mem:
            return "(not found)"

        status = mem.get_romance_status() or "belum_diset"
        return (
            f"{mem.get_display_name()}: status={status} "
            f"pts={mem.romance_points} level={mem.get_romance_level()}"
        )

    def _set_command(self, args: Dict) -> str:
        """
        cmd: command
        args: c=command_string
        Menyimpan command override ke ModelMemory.
        """
        command = args.get("c", "").strip()
        if not self.model_mem:
            return "error: model_mem tidak tersedia"

        if not command:
            # Reset command
            self.model_mem.command = ""
            self.model_mem.save()
            return "command reset"

        # Bersihkan prefix "Nama: " jika model salah menyalin seluruh baris
        # chat (mis. "Shinri: itu akhiran nyan di hapus saja") alih-alih
        # cuma instruksinya saja.
        cleaned = re.sub(r'^[^:]{1,40}:\s*', '', command).strip()
        if cleaned:
            command = cleaned

        self.model_mem.command = command
        self.model_mem.save()
        return f"command set: '{command}'"


# ═════════════════════════════════════════════════════════════════════════════
# PARSE ROUTER OUTPUT
# ═════════════════════════════════════════════════════════════════════════════

def parse_router_output(raw: str) -> List[Dict]:
    """
    Parse output router model.
    Bisa berupa: null | single call {f,a} | array [{f,a},...]
    Return list of calls (kosong jika null/invalid).
    """
    import re
    if not raw:
        return []
    stripped = raw.strip()
    if stripped.lower() == "null":
        return []

    # Hapus markdown fence kalau ada
    stripped = re.sub(r"```(?:json)?\s*", "", stripped, flags=re.IGNORECASE)
    stripped = re.sub(r"```", "", stripped).strip()

    # Coba parse langsung
    try:
        data = json.loads(stripped)
        if isinstance(data, list):
            return [c for c in data if isinstance(c, dict) and "f" in c]
        if isinstance(data, dict) and "f" in data:
            return [data]
        return []
    except json.JSONDecodeError:
        pass

    # Coba temukan array dalam teks
    start = stripped.find("[")
    if start != -1:
        depth = 0; in_str = False; escape = False
        for i, ch in enumerate(stripped[start:], start):
            if escape:    escape = False; continue
            if ch == "\\" and in_str: escape = True; continue
            if ch == '"': in_str = not in_str; continue
            if in_str:    continue
            if ch == "[": depth += 1
            elif ch == "]":
                depth -= 1
                if depth == 0:
                    try:
                        data = json.loads(stripped[start:i+1])
                        if isinstance(data, list):
                            return [c for c in data if isinstance(c, dict) and "f" in c]
                    except Exception:
                        break

    # Coba temukan single object
    start = stripped.find("{")
    if start != -1:
        depth = 0; in_str = False; escape = False
        for i, ch in enumerate(stripped[start:], start):
            if escape:    escape = False; continue
            if ch == "\\" and in_str: escape = True; continue
            if ch == '"': in_str = not in_str; continue
            if in_str:    continue
            if ch == "{": depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    try:
                        data = json.loads(stripped[start:i+1])
                        if isinstance(data, dict) and "f" in data:
                            return [data]
                    except Exception:
                        break

    return []


# ═════════════════════════════════════════════════════════════════════════════
# TOOL EXECUTOR (legacy — untuk backward compat field "tools" di output lama)
# ═════════════════════════════════════════════════════════════════════════════

VALID_TOOL_NAMES = {
    "set_username",
    "set_nickname",
    "get_user_info",
    "add_info",
    "add_note",
    "set_romance_status",
    "add_romance_points",
    "set_vip",
    "get_username_by_id",
}


class ToolExecutor:
    """Legacy executor — menjalankan tool calls dari field 'tools' di output model lama."""

    def __init__(self, user_mem: UserMemory, user_mgr: UserMemoryManager):
        self.user_mem = user_mem
        self.user_mgr = user_mgr

    def execute(self, tool_calls: List[Dict]) -> List[Dict]:
        results = []
        for tc in tool_calls:
            name = tc.get("name", "")
            args = tc.get("arguments", {})
            if name not in VALID_TOOL_NAMES:
                continue
            try:
                result = self._dispatch(name, args)
                results.append({"tool": name, "status": "success", "result": result})
            except Exception as e:
                results.append({"tool": name, "status": "error", "error": str(e)})
        return results

    def _dispatch(self, name: str, args: Dict) -> str:
        if name == "set_username":
            new_name = args.get("username", "").strip()
            if not new_name: raise ValueError("username kosong")
            self.user_mem.update_username(new_name)
            return f"username → '{new_name}'"

        if name == "set_nickname":
            nick = args.get("nickname", "").strip()
            if not nick: raise ValueError("nickname kosong")
            # FIX: replace username field directly
            self.user_mem.update_username(nick)
            return f"nickname → '{nick}'"

        if name == "get_user_info":
            d = self.user_mem.data
            return " | ".join([
                f"username: {d.get('username', '-')}",
                f"nickname: {self.user_mem.get_display_name()}",
                f"romance: {d.get('romance_status', '-')} ({d.get('romance_points', 0)} pts)",
                f"vip: {d.get('vip_user', False)}",
                f"info: {'; '.join(d.get('info_user', [])[-5:]) or '-'}",
            ])

        if name == "get_username_by_id":
            uid = args.get("user_id", "").strip()
            for cached_id, mem in getattr(self.user_mgr, "_cache", {}).items():
                if cached_id == uid:
                    return f"username: {mem.username}, nickname: {mem.get_display_name()}"
            return f"user_id '{uid}' tidak ditemukan"

        if name == "add_info":
            info = args.get("info", "").strip()
            if not info: raise ValueError("info kosong")
            self.user_mem.add_info(info)
            return f"info + '{info}'"

        if name == "add_note":
            note = args.get("note", "").strip()
            if not note: raise ValueError("note kosong")
            self.user_mem.add_note(note)
            return f"note + '{note}'"

        if name == "set_romance_status":
            status = args.get("status", "").strip()
            if not status: raise ValueError("status kosong")
            self.user_mem.set_romance_status(status)
            return f"romance_status → '{status}'"

        if name == "add_romance_points":
            delta = int(args.get("delta", 0))
            self.user_mem.add_romance_points(delta)
            return f"romance_points {'+' if delta >= 0 else ''}{delta} → {self.user_mem.romance_points}"

        if name == "set_vip":
            flag = args.get("vip", False)
            if isinstance(flag, str):
                flag = flag.lower() in ("true", "1", "yes")
            self.user_mem.set_vip(bool(flag))
            return f"vip → {bool(flag)}"

        raise ValueError(f"tool '{name}' tidak dikenal")


# ═════════════════════════════════════════════════════════════════════════════
# HELPERS
# ═════════════════════════════════════════════════════════════════════════════

def extract_tools_from_output(data: Dict) -> List[Dict]:
    """Ambil tools dari output generator (backward compat). Field 'tools' opsional."""
    raw_tools = data.get("tools", [])
    if not isinstance(raw_tools, list):
        return []
    valid = []
    for tc in raw_tools:
        if not isinstance(tc, dict):
            continue
        name = tc.get("name", "")
        args = tc.get("arguments", {})
        if name not in VALID_TOOL_NAMES:
            continue
        if not isinstance(args, dict):
            args = {}
        valid.append({"name": name, "arguments": args})
    return valid


def format_tool_results(results: List[Dict]) -> str:
    """Format hasil tool untuk dimasukkan ke konteks."""
    if not results:
        return ""
    lines = ["#TOOL_RESULTS"]
    for r in results:
        if r["status"] == "success":
            lines.append(f"- {r['tool']}: {r['result']}")
        else:
            lines.append(f"- {r['tool']}: ERROR — {r.get('error', 'unknown')}")
    return "\n".join(lines)


def build_tool_instructions(
    user_mem: "UserMemory",
    char_name: str = "karakter",
) -> str:
    """Legacy — dipertahankan untuk backward compat."""
    display_name = user_mem.get_display_name()
    romance_pts  = user_mem.romance_points
    info_list    = user_mem.data.get("info_user", [])
    filtered     = [x for x in info_list if not str(x).lower().startswith("nickname:")]
    info_str     = "; ".join(filtered[-3:]) if filtered else "(kosong)"
    return f"[USER: {display_name} | romance: {romance_pts}pts | info: {info_str}]"