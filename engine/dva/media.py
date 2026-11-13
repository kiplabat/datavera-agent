"""Images and videos for vision models: resize images, pick frames from videos, and wrap them as the
OpenAI-style `image_url` parts llama-server understands (when the model was loaded with its mmproj file)."""
import base64
import io
from pathlib import Path

IMAGE_EXT = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif", ".tif", ".tiff"}
VIDEO_EXT = {".mp4", ".mov", ".mkv", ".avi", ".webm", ".m4v", ".wmv", ".mpg", ".mpeg"}


def kind_of(path):
    ext = Path(str(path)).suffix.lower()
    return "image" if ext in IMAGE_EXT else ("video" if ext in VIDEO_EXT else None)


def _encode(img, max_side):
    from PIL import Image
    img = img.convert("RGB")
    img.thumbnail((max_side, max_side), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=85)
    return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()


def image_url(path, max_side=896):
    from PIL import Image
    with Image.open(path) as img:
        img.seek(0)
        return _encode(img, max_side)


def video_frames(path, count=8, max_side=768):
    """Evenly spaced frames: list of (seconds, data_url), plus the duration in seconds."""
    import av
    with av.open(str(path)) as box:
        stream = box.streams.video[0]
        duration = float(box.duration / 1_000_000) if box.duration else (
            float(stream.duration * stream.time_base) if stream.duration else 0.0)
        out = []
        if duration > 0:
            stamps = [duration * (i + 0.5) / count for i in range(count)]
            for t in stamps:
                box.seek(int(t / stream.time_base), stream=stream, any_frame=False, backward=True)
                for frame in box.decode(stream):
                    out.append((round(float(frame.time or t), 1), _encode(frame.to_image(), max_side)))
                    break
        else:  # unknown length: take the first frames, spaced out
            for i, frame in enumerate(box.decode(stream)):
                if i % 30 == 0:
                    out.append((round(float(frame.time or 0), 1), _encode(frame.to_image(), max_side)))
                if len(out) >= count:
                    break
    return out, duration


def parts_for(path, frames=None):
    """Message content parts for one image or video file."""
    kind = kind_of(path)
    name = Path(path).name
    if kind == "image":
        return [{"type": "text", "text": f"Image {name}:"}, {"type": "image_url", "image_url": {"url": image_url(path)}}]
    if kind == "video":
        frames, duration = video_frames(path, count=int(frames or 6), max_side=512)
        parts = [{"type": "text", "text": f"Video {name} ({duration:.0f} s). {len(frames)} frames in time order:"}]
        for t, url in frames:
            parts += [{"type": "text", "text": f"Frame at {t:.1f} s:"}, {"type": "image_url", "image_url": {"url": url}}]
        return parts
    raise ValueError(f"{name} is not an image or video this app can read")
