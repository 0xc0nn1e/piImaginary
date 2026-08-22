# Pi Recorder Client

[廣東話](README.zh-HK.md) · [日本語](README.ja.md)

A low-resource, failure-tolerant audio recorder and upload client for Raspberry Pi. It records speech-ready chunks locally, stores upload work in SQLite, and syncs independently when the network is available. Server-side transcription, diarization, speaker identification, and LLM processing belong in a separate repository.

## Architecture

```text
USB microphone -> ALSA arecord (Linux) / FFmpeg AVFoundation (macOS)
                                  |
                                  v
                         .wav.partial -> validate and atomic rename
                                                  |
                                                  v
                                         WAV file + SQLite queue
                                                  |
                                     uploader thread over HTTPS
                                                  v
                                           remote server API
                                                  ^
                                     heartbeat thread every 10 min
```

The recorder never waits for the uploader. Only closed, validated files enter the queue. A reboot changes stale `uploading` entries back to `pending`; failed uploads use capped exponential backoff. Cleanup selects only server-confirmed `uploaded` files.

## Hardware and Audio Format

The primary target is Raspberry Pi Zero 2 W (512 MB) on Raspberry Pi OS with a USB microphone. Pi 4 and Pi 5 are also supported in principle. macOS is a supported secondary runtime through FFmpeg AVFoundation. I2S input is future work.

The MVP uses mono, 16 kHz, 16-bit PCM WAV in 10-minute chunks. WAV is native to `arecord`, has negligible encoding cost, and is widely accepted by transcription systems. It uses about 18.3 MiB per 10 minutes (about 2.76 GB/day). Every WAV is limited to 98,000,000 bytes. Startup rejects audio settings whose estimated chunk size exceeds that ceiling, and an unexpected oversized file is preserved but never queued or uploaded. FLAC would reduce storage without loss but adds an encoder and another failure boundary; Opus is smaller but lossy and also needs extra tooling. Compression should later run after capture in a separate worker.

## Raspberry Pi OS Setup

Install Python, ALSA tools, and virtual-environment support:

```bash
sudo apt update
sudo apt install -y alsa-utils python3 python3-venv
arecord -l
```

### Select the USB microphone explicitly

Do not rely on ALSA's `default` input when the recorder must use only the USB microphone. Raspberry Pi boards do not have a built-in microphone, but `default` may still resolve to another attached audio device. Connect the USB microphone, then list hardware and named ALSA devices:

```bash
arecord -l
arecord -L
```

Find the USB entry, for example `card 1: Device [USB PnP Sound Device], device 0`. Prefer the card name from that output instead of the numeric card index, which can change after a reboot. Test that exact device with `plughw`, which can adapt the microphone's native format to the required mono 16 kHz PCM format:

```bash
arecord -D plughw:CARD=Device,DEV=0 -t wav -f S16_LE -r 16000 -c 1 -d 5 usb-test.wav
aplay usb-test.wav
```

Replace `Device` with the card name shown on your Pi, then set the same value in `.env`:

```dotenv
AUDIO_DEVICE=plughw:CARD=Device,DEV=0
```

If capture is silent or distorted, use `alsamixer`, press F6, select the USB card, and adjust its Capture level. On a Pi Zero 2 W, an unstable microphone or repeated USB disconnects can indicate insufficient power; try a powered USB hub. Avoid `AUDIO_DEVICE=default` when selecting the USB microphone is mandatory.

## macOS Setup and Recording

macOS uses the FFmpeg AVFoundation backend instead of Linux ALSA. It supports the full recorder flow: chunk finalization, local SQLite queue, upload, retry, cleanup, and graceful shutdown.

