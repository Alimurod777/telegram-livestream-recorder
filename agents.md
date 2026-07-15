# agents.md — AI Agent Reference Guide
# Telegram Live Stream Recorder Bot

> Bu fayl loyihada ishlaydigan har qanday sun'iy intellekt agent uchun mo'ljallangan.
> Kodni o'zgartirishdan OLDIN bu faylni to'liq o'qing.

---

## 1. LOYIHA MAQSADI

Foydalanuvchi Telegram guruh yoki kanalida boshlangan **jonli efirni (voice/video chat)**
avtomatik aniqlaydi va yozib oladi. Tayyor fayl (`mp4` yoki `m4a`) bot orqali
foydalanuvchining shaxsiy chatiga (DM) yuboriladi.

---

## 2. FAYL TUZILISHI VA VAZIFALAR

```
record/
├── bot.py              ← ASOSIY kirish nuqtasi. Bot API polling + barcha komandalar
├── stream_recorder.py  ← Yozib olish mantiq. PyTgCalls + FFmpeg pipe
├── database.py         ← SQLite wrapper (3 jadval)
├── config.py           ← Barcha sozlamalar va konstantalar
├── ffmpeg_helper.py    ← FFmpeg yo'lini topish + merge/extract yordamchi funksiyalar
├── requirements.txt    ← Python paketlar ro'yxati
├── recorder.db         ← SQLite ma'lumotlar bazasi (git da yo'q)
├── recordings/         ← Yozib olingan fayllar (git da yo'q)
│   └── {user_id}/      ← Har foydalanuvchi uchun alohida papka
├── venv/               ← Virtual muhit (git da yo'q)
├── agents.md           ← SHU FAYL — AI agent ko'rsatmalari
├── memory.md           ← Loyiha holati, qarorlar, muammolar
└── claude.md           ← Claude uchun maxsus kontekst
```

---

## 3. ARXITEKTURA: IKKI PARALLEL TIZIM

Bot **ikkita mustaqil aloqa kanali** orqali ishlaydi:

### 3a. Bot API (urllib manual polling) — foydalanuvchi buyruqlari uchun
```
Foydalanuvchi → Telegram Bot API → bot.py (getUpdates loop)
                                  → handle_update() → komandalar
```
- `urllib` ishlatiladi (`requests`, `httpx` EMAS)
- `asyncio.to_thread()` orqali bloklovchi I/O asenkron qilinadi
- Webhook YO'Q — faqat long-polling

### 3b. Pyrogram Userbot — jonli efir va media uchun
```
Userbot session → Pyrogram Client (in_memory=True)
               → MessageHandler (filters.service) → auto recording trigger
               → PyTgCalls → ntgcalls → group call join
                                       → StreamFrames (raw PCM + I420)
                                       → FFmpeg subprocess (stdin pipe)
```

---

## 4. YOZIB OLISH JARAYONI (stream_recorder.py)

### 4.1 Boshlash: `start_recording()`
1. `RecordingSession` dataclass yaratiladi
2. `_start_audio_encoder()` — FFmpeg subprocess ochiladi (stdin=PIPE, raw PCM → AAC → .mka)
3. `PyTgCalls(client)` yaratiladi, `on_update()` handler bog'lanadi
4. `call_client.record(channel_id, stream=RecordStream(audio=True, camera=True, screen=True))`
5. `_monitor_call()` task ishga tushadi (10 soniyada bir tekshiradi)

### 4.2 Frame qabul qilish: `_handle_call_update()`
```python
StreamFrames(direction=INCOMING, device=MICROPHONE|SPEAKER)
  → _handle_audio_frames() → numpy mix → queue → FFmpeg stdin

StreamFrames(direction=INCOMING, device=CAMERA|SCREEN)
  → _handle_video_frames() → I420 bytes → FFmpeg stdin
```

**MUHIM**: Video frame o'lchami dinamik — `frame.info.width/height` dan o'qiladi,
`VIDEO_WIDTH`/`VIDEO_HEIGHT` konstantalari encoder boshlanguncha ishlatilmaydi.

### 4.3 FFmpeg pipeline:
```
Audio: raw PCM s16le → [FFmpeg stdin pipe] → AAC → Matroska (.mka)
Video: raw I420 yuv420p → [FFmpeg stdin pipe] → H.264 ultrafast → Matroska (.mkv)
```

### 4.4 To'xtatish va yetkazib berish: `stop_recording()`
1. `leave_call()` → group call dan chiqish
2. FFmpeg stdin yopiladi → FFmpeg finalize bo'ladi
3. Audio `.mka` + Video `.mkv` → mux → `.mp4`
4. Agar fayl >1.85 GB → `-f segment` bilan bo'laklarga bo'linadi
5. `_schedule_completion()` → `deliver_recording()` → Bot API `sendDocument`

---

## 5. MA'LUMOTLAR BAZASI SXEMASI

```sql
-- Foydalanuvchi maqsadli kanali
user_targets (user_id PK, channel_id, channel_title, channel_username, updated_at)

-- Yozib olish tarixi
recordings (id PK, user_id, channel_id, channel_title,
            start_time, end_time, is_video, file_path,
            file_size, status, error_message, created_at)

-- Foydalanuvchi Pyrogram sessiyalari
user_sessions (user_id PK, session_file, session_string,
               logged_in_at, last_active)
```

**Muhim**: `db` — global singleton, `from database import db` orqali ishlatiladi.

