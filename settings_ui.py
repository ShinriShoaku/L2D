#!/usr/bin/env python3
"""
settings_ui.py — Desktop Settings UI untuk Live System.
Jalankan:   python settings_ui.py
Import:     from settings_ui import open_settings; open_settings()
            from settings_ui import open_settings_async; open_settings_async()

Mengedit langsung:
  config.py      — endpoints, routing, model/memory tuning
  liveServer.py  — live server / spam / queue settings
  liveDesktop.py — TTS settings
"""

import ast, json, os, re, sys, threading, tkinter as tk
from tkinter import ttk, messagebox, filedialog
from typing import Any, Dict, List

ROOT = os.path.dirname(os.path.abspath(__file__))
def _fp(n): return os.path.join(ROOT, n)

# ══════════════════════════════════════════════════════════════════
# THEME  — persis dari screenshot
# ══════════════════════════════════════════════════════════════════
C = {
    # backgrounds
    "win":      "#0b0c0f",   # window chrome (outer rounded frame)
    "sidebar":  "#0f1015",   # sidebar bg
    "main":     "#131418",   # main content bg
    "card":     "#1a1b21",   # card bg
    "field":    "#0d0e12",   # input field bg
    "btn":      "#1e1f27",   # normal button bg
    "btn_h":    "#282930",   # normal button hover
    "sel":      "#1e2060",   # sidebar active bg
    # borders
    "bdr":      "#242530",   # card/field border
    "bdr2":     "#2e2f3e",   # subtle border
    # text
    "fg":       "#e2e4f0",   # primary text
    "fg2":      "#9698aa",   # secondary text
    "fg3":      "#4e505f",   # muted / placeholder
    "fg_nav":   "#7880a0",   # nav inactive
    "fg_sel":   "#c0c4ff",   # nav active
    "fg_grp":   "#3a3c4e",   # nav group label
    # accent
    "acc":      "#5b5ef5",   # purple accent (save btn, add ep btn)
    "acc_h":    "#4a4dd4",   # accent hover
    "green":    "#3ecf8e",   # local badge text
    "green_bg": "#0d2318",   # local badge bg
    "blue":     "#5baaf5",   # online badge text
    "blue_bg":  "#0d1e35",   # online badge bg
    "amber":    "#f0a03a",   # unsaved warning
    "red":      "#e05555",   # danger/delete
    "red_bg":   "#2a1212",   # danger hover bg
    # fonts
    "f_ui":  ("Segoe UI", 11),
    "f_ui9": ("Segoe UI", 9),
    "f_ui10":("Segoe UI", 10),
    "f_b":   ("Segoe UI", 11, "bold"),
    "f_h":   ("Segoe UI", 13, "bold"),
    "f_m":   ("Consolas", 11),
    "f_m10": ("Consolas", 10),
}

# ══════════════════════════════════════════════════════════════════
# FILE HELPERS
# ══════════════════════════════════════════════════════════════════
def read_file(p):
    try:
        with open(p, encoding="utf-8") as f: return f.read()
    except FileNotFoundError: return ""

def write_file(p, s):
    with open(p, "w", encoding="utf-8") as f: f.write(s)

def read_var(src, name, default=None):
    m = re.search(r'^' + re.escape(name) + r'\s*=\s*(.+)$', src, re.MULTILINE)
    if not m: return default
    try:    return ast.literal_eval(m.group(1).strip())
    except: return m.group(1).strip().strip('"\'')

def is_literal_var(src, name):
    """True if `name = <value>` is a plain Python literal (str/int/bool/...).
    False if it's a non-literal expression, e.g. AUDIO_PATH = os.path.join(...).
    Such expressions must be written back as raw code, never quoted as a string."""
    m = re.search(r'^' + re.escape(name) + r'\s*=\s*(.+)$', src, re.MULTILINE)
    if not m: return True
    try:
        ast.literal_eval(m.group(1).strip())
        return True
    except Exception:
        return False

def patch_var(src, name, val, raw=False):
    if raw:
        rv = str(val)
    else:
        rv = ("True" if val else "False") if isinstance(val, bool) else \
             (f'"{val}"' if isinstance(val, str) else str(val))
    pat = re.compile(r'^(' + re.escape(name) + r'\s*=\s*)(.+)$', re.MULTILINE)
    new, n = pat.subn(lambda m: m.group(1) + rv, src, count=1)
    return new if n else src.rstrip() + f"\n{name} = {rv}\n"

def patch_dict_key(src, key, val):
    rv = ("True" if val else "False") if isinstance(val, bool) else \
         (f'"{val}"' if isinstance(val, str) else str(val))
    return re.sub(r'("' + re.escape(key) + r'"\s*:\s*)([^,\n\}]+)',
                  lambda m: m.group(1) + rv, src, count=1)

def _find_dict_block(src, name):
    """Locate the full `name = { ... }` assignment in src by counting brace
    depth (ignoring braces inside quoted strings), instead of relying on a
    fragile 'closing brace must be alone on its own line' regex. Returns
    (start, end) char offsets spanning 'name = { ... }', or None if `name`
    isn't assigned at the start of a line, or the braces are unbalanced."""
    m = re.search(r'^' + re.escape(name) + r'\s*=\s*\{', src, re.MULTILINE)
    if not m:
        return None
    i, n = m.end() - 1, len(src)   # i = index of the opening '{'
    depth, in_str, escape = 0, None, False
    while i < n:
        ch = src[i]
        if in_str:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == in_str:
                in_str = None
        elif ch in ("'", '"'):
            in_str = ch
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return (m.start(), i + 1)
        i += 1
    return None  # unbalanced braces — malformed file, bail out safely

def read_endpoints(src):
    span = _find_dict_block(src, "ENDPOINTS")
    if not span: return {}
    try:    return ast.literal_eval(src[span[0]:span[1]].split("=", 1)[1].strip())
    except: return {}

def read_routing(src):
    span = _find_dict_block(src, "CALL_ROUTING")
    if not span: return {}
    try:    return ast.literal_eval(src[span[0]:span[1]].split("=", 1)[1].strip())
    except: return {}

def write_endpoints_block(src, eps):
    lines = ["ENDPOINTS = {"]
    for k, ep in eps.items():
        lines += [f'    "{k}": {{',
                  f'        "url":     "{ep.get("url","")}",',
                  f'        "api_key": "{ep.get("api_key","")}",',
                  f'        "model":   "{ep.get("model","")}",',
                  f'        "disable_thinking": {bool(ep.get("disable_thinking", False))},',
                  "    },"]
    lines.append("}")
    blk = "\n".join(lines)
    span = _find_dict_block(src, "ENDPOINTS")
    if span: return src[:span[0]] + blk + src[span[1]:]
    return src.rstrip() + "\n\n" + blk + "\n"

def write_routing_block(src, rt):
    lines = ["CALL_ROUTING = {"]
    for k, v in rt.items(): lines.append(f'    "{k}": "{v}",')
    lines.append("}")
    blk = "\n".join(lines)
    span = _find_dict_block(src, "CALL_ROUTING")
    if span: return src[:span[0]] + blk + src[span[1]:]
    return src.rstrip() + "\n\n" + blk + "\n"


# ══════════════════════════════════════════════════════════════════
# PASS DEFINITIONS
# ══════════════════════════════════════════════════════════════════
PASS_GROUPS = [
    ("Agent & routing", [
        ("react",            "ReAct loop — tool routing utama"),
        ("category",         "Pass 1 — klasifikasi kategori"),
        ("subintent_data",   "Pass 2a — sub-intent data_user"),
        ("subintent_stream", "Pass 2b — sub-intent stream_state"),
        ("tool_select",      "Pass 3 — pilih tool + argumen"),
    ]),
    ("Pre-soul", [
        ("data_summary",   "Pass 4 — ringkas data dari tool"),
        ("soul_think",     "Pass 5 — grounding intent/tone"),
        ("soul_persona",   "Pass 6 — persona lock"),
        ("cmd_interpret",  "Pass A — gaya bicara → directive"),
        ("identity_merge", "Pass B — gabung directive + context"),
    ]),
    ("Soul generate", [
        ("soul",          "Pass C — generate dialog utama"),
        ("soul_validate", "Pass 8 — validasi output soul"),
        ("opening",       "generate_from_prompt — opening/closing/idle"),
    ]),
    ("Post-processing", [
        ("anim",  "Pass 5 — tentukan animasi karakter"),
        ("trans", "Pass 6 — terjemahkan ID → JP per kalimat"),
    ]),
]
ALL_PASSES = [p for _, g in PASS_GROUPS for p, _ in g]

_DEFAULT_EP = {
    "local_lm_studio": {"url":"http://localhost:1234/v1","api_key":"lm-studio","model":"qwen/qwen3-8b"},
    "local_soul":      {"url":"http://localhost:6969/v1", "api_key":"local",    "model":""},
}
_DEFAULT_RT = {p: "local_lm_studio" for p in ALL_PASSES}
_DEFAULT_RT.update({"soul":"local_soul","opening":"local_soul"})

# ── MCP custom tools ────────────────────────────────────────────────
# Stored in its own JSON file (mcp_custom_tools.json) instead of inside a
# .py file, since the whole point is letting the user define/extend tools
# with plain JSON — no Python edits required.
MCP_TOOLS_FILE = "mcp_custom_tools.json"

_DEFAULT_MCP_TOOLS = [
    {
        "name":        "example_http_tool",
        "description": "Contoh tool kustom — memanggil endpoint HTTP eksternal. "
                        "Edit JSON ini langsung, atau hapus kalau tidak dipakai.",
        "category":    "custom",
        "type":        "http",
        "method":      "GET",
        "url":         "https://api.example.com/lookup?q={query}",
        "headers":     {},
        "body":        None,
        "params": [
            {"name": "query", "type": "string", "required": True,
             "description": "Kata kunci pencarian"}
        ]
    }
]

def read_mcp_tools(path):
    """Returns a list of tool dicts, or None if the file is missing/invalid."""
    raw = read_file(path)
    if not raw.strip():
        return None
    try:
        data = json.loads(raw)
    except Exception:
        return None
    tools = data.get("tools") if isinstance(data, dict) else data
    return tools if isinstance(tools, list) else None

def write_mcp_tools(path, tools):
    write_file(path, json.dumps({"tools": tools}, indent=2, ensure_ascii=False) + "\n")

# Reference list of the built-in router functions already wired up in
# mcp_tools.py's RouterExecutor._dispatch(). Read-only — shown in the MCP
# tab so the user can see what already exists before adding custom tools
# (and avoid picking a name that collides with one of these).
# name=None means the function is currently a stub that always returns "N/A"
# (or, for "gme", a hardcoded "normal") — there's no dedicated handler yet.
BUILTIN_ROUTER_FUNCS = [
    ("User", [
        ("gum",  "get_user_memory",         "Info lengkap user (romance, info, notes)"),
        ("gus",  "get_user_stats",          "Romance points/level, ringkasan gift, last seen"),
        ("guls", "get_user_last_seen",      "Kapan terakhir user chat"),
        ("gur",  "get_user_relationship",   "Status & level romance user"),
        ("gmu",  "get_multiple_users",      "Romance level beberapa user sekaligus"),
        ("gugh", "get_user_gift_history",   "Riwayat gift user"),
        ("guch", "get_user_chat_history",   "Ringkasan chat history user"),
        ("uum",  "update_user_memory",      "Update info/note/romance/nickname/vip user"),
        ("bu",   "banned_user",             "Tandai user banned (khusus admin)"),
    ]),
    ("Chat", [
        ("grc", "get_recent_chat",   "Ringkasan chat terbaru"),
        ("gts", "get_topic_history", "Topik obrolan saat ini"),
        ("sc",  "search_chat",       "Cari kata kunci di chat history"),
        ("gcs", "get_chat_since",    "Ringkasan chat sejak waktu tertentu"),
    ]),
    ("State", [
        ("gsi", "get_stream_info", "Topik/role/style saat ini"),
        ("gsd", None,              "Belum diimplementasi — selalu \"N/A\""),
        ("gvc", None,              "Belum diimplementasi — selalu \"N/A\""),
        ("gav", None,              "Belum diimplementasi — selalu \"N/A\""),
        ("gsm", "get_stream_mood", "Mood/style stream saat ini"),
        ("gca", None,              "Belum diimplementasi — selalu \"N/A\""),
    ]),
    ("Events", [
        ("gre", None, "Belum diimplementasi — selalu \"N/A\""),
        ("gpg", None, "Belum diimplementasi — selalu \"N/A\""),
        ("gns", None, "Belum diimplementasi — selalu \"N/A\""),
        ("gms", None, "Belum diimplementasi — selalu \"N/A\""),
        ("gri", None, "Belum diimplementasi — selalu \"N/A\""),
        ("gpm", None, "Belum diimplementasi — selalu \"N/A\""),
    ]),
    ("Game", [
        ("gcg", None,                    "Belum diimplementasi — selalu \"N/A\""),
        ("ggs", None,                    "Belum diimplementasi — selalu \"N/A\""),
        ("gac", "get_activity_context",  "Role/aktivitas karakter saat ini"),
    ]),
    ("Social", [
        ("gfc", None, "Belum diimplementasi — selalu \"N/A\""),
        ("gtg", None, "Belum diimplementasi — selalu \"N/A\""),
        ("gnf", None, "Belum diimplementasi — selalu \"N/A\""),
        ("gru", None, "Belum diimplementasi — selalu \"N/A\""),
        ("gug", None, "Belum diimplementasi — selalu \"N/A\""),
    ]),
    ("Self", [
        ("gmm",  "get_alfa_mood",             "Mood karakter saat ini"),
        ("gme",  None,                        "Selalu mengembalikan \"normal\" (belum diimplementasi penuh)"),
        ("gcm",  "get_character_mode",        "Role/mode karakter saat ini"),
        ("grr",  "get_recent_responses",      "Respons terakhir karakter"),
        ("gmos", "get_alfa_memory_of_session","Ringkasan sesi: topik/role/style/command"),
    ]),
    ("Meta", [
        ("gtc", "get_time_context", "Hari/tanggal/jam saat ini"),
        ("gpe", None,               "Belum diimplementasi — selalu \"N/A\""),
        ("gan", None,               "Belum diimplementasi — selalu \"N/A\""),
    ]),
    ("v8 baru", [
        ("sn",  "set_nickname",   "Ganti nickname user (self atau target)"),
        ("rs",  "romance_status", "Cek status romance user"),
        ("cmd", "set_command",    "Set command override karakter"),
    ]),
]


