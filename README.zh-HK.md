# Pi Recorder Client

[English](README.md) · [日本語](README.ja.md)

呢個係為 Raspberry Pi 而設、低資源同容錯嘅錄音及 upload client。佢會將語音 chunk 可靠寫入本機、用 SQLite 保存 upload 工作，網絡可用時先獨立同步。轉錄、speaker diarization、speaker identification 同 LLM 分析屬另一個 server repository，唔會喺呢度實作。

## Architecture

```text
USB microphone -> ALSA arecord（Linux）/ FFmpeg AVFoundation（macOS）
                                  |
                                  v
                         .wav.partial -> 驗證及 atomic rename
                                                  |
                                                  v
                                          WAV + SQLite queue
                                                  |
                                         HTTPS uploader thread
                                                  v
                                            remote server API
                                                  ^
                                      heartbeat thread 每 10 分鐘
```

Recorder 永遠唔等 uploader。只有已關閉兼驗證成功嘅檔案先入 queue。重啟時，殘留嘅 `uploading` 會還原做 `pending`；失敗 upload 會用有上限嘅 exponential backoff。Cleanup 只會揀 server 已確認嘅 `uploaded` 檔案。

## Hardware 同 Audio Format

主要 target 係 Raspberry Pi Zero 2 W（512 MB）、Raspberry Pi OS 同 USB microphone。設計原則上亦支援 Pi 4／Pi 5；macOS 係透過 FFmpeg AVFoundation 支援嘅 secondary runtime。I2S input 留待日後加入。

MVP 使用 mono、16 kHz、16-bit PCM WAV，每段預設 10 分鐘。WAV 係 `arecord` 原生格式，幾乎無 encoding CPU 成本，而且 transcription system 普遍支援；代價係每 10 分鐘約 18.3 MiB，即每日約 2.76 GB。每個 WAV 上限係 98,000,000 bytes；audio 設定估算會超限時，程式會拒絕啟動，異常產生嘅超限檔亦只會保留，唔會入 queue 或 upload。FLAC 無損兼慳空間，但要多一個 encoder 同 failure boundary；Opus 更細，但屬有損兼要額外工具。日後 compression 應該放喺 capture 完成後嘅獨立 worker。

## Raspberry Pi OS 設定

安裝 Python、ALSA 工具同 virtual environment 支援：

```bash
sudo apt update
sudo apt install -y alsa-utils python3 python3-venv
arecord -l
```

### 明確選擇 USB microphone

如果 recorder 只可以用 USB microphone，就唔好依賴 ALSA `default` input。Raspberry Pi 本身無內置 microphone，但 `default` 仍有可能指向另一個已連接嘅 audio device。插入 USB microphone 後，列出 hardware 同有名稱嘅 ALSA devices：

```bash
arecord -l
arecord -L
```

搵出 USB entry，例如 `card 1: Device [USB PnP Sound Device], device 0`。最好用輸出入面嘅 card name，唔好用 reboot 後可能改變嘅 card number。用 `plughw` 測試指定 device；佢可以將 microphone 原生格式轉成所需嘅 mono 16 kHz PCM：

```bash
arecord -D plughw:CARD=Device,DEV=0 -t wav -f S16_LE -r 16000 -c 1 -d 5 usb-test.wav
aplay usb-test.wav
```

將 `Device` 換成你部 Pi 顯示嘅 card name，再將同一個值寫入 `.env`：

```dotenv
AUDIO_DEVICE=plughw:CARD=Device,DEV=0
```

如果錄音無聲或者失真，執行 `alsamixer`、按 F6、選 USB card，再調整 Capture level。Pi Zero 2 W 如果 microphone 唔穩定或者不停 USB disconnect，可能係供電不足，可以試 powered USB hub。一定要指定 USB microphone 時，避免使用 `AUDIO_DEVICE=default`。

## macOS 設定同錄音

macOS 會用 FFmpeg AVFoundation backend，唔使用 Linux ALSA。Mac 支援完整 recorder flow，包括 chunk finalization、本機 SQLite queue、upload、retry、cleanup 同 graceful shutdown。

