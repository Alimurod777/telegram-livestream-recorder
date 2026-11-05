# Xatolar va yechimlar

## BUG-016 — Polling connection reset ortiqcha ERROR incident yuborardi

- Dalil: incident 969ab6527b03, 2026-11-04; getUpdates TLS handshake vaqtida
  ConnectionResetError(104) bilan uzilgan. Bu log keyingi tiklanishni tasdiqlamaydi.
- Sabab: URLError umumiy exception handlerga tushib, traceback bilan ERROR
  yuborardi; bot_api ham shu polling xatosini alohida ERROR sifatida log qilardi.
- Yechim: polling tarmoq uzilishini alohida boshqaradi: birinchi retry darhol va
  INFO, ketma-ketlari 60s gacha backoff va stable polling-network WARNING.
  getUpdates transport logi retry loopga topshirildi; boshqa API metodlari saqlandi.
- Regression: wrapped/direct connection reset, offset saqlanishi, retry delay,
  stable diagnostic key va takroriy transport ERROR yo‘qligi. Telegram mock.
  Service restart qilinmaydi; o‘zgarish keyingi ishga tushirishda kuchga kiradi.

## BUG-015 — Tayyor bo‘lmagan broadcast chunk resync va oddiy retry incidentlari

- Dalil: #14 17:47:36 da `Telegram confirmed stream ended` sabab yakunlangan;
  leave ham GROUPCALL_FORBIDDEN (call ended) qaytargan. TIME_TOO_BIG bundan 22s
  oldin kelgan, stop sababi emas. Video 306.8 MiB va alohida audio 48.0 MiB
  muvaffaqiyatli yuborilgan; dropped hisoblagichlar 0.
- Sabab: broadcast shim TIME_TOO_BIG ni raise qilgan; PyTgCalls bu holda
  NOT_READY o‘rniga RESYNC_NEEDED qaytaradi. Telegram hujjatiga ko‘ra bu
  chunk hali tayyor emas: https://core.telegram.org/api/group-calls.
- Yechim: faqat shu BadRequest va broadcast FloodWait 100ms kutib None qaytaradi;
  native callback NOT_READY olib pending chunkni saqlaydi. Boshqa xatolar raise.
- Pyrofork o‘zi kutib qayta urinadigan SaveBigFilePart <=10s ogohlantirishlari
  lokal logda qoladi; kattaroq kutishlar rate_limit va stable dedup bilan yuboriladi.
  Birinchi polling timeout INFO, ketma-ket timeout WARNING/network.
- Regression: test_broadcast, test_diagnostics, test_polling; Telegram mock.
  Service restart qilinmagan; tuzatishlar keyingi xavfsiz restartda kuchga kiradi.

## BUG-013 — Restartda Pyrofork sessiya bazasi yopilgandan keyin update yozilishi

- Holat: kod tuzatildi, 2026-11-03; faol service restart qilinmadi.
- Dalil: 15:23:35 da Client.handle_updates → fetch_peers → storage.update_peers
  `sqlite3.ProgrammingError: Cannot operate on a closed database` bilan yiqilgan.
- Sabab: Pyrofork Session.handle_packet update tasklarini create_task bilan
  ishga tushirib, Client.stop/disconnect paytida ularni kutmasdan SQLite’ni yopadi.
- Yechim: client_lifecycle.py har client instansiyasidagi update tasklarini
  kuzatadi; stop/disconnect oldidan yangi update’larni bloklaydi va in-flight
  tasklarni cancel/await qiladi. Connect gate’ni qayta ochadi. Primary, pool,
  delivery va login clientlarida qo‘llanadi; installed paketlar o‘zgartirilmaydi.
  Shutdown kutish probe cancellationlarini ham klientlarni yopishdan oldin kutadi.
- Tekshiruv: tests/test_client_lifecycle.py real in-memory SQLite bilan close
  race, late update, reconnect, boshqa clientga ta’sir yo‘qligi va haqiqiy
  update xatolari yashirilmasligini tekshiradi.

