#!/usr/bin/env bash
#
# 07-bot-home.sh — VPN-бот (aiogram + Remnawave) на ДОМАШНЕМ сервере рядом с панелью
#
# Запускать от root на домашнем сервере ПОСЛЕ 06-panel-home.sh (панель healthy, сеть remnawave-network есть)
# и после того, как нода FIN в панели online и у неё есть публичный inbound VLESS Reality (вариант A, «FIN напрямую»).
#
# Почему здесь, а не на VPS RU / FIN:
#   - бот ходит к панели по docker-сети (http://remnawave:3000): ни новых правил ACL, ни TLS-пути через туннель;
#   - токен админского API и БД платежей остаются дома; хаб (VPS RU, главная точка риска, 708 МиБ RAM) не нагружается;
#   - на FIN бот требовал бы правило «fin -> дом», а ТЗ прямо запрещает ноде инициировать соединения к панели.
#
# Telegram в РФ заблокирован, поэтому нужен западный выход. Два режима (выбираются автоматически):
#   direct  — домашний сервер подключён к сети/Wi-Fi роутера с VPN: api.telegram.org доступен напрямую.
#             Туннель AmneziaWG этому не мешает: у дома AllowedIPs = 10.66.0.0/24, остальной трафик идёт мимо него,
#             а к панели бот ходит по docker-сети, не через туннель — ни исключений, ни второго AWG-конфига не нужно.
#   egress  — запасной вариант: контейнер tg-egress (Xray-клиент, SOCKS5 -> VLESS Reality -> FIN), нужна --vless.
# Порты на хост НЕ публикуются; бот работает через long polling (только исходящие соединения).
#
# Что делает:
#   1. Проверяет предпосылки: Docker, healthy-контейнер remnawave, сеть remnawave-network, RAM.
#   2. Копирует код в /opt/vpnbot (не трогая .env, data/ и egress/config.json при повторном запуске).
#   3. Определяет режим выхода к Telegram; для egress создаёт egress/config.json из vless-ссылки (--vless).
#   4. Создаёт .env (права 600): запрашивает токены, если не переданы флагами; существующий .env НЕ перезаписывает.
#   5. docker compose build/up, затем самопроверка: Telegram через прокси, панель, токен API.
#   6. Ждёт healthy у vpnbot.
#
# Новое в версии с PostgreSQL и веб-админкой:
#   - БД бота — PostgreSQL 17 в отдельном контейнере vpnbot-postgres (пароль генерируется один раз в .env, порт наружу не публикуется).
#     Старая SQLite-база (data/vpnbot.db) при первом запуске копируется в data/backup-sqlite-*.db и переносится в PostgreSQL,
#     после чего переименовывается в vpnbot.db.imported-* (исходные данные не удаляются).
#   - Веб-админка бота (порт 8088 внутри docker-сети) публикуется ТОЛЬКО так же, как панель: Caddy панели на туннельном
#     адресе 10.66.0.2:443 получает ещё один сайт (по умолчанию bot.1rtp.ru). Публичного доступа нет; имя должно указывать
#     на 10.66.0.2 в hosts на устройстве админа (скрипт напечатает строку). Вход — по одноразовой ссылке из команды /web.
#
# Использование:
#   ./07-bot-home.sh                      # сервер в сети с VPN-роутером: режим direct определится сам
#   ./07-bot-home.sh --vless 'vless://UUID@fin-x7.1rtp.ru:443?type=tcp&security=reality&pbk=...&sni=...&flow=xtls-rprx-vision'
#   ./07-bot-home.sh                      # повторный запуск = обновление кода и перезапуск
#   ./07-bot-home.sh --no-start           # только подготовить файлы
#
set -Eeuo pipefail

C_RESET='\033[0m'; C_GREEN='\033[0;32m'; C_YELLOW='\033[0;33m'; C_RED='\033[0;31m'; C_BLUE='\033[0;34m'
log()  { echo -e "${C_GREEN}[$(date '+%H:%M:%S')]${C_RESET} $*"; }
warn() { echo -e "${C_YELLOW}[$(date '+%H:%M:%S')] ПРЕДУПРЕЖДЕНИЕ:${C_RESET} $*" >&2; }
err()  { echo -e "${C_RED}[$(date '+%H:%M:%S')] ОШИБКА:${C_RESET} $*" >&2; }
step() { echo -e "\n${C_BLUE}==>${C_RESET} $*"; }

