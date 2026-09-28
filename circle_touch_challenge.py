import ctypes
from ctypes import wintypes
import math
import time
from typing import Optional, Tuple

import cv2
import numpy as np

# ============================================================
# CIRCLE TOUCH CHALLENGE
# Android phone -> scrcpy window -> DXCam/MSS -> OpenCV -> Windows click
# ============================================================

SCRCPY_TITLE = "CHESS_MOBILE"
OUTPUT_TITLE = "CIRCLE TOUCH - TEST OUTPUT"

AUTO_CLICK_START = True
SHOW_OUTPUT = True
DISPLAY_EVERY = 1

# Detection
MIN_VALUE = 65
MIN_SATURATION = 0
MIN_AREA = 900
MIN_RADIUS = 24
MAX_RADIUS_RATIO = 0.48

# Screenshot-style target validation.
# The target must be large enough, filled, internally uniform, and have a
# strong circular boundary against the surrounding dark background.
HOUGH_SCALE = 0.50
HOUGH_DP = 1.20
HOUGH_PARAM1 = 90
HOUGH_PARAM2 = 28
HOUGH_MIN_RADIUS = 24
HOUGH_MAX_RADIUS_RATIO = 0.75
HOUGH_MIN_DIST = 45

TARGET_CONFIRM_FRAMES = 3
CORE_COLOR_CONSISTENCY = 0.92
CORE_COLOR_DISTANCE = 20.0
CORE_GRAY_STD_MAX = 8.0
CORE_EDGE_DENSITY_MAX = 0.012
OUTER_CONTRAST_MIN = 25.0
VISIBLE_ARC_MIN = 0.55
MIN_CIRCULARITY = 0.68
MIN_CIRCULARITY_EDGE = 0.42
MIN_FILL = 0.52
MIN_SOLIDITY = 0.84
MIN_ASPECT = 0.60
MAX_ASPECT = 1.67
MORPH_K = 3

# The screenshots show a yellow Finish button at upper-right.
# Only that common button area is ignored; circles elsewhere are allowed.
FINISH_X = 0.58
FINISH_Y = 0.18

# Keep clicking the detected center until the circle really disappears
# from the scrcpy capture.
RECLICK_INTERVAL_MS = 40
GONE_CONFIRM_FRAMES = 3

# End-of-game / result page handling.
# The supplied result-page screenshot has a large yellow rounded
# "Play again" button in the lower-right area.
PLAY_AGAIN_CONFIRM_FRAMES = 2
PLAY_AGAIN_GONE_CONFIRM_FRAMES = 3
PLAY_AGAIN_RECLICK_INTERVAL_MS = 120

PLAY_AGAIN_X_MIN = 0.48
PLAY_AGAIN_Y_MIN = 0.80
PLAY_AGAIN_W_MIN = 0.28
PLAY_AGAIN_H_MIN = 0.055
PLAY_AGAIN_ASPECT_MIN = 1.90
PLAY_AGAIN_ASPECT_MAX = 3.50
PLAY_AGAIN_AREA_RATIO_MIN = 0.006
PLAY_AGAIN_H_MIN_DEG = 18
PLAY_AGAIN_H_MAX_DEG = 42
PLAY_AGAIN_S_MIN = 80
PLAY_AGAIN_V_MIN = 140

OVERLAY_MS = 170
REGION_REFRESH_SEC = 0.25

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
    h = user32.FindWindowW(None, SCRCPY_TITLE)
    return h if h else None


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
        except Exception as e:
            print("[WARN] DXCam unavailable:", e)
            try:
                import mss
                self.mss = mss.mss()
                self.backend = "MSS"
            except Exception as e2:
                raise RuntimeError("Install dxcam or mss") from e2

    def set_region(self, r):
        if r:
            self.region = r

    def grab(self):
        if not self.region:
            return None
        l, t, r, b = self.region
        if self.backend == "DXCAM":
            return self.dx.grab(region=(l, t, r, b))
        frame = np.asarray(self.mss.grab({
            "left": l, "top": t, "width": r - l, "height": b - t
        }))
        return cv2.cvtColor(frame, cv2.COLOR_BGRA2BGR)


    def close(self):
        # Deterministic DXCam/MSS cleanup.
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
            finally:
                self.dx = None

        if self.mss is not None:
            try:
                self.mss.close()
            except Exception:
                pass
            finally:
                self.mss = None


class Tapper:
    """Send reliable real mouse input to the existing scrcpy client."""

    def tap(self, x, y, hwnd):
        t0 = time.perf_counter()

        try:
            user32.SetForegroundWindow(hwnd)

            px = int(round(x))
            py = int(round(y))

            if not user32.SetCursorPos(px, py):
                return (time.perf_counter() - t0) * 1000.0, False

            inputs = (INPUT * 2)()

            inputs[0].type = INPUT_MOUSE
            inputs[0].mi = MOUSEINPUT(
                0, 0, 0,
                MOUSEEVENTF_LEFTDOWN,
                0, 0
            )

            inputs[1].type = INPUT_MOUSE
            inputs[1].mi = MOUSEINPUT(
                0, 0, 0,
                MOUSEEVENTF_LEFTUP,
                0, 0
            )

            sent = user32.SendInput(
                2,
                inputs,
                ctypes.sizeof(INPUT)
            )

            if sent != 2:
                return (time.perf_counter() - t0) * 1000.0, False

            return (time.perf_counter() - t0) * 1000.0, True

        except Exception:
            return (time.perf_counter() - t0) * 1000.0, False

    def close(self):
        pass


def target_mask(frame):
    hsv = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)
    h, s, v = cv2.split(hsv)
    mask = ((v >= MIN_VALUE) & (s >= MIN_SATURATION)).astype(np.uint8) * 255
    k = np.ones((MORPH_K, MORPH_K), np.uint8)
    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, k, iterations=1)
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, k, iterations=1)
    return mask


# Backward-compatible name used by the detector.
def make_target_mask(frame):
    return target_mask(frame)


def ignored_finish(x, y, w, h, W, H):
    return x >= int(W * FINISH_X) and y <= int(H * FINISH_Y)


