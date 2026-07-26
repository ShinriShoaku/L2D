"""
banter_manager.py — Idle banter dengan Story Arc System.

Story Arc:
  - Semua banter mengikuti SATU alur cerita yang berkelanjutan.
  - Saat pool menipis, AI otomatis generate kelanjutan cerita dari topik yang sama.
  - Setelah STORY_ARC_LENGTH banter selesai dimainkan, topik baru dipilih dan arc baru dimulai.
  - Tidak ada lagi pool terpisah "banter_generation" vs "scenario"; semua pakai scenario_continuation.

Pipeline:
  raw items → ANIM+TRANS pipeline → TTS-ready pool
"""

import json
import os
import random
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from typing import Callable, Dict, List, Optional

# ── Tuning ────────────────────────────────────────────────────────────────────
IDLE_TIMEOUT_SEC    = 8
REPLENISH_THRESHOLD = 3
PAUSE_BETWEEN_ITEMS = 1.5
PLAY_TIMEOUT_SEC    = 45

# Story Arc settings
STORY_ARC_LENGTH    = 8   # Jumlah banter sebelum ganti topik baru
STORY_CONTEXT_KEEP  = 3   # Jumlah banter terakhir yang dikirim sebagai konteks
INITIAL_BATCH_SIZE  = 5   # Batch pertama saat arc dimulai
REPLENISH_BATCH_SIZE= 4   # Batch saat replenish tengah arc


