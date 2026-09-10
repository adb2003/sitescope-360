"""SQLAlchemy models.

Importing this package registers every mapper, which Alembic autogenerate and
``Base.metadata.create_all`` both rely on.
"""

from app.database.base import Base
from app.models.annotation import Annotation
from app.models.camera import CameraDevice
from app.models.capture import (
    CaptureFile,
    CaptureFrame,
    CaptureLocation,
    CapturePath,
    CaptureSession,
    QualityResult,
)
from app.models.job import ProcessingJob
from app.models.project import (
    Building,
    CaptureSchedule,
    Floor,
    FloorPlan,
    Project,
    Zone,
)
from app.models.report import Report
from app.models.user import RefreshToken, User

__all__ = [
    "Annotation",
    "Base",
    "Building",
    "CameraDevice",
    "CaptureFile",
    "CaptureFrame",
    "CaptureLocation",
    "CapturePath",
    "CaptureSchedule",
    "CaptureSession",
    "Floor",
    "FloorPlan",
    "ProcessingJob",
    "Project",
    "QualityResult",
    "RefreshToken",
    "Report",
    "User",
    "Zone",
]
