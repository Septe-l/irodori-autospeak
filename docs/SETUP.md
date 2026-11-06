# ゼロから環境を作る手順

> [README](../README.md) ・ [仕組みと設定](ARCHITECTURE.md) ・ [環境構築](SETUP.md) ・ [Docker](DOCKER.md)


このツールは3つの外部サービスに依存します。**SearXNG（検索）**、**LM Studio（文章生成）**、
**Irodori-TTS-Server（音声合成）**。この3つを用意してから本体を入れます。

前提はmacOS（Apple Silicon）です。TTSはMPSで動かします。

---

## 0. 構成を決める

一番重要な制約が1つあります。

> **LM StudioとIrodori-TTSを、同じ32GBのMacに同居させてはいけません。**

両方がGPUとユニファイドメモリを取り合い、スワップが発生します。実測では中央値は
正常なのに**1文の合成が107秒**かかる事故が起きました。中央値ではなく最悪値が
連続再生を壊します。

推奨は2台構成です。

| 役割 | 置き場所 |
|---|---|
| SearXNG | 何でもよい（Linuxの常時稼働機が楽） |
| LM Studio | Mac 1台を専有 |
| 本体アプリ ＋ Irodori-TTS | もう1台のMac |

1台しかない場合も動きますが、**LLMを小さいもの（7B〜14B程度）にしてください**。

TTSは1台（M1 Max）で `num_steps=24` なら RTF 0.56、つまり喋る速さの約1.8倍で
音声を作れます。**TTSサーバーを増やす必要は基本的にありません。**

---

## 1. SearXNG

Dockerで立てるのが早いです。

```bash
docker run -d --name searxng -p 10000:8080 \
  -v "$PWD/searxng:/etc/searxng" \
  -e "BASE_URL=http://localhost:10000/" \
  searxng/searxng
```

起動後、`searxng/settings.yml` の `search.formats` に `json` が含まれているか確認します。
含まれていなければ追記してコンテナを再起動してください。

```yaml
search:
  formats:
    - html
    - json
```

確認:

```bash
curl "http://<SearXNGのホスト>:10000/search?q=test&format=json&language=ja"
```

JSONが返れば完了です。`{"error": ...}` やHTMLが返る場合は `formats` の設定漏れです。

---

## 2. LM Studio

1. https://lmstudio.ai からインストール
2. 日本語の話し言葉が得意なモデルをダウンロード（検証では `gemma-4-26b-a4b-it-mlx` を使用。
   MoEで生成が速く、口調の指示によく従います）
3. モデルをロード
4. **Developer タブ → Server を起動**
5. **「Serve on Local Network」を有効化**（別Macから叩くために必須）

確認:

```bash
curl http://<LM Studioのホスト>:1234/v1/models
```

Server Settings で **「Require API token」** を有効にしている場合は、発行したトークンを
WebUIの「設定 → LLM」のAPIトークン欄に入れて「接続」を押してください（`.env` の
`AUTOSPEAK_LLM_API_KEY` でも初期値を渡せます）。確認の curl にも付けます:

```bash
curl -H "Authorization: Bearer <トークン>" http://<LM Studioのホスト>:1234/v1/models
```

> **注意:** LM Studioはメモリが足りないモデルのロードを拒否します
> （`Model loading was stopped due to insufficient system resources`）。
> 大きなモデルを2つ同時には持てないので、**使うモデルを1つだけロードした状態**にしてください。
> WebUIのモデル一覧には「ロード済み」と表示されるので、それを選ぶのが確実です。

---

## 3. 本体とIrodori-TTS

TTSを動かすMacで作業します。

```bash
# 必要なもの（これだけ）
brew install uv git
```

ffmpegは不要です（wavのみ使い、アップロード音声の変換はmacOS標準の `afconvert` で行います）。

```bash
git clone <このリポジトリ> ~/Documents/Claude/irodori-autospeak
cd ~/Documents/Claude/irodori-autospeak
./run.sh
```

`run.sh` が初回だけ以下を自動でやります。手動での準備は不要です。

- 本体の仮想環境の作成と依存インストール
- `.env.example` から `.env` を作成
- **Irodori-TTS-Server を `vendor/` にclone**して `uv sync --extra cpu` を実行