class BanterManager:
    """
    Manages idle banter dengan story arc system, interrupt phrases,
    opening/closing.

    Args:
        character_cfg  — dict dari character.json
        scenarios_path — path ke scenario.json (berisi idle_topics)
        banter_path    — path ke banter.json (optional pre-seeded banters)
        generate_fn    — fn(prompt, max_tokens, schema=None) → str|None
        pipeline_fn    — fn(raw_items: List[Dict]) → List[Dict]  (ANIM+TRANS)
        play_fn        — fn(segments, expression, on_complete=None)
        pipeline_qs_fn — fn() → int  (current pipeline queue size)
        banter_schema  — JSON schema untuk banter generation
        chat_history   — ChatHistory instance (untuk context injection)
        model_memory   — ModelMemory instance (untuk topic injection)
    """

    def __init__(
        self,
        character_cfg:  Dict,
        scenarios_path: str,
        banter_path:    str,
        generate_fn:    Callable,
        pipeline_fn:    Callable,
        play_fn:        Callable,
        pipeline_qs_fn: Callable,
        banter_schema:  Dict,
        chat_history    = None,
        model_memory    = None,
    ):
        self.char          = character_cfg
        self.generate_fn   = generate_fn
        self.pipeline_fn   = pipeline_fn
        self.play_fn       = play_fn
        self.qs            = pipeline_qs_fn
        self.banter_schema = banter_schema
        self.chat_history  = chat_history
        self.model_memory  = model_memory

        # ── Load data ─────────────────────────────────────────────────────
        self._banter_seed:  List[Dict] = self._load_json(banter_path, [])
        self._scenarios:    Dict       = self._load_json(scenarios_path, {})

        # ── Single unified pool (TTS-ready processed items) ───────────────
        self._banters_avail: deque = deque(self._banter_seed)

        # ── Story Arc State ────────────────────────────────────────────────
        self._current_topic:       str        = ""   # topik aktif saat ini
        self._story_context:       List[str]  = []   # teks banter terakhir (untuk continuity)
        self._story_banter_count:  int        = 0    # banter dimainkan di arc ini
        self._arc_exhausted:       bool       = False

        # ── General State ──────────────────────────────────────────────────
        self._used_banter_ids: set = set()
        self._banter_count         = 0      # total banter dimainkan
        self._is_playing           = False
        self._generating           = False
        self._stopped              = False

        self._interrupt_evt  = threading.Event()
        self._stop_evt       = threading.Event()
        self._interrupt_done = threading.Event()
        self._interrupt_done.set()

        self._idle_timer: Optional[threading.Timer] = None
        self._timer_lock  = threading.Lock()

        self._executor = ThreadPoolExecutor(max_workers=2,
                                             thread_name_prefix="banter")
        print("🎲 [BanterManager] Initialised (Story Arc System)")

    # ══ PUBLIC API ════════════════════════════════════════════════════════════

    def start_idle_timer(self):
        if self._stop_evt.is_set():
            return
        with self._timer_lock:
            if self._idle_timer:
                self._idle_timer.cancel()
            t = threading.Timer(IDLE_TIMEOUT_SEC, self._on_idle_fire)
            t.daemon = True
            t.start()
            self._idle_timer = t

    def interrupt(self):
        self._interrupt_evt.set()
        if self.char.get("interrupt_phrases"):
            self._interrupt_done.clear()
        with self._timer_lock:
            if self._idle_timer:
                self._idle_timer.cancel()
                self._idle_timer = None

    def wait_interrupt_done(self, timeout: float = 8.0) -> bool:
        return self._interrupt_done.wait(timeout=timeout)

    def play_opening(self, on_done: Optional[Callable] = None):
        openings = self._scenarios.get("opening", [])
        if not openings:
            if on_done:
                on_done()
            return
        self._is_playing = True
        threading.Thread(
            target=self._play_item_blocking,
            args=(openings[0], on_done),
            daemon=True,
            name="opening-play"
        ).start()

    def play_closing(self, summary: str, on_done: Optional[Callable] = None):
        def _run():
            prompt = self.char.get("prompts", {}).get("closing", "").format(
                summary=summary
            )
            raw  = self.generate_fn(prompt, max_tokens=500)
            if not raw:
                if on_done:
                    on_done()
                return
            data = self._parse_json(raw)
            if not data:
                if on_done:
                    on_done()
                return
            if "segments" in data:
                segments   = data["segments"]
                expression = data.get("expression", "smile")
            elif "responses" in data:
                segments   = data["responses"]
                expression = data.get("anim", "smile")
            else:
                if on_done:
                    on_done()
                return
            from main import clean_jp_for_tts
            for seg in segments:
                seg["jp"] = clean_jp_for_tts(seg.get("jp", ""))
            self._play_item_blocking(
                {"segments": segments, "expression": expression}, on_done
            )
        threading.Thread(target=_run, daemon=True, name="closing-play").start()

    def stop(self):
        self._stop_evt.set()
        self._interrupt_evt.set()
        with self._timer_lock:
            if self._idle_timer:
                self._idle_timer.cancel()
        self._executor.shutdown(wait=False)

    # ══ INTERNAL: IDLE & BANTER ════════════════════════════════════════════════

    def _on_idle_fire(self):
        if self._stop_evt.is_set():
            return
        # self.qs() sekarang memanggil pipeline.has_work() — True jika queue/gen/play aktif
        if self.qs():
            self.start_idle_timer()
            return
        if not self._is_playing:
            self._start_banter_loop()

    def _start_banter_loop(self):
        self._is_playing = True
        self._interrupt_evt.clear()
        threading.Thread(
            target=self._banter_loop,
            daemon=True,
            name="banter-main-loop"
        ).start()

    def _banter_loop(self):
        try:
            while not self._stop_evt.is_set():
                if self._interrupt_evt.is_set() or self.qs():
                    self._play_interrupt_phrase()
                    return

                item = self._get_next_item()
                if item is None:
                    if not self._generating:
                        self._executor.submit(self._replenish_story)
                    time.sleep(2)
                    continue

                # Trigger replenish saat pool mulai menipis
                if len(self._banters_avail) < REPLENISH_THRESHOLD and not self._generating:
                    self._executor.submit(self._replenish_story)

                for seg in item.get("segments", []):
                    if self._interrupt_evt.is_set() or self.qs():
                        self._play_interrupt_phrase()
                        return
                    from main import clean_jp_for_tts
                    seg["jp"] = clean_jp_for_tts(seg.get("jp", ""))

                    done = threading.Event()
                    self.play_fn(
                        [seg], item.get("expression", "default"),
                        on_complete=done.set
                    )
                    # Poll tiap 200ms agar bisa interrupt di tengah playback
                    deadline = time.monotonic() + PLAY_TIMEOUT_SEC
                    while not done.wait(timeout=0.2):
                        if self._interrupt_evt.is_set() or self.qs():
                            # Tunggu segmen ini selesai dulu baru interrupt
                            # agar audio tidak putus di tengah kalimat
                            done.wait(timeout=PLAY_TIMEOUT_SEC)
                            self._play_interrupt_phrase()
                            return
                        if time.monotonic() >= deadline:
                            break  # timeout safety

                self._banter_count       += 1
                self._story_banter_count += 1

                # Simpan teks banter ke story context untuk continuity
                self._update_story_context(item)

                for _ in range(int(PAUSE_BETWEEN_ITEMS / 0.2)):
                    if self._interrupt_evt.is_set() or self.qs():
                        self._play_interrupt_phrase()
                        return
                    time.sleep(0.2)

        finally:
            self._is_playing = False
            # Pastikan interrupt_done tidak stuck jika loop exit tanpa interrupt phrase
            if not self._interrupt_done.is_set():
                self._interrupt_done.set()

    def _update_story_context(self, item: Dict):
        """Simpan teks banter yang baru dimainkan ke story context."""
        segs = item.get("segments", [])
        if segs:
            # Ambil teks Indonesia dari semua segment, gabung jadi satu entry
            combined = " ".join(s.get("ind", "") for s in segs if s.get("ind"))
            if combined:
                self._story_context.append(combined)
                # Batasi context buffer
                if len(self._story_context) > STORY_CONTEXT_KEEP * 2:
                    self._story_context = self._story_context[-STORY_CONTEXT_KEEP:]

    def _play_interrupt_phrase(self):
        phrases = self.char.get("interrupt_phrases", [])
        if not phrases:
            self._interrupt_done.set()
            self._is_playing = False
            return
        phrase = random.choice(phrases)
        done   = threading.Event()
        self.play_fn(
            [{"ind": phrase["ind"], "jp": phrase["jp"]}],
            phrase.get("expression", "default"),
            on_complete=done.set
        )
        done.wait(timeout=PLAY_TIMEOUT_SEC)
        self._is_playing = False
        self._interrupt_done.set()

    def _play_item_blocking(self, item: Dict, on_done: Optional[Callable]):
        try:
            for seg in item.get("segments", []):
                done = threading.Event()
                self.play_fn(
                    [seg], item.get("expression", "default"),
                    on_complete=done.set
                )
                done.wait(timeout=PLAY_TIMEOUT_SEC)
        finally:
            self._is_playing = False
            if on_done:
                on_done()

    # ══ POOL MANAGEMENT ════════════════════════════════════════════════════════

    def _get_next_item(self) -> Optional[Dict]:
        """Ambil item berikutnya dari pool. Satu pool tunggal (story arc)."""
        if self._banters_avail:
            item = self._banters_avail.popleft()
            self._used_banter_ids.add(item.get("id", ""))
            return item
        return None

    def _pick_new_topic(self) -> str:
        """
        Pilih topik baru untuk arc berikutnya.
        Prioritas: model_memory.topik → idle_topics list → fallback default.
        """
        # Jika sedang ada topik aktif dari percakapan user, ikuti itu
        if self.model_memory and self.model_memory.topik not in ("general", "", None):
            return self.model_memory.topik

        # Pilih dari idle_topics di scenario.json (hindari topik yang baru saja dipakai)
        all_topics = self._scenarios.get("idle_topics", [])
        if all_topics:
            candidates = [
                t.get("name", "") for t in all_topics
                if t.get("name", "") != self._current_topic
            ]
            if candidates:
                return random.choice(candidates)
            # Kalau semua sudah dipakai, reset dan pilih ulang
            return random.choice(all_topics).get("name", "hari ini Liana")

        return "obrolan santai Liana hari ini"

    def _build_story_prompt(self, count: int) -> str:
        """
        Bangun prompt scenario_continuation dengan:
        - Topik aktif saat ini
        - Konteks banter sebelumnya (jika ada)
        - Hint dari chat history / model memory
        """
        base_prompt = self.char.get("prompts", {}).get("scenario_continuation", "")

        # Susun blok context cerita
        context_section = ""
        if self._story_context:
            ctx_lines = "\n".join(
                f"- {t}" for t in self._story_context[-STORY_CONTEXT_KEEP:]
            )
            context_section = (
                f"Konteks banter sebelumnya (lanjutkan dari sini secara natural):\n"
                f"{ctx_lines}"
            )

        # Inject chat hint kalau ada
        chat_hint = self._get_context_hint()
        if chat_hint and not context_section:
            # Hanya inject chat hint di awal arc (sebelum ada story context)
            context_section = chat_hint

        prompt = base_prompt.format(
            count=count,
            topic=self._current_topic,
            context_section=context_section,
        )
        return prompt

    def _get_context_hint(self) -> str:
        """Ambil hint dari chat history + model memory untuk banter context."""
        parts = []
        if self.model_memory:
            topic = self.model_memory.topik
            if topic and topic not in ("general", ""):
                parts.append(f"Topik terakhir percakapan: {topic}")
        if self.chat_history:
            hint = self.chat_history.get_last_topic_hint()
            if hint:
                parts.append(f"Konteks obrolan: {hint}")
        return "\n".join(parts) if parts else ""

    def _replenish_story(self):
        """
        Generate kelanjutan story arc menggunakan scenario_continuation.
        - Jika arc habis (story_banter_count >= STORY_ARC_LENGTH), pilih topik baru.
        - Kirim story_context sebagai konteks agar cerita sambung.
        - Proses raw items lewat ANIM+TRANS pipeline sebelum masuk pool.
        """
        if self._generating:
            return
        self._generating = True
        try:
            # ── Cek apakah perlu mulai arc baru ───────────────────────────
            needs_new_arc = (
                not self._current_topic
                or self._story_banter_count >= STORY_ARC_LENGTH
            )
            if needs_new_arc:
                new_topic = self._pick_new_topic()
                self._current_topic      = new_topic
                self._story_context      = []
                self._story_banter_count = 0
                count = INITIAL_BATCH_SIZE
                print(f"📖 [STORY] Arc baru dimulai: '{self._current_topic}'")
            else:
                count = REPLENISH_BATCH_SIZE
                print(
                    f"📖 [STORY] Melanjutkan arc '{self._current_topic}' "
                    f"(banter ke-{self._story_banter_count}/{STORY_ARC_LENGTH})"
                )

            # ── Build prompt ───────────────────────────────────────────────
            prompt = self._build_story_prompt(count)

            # ── Generate ───────────────────────────────────────────────────
            raw = self.generate_fn(prompt, 1400, schema=self.banter_schema)
            if not raw:
                return
            items = self._parse_json(raw)
            if not isinstance(items, list):
                return

            # ── Build raw list untuk ANIM+TRANS pipeline ───────────────────
            raw_items: List[Dict] = []
            for item in items:
                iid  = f"{item.get('id', 'sc_gen')}_{random.randint(10000, 99999)}"
                text = (item.get("text") or "").strip()
                if text and iid not in self._used_banter_ids:
                    raw_items.append({"id": iid, "text": text})

            if not raw_items:
                return

            # ── PIPELINE: ANIM + TRANS + MERGE ────────────────────────────
            processed = self.pipeline_fn(raw_items)
            # ─────────────────────────────────────────────────────────────

            added = 0
            for p in processed:
                iid = p.get("id")
                if iid and iid not in self._used_banter_ids:
                    self._banters_avail.append(p)
                    self._used_banter_ids.add(iid)
                    added += 1

            print(
                f"🎲 [STORY] +{added} banter TTS-ready "
                f"(pool={len(self._banters_avail)}, "
                f"topik='{self._current_topic}')"
            )

        except Exception as e:
            print(f"❌ [STORY] Replenish error: {e}")
        finally:
            self._generating = False

    # ══ UTILS ═════════════════════════════════════════════════════════════════

    @staticmethod
    def _load_json(path: str, default):
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception as e:
                print(f"⚠️ [BANTER] Load {path}: {e}")
        return default

    @staticmethod
    def _parse_json(raw: str):
        import re
        cleaned = re.sub(r"```(?:json)?\s*", "", raw, flags=re.IGNORECASE).strip()
        cleaned = re.sub(r"```\s*$", "", cleaned).strip()
        try:
            return json.loads(cleaned)
        except Exception:
            pass
        m = re.search(r"(\{.*\}|\[.*\])", cleaned, re.DOTALL)
        if m:
            try:
                return json.loads(m.group(0))
            except Exception:
                pass
        return None
