"""CI checks for the parser's actual Docker isolation and PDF roundtrip.

The inspect mode runs on the CI host using only the standard library. Client
mode runs inside the bot image and exercises the separate parser container.
No Discord connection, deployment credentials, or private files are needed.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import io
import json
import re
import subprocess
import sys
from pathlib import Path


class ContainerCheckFailed(RuntimeError):
    """An isolation assertion failed; messages never include container secrets."""


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ContainerCheckFailed(message)


def _docker(*arguments: str) -> str:
    try:
        result = subprocess.run(
            ["docker", *arguments],
            check=True,
            capture_output=True,
            text=True,
            timeout=20,
        )
    except (OSError, subprocess.SubprocessError):
        raise ContainerCheckFailed("Docker inspection or runtime probe failed.") from None
    return result.stdout


def _inspect_parser(project: str) -> None:
    container_id = _docker("compose", "-p", project, "ps", "-q", "document-parser").strip()
    _require(
        bool(container_id) and "\n" not in container_id, "Expected one running parser container."
    )
    try:
        container = json.loads(_docker("inspect", container_id))[0]
        config = container["Config"]
        host = container["HostConfig"]
        network_settings = container["NetworkSettings"]
    except (ValueError, IndexError, KeyError, TypeError):
        raise ContainerCheckFailed("Docker returned invalid container metadata.") from None

    user = config.get("User", "").split(":", 1)[0]
    _require(
        bool(user) and user != "root" and not (user.isdigit() and int(user) == 0),
        "Parser image must use a nonroot user.",
    )
    _require(host.get("ReadonlyRootfs") is True, "Parser root filesystem must be read-only.")
    _require(host.get("Init") is True, "Parser must use an init process to reap children.")
    _require(
        "ALL" in {cap.upper() for cap in host.get("CapDrop") or []},
        "Parser must drop all Linux capabilities.",
    )
    _require(
        any(
            option in {"no-new-privileges", "no-new-privileges:true", "no-new-privileges=true"}
            for option in host.get("SecurityOpt") or []
        ),
        "Parser must prohibit new privileges.",
    )
    _require(
        not host.get("PortBindings") and not host.get("PublishAllPorts"),
        "Parser ports must not be published.",
    )
    _require(
        not any((network_settings.get("Ports") or {}).values()),
        "Parser has published network ports.",
    )
    _require(not config.get("Volumes"), "Parser image must not declare persistent volumes.")
    _require(
        all(mount.get("Type") == "tmpfs" for mount in container.get("Mounts") or []),
        "Parser must not mount NAS, bind, or persistent volumes.",
    )

    temporary = (host.get("Tmpfs") or {}).get("/tmp", "")
    _require(
        all(flag in temporary.split(",") for flag in ["noexec", "nosuid", "nodev"]),
        "Parser temporary storage needs noexec,nosuid,nodev.",
    )
    size_option = next(
        (option[5:] for option in temporary.split(",") if option.startswith("size=")), ""
    )
    size = re.fullmatch(r"([0-9]+)([kmg]?)(?:b)?", size_option, re.IGNORECASE)
    _require(size is not None, "Parser temporary storage must have a finite size limit.")
    size_bytes = int(size[1]) * {"": 1, "k": 1024, "m": 1024**2, "g": 1024**3}[size[2].lower()]
    _require(
        0 < size_bytes <= 96 * 1024 * 1024,
        "Parser temporary storage must be limited to at most 96 MiB.",
    )
    _require(
        0 < host.get("Memory", 0) <= 768 * 1024 * 1024,
        "Parser memory must be limited to at most 768 MiB.",
    )
    _require(0 < host.get("PidsLimit", 0) <= 64, "Parser must limit process count to at most 64.")
    _require(
        0 < host.get("NanoCpus", 0) <= 1_000_000_000,
        "Parser CPU must be limited to at most one core.",
    )

    names = [entry.split("=", 1)[0].upper() for entry in config.get("Env") or []]
    _require(
        not any(
            name == "DISCORD_TOKEN"
            or name == "SEMINAR_API_TOKEN"
            or any(
                marker in name
                for marker in ["TOKEN", "SECRET", "PASSWORD", "CREDENTIAL", "API_KEY"]
            )
            for name in names
        ),
        "Parser environment must not contain service credentials.",
    )
    networks = network_settings.get("Networks") or {}
    _require(bool(networks), "Parser must join its isolated internal network.")
    for network in networks.values():
        try:
            details = json.loads(_docker("network", "inspect", network["NetworkID"]))[0]
        except (ValueError, KeyError, TypeError, IndexError):
            raise ContainerCheckFailed("Docker returned invalid network metadata.") from None
        _require(details.get("Internal") is True, "Parser may only join internal Docker networks.")

    # These checks run as the image's configured user. They print no environment
    # values, inspect JSON, local paths, addresses, or uploaded document content.
    _docker(
        "exec",
        container_id,
        "python",
        "-c",
        """
