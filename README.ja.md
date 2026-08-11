# Pi Recorder Client

[English](README.md) · [廣東話](README.zh-HK.md)

Raspberry Pi 向けの省リソースで耐障害性のある録音・アップロードクライアントです。音声チャンクをローカルへ確実に保存し、アップロード処理を SQLite に永続化して、ネットワーク利用時に独立して同期します。文字起こし、話者分離、話者識別、LLM 解析は別のサーバーリポジトリで実装します。

## アーキテクチャ

```text
USB microphone -> arecord -> .wav.partial -> 検証・atomic rename
                                               |
                                               v
                                       WAV + SQLite queue
                                               |
                                      HTTPS uploader thread
                                               v
                                         remote server API
```

Recorder は uploader を待ちません。正常に閉じて検証したファイルだけを queue に追加します。再起動時には残った `uploading` を `pending` に戻し、失敗した upload は上限付き exponential backoff で再試行します。Cleanup の対象は server が確認済みの `uploaded` ファイルだけです。

## ハードウェアと音声形式

主な対象は Raspberry Pi Zero 2 W（512 MB）、Raspberry Pi OS、USB microphone です。Pi 4／Pi 5 でも動作できる設計です。I2S input は将来対応とします。

MVP は mono、16 kHz、16-bit PCM WAV を使用し、既定の chunk は 10 分です。WAV は `arecord` のネイティブ形式で encode 負荷がほぼなく、transcription system との互換性も高い一方、10 分で約 18.3 MiB、1 日で約 2.76 GB を使用します。FLAC は可逆圧縮ですが encoder と failure boundary が増えます。Opus はさらに小さいものの非可逆で追加ツールが必要です。将来の compression は capture 後の独立 worker として追加します。

## Raspberry Pi OS の準備

Python、ALSA ツール、virtual environment をインストールします。

```bash
sudo apt update
sudo apt install -y alsa-utils python3 python3-venv
arecord -l
```

### USB microphone を明示的に選択する

Recorder が USB microphone だけを使用する場合、ALSA の `default` input に依存しないでください。Raspberry Pi 本体に内蔵 microphone はありませんが、`default` が別の接続済み audio device を指す可能性があります。USB microphone を接続し、hardware と名前付き ALSA devices を表示します。

```bash
arecord -l
arecord -L
```

たとえば `card 1: Device [USB PnP Sound Device], device 0` のような USB entry を探します。Reboot 後に変わる可能性がある card number ではなく、出力された card name を使用してください。`plughw` は microphone の native format を必要な mono 16 kHz PCM に変換できるため、次のように指定 device をテストします。

```bash
arecord -D plughw:CARD=Device,DEV=0 -t wav -f S16_LE -r 16000 -c 1 -d 5 usb-test.wav
aplay usb-test.wav
```

`Device` を Pi に表示された card name に置き換え、同じ値を `.env` に設定します。

```dotenv
AUDIO_DEVICE=plughw:CARD=Device,DEV=0
```

録音が無音または歪む場合は `alsamixer` を実行し、F6 で USB card を選び、Capture level を調整します。Pi Zero 2 W で microphone が不安定、または USB disconnect が繰り返される場合は電力不足の可能性があるため、powered USB hub を試してください。USB microphone の指定が必須なら `AUDIO_DEVICE=default` は使用しません。

## macOS Development と USB Microphone テスト

macOS には `apt`、ALSA、`arecord` がありません。Mac は development、unit tests、queue/upload integration tests、独立した USB microphone smoke test に使用できます。Production recording command は Linux 専用なので、macOS で `python -m pi_recorder` を audio capture 用に実行しないでください。また `ARECORD_BINARY=ffmpeg` も設定しないでください。両者の CLI は互換ではありません。

