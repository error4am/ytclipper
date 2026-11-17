"""Rolling buffer of recorded segments.

The recorder splits the live stream into short segments on disk. This module
keeps a cheap index of them: when each segment starts and ends on a single
monotonic "stream timeline", so clips and detection agree on time.
"""
from __future__ import annotations

import glob
import os
import threading
import time
from dataclasses import dataclass

from .util import LogBus, ffprobe_duration


@dataclass
class Segment:
    path: str
    name: str
    size: int
    mtime: float
    duration: float
    start: float
    index: int = 0

    @property
    def end(self) -> float:
        return self.start + self.duration


class SegmentBuffer:
    def __init__(
        self,
        directory: str,
        bus: LogBus,
        ffprobe: str,
        keep_seconds: float = 420.0,
        seg_time: float = 4.0,
    ) -> None:
        self.directory = directory
        self.bus = bus
        self.ffprobe = ffprobe
        self.keep_seconds = float(keep_seconds)
        self.seg_time = float(seg_time)
        self.segments: list[Segment] = []
        self.protected: set[str] = set()
        self._durations: dict[tuple[str, int], float] = {}
        self._lock = threading.RLock()
        self._newest_mtime: float = 0.0
        os.makedirs(directory, exist_ok=True)

    # -- lifecycle ---------------------------------------------------------- #
    def reset(self) -> None:
        """Wipe leftovers from a previous run so the timeline starts clean."""
        with self._lock:
            for path in glob.glob(os.path.join(self.directory, "seg_*.mkv")):
                if path in self.protected:
                    continue
                try:
                    os.remove(path)
                except OSError:
                    pass
            self.segments = []
            self._durations.clear()
            self._newest_mtime = 0.0

    # -- index -------------------------------------------------------------- #
    def _files(self) -> list[str]:
        paths = glob.glob(os.path.join(self.directory, "seg_*.mkv"))
        return sorted(paths, key=lambda p: (os.path.getmtime(p), p))

    def refresh(self) -> list[Segment]:
        """Rescan the directory. The newest segment is still being written, so
        only fully closed segments are indexed."""
        with self._lock:
            files = self._files()
            if not files:
                self.segments = []
                self._newest_mtime = 0.0
                return []

            stats: list[tuple[str, int, float]] = []
            for path in files:
                try:
                    st = os.stat(path)
                except FileNotFoundError:
                    continue
                stats.append((path, st.st_size, st.st_mtime))
            if not stats:
                self.segments = []
                return []

            self._newest_mtime = stats[-1][2]
            usable = stats[:-1]

            durations: list[float] = []
            for path, size, _mtime in usable:
                key = (os.path.basename(path), size)
                dur = self._durations.get(key)
                if dur is None:
                    dur = ffprobe_duration(self.ffprobe, path)
                    if dur > 0.02:
                        self._durations[key] = dur
                durations.append(dur)

            # Repair missing durations using neighbour timestamps.
            for i, dur in enumerate(durations):
                if dur > 0.02:
                    continue
                if i > 0:
                    dur = usable[i][2] - usable[i - 1][2]
                if dur <= 0.02:
                    dur = self.seg_time
                durations[i] = min(max(dur, 0.05), self.seg_time * 4)

            segments: list[Segment] = []
            previous_start = None
            for i, ((path, size, mtime), dur) in enumerate(zip(usable, durations)):
                start = mtime - dur
                if previous_start is not None and start < previous_start + 0.01:
                    start = previous_start + 0.01
                previous_start = start
                segments.append(
                    Segment(
                        path=path,
                        name=os.path.basename(path),
                        size=size,
                        mtime=mtime,
                        duration=dur,
                        start=start,
                        index=i,
                    )
                )

            self.segments = segments
            return list(segments)

    # -- timeline ----------------------------------------------------------- #
    def now(self) -> float:
        """Current position on the stream timeline."""
        with self._lock:
            if not self.segments:
                return 0.0
            last = self.segments[-1]
            if self._newest_mtime:
                candidate = min(time.time(), self._newest_mtime + 1.0)
            else:
                candidate = last.end
            return max(candidate, last.end)

    def span(self) -> float:
        with self._lock:
            if not self.segments:
                return 0.0
            return max(0.0, self.segments[-1].end - self.segments[0].start)

    def covered(self, start: float, end: float) -> tuple[bool, float, float]:
        """How much of [start, end] the buffer actually holds."""
        with self._lock:
            segments = list(self.segments)
        if not segments:
            return False, 0.0, 0.0
        got_start = max(start, segments[0].start)
        got_end = min(end, segments[-1].end)
        complete = segments[0].start <= start + 0.5 and segments[-1].end >= end - 0.5
        return complete, got_start, got_end

    def slice(self, start: float, end: float) -> list[tuple[Segment, float, float]]:
        """Segments overlapping [start, end] with (segment, inpoint, duration)."""
        with self._lock:
            segments = list(self.segments)
        picked: list[tuple[Segment, float, float]] = []
        for seg in segments:
            if seg.end <= start + 0.01 or seg.start >= end - 0.01:
                continue
            a = max(start, seg.start)
            b = min(end, seg.end)
            if b - a < 0.04:
                continue
            picked.append((seg, a - seg.start, b - a))
        return picked

    def take(self, start: float, end: float) -> list[tuple[Segment, float, float]]:
        """Slice and mark the chosen segments as protected from pruning, atomically."""
        with self._lock:
            picked = self.slice(start, end)
            self.protected.update(seg.path for seg, _inpoint, _dur in picked)
            return picked

    # -- housekeeping ------------------------------------------------------- #
    def prune(self) -> None:
        horizon = 0.0
        with self._lock:
            if not self.segments:
                return
            horizon = self.now() - self.keep_seconds
            keep: list[Segment] = []
            for seg in self.segments:
                if seg.end < horizon and seg.path not in self.protected:
                    try:
                        os.remove(seg.path)
                    except OSError:
                        pass
                else:
                    keep.append(seg)
            self.segments = keep
            live = {s.name for s in self.segments}
            for key in [k for k in self._durations if k[0] not in live]:
                if not os.path.isfile(os.path.join(self.directory, key[0])):
                    del self._durations[key]

    def protect(self, paths) -> None:
        with self._lock:
            self.protected.update(paths)

    def unprotect(self, paths) -> None:
        with self._lock:
            for path in paths:
                self.protected.discard(path)