## BUG-014 — Yopiq log kanaliga har incidentda qayta murojaat qilinishi

- Holat: fallback/cooldown tuzatildi; kanalga kirish huquqi tashqi sozlama.
- Dalil: configured LOG_CHANNEL_ID uchun read-only getChat `chat not found`,
  OWNER_ID uchun getChat muvaffaqiyatli. Tekshiruvda xabar yuborilmagan.
- Sabab: diagnostics har safar yetib bo‘lmaydigan kanalga sendMessage qilardi;
  owner fallback ishlasa ham har urinish yangi HTTP 400 lokal xato yozardi.
- Yechim: 400 missing chat/rights va 403 da 300s per-target cooldown; shu paytda
  ownerga yuborish, muddati tugagach kanalga qayta urinish. Network timeout bu
  cooldownni qo‘ymaydi. Barcha manzillar ishlamasa takroriy transport logi jamlanadi.
  Shutdown yangi diagnostics enqueue’larini to‘xtatib mavjud navbatni tugatadi.
- Cheklov: bu botni kanalga qo‘shmaydi va noto‘g‘ri IDni taxminan almashtirmaydi;
  log kanali uchun to‘g‘ri ID va botning yuborish huquqi kerak.
- Tekshiruv: tests/test_diagnostics.py — missing-chat javobi va exception,
  owner fallback, cooldown tugagach kanal tiklanishi, transient timeout va late
  shutdown callback. Telegram transporti mock qilingan.

## BUG-012 — Audio navbati to‘lishi va diagnostika xabarlari oqimi

- Holat: kod tuzatildi, 2026-11-03; faol yozuv sabab jonli service restart qilinmadi.
- Dalil: #12 audio yozuvi o‘smoqda, ammo audio_dropped minglab oshgan; har bir
  o‘zgargan dropped hisoblagichi alohida Telegram incidentga aylangan.
- Koddagi sabab: audio faqat BIR callback ichidagi teng capture_time bo‘yicha
  mix qilinardi. Mustaqil callbacklarda kelgan SSRC manbalari bitta navbatga
  ketma-ket yozilardi; ikki parallel manba 1s o‘rniga 2s PCM hosil qilishi mumkin.
  Bu xato regressionda qayta ko‘rsatildi; jonli ortiqcha oqimning har bir manbasi
  eski logda yozilmagani uchun barcha yo‘qotishlarning yagona sababi deb aytilmaydi.
- Yechim: SSRC bo‘yicha alohida buffer, writerda har 10ms bitta aralashtirilgan
  tick. Umumiy chegara 2000 tick, source soni 128, pending tail stopda drain.
  Rotation va pipe recovery mavjud tick chegarasida qoladi.
- Log muammosi: har drop WARNING edi, diagnostics dedup esa sonli matnni hash
  qilardi. Endi 30s local summary, per-capture diagnostic_key orqali 120s dedup.
  Channel/owner/traceback/redaction saqlanadi; audio queue = media kategoriyasi,
  call lost = network. Hisoblagichlar har drop uchun baribir yangilanadi.
- Regression: tests/test_audio_sources.py va EventRegressionTests — alohida
  callbackdagi manbalar, sequential/subtick PCM, writer stop/tail, bounded queue,
  throttling, per-recording Telegram dedup. Haqiqiy Telegramga xabar yuborilmaydi.

## BUG-011 — Rejalashtirilgan efir vaqti kutish timeriga ta’sir qilmasdi

- Holat: kod tuzatildi, 2026-11-03; jonli servis restart qilinmadi.
- Sabab: schedule_date mavjud call faol emas deb qaytarilardi, lekin uning vaqti
  probe loopga uzatilmasdi. Start hodisasi kelmay qolsa, 120s fallback kechikardi.
