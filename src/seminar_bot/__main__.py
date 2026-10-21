from __future__ import annotations

import logging
import os
import sys
from contextlib import contextmanager
from pathlib import Path

from .bot import SeminarBot
from .settings import Settings


@contextmanager
def instance_lock(directory: Path):
    """Prevent concurrent processes sharing an archive and sending duplicate reminders."""
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / "bot.lock").open("a+b") as stream:
        try:
            if os.name == "nt":
                import msvcrt

                stream.write(b"0")
                stream.flush()
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise ValueError("같은 DATA_DIR을 사용하는 봇이 이미 실행 중입니다.") from exc
        yield


def main() -> None:
    try:
        settings = Settings.from_env()
        logging.basicConfig(
            level=os.getenv("LOG_LEVEL", "INFO").upper(),
            format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        )
        with instance_lock(settings.data_dir):
            bot = SeminarBot(settings)
            bot.run(settings.token, log_handler=None)
    except (ValueError, KeyboardInterrupt) as exc:
        if isinstance(exc, ValueError):
            print(str(exc), file=sys.stderr)
            raise SystemExit(2) from None


if __name__ == "__main__":
    main()
