"""In-process event bus. The UI listens on one server-sent-events stream; every event has a sequence number so a
reconnecting window can catch up (Last-Event-ID) from a short ring buffer."""
import asyncio
import itertools
import json
import time
from collections import deque


class EventBus:
    def __init__(self, keep=2000):
        self.seq = itertools.count(1)
        self.buffer = deque(maxlen=keep)
        self.subscribers = set()
        self.loop = None

    def publish(self, kind, /, **data):
        event = {"seq": next(self.seq), "time": time.time(), **data, "kind": kind}
        self.buffer.append(event)
        for queue in list(self.subscribers):
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                pass
        return event

    def publish_threadsafe(self, kind, /, **data):
        if self.loop and self.loop.is_running():
            self.loop.call_soon_threadsafe(lambda: self.publish(kind, **data))
        else:
            self.publish(kind, **data)

    async def stream(self, since=0):
        queue = asyncio.Queue(maxsize=5000)
        self.subscribers.add(queue)
        try:
            for event in list(self.buffer):
                if event["seq"] > since:
                    yield event
            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=15)
                except asyncio.TimeoutError:
                    yield None  # keep-alive
                    continue
                if event["seq"] > since:
                    yield event
        finally:
            self.subscribers.discard(queue)


def sse(event):
    if event is None:
        return ": ping\n\n"
    return f"id: {event['seq']}\nevent: message\ndata: {json.dumps(event, ensure_ascii=False, default=str)}\n\n"
