# Telegram Live Stream Recorder 🎙️📹

A fully automated Telegram Bot that records live streams (Voice and Video chats) in any Telegram group or channel and sends the recorded file directly to your DM.

## Yo‘riqnomalar

- [Botdan foydalanish: login, parallel efir, stop/send](docs/FOYDALANISH.md)
- [Alwaysdata service: o‘rnatish, panel komandasi, yangilash](docs/ALWAYSDATA.md)
- [Service uchun tayyor sozlamalar](deploy/alwaysdata/service.example.txt)

## 🚀 How it Works
1. **Login:** User logs in using a QR code, Phone Number, or Session String.
2. **Targeting:** User sets a target channel/group.
3. **Capture:** The bot uses `py-tgcalls` (ntgcalls WebRTC) to join the voice chat, intercepts the raw audio/video frames, and pipes them directly to **FFmpeg**.
4. **Delivery:** Video/audio files are sent over MTProto; Telegram Bot API is the fallback.

## `/send`, avtomatik bo‘laklash va `/stop`

- `/send` shu paytgacha yig‘ilgan yozuv nusxasini yuboradi. Yozish davom etadi; yuborilgan nusxa 1.97 GB hisobini yangidan boshlamaydi.
- Joriy yozuv hajmi taxminan 1.97 GB ga yetganda, oldingi `/send` nusxasiga kirgan qism bilan yangi yozuv birlashtirilib yuboriladi. Shundan keyin yangi bo‘lak yig‘iladi.
- `/stop` yoki efir tugashi oxirgi avtomatik bo‘lakdan keyingi yozuvni yuboradi. Avtomatik yuborilgan bo‘lak qayta qo‘shilmaydi. Avtomatik bo‘lak hali yuborilmagan bo‘lsa, butun yozuv bitta qilib yuboriladi.
- Video yozuv bilan alohida audio ham yuboriladi.

Masalan, 3 soatlik efirda 1-soatda `/send` berilsa, 0–1 soat nusxasi yuboriladi. Hajm 1.5-soatda taxminan 1.97 GB ga yetsa, 0–1.5 soat bitta qilib yuboriladi. Efir 3-soatda tugaganda faqat 1.5–3 soat yuboriladi.

## 🛠️ Installation & Setup (Boshqa kompyuterda ishga tushirish uchun)

### Bir nechta foydalanuvchi

Bitta bot jarayoni bir nechta foydalanuvchiga parallel xizmat qiladi.
Har kim shaxsiy chatda o‘z Telegram akkaunti bilan `/login` qiladi va
`/target`, `/record`, `/send`, `/stop` buyruqlaridan foydalanadi.
Buyruqlar har bir foydalanuvchi uchun tartib bilan bajariladi; boshqa
foydalanuvchining sekin so‘rovi ularni kutib qolishga majbur qilmaydi.
Sessiyalar, kanal tanlovi va yozuvlar foydalanuvchi bo‘yicha ajratilgan,
fayllar `recordings/{user_id}/` ichida saqlanadi. Har foydalanuvchi bir vaqtda
o‘zining turli Telegram akkauntlari orqali bir nechta efir yozishi mumkin;
bitta akkaunt faqat bitta faol efirga ajratiladi; umumiy parallel yozuvlar soni serverning
CPU, RAM, disk va internet imkoniyatlariga bog‘liq. Bitta bot tokeni uchun
bitta polling jarayonini ishga tushiring.

Telegram long polling 5 soniya kutadi, HTTP ulanishining kutish chegarasi
15 soniya. Buyruq kelsa, Telegram javobni kutish muddati tugashidan oldin
qaytaradi. `.env` orqali `POLLING_TIMEOUT=5` va `POLLING_HTTP_TIMEOUT=15`
ni o‘zgartirish mumkin.
Vaqtinchalik timeout bo‘lsa, bot oxirgi update offset bilan qayta urinadi;
media yozuvi mustaqil davom etadi. Timeout muntazam takrorlansa, serverdan
`api.telegram.org:443` ga ulanish va hosting tarmog‘ini tekshiring.

Efir holati Telegram’dan har 30 soniyada qayta tekshiriladi. Efir tugasa
yoki o‘rniga yangi efir boshlansa, avvalgi yozuv yakunlanadi va akkaunt
qo‘ng‘iroqdan chiqadi; Telegram login sessiyasi keyingi yozuvlar uchun qoladi.
Audio/video kelmasligining o‘zi efir tugagan deb hisoblanmaydi.
Foydalanuvchi MTProto yuklashi ishlamasa, bot o‘z MTProto sessiyasi orqali
faylni foydalanuvchiga yuboradi. Bot API oxirgi zaxira yo‘l bo‘lib qoladi.
Fayllar yuborilgani tasdiqlanmaguncha o‘chirilmaydi.

Follow these steps to deploy or run the project on a new computer after cloning:

### 1. Clone the repository
```bash
git clone https://github.com/Alimurod777/telegram-livestream-recorder.git
cd telegram-livestream-recorder
```

### 2. Create a Virtual Environment & Install Dependencies
```bash
python -m venv venv

# Activate on Windows:
venv\Scripts\activate
# Activate on Linux/Mac:
source venv/bin/activate

pip install -r requirements.txt
```

### 3. Setup Configuration
Create `.env` from the template; keep the existing `config.py` loader:

```bash
cp -n .env.example .env
chmod 600 .env
```

On Windows: `copy .env.example .env` (only if `.env` does not already exist).
Fill `API_ID`, `API_HASH`, `BOT_TOKEN`, `OWNER_ID` and `LOG_CHANNEL_ID` in `.env`.
All optional settings are documented in [.env.example](.env.example).

