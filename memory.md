# memory.md — Loyiha Xotirasi va Qarorlar Jurnali

> Bu fayl loyiha haqidagi barcha muhim qarorlar, muammolar va holatni saqlaydi.
> Yangi AI sessiya boshlaganda bu faylni birinchi o'qing.

---

## HOZIRGI HOLAT (2026-10-25)

### RTMP audio so‘rovi tuzatildi (2026-10-30)
PyTgCalls `download_stream()` audio uchun `video_channel=None` bilan birga
`video_quality=2` yuborardi. Pyrofork TL serializer bu ikkala maydon uchun
bitta flag ishlatadi; natijada noto‘g‘ri so‘rov va `INPUT_FETCH_ERROR` kelardi.
`livestream/broadcast.py` har PyTgCalls instansiyasining Pyrogram bridge’iga
tuzatish o‘rnatadi: audio uchun ikkala video maydoni ham tashlab ketiladi.
Jonli RTMP kanalida eski so‘rov xatosi qayta kuzatildi; tuzatilgan so‘rov
17 387 bayt audio segment qaytardi. PyTgCalls 3.0.0 da ham shu so‘rov tuzilishi
bor, shuning uchun faqat versiya almashtirish bu xatoni bartaraf etmaydi.
Bo‘sh yozuv stop paytida FFmpeg muxer’iga berilmaydi; aniq sabab qaytariladi.

### QA-009: Sifat — native resolution (2026-10-25)
Eski kod `VideoParameters(1280x720)` bilan SFU dan past sifat so'rar va
hamma kadrni 1280x720 canvas ga pad/scale qilardi. Endi:
- `RecordStream` 1920x1080@60 so'raydi (ntgcalls pasaytirmaydi)
- Encoder source native o'lchamida yozadi, bitrate ladder: 1080p→5000k, 720p→3000k
- Audio stereo qoladi (`dynaudnorm` faqat `AUDIO_NORMALIZE=1` da)
- `AudioParameters.bitrate` = PCM sample rate (48000 bo'lishi SHART, Opus bitrate EMAS)

### QA-010: Rolling segments — "era" (2026-10-25)
~1.5 GB (`SEGMENT_ROTATE_BYTES`) ga yetganda `MuxerManager.request_rotation()`
yangi era ochadi; tugagan era background thread da partNNN.mp4 ga merge bo'ladi
va yozish davomida yuklanadi. Frame yo'qotish yo'q: video writer FIFO queue +
generation guard, audio swap tick chegarasida.

### QA-011: Multi-account pool (2026-10-25)
`livestream/accounts.py` — `accounts` jadvali (session_string, flood_until,
active_recordings). FloodWait da avtomatik failover. Komandalar: /accounts,
/addaccount, /delaccount.

### QA-012: Media delivery — sendVideo/sendAudio (2026-10-25)
Eski kod `send_recording_file(..., is_video=...)` ni noto'g'ri chaqirardi —
har bir yuklash CRASH bo'lar edi. Endi: mp4+video→sendVideo (streaming,
duration, WxH, thumbnail), ogg→sendVoice, audio→sendAudio, xato→sendDocument.