def detect_play_again_page(frame):
    """
    Detect the supplied end/result page from its distinctive lower-right
    yellow rounded "Play again" button.

    This detector is intentionally position/shape constrained so a normal
    yellow circle target during the game is not treated as the result page.
    Returns the button center/bbox when the page is visible, otherwise None.
    """
    H, W = frame.shape[:2]

    x0 = int(W * PLAY_AGAIN_X_MIN)
    y0 = int(H * PLAY_AGAIN_Y_MIN)

    roi = frame[y0:H, x0:W]
    if roi.size == 0:
        return None

    hsv = cv2.cvtColor(roi, cv2.COLOR_BGR2HSV)

    lower = np.array([
        PLAY_AGAIN_H_MIN_DEG,
        PLAY_AGAIN_S_MIN,
        PLAY_AGAIN_V_MIN
    ], dtype=np.uint8)

    upper = np.array([
        PLAY_AGAIN_H_MAX_DEG,
        255,
        255
    ], dtype=np.uint8)

    mask = cv2.inRange(hsv, lower, upper)

    k = np.ones((5, 5), np.uint8)
    mask = cv2.morphologyEx(
        mask,
        cv2.MORPH_CLOSE,
        k,
        iterations=2
    )

    contours, _ = cv2.findContours(
        mask,
        cv2.RETR_EXTERNAL,
        cv2.CHAIN_APPROX_SIMPLE
    )

    min_area = float(W * H) * PLAY_AGAIN_AREA_RATIO_MIN
    best = None
    best_score = -1.0

    for contour in contours:
        area = cv2.contourArea(contour)
        if area < min_area:
            continue

        x, y, w, h = cv2.boundingRect(contour)

        abs_x = x0 + x
        abs_y = y0 + y

        if abs_x < int(W * PLAY_AGAIN_X_MIN):
            continue
        if abs_y < int(H * PLAY_AGAIN_Y_MIN):
            continue
        if w < int(W * PLAY_AGAIN_W_MIN):
            continue
        if h < int(H * PLAY_AGAIN_H_MIN):
            continue

        aspect = w / float(max(h, 1))
        if not (
            PLAY_AGAIN_ASPECT_MIN
            <= aspect
            <= PLAY_AGAIN_ASPECT_MAX
        ):
            continue

        area_ratio = area / float(max(w * h, 1))
        if area_ratio < 0.50:
            continue

        # The button is a broad rounded rectangle, not a circle.
        shape_quality = max(
            0.0,
            1.0 - abs(aspect - 2.5) / 2.5
        )

        size_quality = min(
            1.0,
            area / float(max(min_area * 2.0, 1.0))
        )

        score = (
            0.65 * area_ratio
            + 0.35 * shape_quality
            + 0.10 * size_quality
        )

        if score <= best_score:
            continue

        best_score = score

        center_x = abs_x + int(round(w * 0.50))
        center_y = abs_y + int(round(h * 0.50))

        best = {
            "cx": int(center_x),
            "cy": int(center_y),
            "bbox": (abs_x, abs_y, w, h),
            "area": float(area),
            "score": float(score),
            "aspect": float(aspect),
            "area_ratio": float(area_ratio),
        }

    return best


def fit_circle_least_squares(contour):
    """
    Fit a geometric circle to contour boundary points.

    This is more center-focused than using the centroid of a clipped/anti-
    aliased contour. It is especially useful when the target is not perfectly
    filled due to screen capture antialiasing.
    """
    pts = contour.reshape(-1, 2).astype(np.float64)

    if len(pts) < 6:
        return None

    # Keep it fast while retaining enough boundary points.
    if len(pts) > 400:
        idx = np.linspace(
            0,
            len(pts) - 1,
            400
        ).astype(np.int32)
        pts = pts[idx]

    x = pts[:, 0]
    y = pts[:, 1]

    # x^2 + y^2 = 2*cx*x + 2*cy*y + c
    A = np.column_stack(
        (2.0 * x, 2.0 * y, np.ones_like(x))
    )

    b = x * x + y * y

    try:
        solution, _, _, _ = np.linalg.lstsq(
            A,
            b,
            rcond=None
        )

        cx, cy, c = solution
        radius_sq = c + cx * cx + cy * cy

        if radius_sq <= 0:
            return None

        radius = math.sqrt(radius_sq)

        if not np.isfinite(cx + cy + radius):
            return None

        return float(cx), float(cy), float(radius)

    except Exception:
        return None


def refine_center_from_mask(
    mask,
    x,
    y,
    w,
    h,
    fallback_cx,
    fallback_cy
):
    """
    Refine the center using the distance transform.

    For a filled disk, the point farthest from the boundary is at the disk
    center. This is a strong sub-contour center estimate.
    """
    roi = mask[
        y:y + h,
        x:x + w
    ]

    if roi.size == 0:
        return fallback_cx, fallback_cy, 0.0

    distance = cv2.distanceTransform(
        roi,
        cv2.DIST_L2,
        5
    )

    _, max_distance, _, max_loc = cv2.minMaxLoc(
        distance
    )

    if max_distance <= 0:
        return fallback_cx, fallback_cy, 0.0

    refined_x = x + max_loc[0]
    refined_y = y + max_loc[1]

    return (
        float(refined_x),
        float(refined_y),
        float(max_distance)
    )