# ══════════════════════════════════════════════════════════════════
# WIDGET PRIMITIVES
# ══════════════════════════════════════════════════════════════════

def _hline(parent, color=None, padx=0, pady=0):
    tk.Frame(parent, bg=color or C["bdr"], height=1).pack(fill="x", padx=padx, pady=pady)

def _field_entry(parent, var, width=None, **kw):
    """Dark input field matching screenshot style."""
    e = tk.Entry(parent,
                 textvariable=var,
                 font=C["f_m"],
                 bg=C["field"], fg=C["fg"],
                 insertbackground=C["fg"],
                 selectbackground=C["sel"],
                 selectforeground=C["fg"],
                 relief="flat", bd=0,
                 highlightthickness=1,
                 highlightbackground=C["bdr"],
                 highlightcolor=C["acc"],
                 **kw)
    if width: e.config(width=width)
    return e

def _icon_btn(parent, text, cmd, danger=False, size=26):
    """Square icon button — refresh/copy/delete."""
    bg  = C["btn"]
    hbg = C["red_bg"] if danger else C["btn_h"]
    hfg = C["red"]    if danger else C["fg"]
    b = tk.Button(parent, text=text, command=cmd,
                  font=("Segoe UI", 12),
                  bg=bg, fg=C["fg2"],
                  activebackground=hbg,
                  activeforeground=hfg,
                  relief="flat", bd=0,
                  width=2, height=1,
                  cursor="hand2",
                  highlightthickness=1,
                  highlightbackground=C["bdr"])
    b.bind("<Enter>", lambda e: b.config(bg=hbg, fg=hfg))
    b.bind("<Leave>", lambda e: b.config(bg=bg,  fg=C["fg2"]))
    return b

def _accent_btn(parent, text, cmd, width=None):
    """Purple accent button (Save all / Add endpoint)."""
    b = tk.Button(parent, text=text, command=cmd,
                  font=C["f_ui"],
                  bg=C["acc"], fg="#ffffff",
                  activebackground=C["acc_h"],
                  activeforeground="#ffffff",
                  relief="flat", bd=0,
                  padx=14, pady=6,
                  cursor="hand2")
    if width: b.config(width=width)
    b.bind("<Enter>", lambda e: b.config(bg=C["acc_h"]))
    b.bind("<Leave>", lambda e: b.config(bg=C["acc"]))
    return b

def _plain_btn(parent, text, cmd, width=None):
    """Plain dark button (Reload from disk / Export)."""
    b = tk.Button(parent, text=text, command=cmd,
                  font=C["f_ui"],
                  bg=C["btn"], fg=C["fg2"],
                  activebackground=C["btn_h"],
                  activeforeground=C["fg"],
                  relief="flat", bd=0,
                  padx=14, pady=6,
                  highlightthickness=1,
                  highlightbackground=C["bdr"],
                  cursor="hand2")
    if width: b.config(width=width)
    b.bind("<Enter>", lambda e: b.config(bg=C["btn_h"], fg=C["fg"]))
    b.bind("<Leave>", lambda e: b.config(bg=C["btn"],   fg=C["fg2"]))
    return b

class _Toggle(tk.Canvas):
    """Pill toggle switch."""
    W, H = 38, 22
    def __init__(self, parent, var):
        super().__init__(parent, width=self.W, height=self.H,
                         bg=C["card"], highlightthickness=0)
        self._var = var
        self.bind("<Button-1>", lambda e: (var.set(not var.get()), self._draw()))
        self._draw()
    def _draw(self):
        self.delete("all")
        on  = self._var.get()
        tc  = C["acc"] if on else C["btn"]
        r   = self.H // 2
        self.create_oval(1, 1, self.H-1, self.H-1,        fill=tc, outline="")
        self.create_oval(self.W-self.H+1, 1, self.W-1, self.H-1, fill=tc, outline="")
        self.create_rectangle(r, 1, self.W-r, self.H-1,   fill=tc, outline="")
        cx = self.W - r if on else r
        self.create_oval(cx-r+4, 4, cx+r-4, self.H-4, fill=C["fg"], outline="")

_WHEEL_BOUND_ROOTS = set()

class ScrollFrame(tk.Frame):
    def __init__(self, parent, bg=None):
        super().__init__(parent, bg=bg or C["main"])
        c  = tk.Canvas(self, bg=bg or C["main"], highlightthickness=0, bd=0)
        c._is_scroll_canvas = True          # tag so the global handler can find it
        sb = tk.Scrollbar(self, orient="vertical", command=c.yview,
                          bg=C["main"], troughcolor=C["main"],
                          relief="flat", bd=0, width=5)
        c.configure(yscrollcommand=sb.set)
        self.inner = tk.Frame(c, bg=bg or C["main"])
        wid = c.create_window((0,0), window=self.inner, anchor="nw")
        c.bind("<Configure>", lambda e: c.itemconfig(wid, width=e.width))
        self.inner.bind("<Configure>",
            lambda e: c.configure(scrollregion=c.bbox("all")))
        c.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")

        # Bind the wheel handler ONCE per top-level window. Each ScrollFrame used
        # to call canvas.bind_all() itself, which silently overwrote every other
        # tab's binding (only the most-recently-created tab could be scrolled).
        # Instead, bind a single dispatcher that looks up whatever scrollable
        # canvas is actually under the mouse pointer and scrolls that one.
        root = self.winfo_toplevel()
        if root not in _WHEEL_BOUND_ROOTS:
            _WHEEL_BOUND_ROOTS.add(root)

            def _find_canvas(w):
                while w is not None:
                    if getattr(w, "_is_scroll_canvas", False):
                        return w
                    w = w.master
                return None

            def _dispatch(delta_units, e):
                w = root.winfo_containing(e.x_root, e.y_root)
                target = _find_canvas(w)
                if target is not None:
                    target.yview_scroll(delta_units, "units")

            def _on_wheel(e):
                d = -1 if (e.delta > 0 if sys.platform == "darwin" else e.delta < 0) else 1
                _dispatch(d, e)

            root.bind_all("<MouseWheel>", _on_wheel)
            # Linux uses Button-4 (up) / Button-5 (down) instead of <MouseWheel>
            root.bind_all("<Button-4>", lambda e: _dispatch(-1, e))
            root.bind_all("<Button-5>", lambda e: _dispatch(1, e))
            # Cleanup global ref when window is destroyed (prevents GC leak)
            root.bind("<Destroy>", lambda e, r=root: _WHEEL_BOUND_ROOTS.discard(r), add="+")


