from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from seminar_bot.settings import Settings


class WebSettingsTests(unittest.TestCase):
    def setUp(self):
        # No test reads a developer's .env or inherits production credentials.
        self.addCleanup(patch.stopall)
        patch.dict(os.environ, {"DISCORD_TOKEN": "discord-test-value"}, clear=True).start()
        self.dotenv = patch("seminar_bot.settings.load_dotenv", return_value=False).start()

    def web_environment(self) -> dict[str, str]:
        return {
            "SEMINAR_API_TOKEN": "a" * 64,
            "SEMINAR_GUILD_ID": "123",
            "SEMINAR_PAGE_URL": "https://example.com/internal/seminars/",
            "DOCUMENT_PARSER_URL": "http://document-parser:8081",
            "CLOUDFLARE_ACCESS_TEAM_DOMAIN": "example.cloudflareaccess.com",
            "CLOUDFLARE_ACCESS_AUD": "b" * 64,
        }

    def test_web_is_disabled_by_default(self):
        settings = Settings.from_env()
        self.assertEqual(settings.api_token, "")
        self.assertIsNone(settings.api_guild_id)
        self.assertEqual(settings.seminar_page_url, "")
        self.assertEqual(settings.api_host, "127.0.0.1")
        self.assertEqual(settings.api_port, 8080)
        self.assertEqual(settings.submission_url(12), "")
        self.dotenv.assert_called_once_with()

    def test_complete_web_configuration_is_loaded(self):
        with patch.dict(os.environ, self.web_environment()):
            settings = Settings.from_env()
        self.assertEqual(settings.api_guild_id, 123)
        self.assertEqual(settings.parser_url, "http://document-parser:8081")
        self.assertEqual(settings.access_team_domain, "example.cloudflareaccess.com")
        self.assertEqual(settings.access_audience, "b" * 64)
        self.assertEqual(
            settings.submission_url(12), "https://example.com/internal/seminars/?session=12"
        )
        self.assertNotIn(settings.token, repr(settings))
        self.assertNotIn(settings.api_token, repr(settings))

    def test_private_vpc_configuration_needs_no_public_domain_or_access_application(self):
        env = {
            **self.web_environment(),
            "SEMINAR_API_TRANSPORT": "vpc",
            "SEMINAR_RELAY_TOKEN": "c" * 64,
            "CLOUDFLARE_ACCESS_TEAM_DOMAIN": "",
            "CLOUDFLARE_ACCESS_AUD": "",
        }
        with patch.dict(os.environ, env):
            settings = Settings.from_env()
        self.assertEqual(settings.api_transport, "vpc")
        self.assertEqual(settings.relay_token, "c" * 64)
        self.assertNotIn(settings.relay_token, repr(settings))

    def test_private_vpc_requires_a_separate_strong_relay_secret(self):
        for relay in ("", "short", "c" * 257, "contains spaces" * 5, "a" * 64):
            with self.subTest(length=len(relay)):
                env = {
                    **self.web_environment(),
                    "SEMINAR_API_TRANSPORT": "vpc",
                    "SEMINAR_RELAY_TOKEN": relay,
                }
                with (
                    patch.dict(os.environ, env),
                    self.assertRaisesRegex(ValueError, "SEMINAR_RELAY_TOKEN"),
                ):
                    Settings.from_env()

    def test_outbound_relay_requires_a_separate_secret_and_wss_destination(self):
        env = {
            **self.web_environment(),
            "SEMINAR_API_TRANSPORT": "relay",
            "SEMINAR_RELAY_TOKEN": "c" * 64,
            "SEMINAR_RELAY_URL": "wss://seminar-relay.example.workers.dev/connect",
            "SEMINAR_CONNECT_TOKEN": "d" * 64,
            "CLOUDFLARE_ACCESS_TEAM_DOMAIN": "",
            "CLOUDFLARE_ACCESS_AUD": "",
        }
        with patch.dict(os.environ, env):
            settings = Settings.from_env()
        self.assertEqual(settings.api_transport, "relay")
        self.assertEqual(settings.relay_url, env["SEMINAR_RELAY_URL"])
        self.assertNotIn(settings.connect_token, repr(settings))
        for key in ("SEMINAR_RELAY_TOKEN", "SEMINAR_RELAY_URL", "SEMINAR_CONNECT_TOKEN"):
            with self.subTest(missing=key):
                with (
                    patch.dict(os.environ, {**env, key: ""}),
                    self.assertRaisesRegex(ValueError, key),
                ):
                    Settings.from_env()
        for reused in (env["SEMINAR_API_TOKEN"], env["SEMINAR_RELAY_TOKEN"]):
            with self.subTest(reused_connector_secret=True):
                with patch.dict(os.environ, {**env, "SEMINAR_CONNECT_TOKEN": reused}):
                    with self.assertRaisesRegex(ValueError, "SEMINAR_CONNECT_TOKEN"):
                        Settings.from_env()

    def test_relay_destination_rejects_credentials_other_hosts_ports_and_routes(self):
        env = {
            **self.web_environment(),
            "SEMINAR_API_TRANSPORT": "relay",
            "SEMINAR_RELAY_TOKEN": "c" * 64,
        }
        for destination in (
            "http://seminar-relay.example.workers.dev/connect",
            "ws://seminar-relay.example.workers.dev/connect",
            "wss://127.0.0.1/connect",
            "wss://example.com/connect",
            "wss://example.workers.dev/connect",
            "wss://seminar-relay.example.workers.dev.evil.test/connect",
            "wss://user@seminar-relay.example.workers.dev/connect",
            "wss://seminar-relay.example.workers.dev:8443/connect",
            "wss://seminar-relay.example.workers.dev:invalid/connect",
            "wss://seminar-relay.example.workers.dev/other",
            "wss://seminar-relay.example.workers.dev/connect?secret=hidden",
            "wss://seminar-relay.example.workers.dev/connect#fragment",
            "wss://seminar-relay.example.workers.dev/con nect",
        ):
            with self.subTest(destination=destination):
                with patch.dict(os.environ, {**env, "SEMINAR_RELAY_URL": destination}):
                    with self.assertRaisesRegex(ValueError, "SEMINAR_RELAY_URL"):
                        Settings.from_env()

    def test_unknown_transport_never_disables_server_authentication(self):
        for transport in ("", "none", "private", "VPC"):
            with self.subTest(transport=transport):
                with (
                    patch.dict(
                        os.environ, {**self.web_environment(), "SEMINAR_API_TRANSPORT": transport}
                    ),
                    self.assertRaisesRegex(ValueError, "SEMINAR_API_TRANSPORT"),
                ):
                    Settings.from_env()

    def test_invalid_api_credentials_are_rejected(self):
        for value in ("a" * 31, "a" * 257, "a" * 32 + " space", "한" * 64):
            with self.subTest(value_length=len(value)):
                env = {**self.web_environment(), "SEMINAR_API_TOKEN": value}
                with (
                    patch.dict(os.environ, env),
                    self.assertRaisesRegex(ValueError, "SEMINAR_API_TOKEN"),
                ):
                    Settings.from_env()

    def test_web_requires_a_server_and_submission_page(self):
        for missing in ("SEMINAR_GUILD_ID", "SEMINAR_PAGE_URL"):
            with self.subTest(missing=missing):
                env = {**self.web_environment(), missing: ""}
                with patch.dict(os.environ, env), self.assertRaises(ValueError):
                    Settings.from_env()

    def test_web_server_id_must_fit_a_positive_sqlite_integer(self):
        for value in ("0", "-1", str(2**63), "not-an-id"):
            with self.subTest(value=value):
                env = {**self.web_environment(), "SEMINAR_GUILD_ID": value}
                with patch.dict(os.environ, env), self.assertRaises(ValueError):
                    Settings.from_env()

    def test_submission_page_must_be_an_unambiguous_https_url(self):
        for value in (
            "http://example.com/internal/seminars/",
            "https:///internal/seminars/",
            "https://account@example.com/internal/seminars/",
            "https://example.com/internal/seminars/?session=12",
            "https://example.com/internal/seminars/#files",
            "https://example.com/internal/ seminars/",
        ):
            with self.subTest(value=value):
                env = {**self.web_environment(), "SEMINAR_PAGE_URL": value}
                with (
                    patch.dict(os.environ, env),
                    self.assertRaisesRegex(ValueError, "SEMINAR_PAGE_URL"),
                ):
                    Settings.from_env()

    def test_web_requires_an_isolated_document_parser(self):
        env = {**self.web_environment(), "DOCUMENT_PARSER_URL": ""}
        with patch.dict(os.environ, env), self.assertRaisesRegex(ValueError, "DOCUMENT_PARSER_URL"):
            Settings.from_env()

    def test_parser_configuration_cannot_contain_credentials_or_route(self):
        for value in (
            "file:///tmp/parser",
            "http://account@document-parser:8081",
            "http://document-parser:8081/extract",
            "http://document-parser:8081?key=value",
            "http://document-parser:8081#fragment",
        ):
            with self.subTest(value=value):
                env = {**self.web_environment(), "DOCUMENT_PARSER_URL": value}
                with (
                    patch.dict(os.environ, env),
                    self.assertRaisesRegex(ValueError, "DOCUMENT_PARSER_URL"),
                ):
                    Settings.from_env()

    def test_web_requires_access_team_and_application_audience(self):
        for missing in ("CLOUDFLARE_ACCESS_TEAM_DOMAIN", "CLOUDFLARE_ACCESS_AUD"):
            with self.subTest(missing=missing):
                env = {**self.web_environment(), missing: ""}
                with (
                    patch.dict(os.environ, env),
                    self.assertRaisesRegex(ValueError, "CLOUDFLARE_ACCESS"),
                ):
                    Settings.from_env()

    def test_access_team_and_audience_have_the_expected_format(self):
        invalid = (
            ("CLOUDFLARE_ACCESS_TEAM_DOMAIN", "https://example.cloudflareaccess.com"),
            ("CLOUDFLARE_ACCESS_TEAM_DOMAIN", "example.cloudflareaccess.com/path"),
            ("CLOUDFLARE_ACCESS_TEAM_DOMAIN", "example.com"),
            ("CLOUDFLARE_ACCESS_AUD", "b" * 63),
            ("CLOUDFLARE_ACCESS_AUD", "g" * 64),
        )
        for key, value in invalid:
            with self.subTest(key=key, value=value):
                env = {**self.web_environment(), key: value}
                with (
                    patch.dict(os.environ, env),
                    self.assertRaisesRegex(ValueError, "CLOUDFLARE_ACCESS"),
                ):
                    Settings.from_env()

    def test_custom_api_listener_and_parser_trailing_slash(self):
        env = {
            **self.web_environment(),
            "SEMINAR_API_HOST": "0.0.0.0",
            "SEMINAR_API_PORT": "8088",
            "DOCUMENT_PARSER_URL": "http://document-parser:8081/",
        }
        with patch.dict(os.environ, env):
            settings = Settings.from_env()
        self.assertEqual(settings.api_host, "0.0.0.0")
        self.assertEqual(settings.api_port, 8088)
        self.assertEqual(settings.parser_url, "http://document-parser:8081")

    def test_api_port_must_be_valid(self):
        for value in ("0", "65536", "invalid"):
            with self.subTest(value=value):
                env = {**self.web_environment(), "SEMINAR_API_PORT": value}
                with patch.dict(os.environ, env), self.assertRaises(ValueError):
                    Settings.from_env()


if __name__ == "__main__":
    unittest.main()
