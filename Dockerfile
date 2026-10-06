FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /srv
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY alembic.ini ./alembic.ini
COPY app ./app
# /data — для импорта старой SQLite-базы и служебных файлов; процесс не от root
RUN useradd -u 10001 -m bot && mkdir /data && chown bot /data
USER bot
WORKDIR /data
ENV PYTHONPATH=/srv
EXPOSE 8088
CMD ["python", "-m", "app"]
