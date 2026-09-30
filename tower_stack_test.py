import ctypes
from ctypes import wintypes
from collections import deque
import math
import time

import cv2
import numpy as np


# ============================================================
# TOWER STACK - HIGH ACCURACY BOT
#
# Separate file. circle_touch_challenge.py is untouched.
#
# Core strategy:
#   1) Start monitoring immediately.
#   2) Wait only for a VERIFIED Tower Stack match screen.
#   3) Detect the moving block from FRAME DIFFERENCE, not from
#      "top colored rectangle" guesses.
#   4) Detect the stationary target directly underneath it.
#   5) Track several frames and estimate velocity.
#   6) Predict the block centre when the click reaches the game.
#   7) Send EXACTLY ONE click.
#   8) Wait only for the next moving block to appear ABOVE the
#      newly landed block; never wait indefinitely.
#   9) Print detailed live drop/landing diagnostics.
#
# Accuracy-first rule:
#   No click is sent from a generic "colored rectangle" candidate.
#   A click requires a moving-object signal + target below + stable motion.
# ============================================================


SCRCPY_TITLE = "CHESS_MOBILE"
OUTPUT_TITLE = "TOWER STACK - BOT TEST"

AUTO_CLICK_START = True

# Keep the OpenCV debug window OFF for live accuracy tests. It can reduce FPS
# and steal foreground focus from scrcpy.
SHOW_OUTPUT = False
DISPLAY_EVERY = 3

# ------------------------------------------------------------
# GAME AREA
# From the supplied recording the actual Tower board occupies the middle
# portion of the scrcpy client. These are normalized client coordinates.
# ------------------------------------------------------------
GAME_X0 = 0.17
GAME_X1 = 0.83
GAME_Y0 = 0.30
GAME_Y1 = 0.955

# ------------------------------------------------------------
# MATCH GATE
# ------------------------------------------------------------
MATCH_READY_CONFIRM_FRAMES = 8

MATCH_SCORE_X0 = 0.30
MATCH_SCORE_X1 = 0.75
MATCH_SCORE_Y0 = 0.22
MATCH_SCORE_Y1 = 0.31

MATCH_START_X0 = 0.30
MATCH_START_X1 = 0.70
MATCH_START_Y0 = 0.88
MATCH_START_Y1 = 0.96

# ------------------------------------------------------------
# MOVING BLOCK DETECTION
# ------------------------------------------------------------
MOTION_DETECT_SCALE = 0.75
MOTION_DIFF_THRESHOLD = 12

MOTION_ROW_CHANGE_RATIO = 0.045
MOTION_ROW_CHANGE_MIN_PIXELS = 7

MOTION_BAND_MIN_HEIGHT = 7
MOTION_BAND_MAX_HEIGHT = 38

MOVING_Y_MIN_RATIO = 0.52

COLOR_MIN_SATURATION = 42
COLOR_MIN_VALUE = 65

MIN_BLOCK_WIDTH = 6
MAX_BLOCK_WIDTH = 200
MIN_BLOCK_HEIGHT = 5
MAX_BLOCK_HEIGHT = 34

TARGET_SEARCH_BELOW_MIN = 3
TARGET_SEARCH_BELOW_MAX = 35
TARGET_STABLE_ROWS = 5

# Target lock: the stationary block below the mover should not jump to a
# different tower block because one row was rendered differently.
TARGET_LOCK_MAX_X_SHIFT = 9.0
TARGET_LOCK_MAX_Y_SHIFT = 6.0
TARGET_LOCK_MAX_WIDTH_SHIFT = 22.0
TARGET_LOCK_HOLD_FRAMES = 3

# ------------------------------------------------------------
# MOTION TRACKING
# ------------------------------------------------------------
TRACK_HISTORY = 6
TRACK_MIN_POINTS = 3
TRACK_MIN_SPEED = 45.0

TRACK_MAX_Y_JUMP = 18.0
TRACK_MAX_WIDTH_JUMP = 70.0
TRACK_MAX_MISSED = 1

# ------------------------------------------------------------
# CLICK TIMING
# ------------------------------------------------------------
CLICK_DISPATCH_INITIAL_MS = 6.0
CLICK_DISPATCH_EMA_ALPHA = 0.30

CLICK_SAFETY_MS = 1.5
CLICK_LEAD_MIN_MS = 5.0
CLICK_LEAD_MAX_MS = 35.0

# Prediction accuracy gate.
PREDICT_ERROR_ABS_MAX_PX = 5.0
PREDICT_ERROR_RATIO = 0.055

# Small temporal crossing window so a frame boundary does not force a miss.
DROP_TIME_WINDOW_MIN_MS = 4.0
DROP_TIME_WINDOW_MAX_MS = 22.0

# ------------------------------------------------------------
# LANDING / STATE
# ------------------------------------------------------------
LANDING_NEW_MOVER_Y_MIN = 10.0
LANDING_VERIFY_TIMEOUT_MS = 220.0
NO_MOVER_STOP_MS = 1200.0

# ------------------------------------------------------------
# DETAILED LOGGING
# ------------------------------------------------------------
PRINT_TRACK_LOG = True
TRACK_LOG_INTERVAL_MS = 350.0
PRINT_DROP_LOG = True
PRINT_LANDING_LOG = True


# ============================================================
# Windows DPI / mouse input
# ============================================================

try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)
except Exception:
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass


user32 = ctypes.windll.user32
ULONG_PTR = getattr(
    wintypes,
    "ULONG_PTR",
    ctypes.c_size_t,
)


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


# ============================================================
# scrcpy
# ============================================================

def find_scrcpy():
    hwnd = user32.FindWindowW(
        None,
        SCRCPY_TITLE,
    )
    return hwnd if hwnd else None


def client_region(hwnd):
    rect = wintypes.RECT()

    if not user32.GetClientRect(
        hwnd,
        ctypes.byref(rect),
    ):
        return None

    width = rect.right - rect.left
    height = rect.bottom - rect.top

    if width <= 0 or height <= 0:
        return None

    point = wintypes.POINT(0, 0)

    if not user32.ClientToScreen(
        hwnd,
        ctypes.byref(point),
    ):
        return None

    left = int(point.x)
    top = int(point.y)
    right = int(point.x + width)
    bottom = int(point.y + height)

    # DXCam requires the region to remain inside the physical desktop.
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
            screen_w - 1,
        ),
    )

    top = max(
        0,
        min(
            top,
            screen_h - 1,
        ),
    )

    right = max(
        left + 1,
        min(
            right,
            screen_w,
        ),
    )

    bottom = max(
        top + 1,
        min(
            bottom,
            screen_h,
        ),
    )

    return (
        left,
        top,
        right,
        bottom,
    )


# ============================================================
# Capture
# ============================================================

