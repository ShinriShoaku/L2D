#!/usr/bin/env python3
"""
liveDesktop.py — TTS + Audio + Live2D standalone client.
Testing / dev tanpa TikTok live.
"""

import os
import queue
import sys
import threading
import subprocess
import time
import requests
from typing import Callable, Dict, List, Optional, Tuple

# ─── PLATFORM ─────────────────────────────────────────────────────────────────
IS_WINDOWS   = sys.platform == "win32"
_POPEN_FLAGS = subprocess.CREATE_NO_WINDOW if IS_WINDOWS else 0

from wav_detector import WavVoiceDetector
from live2d_client import Live2DClient
from character_manager import CharacterManager
from memory import UserMemory, UserMemoryManager
from settings_ui import open_settings_async
from main import (
    full_generate,
    generate_from_prompt,
    build_opening_prompt,
    build_closing_prompt,
    set_character,
    CHARACTER,
    _ACTIVE_CHAR_NAME,
    MEMORY_DIR,
    MODEL_MEMORY_DIR,
)
import main as _main
_main.DEBUG = False

# ─── TTS CONFIG ───────────────────────────────────────────────────────────────

TTS_SERVER_URL = "http://localhost:7861/api/tts_fast"
TTS_MODEL      = "misora"
TTS_PARAMS     = {
    "language":      "Japanese",
    "noise_scale":   0.6,
    "noise_scale_w": 0.8,
    "length_scale":  1.2,
    "is_symbol":     False,
}

DOCUMENTS_FOLDER = os.path.expanduser("~/Documents")
AUDIO_PATH       = os.path.join(DOCUMENTS_FOLDER, f"{TTS_MODEL}.wav")

L2D_MODEL_ID  = 0
L2D_MODEL_MAP = 0


# ─── AUDIO PLAYER ─────────────────────────────────────────────────────────────

