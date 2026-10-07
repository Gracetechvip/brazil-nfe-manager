from pathlib import Path
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    database_url: str = "sqlite:///./data/nfe.db"
    objects_dir: Path = Path("data/objects")
    staging_dir: Path = Path("data/staging")
    exports_dir: Path = Path("data/exports")
    backups_dir: Path = Path("data/backups")
    session_cookie: str = "nfe_session"
    app_timezone: str = "America/Sao_Paulo"
    max_xml_bytes: int = 10 * 1024 * 1024
    max_zip_bytes: int = 500 * 1024 * 1024
    max_zip_members: int = 50_000
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    def prepare_dirs(self) -> None:
        for path in (self.objects_dir, self.staging_dir, self.exports_dir, self.backups_dir):
            Path(path).mkdir(parents=True, exist_ok=True)


settings = Settings()
settings.prepare_dirs()
