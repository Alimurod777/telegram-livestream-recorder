# memory.md — Loyiha Xotirasi va Qarorlar Jurnali

### Polling connection reset diagnostikasi (2026-11-05)
- Incident 969ab6527b03 getUpdates TLS ulanishining uzilishini ko‘rsatdi.
- Polling transport xatolari retry loopda boshqariladi: birinchi INFO/darhol,
  takrorlari bounded backoff va stable polling-network WARNING; ikki ERROR yo‘q.
- Media capture lifecycle va account sig‘imi o‘zgarmadi; jonli restart qilinmadi.
- BUG-016 sabab/yechim va tarmoqsiz regression tekshiruvlarini saqlaydi.

### #14 yakunlanishi va retry diagnostikasi (2026-11-03)
- Service 31090 logi: #14 stop_reason=Telegram stream ended (17:47:36).
  306.8 MiB video + 48.0 MiB audio ikkita fayl; ketma-ket ikki video qism emas.
  Yakuniy 0 MB pending — yuborilgan fayllar o‘chirilgandan keyingi qoldiq.
- Broadcast TIME_TOO_BIG end signal emas: 100ms kutish va NOT_READY, native
  pending chunkni retry qiladi; RESYNC_NEEDED faqat haqiqiy boshqa xatolarda.
- Qisqa auto-handled upload kutishlari lokal, uzunlari stable rate_limit incident;
  birinchi polling timeout INFO, ketma-ketlari dedup network WARNING.
- Faol service/yozuvlar restart qilinmagan; credentials va installed paketlar
  o‘zgartirilmagan. BUG-015 va regressionlar sabab/yechimni saqlaydi.
- Service PATH bilan to‘liq unittest suite 155/155 o‘tdi; git diff --check toza.

### Restart race va log kanali fallback (2026-11-03)
- User #13 ishlab turganida qolgan closed-database/chat-not-found muammolarini
  hal qilishni so‘radi. Faol yozuvga tegilmagan va service restart qilinmagan.
- Read-only Bot API getChat: log kanali ochilmadi (400 chat not found), owner
  private chati ochildi. Haqiqiy xabar yuborilmadi va .env o‘zgartirilmadi.
- client_lifecycle.py: detached handle_updates tasklari instansiya bo‘yicha
  kuzatiladi; stop/disconnect oldidan cancel/await va late-update guard; reconnect
  qayta ochadi. Primary/pool/delivery/login clientlar prepare qilinadi.
- Diagnostics: missing-chat/forbidden target 300s chetlab o‘tilib owner fallback;
  keyin kanal qayta sinaladi. Network timeout kanalni disable qilmaydi. Shutdown
  yangi enqueue’larni to‘xtatadi; waiting probe cancellationlari ham kutiladi.
- Kanalni botga ochish yoki noto‘g‘ri IDni tuzatish tashqi admin sozlamasi bo‘lib
  qoladi; kod ruxsatni o‘zi bera olmaydi. BUG-013/014 sabab va cheklovlarni yozadi.
- Service PATH bilan to‘liq unittest suite: 150/150 test o‘tdi, diff check toza.
  Testlar Telegramga ulanmagan; bazadagi race real vaqtinchalik SQLite bilan sinaldi.

### Audio overflow va incident oqimi (2026-11-03)
- User boshqa profildan botning efirga kirganini tasdiqladi; asosiy joriy
  muammo audio_dropped va har drop uchun Telegram diagnostika xabari ekanini aytdi.
- #12 qayta ulanishlardan keyin audio yozmoqda. Jonli jarayon restart qilinmadi.
- EventHandler audioni SSRC bilan AudioMixerga beradi. Manbalar callbackdan
  tashqarida ham alohida saqlanib, writer har 10ms ularni bitta PCM tickka mix
  qiladi. Bitta callbackdagi capture_time tengligiga tayanish olib tashlandi.
- Umumiy source navbati 2000 tick, source soni 128; stopda partial tick pad va
  drain qilinadi. Eski handle_audio_frames helper regression/API mosligi uchun qoladi.
- Audio overflow ogohlantirishi 30s summary; diagnostics explicit per-capture
  key bilan 120s dedup, o‘zgaruvchan dropped soni incidentni ko‘paytirmaydi.
- Jonli input SSRC statistikasi eski logda yo‘q: multi-source bug qayta
  ko‘rsatildi, lekin eski yo‘qotishlarning hammasi faqat shundan deyilmaydi.
- Service PATH bilan to‘liq unittest suite: 142/142 test o‘tdi;
  `git diff --check` toza. Telegramga haqiqiy test xabari yuborilmadi.
  Tuzatishlar diskda tayyor, faol eski jarayonga keyingi xavfsiz restartda kiradi.

