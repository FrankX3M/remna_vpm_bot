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

    database_url: str = "sqlite+aiosqlite:///./vpnbot.db"

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