- Yechim: call metadata schedule vaqtini olib yuradi; probe belgilangan vaqtda
  uyg‘onadi, kechikkan scheduled efirni har 5s tekshiradi. Service/raw schedule
  yangilanishi Event bilan kutishni uyg‘otadi. Faqat haqiqiy active call yoziladi;
  vaqtning kelishi scheduled callni live qilmaydi. FloodWait alohida kutiladi.
- Restartda schedule Telegramdan qayta olinadi; DBdagi account/target o‘zgarmaydi.
  Boshqa yozuvlar, ownership va account reservation qoidalari saqlanadi.
- Regression: tests/test_scheduled_waiting.py — vaqtga uyg‘onish, kechikkan efir,
  schedule yangilanishi, target isolation, restartdan rediscovery, cancellation
  va active bo‘lgandan keyingina start. Telegram transporti mock qilingan.

## BUG-010 — Tarmoq xatosi buyruqlar javobini ortiqcha kechiktirishi

- Holat: kod tuzatildi, 2026-11-03; ishlab turgan service avtomatik restart qilinmadi.
- Dalil: service 31090 14:42:50 da pollingni boshlagan; 14:43:15 da timeout,
  keyin 3 soniya qo‘shimcha backoff. Bu log kechikishning barcha sababini isbotlamaydi.
- Sabab: sendMessage har qanday xatoda Markdown o‘rniga plain text bilan qayta
  urinardi; ikki 60 soniyalik socket timeout bitta user navbatini ushlab qolishi
  mumkin edi. /start akkauntni qayta ulashni ham kutardi.
- Yechim: sendMessage socket timeout 10 soniya; faqat Telegramning 400
  `can't parse entities` xatosida plain text fallback. Birinchi polling timeoutdan
  keyin darhol retry, ketma-ket xatolarda backoff saqlanadi. /start cached yoki
  saqlangan session holatini o‘qiydi, Telegramga connect qilmaydi. Haqiqiy account
  komandalarida oldingi restore/authorization tekshiruvi saqlanadi.
- Diagnostika: command argumentlarisiz update yoshi va handler davomiyligi logga
  yoziladi; Telegram/tarmoq kechikishi va handler kutishini ajratish mumkin.
- Regression: tests/test_command_latency.py va tests/test_polling.py — timeoutda
  duplicate yo‘qligi, parsing fallback, tez /start, offset saqlanishi va birinchi
  polling timeoutdagi darhol retry. Telegram transporti mock qilingan.

## BUG-001 — Akkaunt bir vaqtda bir nechta chatga record qilishi mumkin edi

- Holat: tuzatildi, 2026-11-03.
- Sabab: `MAX_RECORDINGS_PER_ACCOUNT` ENV orqali oshirilardi; failover paytida
  bir xil Telegram ID bilan ulangan boshqa client bandligi tekshirilmasdi.
- Yechim: pool va recorder limiti qat’iy 1; start va failover vaqtida Telegram
  akkaunt identifikatori tekshiriladi. Bir xil akkauntning turli sessiyalari
  ham ikkinchi efirga qo‘yilmaydi.
- Tekshiruv: ikki client/bitta Telegram ID, boshqa chat, bir xil chat va band
  failover akkaunti uchun tarmoqsiz regression testlar.

## BUG-002 — Xatolar faqat lokal logda qolardi

- Holat: tuzatildi, 2026-11-03.
- Sabab: botda Telegram diagnostika handleri yo‘q edi.
- Yechim: WARNING/ERROR va background task xatolari bounded async navbat orqali
  log kanal/guruhga; xatoda owner bot chatiga yuboriladi. Transport xatosi o‘zini
  qayta report qilmaydi. Hisobotlar incident ID, request konteksti, traceback,
  maskalash va 120 soniyalik dedup bilan yuboriladi.
- Tekshiruv: kanal muvaffaqiyati, exception/`ok=false` fallback, rekursiya yo‘qligi,
  worker thread konteksti, maxfiy qiymatlar, UTF-16 limit va queue chegarasi.

