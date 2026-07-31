<!--
  README はリポジトリの顔なので、ここはあなたの言葉で書いてください。
  骨組みとリンクだけ置いてあります。<!-- --> のコメントは書き終えたら消してOK。
  詳しい説明は docs/ にあるので、ここには書かなくて大丈夫です。
-->

# Irodori AutoSpeak

<!-- 1〜3行。これが何をするツールなのかを、初めて見た人に向けて。
     例）話題を投げると、自分で調べながら延々と喋り続けてくれるツール。 -->


## できること

<!-- 箇条書きで3〜6個。作った動機や「ここが気に入っている」を混ぜると読ませる文になります。 -->

-
-
-


## デモ

<!-- 任意。スクリーンショットや音声サンプルがあると一番伝わります。
     画像は docs/images/ などに置いて ![説明](docs/images/xxx.png) で貼れます。
     ※ 参照音声そのものはコミットしないこと（voices/ は .gitignore 済み） -->


## 必要なもの

- macOS (Apple Silicon) — 音声合成に MPS を使います
- [SearXNG](https://github.com/searxng/searxng) — Web検索
- [LM Studio](https://lmstudio.ai) — 文章生成
- [Irodori-TTS-Server](https://github.com/Aratako/Irodori-TTS-Server) — 音声合成（初回起動時に自動で取得）

## 使い方

```bash
git clone https://github.com/Septe-l/irodori-autospeak.git
cd irodori-autospeak
./run.sh
```

http://localhost:8000 を開いて、話題を入力してください。

初回はモデルの取得で数分かかります。詳しい手順は **[docs/SETUP.md](docs/SETUP.md)**。

## ドキュメント

| | |
|---|---|
| [仕組みと設定](docs/ARCHITECTURE.md) | 内部構成、設定項目、合成速度の実測値 |
| [環境構築](docs/SETUP.md) | ゼロから動かすまでの手順 |
| [Docker](docs/DOCKER.md) | WebUIを別マシンのコンテナで動かす |

## 参照音声について

このツールは参照音声から声を複製します。**自分が権利を持っている音声か、本人から許諾を
得た音声だけを使ってください。** 他人の声を無断で複製して公開・配信する行為は、法的にも
倫理的にも問題になりえます。ツール側で用途を判別する手段はないので、利用者の責任になります。

## ライセンス

MIT License（[LICENSE](LICENSE)）

依存する [Irodori-TTS-Server](https://github.com/Aratako/Irodori-TTS-Server) は同梱しておらず、
初回起動時に別リポジトリとして取得します（同じくMIT）。モデルの重みは Hugging Face 側で
別途ライセンスされているので、各モデルカードを確認してください。

## 開発について

このツールの実装とドキュメントの多くは、[Claude](https://claude.ai)（Anthropic）との
共同作業で書かれています。
