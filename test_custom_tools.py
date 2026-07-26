"""
test_custom_tools.py
Tes semua custom MCP tools di mcp_custom_tools.json tanpa perlu
main.py / liveDesktop.py jalan. Jalankan langsung:
    python test_custom_tools.py
"""

import json
import os
import sys

# ── Pastikan requests tersedia ────────────────────────────────────────────────
try:
    import requests
except ImportError:
    print("❌ 'requests' belum terinstall. Jalankan: pip install requests")
    sys.exit(1)

# ── Duplikat minimal fungsi dari mcp_tools.py ─────────────────────────────────
# (agar script ini bisa jalan tanpa import chain memory.py dll)

TOOLS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "mcp_custom_tools.json")

def load_tools():
    with open(TOOLS_FILE, encoding="utf-8") as f:
        data = json.load(f)
    raw = data.get("tools") if isinstance(data, dict) else data
    return [t for t in raw if isinstance(t, dict) and t.get("name")]

def fill_template(value, args):
    if isinstance(value, str):
        try:
            return value.format(**{k: ("" if v is None else v) for k, v in args.items()})
        except Exception:
            return value
    if isinstance(value, dict):
        return {k: fill_template(v, args) for k, v in value.items()}
    if isinstance(value, list):
        return [fill_template(v, args) for v in value]
    return value

def run_tool(tool, args):
    ttype = tool.get("type", "http")
    if ttype != "http":
        return f"(unsupported type: {ttype})"
    method  = (tool.get("method") or "GET").upper()
    url     = fill_template(tool.get("url", ""), args)
    headers = fill_template(tool.get("headers") or {}, args)
    body    = fill_template(tool.get("body"), args)
    print(f"  → {method} {url}")
    try:
        resp = requests.request(
            method, url, headers=headers,
            json=body if isinstance(body, (dict, list)) else None,
            data=body if isinstance(body, str) else None,
            timeout=10,
        )
        text = resp.text.strip()
        return text[:600] if text else f"(status {resp.status_code}, no body)"
    except Exception as e:
        return f"(request failed: {e})"

def pretty_result(name, raw):
    """Coba parse JSON, kalau bisa tampilkan ringkas, kalau tidak raw."""
    try:
        data = json.loads(raw)
        return json.dumps(data, indent=2, ensure_ascii=False)[:800]
    except Exception:
        return raw[:600]

# ── Test cases per tool ───────────────────────────────────────────────────────
TEST_CASES = {
    "get_weather": {
        "args": {"city": "Jakarta"},
        "label": "Cuaca Jakarta",
        "extract": lambda d: (
            f"Suhu: {d['current_condition'][0]['temp_C']}°C | "
            f"Feels like: {d['current_condition'][0]['FeelsLikeC']}°C | "
            f"Kondisi: {d['current_condition'][0]['weatherDesc'][0]['value']} | "
            f"Kelembaban: {d['current_condition'][0]['humidity']}%"
        ) if isinstance(d, dict) and "current_condition" in d else str(d)[:200]
    },
    "get_random_joke": {
        "args": {},
        "label": "Random Joke",
        "extract": lambda d: (
            f"Setup: {d.get('setup','?')}\nPunchline: {d.get('punchline','?')}"
        ) if isinstance(d, dict) else str(d)[:200]
    },
    "get_dog_fact": {
        "args": {},
        "label": "Dog Fact",
        "extract": lambda d: (
            d["data"][0]["attributes"]["body"]
            if isinstance(d, dict) and "data" in d and d["data"]
            else str(d)[:200]
        )
    },
}

# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    print("=" * 60)
    print("  MCP CUSTOM TOOLS — TEST RUNNER")
    print("=" * 60)

    tools = load_tools()
    enabled = [t for t in tools if t.get("enabled", False)]
    disabled = [t for t in tools if not t.get("enabled", False)]

    print(f"\n  Tools loaded  : {len(tools)}")
    print(f"  Enabled       : {[t['name'] for t in enabled]}")
    print(f"  Disabled      : {[t['name'] for t in disabled]}")
    print(f"  Skipping disabled tools.\n")

    passed = 0
    failed = 0

    for tool in enabled:
        name = tool["name"]
        cat  = tool.get("category", "custom")
        print(f"\n{'─'*60}")
        print(f"  🧪 TEST: {name}  [category={cat}]")
        print(f"  desc: {tool.get('description','')[:80]}")

        test = TEST_CASES.get(name)
        args = test["args"] if test else {}
        label = test["label"] if test else name

        print(f"  args: {args}")
        raw = run_tool(tool, args)

        # Coba extract ringkas
        try:
            data = json.loads(raw)
            if test and "extract" in test:
                summary = test["extract"](data)
                print(f"\n  ✅ {label}:")
                print(f"  {summary}")
            else:
                print(f"\n  ✅ Raw (JSON):")
                print("  " + json.dumps(data, indent=2, ensure_ascii=False)[:400])
            passed += 1
        except Exception:
            if raw.startswith("(request failed") or raw.startswith("(status"):
                print(f"\n  ❌ FAILED: {raw}")
                failed += 1
            else:
                print(f"\n  ✅ Raw (text): {raw[:300]}")
                passed += 1

    print(f"\n{'='*60}")
    print(f"  RESULT: {passed} passed, {failed} failed")
    print(f"{'='*60}\n")

    # Verifikasi dispatch via mcp_tools._find_custom_tool (kalau bisa import)
    print("  Testing _dispatch integration via mcp_tools.py ...")
    try:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        import importlib
        # Override cache path agar pakai file test kita
        import mcp_tools as mt
        mt._CUSTOM_TOOLS_FILE = TOOLS_FILE
        mt._custom_tools_cache = None

        for tool in enabled:
            name = tool["name"]
            test = TEST_CASES.get(name)
            args = test["args"] if test else {}
            found = mt._find_custom_tool(name)
            if found:
                result = mt._run_custom_tool(found, args)
                try:
                    d = json.loads(result)
                    print(f"  ✅ _dispatch({name!r}) → JSON ok")
                except Exception:
                    print(f"  ✅ _dispatch({name!r}) → {result[:80]}")
            else:
                print(f"  ⚠️  _find_custom_tool({name!r}) → None (disabled atau tidak ada)")
    except ImportError as e:
        print(f"  ℹ️  mcp_tools.py tidak bisa diimport di luar project ({e})")
        print(f"     Ini normal — test HTTP di atas sudah cukup.")

if __name__ == "__main__":
    main()