## BUG-003 — Xususiy kanal havolalari noto‘g‘ri parse qilinardi

- Holat: tuzatildi, 2026-11-03.
- Sabab: `t.me/joinchat/...` username deb, `t.me/c/ID/post` esa `@c` deb olinardi.
- Yechim: invite havolasi o‘z holicha saqlanadi, private post havolasidan -100 ID
  olinadi; `/join` login qilingan akkaunt bilan a’zolikni amalga oshiradi.

## BUG-004 — Ikkinchi akkaunt bo‘lsa ham owner ikkinchi efirni boshlay olmasdi

- Holat: tuzatildi, 2026-11-03.
- Sabab: `active_sessions` va stop tasklari faqat user ID bilan indekslangan;
  `/record`, kutish tekshiruvi va start helper birinchi yozuv borligida qaytardi.
  Akkaunt sig‘imi bilan owner yozuvlari soni aralashtirilgan.
- Yechim: faol yozuvlar `(owner, channel)` kalitiga o‘tdi; bitta owner turli
  akkauntlarda turli chatlarni parallel yozadi. Akkaunt sig‘imi hanuz 1 va
  Telegram ID bilan tekshiriladi. Own session band bo‘lsa owned pool tanlanadi;
  private kanalni kirish huquqi bor akkaunt orqali resolve qilish mumkin.
- `/stop`, `/send`, timer/watchdog/call-ended/shutdown va upload state alohida
  yozuvni boshqaradi. Bir nechta yozuvda argumentsiz stop/send tanlov so‘raydi;
  `all` faqat shu ownerning yozuvlarini tanlaydi. `--account ID` ownership bilan
  tekshiriladi va pool reservation atomik qilinadi. Kutishning account IDsi DBda
  saqlanadi. Boshqa yozuv finalizatsiyasi ownerning qolgan yozuvini o‘chirmaydi.
- Tekshiruv: bitta owner/ikki akkaunt/ikki chat; bir akkaunt bilan concurrent
  start; band own accountdan poolga o‘tish; boshqa owner akkauntini rad etish;
  per-recording va parallel stop; ambiguous stop/send; timer target; private
  chatning pool bilan resolve bo‘lishi; selected pool reservation va waiting
  persistence regression testlari.

## BUG-005 — Failover bekor qilinsa akkaunt band qolishi yoki erta bo‘shatilishi

- Holat: tuzatildi, 2026-11-03.
- Sabab: replacement kanalini resolve qilishda CancelledError reservationni
  bo‘shatmasdi; oldingi account replacement adopt bo‘lishidan oldin free qilinardi.
  Yangi call-client tayyorlash xatosida session identity qisman almashtirilardi.
  Capture boshlangandan keyin status yuborish bekor qilinsa ham account free bo‘lardi.
- Yechim: callback faqat tayyorlangan/reserve qilingan candidate qaytaradi;
  resolve xatosi/cancelda candidate free qilinadi. Recorder yangi call-clientni
  tayyorlaydi, capacity va finalizing holatini tekshiradi, barcha identity
  maydonlarini birga o‘zgartiradi, keyin eski accountni free qiladi. Adoptgacha
  xato/cancelda faqat candidate free. Post-start notification start cleanupdan ajratildi.
- Tekshiruv: tests/test_lifecycle_races.py — resolve cancellation, prepare
  failure/cancellation, successful transfer, stop-during-recovery, post-start
  notification cancellation. Telegramga haqiqiy so‘rov yuborilmaydi.

## BUG-006 — Stop cancellation va parallel start yakunlashni adashtirishi

