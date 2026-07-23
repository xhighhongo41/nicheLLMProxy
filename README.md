# nicheLLM Proxy

OpenAI互換APIを利用するクライアントと上流LLMプロバイダの間で、HTTPリクエストとレスポンスを変換せずに中継するプロキシです。v0.1では、信頼できるネットワーク内での単一listener運用を対象にしています。

## v0.1でサポートすること

- GET、POST、PUT、PATCH、DELETE、HEAD、OPTIONSのHTTPメソッドと、任意のパス・クエリ・本文を上流のOpenAI互換APIへパススルー
- 通常のHTTP応答およびSSE（`text/event-stream`）応答のストリーミング中継
- 上流のHTTPエラーの透過、中継不能な接続・読取エラーの安全な5xx応答
- `GET /health` のヘルスチェック

## v0.1でサポートしないこと

- OpenAI/Anthropic間などのプロトコル変換、リクエスト改変、ruri mode
- ロギング、レート制限、プロキシ自身の認証、TLS終端、複数listener
- インターネットへの安全な直接公開

上流APIの個別エンドポイント・機能の意味的な互換性は、上流プロバイダに依存します。

## 設定

設定JSONにはAPIキーの値を書かず、**環境変数名だけ**を指定します。次は `config.json` の例です。

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
|`UPSTREAM_API_KEY`|はい|上流プロバイダへ送るAPIキー。設定の `api_key_env` と同じ名前にする。|
|`NICHELLM_CONFIG_PATH`|いいえ|設定JSONのパス。既定値は `/app/config/config.json`。ホスト実行時は設定ファイルのパスを必ず指定する。|

クライアントが送る `Authorization` ヘッダーは、上記の上流APIキーで置き換えられます。

## ローカルでの起動（uv）

[uv](https://docs.astral.sh/uv/) を使うと、プロジェクト内の仮想環境で実行できます。グローバルなPythonパッケージのインストールは不要です。

```bash
cp config.example.json config.json
# config.json の upstream.base_url を利用する上流APIに合わせて編集する
export UPSTREAM_API_KEY='上流APIキー'
export NICHELLM_CONFIG_PATH="$PWD/config.json"
uv sync --dev
uv run niche-llm-proxy
```

別のターミナルから、起動を確認できます。

```bash
curl http://127.0.0.1:8000/health
```

## Docker Composeでの起動

DockerイメージにはAPIキーも設定JSONも含めません。実行するホストに設定ファイルを用意してから起動してください。

```bash
cp config.example.json config.json
# config.json の upstream.base_url を編集する
export UPSTREAM_API_KEY='上流APIキー'
docker compose up --build -d
curl http://127.0.0.1:8000/health
```

Composeは `config.json` をコンテナ内の `/app/config/config.json` へ**読み取り専用**でマウントし、公開ポートを `127.0.0.1:8000` に限定します。停止は次のとおりです。

```bash
docker compose down
```

## セキュリティ

- APIキーを `config.json`、Dockerfile、Composeファイル、リポジトリに書き込まないでください。環境変数またはDocker secretsで渡してください。
- `.env` とローカルの `config.json` はGitに追加しないでください。
- v0.1にはプロキシ自身の認証・TLS終端がありません。外部ネットワークへ公開せず、信頼できるネットワーク内だけで利用してください。

## テスト

テストは外部LLMプロバイダや実APIキーを使用しません。

```bash
uv sync --dev
uv run pytest
```

## Docker Hub

v0.1ではDocker Hubへイメージを公開しません。Docker Hubでの公開、タグ運用、CIによる配布はv1.0で導入予定です。
