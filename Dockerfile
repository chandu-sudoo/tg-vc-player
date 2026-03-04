FROM python:3.11-slim

# Install ffmpeg
RUN apt-get update && apt-get install -y ffmpeg && rm -rf /var/lib/apt/lists/*

# Set working directory
WORKDIR /app

# Copy bot file
COPY vc_movie_bot.py .

# Install Python dependencies
RUN pip install --no-cache-dir pyrogram tgcrypto pytgcalls yt-dlp pillow

# Create downloads folder
RUN mkdir -p downloads

# Run the bot
CMD ["python", "vc_movie_bot.py"]
