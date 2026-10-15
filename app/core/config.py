from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    SECRET_KEY: str = "dev-secret-key-change-in-production"
    ENVIRONMENT: str = "development"

    POSTGRES_HOST: str = "db"
    POSTGRES_PORT: int = 5432
    POSTGRES_DB: str = "scaffold_api"
    POSTGRES_USER: str = "appuser"
    POSTGRES_PASSWORD: str = "changeme123"

    REDIS_HOST: str = "redis"
    REDIS_PORT: int = 6379
    REDIS_PASSWORD: str = "changeme123"

    ACCESS_TOKEN_EXPIRE_MINUTES: int = 15
    REFRESH_TOKEN_EXPIRE_DAYS: int = 7

    # ── Casso / VietQR Payment ──────────────────────────────────────
    # Casso dashboard → Webhook secret (the 'secure-token' header value)
    CASSO_WEBHOOK_SECRET: str = ""
    # Bank details for VietQR generation
    BANK_BIN: str = ""             # e.g. "970436" (Vietcombank)
    BANK_ACCOUNT: str = ""         # e.g. "1234567890"
    BANK_ACCOUNT_NAME: str = ""    # e.g. "NGUYEN VAN A"
    # Lifetime plan price in VND (integer)
    PRICE_LIFETIME: int = 590000
    # Shared secret embedded in your desktop/mobile app binary.
    # Clients must send it as: X-App-Secret: <value>
    # Used to guard POST /v1/orders against random internet abuse.
    # Generate with: python -c "from secrets import token_hex; print(token_hex(32))"
    APP_SECRET: str = ""

    @property
    def DATABASE_URL(self) -> str:
        return (
            f"postgresql+asyncpg://{self.POSTGRES_USER}:{self.POSTGRES_PASSWORD}"
            f"@{self.POSTGRES_HOST}:{self.POSTGRES_PORT}/{self.POSTGRES_DB}"
        )

    @property
    def REDIS_URL(self) -> str:
        return f"redis://:{self.REDIS_PASSWORD}@{self.REDIS_HOST}:{self.REDIS_PORT}/0"

    class Config:
        env_file = ".env"


settings = Settings()
