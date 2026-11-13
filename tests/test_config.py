import tempfile
import unittest
from pathlib import Path
from unittest import mock

import yaml
from pydantic import ValidationError

import config
from config import Config, local_url

# The three settings that have no default, by the names config.yaml uses.
REQUIRED = {
    "inputDevice": "Dante Virtual Soundcard",
    "outputPath": "./recordings",
    "companionBaseUrls": ["http://127.0.0.1:8001/"],
}


class ConfigTest(unittest.TestCase):
    def test_only_three_settings_are_required(self):
        settings = Config(**REQUIRED)

        self.assertEqual(settings.framework, "auto")
        self.assertEqual(settings.bitrate, "128k")
        self.assertEqual(
            (settings.listen_host, settings.listen_port), ("127.0.0.1", 8000)
        )
        self.assertIsNone(settings.server_name)
        self.assertIsNone(settings.audio_buffer_ms)

    def test_a_missing_required_setting_is_named(self):
        with self.assertRaises(ValidationError) as raised:
            Config(outputPath="./recordings", companionBaseUrls=["http://x"])
        self.assertIn("inputDevice", str(raised.exception))

    def test_an_unknown_setting_is_rejected(self):
        # What makes a typo in an optional key an error instead of a setting
        # that silently never applies. It also means a key this server has
        # stopped using has to be removed from config.yaml.
        with self.assertRaises(ValidationError) as raised:
            Config(**REQUIRED, apiVersion="0.0.1")
        self.assertIn("apiVersion", str(raised.exception))

    def test_a_single_address_instead_of_a_list_says_what_to_write(self):
        with self.assertRaises(ValidationError) as raised:
            Config(**{**REQUIRED, "companionBaseUrls": "http://127.0.0.1:8001"})
        self.assertIn(
            "companionBaseUrls:\n  - http://127.0.0.1:8001", str(raised.exception)
        )

    def test_addresses_are_tidied_and_deduplicated(self):
        settings = Config(
            **{
                **REQUIRED,
                "companionBaseUrls": [
                    "http://127.0.0.1:8001/",
                    " http://127.0.0.1:8001 ",
                    "http://10.0.0.5:8001//",
                    "",
                ],
            }
        )
        self.assertEqual(
            settings.companion_base_urls,
            ["http://127.0.0.1:8001", "http://10.0.0.5:8001"],
        )

    def test_an_empty_address_list_is_rejected(self):
        with self.assertRaises(ValidationError):
            Config(**{**REQUIRED, "companionBaseUrls": [""]})

    def test_server_name_must_fit_a_companion_variable_name(self):
        self.assertEqual(
            Config(**REQUIRED, serverName="mac_mini_1").server_name, "mac_mini_1"
        )
        for unusable in ("mac mini", "mac-1", ""):
            with self.subTest(unusable), self.assertRaises(ValidationError):
                Config(**REQUIRED, serverName=unusable)

    def test_listen_port_must_be_a_real_port(self):
        for unusable in (0, 65536):
            with self.subTest(unusable), self.assertRaises(ValidationError):
                Config(**REQUIRED, listenPort=unusable)

    def test_the_example_config_is_valid(self):
        # The template people copy, so it has to load as it stands.
        with open(config.EXAMPLE_CONFIG_PATH) as file:
            Config(**yaml.safe_load(file))


class LocalUrlTest(unittest.TestCase):
    def test_where_this_machine_reaches_the_server(self):
        cases = {
            ("127.0.0.1", 8000): "http://127.0.0.1:8000",
            # "Every address" is somewhere to listen, not somewhere to connect.
            ("0.0.0.0", 8000): "http://127.0.0.1:8000",
            ("192.168.1.20", 9000): "http://192.168.1.20:9000",
            ("::", 8000): "http://[::1]:8000",
            ("::1", 8123): "http://[::1]:8123",
        }
        for (host, port), expected in cases.items():
            with self.subTest(host=host):
                self.assertEqual(local_url(host, port), expected)

    def test_a_config_knows_its_own_local_url(self):
        settings = Config(**REQUIRED, listenHost="0.0.0.0", listenPort=9000)
        self.assertEqual(settings.local_url, "http://127.0.0.1:9000")


