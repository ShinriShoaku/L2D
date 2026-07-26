# Live Desktop / Live Server — Refactored Architecture

## File Overview

| File | Role |
|------|------|
| `config.py` | URL, model, storage paths, tuning constants |
| `memory.py` | `UserMemory` + `UserMemoryManager` — per-user JSON |
| `model_memory.py` | `ModelMemory` — per-character JSON (topic, style, romance_points) |
| `chat_history.py` | `ChatHistory` — unified cross-user history (max 7) |
| `live_tracking.py` | `LiveTracker` — per-session chat/like/share/gift/follow |
| `main.py` | Core generate engine (single LLM call, MCP-style memory ops) |
| `banter_manager.py` | Idle banter + scenario, history-context-aware generation |
| `liveDesktop.py` | Standalone desktop CLI + TTS + Live2D |
| `liveServer.py` | TikTok Live WebSocket server |
| `character_manager.py` | Load character folder (unchanged) |
| `character.json` | Liana character config (updated prompts/schema) |

---

## Memory Files Created at Runtime

```
memory/
  user_{safe_id}.json          ← per-user memory
  
liana.memory.json              ← model state (topic, style, romance_points)
liana.history.json             ← unified chat history (max 7)
live_track.json                ← live session tracking
live_chat.json                 ← chat event log
```

### User Memory Format (`memory/user_XXX.json`)
```json
{
  "user_id":             "12345",
  "username":            "Shinri",
  "info_user":           ["suka kopi", "kerja di IT"],
  "romance_status":      "teman",
  "note":                [{"text": "...", "ts": "..."}],
  "gift_history":        [{"gift": "Rose", "count": 3, "ts": "..."}],
  "vip_user":            false,
  "last_chat_timestamp": "Sabtu, 12 Juli 2025, 09:18 AM",
  "_last_chat_iso":      "2025-07-12T02:18:00+00:00"
}
```

### Model Memory Format (`liana.memory.json`)
```json
{
  "topik":          "morning_greeting",
  "role":           "default",
  "command":        "nyann~",
  "style":          "teasing",
  "romance_points": 75
}
```

---

## CTX Format Sent to LLM

```
#CTX
user=Shinri
last_chat=12 mnt yang lalu
topic=morning_greeting
style=teasing
cmd=nyann~
romance=kenal
romance_pts=75
info=suka kopi; kerja IT
#INPUT pagi liana
```

---

## LLM Output Schema

```json
{
  "responses": [
    {"ind": "...", "jp": "...", "anim": "smile"}
  ],
  "points":  3,
  "topic":   "morning_greeting",
  "info":    "user suka kopi",
  "note":    "",
  "command": ""
}
```

- `responses` — min 2 segments, each with Indonesian text, JP (clean), and anim
- `points` — -10..10 romance delta applied to `model_memory.romance_points`
- `topic` — updates `model_memory.topik` if non-empty
- `info` — appended to `user_memory.info_user` if non-empty
- `note` — appended to `user_memory.note` if non-empty
- `command` — updates `model_memory.command` if non-empty

---

## Special Commands

| Command | Where | Effect |
|---------|-------|--------|
| `#op` | Chat input (server or desktop) | Generate opening live speech |
| `#end` | Chat input (server or desktop) | Generate closing + special thanks from tracker |

---

## Romance Levels (`model_memory.get_romance_level()`)

| Points | Level |
|--------|-------|
| 0–49 | `baru_kenal` |
| 50–149 | `kenal` |
| 150–299 | `akrab` |
| 300–499 | `dekat` |
| 500+ | `sangat_dekat` |

---

## Key Design Decisions

1. **No brain-center** — memory ops are returned inline in the LLM output JSON
   (`topic`, `info`, `note`, `command`, `points`). No second LLM call.

2. **Single generate** — `full_generate()` in `main.py` is one LLM call.

3. **Unified history** — `ChatHistory` stores last 7 messages regardless of which
   user sent them. All users share the same conversational context window.

4. **JP cleaned before TTS** — `clean_jp_for_tts()` strips all `()`, `（）`, `[]`,
   `【】`, and stray Latin characters so TTS never reads brackets aloud.

5. **Banter context-aware** — `BanterManager._replenish_banters()` injects
   `ChatHistory.get_last_topic_hint()` and `ModelMemory.topik` into the banter
   generation prompt so idle content flows naturally from recent conversation.

6. **LiveTracker** — tracks all interaction events per session and provides
   `build_thanks_prompt()` which creates a **new separate prompt** sent to the
   model for personalized thank-you speech at stream end.
