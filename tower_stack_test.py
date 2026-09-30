import ctypes
from ctypes import wintypes
from collections import deque
import math
import time

import cv2
import numpy as np

# ============================================================
# TOWER STACK - ACCURATE BOT TEST
# Separate file. The existing circle bot is untouched.
#
# Flow:
#   live scrcpy capture
#   -> game-area block detection
#   -> top moving block tracking
#   -> target block detection
#   -> velocity / direction prediction
#   -> one 3-click ultra-fast burst
#   -> landing verification
#   -> next block
#
# The bot starts scanning immediately. There is no startup sleep.
# Start the Tower Stack game AFTER this program says [READY].
# ============================================================

SCRCPY_TITLE = "CHESS_MOBILE"
OUTPUT_TITLE = "TOWER STACK - BOT TEST"

AUTO_CLICK_START = True
SHOW_OUTPUT = True
DISPLAY_EVERY = 2

# The supplied video is 480x800. Normalized ROI also works when scrcpy
# is resized while keeping the phone aspect ratio.
GAME_X0 = 0.17
GAME_X1 = 0.83
GAME_Y0 = 0.30
GAME_Y1 = 0.955

# Vivid horizontal game blocks.
MIN_SATURATION = 45
MIN_VALUE = 90
MIN_COLOR_SPREAD = 30
MIN_RUN_WIDTH = 15

MIN_BLOCK_WIDTH = 20
MAX_BLOCK_WIDTH = 200
MIN_BLOCK_HEIGHT = 8
MAX_BLOCK_HEIGHT = 34
MIN_BLOCK_ASPECT = 0.75
MAX_BLOCK_ASPECT = 14.0
ROW_SPAN_CHANGE = 6.0
ROW_MEDIAN_WINDOW = 5

# Motion tracking.
MOTION_HISTORY = 7
MIN_MOVING_SPEED = 45.0
MIN_MOTION_FRAMES = 3
MAX_TRACK_GAP = 2
MAX_Y_TRACK_ERROR = 15.0
MAX_WIDTH_TRACK_ERROR = 60.0
STARTUP_MIN_MOVING_Y_RATIO = 0.60

# Prediction.
CLICK_LEAD_MIN_MS = 7.0
CLICK_LEAD_MAX_MS = 22.0
PREDICTION_MAX_SEC = 2.50
PREDICTION_TOLERANCE_MIN = 6.0
PREDICTION_TOLERANCE_RATIO = 0.10
SAFE_OVERLAP_RATIO = 0.30

# User requested three extremely fast taps at the same drop point.
BURST_TAPS = 3
BURST_GAP_MS = 0

# Landing verification.
LANDING_VERTICAL_CHANGE_PX = 3.0
LANDING_VERIFY_TIMEOUT_MS = 300.0
LANDING_MISS_FRAMES = 3

# Game-over detection.
BASE_BOTTOM_TOLERANCE_PX = 45
GAME_OVER_MISS_FRAMES = 18


try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)
except Exception:
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass

user32 = ctypes.windll.user32
ULONG_PTR = getattr(wintypes, "ULONG_PTR", ctypes.c_size_t)


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", wintypes.LONG),
        ("dy", wintypes.LONG),
        ("mouseData", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ULONG_PTR),
    ]


class INPUT(ctypes.Structure):
    _fields_ = [
        ("type", wintypes.DWORD),
        ("mi", MOUSEINPUT),
    ]


INPUT_MOUSE = 0
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004

user32.SendInput.argtypes = [
    wintypes.UINT,
    ctypes.POINTER(INPUT),
    ctypes.c_int,
]
user32.SendInput.restype = wintypes.UINT


def find_scrcpy():
    hwnd = user32.FindWindowW(None, SCRCPY_TITLE)
    return hwnd if hwnd else None


def client_region(hwnd):
    rect = wintypes.RECT()
    if not user32.GetClientRect(hwnd, ctypes.byref(rect)):
        return None

    w = rect.right - rect.left
    h = rect.bottom - rect.top
    if w <= 0 or h <= 0:
        return None

    pt = wintypes.POINT(0, 0)
    if not user32.ClientToScreen(hwnd, ctypes.byref(pt)):
        return None

    return (pt.x, pt.y, pt.x + w, pt.y + h)


class Capture:
    def __init__(self):
        self.region = None
        self.dx = None
        self.mss = None

        try:
            import dxcam
            self.dx = dxcam.create(output_color="BGR")
            self.backend = "DXCAM"
        except Exception as exc:
            print("[WARN] DXCam unavailable:", exc)
            try:
                import mss
                self.mss = mss.mss()
                self.backend = "MSS"
            except Exception as exc2:
                raise RuntimeError("Install dxcam or mss") from exc2

    def set_region(self, region):
        if region:
            self.region = region

    def grab(self):
        if not self.region:
            return None

        left, top, right, bottom = self.region

        if self.backend == "DXCAM":
            return self.dx.grab(
                region=(left, top, right, bottom)
            )

        raw = np.asarray(
            self.mss.grab(
                {
                    "left": left,
                    "top": top,
                    "width": right - left,
                    "height": bottom - top,
                }
            )
        )
        return cv2.cvtColor(raw, cv2.COLOR_BGRA2BGR)

    def close(self):
        if self.dx is not None:
            try:
                release = getattr(self.dx, "release", None)
                if callable(release):
                    release()
                else:
                    stop = getattr(self.dx, "stop", None)
                    if callable(stop):
                        stop()
            except Exception:
                pass
            self.dx = None

        if self.mss is not None:
            try:
                self.mss.close()
            except Exception:
                pass
            self.mss = None


