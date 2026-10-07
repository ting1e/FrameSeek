"""Estimate wall-clock throughput from saved SQLite cursors, including CLI writers."""
from collections import deque
import math
import statistics
import threading
import time


class ProcessingProgress:
    def __init__(self):
        self.samples = deque()
        self.key = None
        self.lock = threading.Lock()

    def update(self, completed, remaining, key, active=True, paused=False, blocked=False, now=None):
        now = time.monotonic() if now is None else now
        with self.lock:
            if key != self.key or not active or paused or (self.samples and completed < self.samples[-1][1]):
                self.samples.clear()
                self.key = key
            if active and not paused:
                if not self.samples or now - self.samples[-1][0] >= 5:
                    self.samples.append((now, completed))
                while len(self.samples) > 1 and now - self.samples[0][0] > 120:
                    self.samples.popleft()
            speed = None
            stable = False
            if len(self.samples) >= 2:
                elapsed = now - self.samples[0][0]
                advanced = completed - self.samples[0][1]
                if elapsed > 0 and advanced > 0:
                    speed = advanced / elapsed
                # Combine polls without a cursor advance: saving happens in chunks,
                # so a zero delta between two polls is not a zero inference speed.
                intervals = []
                previous = self.samples[0]
                for sample in list(self.samples)[1:]:
                    if sample[1] > previous[1] and sample[0] > previous[0]:
                        intervals.append((sample[1]-previous[1])/(sample[0]-previous[0]))
                        previous = sample
                stale = now - previous[0] > 30
                if speed and elapsed >= 30 and len(intervals) >= 2 and not stale:
                    mean = statistics.mean(intervals)
                    stable = mean > 0 and statistics.pstdev(intervals) / mean <= .35
            eta = math.ceil(remaining / speed) if stable and remaining > 0 and not blocked else None
            state = ('paused' if paused else 'idle' if not active or not remaining else
                     'blocked' if blocked else 'stable' if stable else 'warming_up')
            return {'completed_frames':completed, 'remaining_frames':remaining,
                    'frames_per_second':round(speed, 2) if speed else None,
                    'eta_seconds':eta, 'state':state, 'stable':stable}
