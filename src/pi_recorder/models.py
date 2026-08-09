from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Dict, Optional


PENDING = "pending"
UPLOADING = "uploading"
UPLOADED = "uploaded"
FAILED = "failed"


@dataclass(frozen=True)
class RecordingMetadata:
    recording_id: str
    start_time: str
    end_time: str
    duration_seconds: float
    filename: str
    file_path: Path
    file_size: int
    checksum_sha256: str
    upload_status: str
    retry_count: int
    created_at: str
    device_id: str
    audio_format: str
    sample_rate: int
    channels: int
    next_attempt_at: Optional[str] = None
    last_error: Optional[str] = None
    uploaded_at: Optional[str] = None
    local_deleted_at: Optional[str] = None

    def with_queue_state(self, **changes: Any) -> "RecordingMetadata":
        return replace(self, **changes)

    def as_upload_dict(self) -> Dict[str, Any]:
        return {
            "id": self.recording_id,
            "recording_start_time": self.start_time,
            "recording_end_time": self.end_time,
            "duration_seconds": self.duration_seconds,
            "filename": self.filename,
            "file_size": self.file_size,
            "checksum_sha256": self.checksum_sha256,
            "upload_status": self.upload_status,
            "retry_count": self.retry_count,
            "created_at": self.created_at,
            "device_id": self.device_id,
            "audio_format": self.audio_format,
            "sample_rate": self.sample_rate,
            "channels": self.channels,
        }
