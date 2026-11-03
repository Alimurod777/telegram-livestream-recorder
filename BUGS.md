# Xatolar va yechimlar

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
