# Pi Recorder Client

[English](README.md) · [日本語](README.ja.md)

呢個係為 Raspberry Pi 而設、低資源同容錯嘅錄音及 upload client。佢會將語音 chunk 可靠寫入本機、用 SQLite 保存 upload 工作，網絡可用時先獨立同步。轉錄、speaker diarization、speaker identification 同 LLM 分析屬另一個 server repository，唔會喺呢度實作。

## Architecture

```text
USB microphone -> arecord -> .wav.partial -> 驗證及 atomic rename
                                               |
                                               v
                                       WAV + SQLite queue
                                               |
                                      HTTPS uploader thread
                                               v
                                         remote server API
```

Recorder 永遠唔等 uploader。只有已關閉兼驗證成功嘅檔案先入 queue。重啟時，殘留嘅 `uploading` 會還原做 `pending`；失敗 upload 會用有上限嘅 exponential backoff。Cleanup 只會揀 server 已確認嘅 `uploaded` 檔案。

## Hardware 同 Audio Format

主要 target 係 Raspberry Pi Zero 2 W（512 MB）、Raspberry Pi OS 同 USB microphone。設計原則上亦支援 Pi 4／Pi 5；I2S input 留待日後加入。

MVP 使用 mono、16 kHz、16-bit PCM WAV，每段預設 10 分鐘。WAV 係 `arecord` 原生格式，幾乎無 encoding CPU 成本，而且 transcription system 普遍支援；代價係每 10 分鐘約 18.3 MiB，即每日約 2.76 GB。FLAC 無損兼慳空間，但要多一個 encoder 同 failure boundary；Opus 更細，但屬有損兼要額外工具。日後 compression 應該放喺 capture 完成後嘅獨立 worker。

## Raspberry Pi OS 設定

安裝 Python、ALSA 工具同 virtual environment 支援：

```bash
sudo apt update
sudo apt install -y alsa-utils python3 python3-venv
arecord -l
```

啟動 service 前先測試 microphone：

```bash
arecord -D default -t wav -f S16_LE -r 16000 -c 1 -d 5 test.wav
aplay test.wav
```

如有需要，可將 `default` 改成 `plughw:CARD=Device,DEV=0` 等 ALSA device。

## 安裝同設定

Runtime code 無 Python package dependency；`pytest` 只用於 development。

```bash
python3 -m venv .venv
.venv/bin/pip install .
cp .env.example .env
```

編輯 `.env`，主要設定包括：

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

`SERVER_URL` 必須用 HTTPS。留空就只錄音及保存在本機 queue，唔會 upload。永遠唔好 commit `.env`、token、database 或錄音。

## 手動運行

```bash
.venv/bin/python -m pi_recorder
```

用 `SIGINT` 或 `SIGTERM` 停止。程式會先關閉現有有效 partial chunk、計 checksum 同入 queue，然後退出。

## 安裝 systemd Service

提供嘅 unit 預期 application 位於 `/opt/pi-recorder`、設定位於 `/etc/pi-recorder/pi-recorder.env`，並使用屬於 `audio` group 嘅 `pi-recorder` system user。

```bash
sudo useradd --system --create-home --home-dir /var/lib/pi-recorder --user-group --groups audio pi-recorder
sudo install -d -o pi-recorder -g pi-recorder /var/lib/pi-recorder /etc/pi-recorder
sudo cp .env.example /etc/pi-recorder/pi-recorder.env
sudo cp deploy/pi-recorder.service /etc/systemd/system/pi-recorder.service
sudo systemctl daemon-reload
sudo systemctl enable --now pi-recorder
sudo journalctl -u pi-recorder -f
```

啟動前，要將 package 安裝入 `/opt/pi-recorder/.venv`，並設定由 `pi-recorder` 擁有嘅絕對 `RECORDING_DIR` 同 `DATABASE_PATH`。環境檔有 token 時要保護權限：`sudo chmod 600 /etc/pi-recorder/pi-recorder.env`。

## 目錄結構

```text
src/pi_recorder/       recorder、storage、queue、uploader、cleanup
tests/                 唔需要 hardware 嘅 unit 及本機 HTTP integration tests
deploy/                systemd unit
data/recordings/       按 UTC 日期分類嘅 runtime WAV（gitignored）
data/recorder.db       persistent upload state（gitignored）
```

## Server API Contract

Client 用 `multipart/form-data` 發送 `POST {SERVER_URL}{UPLOAD_ENDPOINT}`：

- `audio`：`audio/wav` 檔案
- `metadata`：JSON，包含 ID、UTC 開始／結束時間、duration、filename、size、checksum、device ID、format、sample rate、channels
- `checksum`：SHA-256 文字
- Headers：設定咗先有 `Authorization: Bearer …`，以及 `Idempotency-Key`、`X-Device-ID`、`X-Content-SHA256`

任何 HTTP 2xx 都代表 server 已確認保存；其他 response 會 retry。未來 server 必須以 recording ID 做 idempotency、驗證 size／checksum，並只可以喺 durable storage 完成後回覆 2xx。AI processing 要喺 ACK 之後做，唔可以阻塞 ingestion。

## Development 同測試

```bash
.venv/bin/pip install -e '.[dev]'
.venv/bin/python -m pytest -q
.venv/bin/python -m compileall -q src tests
```

測試使用 fake audio source 同自動產生嘅細 WAV，唔需要 microphone hardware。

## 故障排查

- `arecord: not found`：安裝 `alsa-utils`，或者設定 `ARECORD_BINARY`。
- Device error：執行 `arecord -l`，檢查 `AUDIO_DEVICE`、group 權限，同 microphone 有冇畀其他 process 佔用。
- 檔案一直 pending：檢查 HTTPS URL、DNS／Wi-Fi、token 同 `journalctl`；server offline 期間錄音仍會繼續。
- Disk warning：要恢復 upload／網絡服務；即使低空間，程式都只會移除已確認 upload 嘅檔。
- Configuration exit：檢查數值係正整數、device ID 字元，同 `SERVER_URL` 是否 HTTPS。

## Roadmap

日後可考慮 capture 後 FLAC compression、I2S source、button／LED、VAD、at-rest encryption、remote configuration 同 health reporting。所有改動都要保留簡單 audio-source boundary，同「未確認 upload 嘅錄音永不刪除」規則。
