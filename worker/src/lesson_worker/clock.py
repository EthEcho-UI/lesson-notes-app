"""Time source (injectable so tests can fake suspend/resume and long waits)."""
import datetime as dt
import time

WAKE_SLICE_S = 5.0          # sleep granularity: bounds how late a resume from suspend is noticed
RESUME_JUMP_S = 30.0        # wall clock advanced this much more than the monotonic clock => system was suspended


def iso(t: dt.datetime) -> str:
    return t.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(s: str) -> dt.datetime:
    return dt.datetime.fromisoformat(s.replace("Z", "+00:00"))


class Clock:
    def now(self) -> dt.datetime:
        return dt.datetime.now(dt.timezone.utc)

    def now_iso(self) -> str:
        return iso(self.now())

    def mono(self) -> float:
        return time.monotonic()

    def wall(self) -> float:
        return time.time()

    def sleep(self, s: float) -> None:
        time.sleep(s)

    def wait(self, seconds: float, stop=lambda: False) -> bool:
        """Sleep up to `seconds` in short slices. Returns True as soon as a suspend/resume is detected
        (CLOCK_MONOTONIC does not advance while suspended, wall time does), False when the time elapsed
        normally or `stop()` became true."""
        end = self.mono() + seconds
        last_m, last_w = self.mono(), self.wall()
        while not stop():
            left = end - self.mono()
            if left <= 0:
                return False
            self.sleep(min(WAKE_SLICE_S, left))
            m, w = self.mono(), self.wall()
            if (w - last_w) - (m - last_m) > RESUME_JUMP_S:
                return True
            last_m, last_w = m, w
        return False
