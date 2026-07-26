# nicheLLM Proxy

nicheLLM Proxyは、OpenAI互換クライアントと上流LLMプロバイダーの間で、HTTPリクエストとレスポンスを変換せずに中継します。v0.3では、信頼できるネットワーク内での単一listener運用を対象とします。

[English README](README.md)

## v0.3で対応するHTTPトランスポート

- `GET`、`POST`、`PUT`、`PATCH`、`DELETE`、`HEAD`、`OPTIONS`をパススルーで転送し、リクエストのメソッド、パス、クエリ、本文を保持します。
- JSON、`text/event-stream`（SSE）、multipart upload、バイナリのリクエスト／レスポンスを、単一の生HTTPパイプラインで中継します。エンドポイント固有の本文やSSEイベントを解析・再構成しません。
- HTTPエラーとRange requestの`206 Partial Content`を含む上流ステータス、およびエンドツーエンドresponse headerを透過します。同名のエンドツーエンドheaderも保持します。
- クライアントが送った`Authorization` headerは、設定した上流Bearer API keyに置き換えます。`Host`、hop-by-hop header、受信した`Authorization`以外のエンドツーエンドrequest headerは転送します。
- プロキシの生存状態を確認する`GET /health`と、上流への接続・読取失敗時の安全なプロキシ生成5xx応答。
- プロキシ自身が生成するメッセージの英語（既定）／日本語表示。

次のOpenAI API群は、文書化された代表的なHTTPトランスポートの対象範囲です。これはトランスポートの挙動だけを示すもので、モデル、パラメーター、機能の意味的な受理は上流プロバイダーの責任です。

|API群|代表パス|通信形態|
|---|---|---|
|Chat Completions|`/v1/chat/completions`と保存済みcompletionのsubresource|JSON、SSE|
|Responses|`/v1/responses`とresponseのsubresource|JSON、HTTP SSE|
|Conversations|`/v1/conversations`とitemのsubresource|JSON、ページネーション用クエリ|
|Embeddings、Models、Moderations|`/v1/embeddings`、`/v1/models`、`/v1/moderations`|JSON|
|Images、Audio|生成、編集、音声合成、文字起こし、翻訳のパス|JSON、multipart、SSE、バイナリ|
|Files、Uploads|`/v1/files`、`/v1/uploads`とsubresource|multipart、JSON、バイナリ、Range/206|
|Batches、Fine-tuning Jobs|それぞれのcollectionとoperation subresource|JSON、非同期ポーリング|
|Vector Stores、Containers|それぞれのcollection、file、content subresource|JSON、multipart、バイナリ|

そのほかの廃止予定ではないOpenAI HTTP endpointもワイルドカードルートでは遮断しませんが、個別にトランスポート対象として掲載しません。

## サポートしないこと

- Realtime APIとResponses WebSocket modeを含むWebSocket、WebRTC、SIP通信。HTTP SSEには対応しますが、双方向WebSocketの代替ではありません。
- OpenAI/Anthropicのプロトコル変換、Azure、Geminiその他のプロバイダー固有認証・URL変換を含むプロトコル変換またはプロバイダーアダプター。
- webhook受信・署名検証、Administration API操作、リクエスト変換、ruri mode、ロギング、レート制限、プロキシ自身の認証、TLS終端、複数listener。
- インターネットへの安全な直接公開。

次のAPIはv0.3で新規推奨せず、個別トランスポート対象にもしていません: Assistants（`/v1/assistants`、`/v1/threads`、`/v1/runs`）、Videos API / Sora 2、Reusable Prompts、Evals API / Agent Builder、Legacy Completions、Images Variations。ワイルドカードルートがパスを機械的に転送する場合があっても、対応済み・推奨APIになるわけではありません。

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

プロキシはクライアントが送った`Authorization` headerを、設定した上流Bearer API keyに置き換え、受信した値は上流へ転送しません。同名のエンドツーエンドheaderも保持します。一方、プロバイダー固有のほかの認証情報headerに対する包括的な除去方針は適用しないため、信頼できるネットワーク内だけで運用してください。

## タイムアウトと長時間処理

`connect_seconds`は上流への接続確立を待つ時間を制限します。`read_seconds`は上流から次のバイトを受け取るまでの待機時間を制限するものであり、継続してデータが届くレスポンス全体の所要時間を制限するものではありません。HTTP SSEと通常HTTPレスポンスでは、設定したタイムアウトを維持します。

バックグラウンドresponse、batch、fine-tuning jobでは、1本のプロキシ接続を無期限に保持する代わりに、jobを作成した後にクライアントからstatusをポーリングしてください。RealtimeとResponses WebSocketのworkloadには、双方向通信の別設計が必要であり、v0.3では対応しません。

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

DockerイメージにはAPI keyも設定JSONも含まれません。起動前にホスト上で作成してください。

```bash
cp config.example.json config.json
# config.jsonのupstream.base_urlを設定する。
export UPSTREAM_API_KEY='your-upstream-api-key'
export NICHELLM_LANGUAGE=ja  # 任意。既定は英語。
docker compose up --build -d
curl http://127.0.0.1:8000/health
```

Composeは`config.json`を`/app/config/config.json`へ読み取り専用でマウントし、サービス公開先を`127.0.0.1:8000`に限定します。停止するには次を実行します。

```bash
docker compose down
```

## セキュリティ

- API keyをコミットしないでください。実際の値は環境変数、`.env`、またはDocker secretsだけで管理してください。
- `.env`やローカルの`config.json`をGitへ追加しないでください。
- v0.3にはプロキシ自身の認証・TLS終端がありません。信頼できるネットワーク内だけで運用し、インターネットへ直接公開しないでください。
- 上流APIの意味的な互換性、認可方針、モデルの利用可否、アカウントの利用資格は、HTTPパススルーでは保証しません。特にFine-tuning Jobの利用資格は上流アカウントによって決まります。

## テスト

テストスイートでは疑似上流を使い、外部LLMプロバイダーや実API keyを使用しません。代表的なJSON、Responses SSE、multipart、バイナリ、Range/206、重複header、error、lifecycleのトランスポートcaseを扱います。

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

v0.3ではDocker Hubへイメージを公開しません。コンテナレジストリへの公開、tag、CIによる配布はv1.0で導入予定です。

## 変更履歴

### v0.3.0（2026-07-25）

- JSON、Responses HTTP SSE、multipart、バイナリ、Range/206、重複エンドツーエンドheaderに対する生HTTPパススルーを追加しました。
- 代表的なOpenAI API群の表と、双方向通信、プロトコル変換、deprecatedまたはlegacy APIに関する明確な非対応範囲を追加しました。
- Authorization置換、read timeoutの挙動、HTTPトランスポートと上流の意味的互換性の境界を明確化しました。

### v0.2.0（2026-07-24）

- `gettext`による英語（既定）／日本語のプロキシ生成メッセージを追加しました。
- 英語正本のREADMEと、内容が等価な日本語READMEを追加しました。
- Docker Composeでの実行と、プロキシ生成エラーの日本語応答を確認しました。
