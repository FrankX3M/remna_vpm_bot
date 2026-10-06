from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    bot_token: str
    admin_ids: str = ""

    # Выход к Telegram. В РФ api.telegram.org заблокирован, поэтому бот ходит через SOCKS5-прокси
    # (контейнер tg-egress -> Xray -> FIN). Пусто = напрямую (только если у сервера западный IP).
    telegram_proxy: str = ""
    # Файл-маркер «бот жив и достучался до Telegram»; его возраст проверяет healthcheck контейнера.
    heartbeat_file: str = "/tmp/vpnbot.alive"

    remnawave_url: str
    remnawave_token: str
    remnawave_behind_proxy: bool = False
    sub_base_url: str = ""
    squad_uuids: str = ""
    traffic_limit_gb: int = 0
    hwid_limit: int = 0

    # PostgreSQL в проде (compose задаёт сам); SQLite — только для локальной разработки и тестов
    database_url: str = "sqlite+aiosqlite:///./vpnbot.db"

    # --- веб-админка (доступна только локально: 127.0.0.1 и через Caddy панели в туннеле)
    web_enabled: bool = True
    web_host: str = "0.0.0.0"          # внутри контейнера; наружу порт публикуется только на 127.0.0.1
    web_port: int = 8088
    web_base_url: str = ""             # https://bot.1rtp.ru — адрес, на который ведёт ссылка из бота
    web_secret: str = ""               # подпись cookie-сессий; пусто = случайный на каждый запуск
    web_allowed_hosts: str = ""        # допустимые Host через запятую (пусто = хост из WEB_BASE_URL + localhost)
    web_session_hours: int = 12

    # Значения по умолчанию для настроек, которые админ меняет в веб-панели (см. settings_store.py).
    # Если в панели значение не задано — действует то, что стоит здесь.
    trial_days: int = 30
    referral_bonus_days: int = 30
    username_prefix: str = "tg_"
    log_level: str = "INFO"

    @property
    def admin_id_set(self) -> set[int]:
        return {int(x) for x in self.admin_ids.replace(" ", "").split(",") if x}

    @property
    def squad_list(self) -> list[str]:
        return [x for x in self.squad_uuids.replace(" ", "").split(",") if x]

    @property
    def web_hosts(self) -> set[str]:
        hosts = {h.strip().lower() for h in self.web_allowed_hosts.split(",") if h.strip()}
        if self.web_base_url:
            from urllib.parse import urlparse

            host = urlparse(self.web_base_url).hostname
            if host:
                hosts.add(host.lower())
        return hosts | {"localhost", "127.0.0.1"}

    @property
    def web_cookie_secure(self) -> bool:
        return self.web_base_url.lower().startswith("https://")
