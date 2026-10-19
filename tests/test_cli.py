"""CLI-level checks that need no simulator."""
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


class CliInstall(unittest.TestCase):
    def test_cli_install_is_dry_without_yes(self):
        r = subprocess.run([sys.executable, "-m", "coldstart", "install", "--release", "--boot"],
                           cwd=REPO, capture_output=True, text=True, timeout=30,
                           env={"PATH": "/usr/bin:/bin", "COLDSTART_HOST": "192.0.2.9",
                                "PYTHONPATH": str(REPO)})
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("dry run: nothing was sent", r.stdout)


class PillowMissingTests(unittest.TestCase):
    """The tool starts and explains itself when Pillow cannot be imported."""

    def run_without_pil(self, *args):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "PIL").mkdir()
            (Path(d) / "PIL" / "__init__.py").write_text("raise ImportError('no PIL here')\n")
            jpg = Path(d) / "x.jpg"
            jpg.write_bytes(b"\xff\xd8\xff\xd9")
            args = [a.replace("{jpg}", str(jpg)) for a in args]
            return subprocess.run([sys.executable, "-m", "coldstart", *args], cwd=d,
                                  capture_output=True, text=True, timeout=30,
                                  env={"PATH": "/usr/bin:/bin", "PYTHONPATH": f"{d}:{REPO}",
                                       "COLDSTART_HOST": "192.0.2.9"})

    def test_help_and_dry_run_work(self):
        self.assertEqual(self.run_without_pil("--help").returncode, 0)
        r = self.run_without_pil("launch", "--dry-run")
        self.assertEqual(r.returncode, 0, r.stderr)
        r = self.run_without_pil("status", "--no-probe")
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_classify_says_how_to_install(self):
        r = self.run_without_pil("shot", "--file", "{jpg}")
        self.assertEqual(r.returncode, 2)
        self.assertIn("pip install --user pillow", r.stderr)
        r = self.run_without_pil("learn", "home", "--file", "{jpg}")
        self.assertEqual(r.returncode, 2)
        self.assertIn("Pillow is not installed", r.stderr)


if __name__ == "__main__":
    unittest.main()