# ══════════════════════════════════════════════════════════════════
# TAB: ENDPOINTS
# ══════════════════════════════════════════════════════════════════
class TabEndpoints(tk.Frame):
    def __init__(self, master, app):
        super().__init__(master, bg=C["main"])
        self.app  = app
        self._vars: Dict[str, Dict] = {}
        self._trace_ids: list = []   # (var, trace_id) — removed before each re-render
        self._build()

    def _build(self):
        # ── Sub-header row ──────────────────────────────────────
        bar = tk.Frame(self, bg=C["main"])
        bar.pack(fill="x", padx=20, pady=(16, 12))
        tk.Label(bar, text="REGISTERED ENDPOINTS",
                 font=C["f_ui9"], fg=C["fg3"], bg=C["main"]
                 ).pack(side="left")
        _accent_btn(bar, "+ Add endpoint", self._add
                    ).pack(side="right")

        # ── Scroll ──────────────────────────────────────────────
        sf = ScrollFrame(self, bg=C["main"])
        sf.pack(fill="both", expand=True)
        self._inner = sf.inner
        self._render_all()

    def _render_all(self):
        # Remove all lingering traces before destroying widgets
        for var, tid in self._trace_ids:
            try: var.trace_remove("write", tid)
            except Exception: pass
        self._trace_ids.clear()
        for w in self._inner.winfo_children(): w.destroy()
        self._vars.clear()
        for k in list(self.app.endpoints.keys()):
            self._render_ep(k)
        tk.Frame(self._inner, bg=C["main"], height=20).pack()

    def _render_ep(self, k):
        ep    = self.app.endpoints[k]
        url   = ep.get("url", "")
        local = "localhost" in url or "127.0" in url

        # Outer card
        card = tk.Frame(self._inner,
                        bg=C["card"],
                        highlightthickness=1,
                        highlightbackground=C["bdr"])
        card.pack(fill="x", padx=20, pady=(0, 10))

        # ── Header row ──────────────────────────────────────────
        head = tk.Frame(card, bg=C["card"])
        head.pack(fill="x", padx=14, pady=10)

        # Collapse icon + name
        tk.Label(head, text="▭",
                 font=("Segoe UI",11), fg=C["fg3"], bg=C["card"]
                 ).pack(side="left")

        name_v = tk.StringVar(value=k)
        name_e = tk.Entry(head,
                          textvariable=name_v,
                          font=C["f_b"],
                          bg=C["card"], fg=C["fg"],
                          insertbackground=C["fg"],
                          relief="flat", bd=0,
                          highlightthickness=0,
                          width=20)
        name_e.pack(side="left", padx=(6, 10))

        # Badge
        bbg = C["green_bg"] if local else C["blue_bg"]
        bfg = C["green"]    if local else C["blue"]
        tk.Label(head, text=" local " if local else " online ",
                 font=C["f_ui9"],
                 bg=bbg, fg=bfg,
                 padx=4, pady=2
                 ).pack(side="left")

        # Right buttons
        bframe = tk.Frame(head, bg=C["card"])
        bframe.pack(side="right")
        _icon_btn(bframe, "⟳", lambda kk=k: self._toggle_local(kk)
                  ).pack(side="left", padx=(0, 4))
        _icon_btn(bframe, "⧉", lambda kk=k: self._dup(kk)
                  ).pack(side="left", padx=(0, 4))
        _icon_btn(bframe, "✕", lambda kk=k: self._remove(kk), danger=True
                  ).pack(side="left")

        # ── Divider ─────────────────────────────────────────────
        _hline(card, color=C["bdr"])

        # ── Fields row ──────────────────────────────────────────
        # Three cells separated by vertical dividers
        fields_f = tk.Frame(card, bg=C["card"])
        fields_f.pack(fill="x")

        vars_ = {"_name": name_v}
        defs  = [
            ("URL",        "url",     2),   # weight 2 → wider
            ("API key",    "api_key", 1),
            ("Model name", "model",   1),
        ]
        for i, (lbl, fld, _) in enumerate(defs):
            if i:
                tk.Frame(fields_f, bg=C["bdr"], width=1
                         ).pack(side="left", fill="y", pady=8)

            cell = tk.Frame(fields_f, bg=C["card"])
            cell.pack(side="left", fill="x", expand=True, padx=14, pady=10)

            tk.Label(cell, text=lbl,
                     font=C["f_ui9"], fg=C["fg3"], bg=C["card"]
                     ).pack(anchor="w", pady=(0, 4))

            v = tk.StringVar(value=ep.get(fld, ""))
            vars_[fld] = v
            _field_entry(cell, v).pack(fill="x")
            _tid = v.trace_add("write", lambda *_, kk=k, ff=fld, vv=v:
                        self._on_field(kk, ff, vv.get()))
            self._trace_ids.append((v, _tid))

        # Divider + "Disable thinking" toggle cell — kirim enable_thinking=False
        # (extra_body) ke endpoint ini supaya model (mis. Qwen reasoning) tidak
        # mengeluarkan chain-of-thought/thinking block.
        tk.Frame(fields_f, bg=C["bdr"], width=1
                 ).pack(side="left", fill="y", pady=8)
        think_cell = tk.Frame(fields_f, bg=C["card"])
        think_cell.pack(side="left", padx=14, pady=10)
        tk.Label(think_cell, text="Disable thinking",
                 font=C["f_ui9"], fg=C["fg3"], bg=C["card"]
                 ).pack(anchor="w", pady=(0, 4))
        think_v = tk.BooleanVar(value=bool(ep.get("disable_thinking", False)))
        vars_["disable_thinking"] = think_v
        _Toggle(think_cell, think_v).pack(anchor="w")
        _ttid = think_v.trace_add("write", lambda *_, kk=k, vv=think_v:
                    self._on_field(kk, "disable_thinking", bool(vv.get())))
        self._trace_ids.append((think_v, _ttid))

        _ntid = name_v.trace_add("write", lambda *_, kk=k, vv=name_v:
                         self._on_rename(kk, vv.get()))
        self._trace_ids.append((name_v, _ntid))
        self._vars[k] = vars_

    def _on_field(self, k, field, val):
        if k in self.app.endpoints:
            self.app.endpoints[k][field] = val
            self.app.mark_dirty()

    def _on_rename(self, old_k, new_k):
        new_k = new_k.strip()
        if not new_k or new_k == old_k or new_k in self.app.endpoints: return
        self.app.endpoints = {(new_k if kk == old_k else kk): v
                              for kk, v in self.app.endpoints.items()}
        for p in self.app.routing:
            if self.app.routing[p] == old_k:
                self.app.routing[p] = new_k
        self.app.mark_dirty()

    def _toggle_local(self, k):
        ep  = self.app.endpoints.get(k, {})
        url = ep.get("url","")
        ep["url"] = "http://localhost:1234/v1" if ("localhost" not in url and "127.0" not in url) \
                    else url.replace("localhost","myapi.example.com")
        self.app.endpoints[k] = ep
        self._render_all(); self.app.mark_dirty()

    def _dup(self, k):
        nk = k + "_copy"
        self.app.endpoints[nk] = dict(self.app.endpoints[k])
        self._render_all(); self.app.mark_dirty()

    def _remove(self, k):
        if len(self.app.endpoints) <= 1:
            messagebox.showwarning("Cannot remove", "At least one endpoint required.")
            return
        if messagebox.askyesno("Remove", f"Remove endpoint '{k}'?"):
            del self.app.endpoints[k]
            fb = list(self.app.endpoints.keys())[0]
            for p in self.app.routing:
                if self.app.routing[p] == k: self.app.routing[p] = fb
            self._render_all(); self.app.mark_dirty()

    def _add(self):
        i = len(self.app.endpoints) + 1
        self.app.endpoints[f"endpoint_{i}"] = {
            "url":"http://localhost:1234/v1","api_key":"lm-studio","model":"",
            "disable_thinking": False}
        self._render_all(); self.app.mark_dirty()

    def collect(self): return dict(self.app.endpoints)
    def refresh(self): self._render_all()

# ══════════════════════════════════════════════════════════════════
# TAB: PASS ROUTING
# ══════════════════════════════════════════════════════════════════
class TabRouting(tk.Frame):
    def __init__(self, master, app):
        super().__init__(master, bg=C["main"])
        self.app   = app
        self._vars: Dict[str, tk.StringVar] = {}
        self._dots: Dict[str, tk.Label]     = {}
        self._trace_ids: list               = []
        self._build()

    def _build(self):
        # Init combobox style ONCE, not on every _render
        sty = ttk.Style()
        sty.theme_use("default")
        sty.configure("D.TCombobox",
                      fieldbackground=C["field"],
                      background=C["btn"],
                      foreground=C["fg"],
                      selectbackground=C["field"],
                      selectforeground=C["fg"],
                      arrowcolor=C["fg3"],
                      relief="flat", bd=0)
        sty.map("D.TCombobox", fieldbackground=[("readonly", C["field"])])
        sf = ScrollFrame(self, bg=C["main"])
        sf.pack(fill="both", expand=True)
        self._inner = sf.inner
        self._render()

    def _is_local(self, ep_name):
        url = self.app.endpoints.get(ep_name, {}).get("url","")
        return "localhost" in url or "127.0" in url

    def _render(self):
        for var, tid in self._trace_ids:
            try: var.trace_remove("write", tid)
            except Exception: pass
        self._trace_ids.clear()
        for w in self._inner.winfo_children(): w.destroy()
        self._vars.clear(); self._dots.clear()

        ep_names = list(self.app.endpoints.keys())

        for group_name, passes in PASS_GROUPS:
            # Group label
            gl = tk.Frame(self._inner, bg=C["main"])
            gl.pack(fill="x", padx=20, pady=(14,4))
            tk.Label(gl, text=group_name.upper(),
                     font=C["f_ui9"], fg=C["fg3"], bg=C["main"]
                     ).pack(anchor="w")

            # Card
            card = tk.Frame(self._inner, bg=C["card"],
                            highlightthickness=1,
                            highlightbackground=C["bdr"])
            card.pack(fill="x", padx=20)

            for idx, (pass_key, desc) in enumerate(passes):
                if idx: _hline(card, color=C["bdr"])

                row = tk.Frame(card, bg=C["card"])
                row.pack(fill="x", padx=14, pady=8)

                # Pass name + desc
                left = tk.Frame(row, bg=C["card"])
                left.pack(side="left", fill="x", expand=True)
                tk.Label(left, text=pass_key,
                         font=C["f_m"], fg=C["fg"], bg=C["card"]
                         ).pack(anchor="w")
                tk.Label(left, text=desc,
                         font=C["f_ui9"], fg=C["fg3"], bg=C["card"]
                         ).pack(anchor="w")

                # Arrow
                tk.Label(row, text="→", font=C["f_ui"],
                         fg=C["fg3"], bg=C["card"]
                         ).pack(side="left", padx=10)

                # Combobox
                cur = self.app.routing.get(pass_key, ep_names[0] if ep_names else "")
                v   = tk.StringVar(value=cur)
                self._vars[pass_key] = v
                cb  = ttk.Combobox(row, textvariable=v,
                                   values=ep_names, width=22,
                                   font=C["f_m10"],
                                   style="D.TCombobox",
                                   state="readonly")
                cb.pack(side="left")
                _tid = v.trace_add("write", lambda *_, pk=pass_key: self._on_change(pk))
                self._trace_ids.append((v, _tid))

                # Dot
                is_loc = self._is_local(cur)
                dot = tk.Label(row, text="●",
                               font=("Segoe UI",10),
                               fg=C["green"] if is_loc else C["blue"],
                               bg=C["card"])
                dot.pack(side="left", padx=(8,0))
                self._dots[pass_key] = dot

        # Quick-set
        qs = tk.Frame(self._inner, bg=C["main"])
        qs.pack(fill="x", padx=20, pady=(12,20))
        tk.Label(qs, text="Quick set all →",
                 font=C["f_ui9"], fg=C["fg3"], bg=C["main"]
                 ).pack(side="left")
        for ep_k in ep_names:
            is_loc = self._is_local(ep_k)
            b = tk.Button(qs, text=ep_k,
                          font=C["f_ui9"],
                          bg=C["btn"], fg=C["green"] if is_loc else C["blue"],
                          activebackground=C["btn_h"], activeforeground=C["fg"],
                          relief="flat", bd=0, padx=10, pady=3,
                          highlightthickness=1, highlightbackground=C["bdr"],
                          cursor="hand2",
                          command=lambda kk=ep_k: self._set_all(kk))
            b.pack(side="left", padx=4)

    def _on_change(self, pk):
        val = self._vars[pk].get()
        self.app.routing[pk] = val
        if pk in self._dots:
            self._dots[pk].config(fg=C["green"] if self._is_local(val) else C["blue"])
        self.app.mark_dirty()

    def _set_all(self, ep_k):
        for pk in ALL_PASSES:
            self.app.routing[pk] = ep_k
            if pk in self._vars: self._vars[pk].set(ep_k)
        self.app.mark_dirty()

    def collect(self): return {k: v.get() for k, v in self._vars.items()}
    def refresh(self): self._render()

# ══════════════════════════════════════════════════════════════════
# GENERIC SETTINGS TAB  (shared by Tuning, Live Server, TTS)
# ══════════════════════════════════════════════════════════════════
class GenericTab(tk.Frame):
    def __init__(self, master, app, sections, vals_attr):
        super().__init__(master, bg=C["main"])
        self.app       = app
        self._attr     = vals_attr
        self._vars: Dict[str, tk.Variable] = {}
        self._toggles: Dict[str, _Toggle]  = {}
        sf = ScrollFrame(self, bg=C["main"])
        sf.pack(fill="both", expand=True)
        self._build(sf.inner, sections)

    def _build(self, inner, sections):
        for section_name, fields in sections:
            # Section label
            sg = tk.Frame(inner, bg=C["main"])
            sg.pack(fill="x", padx=20, pady=(16,4))
            tk.Label(sg, text=section_name.upper(),
                     font=C["f_ui9"], fg=C["fg3"], bg=C["main"]
                     ).pack(anchor="w")

            card = tk.Frame(inner, bg=C["card"],
                            highlightthickness=1,
                            highlightbackground=C["bdr"])
            card.pack(fill="x", padx=20)

            src_vals = getattr(self.app, self._attr)

            for idx, (varname, label, sub, dtype, ew) in enumerate(fields):
                if idx: _hline(card, color=C["bdr"])
                row = tk.Frame(card, bg=C["card"])
                row.pack(fill="x", padx=14, pady=9)

                # Left: label + sub
                left = tk.Frame(row, bg=C["card"])
                left.pack(side="left", fill="x", expand=True)
                tk.Label(left, text=label,
                         font=C["f_ui"], fg=C["fg"], bg=C["card"]
                         ).pack(anchor="w")
                tk.Label(left, text=f"{varname}",
                         font=C["f_ui9"], fg=C["fg3"], bg=C["card"]
                         ).pack(anchor="w")

                # Right: widget
                right = tk.Frame(row, bg=C["card"])
                right.pack(side="right")

                cur = src_vals.get(varname)

                if dtype == "bool":
                    v = tk.BooleanVar(value=bool(cur))
                    self._vars[varname] = v
                    t = _Toggle(right, v)
                    t.pack()
                    self._toggles[varname] = t
                elif dtype == "int":
                    v = tk.IntVar(value=int(cur or 0))
                    self._vars[varname] = v
                    sp = tk.Spinbox(right, textvariable=v,
                                   from_=0, to=99999, width=ew or 8,
                                   font=C["f_m"],
                                   bg=C["field"], fg=C["fg"],
                                   buttonbackground=C["btn"],
                                   insertbackground=C["fg"],
                                   relief="flat", bd=0,
                                   highlightthickness=1,
                                   highlightbackground=C["bdr"],
                                   highlightcolor=C["acc"])
                    sp.pack()
                    v.trace_add("write", lambda *_: self.app.mark_dirty())
                else:
                    v = tk.StringVar(value=str(cur or ""))
                    self._vars[varname] = v
                    _field_entry(right, v, width=ew or 24).pack()
                    v.trace_add("write", lambda *_: self.app.mark_dirty())

        tk.Frame(inner, bg=C["main"], height=20).pack()

    def collect(self):
        result = {}
        for k, v in self._vars.items():
            raw = v.get()
            if isinstance(v, tk.BooleanVar): result[k] = bool(raw)
            elif isinstance(v, tk.IntVar):   result[k] = int(raw)
            else:
                try:    result[k] = float(raw) if ("." in str(raw)) else raw
                except: result[k] = raw
        return result

