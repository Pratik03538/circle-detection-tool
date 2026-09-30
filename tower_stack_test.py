import ctypes
from ctypes import wintypes
from collections import deque
import math
import random
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
# Keep the debug window OFF during live play. It consumes CPU and can steal
# foreground focus from scrcpy. Turn it on only while debugging detection.
SHOW_OUTPUT = False
DISPLAY_EVERY = 3

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
MIN_RUN_WIDTH = 4

MIN_BLOCK_WIDTH = 6
MAX_BLOCK_WIDTH = 200
MIN_BLOCK_HEIGHT = 4
MAX_BLOCK_HEIGHT = 34
MIN_BLOCK_ASPECT = 0.75
MAX_BLOCK_ASPECT = 14.0
ROW_SPAN_CHANGE = 6.0
ROW_MEDIAN_WINDOW = 5

# Half-resolution detection keeps block tracking fast while coordinates are
# converted back to the full scrcpy frame.
BLOCK_DETECT_SCALE = 0.75
PRECISION_SCAN_SCALE = 1.0
PRECISION_SCAN_TRIGGER_WIDTH = 32.0

# Motion tracking.
MOTION_HISTORY = 7
MIN_MOVING_SPEED = 45.0
MIN_MOTION_FRAMES = 3
MAX_TRACK_GAP = 2
MAX_Y_TRACK_ERROR = 15.0
MAX_WIDTH_TRACK_ERROR = 60.0
STARTUP_MIN_MOVING_Y_RATIO = 0.60

# Prediction.
CLICK_LEAD_MIN_MS = 10.0
CLICK_LEAD_MAX_MS = 55.0
INITIAL_CLICK_LEAD_MS = 30.0
CLICK_LEAD_EMA_ALPHA = 0.35
PREDICTION_MAX_SEC = 2.50
PREDICTION_TOLERANCE_MIN = 6.0
PREDICTION_TOLERANCE_RATIO = 0.10
SAFE_OVERLAP_RATIO = 0.30

# User requested three extremely fast taps.
# One click only per block.
SINGLE_CLICK = True

# The tap is deliberately NOT forced to the exact centre. For each drop,
# choose one random point in a small area around the moving block.
RANDOM_CLICK_X_HALF_WIDTH = 0.35
RANDOM_CLICK_Y_HALF_HEIGHT = 0.75
RANDOM_CLICK_MIN_DISTANCE_FROM_CENTER = 0.10

# Landing verification.
LANDING_VERTICAL_CHANGE_PX = 3.0
LANDING_VERIFY_TIMEOUT_MS = 300.0
LANDING_MISS_FRAMES = 3

# Game-over detection.
BASE_BOTTOM_TOLERANCE_PX = 45
GAME_OVER_MISS_FRAMES = 18

# ------------------------------------------------------------
# MATCH / GAME READY GATE
# ------------------------------------------------------------
# The bot can be started while another phone page is visible.
# Gameplay detection is disabled until the real Tower Stack
# pre-game screen is visually confirmed for several frames.
MATCH_READY_CONFIRM_FRAMES = 10

MATCH_READY_X0 = 0.17
MATCH_READY_X1 = 0.83
MATCH_READY_Y0 = 0.30
MATCH_READY_Y1 = 0.955

MATCH_SCORE_X0 = 0.30
MATCH_SCORE_X1 = 0.75
MATCH_SCORE_Y0 = 0.22
MATCH_SCORE_Y1 = 0.31