def detect_circle_legacy(frame):
    """
    Legacy contour detector retained for reference/debug only.

    Properties supported:
      - arbitrary color
      - arbitrary size
      - arbitrary position
      - partial/edge target
      - dark background

    Center calculation uses a combination of:
      1) contour geometry
      2) least-squares circle fit
      3) distance-transform interior center

    The detector returns ONLY the strongest valid circle candidate.
    """
    H, W = frame.shape[:2]

    mask = make_target_mask(frame)

    contours, _ = cv2.findContours(
        mask,
        cv2.RETR_EXTERNAL,
        cv2.CHAIN_APPROX_SIMPLE
    )

    best = None
    best_score = -1.0

    max_radius = max(
        MIN_RADIUS,
        min(W, H) * MAX_RADIUS_RATIO
    )

    for contour in contours:

        area = cv2.contourArea(contour)

        if area < MIN_AREA:
            continue

        x, y, w, h = cv2.boundingRect(contour)

        if (
            w < 2 * MIN_RADIUS or
            h < 2 * MIN_RADIUS
        ):
            continue

        aspect = w / float(
            max(h, 1)
        )

        if not (
            MIN_ASPECT <= aspect <= MAX_ASPECT
        ):
            continue

        perimeter = cv2.arcLength(
            contour,
            True
        )

        if perimeter <= 1e-6:
            continue

        circularity = (
            4.0 *
            math.pi *
            area /
            (perimeter * perimeter)
        )

        touches_edge = (
            x <= 1 or
            y <= 1 or
            x + w >= W - 1 or
            y + h >= H - 1
        )

        required_circularity = (
            MIN_CIRCULARITY_EDGE
            if touches_edge
            else MIN_CIRCULARITY
        )

        if circularity < required_circularity:
            continue

        fill = (
            area /
            float(w * h)
        )

        if fill < MIN_FILL:
            continue

        # A circle should be compact and close to convex.
        hull = cv2.convexHull(contour)
        hull_area = cv2.contourArea(hull)

        if hull_area <= 0:
            continue

        solidity = area / float(hull_area)

        if solidity < MIN_SOLIDITY:
            continue

        # ----------------------------------------------------
        # Candidate radius from the actual contour.
        # ----------------------------------------------------

        (
            (enclose_cx, enclose_cy),
            enclose_radius
        ) = cv2.minEnclosingCircle(
            contour
        )

        if not (
            MIN_RADIUS <=
            enclose_radius <=
            max_radius
        ):
            continue

        # ----------------------------------------------------
        # Geometric least-squares fit.
        # ----------------------------------------------------

        fitted = fit_circle_least_squares(
            contour
        )

        fit_cx = enclose_cx
        fit_cy = enclose_cy
        fit_radius = enclose_radius

        if fitted is not None:

            fit_cx, fit_cy, fit_radius = fitted

            # Keep obviously unstable edge fits away.
            if not (
                MIN_RADIUS <=
                fit_radius <=
                max_radius
            ):
                fit_cx = enclose_cx
                fit_cy = enclose_cy
                fit_radius = enclose_radius

        # ----------------------------------------------------
        # Distance-transform center.
        # ----------------------------------------------------

        dt_cx, dt_cy, dt_radius = (
            refine_center_from_mask(
                mask,
                x,
                y,
                w,
                h,
                fit_cx,
                fit_cy
            )
        )

        # ----------------------------------------------------
        # Choose/reconcile center.
        #
        # Full circle:
        #   fit center is usually strongest.
        #
        # Filled circle:
        #   DT center gives a strong interior center.
        #
        # Edge circle:
        #   combine fit and enclosing center carefully.
        # ----------------------------------------------------

        if not touches_edge:

            center_x = (
                0.35 * fit_cx +
                0.65 * dt_cx
            )

            center_y = (
                0.35 * fit_cy +
                0.65 * dt_cy
            )

            estimated_radius = (
                0.70 * fit_radius +
                0.30 * dt_radius
            )

        else:

            # On a clipped circle, use the fit if it looks stable.
            fit_inside = (
                -0.35 * max(enclose_radius, 1)
                <= fit_cx
                <= W + 0.35 * max(enclose_radius, 1)
                and
                -0.35 * max(enclose_radius, 1)
                <= fit_cy
                <= H + 0.35 * max(enclose_radius, 1)
            )

            if fit_inside:

                center_x = (
                    0.70 * fit_cx +
                    0.30 * enclose_cx
                )

                center_y = (
                    0.70 * fit_cy +
                    0.30 * enclose_cy
                )

                estimated_radius = (
                    0.70 * fit_radius +
                    0.30 * enclose_radius
                )

            else:

                center_x = enclose_cx
                center_y = enclose_cy
                estimated_radius = enclose_radius

        center_x = int(
            round(
                max(
                    0,
                    min(
                        W - 1,
                        center_x
                    )
                )
            )
        )

        center_y = int(
            round(
                max(
                    0,
                    min(
                        H - 1,
                        center_y
                    )
                )
            )
        )

        # ----------------------------------------------------
        # Radial consistency check.
        # This rejects bright UI rectangles/text while remaining
        # permissive for targets touching the screen edge.
        # ----------------------------------------------------
        contour_pts = contour.reshape(-1, 2).astype(np.float64)

        if len(contour_pts) >= 8:
            radial = np.sqrt(
                (contour_pts[:, 0] - center_x) ** 2 +
                (contour_pts[:, 1] - center_y) ** 2
            )

            radial_median = float(np.median(radial))
            radial_error = float(
                np.median(np.abs(radial - radial_median))
            )

            radial_limit = (
                max(4.0, radial_median * 0.28)
                if touches_edge
                else max(3.0, radial_median * 0.16)
            )

            if radial_error > radial_limit:
                continue

        # ----------------------------------------------------
        # Center sanity check.
        # ----------------------------------------------------

        box_cx = x + w * 0.5
        box_cy = y + h * 0.5

        offset = math.hypot(
            center_x - box_cx,
            center_y - box_cy
        )

        max_offset = (
            min(w, h) * 0.48
            if touches_edge
            else min(w, h) * 0.20
        )

        if offset > max_offset:
            continue

        # ----------------------------------------------------
        # Target center color/brightness.
        # ----------------------------------------------------

        center_bgr = tuple(
            int(v)
            for v in frame[
                center_y,
                center_x
            ]
        )

        # ----------------------------------------------------
        # Shape score.
        # ----------------------------------------------------

        aspect_quality = max(
            0.0,
            1.0 -
            abs(
                math.log(
                    max(
                        aspect,
                        1e-6
                    )
                )
            )
        )

        score = (
            0.50 *
            min(
                circularity,
                1.0
            )
            +
            0.30 *
            min(
                fill,
                1.0
            )
            +
            0.20 *
            aspect_quality
        )

        # A clean interior distance should be non-trivial.
        if dt_radius >= MIN_RADIUS * 0.55:
            score += 0.03

        if touches_edge:
            score -= 0.02

        if score <= best_score:
            continue

        best_score = score

        best = {
            "cx": center_x,
            "cy": center_y,
            "r": float(
                max(
                    MIN_RADIUS,
                    estimated_radius
                )
            ),
            "bbox": (
                x,
                y,
                w,
                h
            ),
            "area": float(area),
            "score": float(score),
            "circularity": float(circularity),
            "fill": float(fill),
            "solidity": float(solidity),
            "edge": bool(touches_edge),
            "bgr": center_bgr,
            "fit_center": (
                float(fit_cx),
                float(fit_cy)
            ),
            "dt_center": (
                float(dt_cx),
                float(dt_cy)
            ),
            "dt_radius": float(dt_radius)
        }

    return best