With [Homebrew](https://brew.sh/) installed:

```bash
brew install python@3.12 ffmpeg
python3.12 -m venv .venv
.venv/bin/python -m pip install -e '.[dev]'
.venv/bin/python -m pytest -q
```

Connect the USB microphone. In **System Settings → Sound → Input**, select the USB microphone, not the Mac microphone. Apple documents this under [Sound Input settings](https://support.apple.com/guide/mac-help/change-the-sound-input-settings-mchlp2567/mac). Then list AVFoundation devices:

```bash
ffmpeg -f avfoundation -list_devices true -i ""
```

Under `AVFoundation audio devices`, find the USB microphone's index, replace `N` below, and select it explicitly. Do not use `:default` because that could select the internal microphone.

```bash
ffmpeg -f avfoundation -i ":N" -t 5 -ac 1 -ar 16000 -c:a pcm_s16le usb-test.wav
afplay usb-test.wav
```

The first capture may ask for microphone permission for Terminal. The device syntax follows the [FFmpeg AVFoundation input documentation](https://ffmpeg.org/ffmpeg-devices.html#avfoundation). Configure the same USB index in `.env`:

```dotenv
AUDIO_BACKEND=avfoundation
AUDIO_DEVICE=N
FFMPEG_BINARY=ffmpeg
```

Run the complete recorder:

```bash
.venv/bin/python -m pi_recorder
```

`AUDIO_BACKEND=auto` also selects AVFoundation automatically on macOS. Explicit `avfoundation` is recommended while diagnosing a new setup. If macOS denied microphone access, enable your terminal application under **System Settings → Privacy & Security → Microphone**, then restart the recorder.

The macOS backend intentionally rejects `AUDIO_DEVICE=default` so it cannot silently record from the internal microphone.

## Install and Configure

Runtime code has no Python package dependencies. `pytest` is development-only.

```bash
python3 -m venv .venv
.venv/bin/pip install .
cp .env.example .env
```

Edit `.env`. Important settings are:

```dotenv
DEVICE_ID=pi-recorder-01
AUDIO_BACKEND=auto
AUDIO_DEVICE=plughw:CARD=Device,DEV=0
RECORDING_DIR=./data/recordings
DATABASE_PATH=./data/recorder.db
CHUNK_MINUTES=10
MAX_WAV_BYTES=98000000
SERVER_URL=https://recorder.example.com
UPLOAD_ENDPOINT=/api/v1/recordings
API_TOKEN=
HEARTBEAT_ENDPOINT=/api/v1/heartbeats
HEARTBEAT_MINUTES=10
RETENTION_DAYS=7
MIN_FREE_DISK_MB=512
```

`SERVER_URL` must use HTTPS. Leave it empty to record and queue locally without uploading. `HEARTBEAT_ENDPOINT` reports device health every `HEARTBEAT_MINUTES`; leave it empty to disable reporting. Never commit `.env`, tokens, databases, or recordings.

On Raspberry Pi, replace `Device` with the USB card name reported by `arecord -l` or `arecord -L`. On macOS, use `AUDIO_BACKEND=avfoundation` and replace `AUDIO_DEVICE` with the USB index reported by FFmpeg.

## Run Manually

```bash
.venv/bin/python -m pi_recorder
```

Send `SIGINT` or `SIGTERM` to stop. The current valid partial chunk is closed, checksummed, and queued before exit.

### Upload one existing WAV without recording

The manual uploader reads the same `.env`, validates the WAV and 98,000,000-byte limit, calculates metadata and SHA-256, then displays progress while sending one request:

```bash
.venv/bin/python -m pi_recorder.manual_upload /path/to/audio.wav
# Equivalent after package installation:
.venv/bin/pi-recorder-upload /path/to/audio.wav
```

Use `--env-file /path/to/config.env` to select another configuration file. This command never opens a microphone, writes to the SQLite queue, retries in the background, or deletes the source file. A failed upload returns a non-zero exit status, so rerun it manually after fixing the problem.

## Install as a systemd Service

The supplied unit expects the application in `/opt/pi-recorder`, configuration at `/etc/pi-recorder/pi-recorder.env`, and a system user named `pi-recorder` in the `audio` group.

```bash
sudo useradd --system --create-home --home-dir /var/lib/pi-recorder --user-group --groups audio pi-recorder
sudo install -d -o pi-recorder -g pi-recorder /var/lib/pi-recorder /etc/pi-recorder
sudo cp .env.example /etc/pi-recorder/pi-recorder.env
sudo cp deploy/pi-recorder.service /etc/systemd/system/pi-recorder.service
sudo systemctl daemon-reload
sudo systemctl enable --now pi-recorder
sudo journalctl -u pi-recorder -f
```

Install the package into `/opt/pi-recorder/.venv` and set absolute `RECORDING_DIR` and `DATABASE_PATH` values owned by `pi-recorder` before starting. Protect the environment file if it contains a token: `sudo chmod 600 /etc/pi-recorder/pi-recorder.env`.

## Directory Structure

```text
src/pi_recorder/       recorder, storage, queue, uploader, and cleanup code
tests/                 hardware-free unit and local HTTP integration tests
deploy/                systemd unit
data/recordings/       runtime WAV chunks, grouped by UTC date (gitignored)
data/recorder.db       persistent upload state (gitignored)
```

## Server API Contract

The client sends `POST {SERVER_URL}{UPLOAD_ENDPOINT}` as `multipart/form-data` with:

- `audio`: `audio/wav` file
- `metadata`: JSON containing ID, UTC start/end, duration, filename, size, checksum, device ID, format, sample rate, and channels
- `checksum`: SHA-256 text
- Headers: `Authorization: Bearer …` when configured, `Idempotency-Key`, `X-Device-ID`, and `X-Content-SHA256`

Any HTTP 2xx response confirms storage. Other responses retry. The future server must make the recording ID idempotent, verify size and checksum, and return 2xx only after durable storage. AI processing happens after that acknowledgement and must not block ingestion.

The audio file itself is at most 98,000,000 bytes; the complete multipart request is slightly larger. Configure the reverse proxy and server request-body limit accordingly.

## Heartbeat API Contract

Every `HEARTBEAT_MINUTES` the client sends `POST {SERVER_URL}{HEARTBEAT_ENDPOINT}` with an `application/json` body, plus `X-Device-ID` and `Authorization: Bearer …` when a token is configured. Any HTTP 2xx accepts the report; anything else is logged as a warning and skipped until the next interval. Heartbeats are never queued, never retried, and never touch the SQLite database or the microSD card, so reporting failures cannot affect recording.

The client sends `status` `starting` on launch, `running` on each interval, and `stopping` during a clean shutdown. A missing `stopping` beat therefore separates a crash or power loss from an orderly restart.

Every value under `system` may be `null`, because macOS has no `/proc`. Queue counts become `null` if the database cannot be read.

```json
{
  "schema_version": 1,
  "device_id": "pi-recorder-01",
  "sent_at": "2026-08-23T04:00:00.000000+00:00",
  "status": "running",
  "recorder": {
    "audio_backend": "alsa",
    "audio_device": "plughw:CARD=Device,DEV=0",
    "chunk_seconds": 600,
    "process_uptime_seconds": 25201.4,
    "chunks_completed": 42,
    "last_chunk_completed_at": "2026-08-23T03:58:11.000000+00:00",
    "seconds_since_last_chunk": 109.0,
    "last_chunk_filename": "2026-08-23/20260823T034811Z_ab12cd.wav",
    "last_chunk_duration_seconds": 600.0,
    "consecutive_capture_failures": 0,
    "last_capture_error": null,
    "last_capture_error_at": null
  },
  "queue": {
    "pending": 0,
    "uploading": 0,
    "failed": 0,
    "uploaded": 41,
    "oldest_pending_created_at": null,
    "oldest_pending_age_seconds": null,
    "last_uploaded_at": "2026-08-23T03:58:14.000000+00:00",
    "seconds_since_last_upload": 106.0
  },
  "system": {
    "hostname": "pi-recorder-01",
    "platform": "Linux",
    "uptime_seconds": 90431.2,
    "load_average_1m": 0.21,
    "load_average_5m": 0.18,
    "load_average_15m": 0.14,
    "cpu_count": 4,
    "memory_total_bytes": 444030976,
    "memory_available_bytes": 301989888,
    "swap_total_bytes": 104857600,
    "swap_free_bytes": 104857600,
    "cpu_temperature_celsius": 48.3,
    "recording_disk_total_bytes": 31000000000,
    "recording_disk_free_bytes": 19200000000,
    "min_free_disk_mb": 512
  }
}
```

The server should alert on three conditions:

1. No heartbeat for more than `2.5 x HEARTBEAT_MINUTES` (25 minutes by default). The device is offline or the process died; the absence of a report is the signal, not a field inside it.
2. `recorder.seconds_since_last_chunk` above `2.5 x chunk_seconds`, or `recorder.consecutive_capture_failures` above zero. The process is alive but capture is broken, which usually means an unplugged USB microphone or a changed ALSA device name.
3. `queue.pending` plus `queue.failed` rising over time, or `system.recording_disk_free_bytes` near `system.min_free_disk_mb`. Uploads are backing up and the microSD card will fill.

Leave `HEARTBEAT_ENDPOINT` empty to disable reporting. It also requires `SERVER_URL`.

## Development and Testing

```bash
.venv/bin/pip install -e '.[dev]'
.venv/bin/python -m pytest -q
.venv/bin/python -m compileall -q src tests
```

Tests use a fake audio source and small generated WAV files; microphone hardware is not required.

## Troubleshooting

- `arecord: not found`: install `alsa-utils` or set `ARECORD_BINARY`.
- `ffmpeg executable not found`: on macOS, run `brew install ffmpeg`.
- macOS input errors: verify `AUDIO_DEVICE` against the current AVFoundation list and allow microphone access for the terminal application. Device indices may change after reconnecting hardware.
- Device errors: run `arecord -l`, verify `AUDIO_DEVICE`, group membership, and that no other process owns the microphone.
- Files remain pending: verify HTTPS URL, DNS/Wi-Fi, token, and `journalctl`; recording continues while the server is unavailable.
- `WAV file ... maximum`: shorten `CHUNK_MINUTES`, reduce sample rate/channels, or manually split an existing WAV. Oversized files are not uploaded.
- Disk warning: upload or network service must recover. Only confirmed uploads are removed, even under low-space pressure.
- Configuration exit: check positive numeric values, the device ID characters, and HTTPS `SERVER_URL`.
- `Heartbeat (running) failed`: reporting could not reach the server. Recording and uploading are unaffected; verify `HEARTBEAT_ENDPOINT` and that the server accepts JSON there.

## Roadmap

Possible follow-ups include post-capture FLAC compression, I2S sources, button/LED control, VAD, encryption at rest, and remote configuration. These must preserve the simple audio-source boundary and the rule that unconfirmed recordings are never deleted.
