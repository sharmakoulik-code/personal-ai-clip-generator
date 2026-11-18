"""Lightweight local face framing. This does not identify who is speaking."""
import math
from pathlib import Path


def crop_geometry(width: int, height: int, zoom: float) -> tuple[int, int]:
    w = min(float(width), height * 9 / 16) / zoom
    h = w * 16 / 9
    # Even crop sizes avoid chroma subsampling surprises.
    return max(2, int(w) // 2 * 2), max(2, int(h) // 2 * 2)


def clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def target(faces: list[tuple[int, int, int, int]], width: int, height: int, previous: tuple[float, float] | None = None) -> tuple[float, float, str]:
    """Choose group centre when possible; otherwise the largest face. No speaker inference."""
    if not faces:
        return width / 2, height / 2, 'no_face_center'
    if len(faces) > 1:
        left = min(f[0] for f in faces); right = max(f[0] + f[2] for f in faces)
        if right - left <= min(width, height * 9 / 16) * .85:
            top = min(f[1] for f in faces); bottom = max(f[1] + f[3] for f in faces)
            return (left + right) / 2, (top + bottom) / 2, 'group'
    largest = max(faces, key=lambda f: f[2] * f[3])
    chosen = largest
    if len(faces) > 1 and previous is not None:
        # Prefer a nearby comparable face to avoid alternating every sample.
        nearby = min(faces, key=lambda f: math.hypot(f[0] + f[2] / 2 - previous[0], f[1] + f[3] / 2 - previous[1]))
        distance = math.hypot(nearby[0] + nearby[2] / 2 - previous[0], nearby[1] + nearby[3] / 2 - previous[1])
        if distance < width * .2 and nearby[2] * nearby[3] >= largest[2] * largest[3] * .5:
            chosen = nearby
    x, y, w, h = chosen
    return x + w / 2, y + h / 2, 'largest_face' if len(faces) > 1 else 'face'


def smooth(previous: tuple[float, float], desired: tuple[float, float], dt: float, width: int, height: int) -> tuple[float, float]:
    # A deadband and limited movement avoid abrupt jumps between detected faces.
    result = []
    for old, new, size in zip(previous, desired, (width, height)):
        delta = new - old
        if abs(delta) < size * .025: delta = 0
        step = size * .12 * max(dt, .01)
        result.append(old + clamp(delta * .35, -step, step))
    return result[0], result[1]


def detect_frames(path: Path, start: float, duration: float, interval: float, check_cancel=lambda: None, progress=lambda p: None) -> tuple[list[dict], str]:
    import cv2
    cascade = cv2.CascadeClassifier(str(Path(cv2.data.haarcascades) / 'haarcascade_frontalface_default.xml'))
    if cascade.empty(): raise ValueError('Face detector data is missing from the OpenCV installation.')
    capture = cv2.VideoCapture(str(path))
    if not capture.isOpened(): raise ValueError('Could not open the source video for face detection.')
    width, height = int(capture.get(cv2.CAP_PROP_FRAME_WIDTH)), int(capture.get(cv2.CAP_PROP_FRAME_HEIGHT))
    if width <= 0 or height <= 0:
        capture.release(); raise ValueError('Video dimensions are unavailable for face detection.')
    points = []
    previous = (width / 2, height / 2)
    counts = set()
    total = max(1, math.ceil(duration / interval))
    try:
        for i in range(total):
            check_cancel()
            second = min(duration - .001, i * interval)
            capture.set(cv2.CAP_PROP_POS_MSEC, (start + second) * 1000)
            ok, frame = capture.read()
            if not ok:
                raise ValueError('Could not decode the source video while tracking faces.')
            fh, fw = frame.shape[:2]
            scale = min(1.0, 640 / fw)
            image = cv2.resize(frame, None, fx=scale, fy=scale) if scale < 1 else frame
            gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
            raw = cascade.detectMultiScale(gray, scaleFactor=1.1, minNeighbors=5, minSize=(25, 25))
            faces = [(int(x / scale), int(y / scale), int(w / scale), int(h / scale)) for x, y, w, h in raw]
            x, y, label = target(faces, fw, fh, previous if i else None)
            counts.add(label)
            # Aim slightly below face centre for headroom.
            desired = (x, y + min(fh * .10, (max((f[3] for f in faces), default=0)) * .5))
            previous = desired if i == 0 else smooth(previous, desired, interval, fw, fh)
            points.append({'t': round(second, 3), 'x': round(previous[0] / fw, 5), 'y': round(previous[1] / fh, 5)})
            progress((i + 1) / total)
    finally:
        capture.release()
    status = 'group_framing' if 'group' in counts else ('face_framing' if counts - {'no_face_center'} else 'no_face_center')
    return points, status


def crop_positions(points: list[dict], width: int, height: int, zoom: float, horizontal: float, vertical: float) -> tuple[int, int, list[dict]]:
    cw, ch = crop_geometry(width, height, zoom)
    positions = []
    for p in points or [{'t': 0, 'x': .5, 'y': .5}]:
        x = clamp(p['x'] * width - cw / 2 + horizontal * (width - cw) / 2, 0, width - cw)
        y = clamp(p['y'] * height - ch / 2 + vertical * (height - ch) / 2, 0, height - ch)
        positions.append({'t': p['t'], 'x': round(x, 2), 'y': round(y, 2)})
    return cw, ch, positions


def expression(points: list[dict], axis: str) -> str:
    """Continuous linear interpolation, evaluated by FFmpeg for every frame."""
    terms = [f"{points[0][axis]:.2f}"]
    for a, b in zip(points, points[1:]):
        span = b['t'] - a['t']; delta = b[axis] - a[axis]
        if span <= 0 or abs(delta) < .01: continue
        terms.append(f"({delta:.2f})*min(1,max(0,(t-{a['t']:.3f})/{span:.3f}))")
    return '+'.join(terms)