def detect_circle_hough(frame):
    """
    Strict detector for the actual challenge circles shown in the supplied
    screenshots.

    Accepted target:
      * large filled circle
      * one coherent interior colour
      * clean interior (no object/text inside)
      * strong boundary against the dark background
      * can be partially clipped by a screen edge

    It is deliberately colour-independent: the circle may change colour.
    """
    H, W = frame.shape[:2]

    small_w = max(
        1,
        int(round(W * HOUGH_SCALE))
    )
    small_h = max(
        1,
        int(round(H * HOUGH_SCALE))
    )

    small = cv2.resize(
        frame,
        (small_w, small_h),
        interpolation=cv2.INTER_AREA
    )

    gray = cv2.cvtColor(
        small,
        cv2.COLOR_BGR2GRAY
    )

    gray = cv2.GaussianBlur(
        gray,
        (9, 9),
        1.4
    )

    max_radius = max(
        HOUGH_MIN_RADIUS,
        int(
            round(
                min(small_w, small_h) *
                HOUGH_MAX_RADIUS_RATIO
            )
        )
    )

    circles = cv2.HoughCircles(
        gray,
        cv2.HOUGH_GRADIENT,
        dp=HOUGH_DP,
        minDist=HOUGH_MIN_DIST,
        param1=HOUGH_PARAM1,
        param2=HOUGH_PARAM2,
        minRadius=HOUGH_MIN_RADIUS,
        maxRadius=max_radius
    )

    if circles is None:
        return None

    edges = cv2.Canny(
        gray,
        40,
        100
    )

    yy, xx = np.ogrid[
        :small_h,
        :small_w
    ]

    best = None
    best_score = -1.0

    for circle in np.round(
        circles[0],
        2
    ):
        scx, scy, sr = (
            float(circle[0]),
            float(circle[1]),
            float(circle[2])
        )

        if not (
            np.isfinite(scx) and
            np.isfinite(scy) and
            np.isfinite(sr)
        ):
            continue

        if sr < HOUGH_MIN_RADIUS:
            continue

        rr = (
            (xx - scx) ** 2 +
            (yy - scy) ** 2
        )

        # Core is deliberately central. It must be a uniform filled area.
        core = (
            rr <=
            (sr * 0.50) ** 2
        )

        # A slightly wider interior is used to make sure the target does
        # not contain text/an object/another small circle.
        inner = (
            rr <=
            (sr * 0.72) ** 2
        )

        # Outside ring is compared against the core colour.
        outer_ring = (
            (rr >= (sr * 1.04) ** 2) &
            (rr <= (sr * 1.18) ** 2)
        )

        core_pixels = small[core]
        inner_gray = gray[inner]
        ring_pixels = small[outer_ring]

        if (
            len(core_pixels) < 100 or
            len(ring_pixels) < 50
        ):
            continue

        median_bgr = np.median(
            core_pixels,
            axis=0
        ).astype(np.float32)

        colour_distance = np.linalg.norm(
            core_pixels.astype(np.float32) -
            median_bgr,
            axis=1
        )

        consistency = float(
            np.mean(
                colour_distance <=
                CORE_COLOR_DISTANCE
            )
        )

        if consistency < CORE_COLOR_CONSISTENCY:
            continue

        core_std = float(
            np.std(
                gray[core]
            )
        )

        if core_std > CORE_GRAY_STD_MAX:
            continue

        # Any object/mark inside the circle creates internal edges.
        core_edge_density = float(
            np.mean(
                edges[inner] > 0
            )
        )

        if core_edge_density > CORE_EDGE_DENSITY_MAX:
            continue

        if len(ring_pixels) < 50:
            continue

        outer_median = np.median(
            ring_pixels,
            axis=0
        ).astype(np.float32)

        outer_contrast = float(
            np.linalg.norm(
                median_bgr -
                outer_median
            )
        )

        if outer_contrast < OUTER_CONTRAST_MIN:
            continue

        # Estimate how much of the circle circumference remains visible.
        angles = np.linspace(
            0.0,
            2.0 * math.pi,
            72,
            endpoint=False
        )

        sx = np.rint(
            scx +
            sr *
            np.cos(angles)
        ).astype(np.int32)

        sy = np.rint(
            scy +
            sr *
            np.sin(angles)
        ).astype(np.int32)

        visible = (
            (sx >= 0) &
            (sx < small_w) &
            (sy >= 0) &
            (sy < small_h)
        )

        visible_arc = float(
            np.mean(
                visible
            )
        )

        if visible_arc < VISIBLE_ARC_MIN:
            continue

        # Target centre must itself be on-screen because that is where
        # the physical click will be delivered.
        if not (
            0 <= scx < small_w and
            0 <= scy < small_h
        ):
            continue

        # Small edge strength bonus: the target's perimeter should actually
        # be visible, rather than being a smooth background patch.
        circle_samples = 96
        sample_angles = np.linspace(
            0.0,
            2.0 * math.pi,
            circle_samples,
            endpoint=False
        )

        ex = np.rint(
            scx +
            sr *
            np.cos(sample_angles)
        ).astype(np.int32)

        ey = np.rint(
            scy +
            sr *
            np.sin(sample_angles)
        ).astype(np.int32)

        valid_edge = (
            (ex >= 0) &
            (ex < small_w) &
            (ey >= 0) &
            (ey < small_h)
        )

        if np.any(valid_edge):
            edge_strength = float(
                np.mean(
                    gradient_magnitude_at_points(
                        gray,
                        ex[valid_edge],
                        ey[valid_edge]
                    )
                )
            )
        else:
            edge_strength = 0.0

        # Do not require a huge Sobel magnitude because the supplied
        # screenshots have anti-aliased circle edges.
        if edge_strength < 6.0:
            continue

        actual_cx = int(
            round(
                scx /
                HOUGH_SCALE
            )
        )
        actual_cy = int(
            round(
                scy /
                HOUGH_SCALE
            )
        )
        actual_r = float(
            sr /
            HOUGH_SCALE
        )

        actual_cx = max(
            0,
            min(
                W - 1,
                actual_cx
            )
        )
        actual_cy = max(
            0,
            min(
                H - 1,
                actual_cy
            )
        )

        x0 = max(
            0,
            int(
                round(
                    actual_cx -
                    actual_r
                )
            )
        )
        y0 = max(
            0,
            int(
                round(
                    actual_cy -
                    actual_r
                )
            )
        )
        x1 = min(
            W - 1,
            int(
                round(
                    actual_cx +
                    actual_r
                )
            )
        )
        y1 = min(
            H - 1,
            int(
                round(
                    actual_cy +
                    actual_r
                )
            )
        )

        # Score is mostly based on the target-specific properties.
        score = (
            0.45 *
            consistency
            +
            0.25 *
            min(
                outer_contrast / 180.0,
                1.0
            )
            +
            0.15 *
            visible_arc
            +
            0.10 *
            min(
                edge_strength / 60.0,
                1.0
            )
            +
            0.05 *
            min(
                sr / 40.0,
                1.0
            )
        )

        if score <= best_score:
            continue

        best_score = score

        best = {
            "cx": actual_cx,
            "cy": actual_cy,
            "r": max(
                float(MIN_RADIUS),
                actual_r
            ),
            "bbox": (
                x0,
                y0,
                max(
                    1,
                    x1 - x0 + 1
                ),
                max(
                    1,
                    y1 - y0 + 1
                )
            ),
            "area": float(
                math.pi *
                actual_r *
                actual_r
            ),
            "score": float(score),
            "circularity": 1.0,
            "fill": 1.0,
            "solidity": 1.0,
            "edge": bool(
                actual_cx - actual_r < 1
                or
                actual_cy - actual_r < 1
                or
                actual_cx + actual_r >= W - 1
                or
                actual_cy + actual_r >= H - 1
            ),
            "bgr": tuple(
                int(v)
                for v in frame[
                    actual_cy,
                    actual_cx
                ]
            ),
            "fit_center": (
                float(actual_cx),
                float(actual_cy)
            ),
            "dt_center": (
                float(actual_cx),
                float(actual_cy)
            ),
            "dt_radius": float(actual_r),
            "color_consistency": float(
                consistency
            ),
            "core_gray_std": float(
                core_std
            ),
            "core_edge_density": float(
                core_edge_density
            ),
            "background_contrast": float(
                outer_contrast
            ),
            "edge_strength": float(
                edge_strength
            ),
            "visible_arc_fraction": float(
                visible_arc
            )
        }

    return best


