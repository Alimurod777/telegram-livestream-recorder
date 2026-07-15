# claude.md — Claude uchun Maxsus Kontekst

> Bu fayl xususan Anthropic Claude (Opus, Sonnet, Haiku) modellari uchun
> loyiha haqida qisqacha, muhim yo'riqnoma hisoblanadi.

---

## 🤖 CLAUDE UCHUN KO'RSATMALAR

Sen Telegram Live Stream Recorder loyihasida ishlayapsan. Bu loyihaning eng qiyin 
va murakkab qismi — asenkron PyTgCalls va sinxron FFmpeg sub-protsesslarini
birga (concurrency) to'g'ri boshqarish.

O'zgarish qilishdan oldin quyidagilarga e'tibor ber:

### 1. ASOSIY QOIDALAR
- **Hech qachon urllib ni o'zgartirma**: Loyiha Bot API uchun requests/aiohttp emas, aynan urllib ishlatadi.
- **AsyncIO thread lari**: Bot API va FFmpeg I/O kabi barcha sinxron bloklovchi operatsiyalar `asyncio.to_thread()` orqali amalga oshiriladi.
- **PyTgCalls API**: `py-tgcalls` v2.2.8 ishlatiladi. Frame qabul qilish `.record()` orqali amalga oshadi, call property lar async.

### 2. XATOLIKLAR (COMMON PITFALLS) - BUNGA YO'L QO'YMA
- XATO: `await call_client.calls` — bu avval xato deb o'ylangan edi, lekin tekshiruvlar shuni ko'rsatdiki, u ASYNC property va AWAIT QILINISHI SHART!
- XATO: `update.status & ChatUpdate.Status.LEFT_CALL` — `Status` enum obyekt bo'lgani uchun `.value` orqali to'g'ri formatga (`int`) o'tkazib solishtirish kerak.
- XATO: Video freymlarning o'lchamini (`1280x720` kabi) qattiq belgilab qo'yish (hardcode). `ntgcalls` ixtiyoriy o'lchamdagi kadrlarni jo'natadi, o'lchamni birinchi freymdan o'qish zarur.

### 3. KOD TUZILISHI (ARCHITECTURE)
- **`bot.py`** — Manual polling va buyruqlar joylashgan asos.
- **`stream_recorder.py`** — Media stream qabul qilish, audio mixing (Numpy) va FFmpeg orqali kodlash (encoding).
- **`database.py`** — SQLite ma'lumotlar bazasi wrapperi.

### 4. QO'SHIMCHA MA'LUMOT
Loyihaning to'liq holati, arxitekturasi va oldingi xatolar haqida bilish uchun:
- Barcha AI uchun umumiy qoidalar: `agents.md` ni o'qi.
- Loyihaning hozirgi holati va muammolar: `memory.md` ni o'qi.