class Capture:
    def __init__(self):
        self.region = None
        self.dx = None
        self.mss = None

        try:
            import dxcam

            self.dx = dxcam.create(
                output_color="BGR"
            )
            self.backend = "DXCAM"

        except Exception as exc:
            print(
                "[WARN] DXCam unavailable:",
                exc,
            )

            try:
                import mss

                self.mss = mss.mss()
                self.backend = "MSS"

            except Exception as exc2:
                raise RuntimeError(
                    "Install dxcam or mss"
                ) from exc2

    def set_region(self, region):
        if region is not None:
            self.region = region

    def grab(self):
        if self.region is None:
            return None

        left, top, right, bottom = self.region

        screen_w = int(
            user32.GetSystemMetrics(0)
        )
        screen_h = int(
            user32.GetSystemMetrics(1)
        )

        left = max(
            0,
            min(
                int(left),
                screen_w - 1,
            ),
        )

        top = max(
            0,
            min(
                int(top),
                screen_h - 1,
            ),
        )

        right = max(
            left + 1,
            min(
                int(right),
                screen_w,
            ),
        )

        bottom = max(
            top + 1,
            min(
                int(bottom),
                screen_h,
            ),
        )

        self.region = (
            left,
            top,
            right,
            bottom,
        )

        if self.backend == "DXCAM":
            try:
                return self.dx.grab(
                    region=(
                        left,
                        top,
                        right,
                        bottom,
                    )
                )

            except ValueError:
                # A resize/move can happen between region refreshes.
                # Refreshing the region is handled by main on the next loop.
                return None

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

        return cv2.cvtColor(
            raw,
            cv2.COLOR_BGRA2BGR,
        )

    def close(self):
        if self.dx is not None:
            try:
                release = getattr(
                    self.dx,
                    "release",
                    None,
                )

                if callable(release):
                    release()

                else:
                    stop = getattr(
                        self.dx,
                        "stop",
                        None,
                    )

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


# ============================================================
# Single click
# ============================================================

class Tapper:
    def click(
        self,
        screen_x,
        screen_y,
        hwnd,
    ):
        started = time.perf_counter()

        try:
            # Focus repair only when necessary.
            if user32.GetForegroundWindow() != hwnd:
                user32.SetForegroundWindow(hwnd)

            px = int(round(screen_x))
            py = int(round(screen_y))

            if not user32.SetCursorPos(
                px,
                py,
            ):
                return (
                    0.0,
                    False,
                    0,
                )

            inputs = (
                INPUT * 2
            )()

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
                time.perf_counter()
                -
                started
            ) * 1000.0

            return (
                elapsed_ms,
                sent == 2,
                int(sent),
            )

        except Exception:
            return (
                (
                    time.perf_counter()
                    -
                    started
                ) * 1000.0,
                False,
                0,
            )


# ============================================================
# Common geometry helpers
# ============================================================

def game_roi(frame):
    height, width = frame.shape[:2]

    return (
        int(round(width * GAME_X0)),
        int(round(width * GAME_X1)),
        int(round(height * GAME_Y0)),
        int(round(height * GAME_Y1)),
    )


def color_mask(frame):
    hsv = cv2.cvtColor(
        frame,
        cv2.COLOR_BGR2HSV,
    )

    return (
        (
            hsv[:, :, 1]
            >=
            COLOR_MIN_SATURATION
        )
        &
        (
            hsv[:, :, 2]
            >=
            COLOR_MIN_VALUE
        )
    ).astype(np.uint8)


def longest_true_run(values):
    xs = np.flatnonzero(values)

    if xs.size == 0:
        return None

    breaks = np.flatnonzero(
        np.diff(xs) > 1
    )

    starts = np.r_[
        0,
        breaks + 1,
    ]

    ends = np.r_[
        breaks,
        xs.size - 1,
    ]

    lengths = (
        ends -
        starts +
        1
    )

    index = int(
        np.argmax(lengths)
    )

    return (
        int(
            xs[starts[index]]
        ),
        int(
            xs[ends[index]]
        ),
        int(
            lengths[index]
        ),
    )


def run_to_band(
    frame,
    y0,
    y1,
    x0,
    x1,
    mask=None,
):
    if y1 <= y0 or x1 <= x0:
        return None

    if mask is None:
        mask = color_mask(frame)

    roi = mask[
        y0:y1,
        x0:x1
    ]

    if roi.size == 0:
        return None

    column_counts = (
        roi > 0
    ).sum(axis=0)

    required = max(
        2,
        int(
            round(
                roi.shape[0] * 0.50
            )
        ),
    )

    valid = (
        column_counts
        >=
        required
    )

    run = longest_true_run(
        valid
    )

    if run is None:
        return None

    left, right, run_width = run

    if run_width < MIN_BLOCK_WIDTH:
        return None

    left += x0
    right += x0

    width = (
        right -
        left +
        1.0
    )

    height = (
        y1 -
        y0
    )

    if not (
        MIN_BLOCK_WIDTH
        <=
        width
        <=
        MAX_BLOCK_WIDTH
    ):
        return None

    if not (
        MIN_BLOCK_HEIGHT
        <=
        height
        <=
        MAX_BLOCK_HEIGHT
    ):
        return None

    return {
        "x0": float(left),
        "x1": float(right),
        "y0": float(y0),
        "y1": float(y1),
        "cx": float(
            (left + right) * 0.5
        ),
        "cy": float(
            (y0 + y1) * 0.5
        ),
        "w": float(width),
        "h": float(height),
    }


# ============================================================
# Match gate
# ============================================================

def detect_match_ready_screen(frame):
    height, width = frame.shape[:2]

    # Large dark play board.
    bx0 = int(
        round(width * GAME_X0)
    )
    bx1 = int(
        round(width * GAME_X1)
    )
    by0 = int(
        round(height * GAME_Y0)
    )
    by1 = int(
        round(height * GAME_Y1)
    )

    board = frame[
        by0:by1,
        bx0:bx1
    ]

    if board.size == 0:
        return False

    board_gray = cv2.cvtColor(
        board,
        cv2.COLOR_BGR2GRAY,
    )

    dark_ratio = float(
        np.mean(
            board_gray < 85
        )
    )

    if (
        float(np.mean(board_gray))
        >=
        95.0
    ):
        return False

    if dark_ratio < 0.62:
        return False

    # Yellow score box.
    sx0 = int(
        round(width * MATCH_SCORE_X0)
    )
    sx1 = int(
        round(width * MATCH_SCORE_X1)
    )
    sy0 = int(
        round(height * MATCH_SCORE_Y0)
    )
    sy1 = int(
        round(height * MATCH_SCORE_Y1)
    )

    score_roi = frame[
        sy0:sy1,
        sx0:sx1
    ]

    if score_roi.size == 0:
        return False

    hsv = cv2.cvtColor(
        score_roi,
        cv2.COLOR_BGR2HSV,
    )

    yellow = cv2.inRange(
        hsv,
        np.array(
            [15, 90, 140],
            dtype=np.uint8,
        ),
        np.array(
            [40, 255, 255],
            dtype=np.uint8,
        ),
    )

    yellow_ratio = float(
        np.mean(
            yellow > 0
        )
    )

    if yellow_ratio < 0.025:
        return False

    # TAP TO START button.
    tx0 = int(
        round(width * MATCH_START_X0)
    )
    tx1 = int(
        round(width * MATCH_START_X1)
    )
    ty0 = int(
        round(height * MATCH_START_Y0)
    )
    ty1 = int(
        round(height * MATCH_START_Y1)
    )

    start = frame[
        ty0:ty1,
        tx0:tx1
    ]

    if start.size == 0:
        return False

    start_gray = cv2.cvtColor(
        start,
        cv2.COLOR_BGR2GRAY,
    )

    bright = (
        start_gray >= 105
    ).astype(np.uint8) * 255

    bright = cv2.morphologyEx(
        bright,
        cv2.MORPH_CLOSE,
        np.ones(
            (5, 5),
            np.uint8,
        ),
        iterations=1,
    )

    contours, _ = cv2.findContours(
        bright,
        cv2.RETR_EXTERNAL,
        cv2.CHAIN_APPROX_SIMPLE,
    )

    start_area = float(
        max(
            1,
            start.shape[0]
            *
            start.shape[1],
        )
    )

    for contour in contours:
        area = cv2.contourArea(
            contour
        )

        if area < start_area * 0.05:
            continue

        x, y, w, h = cv2.boundingRect(
            contour
        )

        aspect = (
            w /
            float(max(h, 1))
        )

        if (
            2.5
            <=
            aspect
            <=
            9.5
            and
            w >= start.shape[1] * 0.30
            and
            h >= start.shape[0] * 0.14
        ):
            return True

    return False