def gradient_magnitude_at_points(gray, xs, ys):
    gx = cv2.Sobel(
        gray,
        cv2.CV_32F,
        1,
        0,
        ksize=3
    )
    gy = cv2.Sobel(
        gray,
        cv2.CV_32F,
        0,
        1,
        ksize=3
    )

    magnitude = cv2.magnitude(
        gx,
        gy
    )

    return magnitude[
        ys,
        xs
    ]


def detect_circle(frame):
    """
    Only accept a strict screenshot-style target.

    There is intentionally no contour fallback: a missed frame waits,
    whereas a false positive would cause an incorrect touch.
    """
    return detect_circle_hough(frame)


def same_play_again_target(a, b):
    """
    Match two Play Again button detections so one transient yellow shape
    cannot trigger a click by itself.
    """
    ax, ay = a["cx"], a["cy"]
    bx, by = b["cx"], b["cy"]

    center_dist = math.hypot(
        ax - bx,
        ay - by
    )

    aw, ah = a["bbox"][2], a["bbox"][3]
    bw, bh = b["bbox"][2], b["bbox"][3]

    return (
        center_dist <= max(25.0, max(aw, bw) * 0.12)
        and
        abs(aw - bw) <= max(30.0, max(aw, bw) * 0.20)
        and
        abs(ah - bh) <= max(20.0, max(ah, bh) * 0.20)
    )


def same_target(a, b):
    """
    Decide whether two detections are still the same physical circle.
    Tolerance is intentionally generous because the detected center can move
    a few pixels between consecutive screen captures.
    """
    d = math.hypot(
        a["cx"] - b["cx"],
        a["cy"] - b["cy"]
    )

    rr = max(
        float(a["r"]),
        float(b["r"]),
        1.0
    )

    spatial = d <= max(
        18.0,
        rr * 0.45
    )

    size = abs(
        float(a["r"]) - float(b["r"])
    ) <= max(
        12.0,
        rr * 0.35
    )

    cd = math.sqrt(
        sum(
            (
                a["bgr"][i] -
                b["bgr"][i]
            ) ** 2
            for i in range(3)
        )
    )

    color = cd <= 80.0

    return spatial and size and color


def video_rect(frame_w, frame_h, dev_w, dev_h):
    dr = dev_w / dev_h
    fr = frame_w / frame_h
    if fr > dr:
        vh = frame_h
        vw = int(round(vh * dr))
        x0 = (frame_w - vw) // 2
        y0 = 0
    else:
        vw = frame_w
        vh = int(round(vw / dr))
        x0 = 0
        y0 = (frame_h - vh) // 2
    return x0, y0, vw, vh


def to_device(cx, cy, fw, fh, dw, dh):
    x0, y0, vw, vh = video_rect(fw, fh, dw, dh)
    x, y = cx - x0, cy - y0
    if x < 0 or y < 0 or x >= vw or y >= vh:
        return None
    return (
        max(0, min(dw - 1, int(round(x * dw / vw)))),
        max(0, min(dh - 1, int(round(y * dh / vh)))),
    )