class AcceptRequestsFromTest(unittest.TestCase):
    """Which machines may use the API. THIS_SERVER stands for this machine."""

    THIS_SERVER = "192.168.1.20"

    def settings(self, *entries):
        return Config(**REQUIRED, acceptRequestsFrom=list(entries))

    def test_with_no_list_every_address_is_accepted(self):
        self.assertTrue(Config(**REQUIRED).allows("203.0.113.9", self.THIS_SERVER))

    def test_the_key_with_nothing_under_it_means_no_list(self):
        self.assertEqual(
            Config(**REQUIRED, acceptRequestsFrom=None).accept_requests_from, []
        )

    def test_a_listed_address_is_accepted_and_its_neighbour_is_not(self):
        settings = self.settings("192.168.1.50")

        self.assertTrue(settings.allows("192.168.1.50", self.THIS_SERVER))
        self.assertFalse(settings.allows("192.168.1.51", self.THIS_SERVER))

    def test_a_range_covers_every_address_in_it(self):
        settings = self.settings("192.168.1.0/24")

        self.assertTrue(settings.allows("192.168.1.7", self.THIS_SERVER))
        self.assertFalse(settings.allows("192.168.2.7", self.THIS_SERVER))

    def test_an_address_written_with_a_range_means_that_range(self):
        settings = self.settings("192.168.5.7/24")

        self.assertEqual(str(settings.accept_requests_from[0]), "192.168.5.0/24")
        self.assertTrue(settings.allows("192.168.5.99", self.THIS_SERVER))

    def test_this_machine_is_always_accepted(self):
        # The restart scripts and a Companion running alongside rely on it,
        # whichever of its own addresses the machine reaches itself on.
        settings = self.settings("192.168.1.50")

        for client, server in (
            ("127.0.0.1", "127.0.0.1"),
            ("::1", "::1"),
            (self.THIS_SERVER, self.THIS_SERVER),
        ):
            with self.subTest(client):
                self.assertTrue(settings.allows(client, server))

    def test_an_ipv4_caller_seen_through_ipv6_is_still_recognised(self):
        # What a server listening on "::" is told a 192.168.1.50 caller is.
        settings = self.settings("192.168.1.50")

        self.assertTrue(settings.allows("::ffff:192.168.1.50", "::"))
        self.assertFalse(settings.allows("::ffff:192.168.1.51", "::"))

    def test_ipv6_addresses_and_ranges_work_too(self):
        settings = self.settings("fd00::/8")

        self.assertTrue(settings.allows("fd12::1", "fd00::2"))
        self.assertFalse(settings.allows("2001:db8::1", "fd00::2"))

    def test_a_caller_that_cannot_be_identified_is_refused(self):
        settings = self.settings("192.168.1.50")

        for client in (None, "", "not an address"):
            with self.subTest(client):
                self.assertFalse(settings.allows(client, self.THIS_SERVER))

    def test_a_machine_name_is_rejected_with_what_to_write_instead(self):
        with self.assertRaises(ValidationError) as raised:
            self.settings("companion-pc")
        self.assertIn("192.168.1.50", str(raised.exception))
        self.assertIn("machine names are not looked up", str(raised.exception))

    def test_a_single_address_instead_of_a_list_says_what_to_write(self):
        with self.assertRaises(ValidationError) as raised:
            Config(**REQUIRED, acceptRequestsFrom="192.168.1.50")
        self.assertIn("acceptRequestsFrom:\n  - 192.168.1.50", str(raised.exception))


class LoadConfigTest(unittest.TestCase):
    def test_reads_the_file(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "config.yaml"
            path.write_text(yaml.safe_dump({**REQUIRED, "bitrate": "192k"}))
            with mock.patch.object(config, "CONFIG_PATH", path):
                self.assertEqual(config.load_config().bitrate, "192k")

    def test_a_missing_file_points_at_the_example(self):
        with (
            tempfile.TemporaryDirectory() as folder,
            mock.patch.object(config, "CONFIG_PATH", Path(folder) / "config.yaml"),
            self.assertRaises(FileNotFoundError) as raised,
        ):
            config.load_config()
        self.assertIn("config.example.yaml", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
