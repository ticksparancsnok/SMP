# Copyright (c) 2025-2026 Kiss Benedek, operating as "GG4Ever" (https://github.com/ticksparancsnok/SMP)
# License: GNU AFFERO GENERAL PUBLIC LICENSE Version 3
# Attribution required in modified versions: see NOTICE

"""Who is watching which room, and the latest screens (memory only, one server process).

Privacy and bandwidth rules that live here:
  * PCs send frames only while somebody watches their room (WatchRegistry), and
    full-size frames only for a PC someone enlarged (detail view).
  * Frames are kept in memory only, never written to disk, and only the latest
    per PC. They're dropped as soon as nobody watches the room any more.
  * A viewer that stops sending keep-alives (closed tab, sleeping laptop) times out.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field

VIEW_TIMEOUT_S = 20.0  # the page sends a keep-alive at least every 5 s
FRAME_MAX_AGE_S = 120.0  # an older frame is shown as stale, then dropped


@dataclass
class Viewer:
    user_id: uuid.UUID
    user_name: str
    org_id: uuid.UUID
    expires: float
    since: float
    detail_device: uuid.UUID | None = None


@dataclass
class Frame:
    jpeg: bytes
    width: int
    height: int
    screen_width: int
    screen_height: int
    room_id: uuid.UUID
    at: float = field(default_factory=time.time)  # when this picture arrived (the UI's cache key)
    seen: float = field(default_factory=time.time)  # last time the PC said "still the same"


class WatchRegistry:
    def __init__(self, clock=time.monotonic) -> None:
        self._clock = clock
        self._rooms: dict[uuid.UUID, dict[str, Viewer]] = {}

    def touch(self, room_id: uuid.UUID, session: str, user_id: uuid.UUID, user_name: str, org_id: uuid.UUID,
              detail_device: uuid.UUID | None) -> tuple[bool, bool]:
        """Start or keep a viewing session. Returns (new session, detail changed)."""
        now = self._clock()
        viewers = self._rooms.setdefault(room_id, {})
        v = viewers.get(session)
        if v is None or v.user_id != user_id:
            viewers[session] = Viewer(user_id, user_name, org_id, now + VIEW_TIMEOUT_S, now, detail_device)
            return True, detail_device is not None
        changed = v.detail_device != detail_device
        v.expires, v.detail_device = now + VIEW_TIMEOUT_S, detail_device
        return False, changed

    def stop(self, room_id: uuid.UUID, session: str, user_id: uuid.UUID) -> Viewer | None:
        viewers = self._rooms.get(room_id, {})
        v = viewers.get(session)
        if v is None or v.user_id != user_id:
            return None
        del viewers[session]
        if not viewers:
            self._rooms.pop(room_id, None)
        return v

    def prune(self) -> list[tuple[uuid.UUID, Viewer]]:
        """Remove timed-out viewers; returns them (for the audit log)."""
        now, gone = self._clock(), []
        for room_id in list(self._rooms):
            viewers = self._rooms[room_id]
            for session in [s for s, v in viewers.items() if v.expires <= now]:
                gone.append((room_id, viewers.pop(session)))
            if not viewers:
                del self._rooms[room_id]
        return gone

    def is_watched(self, room_id: uuid.UUID | None) -> bool:
        if room_id is None:
            return False
        now = self._clock()
        return any(v.expires > now for v in self._rooms.get(room_id, {}).values())

    def detail_devices(self, room_id: uuid.UUID | None) -> set[uuid.UUID]:
        now = self._clock()
        return {
            v.detail_device for v in self._rooms.get(room_id, {}).values() if v.expires > now and v.detail_device is not None
        }

    def watched_rooms(self) -> set[uuid.UUID]:
        return {r for r in self._rooms if self.is_watched(r)}


class FrameStore:
    """The latest thumbnail and the latest full-size frame of each PC."""

    def __init__(self) -> None:
        self._frames: dict[tuple[uuid.UUID, str], Frame] = {}

    def put(self, device_id: uuid.UUID, kind: str, frame: Frame) -> None:
        self._frames[(device_id, kind)] = frame

    def get(self, device_id: uuid.UUID, kind: str) -> Frame | None:
        f = self._frames.get((device_id, kind))
        if f is not None and time.time() - f.seen > FRAME_MAX_AGE_S:
            del self._frames[(device_id, kind)]
            return None
        return f

    def confirm(self, device_id: uuid.UUID, kind: str, room_id: uuid.UUID | None) -> bool:
        """The PC's screen didn't change: keep the frame we have as current. False when
        we have none (e.g. the server restarted), so the PC must send a picture."""
        f = self.get(device_id, kind)
        if f is None or f.room_id != room_id:
            return False
        f.seen = time.time()
        return True

    def drop_rooms_except(self, watched: set[uuid.UUID]) -> None:
        """Forget every frame of rooms nobody watches any more."""
        for key in [k for k, f in self._frames.items() if f.room_id not in watched]:
            del self._frames[key]

    def drop_device(self, device_id: uuid.UUID) -> None:
        for key in [k for k in self._frames if k[0] == device_id]:
            del self._frames[key]