def draw_overlay(
    img,
    target,
    age_ms,
    status
):
    """
    Thin visualization only.
    It does NOT affect detection.
    """
    cx = target["cx"]
    cy = target["cy"]
    r = int(
        round(target["r"])
    )

    x, y, w, h = target["bbox"]

    # Very small pulse so the detected boundary is easy to see
    # without covering the actual circle.
    pulse = int(
        1 +
        2 *
        (
            0.5 +
            0.5 *
            math.sin(
                age_ms /
                180.0 *
                2.0 *
                math.pi
            )
        )
    )

    # Thin animated border
    cv2.circle(
        img,
        (cx, cy),
        max(
            2,
            r + pulse
        ),
        (0, 255, 0),
        1,
        cv2.LINE_AA
    )

    # Thin bbox
    cv2.rectangle(
        img,
        (x, y),
        (x + w, y + h),
        (0, 255, 255),
        1,
        cv2.LINE_AA
    )

    # Small, thin crosshair
    line_len = max(
        7,
        int(r * 0.20)
    )

    cv2.line(
        img,
        (
            cx - line_len,
            cy
        ),
        (
            cx + line_len,
            cy
        ),
        (255, 255, 255),
        1,
        cv2.LINE_AA
    )

    cv2.line(
        img,
        (
            cx,
            cy - line_len
        ),
        (
            cx,
            cy + line_len
        ),
        (255, 255, 255),
        1,
        cv2.LINE_AA
    )

    # Small center point
    cv2.circle(
        img,
        (cx, cy),
        2,
        (0, 0, 255),
        -1,
        cv2.LINE_AA
    )

    cv2.putText(
        img,
        f"{status} ({cx},{cy})",
        (
            max(5, x),
            max(22, y - 7)
        ),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.48,
        (0, 255, 0),
        1,
        cv2.LINE_AA
    )

def move_output_window(region):
    try:
        if not region:
            return
        l, t, r, b = region
        sw = user32.GetSystemMetrics(0)
        x = r + 10
        y = t
        if x + 540 > sw:
            x = max(10, l - 540)
        cv2.moveWindow(OUTPUT_TITLE, int(x), int(y))
    except Exception:
        pass


def avg(a):
    return float(np.mean(a)) if a else 0.0


def p95(a):
    return float(np.percentile(a, 95)) if a else 0.0


def click_target_center(target, region, hwnd, tapper):
    """
    Click the CURRENT detected center in screen coordinates.
    """
    click_x = region[0] + int(target["cx"])
    click_y = region[1] + int(target["cy"])

    dispatch_ms, ok = tapper.tap(
        click_x,
        click_y,
        hwnd
    )

    return (
        click_x,
        click_y,
        dispatch_ms,
        ok
    )


