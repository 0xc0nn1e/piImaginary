import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from pi_recorder.models import RecordingMetadata
from pi_recorder.queue import UploadQueue
from pi_recorder.storage import StorageManager


LOGGER = logging.getLogger(__name__)
UTC = timezone.utc


class CleanupManager:
    def __init__(
        self,
        storage: StorageManager,
        upload_queue: UploadQueue,
        retention_days: int,
        min_free_disk_mb: int,
    ) -> None:
        self.storage = storage
        self.upload_queue = upload_queue
        self.retention_days = retention_days
        self.minimum_free_bytes = min_free_disk_mb * 1024 * 1024

    def run(self, now: Optional[datetime] = None) -> int:
        current_time = (now or datetime.now(tz=UTC)).astimezone(UTC)
        deleted = 0
        cutoff = current_time - timedelta(days=self.retention_days)
        for recording in self.upload_queue.cleanup_candidates(cutoff):
            if self._delete_uploaded(recording, current_time):
                deleted += 1

        free_bytes = self.storage.free_bytes()
        if free_bytes < self.minimum_free_bytes:
            LOGGER.warning(
                "Disk space is low: %.1f MiB free; only confirmed uploads are eligible for cleanup",
                free_bytes / (1024 * 1024),
            )
            for recording in self.upload_queue.cleanup_candidates():
                if self.storage.free_bytes() >= self.minimum_free_bytes:
                    break
                if self._delete_uploaded(recording, current_time):
                    deleted += 1

        if self.storage.free_bytes() < self.minimum_free_bytes:
            LOGGER.error(
                "Disk space remains below the configured minimum; pending recordings were preserved"
            )
        return deleted

    def _delete_uploaded(self, recording: RecordingMetadata, now: datetime) -> bool:
        if not recording.file_path.exists():
            self.upload_queue.mark_local_deleted(recording.recording_id, now)
            return True
        if not self.storage.unlink_recording(recording.file_path):
            LOGGER.error("Refused to remove unsafe recording path %s", recording.file_path)
            return False
        self.upload_queue.mark_local_deleted(recording.recording_id, now)
        LOGGER.info("Removed retained uploaded recording %s", recording.filename)
        return True
