import os
import time
import random
import math
import asyncio
import logging
from collections import deque
from dataclasses import dataclass, field
from typing import List, Optional, Dict, Any, Callable

logger = logging.getLogger("freebuff.jitter")

MAX_DELAY_MS = 5000.0
MAX_TARGET_CPS = 1000.0


async def _wait_writable(fd: int):
    loop = asyncio.get_running_loop()
    ready = loop.create_future()
    def wake():
        if not ready.done():
            ready.set_result(None)
    loop.add_writer(fd, wake)
    try:
        await ready
    finally:
        loop.remove_writer(fd)


async def write_all(fd: int, payload: bytes):
    """Write every byte without blocking the event loop on a full pipe or PTY."""
    was_blocking = os.get_blocking(fd)
    if was_blocking:
        os.set_blocking(fd, False)
    view = memoryview(payload)
    offset = 0
    try:
        while offset < len(view):
            try:
                written = os.write(fd, view[offset:offset + 65536])
            except BlockingIOError:
                await _wait_writable(fd)
                continue
            except InterruptedError:
                await asyncio.sleep(0)
                continue
            if written <= 0:
                raise OSError("Terminal write made no progress")
            offset += written
            if offset < len(view):
                await asyncio.sleep(0)
    finally:
        if was_blocking:
            try:
                os.set_blocking(fd, True)
            except OSError:
                pass  # The owner may have closed the descriptor during teardown.


@dataclass
class JitterConfig:
    mode: str = "human"  # "human", "fast", "instant"
    min_delay_ms: float = 45.0
    max_delay_ms: float = 95.0
    distribution: str = "poisson"  # "poisson", "gaussian", "uniform"
    pre_enter_min_ms: float = 250.0
    pre_enter_max_ms: float = 500.0
    target_max_cps: float = 11.5
    punctuation_pause: bool = True

    def __post_init__(self):
        self.validate()

    def validate(self):
        if self.mode not in {"human", "fast", "instant"}:
            raise ValueError("Unsupported typing mode")
        if self.distribution not in {"poisson", "gaussian", "uniform"}:
            raise ValueError("Unsupported interval distribution")
        for name in ("min_delay_ms", "max_delay_ms", "pre_enter_min_ms", "pre_enter_max_ms"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not 0 <= value <= MAX_DELAY_MS:
                raise ValueError(f"{name} must be finite and between 0 and {MAX_DELAY_MS:g}")
        if self.min_delay_ms > self.max_delay_ms:
            raise ValueError("min_delay_ms must not exceed max_delay_ms")
        if self.pre_enter_min_ms > self.pre_enter_max_ms:
            raise ValueError("pre_enter_min_ms must not exceed pre_enter_max_ms")
        cps = self.target_max_cps
        if isinstance(cps, bool) or not isinstance(cps, (int, float)) or not math.isfinite(cps) or not 1 <= cps <= MAX_TARGET_CPS:
            raise ValueError(f"target_max_cps must be finite and between 1 and {MAX_TARGET_CPS:g}")


@dataclass
class JitterMetrics:
    typed_chars: int = 0
    keypress_events: int = 0
    multi_char_events: int = 0
    pasted_chars: int = 0
    paste_events: int = 0
    compose_ms: float = 0.0
    max_cps: int = 0
    avg_interval_ms: float = 0.0
    min_interval_ms: float = 0.0
    max_interval_ms: float = 0.0
    pre_enter_ms: float = 0.0
    timestamps: List[float] = field(default_factory=list)
    intervals: List[float] = field(default_factory=list)
    simulated_freebuff_profile: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "typed_chars": self.typed_chars,
            "keypress_events": self.keypress_events,
            "multi_char_events": self.multi_char_events,
            "pasted_chars": self.pasted_chars,
            "paste_events": self.paste_events,
            "compose_ms": round(self.compose_ms, 1),
            "max_cps": self.max_cps,
            "avg_interval_ms": round(self.avg_interval_ms, 1),
            "min_interval_ms": round(self.min_interval_ms, 1),
            "max_interval_ms": round(self.max_interval_ms, 1),
            "pre_enter_ms": round(self.pre_enter_ms, 1),
            "simulated_freebuff_profile": self.simulated_freebuff_profile,
            "sample_intervals": [round(x, 1) for x in self.intervals[:10]]
        }

