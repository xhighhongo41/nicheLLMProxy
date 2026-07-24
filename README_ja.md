# nicheLLM Proxy

nicheLLM Proxyは、OpenAI互換クライアントと上流LLMプロバイダーの間で、HTTPリクエストとレスポンスを変換せずに中継します。v0.2では、信頼できるネットワーク内での単一listener運用を対象とします。

[English README](README.md)

## v0.2でサポートすること

- `GET`、`POST`、`PUT`、`PATCH`、`DELETE`、`HEAD`、`OPTIONS`をパススルーで転送し、リクエストのパス、クエリ、本文を保持します。
- 通常のHTTP応答と、`text/event-stream`（SSE）のストリーミング応答を、バッファリングや再構成をせずに中継します。
- 上流HTTPエラーのステータスと本文を透過し、上流への接続・読取失敗時には安全な5xx応答を返します。
- プロキシの生存状態を確認する`GET /health`。
- プロキシ自身が生成するメッセージの英語（既定）／日本語表示。

## サポートしないこと

- プロトコル変換、リクエスト変換、ruri mode、ロギング、レート制限、プロキシ自身の認証、TLS終端、複数listener。
- インターネットへの安全な直接公開。

上流の個別エンドポイントと機能の意味的な互換性は、上流プロバイダーに依存します。

## 設定

設定JSONにはAPIキーの値を書かず、値を持つ環境変数名だけを指定してください。

```json
{
  "listener": {
    "port": 8000,
    "mode": "passthrough"
  },
  "upstream": {
    "base_url": "https://api.openai.com",
    "api_key_env": "UPSTREAM_API_KEY"
  },
  "timeouts": {
    "connect_seconds": 10,
    "read_seconds": 120
  }
}
```

|環境変数|必須|用途|
|---|---|---|
|`UPSTREAM_API_KEY`|はい|上流プロバイダーへ送るAPIキー。`api_key_env`と同じ名前にします。|
|`NICHELLM_CONFIG_PATH`|いいえ|設定JSONへのパス。既定値は`/app/config/config.json`です。ホスト実行時は指定してください。|
|`NICHELLM_LANGUAGE`|いいえ|プロキシ自身が生成するメッセージの言語。`en`（既定）または`ja`を指定します。`ja-JP`のような値は`ja`として扱い、未対応値は英語へフォールバックします。|

プロキシはクライアントが送った`Authorization`ヘッダーを、設定した上流APIキーに置き換えます。上流が返す本文、SSEイベント、ヘッダー、ステータスは翻訳・変更しません。

## uvによるローカル起動

[uv](https://docs.astral.sh/uv/)は、プロジェクトローカルの仮想環境を作成・利用します。

```bash
cp config.example.json config.json
# config.jsonのupstream.base_urlを利用する上流に合わせて設定する。
export UPSTREAM_API_KEY='your-upstream-api-key'
export NICHELLM_CONFIG_PATH="$PWD/config.json"
export NICHELLM_LANGUAGE=ja  # 任意。既定は英語。
uv sync --dev
uv run niche-llm-proxy
```

別のターミナルからプロキシを確認できます。

```bash
curl http://127.0.0.1:8000/health
```

## Docker Composeによる起動

DockerイメージにはAPIキーも設定JSONも含まれません。起動前にホスト上で作成してください。

```bash
cp config.example.json config.json
# config.jsonのupstream.base_urlを設定する。
export UPSTREAM_API_KEY='your-upstream-api-key'
export NICHELLM_LANGUAGE=ja  # 任意。既定は英語。
docker compose up --build -d
curl http://127.0.0.1:8000/health
```

Composeは`config.json`を`/app/config/config.json`へ読み取り専用でマウントし、サービス公開先を`127.0.0.1:8000`に限定します。DockerfileのビルドとDocker Composeによる起動は、Dockerが利用可能な環境で検証済みです。停止するには次を実行します。

```bash
docker compose down
```

## セキュリティ

- APIキーをコミットしないでください。実際の値は環境変数、`.env`、またはDocker secretsだけで管理してください。
- `.env`やローカルの`config.json`をGitへ追加しないでください。
- v0.2にはプロキシ自身の認証・TLS終端がありません。信頼できるネットワーク内だけで運用し、インターネットへ直接公開しないでください。

## テスト

テストでは外部LLMプロバイダーや実APIキーを使用しません。

```bash
uv sync --dev
uv run pytest
```

## 翻訳カタログ

実行時にはPython標準の`gettext`を使用します。英語のメッセージIDをフォールバックとし、日本語カタログは`src/niche_llm_proxy/locales/ja/LC_MESSAGES/`に置きます。`.po`を編集した後は、GNU gettextでGit管理対象の`.mo`カタログを再生成してください。

```bash
msgfmt --check \
  --output-file src/niche_llm_proxy/locales/ja/LC_MESSAGES/niche_llm_proxy.mo \
  src/niche_llm_proxy/locales/ja/LC_MESSAGES/niche_llm_proxy.po
```

## Docker Hub

v0.2ではDocker Hubへイメージを公開しません。コンテナレジストリへの公開、タグ、CIによる配布はv1.0で導入予定です。

## 変更履歴

### v0.2.0（2026-07-24）

- `gettext`による英語（既定）／日本語のプロキシ生成メッセージを追加しました。
- 英語正本のREADMEと、内容が等価な日本語READMEを追加しました。
- Docker Composeでの実行と、プロキシ生成エラーの日本語応答を確認しました。
