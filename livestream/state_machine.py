from enum import Enum, auto
import logging

logger = logging.getLogger(__name__)


class RecorderState(Enum):
    WAITING = auto()
    CAMERA = auto()
    SCREEN_SHARE = auto()
    STOPPING = auto()
    STOPPED = auto()
    ERROR = auto()


# Valid transitions: from_state -> {allowed target states}
_VALID_TRANSITIONS = {
    RecorderState.WAITING: {
        RecorderState.CAMERA,
        RecorderState.SCREEN_SHARE,
        RecorderState.STOPPING,
        RecorderState.ERROR,
    },
    RecorderState.CAMERA: {
        RecorderState.SCREEN_SHARE,
        RecorderState.STOPPING,
        RecorderState.ERROR,
    },
    RecorderState.SCREEN_SHARE: {
        RecorderState.CAMERA,
        RecorderState.STOPPING,
        RecorderState.ERROR,
    },
    RecorderState.STOPPING: {
        RecorderState.STOPPED,
        RecorderState.ERROR,
    },
    RecorderState.STOPPED: set(),
    RecorderState.ERROR: set(),
}


class StateMachine:
    def __init__(self):
        self._state = RecorderState.WAITING

    @property
    def current_state(self) -> RecorderState:
        return self._state

    @property
    def is_recording(self) -> bool:
        """True when actively recording (CAMERA or SCREEN_SHARE)."""
        return self._state in (RecorderState.CAMERA, RecorderState.SCREEN_SHARE)

    def transition(self, new_state: RecorderState) -> bool:
        """Transition to a new state if the transition is valid.

        Returns True if the transition occurred, False if it was a no-op
        (same state) or invalid.
        """
        if self._state == new_state:
            return False

        allowed = _VALID_TRANSITIONS.get(self._state, set())
        if new_state not in allowed:
            logger.warning(
                "Invalid state transition: %s -> %s (allowed: %s)",
                self._state.name,
                new_state.name,
                ", ".join(s.name for s in allowed) if allowed else "none",
            )
            return False

        logger.info("State transition: %s -> %s", self._state.name, new_state.name)
        self._state = new_state
        return True