class KeystrokeJitterSimulator:
    """
    Produces a configurable local typing simulation with measured intervals and CPS.
    These local measurements do not establish an upstream service input profile.
    """
    def __init__(self, config: Optional[JitterConfig] = None):
        self.config = config or JitterConfig()

    def _sample_interval(self, char: str) -> float:
        min_ms = self.config.min_delay_ms
        max_ms = self.config.max_delay_ms
        mean_ms = (min_ms + max_ms) / 2.0

        if mean_ms <= 0:
            return 0.0
        if self.config.distribution == "poisson":
            # Exponential inter-arrival distribution for Poisson point process
            rate = 1.0 / mean_ms
            d = random.expovariate(rate)
        elif self.config.distribution == "gaussian":
            std = (max_ms - min_ms) / 4.0
            d = random.gauss(mean_ms, std)
        else:
            d = random.uniform(min_ms, max_ms)

        # Natural hesitation on whitespace or punctuation
        if self.config.punctuation_pause and char in " ,.:;?!()[]{}/\\'\":\n\t":
            d *= random.uniform(1.25, 1.65)

        # Clamp to realistic bounds
        return max(min_ms, min(max_ms, d))

    async def type_into_fd(
        self,
        fd: int,
        text: str,
        append_return: bool = True,
        on_char: Optional[Callable[[str, float], None]] = None
    ) -> JitterMetrics:
        """
        Asynchronously writes text character-by-character into fd with jitter delays.
        Guarantees sliding window CPS <= target_max_cps and measures exact composeMs.
        """
        self.config.validate()
        if not text:
            if append_return:
                await write_all(fd, b"\r")
            return JitterMetrics()

        if self.config.mode == "instant":
            start_ts = time.monotonic() * 1000.0
            payload = text.encode("utf-8") + (b"\r" if append_return else b"")
            await write_all(fd, payload)
            end_ts = time.monotonic() * 1000.0
            duration = end_ts - start_ts
            return JitterMetrics(
                typed_chars=len(text),
                keypress_events=1,
                multi_char_events=1,
                compose_ms=duration,
                max_cps=len(text),
                simulated_freebuff_profile=f"v1;tc={len(text)};ke=1;mc=1;pc=0;pe=0;ms={int(duration)};cps={len(text)}"
            )

        # In fast mode, scale intervals down
        speed_factor = 0.25 if self.config.mode == "fast" else 1.0

        start_time = time.monotonic() * 1000.0
        timestamps: List[float] = []
        intervals: List[float] = []
        window_times = deque()
        max_cps = 0

        cur_time = start_time

        for i, char in enumerate(text):
            delay_ms = self._sample_interval(char) * speed_factor

            # Calculate the next allowed arrival directly. A fractional CPS
            # budget still permits only its floor in each one-second window.
            if self.config.mode == "human":
                budget = int(self.config.target_max_cps)
                candidate_arrival = cur_time + delay_ms
                if len(window_times) >= budget:
                    candidate_arrival = max(candidate_arrival, window_times[-budget] + 1000.001)
                delay_ms = max(0, candidate_arrival - time.monotonic() * 1000.0)

            # Perform the async sleep for this character delay
            sleep_sec = max(0.001, delay_ms / 1000.0)
            await asyncio.sleep(sleep_sec)

            # Encode each code point and completely write its UTF-8 bytes
            char_bytes = char.encode("utf-8")
            await write_all(fd, char_bytes)

            actual_now = time.monotonic() * 1000.0
            actual_interval = actual_now - cur_time
            cur_time = actual_now

            timestamps.append(actual_now)
            intervals.append(actual_interval)
            window_times.append(actual_now)

            # Compute sliding-window CPS
            cutoff_now = actual_now - 1000.0
            while window_times and window_times[0] <= cutoff_now:
                window_times.popleft()
            window_count = len(window_times)
            if window_count > max_cps:
                max_cps = window_count

            if on_char:
                try:
                    on_char(char, actual_interval)
                except Exception:
                    pass

        pre_enter_ms = 0.0
        if append_return:
            if self.config.mode == "fast":
                pre_enter_ms = random.uniform(40.0, 80.0)
            else:
                pre_enter_ms = random.uniform(self.config.pre_enter_min_ms, self.config.pre_enter_max_ms)

            await asyncio.sleep(pre_enter_ms / 1000.0)
            await write_all(fd, b"\r")

        finish_time = time.monotonic() * 1000.0
        total_compose_ms = finish_time - start_time

        avg_interval = sum(intervals) / len(intervals) if intervals else 0.0
        min_interval = min(intervals) if intervals else 0.0
        max_interval = max(intervals) if intervals else 0.0

        # Wire string matching Freebuff's encodeInputProfile
        simulated_profile = (
            f"v1;tc={len(text)};ke={len(text)};mc=0;pc=0;pe=0;"
            f"ms={int(total_compose_ms)};cps={max_cps}"
        )

        metrics = JitterMetrics(
            typed_chars=len(text),
            keypress_events=len(text),
            multi_char_events=0,
            compose_ms=total_compose_ms,
            max_cps=max_cps,
            avg_interval_ms=avg_interval,
            min_interval_ms=min_interval,
            max_interval_ms=max_interval,
            pre_enter_ms=pre_enter_ms,
            timestamps=timestamps,
            intervals=intervals,
            simulated_freebuff_profile=simulated_profile
        )

        logger.debug(
            f"Jitter finished: {len(text)} chars in {total_compose_ms:.1f}ms "
            f"(avg IKI: {avg_interval:.1f}ms, max CPS: {max_cps}, pre-enter: {pre_enter_ms:.1f}ms)"
        )
        return metrics

    async def paste_into_fd(
        self,
        fd: int,
        text: str,
        append_return: bool = True
    ) -> JitterMetrics:
        """
        Sends bracketed-paste input for large or multiline texts.
        Encapsulates with \x1b[200~ and \x1b[201~, records pasteEvents & pastedChars,
        and adds realistic pre-Enter hesitation.
        """
        self.config.validate()
        start_time = time.monotonic() * 1000.0
        # Send bracketed paste sequences
        paste_bytes = f"\x1b[200~{text}\x1b[201~".encode("utf-8")
        await write_all(fd, paste_bytes)

        pre_enter_ms = 0.0
        if append_return:
            # Humans review pasted text before sending. The pause therefore
            # scales with the amount of content: a fixed 250–500ms band for
            # every paste regardless of length is its own statistical tell.
            # An explicitly zero pause range disables the review pause.
            if self.config.pre_enter_min_ms or self.config.pre_enter_max_ms:
                review_ms = random.uniform(self.config.pre_enter_min_ms, self.config.pre_enter_max_ms)
                review_ms += min(len(text) / 2000.0, 1.5) * random.uniform(400.0, 1500.0)
                pre_enter_ms = min(review_ms, 5000.0)
            await asyncio.sleep(pre_enter_ms / 1000.0)
            await write_all(fd, b"\r")

        finish_time = time.monotonic() * 1000.0
        total_compose_ms = finish_time - start_time

        simulated_profile = (
            f"v1;tc=0;ke=0;mc=0;pc={len(text)};pe=1;"
            f"ms={int(total_compose_ms)};cps=0"
        )

        metrics = JitterMetrics(
            typed_chars=0,
            keypress_events=0,
            multi_char_events=0,
            pasted_chars=len(text),
            paste_events=1,
            compose_ms=total_compose_ms,
            max_cps=0,
            avg_interval_ms=0.0,
            min_interval_ms=0.0,
            max_interval_ms=0.0,
            pre_enter_ms=pre_enter_ms,
            timestamps=[start_time, finish_time],
            intervals=[],
            simulated_freebuff_profile=simulated_profile
        )
        logger.debug(
            f"Bracketed paste finished: {len(text)} chars in {total_compose_ms:.1f}ms "
            f"(pre-enter: {pre_enter_ms:.1f}ms)"
        )
        return metrics

# Global default simulator
default_jitter_simulator = KeystrokeJitterSimulator()
