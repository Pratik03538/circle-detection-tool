import ctypes
from ctypes import wintypes
import math
import re
import shutil
import os
import subprocess
import time
from typing import Optional, Tuple

import cv2
import numpy as np

# ============================================================
# CIRCLE TOUCH CHALLENGE
# Android phone -> scrcpy window -> OpenCV -> ADB touch
# ============================================================

SCRCPY_TITLE = "CHESS_MOBILE"
OUTPUT_TITLE = "CIRCLE TOUCH - TEST OUTPUT"

# Set this only when adb.exe is not discoverable automatically.
# Example: r"C:\\scrcpy\\adb.exe"
ADB_EXE = ""
SCRCPY_EXE = "scrcpy.exe"
ADB_PATH = None
AUTO_CLICK_START = True
SHOW_OUTPUT = True
DISPLAY_EVERY = 1

# Detection
MIN_VALUE = 65
MIN_SATURATION = 10
MIN_AREA = 180
MIN_RADIUS = 8
MAX_RADIUS_RATIO = 0.48
MIN_CIRCULARITY = 0.38
MIN_CIRCULARITY_EDGE = 0.22
MIN_FILL = 0.52
MIN_ASPECT = 0.55
MAX_ASPECT = 1.80
MORPH_K = 3

# The screenshots show a yellow Finish button at upper-right.
# Only that common button area is ignored; circles elsewhere are allowed.
FINISH_X = 0.58
FINISH_Y = 0.18

# After a click, the old target must disappear/change.
CONFIRM_TIMEOUT_MS = 350
ALLOW_RETRY = False
RETRY_AFTER_MS = 180
MAX_RETRIES = 1

OVERLAY_MS = 170
REGION_REFRESH_SEC = 0.25
PERSISTENT_ADB = False

try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)
except Exception:
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass

user32 = ctypes.windll.user32


def resolve_adb():
    """Find adb.exe without requiring adb to be on PATH."""
    candidates = []

    if ADB_EXE:
        candidates.append(ADB_EXE)

    # Environment variables commonly used by Android SDK.
    for key in ("ADB_PATH", "ANDROID_HOME", "ANDROID_SDK_ROOT"):
        value = os.environ.get(key, "")
        if value:
            pth = value
            if key in ("ANDROID_HOME", "ANDROID_SDK_ROOT"):
                pth = os.path.join(value, "platform-tools", "adb.exe")
            candidates.append(pth)

    local_appdata = os.environ.get("LOCALAPPDATA", "")
    user_profile = os.environ.get("USERPROFILE", "")
    if local_appdata:
        candidates.append(os.path.join(local_appdata, "Android", "Sdk", "platform-tools", "adb.exe"))
    if user_profile:
        candidates.append(os.path.join(user_profile, "AppData", "Local", "Android", "Sdk", "platform-tools", "adb.exe"))

    # If scrcpy is on PATH, look beside scrcpy.exe too. Official Windows
    # scrcpy bundles commonly keep adb.exe in the same directory.
    scrcpy_found = shutil.which(SCRCPY_EXE) or shutil.which("scrcpy")
    if scrcpy_found:
        candidates.append(os.path.join(os.path.dirname(scrcpy_found), "adb.exe"))

    # Finally, normal PATH lookup.
    path_adb = shutil.which("adb")
    if path_adb:
        candidates.append(path_adb)

    for candidate in candidates:
        if candidate and os.path.isfile(candidate):
            return os.path.abspath(candidate)
    return None


ADB_PATH = resolve_adb()


