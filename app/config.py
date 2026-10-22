from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_name: str = "Food Delivery API"
    database_url: str = "sqlite:///./food_delivery.db"
    jwt_secret_key: str = "development-only-change-this-secret"
    jwt_algorithm: str = "HS256"
    access_token_minutes: int = 30
    admin_email: str = "admin@example.com"
    admin_password: str = "ChangeMe123!"
    start_scheduler: bool = True
    debug: bool = False
    model_config = SettingsConfigDict(env_file=".env", case_sensitive=False, extra="ignore")


settings = Settings()