trap 'err "Скрипт прерван на строке $LINENO. Ничего не откатываю (логи: cd $APP_DIR && docker compose logs --tail 80)."' ERR

SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_DIR="/opt/vpnbot"
PANEL_CONTAINER="remnawave"
PANEL_NETWORK="remnawave-network"
SUB_BASE_URL="https://sub.1rtp.ru"
WEB_DOMAIN="bot.1rtp.ru"
WEB_PROXY="true"
PANEL_DIR="/opt/remnawave"
CERT_DIR="/etc/ssl/1rtp"
TUNNEL_IP="10.66.0.2"
VLESS_LINK=""
FORCE_DIRECT="false"
MODE=""
BOT_TOKEN_ARG=""; ADMIN_IDS_ARG=""; PANEL_TOKEN_ARG=""; SQUADS_ARG=""
NO_START="false"
HEALTH_TIMEOUT=180

usage() {
  cat <<EOF
Использование: $0 [опции]

  --src DIR            Каталог с кодом бота (по умолчанию: рядом со скриптом)
  --dir DIR            Каталог установки (по умолчанию: $APP_DIR)
  --vless LINK         vless://-ссылка прямого входа на FIN — принудительно включает режим egress
  --direct             принудительно режим direct (без tg-egress), даже если проверка Telegram не прошла
  --bot-token T        Токен бота от @BotFather (иначе спросит)
  --admin-ids A,B      Telegram ID админов через запятую (иначе спросит)
  --panel-token T      API-токен панели (Settings -> API Tokens) (иначе спросит)
  --squads U1,U2       UUID внутренних сквадов (иначе спросит; можно пусто — но тогда нет доступа к нодам)
  --sub-url URL        Публичный адрес подписок (по умолчанию: $SUB_BASE_URL)
  --web-domain NAME    Имя веб-админки бота (по умолчанию: $WEB_DOMAIN; должно входить в сертификат, напр. *.1rtp.ru)
  --no-web-proxy       Не трогать Caddy панели (веб-админка останется без внешнего входа)
  --panel-dir DIR      Каталог стека панели с Caddyfile (по умолчанию: $PANEL_DIR)
  --no-start           Только подготовить файлы, не запускать
  -h, --help
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --src) SRC_DIR="$2"; shift 2 ;;
    --dir) APP_DIR="$2"; shift 2 ;;
    --vless) VLESS_LINK="$2"; shift 2 ;;
    --direct) FORCE_DIRECT="true"; shift ;;
    --bot-token) BOT_TOKEN_ARG="$2"; shift 2 ;;
    --admin-ids) ADMIN_IDS_ARG="$2"; shift 2 ;;
    --panel-token) PANEL_TOKEN_ARG="$2"; shift 2 ;;
    --squads) SQUADS_ARG="$2"; shift 2 ;;
    --sub-url) SUB_BASE_URL="$2"; shift 2 ;;
    --web-domain) WEB_DOMAIN="$2"; shift 2 ;;
    --no-web-proxy) WEB_PROXY="false"; shift ;;
    --panel-dir) PANEL_DIR="$2"; shift 2 ;;
    --no-start) NO_START="true"; shift ;;
    -h|--help) usage; exit 0 ;;
    *) err "Неизвестный аргумент: $1"; usage; exit 1 ;;
  esac
done

# ---------------------------------------------------------------------------
step "1/6 Проверка предпосылок"
# ---------------------------------------------------------------------------
[[ "$(id -u)" -eq 0 ]] || { err "Запускать нужно от root."; exit 1; }
command -v docker &>/dev/null || { err "Docker не установлен (01-prepare-vps.sh)."; exit 1; }
docker compose version &>/dev/null || { err "Нет плагина docker compose (01-prepare-vps.sh)."; exit 1; }
[[ -f "$SRC_DIR/app/main.py" && -f "$SRC_DIR/docker-compose.yml" ]] || { err "В $SRC_DIR нет кода бота (app/main.py, docker-compose.yml). Укажите --src."; exit 1; }

STATUS="$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' "$PANEL_CONTAINER" 2>/dev/null || echo missing)"
[[ "$STATUS" == "healthy" ]] || { err "Контейнер $PANEL_CONTAINER не healthy (статус: $STATUS). Сначала 06-panel-home.sh."; exit 1; }
docker network inspect "$PANEL_NETWORK" &>/dev/null || { err "Нет docker-сети $PANEL_NETWORK (создаётся 06-panel-home.sh)."; exit 1; }
log "Панель healthy, сеть $PANEL_NETWORK есть."

