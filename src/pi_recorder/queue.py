import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import List, Optional

from pi_recorder.models import FAILED, PENDING, UPLOADED, UPLOADING, RecordingMetadata


UTC = timezone.utc


def _now_iso(now: Optional[datetime] = None) -> str:
    return (now or datetime.now(tz=UTC)).astimezone(UTC).isoformat(timespec="microseconds")


class UploadQueue:
    def __init__(self, database_path: Path) -> None:
        self.database_path = database_path.resolve()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(str(self.database_path), timeout=30.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA synchronous=FULL")
        connection.execute("PRAGMA busy_timeout=30000")
        return connection

    def initialize(self) -> None:
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS recordings (
                    recording_id TEXT PRIMARY KEY,
                    start_time TEXT NOT NULL,
                    end_time TEXT NOT NULL,
                    duration_seconds REAL NOT NULL,
                    filename TEXT NOT NULL,
                    file_path TEXT NOT NULL UNIQUE,
                    file_size INTEGER NOT NULL,
                    checksum_sha256 TEXT NOT NULL,
                    upload_status TEXT NOT NULL,
                    retry_count INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    device_id TEXT NOT NULL,
                    audio_format TEXT NOT NULL,
                    sample_rate INTEGER NOT NULL,
                    channels INTEGER NOT NULL,
                    next_attempt_at TEXT,
                    last_error TEXT,
                    uploaded_at TEXT,
                    local_deleted_at TEXT,
                    CHECK (upload_status IN ('pending', 'uploading', 'uploaded', 'failed'))
                )
                """
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_recordings_upload
                ON recordings (upload_status, next_attempt_at, created_at)
                """
            )

    def add(self, recording: RecordingMetadata) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO recordings (
                    recording_id, start_time, end_time, duration_seconds, filename,
                    file_path, file_size, checksum_sha256, upload_status, retry_count,
                    created_at, device_id, audio_format, sample_rate, channels,
                    next_attempt_at, last_error, uploaded_at, local_deleted_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    recording.recording_id,
                    recording.start_time,
                    recording.end_time,
                    recording.duration_seconds,
                    recording.filename,
                    str(recording.file_path.resolve()),
                    recording.file_size,
                    recording.checksum_sha256,
                    recording.upload_status,
                    recording.retry_count,
                    recording.created_at,
                    recording.device_id,
                    recording.audio_format,
                    recording.sample_rate,
                    recording.channels,
                    recording.next_attempt_at,
                    recording.last_error,
                    recording.uploaded_at,
                    recording.local_deleted_at,
                ),
            )

    def contains_file(self, path: Path) -> bool:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT 1 FROM recordings WHERE file_path = ?", (str(path.resolve()),)
            ).fetchone()
        return row is not None

    def recover_uploading(self, now: Optional[datetime] = None) -> int:
        timestamp = _now_iso(now)
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE recordings
                SET upload_status = ?, next_attempt_at = ?,
                    last_error = 'Recovered interrupted upload after restart'
                WHERE upload_status = ?
                """,
                (PENDING, timestamp, UPLOADING),
            )
        return cursor.rowcount

    def claim_next(self, now: Optional[datetime] = None) -> Optional[RecordingMetadata]:
        timestamp = _now_iso(now)
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT * FROM recordings
                WHERE upload_status IN (?, ?)
                  AND (next_attempt_at IS NULL OR next_attempt_at <= ?)
                  AND local_deleted_at IS NULL
                ORDER BY created_at ASC
                LIMIT 1
                """,
                (PENDING, FAILED, timestamp),
            ).fetchone()
            if row is None:
                connection.commit()
                return None
            connection.execute(
                "UPDATE recordings SET upload_status = ? WHERE recording_id = ?",
                (UPLOADING, row["recording_id"]),
            )
            connection.commit()
            return self._row_to_recording(row).with_queue_state(upload_status=UPLOADING)
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def mark_uploaded(self, recording_id: str, now: Optional[datetime] = None) -> None:
        timestamp = _now_iso(now)
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE recordings
                SET upload_status = ?, uploaded_at = ?, next_attempt_at = NULL, last_error = NULL
                WHERE recording_id = ?
                """,
                (UPLOADED, timestamp, recording_id),
            )

    def mark_failed(
        self,
        recording_id: str,
        error: str,
        retry_base_seconds: int,
        retry_max_seconds: int,
        now: Optional[datetime] = None,
    ) -> int:
        current_time = (now or datetime.now(tz=UTC)).astimezone(UTC)
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT retry_count FROM recordings WHERE recording_id = ?", (recording_id,)
            ).fetchone()
            if row is None:
                raise KeyError("Unknown recording {}".format(recording_id))
            previous_retries = int(row["retry_count"])
            retry_count = previous_retries + 1
            delay = min(retry_max_seconds, retry_base_seconds * (2 ** min(previous_retries, 30)))
            next_attempt = current_time + timedelta(seconds=delay)
            connection.execute(
                """
                UPDATE recordings
                SET upload_status = ?, retry_count = ?, next_attempt_at = ?, last_error = ?
                WHERE recording_id = ?
                """,
                (
                    FAILED,
                    retry_count,
                    _now_iso(next_attempt),
                    error[:1000],
                    recording_id,
                ),
            )
            connection.commit()
            return delay
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def cleanup_candidates(self, uploaded_before: Optional[datetime] = None) -> List[RecordingMetadata]:
        query = """
            SELECT * FROM recordings
            WHERE upload_status = ? AND local_deleted_at IS NULL
        """
        parameters = [UPLOADED]
        if uploaded_before is not None:
            query += " AND uploaded_at <= ?"
            parameters.append(_now_iso(uploaded_before))
        query += " ORDER BY uploaded_at ASC"
        with self._connect() as connection:
            rows = connection.execute(query, parameters).fetchall()
        return [self._row_to_recording(row) for row in rows]

    def mark_local_deleted(self, recording_id: str, now: Optional[datetime] = None) -> None:
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE recordings SET local_deleted_at = ?
                WHERE recording_id = ? AND upload_status = ?
                """,
                (_now_iso(now), recording_id, UPLOADED),
            )
            if cursor.rowcount != 1:
                raise RuntimeError("Refused to mark an unconfirmed recording as locally deleted")

    def get(self, recording_id: str) -> Optional[RecordingMetadata]:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM recordings WHERE recording_id = ?", (recording_id,)
            ).fetchone()
        return None if row is None else self._row_to_recording(row)

    def count(self) -> int:
        with self._connect() as connection:
            row = connection.execute("SELECT COUNT(*) AS count FROM recordings").fetchone()
        return int(row["count"])

    @staticmethod
    def _row_to_recording(row: sqlite3.Row) -> RecordingMetadata:
        return RecordingMetadata(
            recording_id=row["recording_id"],
            start_time=row["start_time"],
            end_time=row["end_time"],
            duration_seconds=row["duration_seconds"],
            filename=row["filename"],
            file_path=Path(row["file_path"]),
            file_size=row["file_size"],
            checksum_sha256=row["checksum_sha256"],
            upload_status=row["upload_status"],
            retry_count=row["retry_count"],
            created_at=row["created_at"],
            device_id=row["device_id"],
            audio_format=row["audio_format"],
            sample_rate=row["sample_rate"],
            channels=row["channels"],
            next_attempt_at=row["next_attempt_at"],
            last_error=row["last_error"],
            uploaded_at=row["uploaded_at"],
            local_deleted_at=row["local_deleted_at"],
        )
