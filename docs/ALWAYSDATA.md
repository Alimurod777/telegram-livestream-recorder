# Alwaysdata’da doimiy service sifatida ishga tushirish

Bot Telegram bilan long polling orqali ishlaydi. Uning doimiy jarayoni
Alwaysdata panelidagi **Advanced → Services** orqali boshqariladi. Ushbu imkoniyat
pullik tariflarda mavjud; service foreground’da ishlashi kerak. Rasmiy manba:
[Alwaysdata Services](https://help.alwaysdata.com/en/docs/web-hosting/services/).

Quyidagi misollar mavjud akkaunt va loyiha manziliga moslangan:

```text
Akkaunt: telegramdata3
Loyiha: /home/telegramdata3/telegram-livestream-recorder
```

Boshqa akkauntda `telegramdata3` o‘rniga o‘z nomingizni yozing. Bu hujjat tayyor
service komandalarini beradi; panelda service’ni ro‘yxatdan o‘tkazish alohida qadam.

## 1. Kod va Python muhiti

SSH orqali Alwaysdata’ga kiring. Yangi o‘rnatish bo‘lsa:

```bash
cd /home/telegramdata3
git clone https://github.com/Alimurod777/telegram-livestream-recorder.git
cd telegram-livestream-recorder
PYTHON_VERSION=3.11 python -m venv venv
venv/bin/python -m pip install --upgrade pip
venv/bin/python -m pip install -r requirements.txt
venv/bin/python -m pip check
```

Loyiha papkasi allaqachon mavjud bo‘lsa, qayta clone qilmang; quyidagi yangilash
bo‘limidan foydalaning. Python 3.11 — shu loyihaning hozirgi virtual muhitiga mos
variant. Alwaysdata’da `python` va `PYTHON_VERSION` orqali versiya tanlanadi:
[rasmiy Python sozlash ko‘rsatmasi](https://help.alwaysdata.com/en/docs/web-hosting/languages/python/configuration/).
Virtual muhit yaratilgach barcha loyiha buyruqlarida `venv/bin/python` ishlatiladi.
`sudo`, systemd yoki alohida web-server kerak emas.

## 2. .env yaratish

Faqat yangi o‘rnatishda `.env` namunasini ko‘chiring:

```bash
cp -n .env.example .env
chmod 600 .env
nano .env
```

Kamida ushbu qiymatlarni o‘zingiznikiga almashtiring:

```dotenv
API_ID=0
API_HASH=
BOT_TOKEN=
OWNER_ID=0
LOG_CHANNEL_ID=0
```

`API_ID/API_HASH` — my.telegram.org/apps, `BOT_TOKEN` — @BotFather orqali olinadi.
`OWNER_ID` — operatorning Telegram user IDsi; owner avval botga `/start` yuboradi.
`LOG_CHANNEL_ID` — bot xabar yubora oladigan kanal yoki guruh IDsi.

Obuna tekshiruvi kerak bo‘lsa `FORCE_SUB_ENABLED=true`, `SUBSCRIBE_CHANNEL` va
`SUBSCRIBE_CHANNEL_URL`ni sozlang; botga kerakli kanalda admin huquqini bering.
`SESSION_DIR=./sessions` launcher loyiha papkasidan ishga tushgani uchun shu loyiha
ichiga yozadi. MongoDB ixtiyoriy; barcha kalitlar [ENV namunasida](../.env.example).
Paneldagi environment qiymatlari `.env`dagi bir xil kalitlardan ustun turadi.
`config.py`ga token yozilmaydi va uni almashtirish shart emas.

## 3. FFmpegni oldindan tayyorlash

Birinchi ishga tushirishda static-ffmpeg binarlari yuklanishi mumkin. Buni service’ni
yoqishdan oldin SSHda bajaring:

```bash
venv/bin/python -c "from ffmpeg_helper import get_ffmpeg_path, get_ffprobe_path; print(get_ffmpeg_path()); print(get_ffprobe_path())"
venv/bin/python -m unittest discover -s tests -t .
```

FFmpeg va FFprobe ishlaydigan manzillarni qaytarishi kerak. Tarmoq/yuklash xatosi
bo‘lsa uni tuzatib keyin service’ni yoqing. Testlar Telegramga xabar yubormaydi;
PATHda FFmpeg topilmasa media pipeline testlari skip bo‘lishi mumkin.

## 4. Alwaysdata panelida service qo‘shish

**Advanced → Services → Add a service** bo‘limida:

| Maydon | Qiymat |
|---|---|
| Name | `telegram-livestream-recorder` |
| Command | `/bin/sh /home/telegramdata3/telegram-livestream-recorder/deploy/alwaysdata/run_service.sh` |
| Working directory | `telegram-livestream-recorder` (home papkaga nisbatan) yoki bo‘sh |
| Environment variables | Ixtiyoriy `PYTHONUNBUFFERED=1`; launcher `-u`ni o‘zi beradi |
| Monitoring command | Bo‘sh qoldiring |

Komandani ko‘chirish uchun:

```text
/bin/sh /home/telegramdata3/telegram-livestream-recorder/deploy/alwaysdata/run_service.sh
```

**Working directory maydoniga `/home/telegramdata3/...` yozmang.** Shu serverdagi
panel maydonni account home papkasiga nisbatan hisoblaydi; to‘liq yo‘l kiritilsa
`/home/telegramdata3/home/telegramdata3/...` hosil bo‘lib service boshlanmaydi.
Faqat `telegram-livestream-recorder` yozing yoki maydonni bo‘sh qoldiring:
launcher ishchi papkani o‘zi to‘g‘rilaydi. Bu cheklov Working directory maydoniga
tegishli; Command ichidagi script manzili esa yuqoridagidek to‘liq yoziladi.

[Launcher](../deploy/alwaysdata/run_service.sh) ishchi papkani o‘zi belgilaydi,
loyihadagi venv Pythonini tanlaydi, `.env` mavjudligini tekshiradi va Pythonni
`exec` orqali foreground’da ishga tushiradi. Bot HTTP port tinglamaydi, shu sabab
port ochish, WSGI site yoki portni tekshiruvchi monitoring komandasi kerak emas.
`nohup`, `&`, `screen` yoki ichki cheksiz restart loop qo‘shmang.

Bitta bot tokeni uchun **faqat bitta** jarayon ishlasin. Hozir bot SSHda ishlayotgan
bo‘lsa, barcha foydalanuvchilarning yozuvlarini yakunlab, eski jarayonni tartibli
ravishda to‘xtating; keyin service’ni yoqing. `/stop all` faqat komandani yuborgan
foydalanuvchining yozuvlariga ta’sir qiladi. `bot.polling.lock`ni ishlayotgan bot
bor paytda o‘chirmang.

## 5. Loglar va botni sinash

Paneldagi service **Logs** tugmasidan stdout/stderrni tekshiring. Alwaysdata service
loglarini `/home/telegramdata3/admin/logs/services/`da saqlaydi; odatda bot nomi
bilan emas, paneldagi service identifikatori bilan topiladi. Jarayonlarni
**Advanced → Processes → Services**da ko‘rish mumkin.
[Rasmiy service diagnostikasi](https://help.alwaysdata.com/en/docs/web-hosting/services/).

Botning o‘z logi:

```bash
cd /home/telegramdata3/telegram-livestream-recorder
tail -n 100 recorder.log
```

Telegramda `/start`, `/login_qr`, `/status` yuboring. Keyin kerakli kanalga qo‘shilib
`/record @kanal` ishlating. To‘liq komanda misollari:
[Botdan foydalanish](FOYDALANISH.md).

## 6. Xavfsiz yangilash va restart

1. Barcha foydalanuvchilar yozuvlarini yakunlaganini tekshiring. Ularga tegishli
   `/stop all` va fayl yetkazilishini kutish kerak.
2. Panelda service’ni vaqtincha o‘chiring/to‘xtating. Ishlayotgan venv paketlarini
   yangilashdan oldin jarayon tugagan bo‘lsin.
3. SSHda:

```bash
cd /home/telegramdata3/telegram-livestream-recorder
git pull --ff-only origin main
venv/bin/python -m pip install -r requirements.txt
venv/bin/python -m pip check
venv/bin/python -m unittest discover -s tests -t .
```

4. `.env.example`ga yangi kalitlar qo‘shilgan bo‘lsa, ularni o‘zingizning `.env`ga
   kiriting; mavjud token/sessionlarni namuna qiymatlari bilan almashtirmang.
5. Panelda service’ni yana yoqing, log va `/status`ni tekshiring.

Alwaysdata Restart `SIGHUP` yuboradi. Bot uni `SIGTERM`/Ctrl+C kabi qabul qilib,
yozuvlarni yakunlashga urinadi. Service to‘xtab qolsa platforma qayta ishga
 tushiradi; ketma-ket muvaffaqiyatsizliklarda service o‘chirilishi mumkin.
Shuning uchun rejalashtirilgan restartni faol yozuvlar tugagach bajaring.
[Rasmiy restart tartibi](https://help.alwaysdata.com/en/docs/web-hosting/services/).

## 7. Tezkor muammo yechish

| Holat | Tekshiruv |
|---|---|
| `create venv` xatosi | `venv/bin/python` mavjudligi va requirements o‘rnatilganini tekshiring |
| `.env` yo‘q yoki `API_ID/API_HASH/BOT_TOKEN` xatosi | Loyiha papkasidagi `.env`ni to‘ldiring |
| `working directory '/home/.../home/...' doesn't exist` | Working directory’ni `telegram-livestream-recorder`ga almashtiring yoki bo‘shating; restart urinishlari tugagan bo‘lsa service’ni yana yoqing |
| `Another bot.py instance` yoki polling `409` | SSH va Services’da bitta token bilan ikki jarayon ishlamayotganini tekshiring |
| `git pull` lokal loglar sabab to‘xtadi | `git status`ni ko‘ring, lokal loglarni boshqa joyga saqlang; DB yoki recording fayllarini o‘chirmang |
| `FloodWait` | Telegram ko‘rsatgan muddatni kuting; kerak bo‘lsa bo‘sh boshqa akkaunt qo‘shing |
| Private kanalga kirish xatosi | Aynan yozadigan akkauntni `/join ... --account ID` bilan a’zo qiling |
| Log kanalga xabar bormayapti | Botning yozish huquqi, log IDsi va owner botni boshlaganini tekshiring |
| Service qayta-qayta o‘chadi | Avval panel Logsdagi asosiy xatoni tuzating, keyin qayta yoqing |

Diskda `MIN_FREE_DISK_BYTES` uchun kamida 2 GB zaxira va yozuvlarning o‘zi uchun
qo‘shimcha joy kerak. Parallel video yozuvlar CPU/RAM/disk yukini oshiradi.
Service’ning real ishlashi hosting limiti va Telegram tarmog‘iga ulanishiga bog‘liq.
