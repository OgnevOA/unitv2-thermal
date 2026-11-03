"""GY-MCU90640 (MLX90640 + STM32) UART driver.

Frame layout (1544 bytes, little-endian):
    0x5A 0x5A            header
    0x02 0x06            payload length = 0x0602 = 1538
    768 x int16          pixel temperatures, degC * 100, row-major 24 rows x 32 cols
    int16                ambient (sensor die) temperature, degC * 100
    uint16               checksum = low 16 bits of the sum of all preceding LE words

The parser itself is stdlib-only so tools/uart_probe.py works without numpy.
"""

import os
import sys
import struct
import threading
import time

try:  # termios is Linux/Unix only; tests on other platforms only need the parser.
    import termios
except ImportError:  # pragma: no cover
    termios = None

HEADER = b"\x5a\x5a"
LEN_FIELD = b"\x02\x06"
FRAME_LEN = 1544
ROWS, COLS = 24, 32
NPIX = ROWS * COLS

# Module commands: A5 <cmd> <arg> <sum of first three bytes & 0xFF>, from the GY-MCU90640
# user manual V1.0 (section IV.4). Baud rate, rate and auto/query mode are applied from flash:
# send the setting, then CMD_SAVE, then power-cycle the module (seen on the real module:
# a new baud rate does not take effect before that). While the module is streaming at
# 9600 baud it ignores incoming commands; put it in query mode first (CMD_QUERY).
RATE_ARGS = {0.5: 0x00, 1: 0x01, 2: 0x02, 4: 0x03, 8: 0x04}
BAUD_ARGS = {9600: 0x01, 115200: 0x02, 460800: 0x03}


def make_cmd(cmd, arg):
    return bytes([0xA5, cmd & 0xFF, arg & 0xFF, (0xA5 + cmd + arg) & 0xFF])


def cmd_rate(hz):
    return make_cmd(0x25, RATE_ARGS[hz])


def cmd_baud(baud):
    return make_cmd(0x15, BAUD_ARGS[baud])


CMD_SAVE = make_cmd(0x65, 0x01)
# Automatic (continuous) output; sent on every port open, because the module may have been
# saved in query mode. Harmless when it is already streaming.
CMD_START = make_cmd(0x35, 0x02)
CMD_QUERY = make_cmd(0x35, 0x01)   # query mode: the module stops streaming