# ============================================================
# Moving block from frame difference
# ============================================================

def longest_row_run(row, min_width=5):
    xs = np.flatnonzero(row)

    if xs.size == 0:
        return None

    breaks = np.flatnonzero(
        np.diff(xs) > 1
    )

    starts = np.r_[0, breaks + 1]
    ends = np.r_[breaks, xs.size - 1]

    lengths = ends - starts + 1
    idx = int(np.argmax(lengths))

    if int(lengths[idx]) < min_width:
        return None

    return (
        int(xs[starts[idx]]),
        int(xs[ends[idx]]),
    )


def detect_colored_bands(frame):
    """
    Detect the horizontal colored rectangles that form the Tower.

    Row-wise spans are used because touching blocks merge into one connected
    component. Each differently positioned horizontal band stays separate.
    """
    height, width = frame.shape[:2]

    x0, x1, y0, y1 = game_roi(frame)
    roi = frame[y0:y1, x0:x1]

    hsv = cv2.cvtColor(
        roi,
        cv2.COLOR_BGR2HSV,
    )

    mask = (
        (hsv[:, :, 1] >= COLOR_MIN_SATURATION)
        &
        (hsv[:, :, 2] >= COLOR_MIN_VALUE)
    ).astype(np.uint8)

    spans = []

    for row in mask:
        run = longest_row_run(
            row > 0,
            min_width=4,
        )

        if run is None:
            spans.append(None)
        else:
            spans.append(
                (
                    float(x0 + run[0]),
                    float(x0 + run[1]),
                )
            )

    bands = []
    i = 0

    while i < len(spans):
        if spans[i] is None:
            i += 1
            continue

        segment = [spans[i]]
        j = i + 1

        previous_left, previous_right = spans[i]

        while j < len(spans):
            current = spans[j]

            if current is None:
                if (
                    j + 1 < len(spans)
                    and
                    spans[j + 1] is not None
                ):
                    j += 1
                    continue

                break

            left, right = current

            if (
                abs(left - previous_left) > 6.0
                or
                abs(right - previous_right) > 6.0
            ):
                break

            segment.append(current)
            previous_left = left
            previous_right = right
            j += 1

        block_height = float(j - i)

        if (
            MIN_BLOCK_HEIGHT
            <=
            block_height
            <=
            MAX_BLOCK_HEIGHT
        ):
            arr = np.asarray(
                segment,
                dtype=np.float64,
            )

            left = float(
                np.median(arr[:, 0])
            )

            right = float(
                np.median(arr[:, 1])
            )

            block_width = (
                right
                -
                left
                +
                1.0
            )

            aspect = (
                block_width
                /
                max(
                    block_height,
                    1.0,
                )
            )

            if (
                MIN_BLOCK_WIDTH
                <=
                block_width
                <=
                MAX_BLOCK_WIDTH
                and
                0.25
                <=
                aspect
                <=
                16.0
            ):
                bands.append(
                    {
                        "x0": left,
                        "x1": right,
                        "y0": float(y0 + i),
                        "y1": float(y0 + j - 1),
                        "cx": (
                            left
                            +
                            right
                        )
                        *
                        0.5,
                        "cy": (
                            y0 + i
                            +
                            y0 + j - 1
                        )
                        *
                        0.5,
                        "w": block_width,
                        "h": block_height,
                    }
                )

        i = max(
            i + 1,
            j,
        )

    bands.sort(
        key=lambda item: item["y0"]
    )

    return bands


def reset_motion_detector():
    detect_moving_block._previous_bands = []
    detect_moving_block._last_moving = None
    detect_moving_block._motion_streak = 0


def detect_moving_block(
    frame,
    previous_gray=None,
    tracked_y=None,
):
    """
    Detect the true moving block by horizontal displacement of a rectangular
    band between consecutive frames.

    This intentionally ignores broad frame-difference blobs. During camera
    scrolling, many static tower blocks change vertically; their horizontal
    coordinate stays almost unchanged.
    """
    bands = detect_colored_bands(frame)

    previous_bands = getattr(
        detect_moving_block,
        "_previous_bands",
        [],
    )

    candidates = []

    for current in bands:
        best = None

        for previous in previous_bands:
            y_error = abs(
                current["cy"]
                -
                previous["cy"]
            )

            width_error = abs(
                current["w"]
                -
                previous["w"]
            )

            if y_error > 7.0:
                continue

            if width_error > max(
                24.0,
                previous["w"] * 0.28,
            ):
                continue

            dx = (
                current["cx"]
                -
                previous["cx"]
            )

            if best is None or abs(dx) > abs(best[0]):
                best = (
                    dx,
                    previous,
                )

        if best is not None:
            dx, previous = best

            if abs(dx) >= 2.0:
                # Prefer a candidate that also resembles the last confirmed
                # mover, but keep the strongest real horizontal displacement.
                continuity = 0.0

                last_moving = getattr(
                    detect_moving_block,
                    "_last_moving",
                    None,
                )

                if last_moving is not None:
                    continuity = max(
                        0.0,
                        2.0
                        -
                        abs(
                            current["cx"]
                            -
                            last_moving["cx"]
                        )
                        /
                        40.0,
                    )

                score = (
                    abs(dx)
                    +
                    continuity
                    -
                    y_error * 0.20
                )

                candidates.append(
                    (
                        score,
                        dx,
                        current,
                    )
                )

    if not candidates:
        detect_moving_block._previous_bands = bands
        detect_moving_block._last_moving = None
        detect_moving_block._motion_streak = 0
        return None, 0.0

    candidates.sort(
        key=lambda item: item[0],
        reverse=True,
    )

    _, dx, current = candidates[0]

    direction = (
        1
        if dx > 0
        else -1
    )

    last_moving = getattr(
        detect_moving_block,
        "_last_moving",
        None,
    )

    if (
        last_moving is not None
        and
        last_moving.get("frame_direction")
        not in
        (0, direction)
    ):
        # This is either a real bounce or a candidate switch. Require one more
        # consistent frame before allowing a drop decision.
        detect_moving_block._motion_streak = 1
    else:
        detect_moving_block._motion_streak = (
            getattr(
                detect_moving_block,
                "_motion_streak",
                0,
            )
            +
            1
        )

    current["frame_dx"] = float(dx)
    current["frame_direction"] = int(direction)
    current["motion_score"] = float(abs(dx))

    detect_moving_block._previous_bands = bands
    detect_moving_block._last_moving = dict(
        current
    )

    if (
        detect_moving_block._motion_streak
        >=
        2
    ):
        return current, 0.0

    return None, 0.0