MATCH_START_X0 = 0.30
MATCH_START_X1 = 0.70
MATCH_START_Y0 = 0.88
MATCH_START_Y1 = 0.96


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
    """
    Return the visible scrcpy client region in physical screen coordinates.

    A maximized/tall scrcpy window can extend a few pixels below the physical
    monitor boundary. DXCam rejects any region outside the monitor, so clamp
    the client rectangle before handing it to the capture backend.
    """
    rect = wintypes.RECT()

    if not user32.GetClientRect(
        hwnd,
        ctypes.byref(rect)
    ):
        return None

    w = rect.right - rect.left
    h = rect.bottom - rect.top

    if w <= 0 or h <= 0:
        return None

    pt = wintypes.POINT(0, 0)

    if not user32.ClientToScreen(
        hwnd,
        ctypes.byref(pt)
    ):
        return None

    left = int(pt.x)
    top = int(pt.y)
    right = int(pt.x + w)
    bottom = int(pt.y + h)

    # DXCam captures the physical desktop. Keep the requested rectangle
    # completely inside the primary desktop bounds.
    screen_w = int(
        user32.GetSystemMetrics(0)
    )
    screen_h = int(
        user32.GetSystemMetrics(1)
    )

    left = max(
        0,
        min(
            left,
            screen_w - 1
        )
    )
    top = max(
        0,
        min(
            top,
            screen_h - 1
        )
    )
    right = max(
        left + 1,
        min(
            right,
            screen_w
        )
    )
    bottom = max(
        top + 1,
        min(
            bottom,
            screen_h
        )
    )

    return (
        left,
        top,
        right,
        bottom
    )


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

        # Defensive clamp on every capture too. This protects against a
        # transient window move/resize between region refreshes.
        screen_w = int(
            user32.GetSystemMetrics(0)
        )
        screen_h = int(
            user32.GetSystemMetrics(1)
        )

        left = max(0, min(left, screen_w - 1))
        top = max(0, min(top, screen_h - 1))
        right = max(left + 1, min(right, screen_w))
        bottom = max(top + 1, min(bottom, screen_h))

        self.region = (
            left,
            top,
            right,
            bottom
        )

        if self.backend == "DXCAM":
            try:
                return self.dx.grab(
                    region=(
                        left,
                        top,
                        right,
                        bottom
                    )
                )
            except ValueError as exc:
                # DXCam is strict about monitor bounds. Re-read the window
                # geometry once before failing the frame.
                raise RuntimeError(
                    "DXCam region invalid after screen clamp: "
                    + str(exc)
                ) from exc

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
    """Send exactly one real left click to the selected screen point."""

    def click(self, screen_x, screen_y, hwnd):
        started = time.perf_counter()

        try:
            if user32.GetForegroundWindow() != hwnd:
                user32.SetForegroundWindow(hwnd)

            px = int(round(screen_x))
            py = int(round(screen_y))

            if not user32.SetCursorPos(px, py):
                return 0.0, False, 0

            inputs = (INPUT * 2)()

            inputs[0].type = INPUT_MOUSE
            inputs[0].mi = MOUSEINPUT(
                0,
                0,
                0,
                MOUSEEVENTF_LEFTDOWN,
                0,
                0,
            )

            inputs[1].type = INPUT_MOUSE
            inputs[1].mi = MOUSEINPUT(
                0,
                0,
                0,
                MOUSEEVENTF_LEFTUP,
                0,
                0,
            )

            sent = user32.SendInput(
                2,
                inputs,
                ctypes.sizeof(INPUT),
            )

            elapsed_ms = (
                time.perf_counter() - started
            ) * 1000.0

            return (
                elapsed_ms,
                sent == 2,
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


def detect_match_ready_screen(frame):
    """
    Confirm the actual Tower Stack pre-game layout.

    This intentionally does not use OCR or generic bright-object detection.
    It requires the same structural layout seen in the supplied recording:
      - large dark play board;
      - yellow SCORE widget;
      - neutral FLOOR widget;
      - wide light TAP TO START button.
    """
    H, W = frame.shape[:2]

    # ---- Main game board ----
    bx0 = int(round(W * MATCH_READY_X0))
    bx1 = int(round(W * MATCH_READY_X1))
    by0 = int(round(H * MATCH_READY_Y0))
    by1 = int(round(H * MATCH_READY_Y1))

    board = frame[by0:by1, bx0:bx1]
    if board.size == 0:
        return False

    board_gray = cv2.cvtColor(
        board,
        cv2.COLOR_BGR2GRAY
    )

    if (
        float(np.mean(board_gray)) >= 90.0
        or
        float(np.mean(board_gray < 85)) < 0.70
    ):
        return False

    # ---- SCORE / FLOOR widgets ----
    sx0 = int(round(W * MATCH_SCORE_X0))
    sx1 = int(round(W * MATCH_SCORE_X1))
    sy0 = int(round(H * MATCH_SCORE_Y0))
    sy1 = int(round(H * MATCH_SCORE_Y1))

    score_roi = frame[sy0:sy1, sx0:sx1]
    if score_roi.size == 0:
        return False

    hsv = cv2.cvtColor(
        score_roi,
        cv2.COLOR_BGR2HSV
    )

    yellow = cv2.inRange(
        hsv,
        np.array([15, 90, 140], dtype=np.uint8),
        np.array([40, 255, 255], dtype=np.uint8)
    )

    yellow_ratio = float(
        np.mean(yellow > 0)
    )

    # Floor box in the recording is dark blue/gray, so avoid requiring a
    # bright gray area. Instead, check that the expected right-side widget
    # contains a compact low-saturation rectangle-like region.
    floor_x0 = int(round(W * 0.54))
    floor_x1 = int(round(W * 0.74))
    floor_roi = frame[
        int(round(H * 0.245)):
        int(round(H * 0.295)),
        floor_x0:floor_x1
    ]

    floor_gray = cv2.cvtColor(
        floor_roi,
        cv2.COLOR_BGR2GRAY
    ) if floor_roi.size else None

    floor_ok = False

    if floor_gray is not None and floor_gray.size:
        # A widget-sized area should be measurably brighter than the
        # surrounding dark header/background, even when its fill is gray.
        floor_mean = float(np.mean(floor_gray))
        floor_p70 = float(np.percentile(floor_gray, 70))
        floor_ok = (
            floor_mean >= 25.0
            and
            floor_p70 >= 45.0
        )

    if yellow_ratio < 0.045 or not floor_ok:
        return False

    # ---- TAP TO START button ----
    tx0 = int(round(W * MATCH_START_X0))
    tx1 = int(round(W * MATCH_START_X1))
    ty0 = int(round(H * MATCH_START_Y0))
    ty1 = int(round(H * MATCH_START_Y1))

    start_roi = frame[ty0:ty1, tx0:tx1]
    if start_roi.size == 0:
        return False

    start_gray = cv2.cvtColor(
        start_roi,
        cv2.COLOR_BGR2GRAY
    )

    bright = (
        start_gray >= 105
    ).astype(np.uint8) * 255

    bright = cv2.morphologyEx(
        bright,
        cv2.MORPH_CLOSE,
        np.ones((5, 5), np.uint8),
        iterations=1
    )

    contours, _ = cv2.findContours(
        bright,
        cv2.RETR_EXTERNAL,
        cv2.CHAIN_APPROX_SIMPLE
    )

    for contour in contours:
        area = cv2.contourArea(contour)

        if area < (
            start_roi.shape[0]
            *
            start_roi.shape[1]
            *
            0.06
        ):
            continue

        x, y, w, h = cv2.boundingRect(contour)
        aspect = w / float(max(h, 1))

        if (
            2.5 <= aspect <= 9.5
            and
            w >= start_roi.shape[1] * 0.35
            and
            h >= start_roi.shape[0] * 0.15
        ):
            return True

    return False


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


def build_row_spans(frame, scale=None):
    """
    Fast block mask at half resolution.

    The returned spans use full-frame x coordinates while each list element
    represents approximately 1 / BLOCK_DETECT_SCALE source rows.
    """
    x0, x1, y0, y1 = game_roi(frame)

    roi = frame[
        y0:y1,
        x0:x1
    ]

    if scale is None:
        scale = BLOCK_DETECT_SCALE

    small_w = max(
        1,
        int(round(roi.shape[1] * scale))
    )

    small_h = max(
        1,
        int(round(roi.shape[0] * scale))
    )

    small = cv2.resize(
        roi,
        (small_w, small_h),
        interpolation=cv2.INTER_AREA
    )

    hsv = cv2.cvtColor(
        small,
        cv2.COLOR_BGR2HSV
    )

    s = hsv[:, :, 1]
    v = hsv[:, :, 2]

    b = small[:, :, 0].astype(np.int16)
    g = small[:, :, 1].astype(np.int16)
    r = small[:, :, 2].astype(np.int16)

    spread = (
        np.maximum(
            np.maximum(b, g),
            r
        )
        -
        np.minimum(
            np.minimum(b, g),
            r
        )
    )

    mask = (
        (s >= MIN_SATURATION)
        &
        (v >= MIN_VALUE)
        &
        (spread >= MIN_COLOR_SPREAD)
    ).astype(np.uint8) * 255

    mask = cv2.morphologyEx(
        mask,
        cv2.MORPH_OPEN,
        np.ones((3, 3), np.uint8),
        iterations=1
    )

    min_run = max(
        6,
        int(round(
            MIN_RUN_WIDTH * scale
        ))
    )

    spans = []

    for row in mask:
        run = longest_run(
            row,
            min_run,
        )

        if run is None:
            spans.append(None)
            continue

        spans.append(
            (
                float(
                    x0 +
                    run[0] / scale
                ),
                float(
                    x0 +
                    run[1] / scale
                ),
            )
        )

    return (
        median_smooth_spans(
            spans,
            ROW_MEDIAN_WINDOW,
        ),
        y0,
        scale,
    )


def spans_to_bands(
    spans,
    y0,
    row_scale=1.0,
):
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
                    and
                    spans[j + 1] is not None
                ):
                    j += 1
                    continue
                break

            cur = np.array(
                current,
                dtype=np.float64,
            )

            if np.max(
                np.abs(cur - last)
            ) > ROW_SPAN_CHANGE:
                break

            segment.append(
                current
            )
            last = cur
            j += 1

        top = (
            y0
            +
            i / row_scale
        )

        bottom = (
            y0
            +
            (j - 1 + 0.95)
            / row_scale
        )

        height = (
            bottom -
            top +
            1.0
        )

        if (
            MIN_BLOCK_HEIGHT
            <= height
            <= MAX_BLOCK_HEIGHT
        ):
            arr = np.asarray(
                segment,
                dtype=np.float64,
            )

            left = float(
                np.median(
                    arr[:, 0]
                )
            )

            right = float(
                np.median(
                    arr[:, 1]
                )
            )

            width = (
                right -
                left +
                1.0
            )

            aspect = (
                width /
                float(
                    max(
                        height,
                        1
                    )
                )
            )

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
                        "y0": float(top),
                        "y1": float(bottom),
                        "cx": (
                            left +
                            right
                        ) * 0.5,
                        "cy": (
                            top +
                            bottom
                        ) * 0.5,
                        "w": width,
                        "h": float(height),
                    }
                )

        i = max(
            i + 1,
            j,
        )

    bands.sort(
        key=lambda b: (
            b["y0"],
            b["x0"],
        )
    )

    return bands


