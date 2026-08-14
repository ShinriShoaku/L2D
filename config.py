from openai import OpenAI

# ══════════════════════════════════════════════════════════════════════════════
# ENDPOINT DEFINITIONS
# Daftarkan semua endpoint yang tersedia di sini.
# Kamu bisa tambah lebih banyak sesuai kebutuhan.
# ══════════════════════════════════════════════════════════════════════════════

ENDPOINTS = {
    "local_lm_studio": {
        "url":     "http://192.168.8.3:1234/v1",
        "api_key": "lm-studio",
        "model":   "qwen/qwen3-4b",
        "disable_thinking": True,
    },
    "local_soul": {
        "url":     "https://api.cometapi.com/v1",
        "api_key": "",
        "model":   "gpt-4o-mini",
        "disable_thinking": False,
    },
}


# ══════════════════════════════════════════════════════════════════════════════
# PER-CALL ROUTING
# Setiap key adalah nama "pass" / step di pipeline.
# Value adalah nama endpoint dari ENDPOINTS di atas.
#
# Pass yang tersedia:
#   react          — Pass R: ReAct agent loop (tool routing utama)
#   category       — Pass 1: klasifikasi kategori besar
#   subintent_data — Pass 2a: sub-intent data_user (read/write)
#   subintent_stream — Pass 2b: sub-intent stream_state (topic)
#   tool_select    — Pass 3: pilih tool + argumen
#   data_summary   — Pass 4: ringkas data mentah dari tool
#   soul_think     — Pass 5: grounding intent/tone/key_point
#   soul_persona   — Pass 6: persona lock (style/stammer/forbidden)
#   cmd_interpret  — Pass A: terjemahkan gaya bicara mentah → directive
#   identity_merge — Pass B: gabung directive + context → address/persona
#   soul           — Pass C/3: generate dialog (memanggil soul client)
#   soul_validate  — Pass 8: validasi output soul via LLM (hanya jika suspicious)
#   anim           — Pass 5 (lama): tentukan animasi
#   trans          — Pass 6: terjemahkan ID → JP per kalimat
#   opening        — generate_from_prompt: soul untuk opening/closing/idle
# ══════════════════════════════════════════════════════════════════════════════

CALL_ROUTING = {
    "react": "local_soul",
    "category": "local_soul",
    "subintent_data": "local_soul",
    "subintent_stream": "local_soul",
    "tool_select": "local_lm_studio",
    "data_summary": "local_lm_studio",
    "soul_think": "local_lm_studio",
    "soul_persona": "local_lm_studio",
    "cmd_interpret": "local_lm_studio",
    "identity_merge": "local_lm_studio",
    "soul": "local_soul",
    "soul_validate": "local_soul",
    "opening": "local_lm_studio",
    "anim": "local_soul",
    "trans": "local_soul",
}


# ══════════════════════════════════════════════════════════════════════════════
# STORAGE
# ══════════════════════════════════════════════════════════════════════════════

STORAGE_DIR      = "."
MEMORY_DIR       = "memory"
MODEL_MEMORY_DIR = "."

# ══════════════════════════════════════════════════════════════════════════════
# TUNING
# ══════════════════════════════════════════════════════════════════════════════

DEBUG        = True
MAX_HISTORY  = 7
MAX_INFO     = 20
MAX_NOTES    = 30
MAX_GIFTS    = 50
MAX_SEGMENTS = 5


# ══════════════════════════════════════════════════════════════════════════════
# INTERNAL HELPERS — jangan ubah kecuali paham strukturnya
# ══════════════════════════════════════════════════════════════════════════════

def _get_endpoint(pass_name: str) -> dict:
    """
    Kembalikan dict endpoint untuk pass_name tertentu.
    Raise ValueError jika routing atau endpoint tidak ditemukan.
    """
    endpoint_key = CALL_ROUTING.get(pass_name)
    if endpoint_key is None:
        raise ValueError(
            f"[CONFIG] Pass '{pass_name}' tidak ada di CALL_ROUTING. "
            f"Tambahkan entry-nya."
        )
    endpoint = ENDPOINTS.get(endpoint_key)
    if endpoint is None:
        raise ValueError(
            f"[CONFIG] Endpoint '{endpoint_key}' (untuk pass '{pass_name}') "
            f"tidak ditemukan di ENDPOINTS."
        )
    return endpoint


def get_client(pass_name: str) -> "OpenAI":
    """
    Kembalikan OpenAI client yang sudah dikonfigurasi untuk pass_name.
    Client di-cache per endpoint key supaya tidak buat koneksi baru tiap call.
    """
    ep = _get_endpoint(pass_name)
    key = CALL_ROUTING[pass_name]
    if key not in _CLIENT_CACHE:
        _CLIENT_CACHE[key] = OpenAI(
            base_url   = ep["url"],
            api_key    = ep["api_key"],
            timeout    = 60,
            max_retries= 0,
        )
    return _CLIENT_CACHE[key]


def get_model(pass_name: str) -> str:
    """Kembalikan nama model untuk pass_name."""
    return _get_endpoint(pass_name)["model"]


def get_extra_body(pass_name: str) -> dict:
    """
    Kembalikan `extra_body` untuk dikirim ke chat.completions.create(),
    berdasarkan flag "disable_thinking" pada endpoint yang dipakai pass_name.
    Kalau endpoint punya "disable_thinking": True, kirim {"enable_thinking": False}
    supaya model (mis. qwen3.5 via CometAPI) tidak mengeluarkan reasoning/thinking.
    Endpoint lain (disable_thinking False/absen) → dict kosong, tidak ada efek.
    """
    ep = _get_endpoint(pass_name)
    if ep.get("disable_thinking"):
        return {"enable_thinking": False}
    return {}


# Cache internal — jangan akses langsung
_CLIENT_CACHE: dict = {}


# ══════════════════════════════════════════════════════════════════════════════
# LEGACY ALIASES — dipakai kode lama yang masih import langsung
# Hapus kalau semua kode sudah pakai get_client() / get_model()
# ══════════════════════════════════════════════════════════════════════════════

# URL & model name dari routing "react" sebagai default offline
LM_STUDIO_URL = ENDPOINTS["local_lm_studio"]["url"]
MODEL_NAME    = ENDPOINTS["local_lm_studio"]["model"]

# URL & model name dari routing "soul" sebagai default online
SOUL_API_URL   = ENDPOINTS["local_soul"]["url"]
SOUL_MODEL_NAME = ENDPOINTS["local_soul"]["model"]

# Legacy client — pakai get_client() untuk kontrol per-pass
client       = OpenAI(base_url=LM_STUDIO_URL, api_key="lm-studio",  timeout=60, max_retries=0)



