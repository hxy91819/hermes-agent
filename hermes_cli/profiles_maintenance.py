"""Persistent user-requested Profile stop markers."""
from pathlib import Path


def parked_marker_path(home: Path) -> Path:
    return Path(home) / "gateway.parked"


def profile_is_parked(home: Path) -> bool:
    """Marker contents are deliberately irrelevant, including for provisioning."""
    return parked_marker_path(home).exists()


