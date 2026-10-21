"""Exercise publication guards against a real temporary Git index."""

import json
import os
import secrets
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
SCRIPT = PROJECT / "scripts" / "check_publication.py"


class PublicationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.git("init", "-q")
        shutil.copyfile(PROJECT / ".gitignore", self.root / ".gitignore")
        shutil.copyfile(PROJECT / ".env.example", self.root / ".env.example")
        self.git("add", ".gitignore", ".env.example")

    def git(self, *args):
        return subprocess.run(["git", *args], cwd=self.root, check=True, capture_output=True)

    def check(self):
        return subprocess.run(
            [sys.executable, str(SCRIPT)], cwd=self.root, capture_output=True, text=True
        )

    def test_source_and_example_are_allowed(self):
        (self.root / "bot.py").write_text("print('ok')\n")
        self.git("add", "bot.py")
        self.assertEqual(self.check().returncode, 0)

    def test_ignored_files_cannot_be_force_added(self):
        for name in (
            ".env",
            ".env.production",
            "secret.KEY",
            "paper.PDF",
            "slides.pptx",
            "seminar.sqlite3-wal",
            "backup.tar.gz",
            "compose.override.yaml",
            "uploads/result.txt",
            "nested/.env.example",
            "config/rotation.json",
            "config/rotation.json.tmp",
        ):
            with self.subTest(name=name):
                path = self.root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("private contents")
                self.git("add", "-f", name)
                result = self.check()
                self.assertEqual(result.returncode, 1)
                self.assertIn(name, result.stderr)
                self.assertNotIn("private contents", result.stderr)
                self.git("rm", "--cached", "-f", name)

    def test_changed_example_is_rejected_even_when_worktree_is_cleaned(self):
        path = self.root / ".env.example"
        original = path.read_bytes()
        path.write_bytes(original.replace(b"replace-with-your-bot-token", b"actual-secret"))
        self.git("add", ".env.example")
        path.write_bytes(original)
        result = self.check()
        self.assertEqual(result.returncode, 1)
        self.assertNotIn("actual-secret", result.stderr)

    def test_unstaged_changes_are_not_read(self):
        path = self.root / ".env.example"
        path.write_bytes(path.read_bytes().replace(b"replace-with-your-bot-token", b"private"))
        self.assertEqual(self.check().returncode, 0)

    def test_duplicate_example_keys_are_rejected(self):
        path = self.root / ".env.example"
        path.write_bytes(path.read_bytes().replace(b"LOG_LEVEL=INFO", b"MAX_FILE_MB=20"))
        self.git("add", ".env.example")
        self.assertEqual(self.check().returncode, 1)

    def test_binary_with_unexpected_extension_is_rejected(self):
        (self.root / "document.dat").write_bytes(b"binary\x00data")
        self.git("add", "document.dat")
        self.assertEqual(self.check().returncode, 1)

    def test_large_text_is_rejected(self):
        (self.root / "large.txt").write_bytes(b"a" * (2 * 1024 * 1024 + 1))
        self.git("add", "large.txt")
        self.assertEqual(self.check().returncode, 1)

    @unittest.skipUnless(hasattr(os, "symlink"), "requires symlinks")
    def test_symlink_is_rejected(self):
        (self.root / "link").symlink_to(".env")
        self.git("add", "link")
        self.assertEqual(self.check().returncode, 1)


class GitleaksTests(unittest.TestCase):
    def test_custom_rules_detect_synthetic_examples(self):
        scanner = shutil.which("gitleaks")
        if not scanner:
            candidate = Path.home() / ".local" / "bin" / "gitleaks"
            scanner = str(candidate) if candidate.is_file() else None
        if not scanner:
            self.skipTest("Gitleaks is not installed")
        # These randomized examples are invalid demonstration credentials.
        examples = {
            "discord-token-shape": ".".join(
                [secrets.token_urlsafe(18), "Ab1cD2", secrets.token_urlsafe(30)]
            ),
            "nas-ddns-address": "private-lab-example" + "." + "myds.me",
            "private-user-home": "/var/services/" + "homes/" + "example-user/",
        }
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for rule, value in examples.items():
                with self.subTest(rule=rule):
                    source = root / "sample.txt"
                    report = root / "report.json"
                    source.write_text(value)
                    result = subprocess.run(
                        [
                            scanner,
                            "dir",
                            "--config",
                            str(PROJECT / ".gitleaks.toml"),
                            "--redact",
                            "--no-banner",
                            "--report-format",
                            "json",
                            "--report-path",
                            str(report),
                            str(source),
                        ],
                        capture_output=True,
                    )
                    self.assertEqual(result.returncode, 1)
                    findings = json.loads(report.read_text())
                    self.assertIn(rule, [item["RuleID"] for item in findings])


if __name__ == "__main__":
    unittest.main()
