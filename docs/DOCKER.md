# WebUI をDockerで動かす

> [README](../README.md) ・ [仕組みと設定](ARCHITECTURE.md) ・ [環境構築](SETUP.md) ・ [Docker](DOCKER.md)


WebUIはHTTPクライアントとFastAPIだけで、モデルもGPUも使いません。x86 Linuxでも動きます。
TTSサーバーはGPU（Apple MPS）が要るのでMacに残し、LAN越しに使います。

```
┌──────────────────────┐        ┌─────────────────────────┐
│ Linux / Docker       │  HTTP  │ Mac                     │
│  WebUI :8000         │───────▶│  Irodori-TTS :8088      │
└──────────┬───────────┘        └─────────────────────────┘
           │ HTTP                ┌─────────────────────────┐
           ├──▶ SearXNG          │ 別のMac                  │
           └──▶ LM Studio  ─────▶│  Irodori-TTS :8089      │
                                 └─────────────────────────┘
```

## 手順

プロジェクト全体をDockerホストに置きます（イメージのビルドに `backend/` と `static/` が要るため）。

```bash
cd docker
cp .env.example .env
$EDITOR .env          # 各サービスのアドレスを自分の環境に合わせる
docker compose up -d --build
```

http://<Dockerホスト>:8000 で開きます。

Mac側はTTSだけ動かします。

```bash
./run.sh --tts-only
```

## 注意

**`.env` のアドレスはコンテナの中から見えるものにしてください。** `127.0.0.1` はコンテナ自身を
指すので使えません。SearXNGが同じLinux機で動いていても、`127.0.0.1` ではなくLAN IPを書きます。

誤って `127.0.0.1` を書いた場合、起動時のログに警告が出ます。

```
WARNING mode=webui but these TTS endpoints point at this container/host: [...]
```

## 保存されるもの

| パス | 中身 |
|---|---|
| `docker/logs/` | 喋った内容のログ（1ターン1行のJSONL） |
| `docker/data/settings.local.json` | WebUIで編集した設定（ペルソナ、TTS接続先、話者モード等） |

`.env` のTTS URLは**初回のみ**エンドポイント一覧の初期値として使われます。以降はWebUIの
「設定 → TTSサーバー」で編集し、`data/settings.local.json` に保存されます。
接続先を変えたいときはWebUIから変更してください。

## 更新

```bash
docker compose up -d --build   # ソースを更新したあと
docker compose logs -f         # ログ
docker compose down            # 停止
```
