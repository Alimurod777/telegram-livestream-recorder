from enum import Enum, auto
from dataclasses import dataclass, field
import time
import os


def media_kind(path: str, info: dict) -> str:
    """Voice/round-video attributes must come from source metadata, not extensions."""
    kind = info.get("media_type")
    if kind in {"voice", "video_note"}:
        return kind
    if info.get("has_video") and os.path.splitext(path)[1].lower() == ".mp4":
        return "video"
    if info.get("has_audio") and not info.get("has_video"):
        return "audio"
    return "document"


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
