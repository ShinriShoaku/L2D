#!/usr/bin/env python3
"""
liveServer.py — TikTok Live → Character → Live2D.
Pipeline: WebSocket event → ResponsePipeline → full_generate → TTS → Live2D
"""

import asyncio
import gc
import json
import os
import queue as _queue_mod
import random
import re
import sys
import threading
import time
from collections import defaultdict, deque
from datetime import datetime, timezone
from typing import Callable, Dict, List, Optional, Tuple

import websockets

from live2d_client import Live2DClient
from wav_detector import WavVoiceDetector
from liveDesktop import request_tts, play_audio
from character_manager import CharacterManager
from memory import UserMemory, UserMemoryManager
from live_tracking import LiveTracker
from banter_manager import BanterManager
from settings_ui import open_settings_async
from main import (
    CHARACTER,
    _ACTIVE_CHAR_NAME,
    set_character,
    full_generate,
    generate_simple,
    generate_from_prompt,
    build_opening_prompt,
    build_closing_prompt,
    make_banter_schema,
    clean_jp_for_tts,
    generate_banter_pipeline,  # ← TAMBAH INI
    MEMORY_DIR,
    MODEL_MEMORY_DIR,
)
import main as _main

_main.DEBUG = False

# ── Platform fix ──────────────────────────────────────────────────────────────
# Windows: set SelectorEventLoop agar websockets tidak konflik dengan
# ProactorEventLoop (default Windows Python 3.8+).
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

# ── Audio channel ─────────────────────────────────────────────────────────────
_AUDIO_CHANNEL = threading.Lock()

# ═════════════════════════════════════════════════════════════════════════════
# KONFIGURASI
# ═════════════════════════════════════════════════════════════════════════════

WS_APP_URL   = "ws://localhost:5678"
L2D_HTTP_URL = "http://localhost:9000"
L2D_WS_URL   = "ws://localhost:9001"

MEMORY_FOLDER   = MEMORY_DIR
CHAT_LOG_PATH   = "live_chat.json"
LIVE_TRACK_PATH = "live_track.json"

# Spam
SPAM_MSG_LIMIT    = 3
SPAM_WINDOW_SEC   = 5.0
SPAM_COOLDOWN_SEC = 30.0

# Dedup & Batch
DEDUP_WINDOW_SEC = 4.0
BATCH_WINDOW_SEC = 2.5

# Gift trigger
GIFT_TRIGGER_MIN = 3
GIFT_TRIGGER_MAX = 5

# Live2D
L2D_MODEL_ID  = 0
L2D_MODEL_MAP = 0

# Queue
MAX_QUEUE_SIZE    = 10
QUEUE_RANDOM_FROM = 5

# Maintenance
CLEANUP_INTERVAL = 120
USER_CACHE_LIMIT = 150


# ═════════════════════════════════════════════════════════════════════════════
# HELPERS
# ═════════════════════════════════════════════════════════════════════════════

def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()

def now_ts() -> float:
    return time.monotonic()

def _safe_id(text: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_\-]", "_", str(text))[:64]

def _load_json(path: str, default):
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            print(f"⚠️ load {path}: {e}")
    return default

def _save_json(path: str, data):
    if isinstance(data, deque):
        data = list(data)
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
    except Exception as e:
        print(f"⚠️ save {path}: {e}")


# ═════════════════════════════════════════════════════════════════════════════
# PLAY ENGINE — TTS + Live2D
# ═════════════════════════════════════════════════════════════════════════════

def play_segments(
    segments:    List[Dict],
    expression:  str,
    l2d:         Live2DClient,
    on_complete: Optional[Callable] = None,
):
    """Non-blocking. Acquires _AUDIO_CHANNEL lalu play sequentially."""
    l2d.set_expression(expression)

    def _acquire_and_play():
        _AUDIO_CHANNEL.acquire()

        def _release_complete():
            _AUDIO_CHANNEL.release()
            if on_complete:
                on_complete()

        if not segments:
            _release_complete()
            return
        _play_seq(segments, expression, l2d, 0, _release_complete)

    threading.Thread(target=_acquire_and_play, daemon=True,
                     name="play-ch").start()


def _play_seq(
    segments:    List[Dict],
    expression:  str,
    l2d:         Live2DClient,
    idx:         int,
    on_complete: Optional[Callable],
):
    if idx >= len(segments):
        if on_complete:
            on_complete()
        return

    seg      = segments[idx]
    ind_txt  = seg.get("ind", "")
    jp_txt   = seg.get("jp",  "")
    seg_anim = seg.get("anim", expression)
    total    = len(segments)

    print(f"🎤 [TTS] {idx+1}/{total} ({seg_anim}): {jp_txt[:40]}...")
    l2d.set_expression(seg_anim)

    audio = request_tts(jp_txt)
    if not audio or not os.path.exists(audio):
        print(f"❌ [TTS] Gagal segmen {idx+1}")
        l2d.show_chat_bubble(ind_txt, 3000)
        l2d.send_random_motions(L2D_MODEL_ID, "response")
        def _skip():
            time.sleep(0.3)
            _play_seq(segments, expression, l2d, idx + 1, on_complete)
        threading.Thread(target=_skip, daemon=True).start()
        return

    analysis    = WavVoiceDetector.analyze(audio, jp_txt)
    mapped      = analysis.get("mappedOutput", "")
    duration_ms = analysis.get("wavDurationMs", 0)

    if mapped and duration_ms > 0:
        l2d.start_lipsync(L2D_MODEL_ID, L2D_MODEL_MAP, mapped, duration_ms)
        l2d.show_chat_bubble(ind_txt, duration_ms)
        l2d.send_random_motions(L2D_MODEL_ID, "response")
    else:
        l2d.show_chat_bubble(ind_txt, 3000)
        l2d.send_random_motions(L2D_MODEL_ID, "response")

    def _after():
        time.sleep(0.3)
        _play_seq(segments, expression, l2d, idx + 1, on_complete)

    play_audio(audio, on_finish_callback=_after)


