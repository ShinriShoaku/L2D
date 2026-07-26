#!/usr/bin/env python3
# live2d_client.py - HTTP dan WebSocket client untuk Live2D server

import json
import time
import threading
import requests
import asyncio
import websockets
from typing import Dict, Callable, Optional, Any
from urllib.parse import urlencode, quote
import random

# ── Konfigurasi Music API ─────────────────────────────────────────────────────
MUSIC_API_URL = "http://localhost:8000"   # URL FastAPI YouTube Player (main.py)

# 🚀 KALIBRASI LYRIC TIMING (SINKRONISASI)
# Jika lirik masih KEDULUAN daripada lagunya, NAIKKAN angka ini (misal ke 1500 atau 2000).
# Jika lirik malah TELAT daripada lagunya, UBAH menjadi minus (misal ke -500).
LYRIC_OFFSET_MS = 1000  


# ═════════════════════════════════════════════════════════════════════════════
# MUSIC API CLIENT
# ═════════════════════════════════════════════════════════════════════════════

class MusicAPIClient:
    """
    Client ringan untuk FastAPI YouTube Audio Player (main.py).
    Semua metode mengembalikan string yang siap ditampilkan via show_system_log.
    """

    def __init__(self, base_url: str = MUSIC_API_URL):
        self.base_url = base_url.rstrip("/")
        self._lyric_enabled  = False
        self._lyric_thread: Optional[threading.Thread] = None
        self._lyric_stop    = threading.Event()
        self._current_lyric_lang = "id"

    # ── Internal HTTP helpers ─────────────────────────────────────────────

    def _get(self, path: str) -> Optional[dict]:
        try:
            r = requests.get(f"{self.base_url}{path}", timeout=8)
            r.raise_for_status()
            return r.json()
        except Exception as e:
            print(f"[MUSIC] GET {path} error: {e}")
            return None

    def _post(self, path: str, params: dict = None, json_body: dict = None) -> Optional[dict]:
        try:
            r = requests.post(
                f"{self.base_url}{path}",
                params=params,
                json=json_body,
                timeout=15,   # search/add bisa lambat karena yt-dlp
            )
            r.raise_for_status()
            return r.json()
        except Exception as e:
            print(f"[MUSIC] POST {path} error: {e}")
            return None

    def _delete(self, path: str) -> Optional[dict]:
        try:
            r = requests.delete(f"{self.base_url}{path}", timeout=8)
            r.raise_for_status()
            return r.json()
        except Exception as e:
            print(f"[MUSIC] DELETE {path} error: {e}")
            return None

    def list_languages(self) -> str:
        """Mengambil daftar semua bahasa subtitle yang tersedia untuk lagu saat ini."""
        data = self._get("/subtitles/list")
        if data is None:
            return "❌ Gagal mengambil daftar lirik atau lagu belum diputar."
            
        langs = data.get("available_languages", [])
        if not langs:
            return "🎤 Tidak ada lirik (subtitle) yang tersedia untuk lagu ini."
            
        return f"📋 Lirik Tersedia ({len(langs)}): " + ", ".join(langs)

    def set_language(self, lang_code: str) -> str:
        """Mengatur bahasa lirik aktif (misal: en, id, ja, ko)."""
        self._current_lyric_lang = lang_code.strip().lower()
        
        # Jika lirik sedang aktif, kita paksa reset target lagu agar thread membaca ulang bahasa baru
        if self._lyric_enabled:
            # Menghentikan sejenak dan menyalakan ulang thread agar lirik langsung berganti bahasa
            return f"🔄 Lirik diubah ke bahasa: [{self._current_lyric_lang}]. Memuat ulang lirik..."
        
        return f"🎯 Bahasa lirik diatur ke: [{self._current_lyric_lang}]. Aktifkan lirik dengan #el"

    # ── Public methods ────────────────────────────────────────────────────

    def request(self, query: str) -> str:
        """Cari & tambah lagu teratas ke antrian. Bisa auto-play jika player idle."""
        print(f"[MUSIC] Mencari: {query!r} ...")
        data = self._post("/search/add-top", params={"q": query})
        if data is None:
            return f"❌ Gagal mencari musik: {query!r}"

        title = data.get("song", {}).get("title", query)
        if data.get("auto_played"):
            return f"▶️ NOW: {title}"
        pos   = data.get("queue_position", "?")
        total = data.get("queue_count",    "?")
        return f"🎵 [{pos+1}/{total}] {title}"

    def remove(self, query: str) -> str:
        """Hapus lagu dari antrian berdasarkan judul (partial match)."""
        data = self._get("/queue")
        if data is None:
            return "❌ Gagal mengambil antrian"

        lower = query.lower()
        match_pos  = None
        match_title = None
        for item in data.get("queue", []):
            title = item.get("song", {}).get("title", "")
            if lower in title.lower():
                match_pos   = item["position"]
                match_title = title
                break

        if match_pos is None:
            return f"❌ '{query}' tidak ada di antrian"

        res = self._delete(f"/queue/{match_pos}")
        if res is None:
            return f"❌ Gagal menghapus '{match_title}'"
        return f"🗑️ Dihapus: {match_title}"

    def list_queue(self) -> str:
        """Tampilkan antrian musik: now playing + next queue (max 4)."""
        data = self._get("/queue")
        if data is None:
            return "❌ Gagal mengambil antrian"

        current = data.get("current_song")
        queue = data.get("queue", [])
        total = len(queue)
        shuffle_on = data.get("shuffle_mode", False)

        if not queue and not current:
            return "📭 Antrian kosong"

        parts = []

        # ── Now Playing (1 baris) ──
        if current:
            dur = current.get("duration") or 0
            m, s = divmod(dur, 60)
            title = current.get("title", "?")
            # Potong judul panjang supaya 1 baris tidak overflow
            if len(title) > 28:
                title = title[:25] + ".."
            parts.append(f"▶️ {title} [{m}:{s:02d}]")

        # ── Next Queue (max 4, 1 baris per lagu) ──
        for item in queue[:4]:
            song = item.get("song", {})
            pos = item.get("position", 0) + 1
            dur = song.get("duration") or 0
            m, s = divmod(dur, 60)
            title = song.get("title", "?")
            if len(title) > 28:
                title = title[:25] + ".."
            parts.append(f"{pos}. {title} [{m}:{s:02d}]")

        if total > 4:
            parts.append(f"...+{total - 4} lagu")

        # ── Footer ──
        footer = f"({total} lagu)"
        if shuffle_on:
            footer = "🔀 " + footer
        parts.append(footer)

        # Gunakan \n yang benar — Live2D overlay render per baris
        return "\n".join(parts)

    def skip(self) -> str:
        """Skip lagu sekarang, mainkan berikutnya dari antrian."""
        data = self._post("/queue/next")
        if data is None:
            return "❌ Gagal skip"
        if data.get("current_song"):
            return f"⏭️ Skip → {data['current_song']['title']}"
        return "⏭️ Skip — antrian habis"

    def stop(self) -> str:
        """Hentikan pemutaran. Antrian tetap ada."""
        data = self._post("/player/stop")
        if data is None:
            return "❌ Gagal stop"
        return "⏹️ Musik dihentikan"

    def play(self) -> str:
        """Mainkan lagu berikutnya dari antrian (jika player sedang idle)."""
        state = self._get("/player/state")
        if state is None:
            return "❌ Gagal cek state player"

        if state.get("is_playing") and state.get("current_song"):
            title = state["current_song"]["title"]
            return f"▶️ Sedang diputar: {title}"

        # Jika ada lagu di antrian, skip ke berikutnya
        if state.get("queue_count", 0) > 0 or state.get("current_song"):
            data = self._post("/queue/next")
            if data is None:
                return "❌ Gagal play"
            if data.get("current_song"):
                return f"▶️ Playing: {data['current_song']['title']}"
        return "📭 Antrian kosong — tambah lagu dengan #req <judul>"

    def clear(self) -> str:
        """Hapus semua antrian dan hentikan pemutaran."""
        state = self._get("/queue")
        total = len(state.get("queue", [])) if state else 0

        self._post("/player/stop")
        data = self._post("/queue/clear")
        if data is None:
            return "❌ Gagal clear antrian"
        self.stop_lyric_thread()
        return f"🧹 Antrian dibersihkan ({total} lagu dihapus)"

    # ── Lyric display ─────────────────────────────────────────────────────

    def toggle_lyric(self, l2d_client) -> str:
        """Toggle tampilan lirik. Fetch sekali lalu tampilkan baris per baris."""
        self._lyric_enabled = not self._lyric_enabled

        if not self._lyric_enabled:
            self.stop_lyric_thread()
            return "🎤 Lirik OFF"

        # Mulai thread lyric baru
        self.stop_lyric_thread()
        self._lyric_stop.clear()
        self._lyric_thread = threading.Thread(
            target=self._lyric_loop,
            args=(l2d_client,),
            daemon=True,
            name="lyric-display",
        )
        self._lyric_thread.start()
        return "🎤 Lirik ON — mengambil subtitle..."

    def stop_lyric_thread(self):
        """Hentikan thread lyric yang berjalan."""
        self._lyric_stop.set()
        self._lyric_enabled = False
        if self._lyric_thread and self._lyric_thread.is_alive():
            self._lyric_thread.join(timeout=2)
        self._lyric_thread = None

    def _lyric_loop(self, l2d_client):
        """
        Background thread lirik pintar dengan fitur Auto-Fallback ke bahasa lain
        jika bahasa pilihan utama mengembalikan error 444.
        """
        last_song_id = None
        last_lang    = None
        events       = []
        event_idx    = 0

        while not self._lyric_stop.is_set():
            state = self._get("/player/state")
            if not state or not state.get("current_song"):
                self._lyric_stop.wait(timeout=2)
                continue

            song_id = state["current_song"].get("id")
            current_lang = self._current_lyric_lang

            # Jika lagu BERGANTI atau user mengubah BAHASA target
            if song_id != last_song_id or current_lang != last_lang:
                last_song_id = song_id
                last_lang    = current_lang
                event_idx    = 0
                
                l2d_client.show_system_log(f"🎤 [{current_lang.upper()}] {state['current_song'].get('title', '?')}")
                
                # 1. Coba ambil bahasa utama pilihan user (misal: id)
                sub_data = self._get(f"/subtitles/current?lang={current_lang}")
                
                # 🚀 TRACKING FALLBACK JIKA RETURN 444 (sub_data is None)
                if sub_data is None:
                    # Jika gagalnya saat mencari 'id', coba fallback otomatis ke 'en' (Inggris)
                    if current_lang == "id":
                        l2d_client.show_system_log("🎤 'id' tak ada, mencoba lirik 'en'...")
                        sub_data = self._get("/subtitles/current?lang=en")
                    
                    # Jika masih None atau dari awal memang bukan 'id', cari bahasa APAPUN yang tersedia
                    if sub_data is None:
                        l2d_client.show_system_log("🎤 Mencari lirik alternatif yang tersedia...")
                        list_data = self._get("/subtitles/list")
                        if list_data and list_data.get("available_languages"):
                            fallback_lang = list_data["available_languages"][0]
                            l2d_client.show_system_log(f"🎤 Fallback ke lirik: [{fallback_lang.upper()}]")
                            sub_data = self._get(f"/subtitles/current?lang={fallback_lang}")

                events = []
                if sub_data:
                    events = sub_data.get("subtitles", {}).get("events", [])
                    
                if not events:
                    l2d_client.show_system_log(f"🎤 Video ini tidak menyediakan lirik sama sekali.")
                    self._lyric_stop.wait(timeout=5)
                    continue

            if state.get("is_paused"):
                self._lyric_stop.wait(timeout=0.5)
                continue

            # Sinkronisasi Waktu Menggunakan elapsed_ms dari Server
            elapsed_ms = state.get("elapsed_ms", 0) - LYRIC_OFFSET_MS
            
            while event_idx < len(events):
                ev = events[event_idx]
                if elapsed_ms >= ev.get("start_ms", 0):
                    # Box Cokelat Gelap Pekat (r=65, g=43, b=21)
                    l2d_client.send_tap_status(
                        f"🎤 {ev['text']}",
                        r=65,
                        g=43,
                        b=21,
                        a=1.0,
                        duration=4000,
                    )
                    event_idx += 1
                else:
                    break

            if event_idx >= len(events):
                self._lyric_stop.wait(timeout=2)
                continue

            self._lyric_stop.wait(timeout=0.2)