def word_checksum(frame):
    """Low 16 bits of the sum of LE uint16 words over everything but the checksum."""
    words = struct.unpack("<%dH" % ((FRAME_LEN - 2) // 2), frame[: FRAME_LEN - 2])
    return sum(words) & 0xFFFF


def byte_checksum(frame):
    """Alternative some firmware variants use: low 16 bits of the byte sum."""
    return sum(frame[: FRAME_LEN - 2]) & 0xFFFF


def frame_checksum(frame):
    return struct.unpack_from("<H", frame, FRAME_LEN - 2)[0]


CHECKSUMS = {
    "word": word_checksum,
    "byte": byte_checksum,
}


class FrameParser(object):
    """Incremental parser: feed() arbitrary chunks, get back complete validated frames."""

    def __init__(self, checksum="word"):
        if checksum != "none" and checksum not in CHECKSUMS:
            raise ValueError("checksum must be one of %s or 'none'" % sorted(CHECKSUMS))
        self.checksum = checksum
        self.buf = bytearray()
        self.frames_ok = 0
        self.frames_bad = 0
        self.bytes_skipped = 0

    def _valid(self, frame):
        if self.checksum == "none":
            return True
        return CHECKSUMS[self.checksum](frame) == frame_checksum(frame)

    def feed(self, data):
        self.buf += data
        out = []
        while True:
            idx = self.buf.find(HEADER + LEN_FIELD)
            if idx < 0:
                # Keep the last 3 bytes: they may be the start of a split header.
                keep = min(len(self.buf), 3)
                self.bytes_skipped += len(self.buf) - keep
                del self.buf[: len(self.buf) - keep]
                return out
            if idx:
                self.bytes_skipped += idx
                del self.buf[:idx]
            if len(self.buf) < FRAME_LEN:
                return out
            frame = bytes(self.buf[:FRAME_LEN])
            if self._valid(frame):
                self.frames_ok += 1
                out.append(frame)
                del self.buf[:FRAME_LEN]
            else:
                # False sync or corrupted frame: drop one byte and search again.
                self.frames_bad += 1
                self.bytes_skipped += 1
                del self.buf[:1]


def decode_raw(frame):
    """-> (list of 768 temps degC, ambient degC). Stdlib only."""
    vals = struct.unpack_from("<%dh" % (NPIX + 1), frame, 4)
    return [v / 100.0 for v in vals[:NPIX]], vals[NPIX] / 100.0


def decode(frame):
    """-> (float32 array of shape (24, 32) in degC, ambient degC). Requires numpy."""
    import numpy as np

    vals = np.frombuffer(frame, dtype="<i2", count=NPIX + 1, offset=4)
    temps = vals[:NPIX].astype(np.float32).reshape(ROWS, COLS) / 100.0
    return temps, float(vals[NPIX]) / 100.0


# ---------------------------------------------------------------------------
# Serial port (raw termios, no pyserial dependency)
# ---------------------------------------------------------------------------

def _baud_const(baud):
    name = "B%d" % baud
    if termios is None or not hasattr(termios, name):
        raise ValueError("unsupported baud rate %d" % baud)
    return getattr(termios, name)


def open_serial(dev, baud):
    """Open dev in raw 8N1 mode; reads block for at most 0.1 s."""
    fd = os.open(dev, os.O_RDWR | os.O_NOCTTY)
    try:
        speed = _baud_const(baud)
        attrs = termios.tcgetattr(fd)
        attrs[0] = 0                                                # iflag
        attrs[1] = 0                                                # oflag
        attrs[2] = termios.CS8 | termios.CREAD | termios.CLOCAL     # cflag
        attrs[3] = 0                                                # lflag
        attrs[4] = speed                                            # ispeed
        attrs[5] = speed                                            # ospeed
        attrs[6][termios.VMIN] = 0
        attrs[6][termios.VTIME] = 1
        termios.tcsetattr(fd, termios.TCSANOW, attrs)
        termios.tcflush(fd, termios.TCIOFLUSH)
    except Exception:
        os.close(fd)
        raise
    return fd


def _log(msg):
    print(time.strftime("%Y-%m-%d %H:%M:%S ") + msg)
    sys.stdout.flush()


class MLXReader(threading.Thread):
    """Background thread keeping the most recent valid thermal frame.

    Reconnects automatically if the port disappears or goes silent.
    """

    # No valid frame for this long after opening: try the next baud rate in auto_bauds
    # (at least 2.5 frame transfer times, so a slow rate like 9600 gets its chance).
    BAUD_HUNT_S = 4.0

    def _hunt_s(self):
        return max(self.BAUD_HUNT_S, 2.5 * FRAME_LEN * 10.0 / max(self.baud, 1))

    def __init__(self, dev, baud=115200, checksum="word", init_cmds=(), auto_bauds=()):
        super(MLXReader, self).__init__(name="mlx-reader")
        self.daemon = True
        self.dev = dev
        self.baud = baud
        # Baud rates to cycle through while no frames arrive, e.g. (115200, 460800, 9600): the
        # reader follows the module wherever it is. It never sends baud commands itself.
        self.auto_bauds = list(auto_bauds)
        self.checksum = checksum
        self.init_cmds = list(init_cmds)
        self._lock = threading.Lock()
        self._stop_evt = threading.Event()
        self._reopen = threading.Event()
        self._pending_writes = []
        self._latest = None          # (temps ndarray, ambient, timestamp, seq)
        self.seq = 0
        self.fps = 0.0
        self.error = None
        self.parser = None

    # -- public API ---------------------------------------------------------
    def latest(self):
        with self._lock:
            return self._latest

    def send(self, data):
        with self._lock:
            self._pending_writes.append(bytes(data))

    def reconfigure(self, dev=None, baud=None, checksum=None):
        if dev is not None:
            self.dev = dev
        if baud is not None:
            self.baud = baud
        if checksum is not None:
            self.checksum = checksum
        self._reopen.set()

    def stop(self):
        self._stop_evt.set()

    def stats(self):
        p = self.parser
        return {
            "dev": self.dev,
            "baud": self.baud,
            "fps": round(self.fps, 2),
            "seq": self.seq,
            "frames_ok": p.frames_ok if p else 0,
            "frames_bad": p.frames_bad if p else 0,
            "bytes_skipped": p.bytes_skipped if p else 0,
            "error": self.error,
        }

    # -- thread body --------------------------------------------------------
    def run(self):
        while not self._stop_evt.is_set():
            fd = None
            try:
                fd = open_serial(self.dev, self.baud)
                self._reopen.clear()
                self.parser = FrameParser(self.checksum)
                for c in self.init_cmds:
                    os.write(fd, c)
                    time.sleep(0.05)
                self.error = None
                t0, self._rx_bytes, frames0 = time.monotonic(), 0, self.parser.frames_ok
                outcome = self._loop(fd)
                if outcome:
                    # One line per failed open: the evidence when the sensor goes quiet.
                    _log("serial %s @ %d: %s after %.1f s (%d bytes, %d frames)" % (
                        self.dev, self.baud, outcome, time.monotonic() - t0,
                        self._rx_bytes, self.parser.frames_ok - frames0))
                if outcome == "no frames" and self.auto_bauds:
                    i = self.auto_bauds.index(self.baud) if self.baud in self.auto_bauds else -1
                    nxt = self.auto_bauds[(i + 1) % len(self.auto_bauds)]
                    self.error = "no frames at %d baud, trying %d" % (self.baud, nxt)
                    self.baud = nxt
            except Exception as e:  # noqa: BLE001 - keep thread alive, report the error
                self.error = "%s: %s" % (type(e).__name__, e)
                _log("serial %s @ %d: %s" % (self.dev, self.baud, self.error))
                self._stop_evt.wait(1.0)
            finally:
                if fd is not None:
                    os.close(fd)

    def _loop(self, fd):
        """Read until stopped or reconfigured. Returns "no frames" if nothing valid arrives
        within _hunt_s() of opening (when auto_bauds is set), "silent" after 10 s without
        any bytes, else None."""
        # Timing uses the monotonic clock: the UnitV2 boots at 1970 and NTP later jumps the
        # wall clock forward by decades. Frame timestamps stay wall-clock (the server compares them).
        opened = last_rx = time.monotonic()
        last_frame_t = None
        first = True
        while not (self._stop_evt.is_set() or self._reopen.is_set()):
            with self._lock:
                writes, self._pending_writes = self._pending_writes, []
            for w in writes:
                os.write(fd, w)
            chunk = os.read(fd, 4096)
            now = time.monotonic()
            if chunk:
                last_rx = now
                self._rx_bytes += len(chunk)
            elif now - last_rx > 10.0:
                return "silent"  # reopen the port (error stays set)
            elif now - last_rx > 3.0:
                self.error = "no data from %s for %.0f s" % (self.dev, now - last_rx)
            if self.auto_bauds and last_frame_t is None and now - opened > self._hunt_s():
                return "no frames"
            for frame in self.parser.feed(chunk):
                temps, amb = decode(frame)
                if last_frame_t is not None:
                    dt = now - last_frame_t
                    if dt > 0:
                        self.fps = 0.8 * self.fps + 0.2 * (1.0 / dt) if self.fps else 1.0 / dt
                last_frame_t = now
                self.error = None
                if first:
                    first = False
                    _log("serial %s @ %d: frames after %.1f s" % (self.dev, self.baud, now - opened))
                with self._lock:
                    self.seq += 1
                    self._latest = (temps, amb, time.time(), self.seq)