# ─── Streaming play: baca dari queue satu per satu ────────────────────────────

import shutil
import tempfile


def _tts_to_tempfile(jp_txt: str) -> Optional[str]:
    """
    Fetch TTS lalu copy hasilnya ke file temporary unik.
    Return path temp file, atau None jika gagal.

    WAJIB copy karena server selalu return path yang SAMA (mutsuki.wav).
    Tanpa copy, prefetch concurrent akan overwrite file yang sedang diplay.
    """
    from liveDesktop import request_tts as _req_tts, AUDIO_PATH as _AUDIO_PATH
    src = _req_tts(jp_txt)
    if not src or not os.path.exists(src):
        return None
    try:
        tmp_dir = os.path.dirname(src)
        fd, dst = tempfile.mkstemp(suffix=".wav", dir=tmp_dir)
        os.close(fd)
        shutil.copy2(src, dst)
        return dst
    except Exception as e:
        print(f"⚠️ [TTS] Gagal copy ke temp: {e}")
        return src


def _play_seq_streaming(
    seg_queue:   "_queue_mod.Queue",
    expression:  str,
    l2d:         Live2DClient,
    on_complete: Optional[Callable],
    *,
    _hold_channel: bool = True,
):
    """
    Streaming TTS player dengan prefetch pipeline + temp file unik per segmen.

    Alur per iterasi:
      1. Gunakan audio dari prefetch iterasi sebelumnya (atau fetch blocking jika pertama)
      2. Peek queue untuk segmen berikutnya — jika ada, langsung prefetch TTS-nya
         ke temp file unik di background thread
      3. Play audio segmen sekarang (blocking via Event)
      4. Hapus temp file segmen sekarang setelah selesai
      5. Ambil hasil prefetch → jadi current_audio iterasi berikutnya
      6. Ulangi sampai sentinel (None) diterima
    """
    from liveDesktop import AUDIO_PATH as _AUDIO_PATH

    if _hold_channel:
        _orig_on_complete = on_complete
        def on_complete():          # noqa: F811
            _AUDIO_CHANNEL.release()
            if _orig_on_complete:
                _orig_on_complete()

    def _worker():
        current_audio   = None   # audio path (temp file) untuk segmen sekarang
        current_seg     = None   # segmen sekarang
        prefetch_holder = [None] # holder audio path untuk segmen berikutnya
        prefetch_thread = None   # thread yang sedang prefetch

        # ── Ambil segmen pertama dari queue (blocking) ────────────────────
        try:
            current_seg = seg_queue.get(timeout=45)
        except Exception:
            current_seg = None

        if current_seg is None:   # sentinel langsung — queue kosong
            if on_complete:
                on_complete()
            return

        # Fetch TTS segmen pertama (blocking — tidak ada yg bisa di-overlap)
        current_audio = _tts_to_tempfile(current_seg.get("jp", ""))

        while True:
            if current_seg is None:   # sentinel
                if on_complete:
                    on_complete()
                return

            ind_txt  = current_seg.get("ind", "")
            jp_txt   = current_seg.get("jp",  "")
            seg_anim = current_seg.get("anim", expression)

            print(f"🎤 [TTS-STREAM] ({seg_anim}): {jp_txt[:40]}...")
            l2d.set_expression(seg_anim)

            # ── Peek segmen berikutnya & prefetch TTS-nya ────────────────
            try:
                next_seg = seg_queue.get_nowait()
            except _queue_mod.Empty:
                next_seg = "EMPTY"   # marker: belum ada di queue, tunggu nanti

            next_audio_holder = [None]
            next_prefetch_thr = None

            if next_seg == "EMPTY":
                # Belum ada di queue — akan di-fetch blocking setelah play selesai
                pass
            elif next_seg is None:
                # Sentinel sudah di-peek — tandai, selesai setelah segmen ini
                pass
            else:
                # Ada segmen berikutnya — prefetch TTS-nya di background
                next_audio_holder = [None]
                next_prefetch_thr = threading.Thread(
                    target=lambda h, t: h.__setitem__(0, _tts_to_tempfile(t)),
                    args=(next_audio_holder, next_seg.get("jp", "")),
                    daemon=True,
                    name="tts-pre",
                )
                next_prefetch_thr.start()

            # ── Play audio segmen sekarang ────────────────────────────────
            if not current_audio or not os.path.exists(current_audio):
                print(f"❌ [TTS-STREAM] Gagal: {jp_txt[:30]}")
                l2d.show_chat_bubble(ind_txt, 3000)
                l2d.send_random_motions(L2D_MODEL_ID, "response")
                time.sleep(0.5)
            else:
                analysis    = WavVoiceDetector.analyze(current_audio, jp_txt)
                mapped      = analysis.get("mappedOutput", "")
                duration_ms = analysis.get("wavDurationMs", 0)

                if mapped and duration_ms > 0:
                    l2d.start_lipsync(L2D_MODEL_ID, L2D_MODEL_MAP, mapped, duration_ms)
                    l2d.show_chat_bubble(ind_txt, duration_ms)
                else:
                    l2d.show_chat_bubble(ind_txt, 3000)
                l2d.send_random_motions(L2D_MODEL_ID, "response")

                done = threading.Event()
                play_audio(current_audio, on_finish_callback=done.set)
                done.wait(timeout=60)
                time.sleep(0.2)

                # Hapus temp file setelah selesai diplay
                try:
                    if current_audio != _AUDIO_PATH:
                        os.remove(current_audio)
                except Exception:
                    pass

            # ── Siapkan segmen & audio berikutnya ────────────────────────
            if next_seg == "EMPTY":
                # Belum ada di queue saat tadi — ambil sekarang (blocking)
                try:
                    current_seg = seg_queue.get(timeout=45)
                except Exception:
                    current_seg = None
                if current_seg is None:
                    if on_complete:
                        on_complete()
                    return
                current_audio = _tts_to_tempfile(current_seg.get("jp", ""))
            else:
                # Sudah di-peek tadi (bisa None/sentinel atau segmen valid)
                current_seg = next_seg
                if next_prefetch_thr is not None:
                    next_prefetch_thr.join(timeout=30)
                current_audio = next_audio_holder[0]

    threading.Thread(target=_worker, daemon=True, name="stream-worker").start()


