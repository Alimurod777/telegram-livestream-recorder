"""Lazy package init — avoids importing pyrogram/pytgcalls for light use.

Accessing an attribute triggers the real import; `from livestream.metadata
import ...` style submodule imports never touch the heavy stack.
"""

__all__ = [
    "StreamRecorder",
    "RecordingSession",
    "stream_recorder",
    "VideoSource",
    "SourceType",
    "StateMachine",
    "RecorderState",
    "MuxerManager",
    "valid_media",
    "account_manager",
]

_IMPORTS = {
    "StreamRecorder": ("livestream.recorder", "StreamRecorder"),
    "RecordingSession": ("livestream.recorder", "RecordingSession"),
    "stream_recorder": ("livestream.recorder", "stream_recorder"),
    "VideoSource": ("livestream.metadata", "VideoSource"),
    "SourceType": ("livestream.metadata", "SourceType"),
    "StateMachine": ("livestream.state_machine", "StateMachine"),
    "RecorderState": ("livestream.state_machine", "RecorderState"),
    "MuxerManager": ("livestream.muxer", "MuxerManager"),
    "valid_media": ("livestream.muxer", "valid_media"),
    "account_manager": ("livestream.accounts", "account_manager"),
}


def __getattr__(name: str):
    if name in _IMPORTS:
        import importlib

        module_name, attr = _IMPORTS[name]
        module = importlib.import_module(module_name)
        return getattr(module, attr)
    raise AttributeError(f"module 'livestream' has no attribute {name!r}")