def run_adb(args, timeout=3.0):
    if not ADB_PATH:
        raise FileNotFoundError(
            "adb.exe not found. Put adb.exe on PATH or set ADB_EXE in this script."
        )
    return subprocess.run(
        [ADB_PATH, *args],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=timeout,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


def adb_ready():
    try:
        r = run_adb(["get-state"], 2)
        return r.returncode == 0 and r.stdout.strip() == "device"
    except Exception:
        return False


def device_size():
    try:
        r = run_adb(["shell", "wm", "size"], 3)
        matches = re.findall(r"(\d+)x(\d+)", (r.stdout or "") + " " + (r.stderr or ""))
        return tuple(map(int, matches[-1])) if matches else None
    except Exception:
        return None


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


class Tapper:
    """Send a real Windows click to the already-running scrcpy window.

    This deliberately avoids adb completely, so it cannot restart the adb
    server used by scrcpy and cannot disconnect an existing scrcpy session.
    """

    def tap(self, x, y, hwnd):
        t0 = time.perf_counter()
        try:
            # Make sure the click goes to scrcpy rather than the OpenCV window.
            user32.SetForegroundWindow(hwnd)
            user32.SetCursorPos(int(x), int(y))
            user32.mouse_event(0x0002, 0, 0, 0, 0)  # left down
            user32.mouse_event(0x0004, 0, 0, 0, 0)  # left up
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


def ignored_finish(x, y, w, h, W, H):
    return x >= int(W * FINISH_X) and y <= int(H * FINISH_Y)


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


def detect_circle(frame):
    """
    Detect the random filled circle.

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

        # Ignore the Finish button region from your screenshots.
        if is_finish_button_area(
            x,
            y,
            w,
            h,
            W,
            H
        ):
            continue

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
                0.65 * fit_cx +
                0.35 * dt_cx
            )

            center_y = (
                0.65 * fit_cy +
                0.35 * dt_cy
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

def same_target(a, b):
    d = math.hypot(a["cx"] - b["cx"], a["cy"] - b["cy"])
    rr = max(a["r"], b["r"], 1)
    spatial = d <= max(12, rr * 0.28)
    size = abs(a["r"] - b["r"]) <= max(10, b["r"] * 0.25)
    cd = math.sqrt(sum((a["bgr"][i] - b["bgr"][i]) ** 2 for i in range(3)))
    color = cd <= 45
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


def main():
    auto_click = AUTO_CLICK_START
    print("=" * 70)
    print("              CIRCLE TOUCH CHALLENGE")
    print("=" * 70)
    print("scrcpy title:", SCRCPY_TITLE)
    print("SPACE = auto click ON/OFF | R = reset stats | Q = quit")
    print("[MODE] No ADB calls. Clicks are sent through the existing scrcpy window.")

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
    overlay_target = None
    overlay_time = 0.0
    last_region_update = 0.0
    last_cap = last_det = last_dispatch = last_dt_tap = 0.0
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

            t1 = time.perf_counter()
            target = detect_circle(frame)
            last_det = (time.perf_counter() - t1) * 1000
            cap_times.append(last_cap)
            det_times.append(last_det)

            # ==================================================
            # WAITING: first valid target -> immediate tap
            # ==================================================
            if pending is None:
                if target is not None:
                    detected_count += 1
                    overlay_target = target
                    overlay_time = time.perf_counter()
                    click_x = region[0] + target["cx"]
                    click_y = region[1] + target["cy"]
                    if auto_click:
                        dispatch_ms, ok = tapper.tap(click_x, click_y, hwnd)
                        click_sent = time.perf_counter()
                        last_dispatch = dispatch_ms
                        last_dt_tap = (click_sent - overlay_time) * 1000
                        if ok:
                            click_count += 1
                            dispatch_times.append(dispatch_ms)
                            dt_tap_times.append(last_dt_tap)
                            pending = {
                                "target": target, "click_x": click_x, "click_y": click_y,
                                "detected_at": overlay_time,
                                "click_at": click_sent, "retries": 0,
                            }
                            state = "CLICK SENT"
                        else:
                            state = "TAP FAILED"
                    else:
                        state = "DETECTED / CLICK OFF"

            # ==================================================
            # AFTER CLICK: wait until old circle is gone/changed
            # ==================================================
            else:
                age = (time.perf_counter() - pending["click_at"]) * 1000
                old_same = target is not None and same_target(target, pending["target"])

                if target is None or not old_same:
                    confirm_ms = (time.perf_counter() - pending["detected_at"]) * 1000
                    confirmed += 1
                    confirm_times.append(confirm_ms)
                    pending = None
                    state = "HIT CONFIRMED"

                    # IMPORTANT: If the next circle is already in this same
                    # frame, click it immediately; do not wait one more frame.
                    if target is not None and auto_click:
                        detected_count += 1
                        overlay_target = target
                        overlay_time = time.perf_counter()
                        click_x = region[0] + target["cx"]
                        click_y = region[1] + target["cy"]
                        dispatch_ms, ok = tapper.tap(click_x, click_y, hwnd)
                        click_sent = time.perf_counter()
                        last_dispatch = dispatch_ms
                        last_dt_tap = (click_sent - overlay_time) * 1000
                        if ok:
                                click_count += 1
                                dispatch_times.append(dispatch_ms)
                                dt_tap_times.append(last_dt_tap)
                                pending = {
                                    "target": target, "click_x": click_x, "click_y": click_y,
                                    "detected_at": overlay_time,
                                    "click_at": click_sent, "retries": 0,
                                }
                                state = "CLICK SENT - NEXT TARGET"

                elif age >= CONFIRM_TIMEOUT_MS:
                    failed += 1
                    if ALLOW_RETRY and pending["retries"] < MAX_RETRIES and age >= RETRY_AFTER_MS:
                        dispatch_ms, ok = tapper.tap(
                            pending["click_x"], pending["click_y"], hwnd
                        )
                        pending["retries"] += 1
                        retries += 1
                        last_dispatch = dispatch_ms
                        state = "RETRY"
                        if ok:
                            dispatch_times.append(dispatch_ms)
                    else:
                        pending = None
                        state = "CONFIRM TIMEOUT"

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
                    f"Targets: {detected_count}  Clicks: {click_count}",
                    f"Confirmed: {confirmed}  Failed: {failed}",
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
                    print("[CONTROL] Stats reset")

    except KeyboardInterrupt:
        pass
    finally:
        tapper.close()
        cv2.destroyAllWindows()

    elapsed = max(time.perf_counter() - run_start, 1e-6)
    print("\n" + "=" * 70)
    print("                  FINAL BENCHMARK")
    print("=" * 70)
    print(f"Frames                : {frame_count}")
    print(f"Loop FPS              : {frame_count / elapsed:.2f}")
    print(f"Targets detected      : {detected_count}")
    print(f"Clicks sent           : {click_count}")
    print(f"Hits visually confirm : {confirmed}")
    print(f"Confirmation failures : {failed}")
    print(f"Retries               : {retries}")
    print(f"Avg capture           : {avg(cap_times):.2f} ms")
    print(f"Avg OpenCV detection  : {avg(det_times):.2f} ms")
    print(f"Avg tap dispatch      : {avg(dispatch_times):.2f} ms")
    print(f"Avg Detect -> Tap      : {avg(dt_tap_times):.2f} ms")
    print(f"P95 Detect -> Confirm : {p95(confirm_times):.2f} ms")
    print("=" * 70)


if __name__ == "__main__":
    main()