class Tapper:
    """
    One Windows SendInput call containing:
        DOWN UP DOWN UP DOWN UP
    There is no intentional sleep between the three taps.
    """

    def burst(self, screen_x, screen_y, hwnd):
        started = time.perf_counter()

        try:
            user32.SetForegroundWindow(hwnd)

            px = int(round(screen_x))
            py = int(round(screen_y))

            if not user32.SetCursorPos(px, py):
                return 0.0, False, 0

            inputs = (INPUT * (BURST_TAPS * 2))()

            for i in range(BURST_TAPS):
                down = i * 2
                up = down + 1

                inputs[down].type = INPUT_MOUSE
                inputs[down].mi = MOUSEINPUT(
                    0, 0, 0, MOUSEEVENTF_LEFTDOWN, 0, 0
                )

                inputs[up].type = INPUT_MOUSE
                inputs[up].mi = MOUSEINPUT(
                    0, 0, 0, MOUSEEVENTF_LEFTUP, 0, 0
                )

            sent = user32.SendInput(
                BURST_TAPS * 2,
                inputs,
                ctypes.sizeof(INPUT),
            )

            elapsed_ms = (
                time.perf_counter() - started
            ) * 1000.0

            return (
                elapsed_ms,
                sent == BURST_TAPS * 2,
                int(sent),
            )

        except Exception:
            return (
                (time.perf_counter() - started) * 1000.0,
                False,
                0,
            )


def game_roi(frame):
    h, w = frame.shape[:2]
    return (
        int(round(w * GAME_X0)),
        int(round(w * GAME_X1)),
        int(round(h * GAME_Y0)),
        int(round(h * GAME_Y1)),
    )


def make_block_mask(frame):
    hsv = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)
    s = hsv[:, :, 1]
    v = hsv[:, :, 2]

    b = frame[:, :, 0].astype(np.int16)
    g = frame[:, :, 1].astype(np.int16)
    r = frame[:, :, 2].astype(np.int16)

    spread = np.maximum(
        np.maximum(b, g), r
    ) - np.minimum(
        np.minimum(b, g), r
    )

    mask = (
        (s >= MIN_SATURATION) &
        (v >= MIN_VALUE) &
        (spread >= MIN_COLOR_SPREAD)
    ).astype(np.uint8) * 255

    kernel = cv2.getStructuringElement(
        cv2.MORPH_RECT, (3, 3)
    )
    return cv2.morphologyEx(
        mask,
        cv2.MORPH_OPEN,
        kernel,
        iterations=1,
    )


