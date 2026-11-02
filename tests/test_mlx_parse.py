"""Parser tests with synthetic GY-MCU90640 frames.

Run on the PC:  python3 -m unittest discover -s tests -v
"""

import os
import random
import struct
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "thermal"))

import mlx_uart as m  # noqa: E402


def make_frame(temps_c, ambient_c=25.0, checksum="word"):
    assert len(temps_c) == m.NPIX
    body = m.HEADER + m.LEN_FIELD
    body += struct.pack("<%dh" % m.NPIX, *[int(round(t * 100)) for t in temps_c])
    body += struct.pack("<h", int(round(ambient_c * 100)))
    tmp = body + b"\x00\x00"
    cs = m.CHECKSUMS[checksum](tmp)
    return body + struct.pack("<H", cs)


def gradient(base=20.0):
    return [base + (i % m.COLS) * 0.1 + (i // m.COLS) * 0.05 for i in range(m.NPIX)]


class ParserTests(unittest.TestCase):
    def test_frame_length(self):
        self.assertEqual(len(make_frame(gradient())), m.FRAME_LEN)

    def test_valid_frame(self):
        temps = gradient()
        p = m.FrameParser()
        frames = p.feed(make_frame(temps, 26.5))
        self.assertEqual(len(frames), 1)
        vals, amb = m.decode_raw(frames[0])
        self.assertAlmostEqual(amb, 26.5)
        for a, b in zip(vals, temps):
            self.assertAlmostEqual(a, b, places=2)

    def test_negative_temperatures(self):
        temps = [-10.0 + i * 0.01 for i in range(m.NPIX)]
        vals, _ = m.decode_raw(m.FrameParser().feed(make_frame(temps, -5.0))[0])
        self.assertAlmostEqual(vals[0], -10.0)

    def test_bad_checksum_dropped(self):
        f = bytearray(make_frame(gradient()))
        f[100] ^= 0xFF
        p = m.FrameParser()
        self.assertEqual(p.feed(bytes(f)), [])
        self.assertGreaterEqual(p.frames_bad, 1)

    def test_checksum_none_accepts(self):
        f = bytearray(make_frame(gradient()))
        f[-1] ^= 0xFF
        self.assertEqual(len(m.FrameParser(checksum="none").feed(bytes(f))), 1)

    def test_byte_checksum_variant(self):
        f = make_frame(gradient(), checksum="byte")
        self.assertEqual(len(m.FrameParser(checksum="byte").feed(f)), 1)

    def test_garbage_before_header(self):
        rnd = random.Random(1)
        junk = bytes(rnd.randrange(256) for _ in range(777)).replace(b"\x5a", b"\x00")
        p = m.FrameParser()
        frames = p.feed(junk + make_frame(gradient()))
        self.assertEqual(len(frames), 1)
        self.assertEqual(p.bytes_skipped, len(junk))

    def test_split_across_reads(self):
        data = make_frame(gradient(20)) + make_frame(gradient(30))
        p = m.FrameParser()
        out = []
        # Odd chunk size forces splits inside the header and inside the payload.
        for i in range(0, len(data), 37):
            out += p.feed(data[i:i + 37])
        self.assertEqual(len(out), 2)
        self.assertAlmostEqual(m.decode_raw(out[1])[0][0], 30.0)

    def test_split_header_bytes(self):
        f = make_frame(gradient())
        p = m.FrameParser()
        self.assertEqual(p.feed(f[:3]), [])
        self.assertEqual(len(p.feed(f[3:])), 1)

    def test_truncated_frame_then_resync(self):
        good = make_frame(gradient(22))
        p = m.FrameParser()
        frames = p.feed(good[:900] + good)
        self.assertEqual(len(frames), 1)
        self.assertAlmostEqual(m.decode_raw(frames[0])[0][0], 22.0)

    def test_numpy_decode_shape(self):
        try:
            import numpy  # noqa: F401
        except ImportError:
            self.skipTest("numpy not installed")
        temps = gradient()
        arr, amb = m.decode(make_frame(temps, 24.0))
        self.assertEqual(arr.shape, (24, 32))
        self.assertAlmostEqual(float(arr[1, 2]), temps[34], places=2)
        self.assertAlmostEqual(amb, 24.0)


class CommandTests(unittest.TestCase):
    def test_command_checksums(self):
        self.assertEqual(m.CMD_SAVE, bytes([0xA5, 0x65, 0x01, 0x0B]))
        self.assertEqual(m.cmd_baud(115200), bytes([0xA5, 0x15, 0x01, 0xBB]))
        self.assertEqual(m.cmd_baud(460800), bytes([0xA5, 0x15, 0x02, 0xBC]))


if __name__ == "__main__":
    unittest.main()


class AutoBaudTests(unittest.TestCase):
    """MLXReader falls back to another baud rate when no valid frames arrive."""

    def test_finds_working_baud(self):
        import threading
        import time

        module_baud = 115200          # what the fake module really talks at
        frame = make_frame(gradient())
        opened = []                   # bauds the reader tried, in order
        stop = threading.Event()

        def fake_open(dev, baud):
            r, w = os.pipe()
            opened.append(baud)

            def feed():
                # Right baud: real frames. Wrong baud: the line looks like noise.
                try:
                    while not stop.is_set():
                        os.write(w, frame if baud == module_baud else os.urandom(512))
                        time.sleep(0.02)
                except OSError:
                    pass              # reader closed its end
                finally:
                    os.close(w)
            threading.Thread(target=feed, daemon=True).start()
            return r

        orig_open, orig_hunt = m.open_serial, m.MLXReader.BAUD_HUNT_S
        m.open_serial, m.MLXReader.BAUD_HUNT_S = fake_open, 0.3
        try:
            rd = m.MLXReader("/dev/fake", baud=460800, auto_bauds=(115200, 460800))
            rd.start()
            deadline = time.time() + 5
            while rd.latest() is None and time.time() < deadline:
                time.sleep(0.05)
            rd.stop()
        finally:
            stop.set()
            m.open_serial, m.MLXReader.BAUD_HUNT_S = orig_open, orig_hunt
        self.assertIsNotNone(rd.latest(), "no frame after falling back")
        self.assertEqual(opened[:2], [460800, 115200])
        self.assertEqual(rd.baud, 115200)

    def test_asks_module_back_to_home_baud(self):
        import threading
        import time
        written = []

        def fake_open(dev, baud):
            r, w = os.pipe()
            def feed():          # noise only: the reader never gets a frame
                try:
                    for _ in range(40):
                        os.write(w, os.urandom(256)); time.sleep(0.02)
                except OSError:
                    pass
                finally:
                    os.close(w)
            threading.Thread(target=feed, daemon=True).start()
            return r

        orig = (m.open_serial, m.MLXReader.BAUD_HUNT_S, m.os.write)
        def spy_write(fd, data):
            written.append(bytes(data))
            return len(data)
        m.open_serial, m.MLXReader.BAUD_HUNT_S = fake_open, 0.2
        m.os.write = spy_write
        try:
            rd = m.MLXReader("/dev/fake", baud=460800, auto_bauds=(115200, 460800))
            rd.start()
            time.sleep(0.6)
            rd.stop()
            time.sleep(0.3)
        finally:
            m.open_serial, m.MLXReader.BAUD_HUNT_S, m.os.write = orig
        self.assertIn(m.cmd_baud(115200), written)
