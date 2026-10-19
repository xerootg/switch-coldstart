import io
import json
import shutil
import tempfile
import unittest
from pathlib import Path

from PIL import Image, ImageDraw

from coldstart.classify import Classifier, distance, grid_of, label_of


def jpeg(colour, box=None, box_colour=(255, 255, 255), size=(1280, 720)):
    im = Image.new("RGB", size, colour)
    if box:
        ImageDraw.Draw(im).rectangle(box, fill=box_colour)
    b = io.BytesIO()
    im.save(b, "JPEG", quality=90)
    return b.getvalue()


class ClassifierTests(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="coldstart-refs-"))
        self.addCleanup(shutil.rmtree, self.dir)

    def clf(self, **kw):
        return Classifier(self.dir, **kw)

    def test_label_of(self):
        self.assertEqual(label_of("home"), "home")
        self.assertEqual(label_of("home-2"), "home")
        self.assertEqual(label_of("user-select"), "user-select")
        self.assertEqual(label_of("user-select-night"), "user-select")
        self.assertEqual(label_of("homebrew"), "homebrew")
        self.assertEqual(label_of("settings"), "settings")

    def test_grid_and_distance(self):
        g = grid_of(jpeg((255, 0, 0)), (4, 3))
        self.assertEqual(len(g), 12)
        self.assertAlmostEqual(g[0][0], 254, delta=3)
        self.assertAlmostEqual(distance(g, g), 0.0)
        h = grid_of(jpeg((0, 0, 0)), (4, 3))
        self.assertAlmostEqual(distance(g, h), 254, delta=4)
        with self.assertRaises(ValueError):
            distance(g, g[:3])

    def test_no_references_is_unknown(self):
        r = self.clf().classify(jpeg((1, 2, 3)))
        self.assertEqual(r.label, "unknown")
        self.assertIn("no references", r.summary())
        self.assertEqual(self.clf().classify(None).label, "unknown")

    def test_learn_and_classify_nearest(self):
        c = self.clf()
        c.learn("home", jpeg((50, 50, 60)))
        c.learn("dialog", jpeg((20, 20, 20), (300, 200, 980, 520)))
        c.learn("game", jpeg((30, 170, 60)))
        c.learn("home-2", jpeg((52, 52, 64)))
        idx = json.loads((self.dir / "index.json").read_text())
        self.assertEqual(idx["home-2"]["label"], "home")
        self.assertEqual(sorted(c.ref_names()), ["dialog", "game", "home", "home-2"])
        r = c.classify(jpeg((51, 51, 61)))
        self.assertEqual(r.label, "home")
        self.assertLess(r.distance, 5)
        self.assertEqual(len(r.ranking), 4)
        self.assertEqual(c.classify(jpeg((22, 22, 22), (300, 200, 980, 520))).label, "dialog")
        self.assertIn("nearest", c.classify(jpeg((30, 170, 60))).summary())

    def test_threshold_makes_unknown(self):
        c = self.clf(threshold=20)
        c.learn("home", jpeg((50, 50, 60)))
        r = c.classify(jpeg((240, 140, 0)))
        self.assertEqual(r.label, "unknown")
        self.assertEqual(r.ref, "home")             # nearest is still reported
        self.assertIn("over threshold 20", r.summary())

    def test_crop_ignores_the_clock(self):
        base = jpeg((50, 50, 60))
        clock = jpeg((50, 50, 60), (0, 0, 1280, 80), (255, 255, 0))
        cropped = self.clf(grid=(16, 9), crop=(0.0, 0.12, 1.0, 1.0), threshold=5)
        cropped.learn("home", base)
        self.assertEqual(cropped.classify(clock).label, "home")
        full = Classifier(self.dir / "full", grid=(16, 9), crop=(0.0, 0.0, 1.0, 1.0), threshold=5)
        full.learn("home", base)
        self.assertEqual(full.classify(clock).label, "unknown")

    def test_bad_names_and_images(self):
        c = self.clf()
        with self.assertRaises(ValueError):
            c.learn("../evil", jpeg((0, 0, 0)))
        with self.assertRaises(OSError):
            c.learn("home", b"not a jpeg")
        self.assertEqual(c.ref_names(), [])

    def test_missing_ref_file_is_ignored(self):
        c = self.clf()
        c.learn("home", jpeg((50, 50, 60)))
        c.learn("game", jpeg((30, 170, 60)))
        (self.dir / "game.jpg").unlink()
        c2 = self.clf()
        self.assertEqual(c2.ref_names(), ["home"])
        self.assertEqual(c2.classify(jpeg((50, 50, 60))).label, "home")


if __name__ == "__main__":
    unittest.main()