先安裝 [Homebrew](https://brew.sh/)，然後執行：

```bash
brew install python@3.12 ffmpeg
python3.12 -m venv .venv
.venv/bin/python -m pip install -e '.[dev]'
.venv/bin/python -m pytest -q
```

插入 USB microphone，去 **System Settings → Sound → Input** 揀 USB microphone，唔好揀 Mac microphone。Apple 嘅步驟見 [Sound Input settings](https://support.apple.com/guide/mac-help/change-the-sound-input-settings-mchlp2567/mac)。之後列出 AVFoundation devices：

```bash
ffmpeg -f avfoundation -list_devices true -i ""
```

喺 `AVFoundation audio devices` 下面搵 USB microphone index，將以下 `N` 換成該 index，明確指定 USB input。唔好用 `:default`，因為佢可能會揀 internal microphone。

```bash
ffmpeg -f avfoundation -i ":N" -t 5 -ac 1 -ar 16000 -c:a pcm_s16le usb-test.wav
afplay usb-test.wav
```

第一次錄音時，macOS 可能會要求授予 Terminal microphone permission。Device syntax 來自 [FFmpeg AVFoundation input 文件](https://ffmpeg.org/ffmpeg-devices.html#avfoundation)。

將同一個 USB index 寫入 `.env`：

```dotenv
AUDIO_BACKEND=avfoundation
AUDIO_DEVICE=N
FFMPEG_BINARY=ffmpeg
```

運行完整 recorder：

```bash
.venv/bin/python -m pi_recorder
```

`AUDIO_BACKEND=auto` 喺 macOS 亦會自動選擇 AVFoundation。新 setup 排查時建議先明確設定 `avfoundation`。如果 macOS 拒絕 microphone 權限，去 **System Settings → Privacy & Security → Microphone** 允許你使用嘅 terminal application，再重開 recorder。

macOS backend 會刻意拒絕 `AUDIO_DEVICE=default`，避免程式靜默使用 internal microphone。

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

`SERVER_URL` 必須用 HTTPS。留空就只錄音及保存在本機 queue，唔會 upload。`HEARTBEAT_ENDPOINT` 每隔 `HEARTBEAT_MINUTES` 上報裝置健康狀態，留空即停用。永遠唔好 commit `.env`、token、database 或錄音。

Raspberry Pi 要將 `Device` 換成 `arecord -l` 或 `arecord -L` 顯示嘅 USB card name。macOS 就設定 `AUDIO_BACKEND=avfoundation`，並將 `AUDIO_DEVICE` 換成 FFmpeg 顯示嘅 USB index。

## 手動運行

```bash
.venv/bin/python -m pi_recorder
```

用 `SIGINT` 或 `SIGTERM` 停止。程式會先關閉現有有效 partial chunk、計 checksum 同入 queue，然後退出。

### 只 upload 一個現有 WAV（唔錄音）

手動 uploader 會讀取同一份 `.env`、驗證 WAV 同 98,000,000-byte 上限、建立 metadata 及 SHA-256，然後用 progress bar 顯示單次 upload 進度：

```bash
.venv/bin/python -m pi_recorder.manual_upload /path/to/audio.wav
# 安裝 package 後亦可用：
.venv/bin/pi-recorder-upload /path/to/audio.wav
```

如要用另一份設定，可加 `--env-file /path/to/config.env`。呢個 command 唔會開 microphone、唔會寫入 SQLite queue、唔會喺 background retry，亦唔會刪除原檔。Upload 失敗會回傳非零 exit status；修正問題後手動再執行即可。

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

Audio file 本身最多 98,000,000 bytes；完整 multipart request 會稍大，所以 reverse proxy 同 server request-body limit 要預留額外空間。

## Heartbeat API Contract

每隔 `HEARTBEAT_MINUTES`，client 會發送 `POST {SERVER_URL}{HEARTBEAT_ENDPOINT}`，body 係 `application/json`，另加 `X-Device-ID`，設定咗 token 就有 `Authorization: Bearer …`。任何 HTTP 2xx 代表 server 已接收；其他 response 只會記低一條 warning，等下一個週期再發。心跳唔會入 queue、唔會 retry、亦唔會寫 SQLite 或 microSD，所以上報失敗絕對唔會影響錄音。

Client 啟動時發 `status` 為 `starting`，每個週期發 `running`，正常關機時發 `stopping`。冇收到 `stopping` 就可以分辨到 crash／斷電同正常重啟。

`system` 下面每個值都可能係 `null`，因為 macOS 冇 `/proc`。如果 database 讀唔到，queue 數字亦會變 `null`。

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

Server 應該就三種情況發出告警：

1. 超過 `2.5 x HEARTBEAT_MINUTES`（預設 25 分鐘）冇收到心跳。裝置離線或者 process 死咗；訊號係「收唔到報告」本身，唔係報告入面某個欄位。
2. `recorder.seconds_since_last_chunk` 超過 `2.5 x chunk_seconds`，或者 `recorder.consecutive_capture_failures` 大過零。Process 生存但錄音壞咗，通常係 USB microphone 被拔或者 ALSA device name 變咗。
3. `queue.pending` 加 `queue.failed` 持續上升，或者 `system.recording_disk_free_bytes` 接近 `system.min_free_disk_mb`。Upload 積壓緊，microSD 會滿。

`HEARTBEAT_ENDPOINT` 留空即停用上報，另外亦需要設定 `SERVER_URL`。

## Development 同測試

```bash
.venv/bin/pip install -e '.[dev]'
.venv/bin/python -m pytest -q
.venv/bin/python -m compileall -q src tests
```

測試使用 fake audio source 同自動產生嘅細 WAV，唔需要 microphone hardware。

## 故障排查

- `arecord: not found`：安裝 `alsa-utils`，或者設定 `ARECORD_BINARY`。
- `ffmpeg executable not found`：macOS 執行 `brew install ffmpeg`。
- macOS input error：重新對照 AVFoundation device list，並確認 terminal application 有 microphone 權限。重新插拔 hardware 後 index 可能改變。
- Device error：執行 `arecord -l`，檢查 `AUDIO_DEVICE`、group 權限，同 microphone 有冇畀其他 process 佔用。
- 檔案一直 pending：檢查 HTTPS URL、DNS／Wi-Fi、token 同 `journalctl`；server offline 期間錄音仍會繼續。
- `WAV file ... maximum`：縮短 `CHUNK_MINUTES`、降低 sample rate／channels，或者先手動分割現有 WAV；超限檔唔會 upload。
- Disk warning：要恢復 upload／網絡服務；即使低空間，程式都只會移除已確認 upload 嘅檔。
- Configuration exit：檢查數值係正整數、device ID 字元，同 `SERVER_URL` 是否 HTTPS。
- `Heartbeat (running) failed`：上報去唔到 server。錄音同 upload 唔受影響；檢查 `HEARTBEAT_ENDPOINT`，同 server 喺嗰個 path 有冇收 JSON。

## Roadmap

日後可考慮 capture 後 FLAC compression、I2S source、button／LED、VAD、at-rest encryption、同 remote configuration。所有改動都要保留簡單 audio-source boundary，同「未確認 upload 嘅錄音永不刪除」規則。
