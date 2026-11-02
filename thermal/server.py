#!/usr/bin/env python3
"""UnitV2 thermal overlay server.

    python3 server.py                 # camera + GY-MCU90640, http://<unitv2>:8080
    python3 server.py --demo          # synthetic thermal source (no sensor needed)
    python3 server.py --no-camera     # thermal on a grey canvas (no camera needed)
"""

import argparse
import json
import math
import os
import socket
import socketserver
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

import cv2
import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import mlx_uart  # noqa: E402
import overlay  # noqa: E402

SERVER_DEFAULTS = {
    "serial_dev": "/dev/ttyS1",
    "serial_baud": 115200,
    "checksum": "word",          # "word", "byte" or "none" - confirm with tools/uart_probe.py
    "camera": 0,                 # index or device path
    "width": 640,
    "height": 480,
    "jpeg_quality": 70,
    "max_fps": 15.0,
    "thermal_timeout": 3.0,      # seconds without a new thermal frame -> hide overlay
}
DEFAULTS = dict(overlay.DEFAULTS)
DEFAULTS.update(SERVER_DEFAULTS)
CHOICES = {
    "colormap": sorted(overlay.COLORMAPS),
    "interp": sorted(overlay.INTERP),
    "range_mode": ["auto", "manual"],
    "checksum": ["word", "byte", "none"],
}


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

def coerce(key, value):
    """Validate/convert a config value against the type of its default."""
    if key not in DEFAULTS:
        raise KeyError("unknown setting %r" % key)
    default = DEFAULTS[key]
    if key == "camera":
        if isinstance(value, str) and value.isdigit():
            return int(value)
        if isinstance(value, (int, str)) and not isinstance(value, bool):
            return value
        raise ValueError("camera must be an index or a path")
    if isinstance(default, bool):
        if isinstance(value, str):
            return value.lower() in ("1", "true", "yes", "on")
        return bool(value)
    if isinstance(default, int):
        return int(value)
    if isinstance(default, float):
        v = float(value)
        if not math.isfinite(v):
            raise ValueError("%s must be finite" % key)
        return v
    v = str(value)
    if key in CHOICES and v not in CHOICES[key]:
        raise ValueError("%s must be one of %s" % (key, CHOICES[key]))
    return v


class Config(object):
    """Settings in a JSON file. Saves are crash-safe: power can be cut at any moment.

    Each save writes config.json.tmp, fsyncs it, moves the previous good file to
    config.json.bak and renames the new one into place. Loading falls back to the .bak
    and then to the defaults, so a damaged file never keeps the server from starting.
    """

    def __init__(self, path):
        self.path = path
        self.bak = path + ".bak"
        self.lock = threading.Lock()
        self.data = dict(DEFAULTS)
        saved = self._load(path)
        if saved is None:
            saved = self._load(self.bak)
            if saved is not None:
                print("config: using the backup %s" % self.bak)
        for k, v in (saved or {}).items():
            try:
                self.data[k] = coerce(k, v)
            except (KeyError, ValueError) as e:
                print("config: ignoring %s: %s" % (k, e))

    @staticmethod
    def _load(path):
        if not os.path.exists(path):
            return None
        try:
            with open(path) as f:
                saved = json.load(f)
            if not isinstance(saved, dict):
                raise ValueError("not a JSON object")
            return saved
        except (OSError, ValueError) as e:  # JSONDecodeError is a ValueError
            print("config: %s is unreadable (%s), set aside as %s.bad" % (path, e, path))
            try:
                os.replace(path, path + ".bad")
            except OSError:
                pass
            return None

    def snapshot(self):
        with self.lock:
            return dict(self.data)

    def update(self, changes):
        clean = {k: coerce(k, v) for k, v in changes.items()}
        with self.lock:
            self.data.update(clean)
            self._save()
        return clean

    def _save(self):
        tmp = self.path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(self.data, f, indent=2, sort_keys=True)
            f.flush()
            os.fsync(f.fileno())
        if os.path.exists(self.path):
            os.replace(self.path, self.bak)
        os.replace(tmp, self.path)
        try:  # make the renames themselves durable
            dfd = os.open(os.path.dirname(os.path.abspath(self.path)), os.O_RDONLY)
            try:
                os.fsync(dfd)
            finally:
                os.close(dfd)
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Sources
# ---------------------------------------------------------------------------

