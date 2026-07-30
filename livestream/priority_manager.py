"""PriorityManager — select the best video source by type priority.

Priority order:
    1. Screen Share (highest)
    2. Camera

Within the same priority tier, the current active source is preferred
for stability. When there is no current active, the source with the
lowest source_id is selected for deterministic behavior.
"""

from typing import List, Optional
from .metadata import VideoSource, SourceType


class PriorityManager:
    @staticmethod
    def get_active_source(
        sources: List[VideoSource],
        current_active_id: Optional[int] = None,
    ) -> Optional[VideoSource]:
        if not sources:
            return None

        # Priority 1: Screen Share
        screen_shares = sorted(
            [s for s in sources if s.source_type == SourceType.SCREEN_SHARE],
            key=lambda s: s.source_id,
        )
        if screen_shares:
            # Prefer current active if it's still a screen share
            if current_active_id is not None:
                match = next(
                    (s for s in screen_shares if s.source_id == current_active_id),
                    None,
                )
                if match:
                    return match
            return screen_shares[0]

        # Priority 2: Camera
        cameras = sorted(
            [s for s in sources if s.source_type == SourceType.CAMERA],
            key=lambda s: s.source_id,
        )
        if cameras:
            if current_active_id is not None:
                match = next(
                    (s for s in cameras if s.source_id == current_active_id),
                    None,
                )
                if match:
                    return match
            return cameras[0]

        # Fallback: lowest source_id overall
        return min(sources, key=lambda s: s.source_id)