def detect_block_data(frame, scale=None):
    spans, y0, row_scale = (
        build_row_spans(
            frame,
            scale,
        )
    )

    return (
        spans_to_bands(
            spans,
            y0,
            row_scale,
        ),
        spans,
        y0,
        row_scale,
    )


def choose_moving_candidate(
    bands,
    tracker,
):
    """
    Pick the moving block using temporal continuity. After a landing the
    tracker is reset, so the highest playable colored band is the new mover.
    """
    if not bands:
        return None

    if (
        tracker.last_seen is not None
        and
        tracker.missed <= MAX_TRACK_GAP
    ):
        best = None
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
                best = band
                best_distance = y_error

        if best is not None:
            return best

    return bands[0]


def target_below_from_spans(
    spans,
    y0,
    moving,
    row_scale=1.0,
):
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
        int(
            round(
                (bottom - y0)
                * row_scale
            )
        ) + 1,
    )

    search_height = max(
        45.0,
        moving["h"] * 2.4,
    )

    end = min(
        len(spans),
        int(
            round(
                (
                    bottom -
                    y0 +
                    search_height
                )
                * row_scale
            )
        ),
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
            top = float(
                y0 +
                i / row_scale
            )
            bottom2 = float(
                y0 +
                (j - 1 + 0.95) /
                row_scale
            )
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


def random_click_near_block(
    moving,
    frame_width,
    frame_height,
):
    """
    Return a random screen-local click point near the moving block.

    The point is generated around the block centre, but is never deliberately
    locked to the exact centre. It remains close enough to stay inside/near
    the moving block and safely inside the game area.
    """
    if moving is None:
        return (
            frame_width * 0.5,
            frame_height * 0.5,
        )

    cx = float(moving["cx"])
    cy = float(moving["cy"])
    half_w = max(
        8.0,
        float(moving["w"]) * RANDOM_CLICK_X_HALF_WIDTH,
    )
    half_h = max(
        6.0,
        float(moving["h"]) * RANDOM_CLICK_Y_HALF_HEIGHT,
    )

    # Draw until the point is not too close to the exact centre.
    for _ in range(8):
        dx = random.uniform(
            -half_w,
            half_w,
        )
        dy = random.uniform(
            -half_h,
            half_h,
        )

        distance = math.hypot(
            dx / max(half_w, 1.0),
            dy / max(half_h, 1.0),
        )

        if distance >= RANDOM_CLICK_MIN_DISTANCE_FROM_CENTER:
            break

    # Keep the random point inside the playable horizontal area so an
    # accidental title-bar/outside click cannot occur.
    left_limit = frame_width * GAME_X0
    right_limit = frame_width * GAME_X1
    top_limit = frame_height * GAME_Y0
    bottom_limit = frame_height * GAME_Y1

    px = max(
        left_limit + 2.0,
        min(
            right_limit - 2.0,
            cx + dx,
        ),
    )

    py = max(
        top_limit + 2.0,
        min(
            bottom_limit - 2.0,
            cy + dy,
        ),
    )

    return float(px), float(py)


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
    print("[READY] Live screen monitor starts immediately.")
    print("[READY] Waiting for the real Tower Stack match screen.")
    print("[READY] No tap is sent while matching/waiting.")
    print("[MODE] Motion prediction + ONE random single click per drop.")

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
    click_lead_ms = INITIAL_CLICK_LEAD_MS
    click_dispatch_ema = None

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

    # Match/session gate.
    match_ready = False
    match_ready_streak = 0

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
    tap_random_x = 0.0
    tap_random_y = 0.0
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

            # --------------------------------------------------------
            # MATCH GATE
            #
            # Until the actual Tower Stack pre-game layout is confirmed,
            # no block detection or motion tracking is allowed.
            # This is intentionally a visual match check, not a timer.
            # --------------------------------------------------------
            if not match_ready:
                if detect_match_ready_screen(frame):
                    match_ready_streak += 1
                else:
                    match_ready_streak = 0

                if match_ready_streak >= MATCH_READY_CONFIRM_FRAMES:
                    match_ready = True
                    match_ready_streak = 0
                    tracker.reset()
                    game_started = False
                    base_missing = 0
                    print(
                        "[MATCH] Tower Stack pre-game screen confirmed."
                    )
                    # Pre-focus scrcpy once now, instead of paying the focus
                    # switch cost during the first drop.
                    user32.SetForegroundWindow(hwnd)

                    print(
                        "[MATCH] Waiting for you to start the game."
                    )

            if match_ready:
                (
                    bands,
                    spans,
                    span_y0,
                    span_row_scale,
                ) = detect_block_data(
                    frame
                )
            else:
                # Feed nothing to the gameplay detector while waiting.
                bands = []
                spans = []
                span_y0 = int(
                    round(frame_h * GAME_Y0)
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
            # Candidate moving block + target below.
            #
            # After a poor overlap, the surviving tower top can be extremely
            # narrow. The fast scan is attempted first; when geometry becomes
            # small/ambiguous, immediately run a full-resolution precision scan.
            # --------------------------------------------------------
            candidate = choose_moving_candidate(
                bands,
                tracker,
            )

            candidate_target = None

            if candidate is not None:
                candidate_target = (
                    target_below_from_spans(
                        spans,
                        span_y0,
                        candidate,
                        span_row_scale,
                    )
                )

            need_precision = (
                match_ready
                and
                game_started
                and
                (
                    candidate is None
                    or
                    candidate["w"] <= PRECISION_SCAN_TRIGGER_WIDTH
                    or
                    candidate_target is None
                )
            )

            if need_precision:
                (
                    precise_bands,
                    precise_spans,
                    precise_y0,
                    precise_row_scale,
                ) = detect_block_data(
                    frame,
                    PRECISION_SCAN_SCALE,
                )

                precise_candidate = choose_moving_candidate(
                    precise_bands,
                    tracker,
                )

                if precise_candidate is not None:
                    precise_target = (
                        target_below_from_spans(
                            precise_spans,
                            precise_y0,
                            precise_candidate,
                            precise_row_scale,
                        )
                    )

                    if precise_target is not None:
                        candidate = precise_candidate
                        candidate_target = precise_target
                        bands = precise_bands
                        spans = precise_spans
                        span_y0 = precise_y0
                        span_row_scale = precise_row_scale
                        state = "PRECISION TRACK - SMALL BLOCK"

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
                    tap_random_x = 0.0
                    tap_random_y = 0.0
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
                                # Predict to the expected touch-arrival time.
                                # Fixed 7-22ms was too short for this live
                                # Windows/scrcpy setup; adapt from measured
                                # burst dispatch time.
                                if click_dispatch_ema is None:
                                    click_lead_ms = (
                                        INITIAL_CLICK_LEAD_MS
                                    )
                                else:
                                    click_lead_ms = max(
                                        CLICK_LEAD_MIN_MS,
                                        min(
                                            CLICK_LEAD_MAX_MS,
                                            click_dispatch_ema
                                            +
                                            last_frame_dt * 1000.0 * 0.50,
                                        ),
                                    )

                                lead = (
                                    click_lead_ms /
                                    1000.0
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

                                    # Keep the drop prediction based on
                                    # the moving block, but randomize the actual
                                    # screen click point around that block. The
                                    # three clicks still share ONE point, so the
                                    # burst stays extremely fast.
                                    (
                                        tap_random_x,
                                        tap_random_y,
                                    ) = random_click_near_block(
                                        candidate,
                                        frame_w,
                                        frame_h,
                                    )

                                    screen_x = (
                                        region[0]
                                        +
                                        tap_random_x
                                    )
                                    screen_y = (
                                        region[1]
                                        +
                                        tap_random_y
                                    )

                                    tap_random_x = float(
                                        tap_random_x
                                    )
                                    tap_random_y = float(
                                        tap_random_y
                                    )

                                    (
                                        dispatch_ms,
                                        ok,
                                        sent,
                                    ) = tapper.click(
                                        screen_x,
                                        screen_y,
                                        hwnd,
                                    )

                                    dispatch_values.append(
                                        dispatch_ms
                                    )

                                    if ok:
                                        drops += 1

                                        if click_dispatch_ema is None:
                                            click_dispatch_ema = (
                                                float(dispatch_ms)
                                            )
                                        else:
                                            click_dispatch_ema = (
                                                CLICK_LEAD_EMA_ALPHA
                                                *
                                                float(dispatch_ms)
                                                +
                                                (1.0 - CLICK_LEAD_EMA_ALPHA)
                                                *
                                                click_dispatch_ema
                                            )

                                        click_lead_ms = max(
                                            CLICK_LEAD_MIN_MS,
                                            min(
                                                CLICK_LEAD_MAX_MS,
                                                click_dispatch_ema
                                                +
                                                last_frame_dt * 1000.0 * 0.50,
                                            ),
                                        )

                                        drop_active = True
                                        post_tap_missing = 0
                                        state = (
                                            "SINGLE CLICK @ "
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
                                            "TAP FAILED {}/2".format(
                                                sent
                                            )
                                        )

            # The match gate is authoritative while the game has not
            # been visually matched.
            if not match_ready:
                state = (
                    "WAITING FOR TOWER MATCH "
                    "{}/{}".format(
                        match_ready_streak,
                        MATCH_READY_CONFIRM_FRAMES,
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
                    "Random Tap: {}".format(
                        "-"
                        if tap_random_x == 0.0
                        else "{},{}".format(
                            int(round(tap_random_x)),
                            int(round(tap_random_y)),
                        )
                    ),
                    "Drops: {}  Lands: {}".format(
                        drops,
                        lands,
                    ),
                    "Tap failures: {}".format(
                        tap_failures
                    ),
                    "Click mode: SINGLE CLICK",
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
                    match_ready = False
                    match_ready_streak = 0
                    moving_band = None
                    target_band = None
                    drop_point = None
                    drop_active = False
                    tap_target = None
                    tap_predicted_center = 0.0
                    tap_prediction_error = 0.0
                    tap_random_x = 0.0
                    tap_random_y = 0.0
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

                    click_dispatch_ema = None
                    click_lead_ms = INITIAL_CLICK_LEAD_MS

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
            "Avg single-click dispatch: {:.2f} ms".format(
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
