"""
concurrency.py — Shared bounded thread pool untuk fan-out LLM call paralel.

Kenapa perlu file terpisah (bukan bikin threading.Thread ad-hoc tiap tempat):
semua fan-out paralel di seluruh pipeline (Analyzer+precheck router, Pass B/C
antar kategori, dst) HARUS berbagi satu batas jumlah slot yang sama — kalau
tiap tempat bikin thread sendiri tanpa batas, jumlah request concurrent ke
server inference lokal bisa jauh melebihi kapasitasnya (mis. 6 thread nembak
barengan padahal server cuma sanggup 2), yang malah bikin semuanya antre dan
latency-nya sama saja (atau lebih buruk karena overhead) dibanding sekuensial.

MAX_PARALLEL_LLM WAJIB disamakan MANUAL dengan setting "max concurrent" di
server inference lokal kamu (LM Studio: Settings → max concurrent). Kode ini
tidak bisa menebak sendiri angka itu.
"""
from __future__ import annotations
import concurrent.futures
import threading

# Samakan dengan setting "max concurrent" di LM Studio (atau server lain).
# Default konservatif 2 — sesuai konfirmasi kamu ("2 sudah cukup"). Naikkan
# ke 4 (via set_max_parallel(4)) kalau nanti mau coba slot lebih banyak.
MAX_PARALLEL_LLM = 2

_lock = threading.Lock()
_executor: concurrent.futures.ThreadPoolExecutor = concurrent.futures.ThreadPoolExecutor(
    max_workers=MAX_PARALLEL_LLM,
    thread_name_prefix="llm-pool",
)


def get_executor() -> concurrent.futures.ThreadPoolExecutor:
    """Executor bersama — semua fan-out paralel di pipeline WAJIB lewat sini,
    jangan bikin ThreadPoolExecutor/threading.Thread sendiri-sendiri, supaya
    total slot concurrent ke server inference tetap terkontrol satu tempat."""
    return _executor


def set_max_parallel(n: int) -> None:
    """
    Ganti ukuran pool saat runtime (mis. kalau kamu naikkan setting LM Studio
    dari 2 ke 4). Executor lama di-shutdown tanpa nunggu task yang sedang
    jalan (wait=False) — task yang sudah dispatch tetap selesai normal via
    reference lama yang sudah dipegang caller, cuma task BARU akan pakai
    pool baru.
    """
    global _executor, MAX_PARALLEL_LLM
    with _lock:
        MAX_PARALLEL_LLM = max(1, int(n))
        old = _executor
        _executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=MAX_PARALLEL_LLM,
            thread_name_prefix="llm-pool",
        )
        old.shutdown(wait=False)


def run_fan_out(tasks: dict) -> dict:
    """
    Convenience helper: submit beberapa task independen sekaligus, tunggu
    semuanya selesai, return dict {key: result}. Kalau salah satu task error,
    hasilnya {"error": str(e)} untuk key itu saja — task lain tidak ikut gagal.

    tasks: {key: (fn, args_tuple, kwargs_dict)}  ATAU  {key: callable_no_arg}

    Contoh:
        results = run_fan_out({
            "analyzer": lambda: cs_analyze(user_id=..., user_input=..., ...),
            "precheck": lambda: task_router.precheck(user_id=..., ...),
        })
        conv_state = results["analyzer"]
        pre        = results["precheck"]
    """
    ex = get_executor()
    futures = {}
    for key, task in tasks.items():
        if callable(task):
            fn, args, kwargs = task, (), {}
        else:
            fn, args, kwargs = task[0], task[1] if len(task) > 1 else (), task[2] if len(task) > 2 else {}
        futures[ex.submit(fn, *args, **kwargs)] = key

    results = {}
    for fut in concurrent.futures.as_completed(futures):
        key = futures[fut]
        try:
            results[key] = fut.result()
        except Exception as e:
            results[key] = {"error": str(e)}
    return results
