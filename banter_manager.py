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

import character_memory as _cmem_mod  # PATCH: reuse wants/facts utk kontinuitas banter

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

# PATCH: persistensi & anti-repeat lintas sesi
_STATE_FILE       = "banter_state.json"
TOPIC_HISTORY_KEEP = 4    # jumlah topik TERAKHIR yang di-exclude dari _pick_new_topic
                          # (dulu cuma exclude 1 topik terakhir — topik lain
                          # gampang balik lagi walau baru aja dibahas beberapa
                          # arc sebelumnya, apalagi idle_topics cuma ada segelintir)


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

        # PATCH: char_dir dipakai buat nyimpen state banter (banter_state.json)
        # bareng banter.json/scenarios.json karakter ini, dan buat akses
        # character_memory (wants/cita-cita) milik karakter yang sama.
        self._char_dir     = os.path.dirname(os.path.abspath(banter_path))
        self._character_id = character_cfg.get("id") or character_cfg.get("name") or "default"
        self._cmem = _cmem_mod.load(self._character_id, character_cfg, char_dir=self._char_dir)

        # ── Load data ─────────────────────────────────────────────────────
        self._banter_seed:  List[Dict] = self._load_json(banter_path, [])
        self._scenarios:    Dict       = self._load_json(scenarios_path, {})

        # ── PATCH: load state PERSISTEN dari sesi sebelumnya (kalau ada) ────
        # Sebelumnya _used_banter_ids/_topic_history cuma di memory, reset
        # tiap restart app — akibatnya 30 banter statis di banter.json main
        # ULANG dari awal SETIAP sesi live baru, kerasa banget ngulang buat
        # viewer yang nonton reguler. Sekarang di-load balik dari disk.
        _state = self._load_json(self._state_path(), {})
        _persisted_used_ids: set = set(_state.get("used_banter_ids", []))
        self._topic_history: List[str] = _state.get("topic_history", [])

        # ── Single unified pool (TTS-ready processed items) ────────────────
        # Filter banter.json yang SUDAH pernah dimainkan di sesi-sesi
        # sebelumnya — kalau semua 30 sudah pernah kepakai (deployment lama),
        # fallback: biarkan re-pool semua (lebih baik replay drpd kosong sama
        # sekali; pada titik itu seharusnya sudah didominasi konten generated).
        _fresh_seed = [b for b in self._banter_seed if b.get("id") not in _persisted_used_ids]
        if not _fresh_seed and self._banter_seed:
            _fresh_seed = list(self._banter_seed)
        self._banters_avail: deque = deque(_fresh_seed)

        # ── Story Arc State ────────────────────────────────────────────────
        self._current_topic:       str        = _state.get("current_topic", "")
        self._story_context:       List[str]  = _state.get("story_context", [])
        self._story_banter_count:  int        = _state.get("story_banter_count", 0)
        self._arc_exhausted:       bool       = False

        # ── General State ──────────────────────────────────────────────────
        self._used_banter_ids: set = _persisted_used_ids
        self._used_text_norms: set = set(_state.get("used_text_norms", []))
        # Seed dari teks banter.json statis juga — biar konten yang di-generate
        # LLM nanti gak kebetulan nge-duplikat salah satu cerita pre-written.
        for _b in self._banter_seed:
            _txt = " ".join(s.get("ind", "") for s in _b.get("segments", []) if s.get("ind"))
            if _txt:
                self._used_text_norms.add(" ".join(_txt.lower().split()))
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
                self._save_state()  # PATCH: persist tiap habis mainin 1 item

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
        """
        Simpan teks banter yang baru dimainkan ke story context (buat
        continuity generate berikutnya) DAN ke chat_history (buat user bisa
        nanya soal itu setelahnya).

        PATCH: root cause "user nanya soal yang baru diomongin di banter,
        karakter gagal jawab" — banter dulu HANYA lewat play_fn (didengar/
        ditampilkan doang), TIDAK PERNAH ditulis ke chat_history sama
        sekali. Jadi begitu user nanya follow-up, [HISTORY] yang dikirim ke
        Soul kosong soal itu — modelnya beneran tidak tahu apa-apa, bukan
        lupa. Sekarang ditulis sebagai pesan "assistant" biasa ke
        chat_history — sama seperti kalau dia ngomong itu langsung ke user,
        jadi otomatis kebaca [HISTORY] di turn chat berikutnya (dalam
        window MAX_HISTORY, lihat chat_history.py).
        """
        segs = item.get("segments", [])
        if segs:
            # Ambil teks Indonesia dari semua segment, gabung jadi satu entry
            combined = " ".join(s.get("ind", "") for s in segs if s.get("ind"))
            if combined:
                self._story_context.append(combined)
                # Batasi context buffer
                if len(self._story_context) > STORY_CONTEXT_KEEP * 2:
                    self._story_context = self._story_context[-STORY_CONTEXT_KEEP:]

                if self.chat_history:
                    try:
                        self.chat_history.add("assistant", combined)
                    except Exception as e:
                        print(f"⚠️ [BANTER] Gagal tulis ke chat_history: {e}")

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
        Prioritas: model_memory.topik → wants aktif (character_memory) →
        idle_topics list → fallback default.

        PATCH: 2 perubahan dari sebelumnya —
        1. Exclude SEMUA topik di _topic_history (rolling, TOPIC_HISTORY_KEEP
           terakhir), bukan cuma _current_topic — sebelumnya dengan idle_topics
           yang cuma segelintir, topik gampang balik lagi cuma beberapa arc
           kemudian.
        2. Sesekali (1/3 kesempatan) coba lanjutin "want"/cita-cita aktif dari
           character_memory (mis. "nabung buat torque wrench") sebagai topik,
           bukan selalu random dari idle_topics — ini yang bikin karakter
           kelihatan punya benang merah cerita antar sesi, bukan cuma
           lompat-lompat topik acak.
        """
        # Jika sedang ada topik aktif dari percakapan user, ikuti itu
        if self.model_memory and self.model_memory.topik not in ("general", "", None):
            return self.model_memory.topik

        # Sesekali lanjutkan want/cita-cita aktif — "scenario ke depan" yang
        # dia sendiri ingin capai, biar cerita terasa progres, bukan reset.
        wants = self._cmem.get_wants(5) if self._cmem else []
        if wants and random.random() < 0.34:
            want = random.choice(wants)
            if want not in self._topic_history[-TOPIC_HISTORY_KEEP:]:
                return want

        # Pilih dari idle_topics di scenario.json — exclude yang baru-baru
        # ini dipakai (rolling history, bukan cuma yang paling terakhir)
        all_topics = self._scenarios.get("idle_topics", [])
        if all_topics:
            recent = set(self._topic_history[-TOPIC_HISTORY_KEEP:]) | {self._current_topic}
            candidates = [t.get("name", "") for t in all_topics if t.get("name", "") not in recent]
            if candidates:
                return random.choice(candidates)
            # Semua idle_topics ada di riwayat baru-baru ini — kalau masih ada
            # want yang belum kepakai, pakai itu drpd terpaksa ulang topik lama
            if wants:
                fresh_wants = [w for w in wants if w not in self._topic_history[-TOPIC_HISTORY_KEEP:]]
                if fresh_wants:
                    return random.choice(fresh_wants)
            return random.choice(all_topics).get("name", "hari ini")

        return "obrolan santai hari ini"

    def _build_story_prompt(self, count: int) -> str:
        """
        Bangun prompt scenario_continuation dengan:
        - Topik aktif saat ini
        - Konteks banter sebelumnya (jika ada)
        - Hint dari chat history / model memory
        - PATCH: daftar topik yang SUDAH dibahas baru-baru ini (larangan
          eksplisit ulang tema), + want/cita-cita aktif sebagai bahan
          "scenario ke depan" yang bisa mulai diprogress ceritanya.
        """
        base_prompt = self.char.get("prompts", {}).get("scenario_continuation", "")

        # Susun blok context cerita
        context_parts = []
        if self._story_context:
            ctx_lines = "\n".join(
                f"- {t}" for t in self._story_context[-STORY_CONTEXT_KEEP:]
            )
            context_parts.append(
                f"Konteks banter sebelumnya (lanjutkan dari sini secara natural):\n{ctx_lines}"
            )

        # PATCH: larangan eksplisit ulang topik yang baru dibahas
        recent_topics = [t for t in self._topic_history[-TOPIC_HISTORY_KEEP:] if t and t != self._current_topic]
        if recent_topics:
            context_parts.append(
                "JANGAN ulangi tema/topik ini, sudah dibahas baru-baru ini: "
                + ", ".join(recent_topics)
            )

        # PATCH: want/cita-cita aktif — kalau topik arc ini SAMA dengan salah
        # satu want, minta ceritanya diprogress (bukan cuma diulang-ulang).
        wants = self._cmem.get_wants(5) if self._cmem else []
        if wants:
            if self._current_topic in wants:
                context_parts.append(
                    f"Topik arc ini adalah cita-cita/rencana yang sedang berjalan: "
                    f"'{self._current_topic}'. Lanjutkan progresnya (mis. sudah sejauh mana, "
                    f"ada perkembangan apa) — JANGAN cuma mengulang niatnya dari awal lagi."
                )
            else:
                context_parts.append(
                    "Cita-cita/rencana lain yang pernah disebut (boleh diselipkan sebagai "
                    "referensi ringan kalau natural, TIDAK WAJIB): " + ", ".join(wants[:3])
                )

        # Inject chat hint kalau ada
        chat_hint = self._get_context_hint()
        if chat_hint and not self._story_context:
            # Hanya inject chat hint di awal arc (sebelum ada story context)
            context_parts.append(chat_hint)

        prompt = base_prompt.format(
            count=count,
            topic=self._current_topic,
            context_section="\n\n".join(context_parts),
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
                # PATCH: sebelum arc lama di-reset, simpan ringkasannya ke
                # character_memory (custom_facts) — ini yang bikin banter
                # bisa "diingat" WALAU udah ke-geser dari chat_history (yang
                # cuma nyimpen 7 pesan terakhir). character_memory persisten,
                # jadi user masih bisa nanya soal arc yang udah lewat lama.
                if self._current_topic and self._story_context and self._cmem:
                    try:
                        arc_summary = f"{self._current_topic}: " + " ".join(self._story_context)
                        self._cmem.add_fact(arc_summary[:400])
                        _cmem_mod.save(self._cmem, char_dir=self._char_dir)
                    except Exception as e:
                        print(f"⚠️ [BANTER] Gagal simpan ringkasan arc: {e}")

                new_topic = self._pick_new_topic()
                self._current_topic      = new_topic
                self._story_context      = []
                self._story_banter_count = 0
                # PATCH: catat ke riwayat topik (rolling) — dipakai
                # _pick_new_topic() buat exclude topik yang baru dibahas
                self._topic_history.append(new_topic)
                if len(self._topic_history) > TOPIC_HISTORY_KEEP * 5:
                    self._topic_history = self._topic_history[-(TOPIC_HISTORY_KEEP * 5):]
                self._save_state()
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
            # PATCH: dulu dedup cuma berdasar ID, tapi ID generated SELALU
            # dikasih random suffix (lihat baris iid di bawah) — jadi cek
            # "iid not in _used_banter_ids" itu SELALU lolos, gak pernah
            # beneran nyaring apa pun. Sekarang dedup TAMBAHAN berdasar ISI
            # teks (dinormalisasi) — inilah yang beneran nyegah LLM generate
            # cerita yang mirip/sama berulang-ulang dalam satu sesi panjang.
            raw_items: List[Dict] = []
            for item in items:
                iid  = f"{item.get('id', 'sc_gen')}_{random.randint(10000, 99999)}"
                text = (item.get("text") or "").strip()
                norm = " ".join(text.lower().split())
                if text and norm not in self._used_text_norms:
                    raw_items.append({"id": iid, "text": text})
                    self._used_text_norms.add(norm)

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
            if added:
                self._save_state()

        except Exception as e:
            print(f"❌ [STORY] Replenish error: {e}")
        finally:
            self._generating = False

    # ══ UTILS ═════════════════════════════════════════════════════════════════

    def _state_path(self) -> str:
        """PATCH: file kecil di folder karakter ini utk nyimpen used_banter_ids,
        topic_history, dan progres arc — biar gak reset tiap restart app."""
        return os.path.join(self._char_dir, _STATE_FILE)

    def _save_state(self):
        """Dipanggil tiap ada perkembangan berarti (habis mainin item, ganti
        topik) — ringan (JSON kecil), aman dipanggil sesering itu."""
        try:
            # Batasi ukuran used_banter_ids yang disimpan — gak perlu simpan
            # SEMUA riwayat selamanya, cukup cukup besar buat nutup deployment
            # jangka panjang tanpa file numpuk gede.
            ids = list(self._used_banter_ids)[-2000:]
            norms = list(self._used_text_norms)[-2000:]
            state = {
                "used_banter_ids":    ids,
                "used_text_norms":    norms,
                "topic_history":      self._topic_history[-(TOPIC_HISTORY_KEEP * 5):],
                "current_topic":      self._current_topic,
                "story_context":      self._story_context[-STORY_CONTEXT_KEEP:],
                "story_banter_count": self._story_banter_count,
            }
            with open(self._state_path(), "w", encoding="utf-8") as f:
                json.dump(state, f, ensure_ascii=False, indent=2)
        except Exception as e:
            print(f"⚠️ [BANTER] Save state gagal: {e}")

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
