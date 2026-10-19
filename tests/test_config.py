import tempfile
import tomllib
import unittest
from pathlib import Path

from coldstart import config


class ConfigTests(unittest.TestCase):
    def write(self, text: str) -> str:
        d = Path(tempfile.mkdtemp(prefix="coldstart-cfg-"))
        self.addCleanup(lambda: [p.unlink() for p in d.iterdir()] and d.rmdir())
        p = d / "c.toml"
        p.write_text(text)
        return str(p)

    def test_defaults_without_file(self):
        c = config.load(None, env={})
        self.assertEqual(c.console.host, "192.168.128.150")
        self.assertEqual(c.botbase.port, 6000)
        self.assertEqual(c.rig.port, 5600)
        self.assertEqual(c.supervise.silent_s, 60.0)
        self.assertEqual(c.supervise.attempts, 3)
        self.assertEqual(c.launch.tile, 0)
        self.assertEqual(c.launch.dleft_presses, 12)
        self.assertEqual(c.twili.hbl_title_id, "01007ef00011e000")
        self.assertEqual(c.notify.webhook, "")
        self.assertIsNone(c.source)

    def test_album_root_auto_or_absolute(self):
        self.assertEqual(config.load(None, env={}).screenshot.album_root, "auto")
        self.assertFalse(config.load(None, env={}).launch.blind_second_a)
        p = self.write('[screenshot]\nalbum_root = "/emuMMC/RAW1/Nintendo/Album"\n')
        self.assertEqual(config.load(p, env={}).screenshot.album_root, "/emuMMC/RAW1/Nintendo/Album")
        with self.assertRaisesRegex(config.ConfigError, "album_root"):
            config.load(self.write('[screenshot]\nalbum_root = "Nintendo/Album"\n'), env={})
        with self.assertRaisesRegex(config.ConfigError, "ambiguity"):
            config.load(self.write('[classify]\nambiguity_factor = -1\n'), env={})

    def test_file_overrides_and_int_to_float(self):
        p = self.write('[launch]\ntile = 2\nwait_rig_s = 30\n[classify]\ngrid = [8, 4]\n')
        c = config.load(p, env={})
        self.assertEqual(c.launch.tile, 2)
        self.assertIsInstance(c.launch.wait_rig_s, float)
        self.assertEqual(c.classify.grid, [8, 4])
        self.assertEqual(c.base_dir, Path(p).parent)
        self.assertEqual(c.path("refs"), Path(p).parent / "refs")

    def test_unknown_key_and_section_are_errors(self):
        with self.assertRaisesRegex(config.ConfigError, "unknown key launch.tiles"):
            config.load(self.write("[launch]\ntiles = 1\n"), env={})
        with self.assertRaisesRegex(config.ConfigError, r"unknown section \[lunch\]"):
            config.load(self.write("[lunch]\ntile = 1\n"), env={})

    def test_type_errors(self):
        with self.assertRaisesRegex(config.ConfigError, "launch.tile: expected an integer"):
            config.load(self.write('[launch]\ntile = "0"\n'), env={})
        with self.assertRaisesRegex(config.ConfigError, "console.icmp: expected true/false"):
            config.load(self.write("[console]\nicmp = 1\n"), env={})
        with self.assertRaisesRegex(config.ConfigError, "classify.crop"):
            config.load(self.write("[classify]\ncrop = [0.5, 0.0, 0.4, 1.0]\n"), env={})
        with self.assertRaisesRegex(config.ConfigError, "hbl_title_id"):
            config.load(self.write('[twili]\nhbl_title_id = "xyz"\n'), env={})
        with self.assertRaisesRegex(config.ConfigError, "not found"):
            config.load("/nonexistent/c.toml", env={})

    def test_env_overrides(self):
        p = self.write('[console]\nhost = "10.0.0.1"\n')
        env = {"SWITCH_IP": "10.0.0.2", "COLDSTART_BOTBASE_PORT": "6001", "COLDSTART_RIG_PORT": "0x15e1"}
        c = config.load(p, env=env)
        self.assertEqual(c.console.host, "10.0.0.2")
        self.assertEqual(c.botbase.port, 6001)
        self.assertEqual(c.rig.port, 0x15E1)
        env["COLDSTART_HOST"] = "10.0.0.3"   # wins over SWITCH_IP
        self.assertEqual(config.load(p, env=env).console.host, "10.0.0.3")
        with self.assertRaisesRegex(config.ConfigError, "COLDSTART_FTP_PORT"):
            config.load(p, env={"COLDSTART_FTP_PORT": "ftp"})
        with self.assertRaisesRegex(config.ConfigError, "not a port"):
            config.load(p, env={"COLDSTART_TWILI_PORT": "70000"})

    def test_config_from_env_variable(self):
        p = self.write("[launch]\ntile = 4\n")
        self.assertEqual(config.load(None, env={"COLDSTART_CONFIG": p}).launch.tile, 4)

    def test_dump_is_valid_toml_with_same_values(self):
        c = config.load(None, env={"COLDSTART_HOST": "1.2.3.4"})
        text = c.dump()
        self.assertIn("COLDSTART_HOST=1.2.3.4", text)
        self.assertEqual(tomllib.loads(text), c.data)

    def test_example_config_is_valid(self):
        example = Path(__file__).resolve().parent.parent / "coldstart.example.toml"
        c = config.load(str(example), env={})
        self.assertEqual(c.data, config.DEFAULTS)


if __name__ == "__main__":
    unittest.main()