### QA-013: Waiting mode (2026-10-25)
Efir boshlanmagan bo'lsa inline tugma bilan so'raladi. Asosiy trigger —
`video_chat_started` service message (event-driven, polling yo'q);
`wait_probe_loop` faqat zaxira. Scheduled efir `video_chat_scheduled` dan
keladi.

---

| Parametr | Qiymat |
|----------|--------|
| **Bot status** | ✅ Ishlamoqda (`@aloqasecretapp_bot`) |
| **User session** | ✅ Aktiv (`user_id: 7256358665`) |
| **Target kanal** | `@bepul_dars3_teacher_shahlo` (`-1003069880992`) |
| **Recording** | Hozirda faol emas |
| **DB** | `recorder.db` — 1 user, 1 target, 0 recordings |
| **PyTgCalls** | `2.2.8` |
| **ntgcalls** | `>=1.2.0` |

---

## ARXITEKTURA QARORLARI

### QA-001: Nima uchun manual urllib polling?
Bot API uchun `requests`, `aiohttp` emas, `urllib` ishlatiladi.
**Sabab**: Minimal bog'liqlik, sodda, ishonchli.
**O'zgartirma**: Faqat foydalanuvchi so'rasa o'zgartir.

### QA-002: PyTgCalls.calls async property
`call_client.calls` — `@property` sifatida ko'rinadi, lekin ichida `async def` ishlatilgan.
Shuning uchun `await call_client.calls` **TO'G'RI** va kerakli.
**Tekshirildi**: `inspect.iscoroutinefunction(calls.fget)` → `True`.

### QA-003: Video frame o'lchamini dinamik aniqlash
`ntgcalls` sender ning native o'lchamida kadrlar yuboradi (masalan, 1280×720, 640×480).
`RecordStream(camera=True)` da `width=-1, height=-1` beriladi — ya'ni "qandayini qabul qil".
`expected_size = frame.info.width * frame.info.height * 3 // 2` (I420 YUV420p).
**Xato bo'lishi**: Hardcoded `VIDEO_WIDTH * VIDEO_HEIGHT * 3 // 2` — barcha kadrlarni rad etadi.

### QA-004: Audio device filter
Guruh chaqiriqlarida incoming audio ikkita device dan kelishi mumkin:
- `Device.MICROPHONE` — ishtirokchilar mikrofoni
- `Device.SPEAKER` — aralash audio chiqish
**Qaror**: Ikkisi ham qabul qilinadi (`if update.device in (Device.MICROPHONE, Device.SPEAKER)`).

### QA-005: pyrofork import nomi
`pyrofork` paketi o'rnatiladi, lekin `pyrogram` nomi bilan import qilinadi.
`from pyrogram import Client` — TO'G'RI.

### QA-006: ChatUpdate.Status.LEFT_CALL bitmask
`LEFT_CALL.value = 87 = 0b1010111` — bu compound bitmask:
`KICKED | LEFT_GROUP | CLOSED_VOICE_CHAT | DISCARDED_CALL | BUSY_CALL`.
Tekshirish: `int(update.status.value) & int(ChatUpdate.Status.LEFT_CALL.value)`.

### QA-007: Fayl yetkazib berish — sendDocument
Bot API `sendDocument` ishlatiladi (MP4 ham, M4A ham).
**Sabab**: `sendVideo` da Telegram server side re-encode qiladi — bu sekin va sifatni pasaytiradi.
`sendDocument` original faylni o'zgartirishsiz jo'natadi.

### QA-008: Session storage
Pyrogram userbot sessiyasi `user_sessions.session_string` da saqlanadi.
`in_memory=True` ishlatiladi — disk da `.session` fayl yo'q.

---

## TUGALLANGAN ISHLAR

### 2026-07-14 — Asosiy implement
- `stream_recorder.py` — `RecordStream` + FFmpeg stdin pipe yozish
- `bot.py` — `attach_user_client_handlers()` avtomatik trigger
- `/record` komandasiga `channel_id` argumenti qo'shildi
- `recover_incomplete()` — qayta ishga tushirishda eski manifest fayllarni finalize qilish

### 2026-07-15 — Bug fixes
- Video frame hardcoded size bug tuzatildi (QA-003)
- ChatUpdate.Status bitmask enum cast tuzatildi (QA-006)
- Audio SPEAKER device filter qo'shildi (QA-004)
- `_video_width`, `_video_height` fields `RecordingSession` ga qo'shildi
- `_start_video_encoder()` endi actual frame dimensions ishlatadi
- Test papkalar (`_pipeline_test*`) tozalandi

---

## MA'LUM MUAMMOLAR VA CHEKLOVLAR

### P-001: Import sekinligi (Windows)
Python jarayonni boshlash 30-120 soniya vaqt oladi.
**Sabab**: Windows Defender/antivirus `venv/` papkasini skanerlaydi.
**Yechim**: Antivirus istisno qo'shing yoki WSL/Linux ishlatish.
**Bu kod xatosi emas.**

### P-002: Uzuq voice chat → recover
Bot to'xtatilganda aktiv recording to'xtatiladi va manifest yoziladi.
Qayta ishga tushganda `recover_incomplete()` manifest topib finalize qiladi.
**Cheklov**: Temp fayl (`.mka`/`.mkv`) to'liq bo'lmasa audio/video lost bo'lishi mumkin.

### P-003: Bot API 2 GB cheklov
Standart Bot API da fayl o'lchami 50 MB.
Local Bot API server o'rnatilsa 2 GB gacha.
`config.py` da `BOT_API_BASE_URL` o'zgartirilsa avtomatik ishlaydi.

### P-004: Bir vaqtda bir user — bir recording
Har bir foydalanuvchi faqat bitta aktiv recording sessiyasiga ega bo'lishi mumkin.
`stream_recorder.is_recording(user_id)` — tekshirish.

### P-005: PyTgCalls bir bot instance
`call_clients[user_id]` — har foydalanuvchi uchun alohida `PyTgCalls(client)` instance.
Agar foydalanuvchi logout qilsa va qayta login qilsa, yangi instance yaratiladi.

---

## MUHIT (ENVIRONMENT)

| | Qiymat |
|-|--------|
| **OS** | Windows 10 |
| **Python** | 3.10.8 |
| **Virtual env** | `c:\Users\User\loyiha\record\venv\` |
| **Loyiha papkasi** | `c:\Users\User\loyiha\record\` |
| **FFmpeg** | `static-ffmpeg` paketi orqali (auto-download) |
| **Lock fayl** | `bot.polling.lock` — bir instance tekshiruvi |

---

## KELAJAK REJALARI

- [ ] Local Bot API server ulash (2 GB fayl uchun)
- [ ] Multi-user stress test
- [ ] `recorder.log` katta bo'lganda aylanish (log rotation)
- [ ] `/record` komandasiga `--audio-only` flag
- [ ] Yozib olish davomida progress xabar (har 5 daqiqada)
- [ ] Kanal jonli efir tugashini `CLOSED_VOICE_CHAT` orqali avtomatik stop