### 4. Run the Bot
```bash
python bot.py
```
*(Note: On the very first run, it might take a moment to download the required FFmpeg binaries automatically).*

---

## 📂 Project Structure
- `bot.py` — Main entry point, polling loop, and Telegram commands.
- `stream_recorder.py` — Core recording engine (PyTgCalls + FFmpeg pipeline).
- `database.py` — SQLite database manager for targets, sessions, and history.
- `ffmpeg_helper.py` — Dynamic FFmpeg binary path resolver.
- `agents.md` & `claude.md` — Technical reference guidelines for AI agents working on this repo.

## ⚠️ Important Note
Credentials belong in `.env`; session backups are in `sessions/`. These, `recordings/` and `recorder.db` are ignored by Git. `config.py` is a tracked loader and contains no credentials. Keep local data when updating the repository.

### Log kanal, owner va obuna

`tg-ytdlp-bot`dagi log yo‘naltirish/takrorlarni cheklash hamda `savebotnew`dagi
request konteksti yondashuvi shu botning urllib + asyncio arxitekturasiga moslangan.
`.env.example`da yangi sozlamalar bor:

- `LOG_CHANNEL_ID`: xato va ogohlantirishlar yuboriladigan kanal yoki guruh.
  Nol bo‘lsa `LOGS_ID`, undan keyin `LOG_EXCEPTION` olinadi.
- `OWNER_ID`: log manziliga yuborish muvaffaqiyatsiz bo‘lsa, bot ownerga yozadi.
  Owner avval botga `/start` yuborgan bo‘lishi kerak.
- `LOG_DEDUP_SECONDS=120`: bir xil xabar va user/komanda uchun takroriy hisobot oralig‘i.
- `FORCE_SUB_ENABLED=true`, `SUBSCRIBE_CHANNEL=-100...` yoki `@username`,
  `SUBSCRIBE_CHANNEL_URL=https://t.me/...`: majburiy obuna va tekshirish tugmasi.
  Bot kanal a’zoligini tekshira olishi uchun kanal administratori bo‘lishi kerak.
  Obunani tekshirish xatosi ruxsat bermaydi; `/stop`, `/status`, `/logout`,
  `/cancel`, `/help` ishlashda davom etadi. Owner obuna tekshiruvidan ozod.

`/join @kanal` yoki `/join https://t.me/+taklif` login qilingan akkauntni
kanal/guruhga a’zo qiladi. Tasdiq talab qilinadigan taklifda so‘rov holati aytiladi;
keyin `/target` yoki `/record` bilan kerakli chat tanlanadi.

Bitta Telegram akkaunt bir vaqtda faqat **bitta chat efirini** yozadi.
Bu cheklov ikki xil session/client bir Telegram IDga tegishli bo‘lganda ham,
zaxira akkauntga o‘tishda ham tekshiriladi. ENV orqali limit oshirilmaydi.

### Xatoni topish

Telegram hisobotidagi `incident=...`ni `recorder.log`dan qidiring:
`rg 'incident=ID' recorder.log*`. Hisobotda vaqt, kategoriya, user/update/komanda,
modul/qator va mavjud traceback bor. `rate_limit`, `session`, `network`,
`access`, `media`, `unexpected` kategoriyalari dastlabki saralash uchun;
aniq sabab traceback va recording/FFmpeg loglari orqali tekshiriladi.
Token, session string, parol va proxy loginlari Telegramga yuborilishdan oldin
maskalanadi. Hisobot navbati cheklangan; navbat to‘lsa yoki Telegramga yuborish
ishlamasa, asl loglar lokal diskda qoladi. Topilgan sabab/yechimlar `BUGS.md`ga yoziladi.

Sozlamalar va kod o‘zgarishlari bot qayta ishga tushganda kuchga kiradi.

### Bir foydalanuvchi — bir nechta parallel efir

Asosiy akkauntga login qiling va `/addaccount <session> [label]` bilan ikkinchi
akkauntni qo‘shing. So‘ng `/record @birinchi_kanal`, undan keyin
`/record @ikkinchi_kanal` yuboring. Asosiy akkaunt band bo‘lsa, ikkinchi yozuv
bo‘sh qo‘shimcha akkaunt orqali boshlanadi. Har bir akkaunt o‘sha kanalga kira
olishi kerak. Kerak bo‘lsa `/join @kanal --account 2` orqali qo‘shiling.
`/record @kanal --account 2` bilan aynan shu akkauntni tanlash ham mumkin;
`2` — `/accounts` ko‘rsatadigan ID.

`/status` barcha yozuvlar va IDlarini ko‘rsatadi. `/stop -100...` yoki `/stop #42`
faqat tanlangan yozuvni to‘xtatadi. `/send -100...` yoki `/send #42` uning nusxasini
jo‘natib, yozishni davom ettiradi. `/stop all` va `/send all` o‘zingizning barcha
yozuvlaringiz uchun ishlaydi. Bir nechta yozuv bo‘lsa, argumentsiz komanda tanlash
ro‘yxatini ko‘rsatadi. Har yozuvning progress xabarida o‘z komandasi beriladi.

Bitta kutilayotgan efirni `/stop waiting` bilan bekor qilish mumkin; boshqa faol
parallel yozuvlar davom etadi. Bo‘sh akkaunt qolmasa, yangi yozuv boshlanmaydi va
ishlayotgan yozuvlarga ta’sir qilmaydi. Boshqa AI yordamchilar uchun amaldagi
arxitektura va cheklovlar `AGENTS.md` / `agents.md`da yozilgan.