class AudioPlayer:
    _instance: Optional["AudioPlayer"] = None
    _lock      = threading.Lock()

    @classmethod
    def get(cls) -> "AudioPlayer":
        with cls._lock:
            if cls._instance is None:
                cls._instance = AudioPlayer()
            return cls._instance

    def __init__(self):
        self._q:       queue.Queue = queue.Queue()
        self._worker   = threading.Thread(target=self._loop, daemon=True,
                                          name="audio-player")
        self._worker.start()
        self._pygame_ok = self._init_pygame()

    def _init_pygame(self) -> bool:
        try:
            import pygame
            # Pre-init sebelum init — kokoro TTS biasanya output 24000Hz, 16-bit, mono
            pygame.mixer.pre_init(frequency=24000, size=-16, channels=1, buffer=512)
            pygame.mixer.init()
            freq, size, ch = pygame.mixer.get_init()
            print(f"🔊 [AUDIO] pygame mixer OK — {freq}Hz, {size}bit, ch={ch}")
            return True
        except Exception as e:
            print(f"⚠️ [AUDIO] pygame tidak tersedia: {e}")
            return False

    def play(self, filepath: str, on_finish: Optional[Callable] = None):
        self._q.put((filepath, on_finish))

    def _loop(self):
        while True:
            filepath  = None
            on_finish = None
            try:
                filepath, on_finish = self._q.get()
                t0 = time.time()
                print(f"▶️  [AUDIO] Play: {os.path.basename(filepath)}")
                self._play_blocking(filepath)
                print(f"⏹️  [AUDIO] Done: {os.path.basename(filepath)} ({time.time()-t0:.2f}s)")
            except Exception as e:
                print(f"❌ [AUDIO] Worker error: {e}")
            if on_finish:
                try:
                    print(f"📣 [AUDIO] Callback fired")
                    on_finish()
                except Exception as e:
                    print(f"❌ [AUDIO] Callback error: {e}")

    def _play_blocking(self, filepath: str):
        if not os.path.exists(filepath):
            print(f"❌ [AUDIO] File tidak ada: {filepath}")
            return

        # ── 1. pygame — re-init mixer sesuai spec WAV aktual ─────────────
        if self._pygame_ok:
            try:
                import pygame
                import wave
                with wave.open(filepath, 'rb') as wf:
                    freq = wf.getframerate()
                    ch   = wf.getnchannels()
                    bits = wf.getsampwidth() * 8

                current = pygame.mixer.get_init()
                if not current or current != (freq, -bits, ch):
                    pygame.mixer.quit()
                    pygame.mixer.init(frequency=freq, size=-bits, channels=ch, buffer=512)

                sound = pygame.mixer.Sound(filepath)
                sound.play()
                while pygame.mixer.get_busy():
                    time.sleep(0.02)
                return
            except Exception as e:
                print(f"⚠️ [AUDIO] pygame error: {e}")
                self._pygame_ok = False

        # ── 2. winsound (Windows fallback) ───────────────────────────────
        if IS_WINDOWS:
            try:
                import winsound
                winsound.PlaySound(filepath, winsound.SND_FILENAME)
                return
            except Exception as e:
                print(f"⚠️ [AUDIO] winsound error: {e}")

        # ── 3. ffplay ────────────────────────────────────────────────────
        try:
            subprocess.run(
                ["ffplay", "-nodisp", "-autoexit", "-loglevel", "quiet", filepath],
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=_POPEN_FLAGS,
            )
            return
        except FileNotFoundError:
            pass
        except subprocess.CalledProcessError as e:
            print(f"⚠️ [AUDIO] ffplay error: {e}")

        # ── 4. aplay / afplay ────────────────────────────────────────────
        for cmd in (["aplay", filepath], ["afplay", filepath]):
            try:
                subprocess.run(cmd, check=True,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                return
            except (FileNotFoundError, subprocess.CalledProcessError):
                continue

        print(f"❌ [AUDIO] Semua metode play gagal: {filepath}")


_player = AudioPlayer.get()


def play_audio(filepath: str, on_finish_callback: Optional[Callable] = None) -> bool:
    if not filepath or not os.path.exists(filepath):
        print(f"❌ [AUDIO] File tidak ditemukan: {filepath}")
        if on_finish_callback:
            threading.Thread(target=on_finish_callback, daemon=True).start()
        return False

    # One-shot guard: pastikan callback hanya dipanggil SEKALI
    # meski ada race condition di player atau pygame event
    if on_finish_callback:
        _called = [False]
        _orig   = on_finish_callback
        def _safe_cb():
            if _called[0]:
                print("⚠️ [AUDIO] Callback dipanggil 2x, diabaikan")
                return
            _called[0] = True
            _orig()
        on_finish_callback = _safe_cb

    _player.play(filepath, on_finish_callback)
    return True


# ─── TTS CLIENT ───────────────────────────────────────────────────────────────

def request_tts(japanese_text: str) -> Optional[str]:
    payload = {"text": japanese_text, "model": TTS_MODEL, **TTS_PARAMS}
    try:
        resp = requests.post(TTS_SERVER_URL, json=payload, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        path = data.get("path")
        if not path:
            print(f"[TTS ERROR] Response tidak ada field 'path'. Raw response: {data}")
            return None
        if not os.path.exists(path):
            print(f"[TTS ERROR] Path dari server tidak ditemukan di filesystem lokal: '{path}'")
            return None
        return path
    except Exception as e:
        print(f"[TTS ERROR] {e}")
        return None


# ─── PLAY SEGMENTS ────────────────────────────────────────────────────────────

import shutil
import tempfile

def _tts_to_tempfile(jp_txt: str) -> Optional[str]:
    """
    Fetch TTS lalu copy hasilnya ke file temporary unik.
    Return path temp file, atau None jika gagal.

    WAJIB copy karena server selalu return path yang SAMA (mutsuki.wav).
    Tanpa copy, prefetch concurrent akan overwrite file yang sedang diplay.
    """
    src = request_tts(jp_txt)
    if not src or not os.path.exists(src):
        return None
    try:
        # Buat temp file di folder yang sama agar tidak cross-drive copy
        tmp_dir = os.path.dirname(src)
        fd, dst = tempfile.mkstemp(suffix=".wav", dir=tmp_dir)
        os.close(fd)
        shutil.copy2(src, dst)
        return dst
    except Exception as e:
        print(f"⚠️ [TTS] Gagal copy ke temp: {e}")
        return src   # fallback: pakai src langsung (tanpa prefetch aman)


def play_segments(
    responses:   List[Dict],
    expression:  str,
    l2d:         Live2DClient,
    on_complete: Optional[Callable] = None,
):
    """
    Play responses sequentially dengan prefetch TTS aman.
    Setiap segmen di-copy ke temp file unik sehingga prefetch concurrent
    tidak overwrite file yang sedang diplay.
    """
    l2d.set_expression(expression)

    def _run():
        _play_seq_safe(responses, expression, l2d, on_complete)

    threading.Thread(target=_run, daemon=True, name="play-seq").start()


def _play_seq_safe(
    responses:   List[Dict],
    expression:  str,
    l2d:         Live2DClient,
    on_complete: Optional[Callable],
):
    """
    Pipeline:
    1. Fetch+copy TTS seg[0] ke temp file (blocking, harus ada dulu)
    2. Mulai prefetch TTS seg[1] di background thread → temp file berbeda
    3. Play seg[0] dari temp file-nya (blocking via Event)
    4. Hapus temp file seg[0] setelah selesai
    5. Ambil hasil prefetch seg[1] (biasanya sudah selesai), ulangi dari 2
    """
    total = len(responses)
    if total == 0:
        if on_complete:
            on_complete()
        return

    # Fetch TTS segmen pertama (blocking, tidak ada yg bisa di-overlap sebelumnya)
    prefetch_result = [None]   # holder untuk audio path segmen berikutnya
    prefetch_thread = None

    # Fetch seg pertama secara blocking di thread ini
    current_audio = _tts_to_tempfile(responses[0].get("jp", ""))

    for idx in range(total):
        seg      = responses[idx]
        ind_txt  = seg.get("ind", "")
        jp_txt   = seg.get("jp",  "")
        seg_anim = seg.get("anim", expression)

        print(f"🎤 [{idx+1}/{total}] ({seg_anim}): {jp_txt[:40]}...")
        l2d.set_expression(seg_anim)

        # ── Mulai prefetch TTS segmen BERIKUTNYA di background ───────────
        # (aman karena masing-masing dapat temp file nama berbeda)
        next_audio_holder = [None]
        if idx + 1 < total:
            next_jp = responses[idx + 1].get("jp", "")
            prefetch_thread = threading.Thread(
                target=lambda h, t: h.__setitem__(0, _tts_to_tempfile(t)),
                args=(next_audio_holder, next_jp),
                daemon=True,
                name=f"tts-pre-{idx+1}",
            )
            prefetch_thread.start()
        else:
            prefetch_thread = None

        # ── Play segmen sekarang ──────────────────────────────────────────
        audio = current_audio
        if not audio or not os.path.exists(audio):
            print(f"❌ [TTS] Gagal segmen {idx+1}")
            l2d.show_chat_bubble(ind_txt, 3000)
            l2d.send_random_motions(L2D_MODEL_ID, "response")
            time.sleep(0.3)
        else:
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

            done_evt = threading.Event()
            play_audio(audio, on_finish_callback=done_evt.set)
            done_evt.wait(timeout=60)
            time.sleep(0.2)

            # Hapus temp file setelah selesai diplay
            try:
                if audio != AUDIO_PATH:   # jangan hapus file asli server
                    os.remove(audio)
            except Exception:
                pass

        # ── Tunggu prefetch selesai, siapkan untuk iterasi berikutnya ────
        if prefetch_thread is not None:
            prefetch_thread.join(timeout=30)
        current_audio = next_audio_holder[0]

    if on_complete:
        on_complete()


# ─── DESKTOP CLIENT ───────────────────────────────────────────────────────────

class DesktopClient:
    """Standalone client untuk testing / dev tanpa TikTok live."""

    def __init__(
        self,
        user_id:        str = "local",
        user_name:      str = "Shinri",
        character_name: str = None,
        l2d:            Optional[Live2DClient] = None,
    ):
        self.user_id   = user_id
        self.user_name = user_name
        self.char_name = character_name or "default"
        self.l2d       = l2d

        self._user_mgr = UserMemoryManager(storage_dir=MEMORY_DIR, max_cache=10)
        self.user_mem  = self._user_mgr.get(user_id, user_name)

    def chat(self, message: str) -> Tuple[List[Dict], str]:
        result = full_generate(
            message, self.user_mem,
            char_name = self.char_name,
            char_data = CHARACTER,
            username  = self.user_name,
        )
        # PENTING: full_generate() bisa memutasi self.user_mem (nickname,
        # romance_points, info, dst lewat ReAct mutate()) tapi TIDAK pernah
        # menulisnya ke disk sendiri. Tanpa save() di sini, perubahan cuma
        # hidup di RAM selama proses ini jalan — begitu restart/cache evict,
        # data balik ke versi lama di disk (bug "data balik ke semula").
        try:
            self.user_mem.save()
        except Exception as e:
            print(f"⚠️  [SAVE] gagal menyimpan user_mem: {e}")
        return result

    def opening(self) -> Tuple[List[Dict], str]:
        prompt = build_opening_prompt(_ACTIVE_CHAR_NAME, CHARACTER)
        return generate_from_prompt(prompt, CHARACTER, char_name=self.char_name)

    def closing(self) -> Tuple[List[Dict], str]:
        prompt = build_closing_prompt(_ACTIVE_CHAR_NAME, CHARACTER)
        return generate_from_prompt(prompt, CHARACTER, char_name=self.char_name)


# ─── MAIN LOOP ────────────────────────────────────────────────────────────────

def main():
    print("\n" + "=" * 60)
    print("🎙️  LIVE DESKTOP (standalone)")
    print("=" * 60)

    # ── Auto-buka Settings UI (non-blocking, jalan di thread terpisah) ────────
    open_settings_async()

    # ── Character selection ───────────────────────────────────────────────
    mgr   = CharacterManager()
    chars = mgr.list_characters()
    if not chars:
        print("⚠️  Tidak ada karakter. Buat characters/<name>/character.json")
        return

    print(f"\n🎭 Karakter: {', '.join(chars)}")
    chosen = input(f"Pilih [{chars[0]}]: ").strip()
    chosen = chosen if chosen in chars else chars[0]
    mgr.load(chosen)
    set_character(mgr.active, mgr.character)
    print(f"✅ Karakter: {mgr.active}")

    # ── L2D ───────────────────────────────────────────────────────────────
    l2d = Live2DClient()
    l2d.add_listener(
        lambda t, _: print("✅ LipSync selesai") if t == "lipSyncFinished" else None
    )

    # ── TTS check ─────────────────────────────────────────────────────────
    print("\n🔍 Cek server TTS...")
    try:
        requests.get("http://localhost:7861/docs", timeout=2)
        print("✅ Server TTS OK")
    except Exception:
        print("⚠️  Server TTS tidak merespons")

    # ── User setup ────────────────────────────────────────────────────────
    user_name = input(f"\n👤 Nama user [Shinri]: ").strip() or "Shinri"
    user_id   = input(f"🆔 User ID   [local]:  ").strip() or "local"

    client = DesktopClient(
        user_id        = user_id,
        user_name      = user_name,
        character_name = mgr.active,
        l2d            = l2d,
    )

    print(f"\n💾 Memory: {client.user_mem.filepath}")
    print("\n💬 Commands:")
    print("  #chl  → clear chat history")
    print("  #op   → opening live")
    print("  #end  → closing live")
    print("  /tts off|on  /l2d off|on")
    print("  /status      /history")
    print("  /expression <name>   /model <name>")
    print("  exit")
    print("-" * 60)

    tts_enabled = True
    l2d_enabled = True

    while True:
        try:
            raw = input(f"\n[{user_name}]: ").strip()
            if not raw:
                continue

            if raw.lower() == "exit":
                print("\n👋 Sampai jumpa~")
                break

            if raw.lower() == "#chl":
                ch = _main._chat_hist
                if ch:
                    try:
                        for attr in ("_messages", "messages", "_history"):
                            if hasattr(ch, attr):
                                setattr(ch, attr, [])
                                break
                        ch.save()
                        _main._last_assistant_response = ""
                        print("✅ Chat history dihapus.")
                    except Exception as e:
                        print(f"❌ Error clear history: {e}")
                continue

            if raw.lower() == "#head":
                l2d.random_tap_all()
                continue

            elif raw.lower() == "#op":
                print("💭 Generating opening...")
                responses, dominant = client.opening()
                print(f"\n[{mgr.active}] OPENING:")
                for i, r in enumerate(responses, 1):
                    print(f"  seg{i} ({r['anim']}): {r['ind']}")
                    print(f"         JP: {r['jp']}")
                if tts_enabled and l2d_enabled:
                    play_segments(responses, dominant, l2d)
                continue

            elif raw.lower() == "#end":
                print("💭 Generating closing...")
                responses, dominant = client.closing()
                print(f"\n[{mgr.active}] CLOSING:")
                for i, r in enumerate(responses, 1):
                    print(f"  seg{i} ({r['anim']}): {r['ind']}")
                    print(f"         JP: {r['jp']}")
                if tts_enabled and l2d_enabled:
                    done_evt = threading.Event()
                    play_segments(responses, dominant, l2d,
                                  on_complete=done_evt.set)
                    done_evt.wait(timeout=60)
                break

            elif raw.lower() == "/tts off":
                tts_enabled = False; print("🔇 TTS off"); continue
            elif raw.lower() == "/tts on":
                tts_enabled = True;  print("🔊 TTS on");  continue
            elif raw.lower() == "/l2d off":
                l2d_enabled = False; print("🎭 L2D off"); continue
            elif raw.lower() == "/l2d on":
                l2d_enabled = True;  print("🎭 L2D on");  continue

            elif raw.lower() == "/status":
                mm = _main._model_mem
                um = client.user_mem.data
                print(f"🎭 Topic    : {mm.topik if mm else '-'}")
                print(f"💕 Romance  : {client.user_mem.romance_points}pts "
                      f"({client.user_mem.get_romance_level()})")
                print(f"🕐 Last chat: {client.user_mem.get_last_chat_ago()}")
                info = um.get("info_user", [])
                if info:
                    print(f"📝 Info     : {'; '.join(info[-3:])}")
                if mm and mm.command:
                    print(f"⚡ Command  : {mm.command}")
                continue

            elif raw.lower() == "/history":
                ch = _main._chat_hist
                if ch:
                    msgs = ch.get_messages()
                    print(f"📜 History ({len(msgs)} entries):")
                    for m in msgs:
                        print(f"  [{m['role']}] {m['content'][:70]}")
                continue

            elif raw.startswith("/"):
                parts = raw[1:].split()
                if parts[0] == "expression" and len(parts) > 1:
                    l2d.set_expression(parts[1])
                elif parts[0] == "model" and len(parts) > 1:
                    l2d.change_model(name=parts[1])
                continue

            # ── Normal chat ───────────────────────────────────────────────
            if l2d_enabled:
                l2d.show_chat_log(f"{user_name}: {raw}")

            print("💭 Berpikir...", end="\r")
            responses, dominant = client.chat(raw)

            print(f"\n[{mgr.active}]")
            for i, r in enumerate(responses, 1):
                print(f"  seg{i} ({r['anim']}): {r['ind']}")
                print(f"         JP: {r['jp']}")
            print(f"  dominant: {dominant}")

            if _main._model_mem:
                print(f"  topic   : {_main._model_mem.topik}")
            print(f"  romance : {client.user_mem.romance_points}pts "
                  f"({client.user_mem.get_romance_level()}) "
                  f"status={client.user_mem.get_romance_status() or '-'}")

            if tts_enabled and l2d_enabled:
                play_segments(responses, dominant, l2d)

        except KeyboardInterrupt:
            print("\n👋 Sampai jumpa~")
            break
        except Exception as e:
            print(f"\n❌ {e}")
            import traceback
            traceback.print_exc()

    l2d.shutdown()


if __name__ == "__main__":
    main()
