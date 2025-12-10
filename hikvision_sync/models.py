"""Models for sync operations."""

from enum import Enum


class SyncResultStatus(str, Enum):
    """Status for sync operations."""
    SUCCESS = "success"
    PARTIAL = "partial"  # Person created but photo failed
    SKIPPED = "skipped"  # Missing employee_no or photo
    FATAL = "fatal"  # Fatal error - stop sync


class SyncResult:
    """Result of a sync operation."""
    def __init__(self, status: SyncResultStatus, message: str = "", step: str = ""):
        self.status = status
        self.message = message
        self.step = step  # "person" or "photo"
    
    def to_dict(self):
        return {
            "status": self.status.value,
            "message": self.message,
            "step": self.step,
        }