# Section definitions
TUNING_SECTIONS = [
    ("Storage paths", [
        ("STORAGE_DIR",      "Storage dir",      "Root output directory",            "str", 20),
        ("MEMORY_DIR",       "Memory dir",        "user_{id}.json files",             "str", 20),
        ("MODEL_MEMORY_DIR", "Model memory dir",  "{char}.memory + .history",         "str", 20),
    ]),
    ("Memory limits", [
        ("MAX_HISTORY",  "Max history",   "Shared history entries → LLM",   "int", 8),
        ("MAX_INFO",     "Max info",      "info_user entries per user",      "int", 8),
        ("MAX_NOTES",    "Max notes",     "Note entries per user",           "int", 8),
        ("MAX_GIFTS",    "Max gifts",     "Gift history entries per user",   "int", 8),
        ("MAX_SEGMENTS", "Max segments",  "Max output segments / response",  "int", 8),
    ]),
    ("Debug", [
        ("DEBUG", "Debug mode", "Print per-call routing info to console", "bool", None),
    ]),
]

LIVE_SECTIONS = [
    ("WebSocket & Live2D URLs", [
        ("WS_APP_URL",   "WS App URL",      "websocket endpoint",  "str", 32),
        ("L2D_HTTP_URL", "Live2D HTTP URL", "vtube studio http",   "str", 32),
        ("L2D_WS_URL",   "Live2D WS URL",   "vtube studio ws",     "str", 32),
    ]),
    ("Anti-spam", [
        ("SPAM_MSG_LIMIT",    "Spam msg limit",      "pesan per window",      "int",   8),
        ("SPAM_WINDOW_SEC",   "Spam window (sec)",   "jendela waktu spam",    "str",   8),
        ("SPAM_COOLDOWN_SEC", "Spam cooldown (sec)", "cooldown setelah spam", "str",   8),
    ]),
    ("Dedup & batch", [
        ("DEDUP_WINDOW_SEC", "Dedup window (sec)", "jendela deduplikasi", "str", 8),
        ("BATCH_WINDOW_SEC", "Batch window (sec)", "jendela batch",       "str", 8),
    ]),
    ("Queue", [
        ("MAX_QUEUE_SIZE",    "Max queue size",   "maks antrian aktif",          "int", 8),
        ("QUEUE_RANDOM_FROM", "Random drop from", "drop random mulai index ini", "int", 8),
    ]),
    ("Gift trigger", [
        ("GIFT_TRIGGER_MIN", "Gift trigger min", "min gift untuk trigger", "int", 8),
        ("GIFT_TRIGGER_MAX", "Gift trigger max", "max gift per trigger",   "int", 8),
    ]),
    ("Maintenance", [
        ("CLEANUP_INTERVAL", "Cleanup interval (sec)", "interval bersih cache", "int", 8),
        ("USER_CACHE_LIMIT", "User cache limit",        "maks user di cache",   "int", 8),
    ]),
    ("Live2D model", [
        ("L2D_MODEL_ID",  "Model ID",  "ID model vtube studio", "int", 8),
        ("L2D_MODEL_MAP", "Model map", "index map model",        "int", 8),
    ]),
]

TTS_SECTIONS = [
    ("TTS server", [
        ("TTS_SERVER_URL", "Server URL",  "endpoint TTS server",  "str", 36),
        ("TTS_MODEL",      "Model name",  "nama model voice",     "str", 20),
    ]),
    ("TTS parameters", [
        ("noise_scale",   "Noise scale",   "variasi fonem",   "str", 10),
        ("noise_scale_w", "Noise scale W", "variasi durasi",  "str", 10),
        ("length_scale",  "Length scale",  "kecepatan bicara","str", 10),
    ]),
    ("Audio output", [
        ("AUDIO_PATH", "Audio path", "path output file .wav", "str", 36),
    ]),
]

