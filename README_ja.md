# nicheLLM Proxy

nicheLLM Proxyは、OpenAI互換クライアントと上流LLMプロバイダーの間でHTTPリクエストとレスポンスを中継します。`passthrough`モードは変換せずに転送し、v1.1で追加された`grok-image`モードはGrok(xAI)の画像生成をOpenAI互換インターフェースとして提供し、v1.2で追加された`gemini-image`モードはGoogle Geminiの画像生成をOpenAI互換インターフェースとして提供します。いずれのモードでも、信頼できるネットワーク内での単一listener運用と、opt-inの構造化プロトコルログを利用できます。

[English README](README.md)

## インストール

### 動作要件

- Dockerと`docker compose`プラグイン(推奨)、または
- ホスト実行にはPython 3.11以降と[uv](https://docs.astral.sh/uv/)。
- リポジトリのcloneと更新にはGit。

どのセットアップでも、上流LLMプロバイダーのアカウントとAPIキーが必要です。設定JSONファイルについては[設定](#設定)を参照してください。

### Docker Composeによる起動(ソースから)

DockerイメージにはAPIキーも設定JSONも含まれません。起動前にホスト上で作成してください。

```bash
git clone https://github.com/xhighhongo41/nicheLLMProxy.git
cd nicheLLMProxy
cp config.example.json config.json
# OpenAI以外の上流を使う場合は、config.jsonのupstream.base_urlを変更する。
export UPSTREAM_API_KEY='your-upstream-api-key'
export NICHELLM_LANGUAGE=ja  # 任意。既定は英語。
docker compose up --build -d
```

同梱の`docker-compose.yml`は、`api_key_env`が別の変数(例: `XAI_API_KEY`)を指す場合も`UPSTREAM_API_KEY`の設定を要求します。`UPSTREAM_API_KEY`に何らかの値を設定するか、`environment`の内容を自分の設定に合わせてください。Composeファイルには`GET /health`を30秒間隔で確認するhealthcheckが含まれ、準備が整うと`docker compose ps`で`healthy`と表示されます。プロキシを確認します。

```bash
curl http://127.0.0.1:8000/health
```

Composeは`config.json`を`/app/config/config.json`へ読み取り専用でマウントし、サービス公開先を`127.0.0.1:8000`に限定します。プロキシ自身に認証がないため、この公開範囲は信頼できるネットワークに限ってください。ネットワーク内の他のマシンから接続するには、`ports`マッピングのホスト側バインドを変更します(例: `"8000:8000"`)。この変更も信頼できるネットワークでのみ行ってください。停止するには次を実行します。

```bash
docker compose down
```

`nichellm-proxy-logs`名前付きボリュームは、コンテナを再作成しても`/var/log/nichellm`を保持します。共有ターミナルに本文を出さないよう注意して、現在のファイルを確認してください。

```bash
docker compose logs -f nichellm-proxy
docker compose exec nichellm-proxy sh -c 'ls -lh /var/log/nichellm'
```

保持済みログを削除する場合は、サービスを停止してから`docker compose down -v`で名前付きボリュームを削除してください。`docker compose down`だけでは削除されません。

### 公開Docker Hubイメージによる起動

公開済みのmulti-platform(`linux/amd64`、`linux/arm64`)イメージは`xhighhongo41/nichellm-proxy`で入手できます。本番では正確なバージョンタグを利用してください。`1.2`や`latest`のようなローリングタグも存在します。

```bash
docker pull xhighhongo41/nichellm-proxy:1.2.1
```

イメージにはAPIキーも設定JSONも含まれません。Composeファイルと同じ場所に、設定で使うAPIキー変数を記した`.env`ファイルを作成し([APIキー管理](#apiキー管理)を参照)、作業ディレクトリに`config.json`([設定](#設定)の完全な例から始めてください)と次のComposeファイルを配置します。

```yaml
services:
  nichellm-proxy:
    image: xhighhongo41/nichellm-proxy:1.2.1
    ports:
      - "127.0.0.1:8000:8000"
    environment:
      NICHELLM_CONFIG_PATH: /app/config/config.json
      NICHELLM_LANGUAGE: ${NICHELLM_LANGUAGE:-en}
      UPSTREAM_API_KEY: ${UPSTREAM_API_KEY:-}
      XAI_API_KEY: ${XAI_API_KEY:-}
      GEMINI_API_KEY: ${GEMINI_API_KEY:-}
    volumes:
      - ./config.json:/app/config/config.json:ro
      - nichellm-proxy-logs:/var/log/nichellm
    restart: unless-stopped

volumes:
  nichellm-proxy-logs:
```

次に起動します。

```bash
docker compose up -d
curl http://127.0.0.1:8000/health
```

この最小構成の例では、リポジトリ同梱の`docker-compose.yml`に含まれるhealthcheckを省いています。コンテナ起動直後は上の`curl`が失敗することがあります。その場合は数秒待って再試行してください。上記のAPIキー変数は任意参照です。プロキシは起動時に、`config.json`の`api_key_env`が指す変数が実際に設定されているかを検証します。名前付きログボリュームの挙動はソースからのCompose構成と同じで、`docker compose down -v`での削除も同様です。

### uvによるローカル起動

[uv](https://docs.astral.sh/uv/)は、プロジェクトローカルの仮想環境を作成・利用します。

```bash
git clone https://github.com/xhighhongo41/nicheLLMProxy.git
cd nicheLLMProxy
cp config.example.json config.json
# OpenAI以外の上流を使う場合は、config.jsonのupstream.base_urlを変更する。
export UPSTREAM_API_KEY='your-upstream-api-key'
export NICHELLM_CONFIG_PATH="$PWD/config.json"
export NICHELLM_LANGUAGE=ja  # 任意。既定は英語。
uv sync
uv run niche-llm-proxy
```

設定例の`file.path`はDockerボリューム用です。ホスト実行では、`file.enabled`を`false`にするか、`file.path`を実行ユーザーが書き込める絶対パスのディレクトリへ変更してください。

別のターミナルからプロキシを確認できます。

```bash
curl http://127.0.0.1:8000/health
```

ホスト実行では、`127.0.0.1`に限定公開するCompose構成と異なり、全インターフェース(`0.0.0.0`)でリッスンします。共有ネットワークではこの点に注意し、必要に応じてファイアウォールでアクセスを制限してください。フォアグラウンドで動作中のプロキシはCtrl-Cで停止します。

### クライアントからの利用

OpenAI互換クライアントの接続先を、上流プロバイダーではなく`http://127.0.0.1:8000/v1`に向けてください。`curl`の場合:

```bash
curl http://127.0.0.1:8000/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"model": "gpt-4o-mini", "messages": [{"role": "user", "content": "Hello"}]}'
```

OpenAI Pythonライブラリでは、`base_url`を設定し、`api_key`には任意のプレースホルダーを使います:

```python
from openai import OpenAI

client = OpenAI(base_url="http://127.0.0.1:8000/v1", api_key="placeholder")
response = client.chat.completions.create(
    model="gpt-4o-mini",
    messages=[{"role": "user", "content": "Hello"}],
)
print(response.choices[0].message.content)
```

`Authorization`ヘッダーは省略できます。プロキシは常にそれを設定済みの上流Bearer APIキーで置き換えるため([APIキー管理](#apiキー管理)を参照)、クライアント側に本物のキーは不要です。画像モードでも`POST /v1/images/generations`を同じように利用できます。

### 既存環境のアップデート

v1.2.1では設定JSONの形式は変更されていません。既存の`config.json`はそのまま動作します。

ソースからDocker Composeで実行している場合:

```bash
git pull
docker compose up --build -d
```

公開Docker Hubイメージで実行している場合: Composeファイル内のイメージタグを更新し(例: `xhighhongo41/nichellm-proxy:1.2.1`)、次を実行します。

```bash
docker compose pull
docker compose up -d
```

uvでローカル実行している場合: 実行中のプロキシを停止し、次を実行してから再起動してください。

```bash
git pull
uv sync
```

## 設定

### 設定ファイル

プロキシは、必須の設定JSONファイルを1つ読み込みます。3つのテンプレートがリポジトリに含まれます(公開イメージだけで実行する場合も、GitHubから取得してください)。

- `config.example.json` — `passthrough`モード(下記)
- `config.grok-image.example.json` — `grok-image`モード
- `config.gemini-image.example.json` — `gemini-image`モード

コンテナ内での既定パスは`/app/config/config.json`です。上記のDocker Compose手順では、ローカルの`config.json`をそこへマウントします。ホスト実行では`NICHELLM_CONFIG_PATH`でパスを指定してください。

`passthrough`の設定例:

```json
{
  "listener": {
    "port": 8000,
    "mode": "passthrough",
    "features": [
      {
        "name": "logging",
        "config": {
          "stdout": true,
          "file": {
            "enabled": true,
            "path": "/var/log/nichellm/proxy.jsonl",
            "max_bytes": 10485760,
            "backup_count": 5
          },
          "capture": {"bodies": false, "max_body_bytes": 1048576},
          "redaction": {
            "additional_header_names": [],
            "additional_query_parameter_names": [],
            "additional_json_field_names": []
          }
        }
      }
    ]
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

設定例の`file.path`はDockerボリューム用です。ホスト実行では、ファイルログを無効にするか、書き込み可能な絶対パスへ変更してください([uvによるローカル起動](#uvによるローカル起動)を参照)。

共通キー:

|キー|型・制約|既定値|
|---|---|---|
|`listener.port`|1〜65535の整数(必須)|—|
|`listener.mode`|`passthrough`、`grok-image`、または`gemini-image`(必須)|—|
|`upstream.base_url`|query・fragmentを含まないhttp/https URL(必須)|—|
|`upstream.api_key_env`|空でない文字列。APIキーを保持する環境変数の名前(必須)|—|
|`timeouts.connect_seconds`|正の数|10.0|
|`timeouts.read_seconds`|正の数|120.0|

`timeouts`オブジェクト自体も省略でき、既定値のあるキーはすべて省略できます。`listener.port`を変更する場合は、Composeの`ports`マッピングのコンテナ側ポートも`listener.port`と一致するように更新してください(例: `"127.0.0.1:8001:8001"`)。同梱`docker-compose.yml`のhealthcheckはコンテナの8000番ポートを確認する固定値のため、`listener.port`が8000の間だけ正確に機能します。

モード固有のキー(`listener.grok_image`、`listener.gemini_image`)と、任意の`listener.features`のloggingフィーチャーは[モードとフィーチャー](#モードとフィーチャー)で説明します。モード固有オブジェクト内の未知キー、不正な値、モード不一致の設定は、起動時の設定エラーとして拒否されます。モード固有オブジェクトとloggingフィーチャーの設定以外の場所の未知キーは黙って無視されるため、設定が効いていないように見える場合はキーのスペル(例: `timeouts`を`timouts`と書く)を確認してください。`grok-image`と`gemini-image`の完全な例は`config.grok-image.example.json`と`config.gemini-image.example.json`にあります。

### 環境変数

|環境変数|必須|用途|
|---|---|---|
|`UPSTREAM_API_KEY`|はい|上流プロバイダーへ送るAPIキー。`api_key_env`と同じ名前にします。|
|`XAI_API_KEY`|いいえ|`grok-image`モードの設定例がxAIへ送るAPIキー。`api_key_env`と同じ名前にします。|
|`GEMINI_API_KEY`|いいえ|`gemini-image`モードの設定例がGoogleへ送るAPIキー。`api_key_env`と同じ名前にします。|
|`NICHELLM_CONFIG_PATH`|いいえ|設定JSONへのパス。既定値は`/app/config/config.json`です。ホスト実行時は指定してください。|
|`NICHELLM_LANGUAGE`|いいえ|プロキシ自身が生成するメッセージの言語。`en`(既定)または`ja`を指定します。`ja-JP`のような値は`ja`として扱い、未対応値は英語へフォールバックします。|

3つのAPIキー変数は、リポジトリ同梱の設定例で`api_key_env`が参照している名前です。変数名自体は設定可能で、`api_key_env`が指名した変数はプロキシの起動前に設定しておく必要があります。

プロキシが読み込む環境変数はこれだけです。環境変数で設定JSONを丸ごと置換・上書きすることはできません。

### APIキー管理

設定JSONにはAPIキーの値を書かず、値を持つ環境変数の名前(`upstream.api_key_env`)だけを指定し、実際の値は環境に設定してください。

```bash
export UPSTREAM_API_KEY='your-upstream-api-key'
```

Docker Composeでは、Composeファイルと同じ場所に置いた`.env`ファイルが実際の値の置き場所として便利です。Composeが自動的に読み込みます。リポジトリの`.env.example`には、上の表のうちAPIキーと言語の環境変数が並んでいます。

プロキシはクライアントが送った`Authorization`ヘッダーを設定した上流Bearer APIキーに置き換え、受信した値は転送しないため、クライアント側に本物の上流キーは不要です。

### タイムアウト

`connect_seconds`は上流への接続確立を待つ時間を制限します。`read_seconds`は上流から次のバイトを受け取るまでの待機時間を制限するものであり、継続してデータが届くレスポンス全体の所要時間を制限するものではありません。HTTP SSEと通常HTTPレスポンスでは、設定したタイムアウトを維持します。

バックグラウンドレスポンス、batch、fine-tuning jobでは、1本のプロキシ接続を無期限に保持する代わりに、jobを作成した後にクライアントからステータスをポーリングしてください。RealtimeとResponses WebSocketのワークロードには、双方向通信の別設計が必要であり、対応しません。

## モードとフィーチャー

`listener.mode`には`passthrough`、`grok-image`、または`gemini-image`を指定できます。`GET /health`はどのモードでも動作します。上流エラーはステータスと本文をそのまま透過し、上流への接続・読取失敗は`passthrough`と同じ502/504応答を返します。

|モード・フィーチャー|機能|設定|
|---|---|---|
|`passthrough`|OpenAI互換APIを無変換で中継|`upstream`|
|`grok-image`|Grok(xAI)の画像生成をOpenAI互換インターフェースとして提供|`listener.grok_image`|
|`gemini-image`|Google Geminiの画像生成をOpenAI互換インターフェースとして提供|`listener.gemini_image`|
|`logging`フィーチャー|構造化プロトコルログ。全モードで利用可能|`listener.features`|

### passthroughモード

`passthrough`は、リクエストとレスポンスを変換せずに転送します。

- `GET`、`POST`、`PUT`、`PATCH`、`DELETE`、`HEAD`、`OPTIONS`をパススルーで転送し、リクエストのメソッド、パス、クエリ、本文を保持します。
- JSON、`text/event-stream`(SSE)、multipartアップロード、バイナリのリクエスト／レスポンスを、単一の生HTTPパイプラインで中継します。エンドポイント固有の本文やSSEイベントを解析・再構成しません。
- HTTPエラーとRangeリクエストの`206 Partial Content`を含む上流ステータス、およびエンドツーエンドのレスポンスヘッダーを透過します。重複する同名のエンドツーエンドヘッダー(複数の`Set-Cookie`など)も保持します。
- クライアントの`Authorization`ヘッダーは[APIキー管理](#apiキー管理)のとおり置き換えます。`Host`、hop-by-hopヘッダー、受信した`Authorization`以外のエンドツーエンドのリクエストヘッダーは転送します。
- プロキシの生存状態を確認する`GET /health`と、上流への接続・読取失敗時の安全なプロキシ生成5xx応答。
- プロキシ自身が生成するメッセージの英語(既定)／日本語表示。
- リクエスト・上流レスポンス・完了・失敗・キャンセルのJSON Linesプロトコルログ。標準出力(`docker compose logs -f`で追跡)と、任意のローテーションファイルへ出力できます。

#### トランスポート検証済みAPI群

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

HTTPパススルーでは、上流APIの意味的な互換性、認可方針、モデルの利用可否、アカウントの利用資格は保証しません。特にFine-tuning Jobの利用資格は上流アカウントによって決まります。

Realtime APIとResponses WebSocket modeを含むWebSocket、WebRTC、SIP通信には対応しません。HTTP SSEには対応しますが、双方向WebSocketの代替ではありません。

一部の非推奨またはレガシーなOpenAI endpoint(Assistants (`/v1/assistants`)やLegacy Completionsなど)は個別のトランスポート検証対象ではありません。ワイルドカードルートがそのようなパスを機械的に転送する場合があっても、対応済み・推奨APIになるわけではありません。

### grok-imageモード

`grok-image`は、Grok(xAI)の画像生成をOpenAI互換インターフェースとして提供します。

|経路|メソッド|動作|
|---|---|---|
|`/v1/images/generations`|POST|OpenAI ImagesリクエストをxAI画像生成APIへ変換して転送し、応答をOpenAI互換に整形|
|`/v1/models`|GET|上流へ無変換で転送|
|`/v1/image-generation-models`|GET|上流へ無変換で転送|
|上記以外の経路|任意|ローカライズ済みエラーを伴うHTTP 404|
|上記経路の未対応メソッド|—|ローカライズ済みエラーを伴うHTTP 405|

`grok-image`モードでは、プロキシはOpenAI専用パラメータの`size`、`quality`、`style`、`seed`、`background`、`moderation`、`output_format`、`output_compression`を除去し、リクエストに`response_format`がなければ`b64_json`を付与し(明示的な`b64_json`と`url`はそのまま透過し、それ以外の値はHTTP 400で拒否)、`n`が1から10の整数であることを検証し、その他不正リクエストは上流へ送る前にHTTP 400で拒否し、`storage_options`などそれ以外のキーはそのまま透過します。`logging`フィーチャーはこのモードでも`passthrough`と同様に機能します。

`listener.grok_image`は任意で、`grok-image`モードでのみ指定できます。リクエストでの直接指定が優先される既定値を持ちます。

- `default_model`: リクエストに`model`がない場合に使う既定モデル。リクエスト・設定の双方にない場合はHTTP 400を返します。
- `aspect_ratio`: リクエストに`aspect_ratio`がない場合に付与します(例: `1:1`、`16:9`)。
- `resolution`: リクエストに`resolution`がない場合に付与します。`1k`または`2k`です。

完全な`grok-image`設定例:

```json
{
  "listener": {
    "port": 8000,
    "mode": "grok-image",
    "grok_image": {
      "default_model": "grok-imagine-image-2.0",
      "aspect_ratio": "1:1",
      "resolution": "1k"
    },
    "features": [
      {
        "name": "logging",
        "config": {
          "stdout": true,
          "file": {
            "enabled": true,
            "path": "/var/log/nichellm/proxy.jsonl",
            "max_bytes": 10485760,
            "backup_count": 5
          },
          "capture": {"bodies": false, "max_body_bytes": 1048576},
          "redaction": {
            "additional_header_names": [],
            "additional_query_parameter_names": [],
            "additional_json_field_names": []
          }
        }
      }
    ]
  },
  "upstream": {
    "base_url": "https://api.x.ai",
    "api_key_env": "XAI_API_KEY"
  },
  "timeouts": {
    "connect_seconds": 10,
    "read_seconds": 120
  }
}
```

### gemini-imageモード

`gemini-image`は、Gemini APIのOpenAI互換層を使って、Google Geminiの画像生成をOpenAI互換インターフェースとして提供します。

|経路|メソッド|動作|
|---|---|---|
|`/v1/images/generations`|POST|`/v1beta/openai/images/generations`へ転送し、応答をOpenAI互換に整形|
|`/v1/models`|GET|`/v1beta/openai/models`へ無変換で転送|
|上記以外の経路|任意|ローカライズ済みエラーを伴うHTTP 404|
|上記経路の未対応メソッド|—|ローカライズ済みエラーを伴うHTTP 405|

`gemini-image`モードでは、プロキシはリクエストに`response_format`がなければ`b64_json`を付与し、`n`が1から10の整数であることを検証し、`response_format`が`b64_json`以外の場合やその他不正リクエストは上流へ送る前にHTTP 400で拒否し、`size`や`quality`などそれ以外のキーはそのまま透過し、リクエストに`size`と`aspect_ratio`の両方がない場合にのみ設定した`aspect_ratio`既定値を付与します。画像データは常にbase64エンコードされたJPEGで返ります。`logging`フィーチャーはこのモードでも`passthrough`と同様に機能します。

OpenAI互換層での画像生成に使えるモデルは、Googleによるホワイトリストに制限されます。2026-09-09時点で動作を確認済みなのは`gemini-3-pro-image-preview`のみで、`gemini-2.5-flash-image`は公式文書に記載がありますが2026-10-02に提供終了予定です。GA名の`gemini-3-pro-image`と`gemini-3.1-flash-image`は現在この層経由ではHTTP 404となり利用できません。

`listener.gemini_image`は任意で、`gemini-image`モードでのみ指定できます。リクエストでの直接指定が優先される既定値を持ちます。

- `default_model`: リクエストに`model`がない場合に使う既定モデル。リクエスト・設定の双方にない場合はHTTP 400を返します。
- `aspect_ratio`: リクエストに`size`と`aspect_ratio`の両方がない場合に付与します(例: `1:1`、`16:9`)。

完全な`gemini-image`設定例:

```json
{
  "listener": {
    "port": 8000,
    "mode": "gemini-image",
    "gemini_image": {
      "default_model": "gemini-3-pro-image-preview",
      "aspect_ratio": "1:1"
    },
    "features": [
      {
        "name": "logging",
        "config": {
          "stdout": true,
          "file": {
            "enabled": true,
            "path": "/var/log/nichellm/proxy.jsonl",
            "max_bytes": 10485760,
            "backup_count": 5
          },
          "capture": {"bodies": false, "max_body_bytes": 1048576},
          "redaction": {
            "additional_header_names": [],
            "additional_query_parameter_names": [],
            "additional_json_field_names": []
          }
        }
      }
    ]
  },
  "upstream": {
    "base_url": "https://generativelanguage.googleapis.com",
    "api_key_env": "GEMINI_API_KEY"
  },
  "timeouts": {
    "connect_seconds": 10,
    "read_seconds": 120
  }
}
```

### loggingフィーチャー

`listener.features`を省略すればプロトコルログなしで動作します。指定する場合は`logging`を1件だけ指定できます。プロキシは重複・未知feature・不正なlogging設定を起動時に拒否します。

- `stdout: true`(既定)はUTF-8 JSON Linesを標準出力へ出します。コンテナでは`docker compose logs -f nichellm-proxy`で追跡できます。
- `file.enabled: true`は同一レコードを`file.path`へ出します。このパスは絶対パスである必要があります。`max_bytes`と`backup_count`はともに正の値が必要です。例の既定値では10 MiBの現行ファイルと5世代を保持するため、概算60 MiBです。
- `capture.bodies`の既定値は**false**です。trueの場合だけ、JSON・テキスト・SSEのリクエスト・レスポンス本文を`max_body_bytes`(既定1 MiB、最大10 MiB)まで記録します。中継ストリームを待機・再構築しません。
- multipartとバイナリ本文は保存しません。バイト数、SHA-256 digest、省略理由だけを記録します。テキスト本文が上限で切れた場合はレコードに明記します。
- `Authorization`、プロキシ自身の認証情報、Cookie、APIキーヘッダー、`token`、`secret`、`password`、`api_key`を含む名前の値はマスクします。プロジェクト固有の名前は3つの`redaction`配列へ追加してください。自由文形式のプロンプトやツール出力に埋め込まれた秘密情報・個人情報をJSON redactionで確実に検出することはできません。

本文captureを有効にすると、ユーザープロンプトとモデル出力を意図的に保存します。信頼できる環境だけで有効にし、ログボリュームへのアクセス制御と保持・削除方針を定めてください。

### サポートしないこと

上記の中継動作に加えて、プロキシは`grok-image`と`gemini-image`の変換以外のプロトコル変換・プロバイダーアダプターを提供しません。また、webhook受信・署名検証、Administration API操作、ruri mode、レート制限、プロキシ自身の認証、TLS終端、複数listenerも提供しません。

## セキュリティ

- nicheLLM Proxyにはプロキシ自身の認証・TLS終端がありません。プロキシに到達できる者は誰でも、設定済みの上流APIキーとその利用枠をプロキシ経由で使えます。信頼できるネットワーク内だけで運用してください。
- インターネットへ直接公開しないでください。ネットワーク越しに使う場合は、利用者自身が手前に置くリバースプロキシでTLS終端と認証を行うことを想定しています。ホスト実行(uv)は全インターフェース(`0.0.0.0`)でリッスンしますが、同梱のCompose構成は`127.0.0.1:8000`のみを公開します。
- プロトコルログは機密データとして扱ってください。本文captureはopt-inですが、プロンプト、モデル出力、個人情報を含み得ます。ログボリュームへのアクセスを制限し、保持・削除方針を定めてください。

## トラブルシューティング

起動時の代表的なエラーと確認ポイント:

- `設定ファイルが見つかりません: {path}`(英語: `Configuration file was not found: {path}`) — そのパスに設定JSONがありません。ホスト実行では`NICHELLM_CONFIG_PATH`を、Dockerでは`config.json`のマウントを確認してください。
- `上流APIキーの環境変数 '{api_key_env}' が設定されていません。`(英語: `Upstream API key environment variable '{api_key_env}' is not set.`) — `api_key_env`が指す変数が設定されていません。シェルでexportするか、Composeファイルと同じ場所の`.env`ファイルに設定してください。
- 設定が効いていないように見える — モード固有オブジェクトとloggingフィーチャーの設定以外の場所の未知キーは黙って無視されます。キーのスペル(例: `timeouts`を`timouts`と書く)を確認してください。

プロキシ自身が出すエラーメッセージは`NICHELLM_LANGUAGE`に応じてローカライズされます(既定は英語、`ja`で日本語)。

## 変更履歴

### v1.2.1（2026-09-09）

- READMEを一般ユーザー向けに再構成しました。インストール（ソースからのDocker Compose、公開Docker Hubイメージ、uvによるローカル実行）、設定、モードとフィーチャー、セキュリティ、この変更履歴の順に構成しました。
- 動作要件の概要、クライアントからの利用例、トラブルシューティング節、既存環境のアップデート手順、公開Docker Hubイメージ用のCompose例を追加しました。
- 開発者向けの内容（テスト手順、翻訳カタログの保守、maintainer向けイメージ公開手順）を削除しました。

### v1.2.0（2026-09-09）

- Gemini APIのOpenAI互換層を使って、Google Geminiの画像生成をOpenAI互換インターフェースとして提供する`gemini-image` listenerモードを追加しました。`POST /v1/images/generations`は`/v1beta/openai/images/generations`へ転送して応答をOpenAI互換に整形し、`GET /v1/models`は`/v1beta/openai/models`へ無変換で転送します。
- `default_model`と`aspect_ratio`の既定値を指定する任意の`listener.gemini_image`設定を追加しました。
- プロトコルログの`logging` featureを`gemini-image`モードにも対応させ、Docker Composeのenvironmentに`XAI_API_KEY`と`GEMINI_API_KEY`の受け渡しを追加しました。

### v1.1.0（2026-09-08）

- Grok(xAI)の画像生成をOpenAI互換インターフェースとして提供する`grok-image` listenerモードを追加しました。`POST /v1/images/generations`はOpenAI ImagesからxAI画像生成APIへ変換して転送し、`GET /v1/models`と`GET /v1/image-generation-models`は無変換で転送します。
- `default_model`、`aspect_ratio`、`resolution`の既定値を指定する任意の`listener.grok_image`設定を追加しました。
- プロトコルログの`logging` featureを`grok-image`モードにも対応させました。

### v1.0.0（2026-07-31）

- stdout JSON Lines、file出力、サイズベースrotation、上限付き本文capture、credential redactionを備えたopt-in logging featureを追加しました。
- 永続Docker Compose log volumeと、test・image build・Docker Hub multi-platform公開用のGitHub Actions workflowを追加しました。

### v0.3.0（2026-07-25）

- JSON、Responses HTTP SSE、multipart、バイナリ、Range/206、重複エンドツーエンドheaderに対する生HTTPパススルーを追加しました。
- 代表的なOpenAI API群の表と、双方向通信、プロトコル変換、deprecatedまたはlegacy APIに関する明確な非対応範囲を追加しました。
- Authorization置換、read timeoutの挙動、HTTPトランスポートと上流の意味的互換性の境界を明確化しました。

### v0.2.0（2026-07-24）

- `gettext`による英語（既定）／日本語のプロキシ生成メッセージを追加しました。
- 英語正本のREADMEと、内容が等価な日本語READMEを追加しました。
- Docker Composeでの実行と、プロキシ生成エラーの日本語応答を確認しました。

## ライセンス

このプロジェクトはMIT Licenseの下でライセンスされます。詳細は[LICENSE](LICENSE)を参照してください。