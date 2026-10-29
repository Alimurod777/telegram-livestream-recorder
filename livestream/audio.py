import logging
import queue
import subprocess
import threading
import time
from typing import Callable, List, Optional

import numpy as np

import config
from config import AUDIO_CHANNELS, AUDIO_SAMPLE_RATE
from ffmpeg_helper import write_all

logger = logging.getLogger(__name__)

TICK_MS = 10
TICK_SAMPLES = AUDIO_SAMPLE_RATE * TICK_MS // 1000
TICK_BYTES = TICK_SAMPLES * AUDIO_CHANNELS * 2  # interleaved s16
JITTER_BUFFER_MS = 250
JITTER_BUFFER_TICKS = JITTER_BUFFER_MS // TICK_MS
NORMALIZE = bool(getattr(config, "AUDIO_NORMALIZE", False))


class AudioMixer:
    """Mixes incoming PCM payloads into paced 10 ms ticks for FFmpeg.

    - Preserves channel count (no forced downmix; set AUDIO_CHANNELS=1 for mono)
    - Optional loudness normalization (AUDIO_NORMALIZE=1), off by default
    - Seamless segment rotation: request_rotate() swaps the encoder process
      at a tick boundary (no ticks lost) via the swap_factory callback
    - Self-healing: on pipe break, obtains a fresh process and keeps writing
    """

    def __init__(self):
        self.audio_queue: "queue.Queue[bytes]" = queue.Queue(maxsize=2000)
        self.audio_dropped = 0
        self.audio_frames = 0
        self.pipe_recoveries = 0
        self._stop_event = threading.Event()
        self._capture_started = threading.Event()
        self._rotate_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._swap_factory: Optional[Callable[[], subprocess.Popen]] = None
        self._pending = bytearray()

    def set_swap_factory(self, factory: Callable[[], subprocess.Popen]) -> None:
        self._swap_factory = factory

    def request_rotate(self) -> None:
        """Swap to a new encoder output at the next tick boundary."""
        self._rotate_event.set()

    def handle_audio_frames(self, payloads: List[bytes]) -> None:
        if not payloads:
            return
        try:
            arrays = [
                np.frombuffer(data[: len(data) - len(data) % 2], dtype="<i2")
                for data in payloads
            ]
            sample_count = max(len(samples) for samples in arrays)
            if len(arrays) == 1:
                mixed = arrays[0]
            else:
                accumulator = np.zeros(sample_count, dtype=np.int32)
                for samples in arrays:
                    accumulator[: len(samples)] += samples.astype(np.int32)
                mixed = (accumulator // len(arrays)).astype("<i2")

            self._pending.extend(mixed.tobytes())
            while len(self._pending) >= TICK_BYTES:
                data = bytes(self._pending[:TICK_BYTES])
                del self._pending[:TICK_BYTES]
                try:
                    self.audio_queue.put_nowait(data)
                    self.audio_frames += 1
                except queue.Full:
                    self.audio_dropped += 1
                    logger.warning("Audio queue full: dropped=%s", self.audio_dropped)
        except Exception:
            logger.exception("Audio mixing failed")

    def start_writer(
        self,
        process: subprocess.Popen,
        capture_event: threading.Event,
        stop_event: threading.Event,
    ) -> threading.Thread:
        self._capture_started = capture_event
        self._stop_event = stop_event
        self._thread = threading.Thread(
            target=self._writer_loop, args=(process,), daemon=True
        )
        self._thread.start()
        return self._thread

    def join_writer(self, timeout: float = 5.0) -> None:
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout)
            if self._thread.is_alive():
                raise TimeoutError("Audio writer did not drain")

    def _swap_process(self) -> Optional[subprocess.Popen]:
        if not self._swap_factory:
            return None
        try:
            return self._swap_factory()
        except Exception:
            logger.exception("Audio swap factory failed")
            return None

    def _writer_loop(self, process: subprocess.Popen) -> None:
        if not process or not process.stdin:
            return

        silence = b"\0" * TICK_BYTES

        while not self._stop_event.is_set():
            if self._capture_started.wait(0.2):
                break

        if self._stop_event.is_set() and not self._capture_started.is_set():
            try:
                process.stdin.close()
            except OSError:
                pass
            return

        buffered_ticks = 0
        while not self._stop_event.is_set() and buffered_ticks < JITTER_BUFFER_TICKS:
            if not self.audio_queue.empty():
                buffered_ticks = self.audio_queue.qsize()
                if buffered_ticks >= JITTER_BUFFER_TICKS:
                    break
            time.sleep(0.01)

        deadline = time.monotonic()

        def _close(proc: subprocess.Popen) -> None:
            try:
                proc.stdin.close()
            except OSError:
                pass

        try:
            while not self._stop_event.is_set() or not self.audio_queue.empty() or self._pending:
                if self._rotate_event.is_set():
                    self._rotate_event.clear()
                    new_process = self._swap_process()
                    if new_process and new_process.stdin:
                        _close(process)
                        process = new_process

                try:
                    chunk = self.audio_queue.get(timeout=0.01)
                except queue.Empty:
                    if self._stop_event.is_set() and self._pending:
                        chunk = bytes(self._pending).ljust(TICK_BYTES, b"\0")
                        self._pending.clear()
                    else:
                        chunk = silence

                try:
                    write_all(process.stdin, chunk)
                except (OSError, ValueError):
                    self.pipe_recoveries += 1
                    logger.warning(
                        "Audio encoder pipe broken (recovery #%s), swapping process",
                        self.pipe_recoveries,
                    )
                    new_process = self._swap_process()
                    if not new_process or not new_process.stdin:
                        break
                    _close(process)
                    process = new_process
                    try:
                        write_all(process.stdin, chunk)
                    except (OSError, ValueError):
                        break

                deadline += 0.01
                delay = deadline - time.monotonic()
                if delay > 0:
                    self._stop_event.wait(delay)
                else:
                    deadline = time.monotonic()
        finally:
            _close(process)
