from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from dotenv import load_dotenv


@dataclass(frozen=True)
class Settings:
    token: str = field(repr=False)
    data_dir: Path
    guild_id: int | None = None
    max_file_mb: int = 20
    rotation_file: Path = Path("config/rotation.json")
    api_token: str = field(default="", repr=False)
    api_guild_id: int | None = None
    seminar_page_url: str = ""
    api_host: str = "127.0.0.1"
    api_port: int = 8080
    parser_url: str = ""
    access_team_domain: str = ""
    access_audience: str = ""
    api_transport: str = "access"
    relay_token: str = field(default="", repr=False)
    relay_url: str = ""
    connect_token: str = field(default="", repr=False)

    @property
    def active_guild_id(self) -> int | None:
        return self.api_guild_id or self.guild_id

    def submission_url(self, session_id: int) -> str:
        return f"{self.seminar_page_url}?session={session_id}" if self.seminar_page_url else ""

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
        api_token = os.getenv("SEMINAR_API_TOKEN", "").strip()
        api_guild = os.getenv("SEMINAR_GUILD_ID", "").strip()
        api_guild_id = int(api_guild) if api_guild else None
        page_url = os.getenv("SEMINAR_PAGE_URL", "").strip()
        parser_url = os.getenv("DOCUMENT_PARSER_URL", "").strip()
        access_team = os.getenv("CLOUDFLARE_ACCESS_TEAM_DOMAIN", "").strip().lower()
        access_aud = os.getenv("CLOUDFLARE_ACCESS_AUD", "").strip()
        transport = os.getenv("SEMINAR_API_TRANSPORT", "access").strip()
        relay_token = os.getenv("SEMINAR_RELAY_TOKEN", "").strip()
        relay_url = os.getenv("SEMINAR_RELAY_URL", "").strip()
        connect_token = os.getenv("SEMINAR_CONNECT_TOKEN", "").strip()
        port = int(os.getenv("SEMINAR_API_PORT", "8080"))
        if not 1 <= port <= 65535:
            raise ValueError("SEMINAR_API_PORT는 1~65535여야 합니다.")
        if api_token and not re.fullmatch(r"[A-Za-z0-9_-]{32,256}", api_token):
            raise ValueError("SEMINAR_API_TOKEN은 안전한 임의 문자열 32~256자로 설정해주세요.")
        if transport not in {"access", "vpc", "relay"}:
            raise ValueError("SEMINAR_API_TRANSPORT는 access, vpc 또는 relay여야 합니다.")
        if (
            api_token
            and transport in {"vpc", "relay"}
            and (
                not re.fullmatch(r"[A-Za-z0-9_-]{32,256}", relay_token) or relay_token == api_token
            )
        ):
            raise ValueError(
                "SEMINAR_RELAY_TOKEN은 API 토큰과 다른 안전한 임의 문자열 32~256자로 설정해주세요."
            )
        if relay_url:
            relay = urlsplit(relay_url)
            try:
                relay_port = relay.port
            except ValueError as exc:
                raise ValueError("SEMINAR_RELAY_URL의 포트를 확인해주세요.") from exc
            if (
                relay.scheme != "wss"
                or not re.fullmatch(
                    r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\."
                    r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.workers\.dev",
                    relay.hostname or "",
                )
                or relay_port not in {None, 443}
                or relay.username
                or relay.password
                or relay.path != "/connect"
                or relay.query
                or relay.fragment
                or any(character.isspace() or ord(character) > 127 for character in relay_url)
            ):
                raise ValueError(
                    "SEMINAR_RELAY_URL은 workers.dev의 WSS 443 /connect 주소여야 합니다."
                )
        if api_token and transport == "relay" and not relay_url:
            raise ValueError("중계 연결에는 SEMINAR_RELAY_URL 설정이 필요합니다.")
        if (
            api_token
            and transport == "relay"
            and (
                not re.fullmatch(r"[A-Za-z0-9_-]{32,256}", connect_token)
                or connect_token in {api_token, relay_token}
            )
        ):
            raise ValueError(
                "SEMINAR_CONNECT_TOKEN은 API 및 중계 토큰과 다른 안전한 임의 문자열 32~256자로 설정해주세요."
            )
        if api_guild_id is not None and not 0 < api_guild_id <= 2**63 - 1:
            raise ValueError("SEMINAR_GUILD_ID는 Discord 서버의 양수 ID여야 합니다.")
        if page_url:
            url = urlsplit(page_url)
            if (
                url.scheme != "https"
                or not url.hostname
                or url.username
                or url.password
                or url.query
                or url.fragment
                or any(character.isspace() for character in page_url)
            ):
                raise ValueError(
                    "SEMINAR_PAGE_URL에 쿼리 없는 HTTPS 제출 페이지 주소를 설정해주세요."
                )
        if api_token and (api_guild_id is None or not page_url):
            raise ValueError(
                "웹 API를 사용하려면 SEMINAR_GUILD_ID와 SEMINAR_PAGE_URL도 설정해주세요."
            )
        if api_token and not parser_url:
            raise ValueError(
                "웹 API에는 격리된 DOCUMENT_PARSER_URL 설정이 필요합니다. Docker Compose 설정을 사용해주세요."
            )
        if parser_url:
            parser = urlsplit(parser_url)
            if (
                parser.scheme not in {"http", "https"}
                or not parser.hostname
                or parser.username
                or parser.password
                or parser.path not in {"", "/"}
                or parser.query
                or parser.fragment
            ):
                raise ValueError("DOCUMENT_PARSER_URL은 문서 분석 서비스의 기본 주소여야 합니다.")
        if (
            api_token
            and transport == "access"
            and (
                not re.fullmatch(r"[a-z0-9-]+\.cloudflareaccess\.com", access_team)
                or not re.fullmatch(r"[a-f0-9]{64}", access_aud)
            )
        ):
            raise ValueError(
                "웹 API에는 CLOUDFLARE_ACCESS_TEAM_DOMAIN과 CLOUDFLARE_ACCESS_AUD 설정이 필요합니다."
            )
        return cls(
            token,
            Path(os.getenv("DATA_DIR", "./data")).resolve(),
            guild_id,
            max_mb,
            Path(os.getenv("ROTATION_FILE", "config/rotation.json")).resolve(),
            api_token,
            api_guild_id,
            page_url,
            os.getenv("SEMINAR_API_HOST", "127.0.0.1"),
            port,
            parser_url.rstrip("/"),
            access_team,
            access_aud,
            transport,
            relay_token,
            relay_url,
            connect_token,
        )
