from typing import Dict, List, Optional
from .metadata import VideoSource
import logging
import time

logger = logging.getLogger(__name__)


class SourceManager:
    def __init__(self):
        self.sources: Dict[int, VideoSource] = {}

    def update_sources(self, detected_sources: List[VideoSource]):
        """Register or refresh detected sources, updating last_seen timestamps.

        The caller (Discover.extract_sources) is responsible for setting
        last_seen on each source. This method simply stores or refreshes
        sources without overriding timestamps.
        """
        for source in detected_sources:
            existing = self.sources.get(source.source_id)
            if existing is None:
                logger.info(
                    "Discovered new source: ssrc=%s type=%s %sx%s",
                    source.source_id,
                    source.source_type.name,
                    source.width,
                    source.height,
                )
                self.sources[source.source_id] = source
            else:
                # Always refresh last_seen to the newer value
                existing.last_seen = max(existing.last_seen, source.last_seen)
                # Update resolution/rotation if changed
                if (
                    existing.width != source.width
                    or existing.height != source.height
                    or existing.rotation != source.rotation
                ):
                    logger.info(
                        "Source %s resolution changed: %sx%s -> %sx%s",
                        source.source_id,
                        existing.width,
                        existing.height,
                        source.width,
                        source.height,
                    )
                    source.last_seen = existing.last_seen
                    self.sources[source.source_id] = source

    def prune_stale(
        self, max_age: float = 2.0, now: Optional[float] = None
    ) -> List[VideoSource]:
        """Remove sources not seen for longer than max_age seconds.

        Returns the list of pruned (removed) sources.
        """
        if now is None:
            now = time.monotonic()
        stale = [
            src
            for src in self.sources.values()
            if (now - src.last_seen) > max_age
        ]
        for src in stale:
            logger.info(
                "Pruning stale source: ssrc=%s type=%s (age=%.1fs)",
                src.source_id,
                src.source_type.name,
                now - src.last_seen,
            )
            del self.sources[src.source_id]
        return stale

    def remove_source(self, source_id: int):
        if source_id in self.sources:
            logger.info("Source removed: %s", source_id)
            del self.sources[source_id]

    def get_sources(self) -> List[VideoSource]:
        return list(self.sources.values())

    def get_source(self, source_id: int) -> Optional[VideoSource]:
        return self.sources.get(source_id)

    def clear(self):
        self.sources.clear()