---

## 6. SOZLAMALAR (config.py)

| Konstanta | Standart qiymat | Izoh |
|-----------|-----------------|------|
| `API_ID` | 11411895 | Telegram app ID |
| `API_HASH` | `62b630c7...` | Telegram app hash |
| `BOT_TOKEN` | `7644055655:AAH...` | Bot token |
| `BOT_API_BASE_URL` | `https://api.telegram.org` | Local server uchun o'zgartirilishi mumkin |
| `BOT_API_UPLOAD_LIMIT` | 49 MB (public) / 1.9 GB (local) | Fayl o'lchami chegarasi |
| `RECORDING_CHUNK_BYTES` | 1.85 GB | Qismga bo'lish chegarasi |
| `OUTPUT_DIR` | `recordings/` | Fayllar papkasi |
| `VIDEO_WIDTH/HEIGHT` | 1280 × 720 | Default, aslida frame dan o'qiladi |
| `VIDEO_FPS` | 30 | Kadrlar tezligi |
| `VIDEO_BITRATE` | `2500k` | H.264 bitrate |
| `AUDIO_SAMPLE_RATE` | 48000 Hz | PCM sample rate |
| `AUDIO_CHANNELS` | 2 | Stereo |
| `AUDIO_BITRATE` | `128k` | AAC bitrate |

---

## 7. BOT KOMANDALAR VA HOLAT MASHINALARI

```
UserState: IDLE → WAITING_PHONE → WAITING_CODE → WAITING_PASSWORD → LOGGED_IN → RECORDING
                                                ↘ (2FA) ↗
           IDLE → WAITING_QR → (scan) → LOGGED_IN
```

| Komanda | Funksiya | Izoh |
|---------|----------|------|
| `/start` | `handle_start()` | Holat va xush kelibsiz |
| `/login_qr` | `handle_login_qr()` | QR code orqali login |
| `/login_phone` | `handle_login_phone()` | Telefon raqam orqali |
| `/login_session <str>` | `handle_login_session()` | Session string bilan |
| `/target <username\|id\|link>` | `handle_target()` | Maqsadli kanalni saqlash |
| `/record [channel_id]` | `handle_record()` | Yozishni boshlash |
| `/stop` | `handle_stop()` | To'xtatish va DM ga yuborish |
| `/status` | `handle_status()` | Hozirgi holat |
| `/logout` | `handle_logout()` | Sessiyani o'chirish |
| `/cancel` | inline | Login jarayonini bekor qilish |

---

## 8. MUHIM CHEKLOVLAR VA QOIDALAR

### 8.1 Nima QILMA:
- `bot.py` ga `import requests` qo'shma — faqat `urllib` ishlatiladi
- `await call_client.calls` ni o'zgartirishga urinma — bu **async property**, await to'g'ri
- `VIDEO_WIDTH × VIDEO_HEIGHT` ni frame size validation uchun ishlatma — bu xato
- Bot API da `sendVideo`/`sendAudio` ishlatma — faqat `sendDocument` ishlatiladi (katta fayllar uchun)
- `user_clients` lug'atini to'g'ridan-to'g'ri o'zgartirma — faqat `get_user_client()` orqali

### 8.2 Nima QILISH:
- Yangi komanda qo'shganda `handle_update()` dispatch ro'yxatiga ham qo'sh
- Session string DB da `user_sessions.session_string` da saqlanadi
- Foydalanuvchi maqsadi `user_targets.channel_id` da saqlanadi (`get_target()` orqali o'qi)
- `stream_recorder` — global singleton (`from stream_recorder import stream_recorder`)
- FFmpeg path har doim `get_ffmpeg_path()` orqali ol, hardcode qilma

### 8.3 Xatoliklarni bartaraf etish:
- Import juda sekin (30-120 soniya) — bu Windows antivirus yoki disk I/O, kod xatosi emas
- `bot.polling.lock` faylini o'chirmasdan oldin bot to'xtatilganini tekshir
- `recordings/_pipeline_test*` papkalari test artefaktlari — o'chirib tashlash mumkin

---

## 9. PAKETLAR VA VERSIYALAR

```
pyrofork==2.3.58        Pyrogram forki (pyrofork, import: pyrogram)
tgcrypto==1.2.5         MTProto kriptografiya
py-tgcalls==2.2.8       PyTgCalls high-level API
ntgcalls>=1.2.0         Low-level WebRTC binding
numpy>=1.26.4           Audio mixing (PCM frames)
static-ffmpeg>=2.7      FFmpeg bundled binary
qrcode==7.4.2           QR login uchun
pillow>=10.2.0          QR image render
aiofiles>=24.1.0        Async file ops
ffmpeg-python==0.2.0    ffmpeg-python wrapper (helper funksiyalarda)
```

**DIQQAT**: `pyrofork` paketi `pyrogram` nomi bilan import qilinadi:
```python
from pyrogram import Client  # TO'G'RI — pyrofork o'rnini bosadi
import pyrofork              # XATO — bunday modul yo'q
```

---

## 10. TESTLASH

Botni ishga tushirish:
```bash
cd c:\Users\User\loyiha\record
venv\Scripts\python.exe bot.py
```

Import tekshirish (bot ishga tushmasdan):
```bash
venv\Scripts\python.exe check_imports.py
```

DB holati tekshirish:
```bash
venv\Scripts\python.exe check_db.py
```
