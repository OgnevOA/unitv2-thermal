"""Thermal-on-camera compositing.

Geometry: the 32x24 thermal grid is mapped onto a rectangle in the camera frame.
    width  = scale_x * frame_width,  height = scale_y * frame_height
    centre = frame centre + (off_x * frame_width, off_y * frame_height)
flip_h / flip_v mirror the grid inside that rectangle. One cv2.warpAffine does
scaling, flipping, offset and cropping at once.

The expensive part (warp + colourise) is cached and redone only when a new
thermal frame or new settings arrive; per camera frame we only blend + draw HUD.
"""

import cv2
import numpy as np

ROWS, COLS = 24, 32

_CMAP_NAMES = ["INFERNO", "MAGMA", "PLASMA", "TURBO", "JET", "HOT", "RAINBOW", "BONE", "VIRIDIS"]
COLORMAPS = {n: getattr(cv2, "COLORMAP_" + n) for n in _CMAP_NAMES if hasattr(cv2, "COLORMAP_" + n)}

INTERP = {"nearest": cv2.INTER_NEAREST, "linear": cv2.INTER_LINEAR, "cubic": cv2.INTER_CUBIC}

DEFAULTS = {
    "alpha": 0.5,            # overlay opacity 0..1
    "colormap": "INFERNO" if "INFERNO" in COLORMAPS else "JET",
    "interp": "cubic",
    "range_mode": "auto",    # "auto" or "manual"
    "t_min": 20.0,           # manual range, degC
    "t_max": 40.0,
    "min_span": 3.0,         # auto range never narrower than this, degC
    "range_smooth": 0.3,     # EMA factor for auto range (1 = no smoothing)
    "hot_only": False,       # hide pixels colder than hot_threshold
    "hot_threshold": 30.0,
    "scale_x": 0.8,
    "scale_y": 0.8,
    "off_x": 0.0,
    "off_y": 0.0,
    "flip_h": False,
    "flip_v": False,
    "show_hud": True,
    "show_outline": False,   # draw the thermal rectangle (handy while calibrating)
}


def thermal_rect(cfg, w, h):
    """-> (x0, y0, rw, rh) in float pixels."""
    rw = cfg["scale_x"] * w
    rh = cfg["scale_y"] * h
    cx = w / 2.0 + cfg["off_x"] * w
    cy = h / 2.0 + cfg["off_y"] * h
    return cx - rw / 2.0, cy - rh / 2.0, rw, rh


def affine(cfg, w, h):
    """2x3 matrix mapping thermal (col, row) pixel centres to frame pixel coordinates."""
    x0, y0, rw, rh = thermal_rect(cfg, w, h)
    sx, sy = rw / COLS, rh / ROWS
    # Pixel-centre convention: x = x0 + (u + 0.5) * sx - 0.5
    if cfg["flip_h"]:
        ax, bx = -sx, x0 + (COLS - 0.5) * sx - 0.5
    else:
        ax, bx = sx, x0 + 0.5 * sx - 0.5
    if cfg["flip_v"]:
        ay, by = -sy, y0 + (ROWS - 0.5) * sy - 0.5
    else:
        ay, by = sy, y0 + 0.5 * sy - 0.5
    return np.array([[ax, 0, bx], [0, ay, by]], dtype=np.float64)


def grid_to_frame(M, col, row):
    return int(round(M[0, 0] * col + M[0, 2])), int(round(M[1, 1] * row + M[1, 2]))


