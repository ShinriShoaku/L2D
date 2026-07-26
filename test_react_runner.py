"""
test_react_runner.py — Harness untuk validasi konsistensi Agent ReAct loop.

Cara pakai:
    python3 test_react_runner.py                  # semua kasus, 5x repeat
    python3 test_react_runner.py --repeat 10
    python3 test_react_runner.py --case A5_write_via_history_confirmation
    python3 test_react_runner.py --category B

PENTING: Ini menguji LAPISAN REASONING saja (apakah model memanggil tool yang
benar, dengan urutan yang benar, tanpa melanggar guard) — TIDAK menguji hasil
data sungguhan. Eksekusi tool di-mock (MockToolBus) supaya:
  1. Test bisa jalan tanpa DB/memory file asli.
  2. Kita bisa assert urutan panggilan (mis. resolve() sebelum lookup(other_user))
     yang sulit dicek kalau langsung lewat RouterExecutor asli.

Begitu loop ReAct asli (_react_agent_loop di main.py) sudah jadi, runner ini
TINGGAL diarahkan ke fungsi itu — ganti import di bagian "WIRE-UP" di bawah.
"""

import argparse
import json
import os
import sys
from collections import Counter, defaultdict
from typing import Dict, List, Any, Tuple, Optional

from react_test_cases import TEST_CASES, get_cases_by_category


# ═══════════════════════════════════════════════════════════════════════════
# MOCK TOOL BUS — mencatat setiap panggilan, balas observation template.
# Tidak pernah benar-benar baca/tulis data nyata.
# ═══════════════════════════════════════════════════════════════════════════

class MockToolBus:
    def __init__(self, case: Dict[str, Any]):
        self.case      = case
        self.calls: List[Dict[str, Any]] = []   # log semua tool call urut waktu
        self.is_admin   = case.get("is_admin", False)
        self.current_id = case["current_user"]["id"]
        self.history    = case.get("history", [])
        self._resolved_ids = {
            h.get("user_id"): h.get("username")
            for h in self.history if h.get("user_id")
        }

    def _log(self, tool: str, **kwargs):
        entry = {"tool": tool, **kwargs}
        self.calls.append(entry)
        return entry

    def lookup(self, scope: str, ref: str = "") -> str:
        self._log("lookup", scope=scope, ref=ref)
        if scope == "other_user_romance_bulk":
            return "DITOLAK: scope ini tidak ada, gunakan scope spesifik per id."
        if scope == "other_user" and ref and ref not in self._resolved_ids and ref != self.case.get("expect", {}).get("_allow_unresolved_id"):
            # ref harus user_id asli, kalau bukan -> tolak (simulate guard)
            if ref not in [self.current_id] and not ref.startswith("u_"):
                return "DITOLAK: ref harus user_id, gunakan resolve() dulu."
        return f"OBS({scope}): <data dummy untuk {ref or 'self'}>"

    def resolve(self, text_ref: str) -> str:
        self._log("resolve", text_ref=text_ref)
        for uid, uname in self._resolved_ids.items():
            if uname and uname.lower() in text_ref.lower():
                return json.dumps({"candidates": [{"user_id": uid, "display_name": uname}]})
        return json.dumps({"candidates": []})

    def mutate(self, scope: str, ref: str, value: str, mode: str = "append") -> str:
        self._log("mutate", scope=scope, ref=ref, value=value, mode=mode)
        if scope != "self_memory" and not self.is_admin:
            return "DITOLAK: mutate selain self_memory butuh admin."
        return f"OK: {scope}/{ref} {mode} <- {value}"

    def admin_action(self, action: str, target: str = "", reason: str = "") -> str:
        self._log("admin_action", action=action, target=target, reason=reason)
        if not self.is_admin:
            return "DITOLAK: bukan admin."
        return f"OK: {action} target={target} reason={reason}"

    def done(self) -> str:
        self._log("done")
        return "OK: loop selesai."


# ═══════════════════════════════════════════════════════════════════════════
# WIRE-UP: ganti bagian ini begitu _react_agent_loop asli sudah ada di main.py
# ═══════════════════════════════════════════════════════════════════════════

def _history_text_from_case(case: Dict[str, Any]) -> str:
    lines = []
    for h in case.get("history", []):
        role    = h.get("role", "user")
        label   = "Alfa" if role == "assistant" else (h.get("username") or "User")
        content = h.get("content", "")
        # Dataset kadang sudah menulis "Nama: isi" di content -> hindari dobel "Nama: Nama: isi"
        prefix = f"{label}:"
        if content.lower().startswith(prefix.lower()):
            content = content[len(prefix):].strip()
        lines.append(f"{label}: {content}")
    return "\n".join(lines)


