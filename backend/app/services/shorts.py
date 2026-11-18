"""YouTube Shorts constants and local layout decisions.

These reflect the requested late-2026 guidance; re-check YouTube's current
rules. Encoding dimensions, target length and loudness are recommendations,
not guarantees of classification or upload acceptance.
"""
MAX_DURATION = 180
DEFAULT_MIN_SECONDS = 20
DEFAULT_MAX_SECONDS = 45
RECOMMENDED_WIDTH = 1080
RECOMMENDED_HEIGHT = 1920
MIN_WIDTH = 720
MIN_HEIGHT = 1280
TARGET_LUFS = -14.0
TARGET_TRUE_PEAK = -1.5
MAX_TITLE = 100
MAX_DESCRIPTION = 5000
MIN_HASHTAGS = 3
MAX_HASHTAGS = 5
CONTAINER = 'mp4'
VIDEO_CODEC = 'h264'
AUDIO_CODEC = 'aac-lc'
PIXEL_FORMAT = 'yuv420p'
COLOUR_SPACE = 'bt709'
OFFICIAL_MIN_DURATION = None
SAFE_ZONE = {'bottom': .20, 'right': .14, 'left': .06, 'top': .08}


def estimate_upscale(src_w, src_h, opts, out_h=RECOMMENDED_HEIGHT):
    if src_w <= 0 or src_h <= 0 or opts.zoom <= 0: raise ValueError('Invalid source dimensions or zoom.')
    crop_h = min(src_h / opts.zoom, src_w / opts.aspect / opts.zoom)
    return out_h / crop_h


def choose_layout(requested, upscale, stack_enabled=False, two_tracks=False):
    if requested == 'auto':
        if stack_enabled and two_tracks: return 'stack', None
        return ('follow' if upscale <= 1.5 else 'fit_blur'), None
    if requested == 'stack' and not two_tracks:
        return 'fit_blur', 'Fewer than two consistently visible faces were detected.'
    if requested in ('follow', 'fit_blur', 'stack', 'letterbox'): return requested, None
    raise ValueError('Unsupported Shorts layout.')

FPS_OPTIONS = (24, 25, 30, 50, 60)


def output_fps(rate):
    """Choose the nearest supported rate without inventing source frames."""
    from fractions import Fraction
    import math
    try: source=float(Fraction(str(rate)))
    except (ValueError, TypeError, ZeroDivisionError, OverflowError): raise ValueError('Source frame rate is unavailable.')
    if not math.isfinite(source) or source <= 0 or source > 1000: raise ValueError('Invalid source frame rate.')
    allowed=[r for r in FPS_OPTIONS if r <= source]
    return max(allowed) if allowed else source


def video_encoder_args():
    return ['-c:v','libx264','-preset','fast','-crf','18','-maxrate','10M','-bufsize','20M']


def output_args(fps):
    return ['-r',str(fps),'-pix_fmt','yuv420p','-colorspace','bt709','-color_primaries','bt709','-color_trc','bt709',
            '-c:a','aac','-profile:a','aac_low','-ar','48000','-ac','2','-b:a','192k','-movflags','+faststart']
