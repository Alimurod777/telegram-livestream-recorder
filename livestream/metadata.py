from enum import Enum, auto
from dataclasses import dataclass, field
import time


class SourceType(Enum):
    CAMERA = auto()
    SCREEN_SHARE = auto()


@dataclass
class VideoSource:
    source_id: int
    source_type: SourceType
    width: int
    height: int
    fps: int = 30
    codec: str = "raw"
    rotation: int = 0
    bitrate: str = ""
    endpoint: str = ""
    last_seen: float = field(default_factory=time.monotonic)

    @property
    def resolution(self) -> tuple:
        """Return (width, height) tuple."""
        return (self.width, self.height)

    @property
    def expected_frame_size(self) -> int:
        """Expected raw I420 (YUV420p) frame size in bytes."""
        return self.width * self.height * 3 // 2