[Homebrew](https://brew.sh/) をインストールしてから実行します。

```bash
brew install python@3.12 ffmpeg
python3.12 -m venv .venv
.venv/bin/python -m pip install -e '.[dev]'
.venv/bin/python -m pytest -q
```

USB microphone を接続し、**System Settings → Sound → Input** で Mac microphone ではなく USB microphone を選びます。Apple の手順は [Sound Input settings](https://support.apple.com/guide/mac-help/change-the-sound-input-settings-mchlp2567/mac) を参照してください。次に AVFoundation devices を表示します。

```bash
ffmpeg -f avfoundation -list_devices true -i ""
```

`AVFoundation audio devices` の USB microphone index を確認し、次の `N` をその index に置き換えて USB input を明示的に指定します。`:default` は internal microphone を選ぶ可能性があるため使用しません。

```bash
ffmpeg -f avfoundation -i ":N" -t 5 -ac 1 -ar 16000 -c:a pcm_s16le usb-test.wav
afplay usb-test.wav
```

初回録音時に macOS が Terminal の microphone permission を要求する場合があります。Device syntax は [FFmpeg AVFoundation input documentation](https://ffmpeg.org/ffmpeg-devices.html#avfoundation) に基づきます。Mac で完全な end-to-end recorder を実行するには、将来別の macOS audio-source adapter が必要です。これは Raspberry Pi MVP の対象外です。

## インストールと設定

Runtime code に Python package dependency はありません。`pytest` は development 専用です。

```bash
python3 -m venv .venv
.venv/bin/pip install .
cp .env.example .env
```

`.env` の主な設定項目は次のとおりです。

```dotenv
DEVICE_ID=pi-recorder-01
AUDIO_DEVICE=plughw:CARD=Device,DEV=0
RECORDING_DIR=./data/recordings
DATABASE_PATH=./data/recorder.db
CHUNK_MINUTES=10
SERVER_URL=https://recorder.example.com
UPLOAD_ENDPOINT=/api/v1/recordings
API_TOKEN=
RETENTION_DAYS=7
MIN_FREE_DISK_MB=512
```

`SERVER_URL` は HTTPS が必須です。空欄の場合は録音とローカル queue 保存だけを行います。`.env`、token、database、録音ファイルは commit しないでください。

`Device` を target Pi の `arecord -l` または `arecord -L` に表示された USB card name に置き換えてください。

## 手動実行

```bash
.venv/bin/python -m pi_recorder
```

`SIGINT` または `SIGTERM` で停止します。現在の有効な partial chunk を閉じ、checksum を計算して queue へ追加してから終了します。

## systemd Service のインストール

付属 unit は application を `/opt/pi-recorder`、設定を `/etc/pi-recorder/pi-recorder.env` に配置し、`audio` group に属する `pi-recorder` system user を使用します。

```bash
sudo useradd --system --create-home --home-dir /var/lib/pi-recorder --user-group --groups audio pi-recorder
sudo install -d -o pi-recorder -g pi-recorder /var/lib/pi-recorder /etc/pi-recorder
sudo cp .env.example /etc/pi-recorder/pi-recorder.env
sudo cp deploy/pi-recorder.service /etc/systemd/system/pi-recorder.service
sudo systemctl daemon-reload
sudo systemctl enable --now pi-recorder
sudo journalctl -u pi-recorder -f
```

起動前に package を `/opt/pi-recorder/.venv` へインストールし、`pi-recorder` が所有する絶対パスを `RECORDING_DIR` と `DATABASE_PATH` に設定します。Token がある環境ファイルは `sudo chmod 600 /etc/pi-recorder/pi-recorder.env` で保護してください。

## ディレクトリ構成

```text
src/pi_recorder/       recorder、storage、queue、uploader、cleanup
tests/                 hardware 不要の unit・ローカル HTTP integration tests
deploy/                systemd unit
data/recordings/       UTC 日付別の runtime WAV（gitignored）
data/recorder.db       persistent upload state（gitignored）
```

## Server API Contract

Client は `POST {SERVER_URL}{UPLOAD_ENDPOINT}` を `multipart/form-data` で送信します。

- `audio`: `audio/wav` ファイル
- `metadata`: ID、UTC 開始／終了時刻、duration、filename、size、checksum、device ID、format、sample rate、channels を含む JSON
- `checksum`: SHA-256 文字列
- Headers: 設定時の `Authorization: Bearer …`、`Idempotency-Key`、`X-Device-ID`、`X-Content-SHA256`

HTTP 2xx のみを保存確認とし、それ以外は再試行します。将来の server は recording ID を idempotent に扱い、size と checksum を検証し、durable storage 完了後だけ 2xx を返す必要があります。AI processing は ACK の後で実行し、ingestion を止めない設計にします。

## Development とテスト

```bash
.venv/bin/pip install -e '.[dev]'
.venv/bin/python -m pytest -q
.venv/bin/python -m compileall -q src tests
```

テストは fake audio source と小さな生成 WAV を使用するため、microphone hardware は不要です。

## トラブルシューティング

- `arecord: not found`: `alsa-utils` をインストールするか `ARECORD_BINARY` を設定します。
- Device error: `arecord -l`、`AUDIO_DEVICE`、group 権限、他 process による microphone 占有を確認します。
- Pending のまま: HTTPS URL、DNS／Wi-Fi、token、`journalctl` を確認します。Server 停止中も録音は継続します。
- Disk warning: upload／network を復旧してください。低容量時でも確認済み upload 以外は削除しません。
- Configuration exit: 正の整数値、device ID の文字、HTTPS の `SERVER_URL` を確認します。

## Roadmap

Capture 後の FLAC compression、I2S source、button／LED、VAD、at-rest encryption、remote configuration、health reporting を将来候補とします。追加時も単純な audio-source boundary と「未確認 upload の録音は削除しない」規則を守ります。
