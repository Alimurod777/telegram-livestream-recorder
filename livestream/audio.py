import numpy as np
import queue
import threading
import time
import logging
import subprocess
from typing import List, Optional

from config import AUDIO_CHANNELS, AUDIO_SAMPLE_RATE

logger = logging.getLogger(__name__)

TICK_MS = 10
TICK_SAMPLES = AUDIO_SAMPLE_RATE * TICK_MS // 1000
TICK_BYTES_MONO = TICK_SAMPLES * 2  
JITTER_BUFFER_MS = 250
JITTER_BUFFER_TICKS = JITTER_BUFFER_MS // TICK_MS

class AudioMixer:
    def __init__(self):
        self.audio_queue = queue.Queue(maxsize=2000)
        self.audio_dropped = 0
        self.audio_frames = 0
        self._stop_event = threading.Event()
        self._capture_started = threading.Event()
        self._thread: Optional[threading.Thread] = None
        
    def downmix_to_mono(self, data: bytes) -> bytes:
        if AUDIO_CHANNELS == 1:
            return data
        arr = np.frombuffer(data[:len(data) - len(data) % 4], dtype="<i2").reshape(-1, 2)
        return arr.mean(axis=1, dtype=np.int32).astype("<i2").tobytes()
        
    def handle_audio_frames(self, payloads: List[bytes]):
        if not payloads:
            return
            
        try:
            mono_payloads = [self.downmix_to_mono(data) for data in payloads]
            if len(mono_payloads) == 1:
                mixed = mono_payloads[0]
            else:
                arrays = [
                    np.frombuffer(data[: len(data) - len(data) % 2], dtype="<i2")
                    for data in mono_payloads
                ]
                sample_count = max(len(samples) for samples in arrays)
                accumulator = np.zeros(sample_count, dtype=np.int32)
                for samples in arrays:
                    accumulator[: len(samples)] += samples.astype(np.int32)
                
                accumulator = accumulator // len(arrays)
                mixed = accumulator.astype("<i2").tobytes()

            if len(mixed) < TICK_BYTES_MONO:
                mixed = mixed.ljust(TICK_BYTES_MONO, b"\0")
            elif len(mixed) > TICK_BYTES_MONO:
                mixed = mixed[:TICK_BYTES_MONO]
                
            self.audio_queue.put_nowait(mixed)
            self.audio_frames += 1
        except queue.Full:
            self.audio_dropped += 1
            try:
                self.audio_queue.get_nowait()
                self.audio_queue.put_nowait(mixed)
            except (queue.Empty, queue.Full):
                pass
        except Exception:
            logger.exception("Audio mixing failed")

    def start_writer(self, process: subprocess.Popen, capture_event: threading.Event, stop_event: threading.Event):
        self._capture_started = capture_event
        self._stop_event = stop_event
        self._thread = threading.Thread(
            target=self._writer_loop,
            args=(process,),
            daemon=True
        )
        self._thread.start()
        return self._thread

    def join_writer(self, timeout: float = 5.0):
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout)

    def _writer_loop(self, process: subprocess.Popen):
        if not process or not process.stdin:
            return
            
        silence = b"\0" * TICK_BYTES_MONO
        
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
        fade_factor = 1.0
        last_chunk_arr = np.zeros(TICK_SAMPLES, dtype=np.int32)
        
        try:
            while not self._stop_event.is_set() or not self.audio_queue.empty():
                try:
                    chunk = self.audio_queue.get(timeout=0.01)
                    fade_factor = 1.0
                    last_chunk_arr = np.frombuffer(chunk, dtype="<i2").astype(np.int32)
                except queue.Empty:
                    if fade_factor > 0:
                        fade_factor = max(0.0, fade_factor - 0.2)
                        arr = (last_chunk_arr * fade_factor).astype("<i2")
                        chunk = arr.tobytes()
                    else:
                        chunk = silence
                
                process.stdin.write(chunk)
                deadline += 0.01
                delay = deadline - time.monotonic()
                if delay > 0:
                    self._stop_event.wait(delay)
                else:
                    deadline = time.monotonic()
        except (BrokenPipeError, OSError):
            pass
        finally:
            try:
                process.stdin.close()
            except OSError:
                pass
