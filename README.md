# Irodori AutoSpeak

## 概要

このツールは、Irodori-TTS-Server(TTSサーバー)・SearXNG(Web検索エンジン)・Simple-Memory-MCP(長期記憶HTTP MCPサーバー)と連携し、「記憶とWeb検索データとユーザーが与えた話題をもとに、永遠に喋り続ける」ものです。

作業中の暇潰しにラジオ感覚で流せるツールを目指しました。

このREADMEを除く全てはClaude Codeで書かれています。

## クイックスタート

まずは実行するMacでGit Cloneを行います。以後、使用するモードに合わせて選択します。

```bash
git clone https://github.com/Septe-l/irodori-autospeak.git
cd irodori-autospeak
```

### WebUI + Single TTSモード(デフォルト)

デフォルト。WebUIとTTSを同一のMacで動かすモードです。リアルタイムで処理するには、ある程度の性能を備えたAppleシリコンが必要です(M1Max/24Core/32GBでリアルタイム再生ができることを検証しています)

```bash
./run.sh
```

http://localhost:8000 が開きます。初回は仮想環境の構築とモデルの取得で数分かかります。

### WebUIモード

TTSは別のマシンに任せて、WebUIのみを実行するモードです。別にTTSを動かせるマシンがあり、WebUIをロースペックのマシンで実行する場合に選択します。

```bash
cp .env.example .env
$EDITOR .env    # AUTOSPEAK_TTS_REF_URLS に TTS を動かすマシンのアドレスを書く
./run.sh --webui-only
```

接続先が未設定のまま実行すると、その場で止まって必要な設定を表示します。

### オプション:WebUIモードをDockerで起動する

WebUIモードはDockerで実行することもできます。Linuxにも対応しています。

```bash
cd docker
cp .env.example .env
$EDITOR .env                  # 各サービスのアドレスを書く（127.0.0.1は不可）
docker compose up -d --build
```

### Single TTS サーバーモード

WebUIに接続可能なTTSのみを実行します。WebUIモードとの組み合わせや、2つのTTSを組み合わせる場合に使用します。

```bash
./run.sh --tts-only
```

使うモデルを変える場合は、実行前に指定します。

```bash
cp .env.example .env
$EDITOR .env    # AUTOSPEAK_TTS_CHECKPOINT を書き換える
./run.sh --tts-only
```

---

補足:これらに加えてLM StudioでのLLMが必要になり、Simple-Memory-MCPと連携する場合はEmbeddingモデルも必要になるため、大きなモデルで実行する場合には莫大なメモリを消費します。最低でも32GB以上のユニファイドメモリを備えたMacを使用するか、メモリに見合った小さなLLMを使用し、複数台のMacでメモリ負荷を分散させることを検討してください。

## 仕組み

Irodori-TTS-Serverを音声生成として活用し、LM StudioのLLMをテキスト生成として活用します。

ユーザーの入力文を元にSearXNGで検索し、得た情報をLM Studioでテキスト処理した後にIrodori-TTS-Serverで音声も生成、WebUIで再生します。

喋る内容はプロンプトで変更可能なほか、サンプル音声やテキスト指示を用いた声質の調整も可能です。

## スマホから使う

WebUIはPWAとして作ってあります。iPhone / iPad なら Safari で開いて
**共有 → ホーム画面に追加** すると、ブラウザのバーが消えてアプリのように全画面で起動します。

> iOSのホーム画面追加はHTTPのままで動きます。一方、Android や デスクトップの Chrome で
> 「インストール」を出すには HTTPS が必要です（`localhost` は例外）。LANのIPで直接開いている
> 場合、Chrome ではインストールできませんが、ブラウザとしては問題なく使えます。

## ドキュメント

| | |
|---|---|
| [仕組みと設定](docs/ARCHITECTURE.md) | 内部構成、設定項目、合成速度の実測値 |
| [環境構築](docs/SETUP.md) | SearXNG・LM Studio を含めたゼロからの手順 |
| [Docker](docs/DOCKER.md) | WebUIを別マシンのコンテナで動かす |

## 参照音声について

このツールは参照音声から声を複製します。自分が権利を持っている音声か、本人から許諾を得た音声だけを使ってください。

## ライセンス

このプログラムはMITライセンスの下でリリースされています。
