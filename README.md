# Telegram Live Stream Recorder 🎙️📹

A fully automated Telegram Bot that records live streams (Voice and Video chats) in any Telegram group or channel and sends the recorded file directly to your DM.

## 🚀 How it Works
1. **Login:** User logs in using a QR code, Phone Number, or Session String.
2. **Targeting:** User sets a target channel/group.
3. **Capture:** The bot uses `py-tgcalls` (ntgcalls WebRTC) to join the voice chat, intercepts the raw audio/video frames, and pipes them directly to **FFmpeg**.
4. **Delivery:** The final `.mp4` file is sent to the user via Telegram Bot API (supports up to 2GB via local Bot API).

## `/send`, avtomatik bo‘laklash va `/stop`

- `/send` shu paytgacha yig‘ilgan yozuv nusxasini yuboradi. Yozish davom etadi; yuborilgan nusxa 1.97 GB hisobini yangidan boshlamaydi.
- Joriy yozuv hajmi taxminan 1.97 GB ga yetganda, oldingi `/send` nusxasiga kirgan qism bilan yangi yozuv birlashtirilib yuboriladi. Shundan keyin yangi bo‘lak yig‘iladi.
- `/stop` yoki efir tugashi oxirgi avtomatik bo‘lakdan keyingi yozuvni yuboradi. Avtomatik yuborilgan bo‘lak qayta qo‘shilmaydi. Avtomatik bo‘lak hali yuborilmagan bo‘lsa, butun yozuv bitta qilib yuboriladi.
- Video yozuv bilan alohida audio ham yuboriladi.

Masalan, 3 soatlik efirda 1-soatda `/send` berilsa, 0–1 soat nusxasi yuboriladi. Hajm 1.5-soatda taxminan 1.97 GB ga yetsa, 0–1.5 soat bitta qilib yuboriladi. Efir 3-soatda tugaganda faqat 1.5–3 soat yuboriladi.

## 🛠️ Installation & Setup (Boshqa kompyuterda ishga tushirish uchun)

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
Copy the example configuration file and rename it to `config.py`:
```bash
# On Windows
copy config.example.py config.py

# On Linux/Mac
cp config.example.py config.py
```
Open `config.py` and replace the placeholder values with your actual data:
- `API_ID` & `API_HASH` (get from my.telegram.org)
- `BOT_TOKEN` (get from @BotFather)

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
By default, the `recordings/` folder (where large media files are saved), `recorder.db` (your personal database), and `config.py` (your secret tokens) are **ignored** by `.gitignore`. This is to protect your privacy. The bot will automatically recreate the database and folders when you run it!
