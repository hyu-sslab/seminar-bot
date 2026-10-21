"""Short-lived document parser. Invoked by the bot with an execution deadline."""

from __future__ import annotations

import json
import sys
from dataclasses import asdict
from pathlib import Path


def main() -> None:
    try:
        import resource

        resource.setrlimit(resource.RLIMIT_AS, (512 * 1024 * 1024, 512 * 1024 * 1024))
        resource.setrlimit(resource.RLIMIT_CPU, (15, 15))
    except ImportError:
        pass  # Windows still has the parent's wall-clock deadline and size limits.

    from .documents import extract_document

    kind = sys.argv[3] if len(sys.argv) > 3 else "paper"
    result = extract_document(Path(sys.argv[1]).read_bytes(), sys.argv[2], kind=kind)
    print(json.dumps(asdict(result), ensure_ascii=False))


if __name__ == "__main__":
    main()
