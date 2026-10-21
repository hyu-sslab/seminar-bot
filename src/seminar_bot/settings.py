from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


@dataclass(frozen=True)
class Settings:
    token: str
    data_dir: Path
    guild_id: int | None = None
    max_file_mb: int = 20

    @classmethod
    def from_env(cls) -> Settings:
        load_dotenv()
        token = os.getenv("DISCORD_TOKEN", "").strip()
        if not token or token == "replace-with-your-bot-token":
            raise ValueError(".env 파일에 DISCORD_TOKEN을 설정해주세요. (.env.example 참고)")
        guild = os.getenv("DISCORD_GUILD_ID", "").strip()
        max_mb = int(os.getenv("MAX_FILE_MB", "20"))
        if not 1 <= max_mb <= 100:
            raise ValueError("MAX_FILE_MB는 1~100 사이여야 합니다.")
        guild_id = int(guild) if guild else None
        if guild_id is not None and guild_id <= 0:
            raise ValueError("DISCORD_GUILD_ID는 양수여야 합니다.")
        return cls(token, Path(os.getenv("DATA_DIR", "./data")).resolve(), guild_id, max_mb)