_CHARACTER_LOADED = False


def _ensure_character_loaded(main_module):
    """
    _react_agent_loop butuh main.CHARACTER['prompts']['agent_react_system'] terisi.
    Kita load character.json langsung ke main.CHARACTER (bukan lewat
    set_character() penuh) supaya runner ini TIDAK menyentuh filesystem
    ModelMemory/ChatHistory asli — cukup isi dict prompts saja.
    """
    global _CHARACTER_LOADED
    if _CHARACTER_LOADED and main_module.CHARACTER.get("prompts"):
        return
    char_path = os.environ.get("ALFA_CHARACTER_JSON", "character/alfa/character.json")
    with open(char_path, "r", encoding="utf-8") as f:
        data = json.load(f)
    main_module.CHARACTER = data
    main_module.ADMIN_USER_ID = data.get("admin_user_id", main_module.ADMIN_USER_ID)
    _CHARACTER_LOADED = True


def run_agent_loop_live(case: Dict[str, Any], bus: MockToolBus, max_steps: int = 4) -> List[Dict]:
    """
    Adapter nyata: panggil _react_agent_loop dari main.py (loop ReAct asli).
    Membutuhkan environment penuh (config.py, LM Studio jalan di LM_STUDIO_URL)
    karena ini benar-benar memanggil model lokal kamu, bukan simulasi.
    """
    import main as _main  # lazy import — baru dibutuhkan saat runner live dipakai
    _ensure_character_loaded(_main)
    history_text = _history_text_from_case(case)
    return _main._react_agent_loop(
        user_input       = case["input"],
        history_text     = history_text,
        current_user_id  = case["current_user"]["id"],
        is_admin         = case.get("is_admin", False),
        tool_bus         = bus,
        max_steps        = max_steps,
        active_command   = case.get("active_command", ""),
    )


def run_agent_loop_stub(case: Dict[str, Any], bus: MockToolBus, max_steps: int = 4) -> List[Dict]:
    """
    STUB fallback — dipakai kalau main.py / LM Studio belum bisa diakses dari
    environment yang menjalankan test (mis. CI tanpa model lokal). TIDAK
    merepresentasikan kualitas reasoning model sungguhan — hanya menjaga
    runner tetap bisa dieksekusi secara struktural.
    """
    raise NotImplementedError(
        "run_agent_loop_stub tidak mensimulasikan reasoning apa pun. "
        "Jalankan dengan --live (membutuhkan main.py + LM Studio aktif) "
        "untuk benar-benar menguji model."
    )


# ═══════════════════════════════════════════════════════════════════════════
# ASSERTIONS
# ═══════════════════════════════════════════════════════════════════════════

def _tool_seq(calls: List[Dict]) -> List[str]:
    return [c["tool"] for c in calls]