# ═════════════════════════════════════════════════════════════════════════════
# RESPONSE PIPELINE
# ═════════════════════════════════════════════════════════════════════════════

class ResponsePipeline:
    """Queue-based: generate → play."""

    def __init__(
        self,
        user_mgr:       UserMemoryManager,
        l2d:            Live2DClient,
        character_name: str = "default",
        on_idle_cb:     Optional[Callable] = None,
    ):
        self.user_mgr       = user_mgr
        self.l2d            = l2d
        self.character_name = character_name
        self.on_idle        = on_idle_cb

        self._queue: List[Tuple[str, str, str]] = []
        self._q_lock    = threading.Lock()
        self._slot      = None
        self._slot_lock = threading.Lock()
        self._is_gen    = False
        self._is_play   = False
        self._gen_evt   = threading.Event()
        self._gen_evt.set()
        self._wait_count = 0
        self._wait_lock  = threading.Lock()
        self._banter: Optional[BanterManager] = None

    def push(self, model_input: str, user_id: str, username: str):
        with self._q_lock:
            qlen = len(self._queue)
            if qlen >= MAX_QUEUE_SIZE:
                print(f"🚫 [QUEUE] Full, drop: {model_input[:35]}")
                return
            if qlen >= QUEUE_RANDOM_FROM and random.random() < 0.5:
                print(f"🎲 [QUEUE] Random drop (slot {qlen+1}): {model_input[:35]}")
                return
            self._queue.append((model_input, user_id, username))
            qlen = len(self._queue)
        print(f"📥 [QUEUE] {qlen} — {username}: {model_input[:50]}")
        self._kick()

    def queue_size(self) -> int:
        with self._q_lock:
            return len(self._queue)

    def has_work(self) -> bool:
        return self.queue_size() > 0 or self._is_gen or self._is_play

    def _kick(self):
        with self._q_lock:
            has_q = bool(self._queue)
        if not has_q:
            return
        # Hanya start gen jika tidak sedang generate dan slot kosong
        with self._slot_lock:
            slot_empty = self._slot is None
        if slot_empty and not self._is_gen and not self._is_play:
            self._start_gen()
        # _play_slot akan dipanggil oleh _start_gen/_on_seg setelah slot terisi
        # JANGAN panggil _play_slot() di sini — ini penyebab double-play

    def _start_gen(self):
        with self._q_lock:
            if not self._queue:
                return
            item = self._queue.pop(0)
        model_input, user_id, username = item
        self._is_gen = True
        self._gen_evt.clear()
        print(f"🤔 [GEN] {username}: {model_input[:50]}")

        def _run():
            # ── Queue segmen untuk streaming play ─────────────────────────
            seg_q           = _queue_mod.Queue()
            expr_holder     = ["neutral"]
            collected_count = [0]
            play_triggered  = [False]

            def _on_seg(seg: Dict, total: int = 1):
                """
                Dipanggil main.py setiap 1 kalimat selesai ditranslasi.

                Strategi trigger play:
                - total <= 3 : JANGAN trigger di sini. Tunggu semua selesai,
                               trigger dilakukan di blok try setelah sentinel dikirim.
                               (Segmen sedikit = translate cepat, tidak perlu streaming)
                - total > 3  : Trigger setelah segmen ke-3 masuk queue. Segmen 1-3
                               sudah siap diplay, sisanya menyusul ke queue sambil
                               _play_seq_streaming berjalan. Prefetch TTS di player
                               memastikan tidak ada jeda antar segmen.
                """
                seg_q.put(seg)
                collected_count[0] += 1
                # Pakai anim segmen pertama sebagai expression awal
                if collected_count[0] == 1:
                    expr_holder[0] = seg.get("anim", "neutral")

                # Hanya trigger early jika total > 3 dan sudah ada 3 segmen
                if total > 3 and collected_count[0] >= 3 and not play_triggered[0]:
                    play_triggered[0] = True
                    with self._slot_lock:
                        if self._slot is None:
                            self._slot = {
                                "queue":    seg_q,
                                "expr":     expr_holder[0],
                                "chat":     model_input,
                                "username": username,
                            }
                    if not self._is_play:
                        self._play_slot()

            try:
                user_mem = self.user_mgr.get(user_id, username)
                segs, expr = full_generate(
                    model_input, user_mem,
                    char_name        = self.character_name,
                    char_data        = CHARACTER,
                    username         = username,
                    segment_callback = _on_seg,
                )
                expr_holder[0] = expr
                # PENTING: full_generate() bisa memutasi user_mem (nickname,
                # romance_points, info, dst lewat ReAct mutate()) tapi TIDAK
                # pernah menulisnya ke disk sendiri. Tanpa save() di sini,
                # perubahan cuma hidup di RAM cache (max_cache terbatas) —
                # begitu entry ini di-evict atau proses restart, data balik
                # ke versi lama di disk (bug "data balik ke semula").
                try:
                    user_mem.save()
                except Exception as e:
                    print(f"⚠️  [SAVE] gagal menyimpan user_mem ({user_id}): {e}")
                seg_q.put(None)    # ← sentinel: semua kalimat sudah masuk queue
                print(f"✅ [GEN] {len(segs)} segs selesai: {segs[0]['ind'][:40]}...")

                # Jika total <= 3 (tunggu semua), trigger play setelah sentinel dikirim
                if not play_triggered[0]:
                    play_triggered[0] = True
                    with self._slot_lock:
                        if self._slot is None:
                            self._slot = {
                                "queue":    seg_q,
                                "expr":     expr,
                                "chat":     model_input,
                                "username": username,
                            }

            except Exception as e:
                print(f"❌ [GEN] Error: {e}")
                seg_q.put(None)   # pastikan sentinel dikirim agar play tidak hang

            finally:
                self._is_gen = False
                self._gen_evt.set()
                # Hanya trigger play jika: ada slot BARU yang belum dimainkan, dan belum ada yg play
                with self._slot_lock:
                    has_slot = self._slot is not None
                if has_slot and not self._is_play:
                    self._play_slot()

        threading.Thread(target=_run, daemon=True,
                         name=f"gen-{user_id[:8]}").start()

    def _spawn_wait(self):
        with self._wait_lock:
            if self._wait_count > 0:
                return
            self._wait_count += 1

        def _wait():
            try:
                self._gen_evt.wait(timeout=60)
                if not self._is_play:
                    self._play_slot()
            finally:
                with self._wait_lock:
                    self._wait_count -= 1

        threading.Thread(target=_wait, daemon=True,
                         name="pipeline-wait").start()

    def _play_slot(self):
        threading.Thread(target=self._play_slot_work, daemon=True,
                         name="play-slot").start()

    def _play_slot_work(self):
        with self._slot_lock:
            slot       = self._slot
            if slot is None:
                pass
            else:
                self._slot     = None
                self._is_play  = True   # ← set DALAM lock agar atomic
        if slot is None:
            if self._is_gen:
                self._spawn_wait()
            return

        # ── Baca slot — bisa dict (streaming) atau tuple (legacy) ─────────
        if isinstance(slot, dict):
            seg_q         = slot["queue"]
            expr          = slot["expr"]
            original_chat = slot["chat"]
            username      = slot.get("username", "")
        else:
            # Format lama: (segs, expr, original_chat, username) — fallback compat
            segs, expr, original_chat, username = slot
            seg_q = _queue_mod.Queue()
            for s in segs:
                seg_q.put(s)
            seg_q.put(None)

        self.l2d.show_chat_log(original_chat)

        if self._banter:
            if not self._banter.wait_interrupt_done(timeout=8.0):
                print("⚠️ [PIPELINE] Timeout interrupt phrase, lanjut...")

        with self._q_lock:
            more = bool(self._queue)
        if more and not self._is_gen:
            self._start_gen()

        def _done():
            _AUDIO_CHANNEL.release()   # ← release channel yang di-acquire di atas
            self._is_play = False
            print("🏁 [PIPELINE] Selesai")
            with self._slot_lock:
                has_slot = self._slot is not None
            with self._q_lock:
                has_q = bool(self._queue)
            if has_slot:
                self._play_slot()
            elif has_q:
                if not self._is_gen:
                    self._start_gen()
                self._spawn_wait()
            else:
                if self.on_idle:
                    self.on_idle()

        # ── Gunakan streaming player (queue-based) ─────────────────────────
        # _AUDIO_CHANNEL sudah di-acquire di sini.
        # _play_seq_streaming dipanggil dengan _hold_channel=False agar tidak
        # double-acquire (yang menyebabkan deadlock atau TTS jalan 2x).
        _AUDIO_CHANNEL.acquire()
        _play_seq_streaming(seg_q, expr, self.l2d, on_complete=_done,
                            _hold_channel=False)


