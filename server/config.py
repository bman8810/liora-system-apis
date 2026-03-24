from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    api_key: str = "dev"
    port: int = 8002
    cors_origins: str = "*"

    model_config = {"env_file": ".env", "extra": "ignore"}


settings = Settings()