def check_case(case: Dict[str, Any], calls: List[Dict]) -> List[str]:
    """Return list of failure messages (kosong = pass)."""
    failures = []
    expect = case.get("expect", {})
    seq = _tool_seq(calls)

    # forbid_tools: tool ini sama sekali tidak boleh muncul
    for forbidden in expect.get("forbid_tools", []):
        if forbidden in seq:
            failures.append(f"tool terlarang '{forbidden}' dipanggil (seq={seq})")

    # calls_min: tool+scope/action ini WAJIB muncul minimal sekali
    for want in expect.get("calls_min", []):
        found = any(
            c["tool"] == want["tool"]
            and all(c.get(k) == v for k, v in want.items() if k != "tool")
            for c in calls
        )
        if not found:
            failures.append(f"calls_min tidak terpenuhi: {want} (calls={calls})")

    # forbid_scope_calls: kombinasi tool+scope spesifik dilarang
    for bad in expect.get("forbid_scope_calls", []):
        found = any(
            c["tool"] == bad["tool"] and all(c.get(k) == v for k, v in bad.items() if k != "tool")
            for c in calls
        )
        if found:
            failures.append(f"forbid_scope_calls dilanggar: {bad}")

    # calls_must_precede: tool A harus muncul SEBELUM tool B
    for before, after in expect.get("calls_must_precede", []):
        idx_before = next((i for i, t in enumerate(seq) if t == before), None)
        idx_after  = next((i for i, t in enumerate(seq) if t == after), None)
        if idx_after is not None:
            if idx_before is None or idx_before > idx_after:
                failures.append(f"'{before}' harus dipanggil sebelum '{after}' (seq={seq})")

    # value_must_contain_any: salah satu mutate call harus mengandung keyword ini
    if "value_must_contain_any" in expect:
        keywords = expect["value_must_contain_any"]
        mutate_values = [c.get("value", "") for c in calls if c["tool"] == "mutate"]
        if not any(any(kw.lower() in v.lower() for kw in keywords) for v in mutate_values):
            failures.append(
                f"tidak ada mutate.value yang mengandung salah satu dari {keywords} "
                f"(mutate_values={mutate_values})"
            )

    # expect_self_scope_only: semua lookup/mutate harus scope self_*, tidak ada other_user
    if expect.get("expect_self_scope_only"):
        for c in calls:
            if c["tool"] in ("lookup", "mutate") and c.get("scope", "").startswith("other_user"):
                failures.append(f"melanggar expect_self_scope_only: {c}")

    # allow_either: minimal salah satu dari beberapa pola sequence ini harus cocok (longgar: subset)
    if "allow_either" in expect:
        ok = False
        for pattern in expect["allow_either"]:
            pat_tools = [p["tool"] for p in pattern]
            if all(t in seq for t in pat_tools):
                ok = True
                break
        if not ok:
            failures.append(f"tidak ada pola di allow_either yang cocok (seq={seq})")

    # must_terminate: loop harus benar2 memanggil done() di akhir
    if expect.get("must_terminate") and (not seq or seq[-1] != "done"):
        failures.append(f"loop tidak diakhiri dengan done() (seq={seq})")

    return failures


# ═══════════════════════════════════════════════════════════════════════════
# RUNNER
# ═══════════════════════════════════════════════════════════════════════════

def run_one_case(case: Dict[str, Any], repeat: int, loop_fn) -> Dict[str, Any]:
    pass_count = 0
    all_failures: List[List[str]] = []
    seqs_seen: Counter = Counter()

    for _ in range(repeat):
        bus = MockToolBus(case)
        try:
            calls = loop_fn(case, bus)
        except Exception as e:
            all_failures.append([f"EXCEPTION: {e}"])
            continue
        failures = check_case(case, calls)
        seqs_seen[tuple(_tool_seq(calls))] += 1
        if not failures:
            pass_count += 1
        else:
            all_failures.append(failures)

    return {
        "id": case["id"],
        "category": case["category"],
        "pass_rate": pass_count / repeat if repeat else 0.0,
        "pass_count": pass_count,
        "repeat": repeat,
        "distinct_sequences": len(seqs_seen),
        "seq_distribution": dict(seqs_seen),
        "sample_failures": all_failures[:3],
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--repeat", type=int, default=5, help="berapa kali ulang tiap case (cek konsistensi)")
    ap.add_argument("--case", type=str, default=None, help="jalankan 1 case by id")
    ap.add_argument("--category", type=str, default=None, help="jalankan semua case 1 kategori (A-I)")
    ap.add_argument("--min-pass-rate", type=float, default=0.8, help="ambang lulus per case")
    ap.add_argument("--live", action="store_true", help="jalankan melawan model nyata (main.py + LM Studio aktif)")
    args = ap.parse_args()

    loop_fn = run_agent_loop_live if args.live else run_agent_loop_stub

    cases = TEST_CASES
    if args.case:
        cases = [c for c in TEST_CASES if c["id"] == args.case]
    elif args.category:
        cases = get_cases_by_category(args.category)

    if not cases:
        print("Tidak ada case yang cocok.")
        sys.exit(1)

    results = []
    for case in cases:
        r = run_one_case(case, args.repeat, loop_fn)
        results.append(r)

    print(f"\n{'CASE':35} {'CAT':4} {'PASS RATE':10} {'#SEQ':5} STATUS")
    print("-" * 75)
    n_fail = 0
    for r in results:
        status = "OK" if r["pass_rate"] >= args.min_pass_rate else "FAIL"
        if status == "FAIL":
            n_fail += 1
        print(f"{r['id']:35} {r['category']:4} {r['pass_rate']*100:>6.0f}%    {r['distinct_sequences']:5} {status}")
        if status == "FAIL" and r["sample_failures"]:
            for f in r["sample_failures"][0]:
                print(f"      └─ {f}")

    print("-" * 75)
    print(f"Total: {len(results)} case, {n_fail} FAIL (ambang {args.min_pass_rate*100:.0f}%)")
    sys.exit(1 if n_fail else 0)


if __name__ == "__main__":
    main()
