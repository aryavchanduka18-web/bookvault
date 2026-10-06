"""Application settings, loaded from .env.

Plain os.environ reads via python-dotenv - no extra settings library, so
there is nothing here you cannot read top to bottom.
"""

import os
from dataclasses import dataclass
from functools import lru_cache

from dotenv import load_dotenv

# Reads .env from the project root into os.environ. Safe to call repeatedly.
load_dotenv()


@dataclass(frozen=True)
class Settings:
    mongodb_uri: str
    db_name: str
    jwt_secret: str
    jwt_algorithm: str
    jwt_expire_minutes: int
    app_env: str


@lru_cache
def get_settings() -> Settings:
    uri = os.environ.get("MONGODB_URI")
    if not uri:
        raise RuntimeError(
            "MONGODB_URI is not set. Copy .env.example to .env and paste your "
            "Atlas connection string into it."
        )

    app_env = os.environ.get("APP_ENV", "development")
    jwt_secret = os.environ.get("JWT_SECRET", "change-me")

    # The development fallback is public knowledge, so anyone could sign their
    # own admin token against it. Refuse to boot a deployed instance with it.
    if app_env == "production" and jwt_secret in ("", "change-me"):
        raise RuntimeError(
            "JWT_SECRET must be set to a random value when APP_ENV=production."
        )

    return Settings(
        mongodb_uri=uri,
        db_name=os.environ.get("DB_NAME", "bookvault"),
        jwt_secret=jwt_secret,
        jwt_algorithm=os.environ.get("JWT_ALGORITHM", "HS256"),
        jwt_expire_minutes=int(os.environ.get("JWT_EXPIRE_MINUTES", "60")),
        app_env=os.environ.get("APP_ENV", "development"),
    )
