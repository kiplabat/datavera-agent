"""Looking at images and videos with the chat's model (only offered when the loaded model can see: it was
loaded with its vision projector, the mmproj file)."""
import asyncio

from . import Tool, ToolError
from .. import media


async def _look(ctx, path, question, count=None):
    server = ctx.app.runtime.active()
    if not server or not server.plan.get("mmproj"):
        raise ToolError("The current model cannot see images. Pick a vision model (marked with an eye).")
    p = ctx.resolve(path)
    if not p.is_file():
        raise ToolError(f"No file at {p}")
    kind = media.kind_of(p)
    if not kind:
        raise ToolError(f"{p.name} is not an image or a video")
    parts = await asyncio.to_thread(media.parts_for, p, count)
    q = question or "Describe what you see in detail, including any text, numbers, charts or people's actions."
    out = await ctx.app.llm.complete([{"role": "user", "content": parts + [{"type": "text", "text": q}]}],
                                     max_tokens=900, temperature=0.2, server=server)
    return out["content"].strip() or "(the model gave no description)"


async def look_at_image(ctx, path, question=""):
    return await _look(ctx, path, question)


async def look_at_video(ctx, path, question=""):
    return await _look(ctx, path, question)


S = lambda props, req: {"type": "object", "properties": props, "required": req}  # noqa: E731
TOOLS = [
    Tool("look_at_image", "Look at an image file (photo, screenshot, chart, scanned page) and answer a question about it.",
         S({"path": {"type": "string"}, "question": {"type": "string"}}, ["path"]), look_at_image, "vision",
         label="Look at image", summary=lambda a: f"Look at {a.get('path')}"),
    Tool("look_at_video", "Watch a video file (evenly spaced frames with timestamps) and answer a question about it.",
         S({"path": {"type": "string"}, "question": {"type": "string"}}, ["path"]), look_at_video, "vision",
         label="Watch video", summary=lambda a: f"Watch {a.get('path')}"),
]