# ══════════════════════════════════════════════════════════════════
# TAB: MCP TOOLS  (custom tools defined in plain JSON)
# ══════════════════════════════════════════════════════════════════
class TabMCP(tk.Frame):
    """Lets the user define custom MCP/router tools without touching Python:
    each tool is one JSON object (name, description, params, how it's called).
    They're stored in mcp_custom_tools.json and edited as raw JSON per-card,
    since the shape of a tool definition varies too much for fixed form fields."""

    def __init__(self, master, app):
        super().__init__(master, bg=C["main"])
        self.app = app
        self._cards: Dict[int, Dict] = {}   # index -> {"text": Text widget, "err": Label, "name_lbl": Label, "badge": Label}
        self._trace_ids: list        = []
        self._builtin_expanded  = False
        self._compiled_expanded = True   # default expand compiled panel
        self._compiled_frame    = tk.Frame(self)  # placeholder
        self._build()

    def _build(self):
        # ── Header bar ───────────────────────────────────────────────────────
        bar = tk.Frame(self, bg=C["main"])
        bar.pack(fill="x", padx=20, pady=(16, 12))
        tk.Label(bar, text="CUSTOM MCP TOOLS",
                 font=C["f_ui9"], fg=C["fg3"], bg=C["main"]
                 ).pack(side="left")
        _accent_btn(bar, "+ Add tool", self._add
                    ).pack(side="right")
        _plain_btn(bar, "↑ Import JSON", self._import
                    ).pack(side="right", padx=(0, 8))
        _plain_btn(bar, "↓ Export JSON", self._export
                    ).pack(side="right", padx=(0, 8))

        # ── Compile toolbar ───────────────────────────────────────────────────
        compile_bar = tk.Frame(self, bg=C["card"],
                               highlightthickness=1, highlightbackground=C["bdr"])
        compile_bar.pack(fill="x", padx=20, pady=(0, 10))

        left_cb = tk.Frame(compile_bar, bg=C["card"])
        left_cb.pack(side="left", fill="x", expand=True, padx=14, pady=10)

        tk.Label(left_cb, text="⚙ Tool Compiler",
                 font=C["f_b"], fg=C["fg"], bg=C["card"]).pack(side="left")

        self._compile_status = tk.Label(
            left_cb, text="  —  belum di-compile",
            font=C["f_ui9"], fg=C["fg3"], bg=C["card"])
        self._compile_status.pack(side="left", padx=(8, 0))

        right_cb = tk.Frame(compile_bar, bg=C["card"])
        right_cb.pack(side="right", padx=14, pady=10)

        self._compile_btn = _accent_btn(right_cb, "▶ Compile Tools", self._compile_tools)
        self._compile_btn.pack(side="right")
        _plain_btn(right_cb, "↺ Force", lambda: self._compile_tools(force=True)
                   ).pack(side="right", padx=(0, 8))

        # Progress bar (tersembunyi saat idle)
        self._progress_frame = tk.Frame(self, bg=C["main"])
        self._progress_frame.pack(fill="x", padx=20, pady=(0, 4))
        self._progress_bar_var = tk.DoubleVar(value=0)
        self._progress_bar = ttk.Progressbar(
            self._progress_frame, variable=self._progress_bar_var,
            maximum=100, mode="determinate", length=400)
        self._progress_lbl = tk.Label(
            self._progress_frame, text="",
            font=C["f_ui9"], fg=C["fg3"], bg=C["main"])
        self._progress_frame.pack_forget()  # hide initially

        # ── Help text ─────────────────────────────────────────────────────────
        help_f = tk.Frame(self, bg=C["main"])
        help_f.pack(fill="x", padx=20, pady=(0, 10))
        tk.Label(help_f,
                 text="Setiap tool didefinisikan sebagai satu objek JSON — edit langsung di "
                      "kotak teksnya lalu klik ✓ Apply. Field minimal: \"name\" dan \"description\". "
                      "Tidak ada toggle enable/disable lagi — tool jadi aktif begitu berhasil "
                      "di-compile lewat ▶ Compile Tools di bawah (ditandai punya .bin di folder tools/).",
                 font=C["f_ui9"], fg=C["fg3"], bg=C["main"], anchor="w",
                 wraplength=760, justify="left"
                 ).pack(fill="x")

        sf = ScrollFrame(self, bg=C["main"])
        sf.pack(fill="both", expand=True)
        self._inner = sf.inner
        self._render_all()
        self._refresh_compile_status()

    # ── Tool Compiler ─────────────────────────────────────────────────────────

    def _refresh_compile_status(self):
        """Baca status dari compiled .bin dan update label."""
        try:
            from tool_compiler import _read_compiled, COMPILED_BIN
            import os, time
            cache = _read_compiled()
            if not cache:
                self._compile_status.config(
                    text="  —  belum di-compile", fg=C["fg3"])
                return
            tools  = cache.get("tools", {})
            ts     = cache.get("compiled_at", 0)
            n      = len(tools)
            ago    = int(time.time() - ts)
            if ago < 60:
                time_str = f"{ago}s ago"
            elif ago < 3600:
                time_str = f"{ago//60}m ago"
            else:
                time_str = f"{ago//3600}h ago"
            aliases = ", ".join(
                f"{m.get('alias','?')}={k}"
                for k, m in tools.items()
            )
            self._compile_status.config(
                text=f"  ✅  {n} tool(s) compiled {time_str}  |  {aliases}",
                fg=C.get("green", C["acc"]))
        except Exception as e:
            self._compile_status.config(
                text=f"  ⚠  {e}", fg=C.get("red", "#e05"))

    def _set_compile_progress(self, pct: float, msg: str):
        """Update progress bar dan label (thread-safe via after)."""
        def _update():
            if pct <= 0:
                self._progress_frame.pack_forget()
                return
            if not self._progress_frame.winfo_ismapped():
                self._progress_frame.pack(fill="x", padx=20, pady=(0, 4))
                self._progress_bar.pack(side="left")
                self._progress_lbl.pack(side="left", padx=(10, 0))
            self._progress_bar_var.set(pct)
            self._progress_lbl.config(text=msg)
            if pct >= 100:
                self.after(2000, lambda: self._set_compile_progress(0, ""))
        self.after(0, _update)

    def _compile_tools(self, force: bool = False):
        """
        Compile custom tools via LLM — dijalankan di background thread
        supaya UI tidak freeze. Progress ditampilkan real-time.

        Tidak ada lagi filter enable/disable: semua tool yang punya "name"
        ikut di-compile. Aktif/tidaknya sebuah tool di runtime sekarang
        ditentukan oleh apakah dia berhasil di-compile (punya .bin di
        folder tools/) — bukan oleh toggle manual.
        """
        import threading

        # Apply semua pending edits dulu
        for i in list(self._cards.keys()):
            self._apply(i)

        to_compile = [t for t in self.app.mcp_tools if t.get("name")]
        if not to_compile:
            self._compile_status.config(
                text="  ⚠  Tidak ada tool untuk di-compile", fg=C.get("red", "#e05"))
            return

        # Disable tombol selama proses
        self._compile_btn.config(state="disabled", text="⏳ Compiling...")
        self._set_compile_progress(5, f"Memulai compile {len(to_compile)} tool(s)...")

        def _bg():
            try:
                from tool_compiler import compile_tools, COMPILED_BIN

                # Coba ambil llm_call dari main
                llm_fn = None
                try:
                    import main as _main
                    llm_fn = _main._llm_call
                except Exception:
                    pass

                total = len(to_compile)
                results = {}

                if llm_fn:
                    # Compile satu per satu dengan progress update
                    from tool_compiler import (
                        _tool_hash, _read_compiled, _write_compiled,
                        _generate_tool_metadata, _fallback_metadata,
                        _tools_bundle_hash
                    )
                    import time

                    old_cache  = _read_compiled() or {}
                    old_tools  = old_cache.get("tools", {})
                    used_aliases: set = set()
                    new_tools  = {}

                    for idx, tool in enumerate(to_compile):
                        name       = tool["name"]
                        tool_hash  = _tool_hash(tool)
                        old_entry  = old_tools.get(name, {})
                        pct        = 10 + int((idx / total) * 80)

                        if not force and old_entry.get("hash") == tool_hash:
                            self._set_compile_progress(pct, f"[{idx+1}/{total}] {name} — cache hit ✓")
                            meta = old_entry
                        else:
                            self._set_compile_progress(pct, f"[{idx+1}/{total}] {name} — generating...")
                            meta = _generate_tool_metadata(tool, llm_fn)
                            if meta is None:
                                self._set_compile_progress(pct, f"[{idx+1}/{total}] {name} — LLM gagal, fallback")
                                meta = _fallback_metadata(tool)

                        # Resolve alias conflict
                        alias    = meta.get("alias", name[:3].lower())
                        orig     = alias
                        suffix   = 2
                        while alias in used_aliases:
                            alias  = f"{orig}{suffix}"
                            suffix += 1
                        meta["alias"]    = alias
                        meta["hash"]     = tool_hash
                        meta["name"]     = name
                        meta["category"] = tool.get("category", "custom")
                        meta["enabled"]  = True
                        new_tools[name]  = meta
                        used_aliases.add(alias)

                    bundle_hash = _tools_bundle_hash(to_compile)
                    _write_compiled({
                        "bundle_hash": bundle_hash,
                        "compiled_at": time.time(),
                        "tools": new_tools,
                    })
                    results = new_tools
                else:
                    # Fallback mode (no LLM available)
                    self._set_compile_progress(30, "LLM tidak tersedia, mode fallback...")
                    results = compile_tools(llm_call=None, force=force, verbose=False)

                # Invalidate runtime cache
                try:
                    import mcp_tools as _mcp
                    _mcp._custom_tools_cache = None
                except Exception:
                    pass

                # Selesai — update UI di main thread
                n = len(results)
                aliases = ", ".join(
                    f"{m.get('alias','?')}={k}"
                    for k, m in results.items()
                )
                self.after(0, lambda: self._compile_status.config(
                    text=f"  ✅  {n} tool(s) compiled  |  {aliases}",
                    fg=C.get("green", C["acc"])))
                self._set_compile_progress(100, f"✅ Selesai — {n} tool(s) compiled")
                # Refresh compiled panel agar langsung tampil hasilnya
                self.after(200, self._refresh_compiled_panel)

            except Exception as e:
                self.after(0, lambda: self._compile_status.config(
                    text=f"  ❌  Error: {e}", fg=C.get("red", "#e05")))
                self._set_compile_progress(0, "")

            finally:
                self.after(0, lambda: self._compile_btn.config(
                    state="normal", text="▶ Compile Tools"))

        threading.Thread(target=_bg, daemon=True).start()

    # ── Compiled tools panel ─────────────────────────────────────────────────

    def _render_compiled_panel(self):
        """
        Panel collapsible yang menampilkan hasil compile tools.
        Format mirip built-in panel: per-tool row dengan alias, desc, sim, triggers.
        Di-refresh setiap _render_all() dipanggil (termasuk setelah compile selesai).
        """
        # Destroy panel lama jika ada
        if hasattr(self, "_compiled_frame") and self._compiled_frame.winfo_exists():
            self._compiled_frame.destroy()

        # Load compiled data
        compiled = {}
        try:
            from tool_compiler import load_compiled_tools
            compiled = load_compiled_tools()
        except Exception:
            pass

        # Selalu tampilkan panel (biar user tahu statusnya)
        card = tk.Frame(self._inner, bg=C["card"],
                        highlightthickness=1, highlightbackground=C["bdr"])
        self._compiled_frame = card
        card.pack(fill="x", padx=20, pady=(0, 14))

        # ── Header ─────────────────────────────────────────────────────────
        head = tk.Frame(card, bg=C["card"], cursor="hand2")
        head.pack(fill="x", padx=14, pady=10)

        arrow_lbl = tk.Label(head,
            text=("▾" if self._compiled_expanded else "▸"),
            font=C["f_ui10"], fg=C["fg3"], bg=C["card"])
        arrow_lbl.pack(side="left", padx=(0, 8))

        title_lbl = tk.Label(head, text="Compiled custom tools",
            font=C["f_b"], fg=C["fg"], bg=C["card"])
        title_lbl.pack(side="left")

        # Badge count
        n = len(compiled)
        badge_color = C["blue_bg"] if n > 0 else C.get("warn_bg", C["card"])
        badge_fg    = C["blue"]    if n > 0 else C["fg3"]
        badge_text  = f" {n} compiled " if n > 0 else " belum di-compile "
        tk.Label(head, text=badge_text, font=C["f_ui9"],
                 bg=badge_color, fg=badge_fg, padx=4, pady=2
                 ).pack(side="left", padx=(10, 0))

        tk.Label(head,
                 text="hasil generate model — alias, deskripsi, simulasi output, trigger phrases",
                 font=C["f_ui9"], fg=C["fg3"], bg=C["card"]
                 ).pack(side="left", padx=(10, 0))

        # Tombol refresh kecil di kanan
        _plain_btn(head, "↺", self._refresh_compiled_panel
                   ).pack(side="right")

        def _toggle_compiled(_e=None):
            self._compiled_expanded = not self._compiled_expanded
            self._render_all()
        for w in (head, arrow_lbl, title_lbl):
            w.bind("<Button-1>", _toggle_compiled)

        if not self._compiled_expanded:
            return

        _hline(card, color=C["bdr"])

        if not compiled:
            tk.Label(card,
                     text="  Belum ada compiled tools. Klik ▶ Compile Tools di atas.",
                     font=C["f_ui9"], fg=C["fg3"], bg=C["card"],
                     anchor="w", pady=10
                     ).pack(fill="x", padx=14, pady=(6, 12))
            return

        body = tk.Frame(card, bg=C["card"])
        body.pack(fill="x", padx=14, pady=(6, 12))

        # ── Per-tool rows ───────────────────────────────────────────────────
        for tool_name, meta in compiled.items():
            alias     = meta.get("alias", "?")
            short     = meta.get("short_desc", "(no description)")
            sim       = meta.get("sim_result", "")
            triggers  = meta.get("trigger_phrases", [])
            category  = meta.get("category", "custom")
            is_llm    = not meta.get("fallback", False)

            # ── Tool card mini ──────────────────────────────────────────────
            tcard = tk.Frame(body, bg=C.get("field", C["card"]),
                             highlightthickness=1,
                             highlightbackground=C["bdr"])
            tcard.pack(fill="x", pady=(0, 8))

            # Row 1: alias + nama + category badge + LLM badge
            r1 = tk.Frame(tcard, bg=tcard["bg"])
            r1.pack(fill="x", padx=10, pady=(8, 2))

            tk.Label(r1, text=alias, font=C["f_m10"], fg=C["acc"],
                     bg=tcard["bg"], width=5, anchor="w"
                     ).pack(side="left")
            tk.Label(r1, text=tool_name, font=C["f_b"], fg=C["fg"],
                     bg=tcard["bg"]
                     ).pack(side="left", padx=(4, 0))

            # Category badge
            tk.Label(r1, text=f" {category} ", font=C["f_ui9"],
                     bg=C["blue_bg"], fg=C["blue"], padx=3, pady=1
                     ).pack(side="left", padx=(10, 0))

            # "Aktif" badge — compiled = enabled (tidak ada lagi toggle manual)
            tk.Label(r1, text=" ● aktif ", font=C["f_ui9"],
                     bg=C.get("green_bg", C["blue_bg"]), fg=C.get("green", C["acc"]),
                     padx=3, pady=1
                     ).pack(side="left", padx=(6, 0))

            # LLM/fallback badge
            gen_text  = " ✦ LLM " if is_llm else " ⚙ fallback "
            gen_color = C["acc"]   if is_llm else C["fg3"]
            gen_bg    = C.get("acc_bg", C["card"]) if is_llm else C["card"]
            tk.Label(r1, text=gen_text, font=C["f_ui9"],
                     bg=gen_bg, fg=gen_color, padx=3, pady=1
                     ).pack(side="left", padx=(6, 0))

            # Row 2: short description
            r2 = tk.Frame(tcard, bg=tcard["bg"])
            r2.pack(fill="x", padx=10, pady=(0, 2))
            tk.Label(r2, text="desc  ", font=C["f_ui9"], fg=C["fg3"],
                     bg=tcard["bg"], width=7, anchor="w"
                     ).pack(side="left")
            tk.Label(r2, text=short, font=C["f_ui9"], fg=C["fg2"],
                     bg=tcard["bg"], anchor="w", wraplength=560, justify="left"
                     ).pack(side="left", fill="x")

            # Row 3: sim_result
            if sim:
                r3 = tk.Frame(tcard, bg=tcard["bg"])
                r3.pack(fill="x", padx=10, pady=(0, 2))
                tk.Label(r3, text="sim   ", font=C["f_ui9"], fg=C["fg3"],
                         bg=tcard["bg"], width=7, anchor="w"
                         ).pack(side="left")
                tk.Label(r3, text=sim[:120], font=("Consolas", 9), fg=C["fg3"],
                         bg=tcard["bg"], anchor="w", wraplength=560, justify="left"
                         ).pack(side="left", fill="x")

            # Row 4: trigger phrases
            if triggers:
                r4 = tk.Frame(tcard, bg=tcard["bg"])
                r4.pack(fill="x", padx=10, pady=(0, 8))
                tk.Label(r4, text="trigger", font=C["f_ui9"], fg=C["fg3"],
                         bg=tcard["bg"], width=7, anchor="w"
                         ).pack(side="left")
                # Chip-style tags
                for phrase in triggers[:6]:
                    tk.Label(r4, text=f" {phrase} ", font=C["f_ui9"],
                             bg=C["bdr"], fg=C["fg2"], padx=4, pady=1
                             ).pack(side="left", padx=(0, 4))

    def _refresh_compiled_panel(self):
        """Refresh hanya compiled panel tanpa re-render semua cards."""
        if hasattr(self, "_compiled_frame") and self._compiled_frame.winfo_exists():
            self._compiled_frame.destroy()
        self._render_compiled_panel()
        self._refresh_compile_status()

    # ── Built-in functions reference (read-only) ────────────────────
    def _rebuild_builtin(self):
        """Rebuild only the builtin reference panel — called by collapse/expand toggle."""
        if hasattr(self, "_builtin_frame") and self._builtin_frame.winfo_exists():
            self._builtin_frame.destroy()
        self._render_builtin_panel()

    def _render_builtin_panel(self):
        n_total = sum(len(fns) for _, fns in BUILTIN_ROUTER_FUNCS)

        card = tk.Frame(self._inner, bg=C["card"],
                        highlightthickness=1, highlightbackground=C["bdr"])
        self._builtin_frame = card   # ref for targeted rebuild
        card.pack(fill="x", padx=20, pady=(16, 14))

        head = tk.Frame(card, bg=C["card"], cursor="hand2")
        head.pack(fill="x", padx=14, pady=10)
        arrow = tk.Label(head, text=("▾" if self._builtin_expanded else "▸"),
                         font=C["f_ui10"], fg=C["fg3"], bg=C["card"])
        arrow.pack(side="left", padx=(0, 8))
        title = tk.Label(head, text="Fungsi bawaan (mcp_tools.py)",
                         font=C["f_b"], fg=C["fg"], bg=C["card"])
        title.pack(side="left")
        tk.Label(head, text=f" {n_total} functions ", font=C["f_ui9"],
                 bg=C["blue_bg"], fg=C["blue"], padx=4, pady=2
                 ).pack(side="left", padx=(10, 0))
        tk.Label(head, text="read-only — daftar fungsi router yang sudah ada,"
                            " supaya nama tool baru kamu tidak bentrok",
                 font=C["f_ui9"], fg=C["fg3"], bg=C["card"]
                 ).pack(side="left", padx=(10, 0))

        def _toggle(_e=None):
            self._builtin_expanded = not self._builtin_expanded
            self._render_all()
        for w in (head, arrow, title):
            w.bind("<Button-1>", _toggle)

        if not self._builtin_expanded:
            return

        _hline(card, color=C["bdr"])
        body = tk.Frame(card, bg=C["card"])
        body.pack(fill="x", padx=14, pady=(6, 12))

        for cat, fns in BUILTIN_ROUTER_FUNCS:
            tk.Label(body, text=cat.upper(), font=C["f_ui9"], fg=C["fg3"], bg=C["card"]
                     ).pack(anchor="w", pady=(8, 2))
            for code, name, desc in fns:
                row = tk.Frame(body, bg=C["card"])
                row.pack(fill="x", pady=1)
                tk.Label(row, text=code, font=C["f_m10"], fg=C["acc"], bg=C["card"],
                         width=6, anchor="w").pack(side="left")
                if name:
                    tk.Label(row, text=name, font=C["f_m10"], fg=C["fg2"], bg=C["card"],
                             width=24, anchor="w").pack(side="left")
                else:
                    tk.Label(row, text="(belum diimplementasi)", font=("Consolas", 10, "italic"),
                             fg=C["fg3"], bg=C["card"], width=24, anchor="w").pack(side="left")
                tk.Label(row, text=desc, font=C["f_ui9"], fg=C["fg3"], bg=C["card"],
                         anchor="w", wraplength=420, justify="left").pack(side="left", fill="x", expand=True)

    # ── Render ───────────────────────────────────────────────────
    def _render_all(self):
        for var, tid in self._trace_ids:
            try: var.trace_remove("write", tid)
            except Exception: pass
        self._trace_ids.clear()
        for w in self._inner.winfo_children(): w.destroy()
        self._cards.clear()
        self._render_builtin_panel()
        self._render_compiled_panel()
        if not self.app.mcp_tools:
            tk.Label(self._inner,
                     text="Belum ada tool kustom. Klik \"+ Add tool\" untuk membuat satu.",
                     font=C["f_ui10"], fg=C["fg3"], bg=C["main"]
                     ).pack(anchor="w", padx=20, pady=20)
        for i in range(len(self.app.mcp_tools)):
            self._render_tool(i)
        tk.Frame(self._inner, bg=C["main"], height=20).pack()

    def _render_tool(self, i):
        tool = self.app.mcp_tools[i]
        name = str(tool.get("name") or "untitled")
        ttype = str(tool.get("type") or "custom")

        card = tk.Frame(self._inner, bg=C["card"],
                        highlightthickness=1, highlightbackground=C["bdr"])
        card.pack(fill="x", padx=20, pady=(0, 10))

        head = tk.Frame(card, bg=C["card"])
        head.pack(fill="x", padx=14, pady=10)

        name_lbl = tk.Label(head, text=name, font=C["f_b"], fg=C["fg"], bg=C["card"])
        name_lbl.pack(side="left", padx=(0, 8))

        badge = tk.Label(head, text=f" {ttype} ", font=C["f_ui9"],
                         bg=C["blue_bg"], fg=C["blue"], padx=4, pady=2)
        badge.pack(side="left")

        bframe = tk.Frame(head, bg=C["card"])
        bframe.pack(side="right")
        _icon_btn(bframe, "✓", lambda ii=i: self._apply(ii)
                  ).pack(side="left", padx=(0, 4))
        _icon_btn(bframe, "⧉", lambda ii=i: self._dup(ii)
                  ).pack(side="left", padx=(0, 4))
        _icon_btn(bframe, "✕", lambda ii=i: self._remove(ii), danger=True
                  ).pack(side="left")

        _hline(card, color=C["bdr"])

        body = tk.Frame(card, bg=C["card"])
        body.pack(fill="x", padx=14, pady=10)

        txt = tk.Text(body, font=C["f_m10"], height=10, wrap="none",
                      bg=C["field"], fg=C["fg"], insertbackground=C["fg"],
                      relief="flat", bd=0, highlightthickness=1,
                      highlightbackground=C["bdr"], highlightcolor=C["acc"],
                      padx=10, pady=8)
        txt.insert("1.0", json.dumps(tool, indent=2, ensure_ascii=False))
        txt.pack(fill="x")

        err_lbl = tk.Label(body, text="", font=C["f_ui9"], fg=C["red"],
                           bg=C["card"], anchor="w")
        err_lbl.pack(fill="x", pady=(6, 0))

        self._cards[i] = {"text": txt, "err": err_lbl, "name_lbl": name_lbl, "badge": badge}

    # ── Actions ──────────────────────────────────────────────────
    def _apply(self, i):
        card = self._cards.get(i)
        if not card: return
        raw = card["text"].get("1.0", "end").strip()
        try:
            parsed = json.loads(raw)
        except Exception as e:
            card["err"].config(text=f"Invalid JSON: {e}")
            return
        if not isinstance(parsed, dict) or not parsed.get("name"):
            card["err"].config(text="JSON harus berupa object dan punya field \"name\".")
            return
        card["err"].config(text="")
        self.app.mcp_tools[i] = parsed
        card["name_lbl"].config(text=str(parsed.get("name")))
        card["badge"].config(text=f" {parsed.get('type','custom')} ")
        self.app.mark_dirty()

    def _dup(self, i):
        if 0 <= i < len(self.app.mcp_tools):
            new_tool = dict(self.app.mcp_tools[i])
            new_tool["name"] = str(new_tool.get("name","tool")) + "_copy"
            self.app.mcp_tools.insert(i + 1, new_tool)
            self._render_all(); self.app.mark_dirty()

    def _remove(self, i):
        if 0 <= i < len(self.app.mcp_tools):
            name = self.app.mcp_tools[i].get("name", "tool")
            if messagebox.askyesno("Remove tool", f"Remove '{name}'?"):
                del self.app.mcp_tools[i]
                self._render_all(); self.app.mark_dirty()

    def _add(self):
        self.app.mcp_tools.append({
            "name": f"new_tool_{len(self.app.mcp_tools)+1}",
            "description": "Deskripsikan tool ini di sini.",
            "category": "custom",
            "type": "http",
            "method": "GET",
            "url": "https://api.example.com/endpoint?param={param}",
            "headers": {},
            "body": None,
            "params": [
                {"name": "param", "type": "string", "required": True, "description": ""}
            ]
        })
        # Nilai "category" yang valid:
        # user | chat | state | event | game | social | self | meta | custom
        self._render_all(); self.app.mark_dirty()

    def _import(self):
        path = filedialog.askopenfilename(
            title="Import MCP tools JSON",
            filetypes=[("JSON files", "*.json"), ("All files", "*.*")])
        if not path: return
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            tools = data.get("tools") if isinstance(data, dict) else data
            if not isinstance(tools, list):
                raise ValueError("File harus berisi list tool atau {\"tools\": [...]}")
            self.app.mcp_tools.extend(t for t in tools if isinstance(t, dict))
            self._render_all(); self.app.mark_dirty()
        except Exception as e:
            messagebox.showerror("Import failed", str(e))

    def _export(self):
        path = filedialog.asksaveasfilename(
            title="Export MCP tools JSON", defaultextension=".json",
            initialfile="mcp_custom_tools.json",
            filetypes=[("JSON files", "*.json"), ("All files", "*.*")])
        if not path: return
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump({"tools": self.app.mcp_tools}, f, indent=2, ensure_ascii=False)
        except Exception as e:
            messagebox.showerror("Export failed", str(e))

    def collect(self):
        # Make sure any unapplied edits still in the text boxes aren't silently
        # dropped on Save All — apply everything currently shown first.
        for i in list(self._cards.keys()):
            self._apply(i)
        return list(self.app.mcp_tools)

    def refresh(self): self._render_all()

