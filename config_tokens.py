# ============================================================
#  CONFIGURATION — Fill in your tokens here
# ============================================================

# ── Step 1: Get from https://my.telegram.org ──────────────────
#   Login → API Development Tools → Create App
API_ID   = 123456            # replace with your numeric API ID
API_HASH = "your_api_hash"   # replace with your API hash string

# ── Step 2: Get from @BotFather on Telegram ───────────────────
#   /newbot → follow steps → copy the token
BOT_TOKEN = "123456789:ABCDefgh_your_bot_token_here"

# ── Optional settings (leave as-is to use defaults) ───────────
DOWNLOAD_DIR   = "./downloads"   # folder to save downloaded videos
DEFAULT_VOLUME = 100             # starting volume (0–200)
MAX_QUEUE      = 20              # max videos in queue per group
YTDLP_FORMAT   = "bestvideo[height<=720]+bestaudio/best[height<=720]"