### Rejalashtirilgan efirni vaqtida tekshirish (2026-11-03)
- User rejalashtirilgan efirga belgilangan vaqtda kirib yozishni so‘radi.
- /record + wait odatdagi oqimda qoladi. get_channel_call schedule_date ni chat
  metadatasida beradi; probe shu vaqtda uyg‘onib, scheduled efir kechiksa 5s
  intervalga o‘tadi. Start service/raw hodisalari erta boshlanishni ham aniqlaydi.
- Schedule service/raw yangilanishlari faqat matching waiting target Eventini
  uyg‘otadi. /status vaqtni server timezone belgisi bilan ko‘rsatadi.
- Vaqt yetishi scheduled callni active qilmaydi: administrator haqiqiy efirni
  boshlashi shart. Account o‘sha paytda reserve qilinadi; boshqa yozuv to‘xtamaydi.
- Restart schedule uchun yangi DB maydoni talab qilmaydi: saqlangan targetdan
  Telegramdagi amaldagi schedule qayta o‘qiladi. FloodWait majburiy pauzasi saqlanadi.
- Tekshiruv: service PATH bilan to‘liq unittest suite — 136/136 o‘tdi;
  `git diff --check` toza. Haqiqiy Telegram so‘rovi/xabari yuborilmadi, service
  restart qilinmadi; o‘zgarish keyingi xavfsiz restartda kuchga kiradi.

### Buyruq javobi kechikishi (2026-11-03)
- Service 31090 14:42:35 da to‘g‘ri canonical command bilan ishga tushgani logda
  tasdiqlandi; 14:42:50 da polling tayyor. Keyingi timeout ortiqcha backoffni ko‘rsatdi.
- sendMessage: 10s socket timeout; faqat Markdown parsing xatosida plain fallback.
  Tarmoq timeout/403/429 xatosida qayta send duplicate va navbat kutishini oshirmaydi.
- Birinchi polling timeout retry darhol, ketma-ket timeoutlarda bounded backoff.
- /start faqat saved/cached login holatini ko‘rsatadi; Telegramga qayta ulanishni
  kutmaydi. Boshqa account komandalarida haqiqiy restore/validation o‘zgarmaydi.
- Update age va handler elapsed loglari maxfiy argumentlarsiz yoziladi.
- Kodni tekshirish uchun jonli bot restart qilinmaydi; BUG-010 regressionlari qo‘shildi.
- Tekshiruv: service PATH bilan `venv/bin/python -m unittest discover -s tests -t .`
  — 130 testning barchasi o‘tdi; `git diff --check` toza. Haqiqiy Telegram xabari
  yuborilmadi va jonli service restart qilinmadi.

> Bu fayl loyiha haqidagi barcha muhim qarorlar, muammolar va holatni saqlaydi.
> Yangi AI sessiya boshlaganda bu faylni birinchi o'qing.

---

## HOZIRGI HOLAT (2026-10-25)

### Efir tugashi va yakunlash/yuborish (2026-10-30)
- Watchdog har 30 soniyada Telegram full-chat/call holatini yangidan o‘qiydi;
  call yo‘qolsa, tugatilsa yoki call ID almashsa, yozuv yakunlanadi.
  Tarmoq xatosi yoki kadr kelmasligining o‘zi auto-stop sababi emas.
- Faylga ishlov beruvchi FFmpeg buyruqlari `-nostdin` bilan ishlaydi.
  Jonli muhitda terminaldan `c` buyrug‘ini o‘qigan remux `Enter command` da
  tiqilib qolgan edi; tuzatilgan remux 6.5 soniyada tugadi.
- 6-yozuv qayta tiklandi: 378118344 bayt video va 65862410 bayt audio
  botning o‘z MTProto sessiyasidan yuborildi, Telegram tasdiqladi.
  Asl fayllar va yetkazish jurnali recordings papkasida saqlandi.
- User MTProto ishlamasa, bot-token bilan alohida MTProto sessiyasi yuklaydi;
  Bot API oxirgi zaxira. FloodWait birinchi yo‘lda bo‘lsa zaxiraga darhol o‘tadi.
- Progress o‘zgarmasa qayta edit qilinmaydi; Telegramning `message is not modified`
  javobi muvaffaqiyat deb qabul qilinadi.
- MTProto video yuklashida thumbnail yetishmasa, FFmpeg JPEG yaratadi.
  Tiklangan video 5656 bayt JPEG thumbnail bilan qayta yuborildi va tasdiqlandi.
- Polling standartlari 5/15 soniya; yakuniy tekshiruvda 75 ta test o‘tdi.

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

