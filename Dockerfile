FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /srv
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app
# БД SQLite лежит в /data (volume), процесс не от root
RUN useradd -u 10001 -m bot && mkdir /data && chown bot /data
USER bot
WORKDIR /data
ENV PYTHONPATH=/srv DATABASE_URL=sqlite+aiosqlite:////data/vpnbot.db
CMD ["python", "-m", "app"]