# ═════════════════════════════════════════════════════════════════════════════
# LIVE SERVER
# ═════════════════════════════════════════════════════════════════════════════

class LiveServer:
    def __init__(
        self,
        ws_url:         str = WS_APP_URL,
        l2d_http:       str = L2D_HTTP_URL,
        l2d_ws:         str = L2D_WS_URL,
        character_name: str = None,
    ):
        self.ws_url = ws_url
        os.makedirs(MEMORY_FOLDER, exist_ok=True)

        # ── Character ─────────────────────────────────────────────────────
        self.char_mgr = CharacterManager()
        self._select_character(character_name)

        # ── Core ──────────────────────────────────────────────────────────
        self.l2d      = Live2DClient(base_url=l2d_http, ws_url=l2d_ws)
        self.user_mgr = UserMemoryManager(storage_dir=MEMORY_FOLDER,
                                           max_cache=USER_CACHE_LIMIT)
        self.tracker  = LiveTracker(filepath=LIVE_TRACK_PATH)

        # ── Pipeline ──────────────────────────────────────────────────────
        self.pipeline = ResponsePipeline(
            self.user_mgr,
            self.l2d,
            character_name = self.char_mgr.active or "default",
            on_idle_cb     = self._on_pipeline_idle,
        )
        self.l2d.add_listener(self._on_l2d_event)

        # ── BanterManager ─────────────────────────────────────────────────
        def _play_fn(segments, expression, on_complete=None):
            play_segments(segments, expression, self.l2d, on_complete)

        self.banter = BanterManager(
            character_cfg  = self.char_mgr.character,
            scenarios_path = self.char_mgr.scenario_path,
            banter_path    = self.char_mgr.banter_path,
            generate_fn    = generate_simple,
            pipeline_fn    = lambda raw: generate_banter_pipeline(
                raw_items=raw,
                char=self.char_mgr.character,
                char_name=self.char_mgr.active or "default",
            ),  # ← TAMBAH INI
            play_fn        = _play_fn,
            pipeline_qs_fn = self.pipeline.has_work,  # ← pakai has_work agar cek gen+play juga
            banter_schema  = make_banter_schema(),
            chat_history   = _main._chat_hist,
            model_memory   = _main._model_mem,
        )
        self.pipeline._banter = self.banter

        # ── Gift trigger state ─────────────────────────────────────────────
        self._gift_trigger: Dict[str, int] = {}

        # ── Anti-spam / dedup / batch ──────────────────────────────────────
        self._spam_times:  Dict[str, List[float]] = defaultdict(list)
        self._spam_warned: Dict[str, float]       = {}
        self._dedup:       Dict[str, Dict]        = {}
        self._pending:     List[Dict]              = []
        self._pending_lock = threading.Lock()
        self._batch_timer: Optional[threading.Timer] = None
        self._batch_lock   = threading.Lock()

        # ── Chat log ──────────────────────────────────────────────────────
        self._chat_log  = deque(_load_json(CHAT_LOG_PATH, []), maxlen=2000)
        self._log_dirty = 0

        self._initialized_users: set = set()
        self._opening_done:      bool = False

        print("✅ [LiveServer] Siap")
        print(f"   Character : {self.char_mgr.active}")
        print(f"   WS        : {ws_url}")
        print(f"   Memory    : {os.path.abspath(MEMORY_FOLDER)}")
        self._start_cleanup_loop()

    # ── Character selection ───────────────────────────────────────────────

    def _select_character(self, requested: Optional[str]):
        chars = self.char_mgr.list_characters()
        if not chars:
            print("⚠️ [CHAR] No characters found.")
            return
        if requested and requested in chars:
            name = requested
        elif len(chars) == 1:
            name = chars[0]
        else:
            print(f"\n🎭 Characters: {', '.join(chars)}")
            choice = input(f"Select [{chars[0]}]: ").strip()
            name   = choice if choice in chars else chars[0]
        if self.char_mgr.load(name):
            set_character(name, self.char_mgr.character)
        else:
            print(f"❌ [CHAR] Failed to load '{name}'")

    # ── Cleanup loop ──────────────────────────────────────────────────────

    def _start_cleanup_loop(self):
        def _loop():
            while True:
                time.sleep(CLEANUP_INTERVAL)
                try:
                    self._cleanup()
                except Exception as e:
                    print(f"⚠️ [CLEANUP] {e}")
        threading.Thread(target=_loop, daemon=True, name="cleanup").start()

    def _cleanup(self):
        now = now_ts()
        for uid in list(self._spam_times):
            fresh = [t for t in self._spam_times[uid] if now - t < SPAM_WINDOW_SEC]
            if fresh:
                self._spam_times[uid] = fresh
            else:
                del self._spam_times[uid]
        for uid in list(self._spam_warned):
            if now - self._spam_warned[uid] > SPAM_COOLDOWN_SEC:
                del self._spam_warned[uid]
        self._dedup = {k: v for k, v in self._dedup.items()
                       if now - v["ts"] < DEDUP_WINDOW_SEC}
        collected = gc.collect()
        print(f"🧹 [CLEANUP] users={self.user_mgr.size()} "
              f"q={self.pipeline.queue_size()} gc={collected}")
        self._flush_log(force=True)

    # ── Chat log ──────────────────────────────────────────────────────────

    def _flush_log(self, force=False):
        self._log_dirty += 1
        if force or self._log_dirty >= 20:
            _save_json(CHAT_LOG_PATH, list(self._chat_log))
            self._log_dirty = 0

    def _log(self, etype, uid, uname, extra=None):
        entry = {"ts": now_iso(), "type": etype,
                 "user_id": uid, "username": uname}
        if extra:
            entry.update(extra)
        self._chat_log.append(entry)
        self._flush_log()

    # ── User init ─────────────────────────────────────────────────────────

    def _ensure_user(self, uid: str, username: str):
        if uid in self._initialized_users:
            return
        self._initialized_users.add(uid)
        mem = self.user_mgr.get(uid, username)
        mem.save()

    # ── Live2D helpers ────────────────────────────────────────────────────

    def _tap_like(self, uname, count):
        self.l2d.send_tap_status(f"❤️ {uname} +{count}",
                                  r=255, g=80, b=80, a=0.9, duration=2000)

    def _tap_follow(self, uname):
        self.l2d.send_tap_status(f"💚 {uname} followed!",
                                  r=80, g=255, b=120, a=0.9, duration=2500)

    def _tap_gift(self, uname, gift, count):
        txt = f"🎁 {uname}: {gift}" + (f" ×{count}" if count > 1 else "")
        self.l2d.send_tap_status(txt, r=255, g=215, b=0, a=0.9, duration=3000)

    def _tap_share(self, uname):
        self.l2d.send_tap_status(f"📤 {uname} shared!",
                                  r=100, g=160, b=255, a=0.85, duration=2000)

    def _music_status(
        self,
        message: str,
        *,
        r: int = 180,
        g: int = 120,
        b: int = 255,
        a: float = 0.95,
        duration: int = 3500,
    ):
        self.l2d.send_tap_status(
            message,
            r=r,
            g=g,
            b=b,
            a=a,
            duration=duration,
        )

    # ── Spam ──────────────────────────────────────────────────────────────

    def _check_spam(self, uid) -> Tuple[bool, bool]:
        now = now_ts()
        if uid in self._spam_warned:
            if now - self._spam_warned[uid] > SPAM_COOLDOWN_SEC:
                del self._spam_warned[uid]
                self._spam_times.pop(uid, None)
        times = [t for t in self._spam_times[uid] if now - t < SPAM_WINDOW_SEC]
        times.append(now)
        self._spam_times[uid] = times
        return len(times) >= SPAM_MSG_LIMIT, uid in self._spam_warned

    # ── Dedup ─────────────────────────────────────────────────────────────

    def _register_msg(self, content, uid, uname) -> bool:
        now  = now_ts()
        self._dedup = {k: v for k, v in self._dedup.items()
                       if now - v["ts"] < DEDUP_WINDOW_SEC}
        norm = content.strip().lower()
        if norm in self._dedup:
            self._dedup[norm]["uids"].append(uid)
            return False
        self._dedup[norm] = {"ts": now, "content": content,
                              "uids": [uid], "unames": [uname]}
        return True

    # ── Batch ─────────────────────────────────────────────────────────────

    def _schedule_batch(self):
        with self._batch_lock:
            if self._batch_timer:
                self._batch_timer.cancel()
            t = threading.Timer(BATCH_WINDOW_SEC, self._flush_batch)
            t.daemon = True
            t.start()
            self._batch_timer = t

    def _flush_batch(self):
        with self._pending_lock:
            if not self._pending:
                return
            batch          = self._pending.copy()
            self._pending.clear()

        groups: Dict[str, Dict] = {}
        for msg in batch:
            norm = msg["content"].strip().lower()
            if norm not in groups:
                groups[norm] = {"content": msg["content"],
                                 "items": [], "is_spam": False}
            groups[norm]["items"].append(msg)
            if msg.get("is_spam"):
                groups[norm]["is_spam"] = True

        for group in groups.values():
            items   = group["items"]
            content = group["content"]
            is_spam = group["is_spam"]

            seen, unique = set(), []
            for m in items:
                if m["user_id"] not in seen:
                    seen.add(m["user_id"])
                    unique.append(m)

            uid0   = unique[0]["user_id"]
            uname0 = unique[0]["username"]

            if len(unique) == 1:
                model_input = (
                    f"[SPAM] {uname0} spam: \"{content}\". Tegur singkat."
                    if is_spam else f"{uname0}: {content}"
                )
            else:
                names    = [u["username"] for u in unique[:4]]
                name_str = (f"{names[0]} dan {names[1]}" if len(names) == 2
                            else ", ".join(names[:-1]) + f" dan {names[-1]}")
                model_input = f"{name_str} bertanya: {content}"

            self.banter.interrupt()
            self.pipeline.push(model_input, uid0, uname0)

    # ── Gift processing ───────────────────────────────────────────────────

    def _process_gift(self, uid: str, uname: str, gift_name: str, count: int):
        mem = self.user_mgr.get(uid, uname)
        mem.add_gift(gift_name, count)
        self.tracker.track_gift(uname, gift_name, count)

        if uid not in self._gift_trigger:
            self._gift_trigger[uid] = random.randint(GIFT_TRIGGER_MIN,
                                                      GIFT_TRIGGER_MAX)
        total = self.tracker.get_gifter_total(uname)
        if total >= self._gift_trigger[uid]:
            gift_summary = mem.get_gift_summary()
            inp = (f"{uname} sudah memberikan gift {gift_name} ×{count}"
                   f"! Total gift: {gift_summary}. Ucapkan terima kasih spesial!")
            print(f"🎁 [GIFT TRIGGER] {inp}")
            self.banter.interrupt()
            self.pipeline.push(inp, uid, uname)
            self._gift_trigger[uid] = total + random.randint(
                GIFT_TRIGGER_MIN, GIFT_TRIGGER_MAX
            )

    # ── Pipeline idle ─────────────────────────────────────────────────────

    def _on_pipeline_idle(self):
        if _main._chat_hist:
            self.banter.chat_history = _main._chat_hist
        if _main._model_mem:
            self.banter.model_memory = _main._model_mem
        self._ensure_banter_pool()
        self.banter.start_idle_timer()

    def _ensure_banter_pool(self):
        pool = getattr(self.banter, "_banters_avail", None)
        if pool is not None and len(pool) == 0:
            def _refill():
                try:
                    self.banter._replenish_story()
                except Exception as e:
                    print(f"❌ [BANTER] Refill error: {e}")
            threading.Thread(target=_refill, daemon=True,
                             name="banter-refill").start()

    # ── Live2D event ──────────────────────────────────────────────────────

    def _on_l2d_event(self, etype, data):
        if etype == "lipSyncFinished":
            print("✅ [L2D] LipSync selesai")

    # ── Special commands ──────────────────────────────────────────────────

    def _handle_opening(self):
        print("🎬 [LIVE] Opening...")
        prompt = build_opening_prompt(_ACTIVE_CHAR_NAME, CHARACTER)
        responses, dominant = generate_from_prompt(
            prompt, CHARACTER, char_name=self.char_mgr.active or "default"
        )
        done = threading.Event()
        play_segments(responses, dominant, self.l2d, on_complete=done.set)
        done.wait(timeout=60)
        self._opening_done = True
        print("✅ [LIVE] Opening selesai")
        self.banter.start_idle_timer()

    def _handle_closing(self):
        print("🔴 [LIVE] Ending...")
        self.banter.interrupt()
        char_name = self.char_mgr.active or "default"

        # ── 1. Closing speech ──────────────────────────────────────────────
        tracker_summary_str = "\n".join(
            f"- {k}: {v}" for k, v in self.tracker.get_summary().items()
            if v and k != "start_ts"
        )
        prompt_close = build_closing_prompt(
            _ACTIVE_CHAR_NAME, CHARACTER, tracker_summary=tracker_summary_str
        )
        close_responses, close_dominant = generate_from_prompt(
            prompt_close, CHARACTER, char_name=char_name
        )
        done1 = threading.Event()
        play_segments(close_responses, close_dominant, self.l2d,
                      on_complete=done1.set)
        done1.wait(timeout=90)

        # ── 2. Special thanks ──────────────────────────────────────────────
        thanks_prompt = self.tracker.build_thanks_prompt(
            _ACTIVE_CHAR_NAME, CHARACTER
        )
        thanks_responses, thanks_dominant = generate_from_prompt(
            thanks_prompt, CHARACTER, char_name=char_name
        )
        done2 = threading.Event()
        play_segments(thanks_responses, thanks_dominant, self.l2d,
                      on_complete=done2.set)
        done2.wait(timeout=90)

        print("✅ [LIVE] Closing selesai")
        self._flush_log(force=True)
        self.banter.stop()

    # ── Event handlers ────────────────────────────────────────────────────

    def _on_comment(self, ev):
        uid     = ev.get("user_id",  "unknown")
        uname   = ev.get("username", "Unknown")
        content = ev.get("comment",  "").strip()
        if not content:
            return

        if content.strip() == "#op":
            threading.Thread(target=self._handle_opening,
                             daemon=True, name="opening").start()
            return
        if content.strip() == "#end":
            threading.Thread(target=self._handle_closing,
                             daemon=True, name="closing").start()
            return
        if content.strip() == "#chl":
            from main import ADMIN_USER_ID
            if uid == ADMIN_USER_ID or uname == ADMIN_USER_ID:
                ch = _main._chat_hist
                if ch:
                    try:
                        for attr in ("_messages", "messages", "_history"):
                            if hasattr(ch, attr):
                                setattr(ch, attr, [])
                                break
                        ch.save()
                        _main._last_assistant_response = ""
                        print("✅ [CHL] Chat history dihapus.")
                        self.l2d.show_system_log("🧹 Chat history cleared")
                    except Exception as e:
                        print(f"❌ [CHL] Error: {e}")
            return

        if not self._opening_done:
            print(f"🔒 [OPENING] Block: {uname}: {content[:40]}")
            return

        

        prefix = f"{uname}:"
        if content.lower().startswith(prefix.lower()):
            content = content[len(prefix):].strip()
        if not content:
            return

        self._ensure_user(uid, uname)
        self._log("comment", uid, uname, {"comment": content})
        self.tracker.track_chat(uname)

        # ── Music commands ─────────────────────────────────────────────────
        # #req <judul>  — request / tambah lagu ke antrian
        #if content.lower().startswith("#req "):
        #    query = content[5:].strip()
        #    if query:                
        #        self._music_status(
        #            f"🔍 Searching: {query}",
        #            r=120, g=180, b=255,
        #            duration=2200,
        #        )
        #        def _do_req(q=query, u=uname):
        #            msg = self.l2d.music_request(q)
        #            self._music_status(
        #                f"{u}: {msg}",
        #                r=180, g=120, b=255,
        #                duration=4000,
        #            )
        #        threading.Thread(target=_do_req, daemon=True, name="music-req").start()
        #    return

        # #rm <judul>   — hapus lagu dari antrian
        if content.lower().startswith("#rm "):
            query = content[4:].strip()
            if query:
                def _do_rm(q=query, u=uname):
                    msg = self.l2d.music_remove(q)
                    self._music_status(f"{u}: {msg}", r=255, g=120, b=120, duration=4000)
                    self.l2d.show_system_log(f"{u}: {msg}")
                threading.Thread(target=_do_rm, daemon=True, name="music-rm").start()
            return

        # #lm           — tampilkan antrian musik
       # if content.strip().lower() == "#list":
       #     def _do_lm():
       #         msg = self.l2d.music_list()
       #         #self.l2d.show_system_log(msg)
       #         self._music_status(
       #             msg,
       #             r=120, g=220, b=255,
       #             duration=6000,
       #         )
       #     threading.Thread(target=_do_lm, daemon=True, name="music-lm").start()
       #     return

        if content.strip().lower() == "#vll":
            def _do_vll():
                msg = self.l2d.music_list_languages()
                # Menggunakan tema Cokelat Gelap agar tulisan putih lirik terlihat kontras
                self._music_status(
                    msg,
                    r=65, g=43, b=21, 
                    duration=5000,
                )
            threading.Thread(target=_do_vll, daemon=True, name="music-vll").start()
            return

        # ── NEW COMMAND: #sl <kode> (Set Language / Pilih Bahasa Lirik) ──
        if content.strip().lower().startswith("#sl"):
            def _do_sl():
                # Mengambil argumen setelah "#sl " (misal: #sl ja -> ja)
                parts = content.strip().split(maxsplit=1)
                if len(parts) < 2:
                    msg = "❌ Format salah. Gunakan: #sl <kode_bahasa> (Contoh: #sl ja)"
                else:
                    lang_code = parts[1]
                    msg = self.l2d.music_set_language(lang_code)
                
                # Menampilkan respons status perubahan bahasa dengan kotak cokelat
                self._music_status(
                    msg,
                    r=65, g=43, b=21,
                    duration=4000,
                )
            threading.Thread(target=_do_sl, daemon=True, name="music-sl").start()
            return
        # #skip         — skip lagu sekarang
       # if content.strip().lower() == "#skip":
       #     def _do_skip():
       #         msg = self.l2d.music_skip()
       #         self._music_status(msg, r=120, g=220, b=255, duration=3000)
       #     threading.Thread(target=_do_skip, daemon=True, name="music-skip").start()
       #     return

        # #stop         — hentikan musik
       # if content.strip().lower() == "#stop":
       #     def _do_stop():
       #         msg = self.l2d.music_stop()
       #         self._music_status(msg, r=255, g=120, b=120, duration=3000)
       #     threading.Thread(target=_do_stop, daemon=True, name="music-stop").start()
       #     return

        # #play         — lanjutkan / mainkan dari antrian
        #if content.strip().lower() == "#play":
        #    def _do_play():
        #        msg = self.l2d.music_play()
        #        self._music_status(msg, r=120, g=255, b=160, duration=3000)
        #    threading.Thread(target=_do_play, daemon=True, name="music-play").start()
        #    return

        # #el           — toggle lirik lagu yang sedang diputar
        # ✅ BENAR — thread.start() di LUAR fungsi
        if content.strip().lower() == "#el":
            def _do_el():
                msg = self.l2d.music_toggle_lyric()
                self._music_status(msg, r=255, g=120, b=220, duration=4000)
            threading.Thread(target=_do_el, daemon=True, name="music-lyric").start()
            return

        # #cm           — clear semua antrian + stop musik
        if content.strip().lower() == "#cm":
            def _do_cm():
                msg = self.l2d.music_clear()
                self._music_status(msg, r=255, g=160, b=80, duration=3000)
            threading.Thread(target=_do_cm, daemon=True, name="music-cm").start()
            return
        # ── End music commands ─────────────────────────────────────────────

        if content.startswith("#") or content.startswith("@"):
            return

        self.l2d.send_tap_status(content, r=255, g=215, b=0, a=0.9, duration=2000)

        is_spam, warned = self._check_spam(uid)
        if is_spam and warned:
            return
        if is_spam and not warned:
            self._spam_warned[uid] = now_ts()

        if not self._register_msg(content, uid, uname):
            return

        with self._pending_lock:
            self._pending.append({
                "user_id": uid, "username": uname,
                "content": content, "is_spam": is_spam,
                "ts": now_ts(),
            })
        self._schedule_batch()

    def _on_like(self, ev):
        uid   = ev.get("user_id",   "unknown")
        uname = ev.get("username",  "Unknown")
        tap   = ev.get("tap_count", 1)
        self._log("like", uid, uname, {"tap_count": tap})
        self.tracker.track_like(uname, tap)
        self._tap_like(uname, tap)
        self.l2d.random_tap_all()
        self.l2d.show_system_log(f"❤️ {uname} like ×{tap}")

    def _on_follow(self, ev):
        uid   = ev.get("user_id",  "unknown")
        uname = ev.get("username", "Unknown")
        self._log("follow", uid, uname)
        self.tracker.track_follow(uname)
        self._tap_follow(uname)
        self.l2d.show_system_log(f"💚 {uname} followed!")

    def _on_gift(self, ev):
        uid       = ev.get("user_id",    "unknown")
        uname     = ev.get("username",   "Unknown")
        gift_name = ev.get("gift_name",  "Gift")
        count     = ev.get("count",      1)
        if ev.get("streakable") and ev.get("streaking"):
            return
        self._ensure_user(uid, uname)
        self._log("gift", uid, uname, {"gift_name": gift_name, "count": count})
        self._tap_gift(uname, gift_name, count)
        self.l2d.show_system_log(f"🎁 {uname} → {gift_name} ×{count}")
        self._process_gift(uid, uname, gift_name, count)

    def _on_join(self, ev):
        uid   = ev.get("user_id",  "unknown")
        uname = ev.get("username", "Unknown")
        self._log("join", uid, uname)
        self.l2d.show_system_log(f"👋 {uname} joined")

    def _on_share(self, ev):
        uid   = ev.get("user_id",  "unknown")
        uname = ev.get("username", "Unknown")
        self._ensure_user(uid, uname)
        self._log("share", uid, uname)
        self.tracker.track_share(uname)
        self._tap_share(uname)
        self.l2d.show_system_log(f"📤 {uname} shared")

    def _on_live_end(self, _ev):
        threading.Thread(target=self._handle_closing, daemon=True,
                         name="closing-auto").start()

    # ── Dispatcher ────────────────────────────────────────────────────────

    def dispatch(self, raw: str):
        try:
            ev = json.loads(raw)
        except json.JSONDecodeError:
            return
        {
            "comment":  self._on_comment,
            "like":     self._on_like,
            "follow":   self._on_follow,
            "gift":     self._on_gift,
            "join":     self._on_join,
            "share":    self._on_share,
            "live_end": self._on_live_end,
        }.get(ev.get("type", ""), lambda _: None)(ev)

    # ── WS loop ───────────────────────────────────────────────────────────

    async def _ws_loop(self):
        delay = 5
        while True:
            try:
                print(f"🔌 [WS] Connecting {self.ws_url}...")
                async with websockets.connect(self.ws_url) as ws:
                    print("✅ [WS] Terhubung!")
                    delay = 5
                    async for msg in ws:
                        self.dispatch(msg)
            except (websockets.ConnectionClosed,
                    ConnectionRefusedError, OSError) as e:
                print(f"❌ [WS] {e}")
            except Exception as e:
                print(f"❌ [WS] Error: {e}")
            print(f"⏳ [WS] Reconnect {delay}s...")
            await asyncio.sleep(delay)
            delay = min(delay * 2, 60)

    # ── Run ───────────────────────────────────────────────────────────────

    def run(self):
        print("\n" + "=" * 62)
        print(f"🚀  LIVE SERVER  [{self.char_mgr.active}]  —  TikTok → Live2D")
        print("    Kirim  #op  di chat untuk OPENING")
        print("    Kirim  #end di chat untuk CLOSING + THANKS")
        print("=" * 62)

        self.tracker.reset()
        print("⏳ [LIVE] Siap. Kirim #op di chat untuk mulai opening live.")

        # Windows: asyncio.run() kadang tidak melempar KeyboardInterrupt
        # dengan bersih saat Ctrl+C — wrap dengan try/finally agar cleanup selalu jalan.
        try:
            asyncio.run(self._ws_loop())
        except (KeyboardInterrupt, SystemExit):
            pass
        finally:
            print("\n👋 Live Server dihentikan.")
            self._flush_log(force=True)
            try:
                self.banter.stop()
            except Exception:
                pass
            try:
                self.l2d.shutdown()
            except Exception:
                pass


# ═════════════════════════════════════════════════════════════════════════════
# ENTRY POINT
# ═════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import argparse

    # ── Auto-buka Settings UI (non-blocking, jalan di thread terpisah) ────────
    open_settings_async()

    p = argparse.ArgumentParser(description="Live Server")
    p.add_argument("--character", "-c", default=None, help="Character name to load")
    p.add_argument("--ws",        default=WS_APP_URL,   help="WebSocket URL")
    p.add_argument("--l2d-http",  default=L2D_HTTP_URL, help="Live2D HTTP URL")
    p.add_argument("--l2d-ws",    default=L2D_WS_URL,   help="Live2D WS URL")
    args = p.parse_args()

    LiveServer(
        ws_url         = args.ws,
        l2d_http       = args.l2d_http,
        l2d_ws         = args.l2d_ws,
        character_name = args.character,
    ).run()