### P-004: Bir akkaunt — bir efir; bir owner — ko‘p akkaunt/efir (2026-11-03)
Bitta owner turli Telegram akkauntlari orqali parallel efirlar yozadi.
Active session kaliti `(owner_user_id, channel_id)`. Eski bir-user/bir-recording
cheklovi bekor qilingan; faqat Telegram akkaunt IDsi bo‘yicha cheklov qolgan.

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

### Log kanal, obuna va akkaunt cheklovi (2026-11-03)
- `tg-ytdlp-bot/HELPERS/logger.py` va `CONFIG/env_config.py`: kanal tanlash,
  dedup va .env sozlamalari o‘rganildi. `savebotnew/core/structured_log.py`,
  `request_context.py`, `error_handler.py` va BUGS.md: kontekst/traceback va
  sabab-yechim orqali diagnostika yondashuvi moslashtirildi.
- `livestream/diagnostics.py`: log kanal/guruh birinchi, owner DM fallback;
  bounded queue, dedup, redaction, incident ID va background exception hook.
- `livestream/subscription.py`: ENV majburiy obuna, qayta tekshirish tugmasi;
  `/stop`, `/status`, `/logout`, `/cancel`, `/help` obunasiz ham ishlaydi.
- `/join` public/private invite va admin tasdig‘ini kutish holatini qo‘llaydi.
- Akkaunt limiti qat’iy 1; Telegram ID bo‘yicha start/failover tekshiruvi bor.
- Yangi ENV kalitlar manba loyihadagi log/owner/obuna sozlamalaridan qo‘shildi.
  Bot token, API ma’lumotlari va sessiyalar ko‘chirilmagan.
- Topilgan buglar va regression tekshiruvlar `BUGS.md`da.
- Tekshiruv: `venv/bin/python -m unittest discover -s tests -t .` — 94 test,
  81 o‘tdi, FFmpeg/FFprobe PATHda yo‘qligi uchun 13 pipeline test skip.
  Test harnessga vaqtinchalik SESSION_DIR qo‘shildi (production sessiyalardan izolyatsiya).
  ENV konfiguratsiyasi va `git diff --check` muvaffaqiyatli.
  Ishlayotgan bot restart qilinmadi; haqiqiy kanal/ownerga test xabar yuborilmadi.

### Bir owner uchun parallel efirlar va AI yo‘riqnomalari (2026-11-03)
- User aniqlashtirdi: bitta owner ikki yoki ko‘p akkaunt kiritgan bo‘lsa, har
  akkaunt bilan alohida efirni bir vaqtda yozishi kerak. Bir akkaunt limiti 1.
- `active_sessions` va `_stop_tasks`: `(owner_user_id, channel_id)` kalitlari;
  `get_sessions(owner)` barcha yozuvlar; ambiguous `get_session(owner)` None.
- Barcha call-ended/timer/watchdog/shutdown va finalizatsiya exact yozuvga tegishli.
  Own akkaunt band bo‘lsa owned poolga o‘tiladi. Private chat accessni pooldan
  resolve qilish va `--account ID` orqali record/join tanlash qo‘shildi.
- `/status` barcha yozuvlar; `/stop|send <chat ID|#recording ID|all>` tanlash;
  argumentsiz ko‘p yozuv holatida ro‘yxat, avtomatik birinchi yozuv tanlanmaydi.
- Bitta pending start/kutish cheklovi aktiv yozuvlar soni cheklovi emas.
  Kutish record bilan parallel ishlaydi; optional account ID DBda saqlanadi.
- AGENTS.md qo‘shildi, agents.md yangilandi, claude.md umumiy hujjatga yo‘naltirildi;
  eski bir-user/bir-recording qoidasining bekor qilingani aniq yozildi.
- Regression suite: 107 test, 94 passed, 13 FFmpeg-dependent skipped. Runtime
  bot restart qilinmadi; Telegram testlari faqat mock bilan ishlaydi.

### Parallel lifecycle cancellation audit (2026-11-03)
- Davom etish so‘roviga binoan failover va stop/start poyga holatlari tekshirildi.
- `recover_recording_account` eski akkauntni oldindan free qilmaydi. Callback
  cancellationda yangi reservationni qaytaradi; recorder call-client tayyorlash
  va identity adoption uchun javobgar, eski reservation faqat adopt bo‘lgach free.
- Capture muvaffaqiyatidan keyingi status cancellation active reservationni free
  qilmaydi. `/stop all` pending start/probe cancelini kutib sessionlarni qayta o‘qiydi.
- `_stop_tasks` endi `id(session)` bilan indekslanadi (avvalgi tuple-key haqidagi
  yozuv superseded): eski upload yangi shu-chat yozuvining stopini bloklamaydi.
  Shield caller canceli registry entryni olib tashlamaydi; done callback tozalaydi.