class Camera(threading.Thread):
    def __init__(self, src, width, height):
        super(Camera, self).__init__(name="camera")
        self.daemon = True
        self.src, self.width, self.height = src, width, height
        self.cond = threading.Condition()
        self.frame, self.seq = None, 0
        self.fps = 0.0
        self.error = None
        self.active = threading.Event()   # set while someone needs frames
        self._reopen = threading.Event()

    def reconfigure(self, src=None, width=None, height=None):
        if src is not None:
            self.src = src
        if width:
            self.width = width
        if height:
            self.height = height
        self._reopen.set()

    def wait_frame(self, last_seq, timeout=1.0):
        with self.cond:
            if self.seq == last_seq:
                self.cond.wait(timeout)
            return self.frame, self.seq

    def run(self):
        while True:
            self._reopen.clear()
            cap = cv2.VideoCapture(self.src)
            if not cap.isOpened():
                self.error = "cannot open camera %r" % (self.src,)
                cap.release()
                time.sleep(2.0)
                continue
            cap.set(cv2.CAP_PROP_FRAME_WIDTH, self.width)
            cap.set(cv2.CAP_PROP_FRAME_HEIGHT, self.height)
            cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)   # fewer queued driver buffers = fresher frames
            self.error = None
            last = None
            fails = 0
            while not self._reopen.is_set():
                if not self.active.wait(0.5):
                    continue          # nobody watching: leave the camera idle
                ok, frame = cap.read()
                if not ok or frame is None:
                    fails += 1
                    if fails > 20:
                        self.error = "camera read failing"
                        break
                    time.sleep(0.05)
                    continue
                fails = 0
                if frame.shape[1] != self.width or frame.shape[0] != self.height:
                    frame = cv2.resize(frame, (self.width, self.height))
                now = time.time()
                if last:
                    self.fps = 0.9 * self.fps + 0.1 / max(now - last, 1e-3)
                last = now
                with self.cond:
                    self.frame, self.seq = frame, self.seq + 1
                    self.cond.notify_all()
            cap.release()
            time.sleep(0.5)


class DemoThermal(object):
    """Stand-in for MLXReader: a warm blob drifting over a 22 degC background."""

    def __init__(self, hz=8.0):
        self.hz, self.seq, self.t0 = hz, 0, time.time()
        self.baud, self.dev = 0, "demo"
        yy, xx = np.mgrid[0:mlx_uart.ROWS, 0:mlx_uart.COLS]
        self._yy, self._xx = yy.astype(np.float32), xx.astype(np.float32)

    def latest(self):
        t = time.time() - self.t0
        self.seq = int(t * self.hz)
        ts = self.t0 + self.seq / self.hz
        cx = 16 + 10 * math.sin(ts * 0.5)
        cy = 12 + 6 * math.sin(ts * 0.37)
        temps = 22.0 + 0.05 * self._xx + 12.0 * np.exp(-((self._xx - cx) ** 2 + (self._yy - cy) ** 2) / 12.0)
        return temps.astype(np.float32), 24.0, ts, self.seq

    def stats(self):
        return {"dev": "demo", "fps": self.hz, "seq": self.seq, "error": None}

    def send(self, data):
        pass

    def reconfigure(self, **kw):
        pass

    def start(self):
        pass


# ---------------------------------------------------------------------------
# Render loop + frame hub
# ---------------------------------------------------------------------------

