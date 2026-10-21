from __future__ import annotations

import re
import unittest
from pathlib import Path

from seminar_bot.bot import MAX_CONCURRENT_UPLOADS
from seminar_bot.documents import MAX_FILE_BYTES
from seminar_bot.parser_service import MAX_CONCURRENT_PARSERS
from seminar_bot.relay import CONNECTION_SLOTS


class ResourceBudgetTests(unittest.TestCase):
    def test_container_memory_and_tmpfs_cover_bounded_concurrent_documents(self):
        compose = (Path(__file__).parents[1] / "compose.yaml").read_text()
        bot, parser = compose.split("  document-parser:\n", 1)
        parser = parser.split("  cloudflared:\n", 1)[0]
        bot_tmp = int(re.search(r"/tmp:[^\n]*size=(\d+)", bot)[1])
        parser_tmp = int(re.search(r"/tmp:[^\n]*size=(\d+)", parser)[1])
        parser_memory_mib = int(
            re.search(r"mem_limit: \$\{DOCUMENT_PARSER_MEMORY_LIMIT:-(\d+)m\}", parser)[1]
        )
        self.assertEqual(MAX_CONCURRENT_UPLOADS, 4)
        self.assertEqual(MAX_CONCURRENT_PARSERS, 4)
        self.assertGreaterEqual(CONNECTION_SLOTS - MAX_CONCURRENT_UPLOADS, 2)
        # Relay uploads and API paper uploads may hold separate copies. Even
        # six occupied connectors must leave room for four API paper files.
        self.assertGreaterEqual(
            bot_tmp, (CONNECTION_SLOTS + MAX_CONCURRENT_UPLOADS) * MAX_FILE_BYTES
        )
        self.assertGreaterEqual(parser_tmp, MAX_CONCURRENT_PARSERS * MAX_FILE_BYTES)
        # Each worker's address-space ceiling is 512 MiB. Include the full
        # tmpfs allowance plus 128 MiB for the service, pipes and runtime.
        self.assertGreaterEqual(
            parser_memory_mib, MAX_CONCURRENT_PARSERS * 512 + parser_tmp // (1024 * 1024) + 128
        )
        self.assertIn("pids_limit: 64", parser)
        self.assertIn("cpus: ${DOCUMENT_PARSER_CPUS:-0}", parser)
