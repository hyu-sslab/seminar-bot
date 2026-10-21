from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import sys
from pathlib import Path

from .documents import DocumentInfo

log = logging.getLogger(__name__)


async def extract_saved(
    path: Path, filename: str, data: bytes, timeout: float = 25
) -> DocumentInfo:
    """Keep PDF/ZIP parsing and any runaway parser away from the Discord event loop."""
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "seminar_bot.extract_worker",
        str(path),
        filename,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
        env={
            key: value
            for key, value in os.environ.items()
            if key in {"PATH", "LANG", "LC_ALL", "SYSTEMROOT"}
        },
    )
    try:
        stdout, _ = await asyncio.wait_for(process.communicate(), timeout=timeout)
        if process.returncode != 0:
            raise ValueError(f"Parser exited with code {process.returncode}")
        return DocumentInfo(**json.loads(stdout))
    except asyncio.CancelledError:
        if process.returncode is None:
            process.kill()
        await process.wait()
        raise
    except (TimeoutError, ValueError, TypeError):
        if process.returncode is None:
            process.kill()
        await process.wait()
        log.warning("Document extraction failed or exceeded deadline; using filename")
        return DocumentInfo(
            title=Path(filename.replace("\\", "/")).stem[:500] or "이름 없는 문서",
            text="",
            doi=None,
            arxiv_id=None,
            sha256=hashlib.sha256(data).hexdigest(),
            extraction_method="filename",
            warning="문서 분석에 실패하거나 시간이 초과되어 파일명으로 기록했습니다.",
        )