- `tests/test_lifecycle_races.py`: 9 yangi regression test. To‘liq suite 116 test:
  103 passed, 13 FFmpeg-dependent skipped; `git diff --check` toza.
- Amaldagi qoida va reservation transfer shartlari agents.mdga yozildi.
  Bot restart qilinmadi va haqiqiy Telegram kanaliga test xabar yuborilmadi.

### GitHub uchun konfiguratsiya namunasi (2026-11-03)
- User ENV namunasi bilan o‘zgarishlarni GitHubga push qilishni so‘radi.
- `.env.example` barcha 31 ta amaldagi os.getenv kalitini hujjatlashtiradi:
  login credentials, log/owner, obuna, Bot API/MTProto limitlari, recording sifati,
  davomiylik/disk, Mongo va session storage. Maxfiy maydonlar bo‘sh.
- `.gitignore`: ENV variantlari, virtual muhit, yangi loglar va lokal error dump
  himoyalangan; `.env.example` istisno. Oldin tracked runtime loglar commitga qo‘shilmaydi.

### Alwaysdata service va foydalanish yo‘riqnomasi (2026-11-03)
- User botni ishlatish, Alwaysdata service sozlash va GitHubga yuborishni so‘radi.
- `docs/FOYDALANISH.md`: login, join, account pool, parallel record, individual/all
  stop/send, waiting, delivery va xatolarni tushunish uchun Uzbek qo‘llanma.
- `docs/ALWAYSDATA.md`: Python3.11 venv, .env, FFmpeg tayyorlash, Advanced > Services
  paneliga aniq command, log, safe update/restart va muammo yechish ko‘rsatmalari.
- `deploy/alwaysdata/run_service.sh`: root cwd, venv Python, umask077, ENV file
  mavjudligi tekshiruvi va foreground exec. `service.example.txt` panel uchun namuna.
- Alwaysdata rasmiy hujjatlariga muvofiq SIGHUP graceful shutdown qo‘shildi.
  README eski config.py credential ko‘rsatmasi o‘rniga haqiqiy .env yo‘li bilan yangilandi.
- 121 test: 108 passed, 13 FFmpeg-dependent skipped; shell syntax va diff tekshiruvi o‘tdi.
- Alwaysdata admin panel service’i ro‘yxatdan o‘tkazilmadi: tayyor command va
  qo‘llanma beriladi. Ishlayotgan bot restart qilinmadi; Telegramga test xabar yuborilmadi.

### Alwaysdata service 31090 ishga tushish xatosi (2026-11-03)
- Foydalanuvchi service ishlamaganini aytdi. SSHda service logi tekshirildi:
  working directory `/home/telegramdata3/home/telegramdata3/telegram-livestream-recorder`
  yo‘q; service maksimal restartdan keyin to‘xtagan. Python/venv executable mavjud,
  Python3.11.17, pip check toza. Xato Python boshlanishidan OLDIN yuz bergan.
- Oldingi absolute Working directory tavsiyasi noto‘g‘ri edi. Panelda faqat
  `telegram-livestream-recorder` yoki bo‘sh maydon ishlatiladi. Command absolute qoladi.
- docs/ALWAYSDATA.md va deploy/alwaysdata/service.example.txt tuzatildi.
- Panelni o‘zgartirish/re-enable uchun to‘g‘ridan-to‘g‘ri boshqaruv vositasi yo‘q;
  mavjud service sozlamasini foydalanuvchi panelda almashtirishi kerak.

### Ishlayotgan companion service bilan mos launcher (2026-11-03)
- User `bash /home/telegramdata3/tg-ytdlp-bot/scripts/start_alwaysdata.sh bot`
  ishlayotganini ko‘rsatdi; o‘sha skript o‘qilib foreground/venv/PATH tartibi olindi.
- Recorder canonical command: `bash /home/telegramdata3/telegram-livestream-recorder/scripts/start_alwaysdata.sh bot`.
  Working directory bo‘sh qoldiriladi; Environment PYTHONUNBUFFERED=1 ixtiyoriy;
  Monitoring command bo‘sh. Actual panel service avtomatik o‘zgartirilmagan.
- `... start_alwaysdata.sh check` shu Alwaysdata SSH muhitida muvaffaqiyatli:
  Python paketlari/config, HOME/bin FFmpeg/FFprobe7.0.2 tasdiqlandi; Telegram ochilmadi.
- HOME/bin va HOME/.local/bin PATHga qo‘shilgach 123 testning BARCHASI o‘tdi,
  oldingi 13 skipped media testi ham shu natijaga kiradi.
