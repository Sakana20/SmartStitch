"""Portrait to landscape batches using the shared full-frame conversion pipeline."""
import re

from .landscape_to_portrait import LandscapeToPortraitManager


class PortraitToLandscapeManager(LandscapeToPortraitManager):
    table = "landscape_jobs"
    title = "竖改横"
    resolutions = {"720p": (1280, 720), "1080p": (1920, 1080)}
    generated = re.compile(r"_竖改横(?:_[0-9]+)?$", re.IGNORECASE)
    source_orientation = "竖屏"

    def accepts_orientation(self, width: float, height: float) -> bool:
        return height > width