# ══════════════════════════════════════════════════════════════════
# TAB: CHARACTERS
# ══════════════════════════════════════════════════════════════════
def _find_character_folder():
    """Cari folder karakter — coba beberapa nama umum relatif terhadap ROOT."""
    for candidate in ("character", "characters", "chars", "char"):
        p = _fp(candidate)
        if os.path.isdir(p):
            return p
    return _fp("character")   # fallback default meski belum ada


def _scan_characters(folder: str):
    """
    Scan folder karakter.  Kembalikan list of dict:
        {"name": str, "path": str, "data": dict|None}
    Pola yang didukung:
      1. <folder>/<nama>/character.json   (pola CharacterManager — prioritas utama)
      2. <folder>/<nama>.json             (file JSON langsung di root folder)
    """
    results = []
    if not os.path.isdir(folder):
        return results
    for entry in sorted(os.scandir(folder), key=lambda e: e.name.lower()):
        if entry.is_dir():
            cfile = os.path.join(entry.path, "character.json")
            if os.path.isfile(cfile):
                data = None
                try:
                    with open(cfile, encoding="utf-8") as f:
                        data = json.load(f)
                except Exception:
                    pass
                results.append({"name": entry.name, "path": cfile, "data": data})
        elif entry.is_file() and entry.name.endswith(".json") and entry.name != "character.json":
            data = None
            try:
                with open(entry.path, encoding="utf-8") as f:
                    data = json.load(f)
            except Exception:
                pass
            name = entry.name[:-5]
            results.append({"name": name, "path": entry.path, "data": data})
    return results


class TabCharacters(tk.Frame):
    """
    Tab Characters — menampilkan daftar karakter yang ditemukan
    di folder `characters/` (atau folder lain yang dipilih user).
    Klik karakter → preview field-field utama dari character.json.
    """

    _DEFAULT_FOLDER = "character"

    def __init__(self, master, app):
        super().__init__(master, bg=C["main"])
        self.app = app
        self._chars: list = []
        self._sel_idx: int = -1
        self._folder_var = tk.StringVar(value=_find_character_folder())
        self._build()
        self._refresh()

    # ── Layout ───────────────────────────────────────────────────
    def _build(self):
        # ── Sub-header row ──────────────────────────────────────
        bar = tk.Frame(self, bg=C["main"])
        bar.pack(fill="x", padx=20, pady=(16, 8))
        tk.Label(bar, text="CHARACTER LIST",
                 font=C["f_ui9"], fg=C["fg3"], bg=C["main"]
                 ).pack(side="left")
        _plain_btn(bar, "⟳ Refresh", self._refresh).pack(side="right")
        _plain_btn(bar, "📂 Folder…", self._pick_folder).pack(side="right", padx=(0, 6))

        # ── Folder path row ─────────────────────────────────────
        frow = tk.Frame(self, bg=C["main"])
        frow.pack(fill="x", padx=20, pady=(0, 10))
        tk.Label(frow, text="Folder:", font=C["f_ui9"], fg=C["fg2"],
                 bg=C["main"]).pack(side="left")
        tk.Label(frow, textvariable=self._folder_var,
                 font=C["f_m10"], fg=C["fg3"], bg=C["main"],
                 anchor="w").pack(side="left", padx=(8, 0))

        _hline(self, color=C["bdr"], padx=20, pady=(0, 0))

        # ── Two-pane: list (left) + detail (right) ───────────────
        pane = tk.Frame(self, bg=C["main"])
        pane.pack(fill="both", expand=True, padx=20, pady=12)

        # Left list
        list_frame = tk.Frame(pane, bg=C["card"],
                              highlightthickness=1,
                              highlightbackground=C["bdr"],
                              width=210)
        list_frame.pack(side="left", fill="y")
        list_frame.pack_propagate(False)

        tk.Label(list_frame, text="Characters",
                 font=C["f_ui9"], fg=C["fg3"], bg=C["card"],
                 anchor="w", padx=12, pady=8
                 ).pack(fill="x")
        _hline(list_frame, color=C["bdr"])

        self._listbox = tk.Listbox(
            list_frame,
            font=C["f_ui"],
            bg=C["card"], fg=C["fg"],
            selectbackground=C["sel"],
            selectforeground=C["fg_sel"],
            activestyle="none",
            relief="flat", bd=0,
            highlightthickness=0,
        )
        self._listbox.pack(fill="both", expand=True, padx=4, pady=4)
        self._listbox.bind("<<ListboxSelect>>", self._on_select)

        # Right detail
        detail_outer = tk.Frame(pane, bg=C["main"])
        detail_outer.pack(side="left", fill="both", expand=True, padx=(12, 0))

        self._detail_header = tk.Label(
            detail_outer, text="Select a character to preview",
            font=C["f_h"], fg=C["fg2"], bg=C["main"], anchor="w")
        self._detail_header.pack(fill="x", pady=(0, 8))

        # Scrollable detail area
        sf = ScrollFrame(detail_outer, bg=C["main"])
        sf.pack(fill="both", expand=True)
        self._detail_inner = sf.inner

    # ── Actions ──────────────────────────────────────────────────
    def _pick_folder(self):
        d = filedialog.askdirectory(
            title="Pilih folder karakter",
            initialdir=self._folder_var.get())
        if d:
            self._folder_var.set(d)
            self._refresh()

    def _refresh(self):
        folder = self._folder_var.get()
        self._chars = _scan_characters(folder)
        self._listbox.delete(0, "end")
        for ch in self._chars:
            self._listbox.insert("end", f"  {ch['name']}")
        self._sel_idx = -1
        self._clear_detail()
        if not self._chars:
            self._detail_header.config(
                text=f"Tidak ada karakter di:\n{folder}",
                fg=C["fg3"])

    def _on_select(self, event=None):
        sel = self._listbox.curselection()
        if not sel:
            return
        idx = sel[0]
        if idx == self._sel_idx:
            return
        self._sel_idx = idx
        ch = self._chars[idx]
        self._render_detail(ch)

    # ── Detail renderer ──────────────────────────────────────────
    def _clear_detail(self):
        for w in self._detail_inner.winfo_children():
            w.destroy()
        self._detail_header.config(
            text="Select a character to preview", fg=C["fg2"])

    def _render_detail(self, ch: dict):
        for w in self._detail_inner.winfo_children():
            w.destroy()

        name = ch["name"]
        data = ch["data"]
        path = ch["path"]

        self._detail_header.config(
            text=f"✦  {name}", fg=C["fg"])

        if data is None:
            tk.Label(self._detail_inner,
                     text="⚠ Gagal membaca character.json",
                     font=C["f_ui"], fg=C["red"], bg=C["main"]
                     ).pack(anchor="w", pady=4)
            return

        def _row(label, value, mono=False):
            row = tk.Frame(self._detail_inner, bg=C["main"])
            row.pack(fill="x", pady=2)
            tk.Label(row, text=f"{label}:", width=18,
                     font=C["f_ui9"], fg=C["fg3"], bg=C["main"],
                     anchor="w").pack(side="left")
            tk.Label(row, text=str(value)[:120],
                     font=C["f_m10"] if mono else C["f_ui"],
                     fg=C["fg"], bg=C["main"], anchor="w",
                     wraplength=460, justify="left"
                     ).pack(side="left", fill="x", expand=True)

        def _section(title):
            tk.Label(self._detail_inner, text=title.upper(),
                     font=C["f_ui9"], fg=C["fg3"], bg=C["main"],
                     anchor="w"
                     ).pack(fill="x", pady=(12, 2))
            _hline(self._detail_inner, color=C["bdr"])

        # ── Path ─────────────────────────────────────────────────
        _row("Path", path, mono=True)

        # ── Basic fields ─────────────────────────────────────────
        _section("Info")
        for key in ("name", "admin_user_id", "version", "language"):
            if key in data:
                _row(key, data[key])

        # ── Prompts ──────────────────────────────────────────────
        prompts = data.get("prompts", {})
        if prompts:
            _section(f"Prompts  ({len(prompts)} keys)")
            for k, v in prompts.items():
                preview = str(v).replace("\n", " ")[:120]
                _row(k, preview + ("…" if len(str(v)) > 120 else ""), mono=False)

        # ── Trans few-shot ───────────────────────────────────────
        few = data.get("trans_few_shot", [])
        if few:
            _section(f"Trans few-shot  ({len(few)} examples)")
            for i, ex in enumerate(few[:5], 1):
                tk.Label(self._detail_inner,
                         text=f"  [{i}] {str(ex)[:100]}",
                         font=C["f_m10"], fg=C["fg2"],
                         bg=C["main"], anchor="w"
                         ).pack(fill="x")

        # ── Other top-level keys ──────────────────────────────────
        skip = {"name", "admin_user_id", "version", "language",
                "prompts", "trans_few_shot"}
        extras = {k: v for k, v in data.items() if k not in skip}
        if extras:
            _section("Other fields")
            for k, v in extras.items():
                if isinstance(v, (dict, list)):
                    _row(k, f"[{type(v).__name__}  {len(v)} items]")
                else:
                    _row(k, v)

        # ── Open file button ─────────────────────────────────────
        btn_row = tk.Frame(self._detail_inner, bg=C["main"])
        btn_row.pack(fill="x", pady=(14, 4))
        _plain_btn(btn_row, "📋  Copy path",
                   lambda p=path: (self.clipboard_clear(),
                                   self.clipboard_append(p))
                   ).pack(side="left", padx=(0, 8))
        _plain_btn(btn_row, "📂  Open folder",
                   lambda p=os.path.dirname(path):
                       os.startfile(p) if sys.platform == "win32" else
                       os.system(f'xdg-open "{p}"')
                   ).pack(side="left")

    def collect(self):
        return {}  # characters tab tidak menyimpan ke file config


