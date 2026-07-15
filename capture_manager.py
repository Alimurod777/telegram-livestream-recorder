from enum import Enum


class CaptureMode(str, Enum):
    AUTO = "auto"
    AUDIO = "audio"
    VIDEO = "video"


class CaptureManager:
    def __init__(self):
        self.sessions = {}

    def status(self):
        return {"active": len(self.sessions)}


capture_manager = CaptureManager()


async def auto_capture(*args, **kwargs):
    return None


def get_capture_status():
    return capture_manager.status()
