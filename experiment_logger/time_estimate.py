"""Thread-safe, measured-throughput estimates; no promised completion deadline."""
from collections import deque
import math
from threading import Lock
from time import monotonic


def duration(seconds):
    seconds = max(0, math.ceil(seconds))
    if seconds < 60:
        return f'{seconds} s'
    minutes, seconds = divmod(seconds, 60)
    if minutes < 60:
        return f'{minutes} min {seconds:02d} s'
    hours, minutes = divmod(minutes, 60)
    return f'{hours} h {minutes:02d} min'


class TimeEstimate:
    def __init__(self, clock=monotonic):
        self.clock = clock
        self.lock = Lock()
        self.started = clock()
        self.label = 'Working'
        self.measurable = None
        self.completed = self.total = 0
        self.samples = deque([(self.started, 0)])
        self.last_progress = self.started

    def phase(self, label, measurable=True):
        with self.lock:
            self.label = label
            self.measurable = measurable
            stamp = self.clock()
            self.samples = deque([(stamp, self.completed)])
            self.last_progress = stamp

    def observe(self, completed, total):
        with self.lock:
            stamp = self.clock()
            completed, total = max(0, completed), max(0, total)
            if completed < self.completed:
                self.samples = deque([(stamp, completed)])
            if completed > self.completed:
                self.last_progress = stamp
            self.completed, self.total = completed, total
            self.samples.append((stamp, completed))
            while len(self.samples) > 2 and stamp - self.samples[1][0] > 8:
                self.samples.popleft()

    def text(self):
        with self.lock:
            stamp = self.clock()
            elapsed = f'Elapsed {duration(stamp - self.started)}'
            prefix = f'{self.label} · '
            if self.measurable is False:
                return prefix + elapsed
            if self.total > 0 and self.completed >= self.total:
                return prefix + 'Finishing… · ' + elapsed
            start, previous = self.samples[0]
            work, interval = self.completed - previous, stamp - start
            if self.total <= 0 or work <= 0 or interval < 0.5:
                return prefix + 'Estimating time… · ' + elapsed
            if stamp - self.last_progress > 5:
                return prefix + 'Waiting for storage; recalculating estimate… · ' + elapsed
            remaining = (self.total - self.completed) * interval / work
            if not math.isfinite(remaining):
                return prefix + 'Estimating time… · ' + elapsed
            return prefix + f'About {duration(remaining)} remaining · ' + elapsed