import os
from pathlib import Path
if os.geteuid() == 0:
    raise SystemExit('Parser unexpectedly runs as root')
try:
    Path('/app/container-ci-write-probe').write_text('probe')
except OSError:
    pass
else:
    raise SystemExit('Parser root filesystem unexpectedly accepted a write')
temporary = Path('/tmp/container-ci-write-probe')
temporary.write_bytes(b'probe')
temporary.unlink()
""",
    )
    _docker(
        "exec",
        container_id,
        "python",
        "-c",
        """
import socket
try:
    connection = socket.create_connection(('1.1.1.1', 443), timeout=2)
except OSError:
    pass
else:
    connection.close()
    raise SystemExit('Parser unexpectedly has outbound Internet connectivity')
""",
    )
    print("Parser isolation checks passed.")


async def _client_roundtrip(parser_url: str) -> None:
    from pypdf import PdfWriter

    from seminar_bot.remote_parser import ParserUnavailable, extract_remote

    title = "Isolated Container PDF Roundtrip"
    writer = PdfWriter()
    writer.add_blank_page(width=612, height=792)
    writer.add_metadata({"/Title": title})
    output = io.BytesIO()
    writer.write(output)
    data = output.getvalue()
    digest = hashlib.sha256(data).hexdigest()
    document = None
    # Compose starts the parser first; tolerate its brief application startup.
    # A complete failure still ends within a finite CI deadline.
    async with asyncio.timeout(45):
        for attempt in range(10):
            try:
                document = await extract_remote(
                    Path("/tmp/container-ci-unused.pdf"), "container-check.pdf", data, parser_url
                )
                break
            except ParserUnavailable:
                if attempt == 9:
                    raise ContainerCheckFailed("Isolated parser PDF roundtrip failed.") from None
                await asyncio.sleep(0.5)
    _require(document is not None, "Parser returned no document.")
    _require(document.title == title, "Parser did not extract the PDF metadata title.")
    _require(document.sha256 == digest, "Parser returned a different file checksum.")
    _require(
        document.extraction_method == "pdf_metadata", "PDF was not parsed by the isolated worker."
    )
    print("Isolated parser PDF roundtrip passed.")


def main() -> int:
    arguments = argparse.ArgumentParser(description=__doc__)
    commands = arguments.add_subparsers(dest="mode", required=True)
    inspect = commands.add_parser(
        "inspect", help="Verify Docker container isolation on the CI host"
    )
    inspect.add_argument("--project", default="seminar-ci")
    client = commands.add_parser("client", help="Exercise the parser from inside the bot image")
    client.add_argument("--parser-url", default="http://document-parser:8081")
    args = arguments.parse_args()
    try:
        if args.mode == "inspect":
            _inspect_parser(args.project)
        else:
            asyncio.run(_client_roundtrip(args.parser_url))
    except ContainerCheckFailed as error:
        print(str(error), file=sys.stderr)
        return 1
    except Exception:
        # Untrusted response/network exceptions must not reveal configuration.
        print("Container validation could not be completed.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
