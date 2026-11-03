# Botdan foydalanish yo‘riqnomasi

Bu bot Telegram kanal/guruhidagi jonli audio va videoni yozib, natijani botdagi
shaxsiy chattingizga yuboradi. Asosiy qoidasi: **bitta Telegram akkaunt — bitta
faol efir; bitta foydalanuvchi bir nechta akkaunt bilan parallel efir yozishi mumkin.**

Alwaysdata’ga o‘rnatish: [ALWAYSDATA.md](ALWAYSDATA.md).
Barcha konfiguratsiya kalitlari: [../.env.example](../.env.example).

## 1. Botga kirish

Botning shaxsiy chatiga `/start` yuboring. So‘ng quyidagilardan birini tanlang:

| Komanda | Vazifasi |
|---|---|
| `/login_qr` | QR orqali Telegram akkauntga kirish |
| `/login` yoki `/login_phone` | Telefon raqami, Telegram kodi va kerak bo‘lsa 2FA paroli orqali kirish |
| `/login_session <session>` | Oldindan yaratilgan session string bilan kirish |
| `/cancel` | Kutilayotgan login jarayonini bekor qilish |

QRni Telegram ilovasidagi Settings → Devices → Link Desktop Device orqali
skanerlang. Telefon loginida botning keyingi ko‘rsatmalariga amal qiling.
Session string akkauntga kirish huquqini beradi: uni faqat o‘zingiz boshqaradigan
botga yuboring, ommaviy kanalga yoki GitHubga joylamang.

## 2. Kanal/guruhga a’zo bo‘lish

Login qilingan akkaunt yoziladigan chatga kira olishi kerak. Ommaviy kanal uchun:

```text
/join @kanal_nomi
```

Xususiy kanal uchun haqiqiy taklif havolasini yuboring:

```text
/join https://t.me/+TAKLIF_KODI
```

Administrator tasdig‘i talab qilinsa, so‘rov yuboriladi; tasdiqlangandan keyin
yozishni boshlang. Xususiy kanalning IDsi yoki post havolasi a’zolik o‘rnini bosmaydi.

## 3. Birinchi efirni yozish

```text
/target @birinchi_kanal
/record
```

Yoki maqsadni saqlamasdan to‘g‘ridan-to‘g‘ri:

```text
/record @birinchi_kanal
/record -1001234567890
/record https://t.me/kanal_nomi?livestream
```

Har misol alohida so‘rov: kerakli bittasidan foydalaning. Efir faol bo‘lsa yozish
boshlanadi. Faol bo‘lmasa, bot kutish rejimini taklif qiladi. Tasdiqlasangiz,
efir boshlanishi aniqlanganda yozishga urinadi. Har foydalanuvchi uchun bir dona
kutish maqsadi bor; bu boshqa faol yozuvlarga xalal bermaydi. Account band bo‘lsa,
efir boshlanganda bo‘sh akkaunt kerak bo‘ladi.

Rejalashtirilgan efirda ham `/record @kanal` yuborib kutishni tasdiqlang.
Bot Telegramdan boshlanish vaqtini oladi va o‘sha vaqtda efirni tekshiradi.
Efir hali boshlanmagan bo‘lsa, har 5 soniyada qayta tekshiradi; boshlangach
yozishga urinadi. Erta boshlangan efir Telegram hodisasi orqali aniqlanadi.
Vaqt o‘zgarsa, kutish yangilanadi; `/status` rejalashtirilgan vaqtni serverning
vaqt zonasi belgisi bilan ko‘rsatadi. Bot qayta ishga tushsa, kutish DBdan,
efirning amaldagi vaqti esa Telegramdan qayta olinadi. Bu efirni sizning nomingizdan
ishga tushirmaydi — administrator efirni boshlashi kerak.

## 4. Ikkinchi akkaunt va parallel efir

Ikkinchi, boshqa Telegram akkauntning session stringini oling. Serverda interaktiv
session yaratish uchun loyiha papkasida `venv/bin/python generate.py` ishlatish
mumkin. U ko‘rsatgan stringni shaxsiy bot chattingizga yuboring:

```text
/addaccount SESSION_STRING ikkinchi
/accounts
```

`/accounts` ko‘rsatgan akkaunt IDsi, masalan, `2` bo‘lsa:

```text
/join @ikkinchi_kanal --account 2
/record @birinchi_kanal
/record @ikkinchi_kanal
```

Birinchi yozuv asosiy akkauntda, ikkinchisi bo‘sh qo‘shimcha akkauntda ishlaydi.
Aniq akkaunt tanlash uchun:

```text
/record @ikkinchi_kanal --account 2
```

Har akkaunt kerakli chatga kira olishi shart. Bitta akkauntning ikki session
stringi ikkita mustaqil akkaunt hisoblanmaydi. Barcha akkauntlar band bo‘lsa,
yangi yozuv rad etiladi va ishlayotgan yozuvlar davom etadi.

## 5. Yozuvlarni boshqarish

```text
/status
```

Natijada yozuv IDsi (`#42` kabi), chat IDsi, ishlayotgan akkaunt va holat ko‘rinadi.

| Komanda | Natija |
|---|---|
| `/send -1001234567890` | Shu chat yozuvining hozirgi nusxasini yuboradi; yozish davom etadi |
| `/send #42` | IDsi 42 bo‘lgan yozuv nusxasini yuboradi |
| `/send all` | O‘zingizning barcha faol yozuvlaringiz nusxalarini yuboradi |
| `/stop -1001234567890` | Faqat shu chat yozuvini yakunlab yuboradi |
| `/stop #42` | Faqat IDsi 42 bo‘lgan yozuvni yakunlaydi |
| `/stop all` | O‘zingizning barcha yozuvlaringizni yakunlaydi va kutishni bekor qiladi |
| `/stop waiting` | Faqat kutish rejimini bekor qiladi |

Bitta yozuv bo‘lsa `/send` va `/stop` argumentsiz ham ishlaydi. Bir nechta yozuv
bo‘lsa, bot ro‘yxat ko‘rsatadi: chat ID yoki `#yozuv ID` bilan tanlang. Bu komandalar
boshqa foydalanuvchilarning yozuvlarini boshqarmaydi.

## 6. Fayllar qanday yuboriladi?

- Har yozuv standart sozlamada 4 soatgacha davom etadi.
- Taxminan 1.97 GB ga yaqin bo‘lak yozish davomida avtomatik yuboriladi.
- `/send` — nusxa; joriy yozuv davom etadi. Avtomatik hajm bo‘lagi va yakuniy
  qismni hisoblash tartibi [README](../README.md)da tushuntirilgan.
- Video bo‘lsa video va alohida audio yuboriladi. Yuborish ishlamasa, fayllar
  tiklash uchun diskda saqlanadi.
- Asosiy yuklash yo‘li MTProto; Bot API zaxira yo‘l. Ommaviy Bot API uchun
  standart limit 49 MB qilib sozlangan.

## 7. Akkauntni olib tashlash va chiqish

Qo‘shimcha akkaunt band bo‘lmasa:

```text
/delaccount 2
```

Asosiy akkauntdan chiqish uchun avval yozuvlar yakunlanishini kuting:

```text
/stop all
/logout
```

`/logout` saqlangan asosiy loginni o‘chiradi; qo‘shimcha akkauntlar `/delaccount`
orqali alohida boshqariladi.

## 8. Kanal obunasi va xatolar

Majburiy obuna yoqilgan bo‘lsa, bot kanalga qo‘shilish va obunani tekshirish
tugmalarini ko‘rsatadi. Xizmat komandalaridan `/stop`, `/status`, `/logout`,
`/cancel`, `/help` obuna tekshiruvi ishlamaganda ham foydalanish mumkin.

Muammo bo‘lsa `/status`ni tekshiring. Operatorga log kanalidagi `incident=...`
ID yordam beradi. Hisobot avval log kanal/guruhga, yuborish ishlamasa ownerning
bot chatiga ketadi. `FloodWait` chiqsa ko‘rsatilgan muddatni kuting; ketma-ket
yangi so‘rovlar yuborish o‘rniga mavjud so‘rov holatini tekshiring.