class App(object):
    def __init__(self, cfg, thermal, camera):
        self.cfg = cfg
        self.thermal = thermal
        self.camera = camera
        c = cfg.snapshot()
        self.comp = overlay.Compositor(c)
        self.comp_lock = threading.Lock()
        self.hub = threading.Condition()
        self.jpeg, self.jpeg_seq = None, 0
        self.viewers = 0
        self.want_until = 0.0        # keep rendering until this time (snapshots)
        self.out_fps = 0.0

    def demand(self, seconds=2.0):
        with self.hub:
            self.want_until = max(self.want_until, time.time() + seconds)

    def active(self):
        return self.viewers > 0 or time.time() < self.want_until

    def wait_jpeg(self, last_seq, timeout=2.0):
        with self.hub:
            if self.jpeg_seq == last_seq:
                self.hub.wait(timeout)
            return self.jpeg, self.jpeg_seq

    def apply_config(self, changes):
        c = self.cfg.snapshot()
        with self.comp_lock:
            self.comp.update_config(c)
        if {"serial_dev", "serial_baud", "checksum"} & set(changes):
            self.thermal.reconfigure(dev=c["serial_dev"], baud=c["serial_baud"], checksum=c["checksum"])
        if self.camera and {"camera", "width", "height"} & set(changes):
            self.camera.reconfigure(c["camera"], c["width"], c["height"])

    def stats(self):
        s = {"thermal": self.thermal.stats(), "out_fps": round(self.out_fps, 1), "viewers": self.viewers}
        if self.camera:
            s["camera"] = {"fps": round(self.camera.fps, 1), "error": self.camera.error, "src": self.camera.src}
        return s

    def run(self):
        cam_seq = 0
        last_out = None
        while True:
            if not self.active():
                if self.camera:
                    self.camera.active.clear()
                time.sleep(0.1)
                continue
            loop_start = time.time()
            c = self.cfg.snapshot()
            frame = None
            if self.camera:
                self.camera.active.set()
                frame, cam_seq = self.camera.wait_frame(cam_seq, timeout=1.0)
            if frame is None:
                # No camera (or not ready): thermal on a grey canvas so bring-up still works.
                frame = np.full((c["height"], c["width"], 3), 60, np.uint8)
                if self.camera and self.camera.error:
                    overlay.Compositor._text(frame, self.camera.error, (10, 50), 0.5, (0, 200, 255))
            else:
                frame = frame.copy()

            th = self.thermal.latest()
            if th is not None and time.time() - th[2] > c["thermal_timeout"]:
                th = None
            ts = self.thermal.stats()
            info = "video %.1f fps  thermal %.1f fps" % (self.out_fps, ts.get("fps", 0))
            if ts.get("error"):
                info += "  [%s]" % ts["error"]

            with self.comp_lock:
                self.comp.render(frame, th, info)
            ok, buf = cv2.imencode(".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), int(c["jpeg_quality"])])
            if not ok:
                continue
            now = time.time()
            if last_out:
                self.out_fps = 0.9 * self.out_fps + 0.1 / max(now - last_out, 1e-3)
            last_out = now
            with self.hub:
                self.jpeg, self.jpeg_seq = buf.tobytes(), self.jpeg_seq + 1
                self.hub.notify_all()

            # Frame-rate cap.
            spare = 1.0 / max(c["max_fps"], 1.0) - (time.time() - loop_start)
            if spare > 0:
                time.sleep(spare)


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------

STREAM_SNDBUF = 16384   # bytes; Linux doubles it. Keeps the stream from queueing old frames.


class ThreadingHTTPServer(socketserver.ThreadingMixIn, HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def make_handler(app):
    index_path = os.path.join(HERE, "static", "index.html")

    class Handler(BaseHTTPRequestHandler):
        server_version = "UnitV2Thermal/1.0"

        def log_message(self, fmt, *args):
            pass

        def _send(self, code, body, ctype="application/json"):
            if isinstance(body, (dict, list)):
                body = json.dumps(body).encode()
            elif isinstance(body, str):
                body = body.encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _json_body(self):
            n = int(self.headers.get("Content-Length") or 0)
            if n > 65536:
                raise ValueError("body too large")
            return json.loads(self.rfile.read(n).decode() or "{}")

        def do_GET(self):
            path = self.path.split("?", 1)[0]
            if path in ("/", "/index.html"):
                with open(index_path, "rb") as f:
                    self._send(200, f.read(), "text/html; charset=utf-8")
            elif path == "/stream":
                self._stream()
            elif path == "/snapshot.jpg":
                app.demand(2.0)
                jpg, _ = app.wait_jpeg(app.jpeg_seq, timeout=3.0)
                if jpg is None:
                    self._send(503, {"error": "no frame"})
                else:
                    self._send(200, jpg, "image/jpeg")
            elif path == "/api/config":
                self._send(200, {"config": app.cfg.snapshot(), "defaults": DEFAULTS, "choices": CHOICES})
            elif path == "/api/stats":
                self._send(200, app.stats())
            elif path == "/api/thermal":
                th = app.thermal.latest()
                if th is None:
                    self._send(503, {"error": "no thermal frame yet"})
                else:
                    self._send(200, {
                        "rows": mlx_uart.ROWS, "cols": mlx_uart.COLS,
                        "ambient": th[1], "timestamp": th[2], "seq": th[3],
                        "temps": np.round(th[0], 2).tolist(),
                    })
            else:
                self._send(404, {"error": "not found"})

        def do_POST(self):
            path = self.path.split("?", 1)[0]
            try:
                body = self._json_body()
                if not isinstance(body, dict):
                    raise ValueError("expected a JSON object")
                if path == "/api/config":
                    changes = app.cfg.update(body)
                    app.apply_config(changes)
                    self._send(200, {"ok": True, "config": app.cfg.snapshot()})
                elif path == "/api/mlx":
                    self._send(200, self._mlx(body))
                else:
                    self._send(404, {"error": "not found"})
            except (KeyError, ValueError, TypeError) as e:
                self._send(400, {"error": str(e)})

        def _mlx(self, body):
            """Module commands: {"rate": 4} / {"baud": 460800} / {"save": true}."""
            sent = []
            if "rate" in body:
                hz = float(body["rate"])
                if hz not in mlx_uart.RATE_ARGS:
                    raise ValueError("rate must be one of %s" % sorted(mlx_uart.RATE_ARGS))
                app.thermal.send(mlx_uart.cmd_rate(hz))
                sent.append("rate %g Hz" % hz)
            if body.get("save"):
                app.thermal.send(mlx_uart.CMD_SAVE)
                sent.append("save")
            if "baud" in body:
                baud = int(body["baud"])
                if baud not in mlx_uart.BAUD_ARGS:
                    raise ValueError("baud must be one of %s" % sorted(mlx_uart.BAUD_ARGS))
                app.thermal.send(mlx_uart.cmd_baud(baud))
                time.sleep(0.4)  # let the reader thread flush the command at the old rate
                app.cfg.update({"serial_baud": baud})
                app.apply_config({"serial_baud": baud})
                sent.append("baud %d (UnitV2 side switched too)" % baud)
            return {"ok": True, "sent": sent}

        def _stream(self):
            # Keep the kernel send buffer to about one frame. Left alone, TCP auto-tuning grows
            # it to megabytes: on a slow WiFi link that queues many seconds of old frames.
            # With a small buffer, write() blocks instead, and the next wait_jpeg() skips
            # straight to the newest frame, so delay stays at about one frame's transfer time.
            try:
                self.connection.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, STREAM_SNDBUF)
                self.connection.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            except OSError:
                pass
            self.send_response(200)
            self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            with app.hub:
                app.viewers += 1
            seq = 0
            try:
                while True:
                    jpg, seq2 = app.wait_jpeg(seq, timeout=2.0)
                    if jpg is None or seq2 == seq:
                        continue
                    seq = seq2
                    self.wfile.write(b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: "
                                     + str(len(jpg)).encode() + b"\r\n\r\n" + jpg + b"\r\n")
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass
            finally:
                with app.hub:
                    app.viewers -= 1

    return Handler


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default=os.path.join(HERE, "config.json"))
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--demo", action="store_true", help="synthetic thermal data instead of the UART sensor")
    ap.add_argument("--no-camera", action="store_true", help="render on a grey canvas instead of the camera")
    a = ap.parse_args()

    cfg = Config(a.config)
    c = cfg.snapshot()
    if a.demo:
        thermal = DemoThermal()
    else:
        thermal = mlx_uart.MLXReader(c["serial_dev"], c["serial_baud"], c["checksum"],
                                     init_cmds=[mlx_uart.CMD_START],
                                     auto_bauds=sorted(mlx_uart.BAUD_ARGS))
    thermal.start()
    camera = None if a.no_camera else Camera(c["camera"], c["width"], c["height"])
    if camera:
        camera.start()

    app = App(cfg, thermal, camera)
    threading.Thread(target=app.run, name="render", daemon=True).start()

    httpd = ThreadingHTTPServer((a.host, a.port), make_handler(app))
    print("serving on http://%s:%d  (thermal: %s, camera: %s)" % (
        a.host, a.port, "demo" if a.demo else "%s @ %d" % (c["serial_dev"], c["serial_baud"]),
        "off" if camera is None else c["camera"]))
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
