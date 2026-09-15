# nicheLLM Proxy

nicheLLM Proxyは、OpenAI互換クライアントと上流LLMプロバイダーの間でHTTPリクエストとレスポンスを中継します。`passthrough`モードは変換せずに転送し、v1.1で追加された`grok-image`モードはGrok(xAI)の画像生成をOpenAI互換インターフェースとして提供し、v1.2で追加された`gemini-image`モードはGoogle Geminiの画像生成をOpenAI互換インターフェースとして提供し、v1.3で追加された`featherless`モードはfeatherless.aiをモデルホワイトリストとAPIキーごとの同時リクエストキューイングで提供します。v1.4からは、1プロセスで複数のリスナーを異なるモードで並行に待ち受けられます。いずれのモードも信頼できるネットワーク内での動作を前提とし、opt-inの構造化プロトコルログに対応します。

[English README](README.md)

## インストール

### 動作要件

- Dockerと`docker compose`プラグイン(推奨)、または
- ホスト実行にはPython 3.11以降と[uv](https://docs.astral.sh/uv/)。
- リポジトリのcloneと更新にはGit。

どのセットアップでも、上流LLMプロバイダーのアカウントとAPIキーが必要です。設定ファイルについては[設定](#設定)を参照してください。`featherless`モードではプロキシ自身はAPIキーを保持せず、各クライアントが自分の`Authorization`ヘッダーを送ります([featherlessモード](#featherlessモード)を参照)。

### Docker Composeによる起動(ソースから)

DockerイメージにはAPIキーも設定ファイルも含まれません。起動前にホスト上で作成してください。

```bash
git clone https://github.com/xhighhongo41/nicheLLMProxy.git
cd nicheLLMProxy
cp config.example.jsonc config.jsonc
# OpenAI以外の上流を使う場合は、config.jsoncのupstream.base_urlを変更する。
export UPSTREAM_API_KEY='your-upstream-api-key'
export NICHELLM_LANGUAGE=ja  # 任意。既定は英語。
docker compose up --build -d
```

同梱の`docker-compose.yml`は、`api_key_env`が別の変数(例: `XAI_API_KEY`)を指す場合も`UPSTREAM_API_KEY`の設定を要求します。`UPSTREAM_API_KEY`に何らかの値を設定するか、`environment`の内容を自分の設定に合わせてください。`featherless`モードではAPIキー変数が一切不要です。`UPSTREAM_API_KEY`に任意の値を設定するか、`environment`の内容を調整してください。Composeファイルには`GET /health`を30秒間隔で確認するhealthcheckが含まれます。healthcheckはマウントされた`config.jsonc`からリスナーポートを読み取り、全リスナーポートを順に確認するため、どのポートの組み合わせでも正確に機能します。準備が整うと`docker compose ps`で`healthy`と表示されます。プロキシを確認します。

```bash
curl http://127.0.0.1:8000/health
```

Composeは`config.jsonc`を`/app/config/config.jsonc`へ読み取り専用でマウントし、サービス公開先を既定で`127.0.0.1:8000`に限定します。複数リスナー構成では、リスナーポートごとに1つの`ports`マッピングを追加してください(例: `"127.0.0.1:8001:8001"`)。プロキシ自身に認証がないため、この公開範囲は信頼できるネットワークに限ってください。ネットワーク内の他のマシンから接続するには、`ports`マッピングのホスト側バインドを変更します(例: `"8000:8000"`)。この変更も信頼できるネットワークでのみ行ってください。停止するには次を実行します。

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

公開済みのmulti-platform(`linux/amd64`、`linux/arm64`)イメージは`xhighhongo41/nichellm-proxy`で入手できます。本番では正確なバージョンタグを利用してください。`1.3`や`latest`のようなローリングタグも存在します。

```bash
docker pull xhighhongo41/nichellm-proxy:1.4.1
```

イメージにはAPIキーも設定ファイルも含まれません。Composeファイルと同じ場所に、設定で使うAPIキー変数を記した`.env`ファイルを作成し([APIキー管理](#apiキー管理)を参照)、作業ディレクトリに`config.jsonc`([設定](#設定)の完全な例から始めてください)と次のComposeファイルを配置します。

```yaml
services:
  nichellm-proxy:
    image: xhighhongo41/nichellm-proxy:1.4.1
    # config.jsoncで定義したリスナーポートごとに1つのマッピングを追加。
    ports:
      - "127.0.0.1:8000:8000"
    environment:
      NICHELLM_CONFIG_PATH: /app/config/config.jsonc
      NICHELLM_LANGUAGE: ${NICHELLM_LANGUAGE:-en}
      UPSTREAM_API_KEY: ${UPSTREAM_API_KEY:-}
      XAI_API_KEY: ${XAI_API_KEY:-}
      GEMINI_API_KEY: ${GEMINI_API_KEY:-}
    volumes:
      - ./config.jsonc:/app/config/config.jsonc:ro
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

この最小構成の例では、リポジトリ同梱の`docker-compose.yml`に含まれるhealthcheckを省いています。コンテナ起動直後は上の`curl`が失敗することがあります。その場合は数秒待って再試行してください。`config.jsonc`で定義したリスナーポートごとに、`ports`マッピングを1つずつ追加してください。上記のAPIキー変数は任意参照です。プロキシは起動時に、各リスナー項目の`upstream.api_key_env`が指す変数が実際に設定されているかを検証します。名前付きログボリュームの挙動はソースからのCompose構成と同じで、`docker compose down -v`での削除も同様です。

### uvによるローカル起動

[uv](https://docs.astral.sh/uv/)は、プロジェクトローカルの仮想環境を作成・利用します。

```bash
git clone https://github.com/xhighhongo41/nicheLLMProxy.git
cd nicheLLMProxy
cp config.example.jsonc config.jsonc
# OpenAI以外の上流を使う場合は、config.jsoncのupstream.base_urlを変更する。
export UPSTREAM_API_KEY='your-upstream-api-key'
export NICHELLM_CONFIG_PATH="$PWD/config.jsonc"
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

`Authorization`ヘッダーは省略できます。プロキシは常にそれを設定済みの上流Bearer APIキーで置き換えるため([APIキー管理](#apiキー管理)を参照)、クライアント側に本物のキーは不要です。`featherless`モードでは逆に、各クライアントが本物のfeatherless.aiキーを`Authorization`で送る必要があり、プロキシはそれを変更せず転送します。画像モードでも`POST /v1/images/generations`を同じように利用できます。

### 既存環境のアップデート

v1.4.0では設定ファイルの形式が変わります。最上位の`listener`、`upstream`、`timeouts`ブロックは必須の最上位`listeners`配列へ置き換えられ、v1.4より前のファイルは起動時にエラー `Since v1.4 the configuration requires a top-level 'listeners' array. See the README for the migration guide.` で拒否されます。移行では、`port`と`mode`を`upstream`・`timeouts`ブロックとともに`listeners`配列の1項目へ移動してください(詳細は[設定](#設定))。手を加えていない既存の`config.json`は既定パスのフォールバックで読み込まれますが、書き換えるまで起動できません。

ソースからDocker Composeで実行している場合:

```bash
git pull
docker compose up --build -d
```

公開Docker Hubイメージで実行している場合: Composeファイル内のイメージタグを更新し(例: `xhighhongo41/nichellm-proxy:1.4.1`)、次を実行します。

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

プロキシは、必須の設定ファイルを1つ読み込みます。5つのテンプレートがリポジトリに含まれます(公開イメージだけで実行する場合も、GitHubから取得してください)。

- `config.example.jsonc` — `passthrough`モード(下記)
- `config.multi-listener.example.jsonc` — 4モード全部をポート8000〜8003で
- `config.grok-image.example.jsonc` — `grok-image`モード
- `config.gemini-image.example.jsonc` — `gemini-image`モード
- `config.featherless.example.jsonc` — `featherless`モード

コンテナ内での既定パスは`/app/config/config.jsonc`を優先し、JSONCファイルが存在しない場合は`/app/config/config.json`へフォールバックします。上記のDocker Compose手順では、ローカルの`config.jsonc`を優先パスへマウントします。ホスト実行では`NICHELLM_CONFIG_PATH`でパスを指定してください。

設定ファイルはJSONCに対応します。`//`の行コメントと`/* */`のブロックコメントは拡張子にかかわらず利用でき、文字列値の中のコメント記号(例: URL内の`//`)は保持されます。設定には必須の最上位`listeners`配列が1つあります。各項目は1つの完全で独立したリスナーで、1つのポートを束ね、独自の`mode`、`upstream`、`timeouts`、任意のモード固有ブロック、任意の`features`を持ちます。1プロセスが全項目を並行に待ち受けます。`config.multi-listener.example.jsonc`は4モード全部をポート8000〜8003で組み合わせた例です。ポートは一意である必要があり、重複は設定エラーとして拒否されます。

`passthrough`の設定例:

```json
{
  "listeners": [
    {
      "port": 8000,
      "mode": "passthrough",
      "upstream": {
        "base_url": "https://api.openai.com",
        "api_key_env": "UPSTREAM_API_KEY"
      },
      "timeouts": {
        "connect_seconds": 10,
        "read_seconds": 120
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
    }
  ]
}
```

設定例の`file.path`はDockerボリューム用です。ホスト実行では、ファイルログを無効にするか、書き込み可能な絶対パスへ変更してください([uvによるローカル起動](#uvによるローカル起動)を参照)。

共通キー:

|キー|型・制約|既定値|
|---|---|---|
|`listeners[].port`|1〜65535の整数(必須)|—|
|`listeners[].mode`|`passthrough`、`grok-image`、`gemini-image`、または`featherless`(必須)|—|
|`listeners[].upstream.base_url`|query・fragmentを含まないhttp/https URL(必須)|—|
|`listeners[].upstream.api_key_env`|空でない文字列。APIキーを保持する環境変数の名前(`featherless`モード以外は必須。`featherless`モードでは指定自体がエラー)|—|
|`listeners[].timeouts.connect_seconds`|正の数|10.0|
|`listeners[].timeouts.read_seconds`|正の数または`null`|120.0|

`timeouts`オブジェクト自体も省略でき、既定値のあるキーはすべて省略できます。複数のリスナーが同じ`upstream`設定を繰り返して、1つの上流プロバイダーを共有できます。同梱`docker-compose.yml`のhealthcheckはマウントされた`config.jsonc`からリスナーポートを読み取り、それぞれを確認するため、どのポートの組み合わせでも正確に機能します。

モード固有のキー(リスナー項目内の`grok_image`、`gemini_image`、`featherless`)と、任意の`features`のloggingフィーチャーは[モードとフィーチャー](#モードとフィーチャー)で説明します。`grok-image`・`gemini-image`・`featherless`の各モードではモード固有キーが必須です。これらのモードのリスナー項目でモード固有キーが欠落している場合、そのリスナーは起動対象から外れ、ポート・モード・欠落キー名を含む起動時警告が標準エラー出力に表示され、他のリスナーは通常どおり起動します。`passthrough`にモード固有キーはありません。全リスナーがこの方法でスキップされた場合、起動は設定エラーで失敗します。モード固有オブジェクト内の未知キーと不正な値は、起動時の設定エラーとして拒否されます。項目の`mode`に属さないモード固有ブロック、最上位の未知キー、`logging.file.enabled`が`false`のときに無視される`logging.file`の設定は、起動時の警告として報告されて無視されます。プロキシは起動を続け、警告は1件ずつ標準エラー出力に表示されるため、設定が効いていないように見える場合はキーのスペル(例: `timeouts`を`timouts`と書く)と起動時の警告を確認してください。各モードの完全な例は`config.grok-image.example.jsonc`、`config.gemini-image.example.jsonc`、`config.featherless.example.jsonc`にあります。

### 環境変数

|環境変数|必須|用途|
|---|---|---|
|`UPSTREAM_API_KEY`|はい|上流プロバイダーへ送るAPIキー。利用するリスナー項目の`api_key_env`と同じ名前にします。|
|`XAI_API_KEY`|いいえ|`grok-image`モードの設定例がxAIへ送るAPIキー。`api_key_env`と同じ名前にします。|
|`GEMINI_API_KEY`|いいえ|`gemini-image`モードの設定例がGoogleへ送るAPIキー。`api_key_env`と同じ名前にします。|
|`NICHELLM_CONFIG_PATH`|いいえ|設定ファイルへのパス。未指定の場合、コンテナでは`/app/config/config.jsonc`を既定とし、JSONCファイルが存在しない場合は`/app/config/config.json`へフォールバックします。ホスト実行時は指定してください。指定した場合はそのパスのみを利用し、フォールバックしません。|
|`NICHELLM_LANGUAGE`|いいえ|プロキシ自身が生成するメッセージの言語。`en`(既定)または`ja`を指定します。`ja-JP`のような値は`ja`として扱い、未対応値は英語へフォールバックします。|

3つのAPIキー変数は、リポジトリ同梱の設定例で`api_key_env`が参照している名前です。変数名自体は設定可能で、`api_key_env`が指名した変数はプロキシの起動前に設定しておく必要があります。各リスナー項目は異なる変数を指名できるため、1プロセスで複数のプロバイダーを扱えます。`featherless`モードはAPIキー環境変数を一切読みません。各クライアントが自分の`Authorization`ヘッダーを送り、プロキシはそれをfeatherless.aiへ変更せず中継します。

プロキシが読み込む環境変数はこれだけです。環境変数で設定ファイルを丸ごと置換・上書きすることはできません。

### APIキー管理

設定ファイルにはAPIキーの値を書かず、値を持つ環境変数の名前(リスナー項目の`upstream.api_key_env`)だけを指定し、実際の値は環境に設定してください。

```bash
export UPSTREAM_API_KEY='your-upstream-api-key'
```

Docker Composeでは、Composeファイルと同じ場所に置いた`.env`ファイルが実際の値の置き場所として便利です。Composeが自動的に読み込みます。リポジトリの`.env.example`には、上の表のうちAPIキーと言語の環境変数が並んでいます。

プロキシはクライアントが送った`Authorization`ヘッダーを設定した上流Bearer APIキーに置き換え、受信した値は転送しないため、クライアント側に本物の上流キーは不要です。`featherless`モードではこの置き換えを行いません。プロキシは上流APIキーを保持せず、`api_key_env`を定義してはならず、各クライアントの`Authorization`ヘッダーを変更せず中継します。このため、すべてのクライアントが自分のfeatherless.ai APIキーを必要とします([featherlessモード](#featherlessモード)を参照)。

### タイムアウト

各リスナー項目は独自の任意`timeouts`ブロックを持ちます。`connect_seconds`は上流への接続確立を待つ時間を制限します。`read_seconds`は上流から次のバイトを受け取るまでの待機時間を制限するものであり、継続してデータが届くレスポンス全体の所要時間を制限するものではありません。HTTP SSEと通常HTTPレスポンスでは、設定したタイムアウトを維持します。`read_seconds`に`null`を指定すると読み取りタイムアウトを無効化し、上流応答を無制限に待ちます。`connect_seconds`は常に正の数である必要があります。

バックグラウンドレスポンス、batch、fine-tuning jobでは、1本のプロキシ接続を無期限に保持する代わりに、jobを作成した後にクライアントからステータスをポーリングしてください。RealtimeとResponses WebSocketのワークロードには、双方向通信の別設計が必要であり、対応しません。

## モードとフィーチャー

各リスナー項目の`mode`には`passthrough`、`grok-image`、`gemini-image`、または`featherless`を指定でき、1プロセスが設定された全リスナーを並行に待ち受けます。`GET /health`はどのモードでも動作し、各ポートが自身の`status`、`version`、`mode`、有効なフィーチャー名を返します。起動時には、プロキシのバージョンとリスナー数を示す集計行を1行、続けて各リスナーのポート・モード・有効なフィーチャー名を示す行を1行ずつ標準出力へ表示し、設定警告は1件ずつ標準エラー出力に表示します。リスナー個別の警告には`[port N]`接頭辞が付きます。リスナー数とリスナー行は実際に起動したリスナーのみが対象で、モード固有キー欠落でスキップされたリスナーは警告行を通じてのみ報告されます。上流エラーはステータスと本文をそのまま透過し、上流への接続・読取失敗は`passthrough`と同じ502/504応答を返します。

|モード・フィーチャー|機能|設定|
|---|---|---|
|`passthrough`|OpenAI互換APIを無変換で中継|`upstream`|
|`grok-image`|Grok(xAI)の画像生成をOpenAI互換インターフェースとして提供|`grok_image`|
|`gemini-image`|Google Geminiの画像生成をOpenAI互換インターフェースとして提供|`gemini_image`|
|`featherless`|featherless.aiをモデルホワイトリストとAPIキーごとの同時リクエストキューイングで中継|`featherless`|
|`logging`フィーチャー|構造化プロトコルログ。全モードで利用可能|`features`|

設定列のモード固有キーは、その項目の`mode`では必須です。欠落しているリスナー項目は起動対象から外れ、ポート・モード・欠落キー名を含む起動時警告が表示されます。`passthrough`にモード固有キーはありません。`upstream`ブロックは全モードで共通です。

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

`grok_image`キーは`grok-image`リスナー項目内では必須です。欠落している項目は起動対象から外れ、起動時警告として報告されます。他のモードでは起動時警告付きで無視されます。リクエストでの直接指定が優先される既定値を持ちます。

- `default_model`: リクエストに`model`がない場合に使う既定モデル。リクエスト・設定の双方にない場合はHTTP 400を返します。
- `aspect_ratio`: リクエストに`aspect_ratio`がない場合に付与します(例: `1:1`、`16:9`)。
- `resolution`: リクエストに`resolution`がない場合に付与します。`1k`または`2k`です。

完全な`grok-image`設定例:

```json
{
  "listeners": [
    {
      "port": 8000,
      "mode": "grok-image",
      "grok_image": {
        "default_model": "grok-imagine-image-2.0",
        "aspect_ratio": "1:1",
        "resolution": "1k"
      },
      "upstream": {
        "base_url": "https://api.x.ai",
        "api_key_env": "XAI_API_KEY"
      },
      "timeouts": {
        "connect_seconds": 10,
        "read_seconds": 120
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
    }
  ]
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

`gemini-image`モードでは、プロキシはリクエストに`response_format`がなければ`b64_json`を付与し、`n`は省略可または1のみであることを検証し(Geminiはリクエストごとに1枚しか返さないため、それ以外の`n`はHTTP 400で拒否)、`response_format`が`b64_json`以外の場合やその他不正リクエストは上流へ送る前にHTTP 400で拒否し、`size`や`quality`などそれ以外のキーはそのまま透過し、リクエストに`size`と`aspect_ratio`の両方がない場合にのみ設定した`aspect_ratio`既定値を付与します。画像データは常にbase64エンコードされたJPEGで返ります。`logging`フィーチャーはこのモードでも`passthrough`と同様に機能します。

OpenAI互換層での画像生成に使えるモデルは、Googleによるホワイトリストに制限されます。2026-09-09時点で動作を確認済みなのは`gemini-3-pro-image-preview`のみで、`gemini-2.5-flash-image`は公式文書に記載がありますが2026-10-02に提供終了予定です。GA名の`gemini-3-pro-image`と`gemini-3.1-flash-image`は現在この層経由ではHTTP 404となり利用できません。

`gemini_image`キーは`gemini-image`リスナー項目内では必須です。欠落している項目は起動対象から外れ、起動時警告として報告されます。他のモードでは起動時警告付きで無視されます。リクエストでの直接指定が優先される既定値を持ちます。

- `default_model`: リクエストに`model`がない場合に使う既定モデル。リクエスト・設定の双方にない場合はHTTP 400を返します。
- `aspect_ratio`: リクエストに`size`と`aspect_ratio`の両方がない場合に付与します(例: `1:1`、`16:9`)。

完全な`gemini-image`設定例:

```json
{
  "listeners": [
    {
      "port": 8000,
      "mode": "gemini-image",
      "gemini_image": {
        "default_model": "gemini-3-pro-image-preview",
        "aspect_ratio": "1:1"
      },
      "upstream": {
        "base_url": "https://generativelanguage.googleapis.com",
        "api_key_env": "GEMINI_API_KEY"
      },
      "timeouts": {
        "connect_seconds": 10,
        "read_seconds": 120
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
    }
  ]
}
```

### featherlessモード

`featherless`は、[featherless.ai](https://featherless.ai)をモデルホワイトリストと同時リクエストキューイング付きのOpenAI互換インターフェースとして提供します。プロキシはAPIキーを保持しません。各クライアントの`Authorization`ヘッダーをfeatherless.aiへ変更せず中継するため、すべてのクライアントが自分のfeatherless.ai APIキーを必要とします。

|経路|メソッド|動作|
|---|---|---|
|`/v1/models`|GET|ホワイトリスト内のモデルのみを、プロキシのモデル情報キャッシュから組み立てて返却。クエリパラメータは無視|
|`/v1/models/{model_id}`|GET|ホワイトリスト内のモデル詳細をキャッシュから返却。それ以外はOpenAI互換エラーでHTTP 404|
|`model`フィールドを持つJSONリクエスト|POST等|ホワイトリスト照合と同時接続ゲートを経て、クライアントの`Authorization`付きで転送|
|その他のリクエスト|任意|ゲートなしで転送|

`model`がホワイトリストにないリクエストは、上流へ送る前にHTTP 404で拒否します。非JSONリクエストと`model`フィールドのないJSONリクエストはゲートなしで転送し、上流が通常どおり拒否します。`Authorization`ヘッダーのないリクエストはゲートを通さずにそのまま転送します。

#### 同時接続制御

featherless.aiは同時リクエストをunitで計量します。処理中の各リクエストはモデルサイズに応じたコストを消費します(小規模モデルは1、中規模は2、大規模は4。正確な値は各モデルの`concurrency_cost`から取得)。プロキシはモデル情報キャッシュから各リクエストのモデルのコストを解決し、次をします。

- 起動時に`GET /v1/plan`からプランの同時接続上限を取得。`concurrency_limit`設定があればそれを優先。
- `GET /account/concurrency`スナップショットを定期的に取得して実際の使用量を追跡。同じAPIキーでプロキシを経由しないリクエストの消費も反映。
- プロキシ自身の予約をAPIキーごとに管理し、`max(ローカル予約, 上流使用量) + コスト`が上限を超えるリクエストは、APIキーごとのキューで待機。残り予算に収まる待機リクエストは到着順に受け入れ、収まらないリクエストはいったんスキップして、それより小さい後続リクエストが先に通ることがあります。スキップされたリクエストも、収まるまで待つかタイムアウトするまでキューに残ります。
- 待機が`max_queue_wait_seconds`(既定60秒)を超えたら、OpenAI互換エラーでHTTP 429を応答。
- 待機中のクライアント切断を検知したら、キュー枠と予約を解放。

`logging`フィーチャーはこのモードでも`passthrough`と同様に機能します。

#### `featherless`設定

`featherless`キーは`featherless`モードでは必須です。欠落している項目は起動対象から外れ、起動時警告として報告されます。他のモードでは起動時警告付きで無視されます。

- `model_whitelist`: 必須。モデルid文字列の空でないリスト(例: `moonshotai/Kimi-K2.6`)。完全一致のみ。`GET /v1/models`に表示され、リクエストで受け付けられるのはこれらのモデルだけです。
- `concurrency_limit`: 任意の正の整数。`GET /v1/plan`から取得するプラン上限を上書きします。省略時はプランに自動追従します。
- `max_queue_wait_seconds`: 任意の正の数。既定値は60。
- `cache_ttl_seconds`: 任意の正の数。既定値は300。モデル情報(利用可否を含む)をこの時間キャッシュします。取得時に利用不可能だったモデルは、次の更新で再試行されます。

#### 新しいモデルの追加

featherless.aiは数万のモデルを提供しており、ホワイトリストは手動で管理します。モデルを追加するには、featherless.aiのサイトか次のコマンドでモデルidを確認してください。

```bash
curl -s 'https://api.featherless.ai/v1/models?per_page=100' | jq -r '.data[].id' | head -50
```

(認証不要。一覧は`page`でページネーションされます。)確認したidを`model_whitelist`に追記し、プロキシを再起動してください。モデル情報は`cache_ttl_seconds`ごとに更新されます。

完全な`featherless`設定例:

```json
{
  "listeners": [
    {
      "port": 8000,
      "mode": "featherless",
      "featherless": {
        "model_whitelist": [
          "moonshotai/Kimi-K2.6",
          "Qwen/Qwen3-Coder-480B"
        ],
        "max_queue_wait_seconds": 60,
        "cache_ttl_seconds": 300
      },
      "upstream": {
        "base_url": "https://api.featherless.ai"
      },
      "timeouts": {
        "connect_seconds": 10,
        "read_seconds": 120
      }
    }
  ]
}
```

### loggingフィーチャー

リスナー項目の`features`配列は省略でき、そのリスナーはプロトコルログなしで動作します。指定する場合は`logging`を1件だけ指定できます。プロキシは重複・未知feature・不正なlogging設定を起動時に拒否します。

- `stdout: true`(既定)はUTF-8 JSON Linesを標準出力へ出します。コンテナでは`docker compose logs -f nichellm-proxy`で追跡できます。
- `file.enabled: true`は同一レコードを`file.path`へ出します。このパスは絶対パスである必要があります。`max_bytes`と`backup_count`はともに正の値が必要です。例の既定値では10 MiBの現行ファイルと5世代を保持するため、概算60 MiBです。複数のリスナーが同じパスへプロトコルログを書く場合、プロキシは起動時警告を表示しますが起動を続けます。
- 全レコードに、その交換を処理したリスナーを示す`listener_port`フィールドが含まれるため、並行するリスナーの記録を区別できます。
- `capture.bodies`の既定値は**false**です。trueの場合だけ、JSON・テキスト・SSEのリクエスト・レスポンス本文を`max_body_bytes`(既定1 MiB、最大10 MiB)まで記録します。中継ストリームを待機・再構築しません。
- multipartとバイナリ本文は保存しません。バイト数、SHA-256 digest、省略理由だけを記録します。テキスト本文が上限で切れた場合はレコードに明記します。
- `Authorization`、プロキシ自身の認証情報、Cookie、APIキーヘッダー、`token`、`secret`、`password`、`api_key`を含む名前の値はマスクします。プロジェクト固有の名前は3つの`redaction`配列へ追加してください。自由文形式のプロンプトやツール出力に埋め込まれた秘密情報・個人情報をJSON redactionで確実に検出することはできません。

`grok-image`と`gemini-image`の各モードでは、上流へのリクエスト送出時点で、変換後リクエストのバイト数とSHA-256 digestを記録した`upstream_request_sent`イベントも出力します。

本文captureを有効にすると、ユーザープロンプトとモデル出力を意図的に保存します。信頼できる環境だけで有効にし、ログボリュームへのアクセス制御と保持・削除方針を定めてください。

### サポートしないこと

上記の中継動作に加えて、プロキシは`grok-image`と`gemini-image`の変換以外のプロトコル変換・プロバイダーアダプターを提供しません。また、webhook受信・署名検証、Administration API操作、レート制限、プロキシ自身の認証、TLS終端も提供しません。`featherless`モードのAPIキー管理はクライアント側のみです。プロキシ側でのキー管理やホワイトリスト管理APIは提供しません。

## セキュリティ

- nicheLLM Proxyにはプロキシ自身の認証・TLS終端がありません。プロキシに到達できる者は誰でも、設定済みの上流APIキーとその利用枠をプロキシ経由で使えます。信頼できるネットワーク内だけで運用してください。
- インターネットへ直接公開しないでください。ネットワーク越しに使う場合は、利用者自身が手前に置くリバースプロキシでTLS終端と認証を行うことを想定しています。ホスト実行(uv)は全インターフェース(`0.0.0.0`)でリッスンしますが、同梱のCompose構成は既定で`127.0.0.1:8000`のみを公開します。
- プロトコルログは機密データとして扱ってください。本文captureはopt-inですが、プロンプト、モデル出力、個人情報を含み得ます。ログボリュームへのアクセスを制限し、保持・削除方針を定めてください。

## トラブルシューティング

起動時の代表的なエラーと確認ポイント:

- `v1.4以降の設定には最上位の 'listeners' 配列が必要です。移行手順はREADMEを参照してください。`(英語: `Since v1.4 the configuration requires a top-level 'listeners' array. See the README for the migration guide.`) — 設定がv1.4より前の形式です。[設定](#設定)の`listeners`形式へ書き換えてください。
- `設定ファイルが見つかりません: {path}`(英語: `Configuration file was not found: {path}`) — そのパスに設定ファイルがありません。ホスト実行では`NICHELLM_CONFIG_PATH`を、Dockerでは`config.jsonc`のマウントを確認してください。
- `上流APIキーの環境変数 '{api_key_env}' が設定されていません。`(英語: `Upstream API key environment variable '{api_key_env}' is not set.`) — `api_key_env`が指す変数が設定されていません。シェルでexportするか、Composeファイルと同じ場所の`.env`ファイルに設定してください。
- `モード '{mode}' には '{section}' セクションが必要ですが設定に存在しないため、ポート {port} のリスナーをスキップしました。`(英語: `Skipped the listener on port {port} because mode '{mode}' requires a '{section}' section, which is missing from the configuration.`) — 該当ポートのリスナー項目はモード固有キーが必須のモードで設定されていますが、そのキーが欠落しているため、このリスナーは起動されていません。欠落キーを追加するか(詳細は[モードとフィーチャー](#モードとフィーチャー))、`mode`を`passthrough`に変更してください。他のリスナーは通常どおり起動します。
- 設定が効いていないように見える — 最上位の未知キー、項目の`mode`に属さないモード固有ブロック、`logging.file.enabled`が`false`のときの`logging.file`の設定は、起動時の警告とともに無視されます。リスナー個別の警告には`[port N]`接頭辞が付きます。起動時に表示される警告行と、キーのスペル(例: `timeouts`を`timouts`と書く)を確認してください。

プロキシ自身が出すエラーメッセージは`NICHELLM_LANGUAGE`に応じてローカライズされます(既定は英語、`ja`で日本語)。

## 変更履歴

### v1.4.1(2026-09-15)

- 挙動変更: `grok-image`・`gemini-image`・`featherless`の各モードのリスナー項目でモード固有キー(`grok_image`、`gemini_image`、`featherless`)が欠落している場合、そのリスナーは起動対象から外れ、ポート・モード・欠落キー名を含む起動時警告が表示されるようになりました。従来は`grok_image`・`gemini_image`の欠落時に既定値で起動し、`featherless`の欠落時は起動中にクラッシュしていました。他のリスナーは通常どおり起動し、全リスナーがスキップされた場合は設定エラーで起動に失敗します。
- `tools/check.sh`にruffのlintゲートを追加しました。ruffは開発依存として`uv.lock`に固定されます。
- `/health`・起動出力・スキップ警告・SIGINT終了を検証する実プロセス起動スモークテストを追加しました。

### v1.4.0(2026-09-15)

- 破壊的設定変更: 最上位の`listener`、`upstream`、`timeouts`ブロックを、各項目が1つの完全なリスナー(ポート、モード、upstream、timeouts、モード固有ブロック、features)を記述する必須の最上位`listeners`配列へ置き換えました。v1.4より前のファイルは、READMEの移行手順を指すエラーで起動時に拒否されます。
- 1プロセスで複数リスナーを異なるモードで並行待受(ポートは一意)、設定ファイルのJSONCコメント対応、`GET /health`のリスナーごとの提供、プロトコルログレコードへの`listener_port`フィールド追加、全リスナーを列挙する起動出力に対応しました。

全履歴は英語の[CHANGELOG.md](CHANGELOG.md)を参照してください。

## ライセンス

このプロジェクトはMIT Licenseの下でライセンスされます。詳細は[LICENSE](LICENSE)を参照してください。