MEM_AVAIL=$(awk '/MemAvailable/ {printf "%d", $2/1024}' /proc/meminfo)
(( MEM_AVAIL >= 400 )) || warn "Свободно только ${MEM_AVAIL} МиБ RAM — боту и Xray-клиенту нужно ~250 МиБ."

if ! command -v python3 &>/dev/null; then
  log "python3 нет — ставлю python3-minimal (нужен для конвертации vless-ссылки)."
  apt-get install -y --no-install-recommends python3-minimal >/dev/null
fi

# ---------------------------------------------------------------------------
step "2/6 Файлы бота в $APP_DIR"
# ---------------------------------------------------------------------------
mkdir -p "$APP_DIR/data" "$APP_DIR/egress"
chmod 700 "$APP_DIR"
if [[ "$(realpath "$SRC_DIR")" != "$(realpath "$APP_DIR")" ]]; then
  # код обновляем целиком, но .env, data/ и egress/config.json (секреты и состояние) не трогаем
  rm -rf "$APP_DIR/app" "$APP_DIR/tools"
  cp -a "$SRC_DIR/app" "$SRC_DIR/tools" "$APP_DIR/"
  for f in Dockerfile .dockerignore alembic.ini docker-compose.yml requirements.txt .env.example README.md smoke_test.py test_web.py test_bot.py test_trybit.py test_trybit_core.py test_egress.py 07-bot-home.sh; do
    [[ -f "$SRC_DIR/$f" ]] && cp -a "$SRC_DIR/$f" "$APP_DIR/$f"
  done
  find "$APP_DIR" -name __pycache__ -type d -prune -exec rm -rf {} +
fi
chown -R 10001:10001 "$APP_DIR/data"       # пользователь bot в контейнере
log "Код обновлён, data/ и секреты сохранены."

# ---------------------------------------------------------------------------
step "3/6 Выход к Telegram"
# ---------------------------------------------------------------------------
EGRESS_CFG="$APP_DIR/egress/config.json"
tg_direct_ok() {
  local code
  code="$(curl -sS -o /dev/null -w '%{http_code}' --max-time 10 https://api.telegram.org/ 2>/dev/null || true)"
  [[ "$code" =~ ^[1-5][0-9][0-9]$ ]]
}
if [[ -n "$VLESS_LINK" ]]; then
  MODE="egress"
elif [[ "$FORCE_DIRECT" == "true" ]]; then
  MODE="direct"
elif tg_direct_ok; then
  MODE="direct"; log "api.telegram.org доступен с этого сервера напрямую (VPN на роутере работает)."
elif [[ -s "$EGRESS_CFG" ]]; then
  MODE="egress"; log "Напрямую Telegram недоступен, использую существующий $EGRESS_CFG."
else
  err "api.telegram.org недоступен с этого сервера. Варианты:"
  err "  1) подключите сервер к сети роутера с VPN и запустите скрипт снова;"
  err "  2) --vless 'vless://...' — выход через FIN (контейнер tg-egress)."
  exit 1
fi
log "Режим выхода к Telegram: $MODE"
if [[ "$MODE" == "egress" ]]; then
  if [[ -n "$VLESS_LINK" ]]; then
    python3 "$APP_DIR/tools/vless2xray.py" "$VLESS_LINK" -o "$EGRESS_CFG"
    chmod 644 "$EGRESS_CFG"     # читает не-root процесс Xray в контейнере; каталог $APP_DIR закрыт правами 700
  fi
  chmod 755 "$APP_DIR/egress"
  PROXY_VALUE="socks5://tg-egress:1080"; PROFILE_VALUE="egress"
else
  PROXY_VALUE=""; PROFILE_VALUE=""
fi

# ---------------------------------------------------------------------------
step "4/6 .env"
# ---------------------------------------------------------------------------
ENV_FILE="$APP_DIR/.env"
ask() {  # ask VAR "подсказка" [secret]
  local var="$1" prompt="$2" secret="${3:-}" val=""
  if [[ ! -t 0 ]]; then echo ""; return; fi
  if [[ "$secret" == "secret" ]]; then read -r -s -p "$prompt: " val; echo >&2; else read -r -p "$prompt: " val; fi
  echo "$val"
}
if [[ -f "$ENV_FILE" ]]; then
  log ".env уже существует — не трогаю."