def detect_target_candidates(
    frame,
    moving,
):
    """
    Return all plausible stationary target blocks directly below the mover.

    The target is not selected solely from the closest row. Candidates are
    ranked by vertical gap and horizontal overlap so the tracker can lock to
    the same stationary tower block across frames.
    """
    if moving is None:
        return []

    bands = detect_colored_bands(frame)
    candidates = []

    for band in bands:
        gap = (
            band["y0"]
            -
            moving["y1"]
        )

        if (
            gap < -1.0
            or
            gap > TARGET_SEARCH_BELOW_MAX
        ):
            continue

        overlap = (
            min(
                moving["x1"],
                band["x1"],
            )
            -
            max(
                moving["x0"],
                band["x0"],
            )
        )

        if overlap <= 2.0:
            continue

        overlap_ratio = (
            overlap
            /
            max(
                1.0,
                min(
                    moving["w"],
                    band["w"],
                ),
            )
        )

        if overlap_ratio < 0.08:
            continue

        candidates.append(
            {
                **band,
                "gap": float(gap),
                "overlap": float(overlap),
                "overlap_ratio": float(overlap_ratio),
            }
        )

    candidates.sort(
        key=lambda item: (
            item["gap"],
            -item["overlap_ratio"],
            -item["w"],
        )
    )

    return candidates


def choose_locked_target(
    candidates,
    previous_target=None,
):
    """
    Keep the stationary target locked instead of allowing a single noisy
    frame to jump to another stack layer.
    """
    if not candidates:
        return (
            previous_target
            if previous_target is not None
            else None
        )

    if previous_target is None:
        return candidates[0]

    stable = []

    for candidate in candidates:
        if abs(
            candidate["cx"]
            -
            previous_target["cx"]
        ) > TARGET_LOCK_MAX_X_SHIFT:
            continue

        if abs(
            candidate["cy"]
            -
            previous_target["cy"]
        ) > TARGET_LOCK_MAX_Y_SHIFT:
            continue

        if abs(
            candidate["w"]
            -
            previous_target["w"]
        ) > TARGET_LOCK_MAX_WIDTH_SHIFT:
            continue

        stable.append(candidate)

    if stable:
        stable.sort(
            key=lambda item: (
                abs(
                    item["cx"]
                    -
                    previous_target["cx"]
                ),
                abs(
                    item["cy"]
                    -
                    previous_target["cy"]
                ),
                -item["overlap_ratio"],
            )
        )

        return stable[0]

    # Do not jump to a totally different block from one bad frame.
    return previous_target


def detect_target_below(
    frame,
    moving,
    previous_target=None,
):
    candidates = detect_target_candidates(
        frame,
        moving,
    )

    return choose_locked_target(
        candidates,
        previous_target,
    )


# ============================================================
# Motion tracker
# ============================================================

class MotionTracker:
    def __init__(self):
        self.history = deque(
            maxlen=TRACK_HISTORY
        )

        self.last_seen = None
        self.missed = 0

    def reset(self):
        self.history.clear()
        self.last_seen = None
        self.missed = 0

    def update(
        self,
        block,
        now,
    ):
        if block is None:
            self.missed += 1
            return

        if self.last_seen is not None:
            y_jump = abs(
                block["cy"]
                -
                self.last_seen["cy"]
            )

            width_jump = abs(
                block["w"]
                -
                self.last_seen["w"]
            )

            if (
                self.missed
                >
                TRACK_MAX_MISSED
                or
                y_jump
                >
                TRACK_MAX_Y_JUMP
                or
                width_jump
                >
                TRACK_MAX_WIDTH_JUMP
            ):
                self.history.clear()

        self.missed = 0

        self.history.append(
            (
                now,
                float(block["cx"]),
                float(block["cy"]),
                float(block["w"]),
                float(block["h"]),
            )
        )

        self.last_seen = dict(
            block
        )

    def current(self):
        if not self.history:
            return None

        item = self.history[-1]

        return {
            "cx": item[1],
            "cy": item[2],
            "w": item[3],
            "h": item[4],
        }

    def velocity(self):
        if len(self.history) < 2:
            return 0.0

        items = list(
            self.history
        )

        times = np.asarray(
            [
                item[0]
                -
                items[0][0]
                for item in items
            ],
            dtype=np.float64,
        )

        xs = np.asarray(
            [
                item[1]
                for item in items
            ],
            dtype=np.float64,
        )

        if len(times) < 2:
            return 0.0

        dt = times[-1]

        if dt <= 1e-6:
            return 0.0

        slope = np.polyfit(
            times,
            xs,
            1,
        )[0]

        if not np.isfinite(
            slope
        ):
            return 0.0

        return float(
            slope
        )

    def stable(self):
        if len(
            self.history
        ) < TRACK_MIN_POINTS:
            return False

        if abs(
            self.velocity()
        ) < TRACK_MIN_SPEED:
            return False

        items = list(
            self.history
        )[-4:]

        signs = []

        for first, second in zip(
            items[:-1],
            items[1:],
        ):
            dx = (
                second[1]
                -
                first[1]
            )

            if abs(dx) < 0.8:
                continue

            signs.append(
                1
                if dx > 0
                else -1
            )

        if len(signs) < 2:
            return False

        return (
            signs[-1]
            ==
            signs[-2]
        )

    def reversal(self):
        if len(
            self.history
        ) < 3:
            return False

        first = self.history[-3][1]
        second = self.history[-2][1]
        third = self.history[-1][1]

        dx1 = second - first
        dx2 = third - second

        if (
            abs(dx1) < 1.0
            or
            abs(dx2) < 1.0
        ):
            return False

        return (
            (
                dx1 > 0
                and
                dx2 < 0
            )
            or
            (
                dx1 < 0
                and
                dx2 > 0
            )
        )


# ============================================================
# Physics / prediction
# ============================================================