# ══════════════════════════════════════════════════════════════════
# TAB: PREVIEW
# ══════════════════════════════════════════════════════════════════
class TabPreview(tk.Frame):
    def __init__(self, master, app):
        super().__init__(master, bg=C["main"])
        self.app = app
        bar = tk.Frame(self, bg=C["main"])
        bar.pack(fill="x", padx=20, pady=(14,8))
        tk.Label(bar, text="CONFIG.PY PREVIEW",
                 font=C["f_ui9"], fg=C["fg3"], bg=C["main"]
                 ).pack(side="left")
        _plain_btn(bar, "Copy", self._copy).pack(side="right")

        outer = tk.Frame(self, bg=C["card"],
                         highlightthickness=1,
                         highlightbackground=C["bdr"])
        outer.pack(fill="both", expand=True, padx=20, pady=(0,20))

        self._txt = tk.Text(outer,
                            bg=C["field"], fg=C["fg2"],
                            font=C["f_m10"],
                            relief="flat", bd=0,
                            wrap="none",
                            insertbackground=C["fg"],
                            selectbackground=C["sel"],
                            selectforeground=C["fg"],
                            state="disabled",
                            padx=14, pady=12)
        sb = tk.Scrollbar(outer, command=self._txt.yview,
                          bg=C["main"], troughcolor=C["main"],
                          relief="flat", bd=0, width=5)
        self._txt.config(yscrollcommand=sb.set)
        self._txt.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")

    def refresh(self, src):
        self._txt.config(state="normal")
        self._txt.delete("1.0","end")
        self._txt.insert("1.0", src)
        self._txt.config(state="disabled")

    def _copy(self):
        s = self._txt.get("1.0","end")
        self.clipboard_clear(); self.clipboard_append(s)

# ══════════════════════════════════════════════════════════════════
# SIDEBAR NAV ITEM
# ══════════════════════════════════════════════════════════════════
class TabButton(tk.Frame):
    """Horizontal top tab button (replaces the old vertical sidebar nav item)."""
    def __init__(self, parent, icon, label, key, on_click):
        super().__init__(parent, bg=C["win"], cursor="hand2")
        self._key   = key
        self._click = on_click
        self._active = False

        self._row = tk.Frame(self, bg=C["win"])
        self._row.pack(fill="x", padx=14, pady=(10, 8))

        self._icon_lbl = tk.Label(self._row, text=icon,
                                  font=("Segoe UI", 12),
                                  fg=C["fg_nav"], bg=C["win"])
        self._icon_lbl.pack(side="left", padx=(0, 6))

        self._text_lbl = tk.Label(self._row, text=label,
                                  font=C["f_ui10"],
                                  fg=C["fg_nav"], bg=C["win"])
        self._text_lbl.pack(side="left")

        self._underline = tk.Frame(self, bg=C["win"], height=2)
        self._underline.pack(fill="x")

        for w in (self, self._row, self._icon_lbl, self._text_lbl):
            w.bind("<Button-1>", lambda e: on_click(key))
            w.bind("<Enter>",    self._hover_on)
            w.bind("<Leave>",    self._hover_off)

    def _hover_on(self, e):
        if self._active: return
        self._text_lbl.config(fg=C["fg"])
        self._icon_lbl.config(fg=C["fg"])

    def _hover_off(self, e):
        if self._active: return
        self._text_lbl.config(fg=C["fg_nav"])
        self._icon_lbl.config(fg=C["fg_nav"])

    def set_active(self, active: bool):
        self._active = active
        fg = C["fg_sel"] if active else C["fg_nav"]
        self._text_lbl.config(fg=fg)
        self._icon_lbl.config(fg=fg)
        self._underline.config(bg=C["acc"] if active else C["win"])

