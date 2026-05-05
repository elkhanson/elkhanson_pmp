FROM python:3.12-slim

# Working directory
WORKDIR /app

# System deps (none beyond defaults — pure Python bot)
# Just ensure pip cache is fresh
RUN pip install --no-cache-dir --upgrade pip

# Install Python deps
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy source
COPY bot.py database.py questions.json ./

# SQLite file lives next to the code — mount a volume here in prod
# so stats survive redeploys: -v /your/data:/app
VOLUME ["/app"]

CMD ["python", "-u", "bot.py"]
