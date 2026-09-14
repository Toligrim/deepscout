from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    searxng_url: str = os.getenv("SEARXNG_URL", "http://127.0.0.1:8888").rstrip("/")
    db_path: Path = Path(os.getenv("DEEPSCOUT_DB", "./data/deepscout.db"))
    timeout: float = float(os.getenv("DEEPSCOUT_TIMEOUT", "20"))
    user_agent: str = os.getenv(
        "DEEPSCOUT_USER_AGENT",
        "DeepScout/0.1 (+self-hosted research tool)",
    )


settings = Settings()