else
  BOT_TOKEN="${BOT_TOKEN_ARG:-$(ask BOT_TOKEN 'BOT_TOKEN (от @BotFather)' secret)}"
  ADMIN_IDS="${ADMIN_IDS_ARG:-$(ask ADMIN_IDS 'ADMIN_IDS (Telegram ID через запятую)')}"
  PANEL_TOKEN="${PANEL_TOKEN_ARG:-$(ask PANEL_TOKEN 'API-токен панели (Settings -> API Tokens)' secret)}"
  SQUADS="${SQUADS_ARG:-$(ask SQUADS 'SQUAD_UUIDS (через запятую)')}"
  umask 077
  sed -e "s|^BOT_TOKEN=.*|BOT_TOKEN=${BOT_TOKEN:-CHANGE_ME}|" \
      -e "s|^ADMIN_IDS=.*|ADMIN_IDS=${ADMIN_IDS}|" \
      -e "s|^REMNAWAVE_TOKEN=.*|REMNAWAVE_TOKEN=${PANEL_TOKEN:-CHANGE_ME}|" \
      -e "s|^SQUAD_UUIDS=.*|SQUAD_UUIDS=${SQUADS}|" \
      -e "s|^SUB_BASE_URL=.*|SUB_BASE_URL=${SUB_BASE_URL}|" \
      "$APP_DIR/.env.example" > "$ENV_FILE"
  chmod 600 "$ENV_FILE"
  log ".env создан (права 600)."
fi
# режим выхода к Telegram всегда синхронизируем с тем, что определили выше
for kv in "TELEGRAM_PROXY=$PROXY_VALUE" "COMPOSE_PROFILES=$PROFILE_VALUE"; do
  k="${kv%%=*}"
  if grep -q "^$k=" "$ENV_FILE"; then sed -i "s|^$k=.*|$kv|" "$ENV_FILE"; else echo "$kv" >> "$ENV_FILE"; fi
done
# PostgreSQL и веб-админка: секреты генерируем ОДИН раз и больше не меняем (пароль уже записан в том Postgres)
set_env() {  # set_env KEY VALUE — задаёт только если ключа нет или он пуст
  local k="$1" v="$2"
  if grep -qE "^$k=.+" "$ENV_FILE"; then return 0; fi
  if grep -q "^$k=" "$ENV_FILE"; then sed -i "s|^$k=.*|$k=$v|" "$ENV_FILE"; else echo "$k=$v" >> "$ENV_FILE"; fi
}
set_env PG_PASSWORD "$(openssl rand -hex 24)"
set_env WEB_SECRET "$(openssl rand -hex 32)"
set_env WEB_BASE_URL "https://$WEB_DOMAIN"
set_env WEB_ENABLED "true"
chmod 600 "$ENV_FILE"
WEB_URL="$(grep -E '^WEB_BASE_URL=' "$ENV_FILE" | cut -d= -f2-)"
WEB_DOMAIN="$(echo "$WEB_URL" | sed -E 's|^https?://||; s|[/:].*$||')"
if grep -qE '^(BOT_TOKEN|REMNAWAVE_TOKEN)=(CHANGE_ME|123456:ABC\.\.\.|eyJhbGciOi\.\.\.)?$' "$ENV_FILE"; then
  err "В $ENV_FILE не заполнены BOT_TOKEN / REMNAWAVE_TOKEN. Впишите их и запустите скрипт ещё раз."
  exit 1
fi
grep -qE '^SQUAD_UUIDS=.+' "$ENV_FILE" || warn "SQUAD_UUIDS пуст: новые пользователи бота не получат доступ к нодам."

if [[ "$NO_START" == "true" ]]; then
  warn "--no-start: стек не запускаю. Запуск: cd $APP_DIR && docker compose up -d --build"
  exit 0
fi

# ---------------------------------------------------------------------------
step "5/6 Сборка и запуск (PostgreSQL, бот, веб-админка)"
# ---------------------------------------------------------------------------
cd "$APP_DIR"
docker compose config -q || { err "docker-compose.yml не проходит проверку."; exit 1; }