> **Irodori-TTS-Server はこのリポジトリに同梱していません。**
> `vendor/` 配下に独立したgitリポジトリとして置く形にしてあります。同梱すると本家の
> 更新を取り込めなくなるためです。更新したいときは:
> ```bash
> git -C vendor/Irodori-TTS-Server pull && (cd vendor/Irodori-TTS-Server && uv sync --extra cpu)
> ```
> 検証済みのコミットは `1fc3e10` です。

`--extra cpu` という名前ですが、pyproject内のCPU向けindex指定は linux/win32 に限定されて
いるため、macOSでは通常のPyPI版PyTorchが入ります。これはMPS対応版です。

初回実行が終わったら、`.env` を自分の環境に合わせて編集して `run.sh` を叩き直します。

### ポート

`.env` で変えられます。シェルスクリプトとPython側が同じ `.env` を読むので、
ここを書き換えるだけで `run.sh` / `start_tts.sh` / `stop_tts.sh` すべてが追従します。

```ini
AUTOSPEAK_HOST=0.0.0.0
AUTOSPEAK_PORT=8000           # WebUI
AUTOSPEAK_TTS_PORT=8088       # このマシンのTTS
AUTOSPEAK_TTS_CHECKPOINT=Aratako/Irodori-TTS-v4-Small
```

他のマシンのTTSは、WebUIの「設定 → TTSサーバー」で追加するのが基本です。
`AUTOSPEAK_TTS_REF_URLS` / `AUTOSPEAK_TTS_CAPTION_URLS` は初回起動時の初期値にしかなりません。

```ini
AUTOSPEAK_SEARXNG_URL=http://192.168.1.10:10000
AUTOSPEAK_LLM_URL=http://192.168.1.11:1234
AUTOSPEAK_LLM_MODEL=gemma-4-26b-a4b-it-mlx
AUTOSPEAK_TTS_REF_URLS=http://127.0.0.1:8088
AUTOSPEAK_TTS_CAPTION_URLS=http://127.0.0.1:8089
```

---

## 4. 起動

```bash
./run.sh
```

TTSサーバーを1つ立ち上げてから http://localhost:8000 を開きます。

**初回はモデルのダウンロードで数分かかります。**`AUTOSPEAK_TTS_CHECKPOINT` のものを取得します。

| チェックポイント | 用途 |
|---|---|
| `Aratako/Irodori-TTS-v4-Small`（既定） | 参照音声のクローンとテキスト指定の**両方** |
| `.../Irodori-TTS-v4-Small-Quantized/int8-weight-only` | 同上・省メモリ（MPSでは速くはならない） |
| `Aratako/Irodori-TTS-500M-v3` | 参照音声のみ（v3） |
| `Aratako/Irodori-TTS-600M-v3-VoiceDesign` | テキスト指定のみ（v3） |

いずれもMITライセンス・非ゲートなのでHugging Faceのトークンは不要です。

**1台につきTTSは1つです。** 2つ載せると片方が合成していなくてもメモリとGPUを取り合い、
約17%遅くなります。既定の v4 は1モデルで両方の声の作り方に対応するので、
通常は1台で足ります。合成を並列化したい場合だけ、2台目のマシンで
`./run.sh --tts-only` を動かし、WebUIの「設定 → TTSサーバー」でアドレスを追加してください。

2回目以降の起動は20秒ほどです。TTSだけ止めるときは `./stop_tts.sh`。

---

## 5. 初期設定（WebUIの「設定」から）

1. **LLMモデル** — 「ロード済み」と付いているものを選ぶ
2. **話者モード** — 「1人で喋る」か「2人が交互に喋る」
3. **参照音声の管理** — 音声ファイルをアップロード
   - wav / flac / mp3 / m4a / ogg / opus / aac / webm に対応
   - wav・flac以外は自動でwavに変換されます
   - **10〜15秒程度の、雑音のないはっきりした音声**が向いています
   - 長い音声は開始位置と長さを指定して切り出せます（既定15秒）
   - 名前は日本語でも構いません（「名前変更」で後から変えられます）
4. **各話者のペルソナ** — 口調と性格を書く。声に出して読める文章だけを出させるための
   共通ルール（記号を使わない、視聴者コメントを捏造しない等）は初期値に入っています