class Live2DClient:
    def __init__(
        self,
        base_url:      str = "http://localhost:9000",
        ws_url:        str = "ws://localhost:9001",
        music_api_url: str = MUSIC_API_URL,
    ):
        self.base_url = base_url
        self.drag_endpoint = f"{base_url}/startRealisticDragSimulation"
        self.ws_url = ws_url
        self.ws = None
        self.ws_connected = threading.Event()
        self.should_reconnect = True
        self.ws_thread = None
        self.listeners = []
        self._loop = None          # ← event loop aktif disimpan di sini
        self._start_ws()
        # ── Music API ────────────────────────────────────────────────────────
        self._music = MusicAPIClient(base_url=music_api_url)
        self.area_files = {
            "head": "tap_head.json",
            "head2": "tap_head_2.json",
            "bust": "tap_bust.json",
            "body": "tap_body.json"
        }

    def random_tap_all(self):            
        available_areas = list(self.area_files.keys())            
        random_area = random.choice(available_areas)        
        self.execute_drag_simulation(random_area)

    def _send_post_config(self, payload: Dict[str, Any]):
        """Versi yang menangani respons teks biasa maupun JSON"""
        try:
            response = requests.post(self.drag_endpoint, json=payload, timeout=5)
            try:
                return response.json()
            except json.JSONDecodeError:
                return {"status": "success", "message": response.text}
                
        except Exception as e:
            print(f"Error mengirim config ke Node.js: {e}")
            return None

    def execute_drag_simulation(self, area: str):
        """Versi dinamis dari TapHead/TapBody/TapBust"""
        file_path = self.area_files.get(area)
        if not file_path:
            print(f"Area '{area}' tidak ditemukan.")
            return

        try:
            # Membaca JSON file
            with open(file_path, 'r') as f:
                data = json.load(f)
            
            pos_data = data.get("position", {})
            
            # Membangun konfigurasi drag (sesuai struktur Java kamu)
            config = {
                "leftBoundary": pos_data.get("lift"),
                "rightBoundary": pos_data.get("right"),
                "center": pos_data.get("center"),
                "delayMs": 1,
                "minDragPixels": 30,
                "maxDragPixels": 100,
                "minYOffset": -10,
                "maxYOffset": 10,
                "durationSeconds": 1,
                "initialMovingRight": True 
            }
            
            self._send_post_config(config)
            
        except Exception as e:
            print(f"Error dalam simulasi: {e}")

    def _start_ws(self):
        self.ws_thread = threading.Thread(target=self._ws_loop, daemon=True)
        self.ws_thread.start()

    def _ws_loop(self):
        while self.should_reconnect:
            try:
                loop = asyncio.new_event_loop()
                self._loop = loop          # ← simpan loop agar _send_ws bisa pakai
                loop.run_until_complete(self._connect_ws())
            except Exception as e:
                print(f"[L2D WS] Error: {e}")
            finally:
                self._loop = None          # ← hapus referensi saat loop mati
            time.sleep(5)

    async def _connect_ws(self):
        try:
            async with websockets.connect(self.ws_url) as ws:
                self.ws = ws
                await ws.send("JavaClientReady")
                self.ws_connected.set()
                print("[L2D] WebSocket connected")
                async for msg in ws:
                    self._handle_message(msg)
        except Exception as e:
            print(f"[L2D WS] Connection failed: {e}")
            self.ws_connected.clear()
        finally:
            self.ws = None
            self.ws_connected.clear()

    def _handle_message(self, msg: str):
        try:
            data = json.loads(msg)
            if data.get("type") == "saweriaFinished":
                event = data.get("event")
                for cb in self.listeners:
                    cb("saweriaFinished", event)
        except:
            if msg == "lipSyncFinished":
                for cb in self.listeners:
                    cb("lipSyncFinished", None)

    def add_listener(self, callback: Callable):
        self.listeners.append(callback)

    def remove_listener(self, callback: Callable):
        try:
            self.listeners.remove(callback)
        except ValueError:
            pass

    def _send_ws(self, message: str):
        # Kirim dari thread non-async ke async loop yang sedang jalan
        if self.ws and self.ws_connected.is_set() and self._loop and self._loop.is_running():
            try:
                asyncio.run_coroutine_threadsafe(self.ws.send(message), self._loop)
            except Exception as e:
                print(f"[L2D] Error sending WS: {e}")

    def _get(self, endpoint: str) -> str:
        try:
            resp = requests.get(f"{self.base_url}{endpoint}", timeout=5)
            return resp.text
        except Exception as e:
            return f"Error: {e}"

    def _post(self, endpoint: str, data: Dict) -> str:
        try:
            resp = requests.post(f"{self.base_url}{endpoint}", json=data, timeout=5)
            return resp.text
        except Exception as e:
            return f"Error: {e}"

    def start_lipsync(self, model_id: int, model_map: int, text: str, total_duration: int) -> str:
        body = {
            "modelId": model_id,
            "modelMap": model_map,
            "text": text,
            "totalDuration": total_duration
        }
        return self._post("/startLive2dLipsync", body)

    def change_model(self, model: int = None, name: str = None):
        if model is not None:
            self._get(f"/changeModel/{model}")
        elif name is not None:
            self._get(f"/changeModelName/{name}")

    def set_expression(self, name: str, model_id: int = None):
        if model_id is None:
            self._get(f"/setExpression/{name}")
        else:
            self._post("/sendExpression", {"modelId": model_id, "name": name})

    def show_chat_bubble(self, message: str, duration_ms: int = 3000):
        encoded = quote(message)
        self._get(f"/showChatBubble/{encoded}?duration={duration_ms}")

    def show_chat_log(self, message: str):
        encoded = quote(message)
        self._get(f"/showChatLog/{encoded}")

    def show_system_log(self, message: str):
        encoded = quote(message)
        self._get(f"/showSystemLog/{encoded}")

    def show_notification(self, image_url: str = None, text: str = None):
        params = {}
        if image_url:
            params["img"] = image_url
        if text:
            params["text"] = text
        if params:
            self._get(f"/showNotification?{urlencode(params)}")

    def send_tap_status(self, message: str, r: int, g: int, b: int, a: float, duration: int):
        data = {
            "type": "tapStatus",
            "message": message,
            "color": {"r": r, "g": g, "b": b, "a": a},
            "duration": duration
        }
        self._send_ws(json.dumps(data))

    def send_random_motions(self, model_id: int = 0, name: str = "response") -> str:
        """Trigger random motion group di Live2D model setelah chat bubble tampil."""
        return self._post("/setRandomMotions", {"modelId": model_id, "name": name})

    # ── Music API methods ─────────────────────────────────────────────────────

    def music_request(self, query: str) -> str:
        """#req <judul> — Cari & tambah lagu ke antrian via YouTube search."""
        return self._music.request(query)

    def music_remove(self, query: str) -> str:
        """#rm <judul> — Hapus lagu dari antrian berdasarkan nama."""
        return self._music.remove(query)

    def music_list(self) -> str:
        """#lm — Tampilkan semua antrian musik."""
        return self._music.list_queue()

    def music_skip(self) -> str:
        """#skip — Skip lagu sekarang, mainkan berikutnya."""
        return self._music.skip()

    def music_stop(self) -> str:
        """#stop — Hentikan pemutaran musik."""
        return self._music.stop()

    def music_play(self) -> str:
        """#play — Lanjutkan pemutaran dari antrian."""
        return self._music.play()

    def music_toggle_lyric(self) -> str:
        """#el — Toggle tampilan lirik lagu yang sedang diputar."""
        return self._music.toggle_lyric(self)

    def music_clear(self) -> str:
        """#cm — Hapus semua antrian musik dan hentikan pemutaran."""
        return self._music.clear()

    # Tambahkan dua fungsi pembantu ini di dalam class Live2DClient (di bawah music_clear atau sekitarnya):
    
    def music_list_languages(self) -> str:
        """Mengambil daftar kode bahasa subtitle yang tersedia."""
        return self._music.list_languages()

    def music_set_language(self, lang_code: str) -> str:
        """Mengubah target bahasa lirik."""
        return self._music.set_language(lang_code)

    def shutdown(self):
        self.should_reconnect = False
        self._music.stop_lyric_thread()
        if self.ws:
            try:
                asyncio.new_event_loop().run_until_complete(self.ws.close())
            except:
                pass