# Docker Hub / ghcr.io из РФ иногда рвут соединение — повторяем (докачка идёт с места обрыва)
OK="false"
for attempt in 1 2 3 4 5; do
  if COMPOSE_PARALLEL_LIMIT=1 docker compose pull --ignore-buildable && docker compose build --pull; then OK="true"; break; fi
  warn "Скачивание/сборка не удалась (попытка $attempt из 5), повторяю через 15 с..."
  sleep 15
done
[[ "$OK" == "true" ]] || { err "Образы не получены. См. зеркала в /etc/docker/daemon.json (registry-mirrors) и повторите — скрипт идемпотентен."; exit 1; }

if [[ "$MODE" == "egress" ]]; then
  docker compose up -d tg-egress
  sleep 3
  if [[ "$(docker inspect -f '{{.State.Running}}' tg-egress 2>/dev/null)" != "true" ]]; then
    err "tg-egress не запустился. Логи:"; docker compose logs --tail 40 tg-egress || true; exit 1
  fi
fi

# --- PostgreSQL: поднимаем и ждём готовности, затем переносим старую SQLite-базу (один раз) -----------
docker compose up -d postgres
for _ in $(seq 1 30); do
  [[ "$(docker inspect -f '{{.State.Health.Status}}' vpnbot-postgres 2>/dev/null)" == "healthy" ]] && break
  sleep 2
done
[[ "$(docker inspect -f '{{.State.Health.Status}}' vpnbot-postgres 2>/dev/null)" == "healthy" ]] || { err "vpnbot-postgres не стал healthy:"; docker compose logs --tail 30 postgres || true; exit 1; }

OLD_DB="$APP_DIR/data/vpnbot.db"
if [[ -s "$OLD_DB" ]]; then
  TS="$(date +%Y%m%d-%H%M%S)"
  cp -a "$OLD_DB" "$APP_DIR/data/backup-sqlite-$TS.db"
  log "Найдена старая SQLite-база. Копия: data/backup-sqlite-$TS.db. Переношу данные в PostgreSQL..."
  set +e
  docker compose run --rm vpnbot python -m app.sqlite_import /data/vpnbot.db
  RC=$?
  set -e
  if [[ $RC -eq 0 ]]; then
    mv -n "$OLD_DB" "$OLD_DB.imported-$TS"
    log "Импорт завершён; старый файл переименован в vpnbot.db.imported-$TS (не удаляйте до проверки)."
  elif [[ $RC -eq 2 ]]; then
    warn "В PostgreSQL уже есть данные — импорт пропущен. Старый файл оставлен как есть: $OLD_DB"
  else
    err "Импорт SQLite -> PostgreSQL не удался (код $RC). Данные в PostgreSQL не записаны (одна транзакция), старый файл на месте."
    exit 1
  fi
fi

log "Самопроверка (БД + Telegram через FIN + панель + токен)..."
if ! docker compose run --rm --no-deps vpnbot python -m app.selfcheck; then
  err "Самопроверка не пройдена. Частые причины:"
  err "  - Telegram: режим $MODE; при direct проверьте, что сервер в сети с VPN (curl -sI https://api.telegram.org), при egress: docker compose logs tg-egress"
  err "  - неверный REMNAWAVE_TOKEN или панель ещё не поднялась:   docker compose logs remnawave (в /opt/remnawave)"
  err "  - неверный BOT_TOKEN"
  exit 1
fi

docker compose up -d vpnbot

# --- Веб-админка: сайт в Caddy панели (только туннельный адрес) -------------------------------------------
if [[ "$WEB_PROXY" == "true" ]]; then
  CADDYFILE="$PANEL_DIR/Caddyfile"
  DOM_RE="${WEB_DOMAIN//./\\.}"
  if [[ ! -f "$CADDYFILE" ]] || ! docker ps --format '{{.Names}}' | grep -qx caddy; then
    warn "Caddy панели не найден ($CADDYFILE / контейнер caddy) — веб-админку не публикую. Повторите запуск с --panel-dir."
  elif grep -qE "^${DOM_RE}:443" "$CADDYFILE"; then
    log "В Caddyfile уже есть сайт $WEB_DOMAIN — не трогаю."
  else
    if [[ -s "$CERT_DIR/fullchain.pem" ]] && ! openssl x509 -in "$CERT_DIR/fullchain.pem" -noout -checkhost "$WEB_DOMAIN" 2>/dev/null | grep -q 'does match'; then
      warn "Сертификат $CERT_DIR/fullchain.pem НЕ покрывает $WEB_DOMAIN — браузер покажет ошибку TLS. Выберите имя из *.1rtp.ru (--web-domain) или перевыпустите сертификат."
    fi
    BAK="$CADDYFILE.bak-bot-$(date +%Y%m%d-%H%M%S)"
    cp -a "$CADDYFILE" "$BAK"
    cat >> "$CADDYFILE" <<CADDY_EOF