5. **num_steps** — 24が最適点。上げると品質は上がるが余裕がなくなります

ここで変更した内容は `settings.local.json` に保存され、再起動しても残ります。
URLとディレクトリだけは `.env` 側（起動時にのみ読む）です。

---

## 6. 動作確認

```bash
# TTSの合成速度。RTFが1.0未満なら喋る速さより速く作れている
./.venv/bin/python bench_tts.py --voice <voice_id> --steps 40,24,16

# 通し検証。無音が何秒生じたかを報告する
./.venv/bin/python e2e_test.py --topic "好きな話題" --switch-to "別の話題" --switch-after 90
```

`speech coverage` が95%以上、`gaps` が0なら正常です。

---

## WebUIを別マシンのDockerで動かす

WebUIはHTTPクライアントとFastAPIだけで、モデルもGPUも使いません。x86 Linuxでも動きます。
TTSサーバーはGPU（またはApple MPS）が要るので、Macに残したままLAN越しに使います。

```
┌──────────────────────┐        ┌─────────────────────────┐
│ Linux / Docker       │  HTTP  │ Mac                     │
│  WebUI :8000         │───────▶│  Irodori-TTS :8088/:8089│
└──────────┬───────────┘        └─────────────────────────┘
           │ HTTP
           ├──▶ SearXNG
           └──▶ LM Studio
```

Docker一式は `docker/` にまとまっています。詳細は [DOCKER.md](DOCKER.md)。

```bash
cd docker
cp .env.example .env
$EDITOR .env                 # コンテナから見えるアドレスを書く（127.0.0.1は不可）
docker compose up -d --build
```

別マシンへ丸ごと送って起動するスクリプトもあります。

```bash
./docker/deploy.sh youruser@192.168.1.10
```

Mac側はTTSだけ動かします。

```bash
./run.sh --tts-only
```

TTSサーバーは `0.0.0.0` でlistenするので、そのままLANから届きます。

Dockerを使わず、別のMacでWebUIだけ動かす場合も同じです。

```bash
./run.sh --webui-only    # .env に TTS の接続先を書いておくこと
```

`--webui-only` は接続先が未設定なら起動を止めます（`127.0.0.1` を見にいく事故を防ぐため）。
TTS本体の取得（1.3GB）も行いません。

### 分離時に効いてくる違い

- **参照音声はTTSサーバー側に保存されます。** WebUIはアップロードされた音声を
  `POST /v1/audio/voices` で全ての参照用エンドポイントへ配ります。TTSサーバーが複数ある
  場合は全台に配られます（片方にしか無いと、その台に当たった文で合成が失敗するため）。
- **voice_id はASCII英数字・アンダースコア・ハイフンだけです。** TTSサーバー側の制約です。
  日本語のファイル名でアップロードすると `voice_1a2b3c` のようなIDが自動生成されます。
  表示名は別に持っているので、「名前変更」で日本語の名前を付けられます。
- **音声変換は ffmpeg で行います。** macOSでは `afconvert`、Linuxコンテナでは `ffmpeg` を
  自動で使い分けます（イメージに同梱済み）。
- 合成された音声はHTTPでWebUI側に届き、WebUI側の `audio_cache/` に置かれます。
  ブラウザはそこから再生するので、ここは分離しても変わりません。

## つまずきやすい点

**音がまったく鳴らない**
ブラウザの自動再生ブロックです。話題を送信するボタンを一度押せば解除されます。

**`num_steps` を上げたら音が途切れる**
RTFが1.0を超えています。`bench_tts.py` で実測して、1.0を十分下回る値に戻してください。

**TTSが極端に遅い、たまに数十秒かかる**
同じMacでLLMも動いていないか確認してください。`sysctl vm.swapusage` でスワップ使用量を見て、
数GB使われているならメモリ不足です。

**検索結果が話題と無関係**
LLMが検索クエリではなく説明文を返しています。句点を含むものや40文字超は自動で弾いて
いますが、モデルによっては指示に従いにくいことがあります。別のモデルを試してください。

**声が参照音声に似ない**
参照音声が長すぎるか、雑音・BGMが入っている可能性があります。10〜15秒の
クリーンな部分だけを切り出してアップロードし直してください。