def moving_center_limits(
    frame,
    block_width,
):
    height, width = frame.shape[:2]

    half = max(
        3.0,
        float(block_width) * 0.5,
    )

    left = (
        width * GAME_X0
        +
        half
    )

    right = (
        width * GAME_X1
        -
        half
    )

    if right <= left:
        center = (
            width * 0.5
        )

        return (
            center,
            center,
        )

    return (
        left,
        right,
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

    span = max(
        1.0,
        right - left,
    )

    coordinate = (
        x
        -
        left
        +
        vx * seconds
    )

    period = (
        2.0
        *
        span
    )

    value = (
        coordinate
        %
        period
    )

    if value <= span:
        return (
            left
            +
            value
        )

    return (
        right
        -
        (
            value
            -
            span
        )
    )


def time_to_target(
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
        min(
            right,
            target_x,
        ),
    )

    speed = abs(vx)

    if vx > 0.0:
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


def overlap_width(
    center_x,
    block_width,
    target,
):
    left = (
        center_x
        -
        block_width * 0.5
    )

    right = (
        center_x
        +
        block_width * 0.5
    )

    return max(
        0.0,
        min(
            right,
            target["x1"],
        )
        -
        max(
            left,
            target["x0"],
        ),
    )


# ============================================================
# Drawing
# ============================================================

def draw_box(
    image,
    block,
    bgr,
):
    if block is None:
        return

    x0 = int(
        round(block["x0"])
    )
    y0 = int(
        round(block["y0"])
    )
    x1 = int(
        round(block["x1"])
    )
    y1 = int(
        round(block["y1"])
    )

    cv2.rectangle(
        image,
        (x0, y0),
        (x1, y1),
        bgr,
        2,
        cv2.LINE_AA,
    )


def put_text(
    image,
    text,
    y,
):
    cv2.putText(
        image,
        text,
        (8, y),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.45,
        (255, 255, 255),
        1,
        cv2.LINE_AA,
    )


def move_output_window(
    region,
):
    try:
        left, top, right, _ = region

        screen_w = int(
            user32.GetSystemMetrics(0)
        )

        x = (
            right
            +
            10
        )

        if x + 540 > screen_w:
            x = max(
                10,
                left - 540,
            )

        cv2.moveWindow(
            OUTPUT_TITLE,
            int(x),
            int(top),
        )

    except Exception:
        pass


# ============================================================
# Main
# ============================================================

def main():
    print("=" * 72)
    print(
        "              TOWER STACK - HIGH ACCURACY BOT"
    )
    print("=" * 72)
    print(
        "scrcpy title:",
        SCRCPY_TITLE,
    )
    print(
        "SPACE = auto click ON/OFF | "
        "R = reset stats | Q = quit"
    )
    print(
        "[READY] Live screen monitoring starts immediately."
    )
    print(
        "[READY] Waiting for a verified Tower Stack screen."
    )
    print(
        "[MODE] Motion-difference tracking + prediction + "
        "ONE single click per block."
    )

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

    region = client_region(
        hwnd
    )

    if not region:
        print(
            "[ERROR] Could not read scrcpy client region."
        )

        return

    print(
        "[SCRCPY] Existing window found."
    )
    print(
        "[SCRCPY] Client region:",
        region,
    )

    capture = Capture()
    capture.set_region(
        region
    )

    print(
        "[CAPTURE]",
        capture.backend,
    )

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

        move_output_window(
            region
        )

    auto_click = AUTO_CLICK_START

    # Session state.
    match_ready = False
    match_ready_streak = 0

    game_started = False

    drop_active = False

    previous_gray = None

    last_mover = None
    last_target = None
    locked_target = None
    target_hold_frames = 0

    tap_started_at = 0.0

    tap_moving_y = 0.0
    tap_moving_width = 0.0

    tap_target = None

    tap_predicted_x = 0.0
    tap_prediction_error = 0.0

    tap_expected_overlap = 0.0

    # Timing adaptation.
    dispatch_ema = (
        CLICK_DISPATCH_INITIAL_MS
    )

    timing_bias_ms = 0.0

    # Counters.
    frame_count = 0
    drop_count = 0
    landing_count = 0
    tap_failures = 0
    uncertain_landings = 0

    # Metrics.
    actual_errors = []
    actual_overlaps = []
    dispatch_times = []
    landing_times = []

    run_start = time.perf_counter()

    last_track_log_at = 0.0
    last_no_mover_at = time.perf_counter()

    frame_index = 0

    try:
        while True:
            loop_now = time.perf_counter()

            # Refresh scrcpy region frequently but not every expensive frame.
            if (
                frame_index % 30
                ==
                0
            ):
                current_hwnd = find_scrcpy()

                if current_hwnd:
                    hwnd = current_hwnd

                    current_region = client_region(
                        hwnd
                    )

                    if current_region:
                        region = current_region
                        capture.set_region(
                            region
                        )

            grab_started = time.perf_counter()

            frame = capture.grab()

            if frame is None:
                state = "NO FRAME"

                time.sleep(0.001)
                continue

            frame_capture_time = (
                time.perf_counter()
            )

            capture_ms = (
                frame_capture_time
                -
                grab_started
            ) * 1000.0

            frame_count += 1
            frame_index += 1

            frame_h, frame_w = frame.shape[:2]

            # --------------------------------------------------------
            # Match gate first.
            # --------------------------------------------------------
            if not match_ready:
                if detect_match_ready_screen(
                    frame
                ):
                    match_ready_streak += 1

                else:
                    match_ready_streak = 0

                if (
                    match_ready_streak
                    >=
                    MATCH_READY_CONFIRM_FRAMES
                ):
                    match_ready = True
                    match_ready_streak = 0
                    tracker.reset()
                    game_started = False
                    drop_active = False
                    previous_gray = None
                    locked_target = None
                    target_hold_frames = 0
                    reset_motion_detector()

                    # Pre-focus once.
                    user32.SetForegroundWindow(
                        hwnd
                    )

                    print(
                        "[MATCH] Verified Tower Stack "
                        "pre-game screen."
                    )
                    print(
                        "[MATCH] Start the game normally."
                    )

                if not match_ready:
                    state = (
                        "WAITING FOR TOWER MATCH "
                        "{}/{}".format(
                            match_ready_streak,
                            MATCH_READY_CONFIRM_FRAMES,
                        )
                    )

                    previous_gray = cv2.cvtColor(
                        frame,
                        cv2.COLOR_BGR2GRAY,
                    )

                    if SHOW_OUTPUT:
                        out = frame.copy()
                        put_text(
                            out,
                            state,
                            24,
                        )
                        cv2.imshow(
                            OUTPUT_TITLE,
                            out,
                        )

                        key = (
                            cv2.waitKey(1)
                            &
                            0xFF
                        )

                        if key == ord("q"):
                            break

                        if key == 32:
                            auto_click = not auto_click

                    continue

            # --------------------------------------------------------
            # Moving block from temporal difference.
            # --------------------------------------------------------
            detect_started = time.perf_counter()

            moving, _ = detect_moving_block(
                frame,
                previous_gray,
                tracker.last_seen["cy"]
                if tracker.last_seen is not None
                else None,
            )

            detector_ms = (
                time.perf_counter()
                -
                detect_started
            ) * 1000.0

            target = None

            if moving is not None:
                raw_target_candidates = (
                    detect_target_candidates(
                        frame,
                        moving,
                    )
                )

                new_locked_target = (
                    choose_locked_target(
                        raw_target_candidates,
                        locked_target,
                    )
                )

                if (
                    new_locked_target is not None
                ):
                    if (
                        locked_target is None
                        or
                        abs(
                            new_locked_target["cx"]
                            -
                            locked_target["cx"]
                        ) <= TARGET_LOCK_MAX_X_SHIFT
                    ):
                        target_hold_frames = min(
                            TARGET_LOCK_HOLD_FRAMES,
                            target_hold_frames + 1,
                        )

                    else:
                        target_hold_frames = 0

                    locked_target = dict(
                        new_locked_target
                    )

                elif (
                    target_hold_frames
                    <
                    TARGET_LOCK_HOLD_FRAMES
                ):
                    target_hold_frames += 1

                target = (
                    dict(locked_target)
                    if locked_target is not None
                    else None
                )

            current_time = time.perf_counter()

            # --------------------------------------------------------
            # POST-TAP LANDING VERIFICATION
            # --------------------------------------------------------
            if drop_active:
                elapsed_ms = (
                    current_time
                    -
                    tap_started_at
                ) * 1000.0

                new_mover = moving

                # New block must appear clearly ABOVE the block we just
                # dropped. A same-y rectangle is not enough.
                new_block_ready = (
                    new_mover is not None
                    and
                    new_mover["cy"]
                    <=
                    (
                        tap_moving_y
                        -
                        max(
                            LANDING_NEW_MOVER_Y_MIN,
                            tap_moving_width * 0.50,
                        )
                    )
                )

                if new_block_ready:
                    new_target = detect_target_below(
                        frame,
                        new_mover,
                    )

                    # The newly created top stationary block is the actual
                    # result of the previous overlap/cut.
                    actual_landed = new_target

                    actual_error = None
                    actual_overlap_ratio = None

                    if (
                        actual_landed is not None
                        and
                        tap_target is not None
                    ):
                        actual_error = abs(
                            actual_landed["cx"]
                            -
                            tap_target["cx"]
                        )

                        actual_overlap_px = (
                            overlap_width(
                                actual_landed["cx"],
                                actual_landed["w"],
                                tap_target,
                            )
                        )

                        denominator = max(
                            1.0,
                            min(
                                actual_landed["w"],
                                tap_target["w"],
                            ),
                        )

                        actual_overlap_ratio = (
                            actual_overlap_px
                            /
                            denominator
                        )

                        actual_errors.append(
                            actual_error
                        )

                        actual_overlaps.append(
                            actual_overlap_ratio
                        )

                    landing_count += 1
                    landing_times.append(
                        elapsed_ms
                    )

                    if (
                        PRINT_LANDING_LOG
                    ):
                        if actual_landed is None:
                            landed_desc = (
                                "GEOMETRY=NOT_FOUND"
                            )

                        else:
                            landed_desc = (
                                "LANDED "
                                "x0={:.1f} x1={:.1f} "
                                "cx={:.1f} w={:.1f}"
                                .format(
                                    actual_landed["x0"],
                                    actual_landed["x1"],
                                    actual_landed["cx"],
                                    actual_landed["w"],
                                )
                            )

                        print(
                            "[LANDING #{:03d}] "
                            "verify={:.1f}ms | "
                            "pred_err={:.2f}px | "
                            "pred_overlap={:.2f}% | "
                            "actual_err={} | "
                            "actual_overlap={} | "
                            "{}"
                            .format(
                                landing_count,
                                elapsed_ms,
                                tap_prediction_error,
                                tap_expected_overlap * 100.0,
                                (
                                    "{:.2f}px".format(
                                        actual_error
                                    )
                                    if actual_error is not None
                                    else "-"
                                ),
                                (
                                    "{:.2f}%".format(
                                        actual_overlap_ratio
                                        *
                                        100.0
                                    )
                                    if actual_overlap_ratio is not None
                                    else "-"
                                ),
                                landed_desc,
                            )
                        )

                    # Seed the next block immediately. We do NOT leave the
                    # tracker in a stale post-tap state.
                    tracker.reset()
                    reset_motion_detector()


                    last_mover = new_mover
                    last_target = new_target

                    drop_active = False
                    tap_target = None
                    tap_moving_y = 0.0
                    tap_moving_width = 0.0
                    tap_predicted_x = 0.0
                    tap_prediction_error = 0.0
                    tap_expected_overlap = 0.0
                    locked_target = None
                    target_hold_frames = 0

                    last_no_mover_at = current_time

                    state = (
                        "LANDING CONFIRMED - "
                        "NEXT BLOCK TRACKING"
                    )

                elif (
                    elapsed_ms
                    >=
                    LANDING_VERIFY_TIMEOUT_MS
                ):
                    # Never wait indefinitely after a tap. Reset the visual
                    # tracker and immediately continue looking for motion.
                    uncertain_landings += 1

                    tracker.reset()
                    reset_motion_detector()

                    drop_active = False
                    tap_target = None
                    tap_moving_y = 0.0
                    tap_moving_width = 0.0
                    tap_predicted_x = 0.0
                    tap_prediction_error = 0.0
                    tap_expected_overlap = 0.0
                    locked_target = None
                    target_hold_frames = 0

                    last_no_mover_at = current_time

                    state = (
                        "LANDING TIMEOUT - "
                        "IMMEDIATE RESCAN"
                    )

                else:
                    state = (
                        "VERIFY LANDING "
                        "{:.0f}ms".format(
                            elapsed_ms
                        )
                    )

            # --------------------------------------------------------
            # NORMAL TRACK / DROP
            # --------------------------------------------------------
            else:
                if moving is None:
                    tracker.update(
                        None,
                        current_time,
                    )

                    target_hold_frames = min(
                        target_hold_frames + 1,
                        TARGET_LOCK_HOLD_FRAMES,
                    )

                    last_no_mover_at = current_time

                    if game_started:
                        state = (
                            "SCANNING - "
                            "NO MOVING BLOCK"
                        )
                    else:
                        state = (
                            "SCANNING - "
                            "WAITING FOR FIRST BLOCK"
                        )

                else:
                    tracker.update(
                        moving,
                        current_time,
                    )

                    last_mover = moving
                    last_target = target

                    # Ignore countdown/GO-like changes until the mover is
                    # actually in the lower gameplay area.
                    if (
                        not game_started
                        and
                        moving["cy"]
                        <
                        frame_h * MOVING_Y_MIN_RATIO
                    ):
                        tracker.reset()
                        state = (
                            "SCANNING - "
                            "IGNORE START ANIMATION"
                        )

                    else:
                        if (
                            not game_started
                            and
                            tracker.stable()
                        ):
                            game_started = True

                            print(
                                "[GAME] First real moving block "
                                "confirmed."
                            )

                        if (
                            tracker.reversal()
                        ):
                            # Preserve only the latest point after a bounce;
                            # the new direction will be learned immediately.
                            last_point = (
                                tracker.history[-1]
                            )

                            tracker.history.clear()
                            tracker.history.append(
                                last_point
                            )

                            state = (
                                "BOUNCE - "
                                "RELEARNING DIRECTION"
                            )

                        if (
                            not tracker.stable()
                        ):
                            state = (
                                "TRACKING - "
                                "BUILDING VELOCITY"
                            )

                        elif target is None:
                            state = (
                                "TRACKING - "
                                "TARGET NOT YET RESOLVED"
                            )

                        else:
                            vx = tracker.velocity()

                            current_x = (
                                moving["cx"]
                            )

                            left_limit, right_limit = (
                                moving_center_limits(
                                    frame,
                                    moving["w"],
                                )
                            )

                            target_x = (
                                target["cx"]
                            )

                            hit_time = (
                                time_to_target(
                                    current_x,
                                    vx,
                                    target_x,
                                    left_limit,
                                    right_limit,
                                )
                            )

                            if (
                                hit_time is None
                                or
                                hit_time > 2.0
                            ):
                                state = (
                                    "TRACKING - "
                                    "TARGET CROSSING REPLAN"
                                )

                            else:
                                decision_elapsed_ms = (
                                    max(
                                        0.0,
                                        (
                                            time.perf_counter()
                                            -
                                            frame_capture_time
                                        )
                                        *
                                        1000.0,
                                    )
                                )

                                lead_ms = (
                                    decision_elapsed_ms
                                    +
                                    dispatch_ema
                                    +
                                    CLICK_SAFETY_MS
                                    +
                                    timing_bias_ms
                                )

                                lead_ms = max(
                                    CLICK_LEAD_MIN_MS,
                                    min(
                                        CLICK_LEAD_MAX_MS,
                                        lead_ms,
                                    ),
                                )

                                lead = (
                                    lead_ms
                                    /
                                    1000.0
                                )

                                predicted_x = (
                                    reflected_position(
                                        current_x,
                                        vx,
                                        lead,
                                        left_limit,
                                        right_limit,
                                    )
                                )

                                prediction_error = abs(
                                    predicted_x
                                    -
                                    target_x
                                )

                                error_limit = max(
                                    PREDICT_ERROR_ABS_MAX_PX,
                                    target["w"]
                                    *
                                    PREDICT_ERROR_RATIO,
                                )

                                crossing_window = max(
                                    DROP_TIME_WINDOW_MIN_MS
                                    /
                                    1000.0,
                                    min(
                                        DROP_TIME_WINDOW_MAX_MS
                                        /
                                        1000.0,
                                        error_limit
                                        /
                                        max(
                                            abs(vx),
                                            1.0,
                                        ),
                                    ),
                                )

                                predicted_overlap_px = (
                                    overlap_width(
                                        predicted_x,
                                        moving["w"],
                                        target,
                                    )
                                )

                                predicted_overlap_ratio = (
                                    predicted_overlap_px
                                    /
                                    float(
                                        max(
                                            1.0,
                                            min(
                                                moving["w"],
                                                target["w"],
                                            ),
                                        )
                                    )
                                )

                                # The moving block should be predicted to land
                                # at/near the target centre. This is stricter
                                # than merely requiring "some overlap".
                                ready = (
                                    abs(
                                        hit_time
                                        -
                                        lead
                                    )
                                    <=
                                    crossing_window
                                    and
                                    prediction_error
                                    <=
                                    error_limit
                                    and
                                    predicted_overlap_ratio
                                    >=
                                    0.92
                                )

                                if not ready:
                                    state = (
                                        "TRACKING - "
                                        "PREDICTING DROP"
                                        " / TARGET LOCK "
                                        f"{target_hold_frames}/"
                                        f"{TARGET_LOCK_HOLD_FRAMES}"
                                    )

                                elif not auto_click:
                                    state = (
                                        "DROP READY - "
                                        "AUTO CLICK OFF"
                                    )

                                else:
                                    # Accuracy comes from the predicted block
                                    # centre, not from a random screen point.
                                    # One click only.
                                    click_x_local = (
                                        predicted_x
                                    )

                                    click_y_local = (
                                        moving["cy"]
                                    )

                                    screen_x = (
                                        region[0]
                                        +
                                        click_x_local
                                    )

                                    screen_y = (
                                        region[1]
                                        +
                                        click_y_local
                                    )

                                    if (
                                        PRINT_DROP_LOG
                                    ):
                                        print(
                                            "[DROP #{:03d}] "
                                            "MOVER x0={:.1f} x1={:.1f} "
                                            "cx={:.1f} w={:.1f} y={:.1f} | "
                                            "TARGET-LOCK x0={:.1f} x1={:.1f} "
                                            "cx={:.1f} w={:.1f} "
                                            "hold={}/{} | "
                                            "V={:+.1f}px/s | "
                                            "hit={:.1f}ms | "
                                            "decision={:.1f}ms | "
                                            "lead={:.1f}ms | "
                                            "pred={:.1f} | "
                                            "error={:.2f}px | "
                                            "overlap={:.2f}% | "
                                            "click=({:.1f},{:.1f})"
                                            .format(
                                                drop_count + 1,
                                                moving["x0"],
                                                moving["x1"],
                                                moving["cx"],
                                                moving["w"],
                                                moving["cy"],
                                                target["x0"],
                                                target["x1"],
                                                target["cx"],
                                                target["w"],
                                                target_hold_frames,
                                                TARGET_LOCK_HOLD_FRAMES,
                                                vx,
                                                hit_time * 1000.0,
                                                decision_elapsed_ms,
                                                lead_ms,
                                                predicted_x,
                                                prediction_error,
                                                predicted_overlap_ratio
                                                *
                                                100.0,
                                                click_x_local,
                                                click_y_local,
                                            )
                                        )

                                    tap_started_at = (
                                        time.perf_counter()
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

                                    dispatch_times.append(
                                        dispatch_ms
                                    )

                                    if (
                                        dispatch_ema
                                        is
                                        None
                                    ):
                                        dispatch_ema = (
                                            float(dispatch_ms)
                                        )

                                    else:
                                        dispatch_ema = (
                                            CLICK_DISPATCH_EMA_ALPHA
                                            *
                                            float(dispatch_ms)
                                            +
                                            (
                                                1.0
                                                -
                                                CLICK_DISPATCH_EMA_ALPHA
                                            )
                                            *
                                            dispatch_ema
                                        )

                                    if ok:
                                        drop_count += 1

                                        timing_bias_ms = max(
                                            -18.0,
                                            min(
                                                18.0,
                                                timing_bias_ms,
                                            ),
                                        )

                                        drop_active = True

                                        tap_target = dict(
                                            target
                                        )

                                        tap_moving_y = (
                                            moving["cy"]
                                        )

                                        tap_moving_width = (
                                            moving["w"]
                                        )

                                        tap_predicted_x = (
                                            predicted_x
                                        )

                                        tap_prediction_error = (
                                            prediction_error
                                        )

                                        tap_expected_overlap = (
                                            predicted_overlap_ratio
                                        )

                                        state = (
                                            "ONE CLICK SENT"
                                        )

                                    else:
                                        tap_failures += 1

                                        tracker.reset()

                                        state = (
                                            "CLICK FAILED "
                                            "{}/2".format(
                                                sent
                                            )
                                        )

            # --------------------------------------------------------
            # Compact live tracking log.
            # --------------------------------------------------------
            now_for_log = time.perf_counter()

            if (
                PRINT_TRACK_LOG
                and
                match_ready
                and
                moving is not None
                and
                (
                    now_for_log
                    -
                    last_track_log_at
                )
                *
                1000.0
                >=
                TRACK_LOG_INTERVAL_MS
            ):
                last_track_log_at = (
                    now_for_log
                )

                target_desc = (
                    "NONE"
                    if target is None
                    else
                    "x0={:.1f} x1={:.1f} "
                    "cx={:.1f} w={:.1f}"
                    .format(
                        target["x0"],
                        target["x1"],
                        target["cx"],
                        target["w"],
                    )
                )

                print(
                    "[TRACK] "
                    "mover x0={:.1f} x1={:.1f} "
                    "cx={:.1f} w={:.1f} y={:.1f} | "
                    "target={} | "
                    "v={:+.1f}px/s | "
                    "capture={:.2f}ms | "
                    "detect={:.2f}ms | "
                    "state={}"
                    .format(
                        moving["x0"],
                        moving["x1"],
                        moving["cx"],
                        moving["w"],
                        moving["cy"],
                        target_desc,
                        tracker.velocity(),
                        capture_ms,
                        detector_ms,
                        state,
                    )
                )

            # --------------------------------------------------------
            # Game over / no moving block timeout.
            # --------------------------------------------------------
            if (
                game_started
                and
                not drop_active
                and
                moving is None
                and
                (
                    current_time
                    -
                    last_no_mover_at
                )
                *
                1000.0
                >=
                NO_MOVER_STOP_MS
            ):
                state = (
                    "NO MOVING BLOCK - "
                    "GAME STOP / RESCAN"
                )

                print(
                    "[GAME] No moving block for "
                    "{:.0f}ms. Stopping this run."
                    .format(
                        (
                            current_time
                            -
                            last_no_mover_at
                        )
                        *
                        1000.0
                    )
                )

                break

            # --------------------------------------------------------
            # Keep current frame for next temporal-difference pass.
            # --------------------------------------------------------
            previous_gray = cv2.cvtColor(
                frame,
                cv2.COLOR_BGR2GRAY,
            )

            # --------------------------------------------------------
            # Optional visual output.
            # --------------------------------------------------------
            if (
                SHOW_OUTPUT
                and
                frame_index % DISPLAY_EVERY == 0
            ):
                out = frame.copy()

                if moving is not None:
                    draw_box(
                        out,
                        moving,
                        (0, 255, 0),
                    )

                if target is not None:
                    draw_box(
                        out,
                        target,
                        (255, 255, 0),
                    )

                    target_x = int(
                        round(
                            target["cx"]
                        )
                    )

                    cv2.line(
                        out,
                        (
                            target_x,
                            int(target["y0"]),
                        ),
                        (
                            target_x,
                            int(target["y1"]),
                        ),
                        (255, 255, 255),
                        1,
                        cv2.LINE_AA,
                    )

                put_text(
                    out,
                    "STATE: " + state,
                    22,
                )

                put_text(
                    out,
                    "FPS: {:.1f}".format(
                        frame_count
                        /
                        max(
                            1e-6,
                            time.perf_counter()
                            -
                            run_start,
                        )
                    ),
                    42,
                )

                put_text(
                    out,
                    "Moving: {}".format(
                        "YES"
                        if moving is not None
                        else
                        "NO"
                    ),
                    62,
                )

                put_text(
                    out,
                    "Target: {}".format(
                        "YES"
                        if target is not None
                        else
                        "NO"
                    ),
                    82,
                )

                put_text(
                    out,
                    "Velocity: {:.1f}px/s".format(
                        tracker.velocity()
                    ),
                    102,
                )

                put_text(
                    out,
                    "Drops: {}  Landings: {}".format(
                        drop_count,
                        landing_count,
                    ),
                    122,
                )

                cv2.imshow(
                    OUTPUT_TITLE,
                    out,
                )

                key = (
                    cv2.waitKey(1)
                    &
                    0xFF
                )

                if key == ord("q"):
                    break

                if key == 32:
                    auto_click = not auto_click

                elif key == ord("r"):
                    match_ready = False
                    match_ready_streak = 0
                    game_started = False
                    drop_active = False
                    previous_gray = None

                    tracker.reset()

                    last_mover = None
                    last_target = None
                    locked_target = None
                    target_hold_frames = 0

                    tap_target = None
                    tap_predicted_x = 0.0
                    tap_prediction_error = 0.0
                    tap_expected_overlap = 0.0

                    actual_errors.clear()
                    actual_overlaps.clear()
                    dispatch_times.clear()
                    landing_times.clear()

                    frame_count = 0
                    drop_count = 0
                    landing_count = 0
                    tap_failures = 0
                    uncertain_landings = 0

                    dispatch_ema = (
                        CLICK_DISPATCH_INITIAL_MS
                    )

                    timing_bias_ms = 0.0

                    run_start = (
                        time.perf_counter()
                    )

                    print(
                        "[CONTROL] Full state/stats reset."
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
        1e-6,
        time.perf_counter()
        -
        run_start,
    )

    print("")
    print("=" * 72)
    print("                    FINAL TOWER TEST")
    print("=" * 72)
    print(
        "Frames                  :",
        frame_count,
    )
    print(
        "Loop FPS                : {:.2f}".format(
            frame_count
            /
            elapsed
        )
    )
    print(
        "Drop decisions          :",
        drop_count,
    )
    print(
        "Landing confirmed       :",
        landing_count,
    )
    print(
        "Tap failures            :",
        tap_failures,
    )
    print(
        "Uncertain landings      :",
        uncertain_landings,
    )

    if actual_errors:
        print(
            "Avg ACTUAL landing error: "
            "{:.2f} px".format(
                float(
                    np.mean(
                        actual_errors
                    )
                )
            )
        )

        print(
            "Max ACTUAL landing error: "
            "{:.2f} px".format(
                float(
                    np.max(
                        actual_errors
                    )
                )
            )
        )

    else:
        print(
            "Avg ACTUAL landing error: -"
        )

        print(
            "Max ACTUAL landing error: -"
        )

    if actual_overlaps:
        print(
            "Avg ACTUAL overlap      : "
            "{:.2f}%".format(
                100.0
                *
                float(
                    np.mean(
                        actual_overlaps
                    )
                )
            )
        )

        print(
            "Actual >=90% placements: {} / {}".format(
                sum(
                    value >= 0.90
                    for value in actual_overlaps
                ),
                len(
                    actual_overlaps
                ),
            )
        )

    else:
        print(
            "Avg ACTUAL overlap      : -"
        )

        print(
            "Actual >=90% placements: -"
        )

    if dispatch_times:
        print(
            "Avg single-click dispatch: "
            "{:.2f} ms".format(
                float(
                    np.mean(
                        dispatch_times
                    )
                )
            )
        )

    else:
        print(
            "Avg single-click dispatch: -"
        )

    if landing_times:
        print(
            "Avg landing verify time  : "
            "{:.2f} ms".format(
                float(
                    np.mean(
                        landing_times
                    )
                )
            )
        )

    else:
        print(
            "Avg landing verify time  : -"
        )

    print(
        "Final timing bias        : "
        "{:+.2f} ms".format(
            timing_bias_ms
        )
    )

    print("=" * 72)


if __name__ == "__main__":
    main()