# ══════════════════════════════════════════════════════════════════
# MAIN APP
# ══════════════════════════════════════════════════════════════════
class SettingsApp:
    NAV = [
        ("CONFIG",   None,         None),
        ("⊟",       "Endpoints",   "endpoints"),
        ("⇌",       "Pass routing","routing"),
        ("⚡",       "Model & memory","tuning"),
        ("RUNTIME",  None,         None),
        ("⊙",       "Live server", "live"),
        ("♪",       "TTS",         "tts"),
        ("TOOLS",    None,         None),
        ("⚒",       "MCP tools",   "mcp"),
        ("♟",       "Characters",  "characters"),
        ("⌥",       "Preview",     "preview"),
    ]
    META = {
        "endpoints":("Endpoints",     "config.py — registered model endpoints"),
        "routing":  ("Pass routing",  "config.py — pipeline pass → endpoint mapping"),
        "tuning":   ("Model & memory","config.py — tuning constants"),
        "live":     ("Live server",   "liveServer.py — runtime settings"),
        "tts":      ("TTS",           "liveDesktop.py — text-to-speech settings"),
        "mcp":        ("MCP tools",    "mcp_custom_tools.json — custom tool/function definitions"),
        "characters": ("Characters",  "characters/ — list & preview karakter yang tersedia"),
        "preview":    ("Preview",     "config.py — generated output"),
    }

    def __init__(self):
        self.root = tk.Tk()
        self.root.title("Settings — Live System")
        self.root.geometry("960x700")
        self.root.minsize(860, 560)
        self.root.configure(bg=C["win"])

        self.endpoints:   Dict[str,Dict] = {}
        self.routing:     Dict[str,str]  = {}
        self.config_vals: Dict[str,Any]  = {}
        self.server_vals: Dict[str,Any]  = {}
        self.tts_vals:    Dict[str,Any]  = {}
        self.mcp_tools:   List[Dict]     = []
        self._raw_vars: set              = set()   # vars whose source is a code
                                                     # expression, not a literal
                                                     # (e.g. AUDIO_PATH = os.path.join(...))
        self._dirty  = False
        self._cur    = ""
        self._navitems: Dict[str, TabButton] = {}

        self._load_all()
        self._build()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    # ── Load ─────────────────────────────────────────────────────
    def _load_all(self):
        self._raw_vars = set()
        src = read_file(_fp("config.py"))
        self.endpoints   = read_endpoints(src) or dict(_DEFAULT_EP)
        self.routing     = read_routing(src)   or dict(_DEFAULT_RT)
        for v in ["STORAGE_DIR","MEMORY_DIR","MODEL_MEMORY_DIR",
                  "DEBUG","MAX_HISTORY","MAX_INFO","MAX_NOTES",
                  "MAX_GIFTS","MAX_SEGMENTS"]:
            self.config_vals[v] = read_var(src, v)
            if not is_literal_var(src, v): self._raw_vars.add(v)

        srv = read_file(_fp("liveServer.py"))
        for v in ["WS_APP_URL","L2D_HTTP_URL","L2D_WS_URL",
                  "SPAM_MSG_LIMIT","SPAM_WINDOW_SEC","SPAM_COOLDOWN_SEC",
                  "DEDUP_WINDOW_SEC","BATCH_WINDOW_SEC",
                  "GIFT_TRIGGER_MIN","GIFT_TRIGGER_MAX",
                  "MAX_QUEUE_SIZE","QUEUE_RANDOM_FROM",
                  "CLEANUP_INTERVAL","USER_CACHE_LIMIT",
                  "L2D_MODEL_ID","L2D_MODEL_MAP"]:
            self.server_vals[v] = read_var(srv, v)
            if not is_literal_var(srv, v): self._raw_vars.add(v)

        tts = read_file(_fp("liveDesktop.py"))
        for v in ["TTS_SERVER_URL","TTS_MODEL","AUDIO_PATH"]:
            self.tts_vals[v] = read_var(tts, v)
            if not is_literal_var(tts, v): self._raw_vars.add(v)
        m = re.search(r'TTS_PARAMS\s*=\s*(\{.*?\})', tts, re.DOTALL)
        if m:
            try:
                p = ast.literal_eval(m.group(1))
                for k in ["noise_scale","noise_scale_w","length_scale"]:
                    self.tts_vals[k] = p.get(k)
            except: pass

        self.mcp_tools = read_mcp_tools(_fp(MCP_TOOLS_FILE))
        if self.mcp_tools is None:
            self.mcp_tools = [dict(t) for t in _DEFAULT_MCP_TOOLS]

    # ── Build UI ─────────────────────────────────────────────────
    def _build(self):
        # ── TOPBAR (icon + dynamic title/sub on left, actions on right) ──
        topbar = tk.Frame(self.root, bg=C["win"], height=60)
        topbar.pack(fill="x")
        topbar.pack_propagate(False)

        tbl = tk.Frame(topbar, bg=C["win"])
        tbl.pack(side="left", padx=18, fill="y")
        gear2 = tk.Frame(tbl, bg=C["acc"], width=28, height=28)
        gear2.pack(side="left", pady=16)
        gear2.pack_propagate(False)
        tk.Label(gear2, text="⚙", font=("Segoe UI",13),
                 fg="#fff", bg=C["acc"]
                 ).place(relx=.5, rely=.5, anchor="center")
        tf = tk.Frame(tbl, bg=C["win"])
        tf.pack(side="left", padx=10, pady=10)
        self._tb_title = tk.Label(tf, text="Endpoints",
                                  font=C["f_b"], fg=C["fg"], bg=C["win"])
        self._tb_title.pack(anchor="w")
        self._tb_sub   = tk.Label(tf, text="config.py — registered model endpoints",
                                  font=C["f_ui9"], fg=C["fg3"], bg=C["win"])
        self._tb_sub.pack(anchor="w")

        tbr = tk.Frame(topbar, bg=C["win"])
        tbr.pack(side="right", padx=16, fill="y")
        _plain_btn(tbr, "↓ Export config.py", self._export_config
                   ).pack(side="right", pady=16, padx=(8,0))
        _accent_btn(tbr, "Save all", self._save_all
                    ).pack(side="right", pady=16, padx=(8,0))
        _plain_btn(tbr, "Reload from disk", self._reload
                   ).pack(side="right", pady=16, padx=(8,0))
        self._status_lbl = tk.Label(tbr, text="",
                                    font=C["f_ui9"], fg=C["amber"],
                                    bg=C["win"])
        self._status_lbl.pack(side="right", padx=(8,0))

        _hline(self.root, color=C["bdr"])

        # ── TAB BAR (horizontal — replaces old vertical sidebar) ──
        tabbar = tk.Frame(self.root, bg=C["win"])
        tabbar.pack(fill="x")
        tabbar_inner = tk.Frame(tabbar, bg=C["win"])
        tabbar_inner.pack(side="left", padx=10)

        for icon, label, key in self.NAV:
            if key is None:
                continue  # group labels dropped in the flat top tab bar
            item = TabButton(tabbar_inner, icon, label, key, self._show)
            item.pack(side="left")
            self._navitems[key] = item

        _hline(self.root, color=C["bdr"])

        # ── MAIN AREA (full width now, no sidebar) ────────────────
        main_outer = tk.Frame(self.root, bg=C["main"])
        main_outer.pack(side="left", fill="both", expand=True)

        # Content
        self.content = tk.Frame(main_outer, bg=C["main"])
        self.content.pack(fill="both", expand=True)

        self._tabs: Dict[str, tk.Frame] = {
            "endpoints": TabEndpoints(self.content, self),
            "routing":   TabRouting(self.content, self),
            "tuning":    GenericTab(self.content, self, TUNING_SECTIONS, "config_vals"),
            "live":      GenericTab(self.content, self, LIVE_SECTIONS,   "server_vals"),
            "tts":       GenericTab(self.content, self, TTS_SECTIONS,    "tts_vals"),
            "mcp":        TabMCP(self.content, self),
            "characters": TabCharacters(self.content, self),
            "preview":    TabPreview(self.content, self),
        }
        for t in self._tabs.values():
            t.place(relwidth=1, relheight=1)

        self._show("endpoints")

    # ── Navigation ───────────────────────────────────────────────
    def _show(self, key):
        self._cur = key
        for k, item in self._navitems.items():
            item.set_active(k == key)
        for k, t in self._tabs.items():
            t.lift() if k == key else t.lower()
        title, sub = self.META.get(key, (key,""))
        self._tb_title.config(text=title)
        self._tb_sub.config(text=sub)
        if key == "preview":
            self._tabs["preview"].refresh(self._gen_config())

    # ── Dirty ────────────────────────────────────────────────────
    def mark_dirty(self):
        self._dirty = True
        self._status_lbl.config(text="● Unsaved changes", fg=C["amber"])

    def _mark_saved(self):
        self._dirty = False
        self._status_lbl.config(text="✓ Saved", fg=C["green"])
        self.root.after(3000, lambda: self._status_lbl.config(text=""))

    # ── Save ─────────────────────────────────────────────────────
    def _save_all(self):
        self.endpoints = self._tabs["endpoints"].collect()
        self.routing   = self._tabs["routing"].collect()
        cfg = self._tabs["tuning"].collect()
        srv = self._tabs["live"].collect()
        tts = self._tabs["tts"].collect()
        self.mcp_tools = self._tabs["mcp"].collect()
        errors = []

        try:
            src = read_file(_fp("config.py"))
            src = write_endpoints_block(src, self.endpoints)
            src = write_routing_block(src, self.routing)
            for k, v in cfg.items():
                src = patch_var(src, k, v, raw=(k in self._raw_vars))
            write_file(_fp("config.py"), src)
        except Exception as e: errors.append(f"config.py: {e}")

        if os.path.exists(_fp("liveServer.py")):
            try:
                src = read_file(_fp("liveServer.py"))
                for k, v in srv.items():
                    src = patch_var(src, k, v, raw=(k in self._raw_vars))
                write_file(_fp("liveServer.py"), src)
            except Exception as e: errors.append(f"liveServer.py: {e}")

        if os.path.exists(_fp("liveDesktop.py")):
            try:
                src = read_file(_fp("liveDesktop.py"))
                for k in ["TTS_SERVER_URL","TTS_MODEL","AUDIO_PATH"]:
                    if k in tts: src = patch_var(src, k, tts[k], raw=(k in self._raw_vars))
                for k in ["noise_scale","noise_scale_w","length_scale"]:
                    if k in tts: src = patch_dict_key(src, k, tts[k])
                write_file(_fp("liveDesktop.py"), src)
            except Exception as e: errors.append(f"liveDesktop.py: {e}")

        try:
            write_mcp_tools(_fp(MCP_TOOLS_FILE), self.mcp_tools)
            # Invalidate cache custom tools supaya task_router pakai data terbaru
            try:
                import mcp_tools as _mcp_mod
                _mcp_mod._custom_tools_cache = None
            except Exception:
                pass
        except Exception as e: errors.append(f"{MCP_TOOLS_FILE}: {e}")

        if errors: messagebox.showerror("Save errors", "\n".join(errors))
        else:
            self._mark_saved()
            self._tabs["routing"].refresh()

    # ── Reload ───────────────────────────────────────────────────
    def _reload(self):
        if self._dirty:
            if not messagebox.askyesno("Unsaved changes","Discard and reload?"): return
        self._load_all()
        for t in self._tabs.values(): t.destroy()
        self._tabs = {
            "endpoints": TabEndpoints(self.content, self),
            "routing":   TabRouting(self.content, self),
            "tuning":    GenericTab(self.content, self, TUNING_SECTIONS, "config_vals"),
            "live":      GenericTab(self.content, self, LIVE_SECTIONS,   "server_vals"),
            "tts":       GenericTab(self.content, self, TTS_SECTIONS,    "tts_vals"),
            "mcp":        TabMCP(self.content, self),
            "characters": TabCharacters(self.content, self),
            "preview":    TabPreview(self.content, self),
        }
        for t in self._tabs.values(): t.place(relwidth=1, relheight=1)
        self._show("endpoints")
        self._dirty = False
        self._status_lbl.config(text="Reloaded", fg=C["blue"])
        self.root.after(2000, lambda: self._status_lbl.config(text=""))

    # ── Config generator ─────────────────────────────────────────
    def _gen_config(self):
        cfg = self._tabs["tuning"].collect() if "tuning" in self._tabs else self.config_vals
        t   = lambda k, d="": str(cfg.get(k, self.config_vals.get(k, d)))

        ep_lines = "\n".join(
            f'    "{k}": {{\n'
            f'        "url":     "{ep.get("url","")}",\n'
            f'        "api_key": "{ep.get("api_key","")}",\n'
            f'        "model":   "{ep.get("model","")}",\n'
            f'        "disable_thinking": {bool(ep.get("disable_thinking", False))},\n'
            f'    }},'
            for k, ep in self.endpoints.items())
        rt_lines = "\n".join(
            f'    "{k}": "{v}",' for k, v in self.routing.items())

        lm_key  = list(self.endpoints.keys())[0]
        sk_key  = self.routing.get("soul", lm_key)
        lm_ep   = self.endpoints.get(lm_key, {})
        soul_ep = self.endpoints.get(sk_key, lm_ep)

        return (
            f"from openai import OpenAI\n\n"
            f"ENDPOINTS = {{\n{ep_lines}\n}}\n\n"
            f"CALL_ROUTING = {{\n{rt_lines}\n}}\n\n"
            f"STORAGE_DIR      = {repr(t('STORAGE_DIR', '.'))}\n"
            f"MEMORY_DIR       = {repr(t('MEMORY_DIR', 'memory'))}\n"
            f"MODEL_MEMORY_DIR = {repr(t('MODEL_MEMORY_DIR', '.'))}\n\n"
            f"DEBUG        = {t('DEBUG', 'True')}\n"
            f"MAX_HISTORY  = {t('MAX_HISTORY', 7)}\n"
            f"MAX_INFO     = {t('MAX_INFO', 20)}\n"
            f"MAX_NOTES    = {t('MAX_NOTES', 30)}\n"
            f"MAX_GIFTS    = {t('MAX_GIFTS', 50)}\n"
            f"MAX_SEGMENTS = {t('MAX_SEGMENTS', 5)}\n\n"
            f"def _get_endpoint(pass_name):\n"
            f"    ep_key = CALL_ROUTING.get(pass_name)\n"
            f"    if ep_key is None: raise ValueError(f'Pass {{pass_name}} not in CALL_ROUTING')\n"
            f"    ep = ENDPOINTS.get(ep_key)\n"
            f"    if ep is None: raise ValueError(f'Endpoint {{ep_key}} not found')\n"
            f"    return ep\n\n"
            f"def get_client(pass_name):\n"
            f"    ep  = _get_endpoint(pass_name)\n"
            f"    key = CALL_ROUTING[pass_name]\n"
            f"    if key not in _CLIENT_CACHE:\n"
            f"        _CLIENT_CACHE[key] = OpenAI(base_url=ep['url'], api_key=ep['api_key'], timeout=60, max_retries=0)\n"
            f"    return _CLIENT_CACHE[key]\n\n"
            f"def get_model(pass_name): return _get_endpoint(pass_name)['model']\n\n"
            f"def get_extra_body(pass_name):\n"
            f"    ep = _get_endpoint(pass_name)\n"
            f"    return {{'enable_thinking': False}} if ep.get('disable_thinking') else {{}}\n\n"
            f"_CLIENT_CACHE = {{}}\n\n"
            f"LM_STUDIO_URL   = {repr(lm_ep.get('url',''))}\n"
            f"MODEL_NAME      = {repr(lm_ep.get('model',''))}\n"
            f"SOUL_API_URL    = {repr(soul_ep.get('url',''))}\n"
            f"SOUL_MODEL_NAME = {repr(soul_ep.get('model',''))}\n"
            f"client = OpenAI(base_url=LM_STUDIO_URL, api_key={repr(lm_ep.get('api_key',''))}, timeout=60, max_retries=0)\n"
        )

    def _export_config(self):
        path = filedialog.asksaveasfilename(
            defaultextension=".py",
            filetypes=[("Python files","*.py")],
            initialfile="config.py",
            title="Export config.py")
        if path:
            write_file(path, self._gen_config())
            self._status_lbl.config(text="Exported", fg=C["blue"])
            self.root.after(2000, lambda: self._status_lbl.config(text=""))

    # ── Close ────────────────────────────────────────────────────
    def _on_close(self):
        if self._dirty:
            ans = messagebox.askyesnocancel("Unsaved changes","Save before closing?")
            if ans is None:  return
            if ans:          self._save_all()
        self.root.destroy()

    def run(self): self.root.mainloop()

# ══════════════════════════════════════════════════════════════════
def open_settings(): SettingsApp().run()

def open_settings_async():
    """Non-blocking version of open_settings(). Runs the Settings UI window
    in a background daemon thread so the caller (e.g. liveDesktop.py's
    main()) can keep running immediately instead of waiting for the
    settings window to be closed first."""
    def _run():
        try:
            SettingsApp().run()
        except Exception as e:
            print(f"[settings_ui] Failed to open settings window: {e}")
    t = threading.Thread(target=_run, name="SettingsUI", daemon=True)
    t.start()
    return t

if __name__ == "__main__": open_settings()