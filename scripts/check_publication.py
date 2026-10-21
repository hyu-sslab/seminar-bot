"""Check the Git index without printing file contents or credentials."""

import os
import subprocess
import sys
from pathlib import Path


def git(*args: str, input_data: bytes | None = None) -> bytes:
    return subprocess.check_output(["git", *args], input=input_data)


def check_index() -> list[str]:
    entries = git("ls-files", "--stage", "-z").split(b"\0")
    paths = [entry.split(b"\t", 1)[1] for entry in entries if entry]
    result = subprocess.run(
        ["git", "check-ignore", "--no-index", "--stdin", "-z"],
        input=b"\0".join(paths) + b"\0",
        stdout=subprocess.PIPE,
        check=False,
    )
    if result.returncode not in (0, 1):
        raise RuntimeError("git check-ignore failed")
    ignored = set(result.stdout.split(b"\0"))
    errors = []
    for entry in entries:
        if not entry:
            continue
        metadata, raw_path = entry.split(b"\t", 1)
        mode, object_id, stage = metadata.split()
        path = raw_path.decode("utf-8", errors="backslashreplace")
        if raw_path in ignored:
            errors.append(f"{path}: ignored/private file is tracked")
        if mode != b"100644" and mode != b"100755":
            errors.append(f"{path}: symlink, submodule, or unsupported entry")
            continue
        if stage != b"0":
            errors.append(f"{path}: unresolved merge entry")
            continue
        size = int(git("cat-file", "-s", object_id.decode()))
        if size > 2 * 1024 * 1024:
            errors.append(f"{path}: exceeds the public source size limit (2 MiB)")
            continue
        content = git("cat-file", "blob", object_id.decode())
        try:
            content.decode("utf-8")
        except UnicodeDecodeError:
            errors.append(f"{path}: binary files are not allowed in public source")
        if b"\0" in content:
            errors.append(f"{path}: binary files are not allowed in public source")
        if path == ".env.example":
            expected = {
                b"DISCORD_TOKEN": b"replace-with-your-bot-token",
                b"DISCORD_GUILD_ID": b"",
                b"DATA_DIR": b"./data",
                b"MAX_FILE_MB": b"20",
                b"LOG_LEVEL": b"INFO",
                b"SEMINAR_API_TOKEN": b"",
                b"SEMINAR_GUILD_ID": b"",
                b"SEMINAR_PAGE_URL": b"",
                b"SEMINAR_API_TRANSPORT": b"access",
                b"SEMINAR_RELAY_TOKEN": b"",
                b"CLOUDFLARE_ACCESS_TEAM_DOMAIN": b"",
                b"CLOUDFLARE_ACCESS_AUD": b"",
                b"CLOUDFLARE_TUNNEL_TOKEN": b"",
            }
            settings = [
                line.strip()
                for line in content.splitlines()
                if line.strip() and not line.lstrip().startswith(b"#")
            ]
            actual = dict(line.split(b"=", 1) for line in settings if b"=" in line)
            if len(settings) != len(expected) or actual != expected:
                errors.append(f"{path}: must contain only the approved example values")
    return errors


def main() -> int:
    root = git("rev-parse", "--show-toplevel").decode().strip()
    os.chdir(Path(root))
    errors = check_index()
    if errors:
        print("Publication check failed:", file=sys.stderr)
        for error in errors:
            print(f"- {error}", file=sys.stderr)
        return 1
    print("Publication check passed: no private paths or binary files in the Git index.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