# Веб-админка VPN-бота (добавлено 07-bot-home.sh). Доступна только через туннель, как панель.
$WEB_DOMAIN:443 {
	tls /certs/fullchain.pem /certs/privkey.pem
	encode zstd gzip
	reverse_proxy vpnbot:8088
}
CADDY_EOF
    if docker exec caddy caddy validate --config /etc/caddy/Caddyfile >/dev/null 2>&1 \
       && docker exec caddy caddy reload --config /etc/caddy/Caddyfile --force >/dev/null 2>&1; then
      log "Caddy: добавлен сайт $WEB_DOMAIN -> vpnbot:8088 (слушает только $TUNNEL_IP:443)."
    else
      err "Caddy не принял новый Caddyfile — возвращаю прежний ($BAK)."
      cat "$BAK" > "$CADDYFILE"          # содержимое на месте: bind-mount файла не теряется
      docker exec caddy caddy reload --config /etc/caddy/Caddyfile --force || true
      exit 1
    fi
  fi
fi

# ---------------------------------------------------------------------------
step "6/6 Ожидание healthy"
# ---------------------------------------------------------------------------
DEADLINE=$(( $(date +%s) + HEALTH_TIMEOUT ))
ST=""
while (( $(date +%s) < DEADLINE )); do
  ST="$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' vpnbot 2>/dev/null || echo missing)"
  [[ "$ST" == "healthy" ]] && break
  sleep 5
done
if [[ "$ST" != "healthy" ]]; then
  err "vpnbot не стал healthy (статус: $ST). Логи:"; docker compose logs --tail 40 vpnbot || true; exit 1
fi

LISTEN="$(ss -H -ltn 2>/dev/null | awk '{print $4}' | grep -E ':1080$' || true)"
[[ -z "$LISTEN" ]] && log "SOCKS-порт 1080 на хосте не слушает (виден только в docker-сети vpnbot-net)." || warn "На хосте слушает :1080 — проверьте compose: порты публиковаться не должны."

log "Бот запущен и проверен."
echo
warn "Дальше:"
if grep -qE '^TRYBIT_API_KEY=.+' "$ENV_FILE" 2>/dev/null; then
  warn "  Trybit: POSTBACK принимается на https://$WEB_DOMAIN/trybit/postback (внутри туннеля). Публичный адрес для кабинета Trybit — Caddy на VPS RU, см. deploy/ru-caddy-trybit.snippet и README."
else
  warn "  Trybit (оплата криптой): впишите TRYBIT_API_KEY / TRYBIT_SHOP_ID / TRYBIT_SECRET_KEY в $ENV_FILE и повторите запуск; цены в рублях: /setprice ID СУММА RUB."
fi
warn "  0) Веб-админка: на устройстве админа добавьте в hosts строку  $TUNNEL_IP $WEB_DOMAIN  (туннель AWG включён), затем отправьте боту /web — придёт одноразовая ссылка. Все параметры (пробный период, рефералы, тексты, инструкции, тарифы, промокоды) — в разделе «Настройки»."
warn "  1) В Telegram откройте бота, отправьте /start и /admin. Тарифы можно вести в веб-админке (раздел «Тарифы»)."
warn "  2) Зафиксируйте версии: docker compose images  ->  XRAY_TAG=... в $APP_DIR/.env"
warn "  3) Бэкапы (этап 9): ежедневно  docker exec vpnbot-postgres pg_dump -U vpnbot -Fc vpnbot > /var/backups/vpnbot-\$(date +%F).dump  и копия $APP_DIR/.env (в нём PG_PASSWORD и WEB_SECRET)."
warn "  4) Мониторинг: 'docker inspect -f {{.State.Health.Status}} vpnbot' != healthy -> алерт (значит нет связи с Telegram/FIN)"
warn "  5) Режим direct зависит от VPN на роутере: включите там kill-switch и проверяйте туннель AWG (awg show: handshake с хабом)."