def main():
    auto_click = AUTO_CLICK_START
    print("=" * 70)
    print("              CIRCLE TOUCH CHALLENGE")
    print("=" * 70)
    print("scrcpy title:", SCRCPY_TITLE)
    print("SPACE = auto click ON/OFF | R = reset stats | Q = quit")
    print("[MODE] Strict circle detection + result-page Play Again handling.")

    hwnd = find_scrcpy()
    if not hwnd:
        print(f"[ERROR] Window {SCRCPY_TITLE!r} not found.")
        print('Start: scrcpy.exe --window-title=CHESS_MOBILE --always-on-top')
        return

    region = client_region(hwnd)
    if not region:
        print("[ERROR] Could not read scrcpy client region.")
        return

    print("[SCRCPY] Existing window found.")
    print("[SCRCPY] Client region:", region)

    cap = Capture()
    cap.set_region(client_region(hwnd))
    print("[CAPTURE]", cap.backend)

    tapper = Tapper()
    print("[READY] Put the circle game on the phone.")

    if SHOW_OUTPUT:
        cv2.namedWindow(OUTPUT_TITLE, cv2.WINDOW_NORMAL)
        cv2.resizeWindow(OUTPUT_TITLE, 520, 900)
        move_output_window(cap.region)

    pending = None
    candidate_target = None
    candidate_streak = 0

    # Result-page state. While this page is visible, circle detection/clicking
    # is completely disabled and only "Play again" is handled.
    play_again_target = None
    play_again_candidate_streak = 0
    play_again_active = False
    play_again_gone_frames = 0
    play_again_last_click_at = 0.0
    play_again_clicks = 0

    overlay_target = None
    overlay_time = 0.0
    last_region_update = 0.0
    last_cap = last_det = last_dispatch = last_dt_tap = 0.0
    last_confirm_reason = "NONE"
    state = "WAITING"
    frame_count = detected_count = click_count = confirmed = failed = retries = 0
    cap_times, det_times, dispatch_times, dt_tap_times, confirm_times = [], [], [], [], []
    run_start = time.perf_counter()
    frame_idx = 0

    try:
        while True:
            now = time.perf_counter()
            if now - last_region_update > REGION_REFRESH_SEC:
                hwnd_now = find_scrcpy()
                if hwnd_now:
                    hwnd = hwnd_now
                    new_region = client_region(hwnd)
                    if new_region:
                        region = new_region
                        cap.set_region(region)
                last_region_update = now

            t0 = time.perf_counter()
            frame = cap.grab()
            last_cap = (time.perf_counter() - t0) * 1000
            if frame is None:
                state = "NO FRAME"
                continue

            frame_count += 1
            frame_idx += 1
            fh, fw = frame.shape[:2]

            # ==================================================
            # RESULT PAGE HAS PRIORITY OVER CIRCLE DETECTION.
            #
            # When the "You won!" page is visible:
            #   1) confirm the Play again button for 2 frames;
            #   2) click its center;
            #   3) keep clicking while the page remains visible;
            #   4) after 3 consecutive page-miss frames, resume circle
            #      detection from a clean state.
            #
            # This completely prevents the trophy/buttons/text on the
            # result page from being interpreted as circle targets.
            # ==================================================
            page_t0 = time.perf_counter()
            play_again = detect_play_again_page(frame)
            page_det_ms = (time.perf_counter() - page_t0) * 1000.0

            target = None

            if play_again is not None:
                # Result page is visible, so any old circle state is invalid.
                pending = None
                candidate_target = None
                candidate_streak = 0

                if (
                    play_again_target is not None
                    and
                    same_play_again_target(
                        play_again,
                        play_again_target
                    )
                ):
                    play_again_candidate_streak += 1
                else:
                    play_again_target = play_again
                    play_again_candidate_streak = 1

                play_again_gone_frames = 0
                overlay_target = None
                overlay_time = time.perf_counter()

                if play_again_candidate_streak < PLAY_AGAIN_CONFIRM_FRAMES:
                    state = (
                        f"PLAY AGAIN DETECT "
                        f"{play_again_candidate_streak}/"
                        f"{PLAY_AGAIN_CONFIRM_FRAMES}"
                    )
                else:
                    play_again_active = True

                    age_ms = (
                        time.perf_counter()
                        - play_again_last_click_at
                    ) * 1000.0

                    # First click immediately after confirmation, then
                    # repeat only while the button is still really visible.
                    if (
                        play_again_last_click_at <= 0.0
                        or
                        age_ms >= PLAY_AGAIN_RECLICK_INTERVAL_MS
                    ):
                        click_x = region[0] + int(play_again["cx"])
                        click_y = region[1] + int(play_again["cy"])

                        dispatch_ms, ok = tapper.tap(
                            click_x,
                            click_y,
                            hwnd
                        )

                        play_again_last_click_at = time.perf_counter()
                        last_dispatch = dispatch_ms
                        play_again_clicks += 1

                        if ok:
                            click_count += 1
                            dispatch_times.append(dispatch_ms)
                            state = "PLAY AGAIN - CLICK"
                        else:
                            failed += 1
                            state = "PLAY AGAIN - TAP FAILED"
                    else:
                        state = "PLAY AGAIN - WAIT"

                # Do not run any circle detection logic on this page.
                last_det = 0.0

            else:
                # No result page detected.
                if play_again_active:
                    play_again_gone_frames += 1

                    # Do not immediately switch back on a single missed frame.
                    # Only after the page is truly gone do we clear all page
                    # state and start looking for the next circle.
                    if (
                        play_again_gone_frames
                        < PLAY_AGAIN_GONE_CONFIRM_FRAMES
                    ):
                        state = (
                            f"PLAY AGAIN PAGE LEAVING "
                            f"({play_again_gone_frames}/"
                            f"{PLAY_AGAIN_GONE_CONFIRM_FRAMES})"
                        )
                        last_det = 0.0
                    else:
                        play_again_active = False
                        play_again_target = None
                        play_again_candidate_streak = 0
                        play_again_gone_frames = 0
                        play_again_last_click_at = 0.0

                        pending = None
                        candidate_target = None
                        candidate_streak = 0

                        state = "GAME PAGE GONE - WAITING FOR CIRCLE"
                        last_det = 0.0

                else:
                    play_again_target = None
                    play_again_candidate_streak = 0
                    play_again_gone_frames = 0

                    # Only now is circle detection enabled again.
                    t1 = time.perf_counter()
                    target = detect_circle(frame)
                    last_det = (time.perf_counter() - t1) * 1000
                    cap_times.append(last_cap)
                    det_times.append(last_det)

            # ==================================================
            # TARGET -> CLICK CENTER -> KEEP CLICKING UNTIL THE
            # CIRCLE DISAPPEARS FROM THE SCRCPY CAPTURE.
            #
            # This is the requested behavior:
            #   1) Detect circle center.
            #   2) Click its center immediately.
            #   3) Keep re-clicking the CURRENT center while the circle
            #      is still visible.
            #   4) Stop only after the circle is absent for several
            #      consecutive captured frames.
            # ==================================================
            if pending is None:
                # --------------------------------------------------
                # WAIT FOR A REAL TARGET:
                # The circle must be detected consistently in multiple
                # consecutive frames before ANY click is allowed.
                # --------------------------------------------------
                if target is None:
                    candidate_target = None
                    candidate_streak = 0
                    state = "WAITING FOR CIRCLE"

                else:
                    if (
                        candidate_target is not None
                        and
                        same_target(
                            target,
                            candidate_target
                        )
                    ):
                        candidate_streak += 1
                    else:
                        candidate_target = target
                        candidate_streak = 1

                    overlay_target = target
                    overlay_time = time.perf_counter()

                    if candidate_streak < TARGET_CONFIRM_FRAMES:
                        state = (
                            f"CIRCLE CANDIDATE "
                            f"{candidate_streak}/{TARGET_CONFIRM_FRAMES}"
                        )

                    else:
                        (
                            click_x,
                            click_y,
                            dispatch_ms,
                            ok
                        ) = click_target_center(
                            target,
                            region,
                            hwnd,
                            tapper
                        )

                        click_sent = time.perf_counter()
                        last_dispatch = dispatch_ms
                        last_dt_tap = (
                            click_sent -
                            overlay_time
                        ) * 1000

                        if ok:
                            detected_count += 1
                            click_count += 1
                            dispatch_times.append(
                                dispatch_ms
                            )
                            dt_tap_times.append(
                                last_dt_tap
                            )

                            pending = {
                                "target": target,
                                "click_x": click_x,
                                "click_y": click_y,
                                "started_at": overlay_time,
                                "last_click_at": click_sent,
                                "clicks": 1,
                                "gone_frames": 0,
                            }

                            candidate_target = None
                            candidate_streak = 0

                            last_confirm_reason = (
                                "3-FRAME REAL CIRCLE"
                            )
                            state = "CLICK CENTER"

                        else:
                            failed += 1
                            candidate_target = None
                            candidate_streak = 0
                            state = "TAP FAILED"

            else:
                age_ms = (
                    time.perf_counter() -
                    pending["last_click_at"]
                ) * 1000

                # --------------------------------------------------
                # Circle still detected.
                # Re-detect the center and click THAT current center.
                # --------------------------------------------------
                if target is not None:
                    pending["gone_frames"] = 0

                    same = same_target(
                        target,
                        pending["target"]
                    )

                    if same:
                        pending["target"] = target
                        overlay_target = target

                        # Even when the center moves a little, always click
                        # the latest detected center rather than the first one.
                        if age_ms >= RECLICK_INTERVAL_MS:
                            (
                                click_x,
                                click_y,
                                dispatch_ms,
                                ok
                            ) = click_target_center(
                                target,
                                region,
                                hwnd,
                                tapper
                            )

                            pending["last_click_at"] = time.perf_counter()
                            pending["click_x"] = click_x
                            pending["click_y"] = click_y
                            pending["clicks"] += 1

                            last_dispatch = dispatch_ms
                            last_dt_tap = (
                                pending["last_click_at"] -
                                overlay_time
                            ) * 1000

                            if ok:
                                click_count += 1
                                retries += 1
                                dispatch_times.append(dispatch_ms)
                                dt_tap_times.append(last_dt_tap)
                                state = "CLICK CENTER - AGAIN"
                            else:
                                failed += 1
                                state = "TAP FAILED - RETRY"

                    else:
                        # Old circle is gone and a different target is
                        # already visible. Complete the old target and
                        # immediately start clicking the new one.
                        confirm_ms = (
                            time.perf_counter() -
                            pending["started_at"]
                        ) * 1000

                        confirmed += 1
                        confirm_times.append(confirm_ms)
                        last_confirm_reason = "NEW TARGET"

                        pending = None
                        state = "HIT - NEXT TARGET"

                        detected_count += 1
                        overlay_target = target
                        overlay_time = time.perf_counter()

                        (
                            click_x,
                            click_y,
                            dispatch_ms,
                            ok
                        ) = click_target_center(
                            target,
                            region,
                            hwnd,
                            tapper
                        )

                        click_sent = time.perf_counter()
                        last_dispatch = dispatch_ms
                        last_dt_tap = (
                            click_sent - overlay_time
                        ) * 1000

                        if ok:
                            click_count += 1
                            dispatch_times.append(dispatch_ms)
                            dt_tap_times.append(last_dt_tap)

                            pending = {
                                "target": target,
                                "click_x": click_x,
                                "click_y": click_y,
                                "started_at": overlay_time,
                                "last_click_at": click_sent,
                                "clicks": 1,
                                "gone_frames": 0,
                            }

                            state = "CLICK CENTER - NEXT"
                        else:
                            failed += 1
                            state = "TAP FAILED - NEXT"

                # --------------------------------------------------
                # Detector did not see the circle in this frame.
                #
                # IMPORTANT:
                # Never click blindly on the last known center here.
                # A missed frame can happen exactly while the old circle
                # is disappearing and BEFORE the next circle appears.
                # Blind re-clicks were causing occasional taps before
                # the new circle was actually visible.
                #
                # We still require GONE_CONFIRM_FRAMES consecutive misses
                # before completing the old target, but there is ZERO tap
                # dispatch while no real circle is currently visible.
                # --------------------------------------------------
                else:
                    pending["gone_frames"] += 1

                    state = (
                        f"WAITING CIRCLE "
                        f"({pending['gone_frames']}/{GONE_CONFIRM_FRAMES})"
                    )

                    if pending["gone_frames"] >= GONE_CONFIRM_FRAMES:
                        confirm_ms = (
                            time.perf_counter() -
                            pending["started_at"]
                        ) * 1000

                        confirmed += 1
                        confirm_times.append(confirm_ms)
                        last_confirm_reason = (
                            f"GONE {GONE_CONFIRM_FRAMES} FRAMES"
                        )

                        pending = None
                        state = "HIT CONFIRMED"

            # ==================================================
            # OUTPUT / VISUAL TEST OVERLAY
            # ==================================================
            if SHOW_OUTPUT and frame_idx % DISPLAY_EVERY == 0:
                out = frame.copy()
                if overlay_target is not None:
                    age = (time.perf_counter() - overlay_time) * 1000
                    if target is not None or age <= OVERLAY_MS or pending is not None:
                        draw_overlay(out, target if target is not None else overlay_target,
                                     age, state)

                total_fps = frame_count / max(time.perf_counter() - run_start, 1e-6)
                text_lines = [
                    f"STATE: {state}",
                    f"FPS: {total_fps:.1f}",
                    f"Capture: {last_cap:.2f} ms",
                    f"Detect: {last_det:.2f} ms",
                    f"Tap dispatch: {last_dispatch:.2f} ms",
                    f"Detect->Tap: {last_dt_tap:.2f} ms",
                    f"Candidate: {candidate_streak}/{TARGET_CONFIRM_FRAMES}",
                    f"Center clicks: {pending['clicks'] if pending is not None else 0}",
                    f"Confirm: {last_confirm_reason}",
                    f"Targets: {detected_count}  Clicks: {click_count}",
                    f"Completed: {confirmed}  Tap failures: {failed}",
                    f"Extra center clicks: {retries}",
                ]
                yy = 24
                for s in text_lines:
                    cv2.putText(out, s, (10, yy), cv2.FONT_HERSHEY_SIMPLEX,
                                0.52, (255, 255, 255), 2, cv2.LINE_AA)
                    yy += 21

                cv2.imshow(OUTPUT_TITLE, out)
                key = cv2.waitKey(1) & 0xFF
                if key == ord("q"):
                    break
                if key == 32:
                    auto_click = not auto_click
                    print("[CONTROL] AUTO_CLICK =", auto_click)
                elif key == ord("r"):
                    frame_count = detected_count = click_count = confirmed = failed = retries = 0
                    cap_times.clear(); det_times.clear(); dispatch_times.clear(); dt_tap_times.clear(); confirm_times.clear()
                    run_start = time.perf_counter()
                    last_confirm_reason = "NONE"
                    pending = None
                    candidate_target = None
                    candidate_streak = 0
                    play_again_target = None
                    play_again_candidate_streak = 0
                    play_again_active = False
                    play_again_gone_frames = 0
                    play_again_last_click_at = 0.0
                    play_again_clicks = 0
                    print("[CONTROL] Stats reset")

    except KeyboardInterrupt:
        pass
    finally:
        tapper.close()
        try:
            cap.close()
        except Exception:
            pass
        cv2.destroyAllWindows()

    elapsed = max(time.perf_counter() - run_start, 1e-6)
    print("\n" + "=" * 70)
    print("                  FINAL BENCHMARK")
    print("=" * 70)
    print(f"Frames                : {frame_count}")
    print(f"Loop FPS              : {frame_count / elapsed:.2f}")
    print(f"Targets detected      : {detected_count}")
    print(f"Clicks sent           : {click_count}")
    print(f"Targets completed     : {confirmed}")
    print(f"Tap failures          : {failed}")
    print(f"Extra center clicks   : {retries}")
    print(f"Play Again clicks     : {play_again_clicks}")
    print(f"Avg capture           : {avg(cap_times):.2f} ms")
    print(f"Avg OpenCV detection  : {avg(det_times):.2f} ms")
    print(f"Avg tap dispatch      : {avg(dispatch_times):.2f} ms")
    print(f"Avg Detect -> Tap      : {avg(dt_tap_times):.2f} ms")
    print(f"P95 Detect -> Confirm : {p95(confirm_times):.2f} ms")
    print("=" * 70)


if __name__ == "__main__":
    main()
