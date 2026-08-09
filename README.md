# Pi Recorder Client

[廣東話](README.zh-HK.md) · [日本語](README.ja.md)

A low-resource, failure-tolerant audio recorder and upload client for Raspberry Pi. It records speech-ready chunks locally, stores upload work in SQLite, and syncs independently when the network is available. Server-side transcription, diarization, speaker identification, and LLM processing belong in a separate repository.

## Architecture

```text
USB microphone -> arecord -> .wav.partial -> validate and atomic rename
                                               |
                                               v
                                      WAV file + SQLite queue
                                               |
                                  uploader thread over HTTPS
                                               v
                                        remote server API
```

The recorder never waits for the uploader. Only closed, validated files enter the queue. A reboot changes stale `uploading` entries back to `pending`; failed uploads use capped exponential backoff. Cleanup selects only server-confirmed `uploaded` files.

## Hardware and Audio Format

The primary target is Raspberry Pi Zero 2 W (512 MB) on Raspberry Pi OS with a USB microphone. Pi 4 and Pi 5 are also supported in principle. I2S input is future work.

The MVP uses mono, 16 kHz, 16-bit PCM WAV in 10-minute chunks. WAV is native to `arecord`, has negligible encoding cost, and is widely accepted by transcription systems. It uses about 18.3 MiB per 10 minutes (about 2.76 GB/day). FLAC would reduce storage without loss but adds an encoder and another failure boundary; Opus is smaller but lossy and also needs extra tooling. Compression should later run after capture in a separate worker.

## Raspberry Pi OS Setup

Install Python, ALSA tools, and virtual-environment support:

```bash
sudo apt update
sudo apt install -y alsa-utils python3 python3-venv
arecord -l
```

Test the selected microphone before running the service:

```bash
arecord -D default -t wav -f S16_LE -r 16000 -c 1 -d 5 test.wav
aplay test.wav
```

If needed, replace `default` with an ALSA device such as `plughw:CARD=Device,DEV=0`.

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
AUDIO_DEVICE=default
RECORDING_DIR=./data/recordings
DATABASE_PATH=./data/recorder.db
CHUNK_MINUTES=10
SERVER_URL=https://recorder.example.com
UPLOAD_ENDPOINT=/api/v1/recordings
API_TOKEN=
RETENTION_DAYS=7
MIN_FREE_DISK_MB=512
```

`SERVER_URL` must use HTTPS. Leave it empty to record and queue locally without uploading. Never commit `.env`, tokens, databases, or recordings.

## Run Manually

```bash
.venv/bin/python -m pi_recorder
```

Send `SIGINT` or `SIGTERM` to stop. The current valid partial chunk is closed, checksummed, and queued before exit.

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

## Development and Testing

```bash
.venv/bin/pip install -e '.[dev]'
.venv/bin/python -m pytest -q
.venv/bin/python -m compileall -q src tests
```

Tests use a fake audio source and small generated WAV files; microphone hardware is not required.

## Troubleshooting

- `arecord: not found`: install `alsa-utils` or set `ARECORD_BINARY`.
- Device errors: run `arecord -l`, verify `AUDIO_DEVICE`, group membership, and that no other process owns the microphone.
- Files remain pending: verify HTTPS URL, DNS/Wi-Fi, token, and `journalctl`; recording continues while the server is unavailable.
- Disk warning: upload or network service must recover. Only confirmed uploads are removed, even under low-space pressure.
- Configuration exit: check positive numeric values, the device ID characters, and HTTPS `SERVER_URL`.

## Roadmap

Possible follow-ups include post-capture FLAC compression, I2S sources, button/LED control, VAD, encryption at rest, remote configuration, and health reporting. These must preserve the simple audio-source boundary and the rule that unconfirmed recordings are never deleted.
