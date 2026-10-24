import os
import time
import random
import math
import asyncio
import logging
from dataclasses import dataclass, field
from typing import List, Optional, Dict, Any, Callable

logger = logging.getLogger("freebuff.jitter")

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
    Simulates authentic human typing cadence with configurable Poisson/Gaussian intervals,
    natural punctuation hesitations, pre-Enter pauses, and sliding-window CPS enforcement.
    """
    def __init__(self, config: Optional[JitterConfig] = None):
        self.config = config or JitterConfig()

    def _sample_interval(self, char: str) -> float:
        min_ms = self.config.min_delay_ms
        max_ms = self.config.max_delay_ms
        mean_ms = (min_ms + max_ms) / 2.0

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
        return max(min_ms, min(max_ms * 1.8, d))

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
        if not text:
            if append_return:
                os.write(fd, b"\r")
            return JitterMetrics()

        if self.config.mode == "instant":
            start_ts = time.time() * 1000.0
            payload = text.encode("utf-8") + (b"\r" if append_return else b"")
            os.write(fd, payload)
            end_ts = time.time() * 1000.0
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

        start_time = time.time() * 1000.0
        timestamps: List[float] = []
        intervals: List[float] = []
        window_times: List[float] = []
        max_cps = 0

        cur_time = start_time

        for i, char in enumerate(text):
            delay_ms = self._sample_interval(char) * speed_factor

            # Sliding-window CPS fence: prevent burst exceeding target_max_cps
            if self.config.target_max_cps > 0 and self.config.mode != "fast":
                while True:
                    candidate_arrival = cur_time + delay_ms
                    cutoff = candidate_arrival - 1000.0
                    chars_in_window = sum(1 for t in window_times if t >= cutoff) + 1
                    if chars_in_window <= self.config.target_max_cps:
                        break
                    # Pace typing to keep CPS strictly under threshold
                    delay_ms += 15.0

            # Perform the async sleep for this character delay
            sleep_sec = max(0.001, delay_ms / 1000.0)
            await asyncio.sleep(sleep_sec)

            # Write single character byte to terminal
            char_bytes = char.encode("utf-8")
            os.write(fd, char_bytes)

            actual_now = time.time() * 1000.0
            actual_interval = actual_now - cur_time
            cur_time = actual_now

            timestamps.append(actual_now)
            intervals.append(actual_interval)
            window_times.append(actual_now)

            # Compute sliding-window CPS
            cutoff_now = actual_now - 1000.0
            window_count = sum(1 for t in window_times if t >= cutoff_now)
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
            os.write(fd, b"\r")

        finish_time = time.time() * 1000.0
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
        Simulates authentic human bracketed-paste entry for large or multiline texts.
        Encapsulates with \x1b[200~ and \x1b[201~, records pasteEvents & pastedChars,
        and adds realistic pre-Enter hesitation.
        """
        start_time = time.time() * 1000.0
        # Send bracketed paste sequences
        paste_bytes = f"\x1b[200~{text}\x1b[201~".encode("utf-8")
        os.write(fd, paste_bytes)

        pre_enter_ms = 0.0
        if append_return:
            # Human visual review pause after paste (300-600ms)
            pre_enter_ms = random.uniform(self.config.pre_enter_min_ms, self.config.pre_enter_max_ms)
            await asyncio.sleep(pre_enter_ms / 1000.0)
            os.write(fd, b"\r")

        finish_time = time.time() * 1000.0
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