class Compositor(object):
    def __init__(self, cfg):
        self.cfg = dict(DEFAULTS)
        self.cfg.update({k: v for k, v in cfg.items() if k in DEFAULTS})
        self._key = None          # (thermal seq, frame size, cfg version) the cache was built for
        self._cfg_ver = 0
        self._lo = self._hi = None
        self._cache = None
        self._bar_cache = None

    def update_config(self, cfg):
        self.cfg.update({k: v for k, v in cfg.items() if k in DEFAULTS})
        self._cfg_ver += 1
        self._bar_cache = None

    # -- range --------------------------------------------------------------
    def _range(self, temps):
        c = self.cfg
        if c["range_mode"] == "manual":
            lo, hi = float(c["t_min"]), float(c["t_max"])
        else:
            fmin, fmax = float(temps.min()), float(temps.max())
            if self._lo is None:
                self._lo, self._hi = fmin, fmax
            else:
                k = float(c["range_smooth"])
                self._lo += k * (fmin - self._lo)
                self._hi += k * (fmax - self._hi)
            lo, hi = self._lo, self._hi
        span = max(hi - lo, float(c["min_span"]), 0.1)
        mid = (hi + lo) / 2.0
        if hi - lo < span:
            lo, hi = mid - span / 2.0, mid + span / 2.0
        return lo, hi

    # -- cached thermal layer -----------------------------------------------
    def _build(self, temps, w, h):
        c = self.cfg
        lo, hi = self._range(temps)
        M = affine(c, w, h)
        interp = INTERP.get(c["interp"], cv2.INTER_CUBIC)
        warped = cv2.warpAffine(temps, M, (w, h), flags=interp, borderMode=cv2.BORDER_REPLICATE)

        # Visible rectangle (intersection of thermal rect and frame).
        x0, y0, rw, rh = thermal_rect(c, w, h)
        ix0, iy0 = max(0, int(round(x0))), max(0, int(round(y0)))
        ix1, iy1 = min(w, int(round(x0 + rw))), min(h, int(round(y0 + rh)))
        mask = np.zeros((h, w), dtype=bool)
        if ix1 > ix0 and iy1 > iy0:
            mask[iy0:iy1, ix0:ix1] = True
        if c["hot_only"]:
            mask &= warped >= float(c["hot_threshold"])

        norm = np.clip((warped - lo) * (255.0 / (hi - lo)), 0, 255).astype(np.uint8)
        color = cv2.applyColorMap(norm, COLORMAPS.get(c["colormap"], cv2.COLORMAP_JET))

        r_max, c_max = np.unravel_index(int(np.argmax(temps)), temps.shape)
        r_min, c_min = np.unravel_index(int(np.argmin(temps)), temps.shape)
        self._cache = {
            "color": color,
            "mask3": mask[:, :, None],
            "any": bool(mask.any()),
            "warped": warped,
            "lo": lo, "hi": hi,
            "tmax": float(temps[r_max, c_max]), "pmax": grid_to_frame(M, c_max, r_max),
            "tmin": float(temps[r_min, c_min]), "pmin": grid_to_frame(M, c_min, r_min),
            "rect": (int(round(x0)), int(round(y0)), int(round(x0 + rw)), int(round(y0 + rh))),
        }

    def render(self, frame, thermal, stats_text=""):
        """Blend thermal onto frame (in place) and return it.

        thermal: None or (temps (24,32) float32, ambient, timestamp, seq)
        """
        h, w = frame.shape[:2]
        if thermal is not None:
            key = (thermal[3], w, h, self._cfg_ver)
            if key != self._key:
                self._build(thermal[0], w, h)
                self._key = key
        cache = self._cache if thermal is not None else None
        c = self.cfg

        if cache is not None and cache["any"] and c["alpha"] > 0:
            a = float(c["alpha"])
            blended = cv2.addWeighted(frame, 1.0 - a, cache["color"], a, 0)
            np.copyto(frame, blended, where=cache["mask3"])

        if c["show_hud"]:
            self._hud(frame, cache, thermal, stats_text)
        return frame

    # -- HUD ----------------------------------------------------------------
    @staticmethod
    def _text(img, s, org, scale=0.5, color=(255, 255, 255)):
        # Same thickness for shadow and text: Hershey glyph advance grows with thickness.
        x, y = org
        for dx, dy in ((1, 1), (-1, -1), (1, -1), (-1, 1)):
            cv2.putText(img, s, (x + dx, y + dy), cv2.FONT_HERSHEY_SIMPLEX, scale, (0, 0, 0), 1, cv2.LINE_AA)
        cv2.putText(img, s, org, cv2.FONT_HERSHEY_SIMPLEX, scale, color, 1, cv2.LINE_AA)

    def _scale_bar(self, h):
        if self._bar_cache is None or self._bar_cache.shape[0] != h:
            grad = np.linspace(255, 0, h).astype(np.uint8).reshape(h, 1)
            bar = cv2.applyColorMap(np.repeat(grad, 12, axis=1), COLORMAPS.get(self.cfg["colormap"], cv2.COLORMAP_JET))
            self._bar_cache = bar
        return self._bar_cache

    def _hud(self, img, cache, thermal, stats_text):
        h, w = img.shape[:2]
        if cache is None:
            self._text(img, "no thermal data", (10, 24), 0.6, (0, 0, 255))
            if stats_text:
                self._text(img, stats_text, (10, h - 10), 0.45)
            return

        c = self.cfg
        if c["show_outline"]:
            x0, y0, x1, y1 = cache["rect"]
            cv2.rectangle(img, (x0, y0), (x1 - 1, y1 - 1), (255, 255, 255), 1)

        # Hottest / coldest markers.
        px, py = cache["pmax"]
        if 0 <= px < w and 0 <= py < h:
            cv2.drawMarker(img, (px, py), (0, 0, 255), cv2.MARKER_CROSS, 18, 2)
            self._text(img, "%.1fC" % cache["tmax"], (min(px + 8, w - 60), max(py - 8, 14)), 0.5, (80, 80, 255))
        px, py = cache["pmin"]
        if 0 <= px < w and 0 <= py < h:
            cv2.drawMarker(img, (px, py), (255, 160, 0), cv2.MARKER_TILTED_CROSS, 12, 1)

        # Centre spot.
        cx, cy = w // 2, h // 2
        cv2.drawMarker(img, (cx, cy), (255, 255, 255), cv2.MARKER_CROSS, 14, 1)
        x0, y0, x1, y1 = cache["rect"]
        if x0 <= cx < x1 and y0 <= cy < y1:
            self._text(img, "%.1fC" % float(cache["warped"][cy, cx]), (cx + 8, cy + 18), 0.5)

        # Scale bar on the right.
        bh = int(h * 0.5)
        bar = self._scale_bar(bh)
        bx, by = w - 22, (h - bh) // 2
        img[by:by + bh, bx:bx + bar.shape[1]] = bar
        cv2.rectangle(img, (bx - 1, by - 1), (bx + bar.shape[1], by + bh), (255, 255, 255), 1)
        self._text(img, "%.1f" % cache["hi"], (bx - 44, by + 10), 0.42)
        self._text(img, "%.1f" % cache["lo"], (bx - 44, by + bh), 0.42)

        # Top line: min / max / ambient.  Bottom line: stats.
        self._text(img, "max %.1f  min %.1f  amb %.1f C" % (cache["tmax"], cache["tmin"], thermal[1]), (10, 22), 0.55)
        if stats_text:
            self._text(img, stats_text, (10, h - 10), 0.45)