- Holat: tuzatildi, 2026-11-03.
- Sabab: shieldni kutayotgan caller cancellationda `_stop_tasks`dan task darhol
  olinardi, holbuki finalization ishlashda davom etardi. Ikkinchi stop dublikat
  finalizer yaratishi mumkin edi. Bir xil chat yangi yozuvda qayta ishlatilsa,
  eski upload taski yangi stopni bloklardi. `/stop all` esa pending startni
  cancel qilishdan OLDINGI session snapshotni ishlatardi.
- Yechim: finalizerlar runtime session identity (`id(session)`) bilan ajratiladi,
  task haqiqiy tugaganda done callback registrydan oladi. Pending start/probe
  cancellationlari kutilgandan keyin stop session ro‘yxatini yangidan oladi.
- Tekshiruv: cancelled stop waiterda single finalization; bir chatdagi eski
  delivery va yangi recordingning mustaqil stopi; cancel paytida commit bo‘lgan
  yangi recordingni `/stop all` qamrab olishi regression testlari.

## BUG-007 — Alwaysdata restart signali yozuvlarni tartibli yakunlamasdi

- Holat: tuzatildi, 2026-11-03.
- Sabab: Alwaysdata service Restart SIGHUP yuboradi, bot esa faqat SIGINT/SIGTERM
  uchun shutdown handler o‘rnatardi. SIGHUP default holatda jarayonni to‘xtatardi.
- Yechim: `install_shutdown_handlers` Linuxda SIGHUPni ham mavjud shutdown eventga
  ulaydi; yozuvlarni yakunlash va client cleanup yo‘li ishga tushadi. Service
  launcher to‘g‘ri cwd, venv Python va foreground exec bilan signalni Python’ga uzatadi.
- Tekshiruv: SIGHUP/SIGTERM/SIGINT event callbacklari, platform fallback va
  launcherning PID/cwd/argument/umask/ENVni shell sifatida bajarmaslik testlari.
- Manba: https://help.alwaysdata.com/en/docs/web-hosting/services/

## BUG-008 — Alwaysdata Working directory maydonida home yo‘li takrorlanishi

- Holat: yo‘riqnoma tuzatildi; mavjud service panelidagi qiymatni almashtirish kerak.
- Sabab: yordamchi paneldagi Working directory maydoniga absolute loyiha yo‘lini
  tavsiya qilgan. Service 31090 logida platforma home yo‘lini yana qo‘shib,
  `/home/telegramdata3/home/telegramdata3/telegram-livestream-recorder` mavjud
  emasligi sabab Pythonni ishga tushirmagan. Maksimal restart urinishlaridan keyin
  2026-11-03 09:47:22 da service stopped bo‘lgan.
- Yechim: Working directory = `telegram-livestream-recorder` yoki bo‘sh; Command
  ichidagi `/bin/sh /home/telegramdata3/.../run_service.sh` absolute yo‘li saqlanadi.
  Service’ni panelda yana yoqish kerak. Launcher ishchi papkani o‘zi belgilaydi.
- Dalil: `/home/telegramdata3/admin/logs/services/2026/31090-2026-11-03.log`.

## BUG-009 — Service PATH accountdagi FFmpegni topmasligi

- Holat: tuzatildi, 2026-11-03.
- Sabab: non-interactive service PATHiga `$HOME/bin` kirmasdi; mavjud
  `/home/telegramdata3/bin/ffmpeg` va `ffprobe` SSH/test default PATHida ham topilmasdi.
- Yechim: ishlayotgan tg-ytdlp-bot launcheriga mos `scripts/start_alwaysdata.sh`
  qo‘shildi (`bot|check`); account bin manzillari PATHga qo‘shiladi. Oldingi deploy
  launcher ham shu PATHni oladi. Canonical komanda endi `bash .../scripts/start_alwaysdata.sh bot`.
- Tekshiruv: real `check` — credentials yashirilgan, barcha importlar va FFmpeg7.0.2
  ishladi; Telegram clienti ochilmadi. Service PATH bilan 123 testning barchasi
  o‘tdi, shu jumladan avval skip bo‘lgan 13 media pipeline testi.