def median_smooth_spans(spans, window):
    if not spans:
        return spans

    arr = np.full(
        (len(spans), 2),
        np.nan,
        dtype=np.float64,
    )

    for i, span in enumerate(spans):
        if span is not None:
            arr[i, 0] = float(span[0])
            arr[i, 1] = float(span[1])

    out = arr.copy()
    half = max(1, window // 2)

    for i in range(len(arr)):
        lo = max(0, i - half)
        hi = min(len(arr), i + half + 1)
        seg = arr[lo:hi]

        valid = (
            np.isfinite(seg[:, 0]) &
            np.isfinite(seg[:, 1])
        )

        if np.any(valid):
            out[i, 0] = np.median(seg[valid, 0])
            out[i, 1] = np.median(seg[valid, 1])

    result = []
    for row in out:
        if np.all(np.isfinite(row)):
            result.append(
                (float(row[0]), float(row[1]))
            )
        else:
            result.append(None)

    return result


def longest_run(row, min_width):
    xs = np.flatnonzero(row)
    if xs.size == 0:
        return None

    breaks = np.flatnonzero(np.diff(xs) > 1)
    starts = np.r_[0, breaks + 1]
    ends = np.r_[breaks, xs.size - 1]

    lengths = ends - starts + 1
    i = int(np.argmax(lengths))

    if int(lengths[i]) < min_width:
        return None

    return (
        int(xs[starts[i]]),
        int(xs[ends[i]]),
    )


def build_row_spans(frame):
    x0, x1, y0, y1 = game_roi(frame)
    roi = frame[y0:y1, x0:x1]
    mask = make_block_mask(roi)

    spans = []

    for row in mask:
        run = longest_run(
            row,
            MIN_RUN_WIDTH,
        )

        if run is None:
            spans.append(None)
        else:
            spans.append(
                (
                    float(run[0] + x0),
                    float(run[1] + x0),
                )
            )

    return (
        median_smooth_spans(
            spans,
            ROW_MEDIAN_WINDOW,
        ),
        y0,
    )


def spans_to_bands(spans, y0):
    bands = []
    i = 0

    while i < len(spans):
        if spans[i] is None:
            i += 1
            continue

        segment = [spans[i]]
        j = i + 1
        last = np.array(
            spans[i],
            dtype=np.float64,
        )

        while j < len(spans):
            current = spans[j]

            if current is None:
                if (
                    j + 2 < len(spans)
                    and spans[j + 1] is not None
                ):
                    j += 1
                    continue
                break

            cur = np.array(
                current,
                dtype=np.float64,
            )

            if np.max(np.abs(cur - last)) > ROW_SPAN_CHANGE:
                break

            segment.append(current)
            last = cur
            j += 1

        height = j - i

        if (
            MIN_BLOCK_HEIGHT
            <= height
            <= MAX_BLOCK_HEIGHT
        ):
            arr = np.asarray(
                segment,
                dtype=np.float64,
            )

            left = float(np.median(arr[:, 0]))
            right = float(np.median(arr[:, 1]))
            width = right - left + 1.0
            aspect = width / float(max(height, 1))

            if (
                MIN_BLOCK_WIDTH
                <= width
                <= MAX_BLOCK_WIDTH
                and
                MIN_BLOCK_ASPECT
                <= aspect
                <= MAX_BLOCK_ASPECT
            ):
                bands.append(
                    {
                        "x0": left,
                        "x1": right,
                        "y0": float(y0 + i),
                        "y1": float(y0 + j - 1),
                        "cx": (left + right) * 0.5,
                        "cy": (y0 + i + y0 + j - 1) * 0.5,
                        "w": width,
                        "h": float(height),
                    }
                )

        i = max(i + 1, j)

    bands.sort(
        key=lambda b: (b["y0"], b["x0"])
    )
    return bands


def detect_block_data(frame):
    spans, y0 = build_row_spans(frame)
    return spans_to_bands(spans, y0), spans, y0


def target_below_from_spans(spans, y0, moving):
    """
    Find the first stable horizontal block below the moving block.
    The few rows immediately touching the moving block are skipped because
    compression/anti-aliasing can temporarily blend the two blocks.
    """
    if moving is None:
        return None

    bottom = int(round(moving["y1"]))
    start = max(
        0,
        bottom - y0 + 2,
    )
    end = min(
        len(spans),
        bottom - y0
        + int(round(max(45.0, moving["h"] * 2.4))),
    )

    pieces = []
    i = start

    while i < end:
        if spans[i] is None:
            i += 1
            continue

        segment = [spans[i]]
        j = i + 1
        last = np.array(
            spans[i],
            dtype=np.float64,
        )

        while j < end and spans[j] is not None:
            cur = np.array(
                spans[j],
                dtype=np.float64,
            )

            if np.max(np.abs(cur - last)) > ROW_SPAN_CHANGE:
                break

            segment.append(
                (float(cur[0]), float(cur[1]))
            )
            last = cur
            j += 1

        if len(segment) >= 6:
            arr = np.asarray(
                segment,
                dtype=np.float64,
            )

            left = float(np.median(arr[:, 0]))
            right = float(np.median(arr[:, 1]))
            top = float(y0 + i)
            bottom2 = float(y0 + j - 1)
            width = right - left + 1.0
            height = bottom2 - top + 1.0
            aspect = width / float(max(height, 1))

            if (
                MIN_BLOCK_WIDTH
                <= width
                <= MAX_BLOCK_WIDTH
                and
                MIN_BLOCK_HEIGHT
                <= height
                <= MAX_BLOCK_HEIGHT
                and
                MIN_BLOCK_ASPECT
                <= aspect
                <= MAX_BLOCK_ASPECT
                and
                top >= moving["y1"] - 5.0
            ):
                pieces.append(
                    {
                        "x0": left,
                        "x1": right,
                        "y0": top,
                        "y1": bottom2,
                        "cx": (left + right) * 0.5,
                        "cy": (top + bottom2) * 0.5,
                        "w": width,
                        "h": height,
                    }
                )

        i = max(i + 1, j)

    if not pieces:
        return None

    pieces.sort(
        key=lambda p: (
            max(0.0, p["y0"] - moving["y1"]),
            abs(p["w"] - moving["w"]),
        )
    )
    return pieces[0]


def normalize_edge_clipped_band(
    band,
    frame_width,
    nominal_width,
):
    """
    When a moving block is partially outside the game area, the visible
    rectangle is narrower than the real block. Recover its centre using the
    previous full-width measurement.
    """
    if band is None:
        return None

    out = dict(band)
    left_edge = frame_width * GAME_X0
    right_edge = frame_width * GAME_X1

    observed = float(band["w"])

    if nominal_width is None or nominal_width <= 0:
        nominal_width = observed

    nominal_width = max(
        observed,
        float(nominal_width),
    )

    out["effective_w"] = nominal_width

    if (
        band["x0"] <= left_edge + 1.5
        and
        observed < nominal_width * 0.80
    ):
        out["cx"] = (
            band["x1"] -
            nominal_width * 0.50
        )
        out["w"] = nominal_width

    elif (
        band["x1"] >= right_edge - 1.5
        and
        observed < nominal_width * 0.80
    ):
        out["cx"] = (
            band["x0"] +
            nominal_width * 0.50
        )
        out["w"] = nominal_width

    return out


class MotionTracker:
    def __init__(self):
        self.history = deque(maxlen=MOTION_HISTORY)
        self.last_seen = None
        self.missed = 0

    def reset(self):
        self.history.clear()
        self.last_seen = None
        self.missed = 0

    def nominal_width(self):
        if not self.history:
            return None

        widths = [
            item[3]
            for item in self.history
            if item[3] >= 35.0
        ]

        if widths:
            return float(np.median(widths))

        return float(
            np.median(
                [item[3] for item in self.history]
            )
        )

    def update(self, band, now):
        if band is None:
            self.missed += 1
            return

        if self.last_seen is not None:
            y_error = abs(
                band["cy"] -
                self.last_seen["cy"]
            )
            width_error = abs(
                band["w"] -
                self.last_seen["w"]
            )

            if (
                self.missed > MAX_TRACK_GAP
                or
                y_error > MAX_Y_TRACK_ERROR
                or
                width_error > MAX_WIDTH_TRACK_ERROR
            ):
                self.history.clear()

        self.missed = 0
        self.history.append(
            (
                now,
                float(band["cx"]),
                float(band["cy"]),
                float(band["w"]),
                float(band["h"]),
            )
        )
        self.last_seen = dict(band)

    def current_x(self):
        if not self.history:
            return None
        return self.history[-1][1]

    def current_y(self):
        if not self.history:
            return None
        return self.history[-1][2]

    def current_width(self):
        if not self.history:
            return None
        return self.history[-1][3]

    def velocity(self):
        if len(self.history) < 2:
            return 0.0

        items = list(self.history)
        values = []

        for a, b in zip(items[:-1], items[1:]):
            dt = b[0] - a[0]

            if dt <= 1e-6:
                continue

            values.append(
                (b[1] - a[1]) / dt
            )

        if not values:
            return 0.0

        return float(
            np.median(values[-5:])
        )

    def stable(self):
        if len(self.history) < MIN_MOTION_FRAMES:
            return False

        if abs(self.velocity()) < MIN_MOVING_SPEED:
            return False

        items = list(self.history)
        signs = []

        for a, b in zip(
            items[-4:-1],
            items[-3:],
        ):
            dt = b[0] - a[0]
            if dt <= 1e-6:
                continue

            dx = b[1] - a[1]

            if abs(dx) > 0.5:
                signs.append(
                    1 if dx > 0 else -1
                )

        return (
            len(signs) >= 2
            and
            signs[-1] == signs[-2]
        )

    def detect_reversal(self):
        if len(self.history) < 4:
            return False

        a = self.history[-3][1]
        b = self.history[-2][1]
        c = self.history[-1][1]

        dx1 = b - a
        dx2 = c - b

        if abs(dx1) < 1.0 or abs(dx2) < 1.0:
            return False

        return (
            (dx1 > 0 and dx2 < 0)
            or
            (dx1 < 0 and dx2 > 0)
        )


def estimate_bounds(frame, moving_width):
    h, w = frame.shape[:2]

    half = max(
        10.0,
        float(moving_width) * 0.5,
    )

    return (
        w * GAME_X0 - half,
        w * GAME_X1 + half,
    )


def reflected_position(
    x,
    vx,
    seconds,
    left,
    right,
):
    if seconds <= 0.0:
        return float(x)

    if abs(vx) < 1e-9:
        return float(x)

    span = max(1.0, right - left)
    u = (x - left) + vx * seconds
    period = 2.0 * span
    m = u % period

    if m <= span:
        return left + m

    return right - (m - span)


def time_to_next_crossing(
    x,
    vx,
    target_x,
    left,
    right,
):
    if abs(vx) < 1e-9:
        return None

    target_x = max(
        left,
        min(right, target_x),
    )
    speed = abs(vx)

    if vx > 0:
        if target_x >= x:
            return (
                target_x - x
            ) / speed

        return (
            right - x
        ) / speed + (
            right - target_x
        ) / speed

    if target_x <= x:
        return (
            x - target_x
        ) / speed

    return (
        x - left
    ) / speed + (
        target_x - left
    ) / speed


def choose_drop_point(moving, target):
    """
    Maximum overlap occurs around target centre. The safe interval prevents
    the requested tap point from being placed at a dangerous edge.
    """
    needed = (
        min(
            moving["w"],
            target["w"],
        )
        *
        SAFE_OVERLAP_RATIO
    )

    low = (
        target["x0"]
        -
        moving["w"] * 0.5
        +
        needed
    )

    high = (
        target["x1"]
        +
        moving["w"] * 0.5
        -
        needed
    )

    ideal = target["cx"]

    return (
        float(max(low, min(high, ideal))),
        float(low),
        float(high),
    )


def overlap_width(cx, moving_width, target):
    left = cx - moving_width * 0.5
    right = cx + moving_width * 0.5

    return max(
        0.0,
        min(right, target["x1"])
        -
        max(left, target["x0"]),
    )


def click_lead_seconds(frame_dt):
    lead = (
        0.5 * max(0.0, frame_dt)
        +
        0.004
    )

    lead = max(
        CLICK_LEAD_MIN_MS / 1000.0,
        lead,
    )

    lead = min(
        CLICK_LEAD_MAX_MS / 1000.0,
        lead,
    )

    return lead


def draw_block(img, block, bgr):
    if block is None:
        return

    x0 = int(round(block["x0"]))
    y0 = int(round(block["y0"]))
    x1 = int(round(block["x1"]))
    y1 = int(round(block["y1"]))

    cv2.rectangle(
        img,
        (x0, y0),
        (x1, y1),
        bgr,
        2,
        cv2.LINE_AA,
    )


def put_text(img, text, y):
    cv2.putText(
        img,
        text,
        (7, y),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.45,
        (255, 255, 255),
        1,
        cv2.LINE_AA,
    )


def move_output_window(region):
    try:
        if not region:
            return

        left, top, right, bottom = region
        screen_width = user32.GetSystemMetrics(0)
        x = right + 10

        if x + 540 > screen_width:
            x = max(10, left - 540)

        cv2.moveWindow(
            OUTPUT_TITLE,
            int(x),
            int(top),
        )
    except Exception:
        pass


def main():
    print("=" * 72)
    print("              TOWER STACK - ACCURATE BOT TEST")
    print("=" * 72)
    print("scrcpy title:", SCRCPY_TITLE)
    print("SPACE = auto click ON/OFF | R = reset stats | Q = quit")
    print("[READY] Live scan starts immediately.")
    print("[READY] Start Tower Stack after this message.")
    print("[MODE] Motion prediction + 3 ultra-fast taps per drop.")

    hwnd = find_scrcpy()

    if not hwnd:
        print(
            "[ERROR] Window {!r} not found.".format(
                SCRCPY_TITLE
            )
        )
        print(
            "Start: scrcpy.exe "
            "--window-title=CHESS_MOBILE --always-on-top"
        )
        return

    region = client_region(hwnd)

    if not region:
        print(
            "[ERROR] Could not read scrcpy client region."
        )
        return

    print("[SCRCPY] Existing window found.")
    print("[SCRCPY] Client region:", region)

    capture = Capture()
    capture.set_region(region)
    print("[CAPTURE]", capture.backend)

    tapper = Tapper()
    tracker = MotionTracker()

    if SHOW_OUTPUT:
        cv2.namedWindow(
            OUTPUT_TITLE,
            cv2.WINDOW_NORMAL,
        )
        cv2.resizeWindow(
            OUTPUT_TITLE,
            520,
            900,
        )
        move_output_window(region)

    auto_click = AUTO_CLICK_START
    state = "LIVE SCAN"

    run_start = time.perf_counter()
    last_time = time.perf_counter()
    last_frame_dt = 1.0 / 60.0

    frames = 0
    motion_frames = 0
    drops = 0
    lands = 0
    tap_failures = 0
    rescan_events = 0

    center_errors = []
    overlap_values = []
    dispatch_values = []
    landing_values = []

    game_started = False
    base_missing = 0

    moving_band = None
    target_band = None
    drop_point = None

    drop_active = False
    tap_started_at = 0.0
    tap_x = 0.0
    tap_moving_y = 0.0
    tap_moving_width = 0.0
    tap_target_width = 0.0
    tap_target = None
    tap_predicted_center = 0.0
    tap_prediction_error = 0.0
    post_tap_missing = 0

    frame_index = 0

    try:
        while True:
            now = time.perf_counter()
            frame_dt = now - last_time
            last_time = now

            if 0.001 <= frame_dt <= 0.2:
                last_frame_dt = frame_dt

            current_hwnd = find_scrcpy()
            if current_hwnd:
                hwnd = current_hwnd
                current_region = client_region(hwnd)

                if current_region:
                    region = current_region
                    capture.set_region(region)

            frame = capture.grab()

            if frame is None:
                state = "NO FRAME"
                continue

            frames += 1
            frame_index += 1

            frame_h, frame_w = frame.shape[:2]

            bands, spans, span_y0 = detect_block_data(
                frame
            )

            # Base is only used after gameplay has actually started.
            base_exists = any(
                b["y1"]
                >=
                (
                    frame_h * GAME_Y1
                    -
                    BASE_BOTTOM_TOLERANCE_PX
                )
                and
                b["w"] >= 50
                for b in bands
            )

            if game_started:
                if base_exists:
                    base_missing = 0
                else:
                    base_missing += 1

            # --------------------------------------------------------
            # Candidate = top-most visible block. Once the moving block
            # exists, it is above the current tower top.
            # --------------------------------------------------------
            candidate = None

            if (
                tracker.last_seen is not None
                and
                tracker.missed <= MAX_TRACK_GAP
            ):
                best_distance = float("inf")

                for band in bands:
                    y_error = abs(
                        band["cy"]
                        -
                        tracker.last_seen["cy"]
                    )
                    width_error = abs(
                        band["w"]
                        -
                        tracker.last_seen["w"]
                    )

                    if (
                        y_error <= MAX_Y_TRACK_ERROR
                        and
                        width_error <= MAX_WIDTH_TRACK_ERROR
                        and
                        y_error < best_distance
                    ):
                        candidate = band
                        best_distance = y_error

            if candidate is None and bands:
                candidate = bands[0]

            candidate_target = None

            if candidate is not None:
                candidate_target = (
                    target_below_from_spans(
                        spans,
                        span_y0,
                        candidate,
                    )
                )

            # --------------------------------------------------------
            # GAME OVER
            # --------------------------------------------------------
            if (
                game_started
                and
                not base_exists
                and
                base_missing >= GAME_OVER_MISS_FRAMES
                and
                not drop_active
            ):
                state = "GAME OVER / STOPPED"
                break

            # --------------------------------------------------------
            # POST TAP: verify vertical drop / disappearance.
            # --------------------------------------------------------
            if drop_active:
                if candidate is None:
                    post_tap_missing += 1
                else:
                    post_tap_missing = 0

                vertical_change = 0.0

                if candidate is not None:
                    vertical_change = (
                        candidate["y0"]
                        -
                        tap_moving_y
                    )

                elapsed_ms = (
                    time.perf_counter()
                    -
                    tap_started_at
                ) * 1000.0

                landing = (
                    vertical_change
                    >=
                    LANDING_VERTICAL_CHANGE_PX
                    or
                    post_tap_missing
                    >=
                    LANDING_MISS_FRAMES
                )

                # A newly spawned moving block is substantially above the
                # old moving block. That also means the previous block landed.
                if (
                    candidate is not None
                    and
                    candidate["y0"]
                    <
                    tap_moving_y
                    -
                    max(
                        6.0,
                        tap_moving_width * 0.20,
                    )
                ):
                    landing = True

                if landing:
                    lands += 1

                    landing_values.append(
                        elapsed_ms
                    )

                    if tap_target is not None:
                        overlap = overlap_width(
                            tap_predicted_center,
                            tap_moving_width,
                            tap_target,
                        )

                        denom = max(
                            1.0,
                            min(
                                tap_moving_width,
                                tap_target_width,
                            ),
                        )

                        overlap_ratio = (
                            overlap / denom
                        )

                        overlap_values.append(
                            overlap_ratio
                        )

                        center_errors.append(
                            float(tap_prediction_error)
                        )

                    drop_active = False
                    tracker.reset()
                    moving_band = None
                    target_band = None
                    drop_point = None
                    tap_target = None
                    tap_predicted_center = 0.0
                    tap_prediction_error = 0.0
                    post_tap_missing = 0
                    state = "LANDING CONFIRMED"

                elif (
                    elapsed_ms
                    >=
                    LANDING_VERIFY_TIMEOUT_MS
                ):
                    # Do not blindly send another burst just because a
                    # visual transition was not confirmed. Re-scan the scene
                    # from scratch for the next reliable state.
                    drop_active = False
                    tracker.reset()
                    moving_band = None
                    target_band = None
                    drop_point = None
                    tap_target = None
                    tap_predicted_center = 0.0
                    tap_prediction_error = 0.0
                    post_tap_missing = 0
                    rescan_events += 1
                    state = "LANDING UNCERTAIN - RESCAN"

                else:
                    state = "VERIFY LANDING"

            # --------------------------------------------------------
            # NORMAL TRACK / PREDICT / DROP
            # --------------------------------------------------------
            else:
                valid_start_candidate = (
                    candidate is not None
                    and
                    candidate_target is not None
                )

                if not valid_start_candidate:
                    tracker.update(
                        None,
                        now,
                    )
                    moving_band = None
                    target_band = None
                    drop_point = None
                    state = (
                        "LIVE SCAN - LOOKING FOR MOVING BLOCK"
                    )

                elif (
                    not game_started
                    and
                    candidate["cy"]
                    <
                    frame_h * STARTUP_MIN_MOVING_Y_RATIO
                ):
                    # Countdown / GO animation can be bright and large.
                    # Never let it enter the moving-block tracker.
                    tracker.reset()
                    moving_band = None
                    target_band = None
                    drop_point = None
                    state = "LIVE SCAN - IGNORE START ANIMATION"

                else:
                    corrected = normalize_edge_clipped_band(
                        candidate,
                        frame_w,
                        tracker.nominal_width(),
                    )

                    tracker.update(
                        corrected,
                        now,
                    )

                    if tracker.detect_reversal():
                        recent = list(
                            tracker.history
                        )[-2:]

                        tracker.history = deque(
                            recent,
                            maxlen=MOTION_HISTORY,
                        )

                        state = "BOUNCE - TRACK RESET"

                    if (
                        not game_started
                        and
                        tracker.stable()
                    ):
                        game_started = True
                        print(
                            "[GAME] First real moving block confirmed."
                        )

                    moving_band = candidate
                    target_band = candidate_target
                    motion_frames += 1

                    if not tracker.stable():
                        state = (
                            "TRACKING - CALCULATING MOTION"
                        )
                    else:
                        vx = tracker.velocity()
                        current_x = tracker.current_x()
                        nominal_width = (
                            tracker.nominal_width()
                            or
                            tracker.current_width()
                        )

                        if (
                            current_x is None
                            or
                            nominal_width is None
                            or
                            abs(vx) < MIN_MOVING_SPEED
                        ):
                            state = (
                                "TRACKING - MOTION NOT STABLE"
                            )
                        else:
                            left, right = (
                                estimate_bounds(
                                    frame,
                                    nominal_width,
                                )
                            )

                            desired, safe_low, safe_high = (
                                choose_drop_point(
                                    corrected,
                                    candidate_target,
                                )
                            )

                            time_hit = (
                                time_to_next_crossing(
                                    current_x,
                                    vx,
                                    desired,
                                    left,
                                    right,
                                )
                            )

                            if (
                                time_hit is None
                                or
                                time_hit > PREDICTION_MAX_SEC
                            ):
                                state = (
                                    "TRACKING - REPLAN"
                                )
                            else:
                                lead = (
                                    click_lead_seconds(
                                        last_frame_dt
                                    )
                                )

                                predicted_x = (
                                    reflected_position(
                                        current_x,
                                        vx,
                                        lead,
                                        left,
                                        right,
                                    )
                                )

                                tolerance = max(
                                    PREDICTION_TOLERANCE_MIN,
                                    candidate_target["w"]
                                    *
                                    PREDICTION_TOLERANCE_RATIO,
                                )

                                prediction_error = abs(
                                    predicted_x
                                    -
                                    desired
                                )

                                drop_point = desired

                                # No arbitrary waiting. The instant the
                                # predicted point enters the target window,
                                # decide.
                                ready = (
                                    prediction_error
                                    <=
                                    tolerance
                                    or
                                    time_hit
                                    <=
                                    (
                                        lead
                                        +
                                        max(
                                            last_frame_dt * 0.50,
                                            0.004,
                                        )
                                    )
                                )

                                if not ready:
                                    state = (
                                        "TRACKING - PREDICTING DROP"
                                    )
                                elif not auto_click:
                                    state = (
                                        "DROP READY - AUTO CLICK OFF"
                                    )
                                else:
                                    tap_target = dict(
                                        candidate_target
                                    )

                                    tap_x = float(
                                        desired
                                    )
                                    tap_moving_y = float(
                                        candidate["cy"]
                                    )
                                    tap_moving_width = float(
                                        nominal_width
                                    )
                                    tap_target_width = float(
                                        candidate_target["w"]
                                    )

                                    # Record the predicted moving-block centre
                                    # at the actual click lead. This is the real
                                    # timing/prediction accuracy metric; tap_x is
                                    # intentionally the target centre.
                                    tap_predicted_center = float(
                                        predicted_x
                                    )
                                    tap_prediction_error = float(
                                        abs(
                                            predicted_x - desired
                                        )
                                    )

                                    tap_started_at = (
                                        time.perf_counter()
                                    )

                                    screen_x = (
                                        region[0]
                                        +
                                        tap_x
                                    )
                                    screen_y = (
                                        region[1]
                                        +
                                        tap_moving_y
                                    )

                                    (
                                        dispatch_ms,
                                        ok,
                                        sent,
                                    ) = tapper.burst(
                                        screen_x,
                                        screen_y,
                                        hwnd,
                                    )

                                    dispatch_values.append(
                                        dispatch_ms
                                    )

                                    if ok:
                                        drops += 1
                                        drop_active = True
                                        post_tap_missing = 0
                                        state = (
                                            "3-TAP BURST @ "
                                            "{},{}".format(
                                                int(round(tap_x)),
                                                int(round(
                                                    tap_moving_y
                                                )),
                                            )
                                        )
                                    else:
                                        tap_failures += 1
                                        tracker.reset()
                                        state = (
                                            "TAP FAILED {}/6".format(
                                                sent
                                            )
                                        )

            # --------------------------------------------------------
            # OUTPUT
            # --------------------------------------------------------
            if (
                SHOW_OUTPUT
                and
                frame_index % DISPLAY_EVERY == 0
            ):
                out = frame.copy()

                if moving_band is not None:
                    draw_block(
                        out,
                        moving_band,
                        (0, 255, 0),
                    )

                if target_band is not None:
                    draw_block(
                        out,
                        target_band,
                        (255, 255, 0),
                    )

                    target_x = int(
                        round(
                            target_band["cx"]
                        )
                    )

                    cv2.line(
                        out,
                        (
                            target_x,
                            int(target_band["y0"]),
                        ),
                        (
                            target_x,
                            int(target_band["y1"]),
                        ),
                        (255, 255, 255),
                        1,
                        cv2.LINE_AA,
                    )

                if drop_point is not None:
                    cv2.circle(
                        out,
                        (
                            int(round(drop_point)),
                            int(round(
                                moving_band["cy"]
                                if moving_band is not None
                                else tap_moving_y
                            )),
                        ),
                        5,
                        (0, 0, 255),
                        -1,
                    )

                elapsed = max(
                    time.perf_counter()
                    -
                    run_start,
                    1e-6,
                )

                lines = [
                    "STATE: " + state,
                    "FPS: {:.1f}".format(
                        frames / elapsed
                    ),
                    "Blocks: {}".format(
                        len(bands)
                    ),
                    "Velocity: {:.1f}px/s".format(
                        tracker.velocity()
                    ),
                    "Moving X: {}".format(
                        "-"
                        if moving_band is None
                        else str(int(round(
                            moving_band["cx"]
                        )))
                    ),
                    "Target X: {}".format(
                        "-"
                        if target_band is None
                        else str(int(round(
                            target_band["cx"]
                        )))
                    ),
                    "Drop X: {}".format(
                        "-"
                        if drop_point is None
                        else str(int(round(
                            drop_point
                        )))
                    ),
                    "Drops: {}  Lands: {}".format(
                        drops,
                        lands,
                    ),
                    "Tap failures: {}".format(
                        tap_failures
                    ),
                    "Burst: {} taps / event".format(
                        BURST_TAPS
                    ),
                ]

                y = 22
                for line in lines:
                    put_text(
                        out,
                        line,
                        y,
                    )
                    y += 19

                cv2.imshow(
                    OUTPUT_TITLE,
                    out,
                )

                key = cv2.waitKey(1) & 0xFF

                if key == ord("q"):
                    break

                if key == 32:
                    auto_click = not auto_click
                    print(
                        "[CONTROL] AUTO_CLICK =",
                        auto_click,
                    )

                elif key == ord("r"):
                    tracker.reset()
                    game_started = False
                    base_missing = 0
                    moving_band = None
                    target_band = None
                    drop_point = None
                    drop_active = False
                    tap_target = None
                    tap_predicted_center = 0.0
                    tap_prediction_error = 0.0
                    post_tap_missing = 0

                    frames = 0
                    motion_frames = 0
                    drops = 0
                    lands = 0
                    tap_failures = 0
                    rescan_events = 0

                    center_errors.clear()
                    overlap_values.clear()
                    dispatch_values.clear()
                    landing_values.clear()

                    run_start = time.perf_counter()
                    print(
                        "[CONTROL] Stats reset"
                    )

    except KeyboardInterrupt:
        pass

    finally:
        try:
            capture.close()
        except Exception:
            pass

        cv2.destroyAllWindows()

    elapsed = max(
        time.perf_counter() - run_start,
        1e-6,
    )

    print("")
    print("=" * 72)
    print("                    FINAL TOWER TEST")
    print("=" * 72)
    print("Frames                  :", frames)
    print(
        "Loop FPS                : {:.2f}".format(
            frames / elapsed
        )
    )
    print(
        "Drop decisions          :",
        drops,
    )
    print(
        "Landing confirmed       :",
        lands,
    )
    print(
        "Tap failures            :",
        tap_failures,
    )
    print(
        "Rescan / uncertain      :",
        rescan_events,
    )

    if center_errors:
        print(
            "Avg center error        : {:.2f} px".format(
                float(np.mean(center_errors))
            )
        )
        print(
            "Max center error        : {:.2f} px".format(
                float(np.max(center_errors))
            )
        )
    else:
        print("Avg center error        : -")
        print("Max center error        : -")

    if overlap_values:
        print(
            "Avg overlap             : {:.2f}%".format(
                100.0 * float(np.mean(
                    overlap_values
                ))
            )
        )
        print(
            ">=90% overlap events    : {}".format(
                sum(
                    v >= 0.90
                    for v in overlap_values
                )
            )
        )
    else:
        print("Avg overlap             : -")
        print(">=90% overlap events    : -")

    if landing_values:
        print(
            "Avg landing time        : {:.2f} ms".format(
                float(np.mean(
                    landing_values
                ))
            )
        )
    else:
        print("Avg landing time        : -")

    if dispatch_values:
        print(
            "Avg burst dispatch      : {:.2f} ms".format(
                float(np.mean(
                    dispatch_values
                ))
            )
        )
    else:
        print("Avg burst dispatch      : -")

    print("=" * 72)


if __name__ == "__main__":
    main()
