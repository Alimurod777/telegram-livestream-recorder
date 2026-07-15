with open("bot_live.err.log", "r", encoding="utf-8", errors="replace") as f:
    content = f.read()

# Get last 6000 characters
print(content[-6000:])
