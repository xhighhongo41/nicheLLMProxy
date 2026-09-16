# nicheLLM Proxy

nicheLLM Proxy relays HTTP requests and responses between an OpenAI-compatible client and an upstream LLM provider. The `passthrough` mode forwards without transformation, the `grok-image` mode added in v1.1 presents Grok (xAI) image generation through an OpenAI-compatible interface, the `gemini-image` mode added in v1.2 presents Google Gemini image generation through an OpenAI-compatible interface, and the `featherless` mode added in v1.3 presents featherless.ai through a model whitelist and per-key concurrent-request queueing. Since v1.4, one process can serve multiple listeners in different modes concurrently; all modes run in a trusted network and support opt-in structured protocol logging.

[日本語版 README](README_ja.md)

## Installation

### Requirements

- Docker with the `docker compose` plugin (recommended), or
- Python 3.11 or later with [uv](https://docs.astral.sh/uv/) for host execution.
- Git, to clone and update the repository.

An upstream LLM provider account with an API key is required in every setup. See [Configuration](#configuration) for the configuration file. In `featherless` mode the proxy itself holds no API key: each client sends its own `Authorization` header (see [featherless mode](#featherless-mode)).

### Run with Docker Compose (from source)

The Docker image contains no API key or configuration file. Create them on the host before starting the service.

```bash
git clone https://github.com/xhighhongo41/nicheLLMProxy.git
cd nicheLLMProxy
cp config.example.jsonc config.jsonc
# If you use a provider other than OpenAI, change upstream.base_url in config.jsonc.
export UPSTREAM_API_KEY='your-upstream-api-key'
export NICHELLM_LANGUAGE=ja  # optional; English is the default
docker compose up --build -d
```

The bundled `docker-compose.yml` requires `UPSTREAM_API_KEY` to be set even when `api_key_env` names a different variable (for example `XAI_API_KEY`); set any value for it, or adjust the `environment` entries to your configuration. In `featherless` mode no API key variable is needed at all; set any value for `UPSTREAM_API_KEY`, or adjust the `environment` entries. The Compose file includes a healthcheck that probes `GET /health` on every listener port every 30 seconds, reading the configured ports from the mounted `config.jsonc` so it stays accurate for any port set; `docker compose ps` shows the service as `healthy` once it is ready. Check the proxy:

```bash
curl http://127.0.0.1:8000/health
```

Compose mounts `config.jsonc` read-only at `/app/config/config.jsonc` and publishes only `127.0.0.1:8000` by default; for a multi-listener setup, add one `ports` mapping per listener port (for example `"127.0.0.1:8001:8001"`). The proxy has no authentication, so keep this binding on trusted networks. To let other machines on your network connect, change the host-side binding in the `ports` mapping (for example `"8000:8000"`), only on networks you trust. Stop the service with:

```bash
docker compose down
```

The `nichellm-proxy-logs` named volume persists `/var/log/nichellm` across container recreation. Inspect the current file without printing sensitive bodies to shared terminals:

```bash
docker compose logs -f nichellm-proxy
docker compose exec nichellm-proxy sh -c 'ls -lh /var/log/nichellm'
```

To delete retained logs deliberately, stop the service and remove the named volume with `docker compose down -v`; `docker compose down` alone does not remove it.

### Run with the published Docker Hub image

Published multi-platform (`linux/amd64`, `linux/arm64`) images are available at `xhighhongo41/nichellm-proxy`. Use an exact version tag in production; rolling tags such as `1.3` and `latest` also exist.

```bash
docker pull xhighhongo41/nichellm-proxy:1.4.3
```

The image contains no API key or configuration file. Create a `.env` file next to the Compose file with the API key variables your configuration uses (see [API key management](#api-key-management)), and put a `config.jsonc` (start from the full example in [Configuration](#configuration)) and this Compose file in a working directory:

```yaml
services:
  nichellm-proxy:
    image: xhighhongo41/nichellm-proxy:1.4.3
    # Add one mapping per listener port defined in config.jsonc.
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

Then start it:

```bash
docker compose up -d
curl http://127.0.0.1:8000/health
```

This minimal example omits the healthcheck included in the bundled `docker-compose.yml`; the `curl` above can fail while the container is still starting — retry after a few seconds. Add one `ports` mapping per listener port defined in your `config.jsonc`. The API key variables above are optional references; the proxy verifies at startup that the variable named by `upstream.api_key_env` in each listener entry is actually set. The named log volume behaves the same as in the Compose-from-source setup, including removal with `docker compose down -v`.

### Run locally with uv

[uv](https://docs.astral.sh/uv/) creates and uses a project-local virtual environment.

```bash
git clone https://github.com/xhighhongo41/nicheLLMProxy.git
cd nicheLLMProxy
cp config.example.jsonc config.jsonc
# If you use a provider other than OpenAI, change upstream.base_url in config.jsonc.
export UPSTREAM_API_KEY='your-upstream-api-key'
export NICHELLM_CONFIG_PATH="$PWD/config.jsonc"
export NICHELLM_LANGUAGE=ja  # optional; English is the default
uv sync
uv run niche-llm-proxy
```

The `file.path` in the example is intended for the Docker volume. For host execution, either set `file.enabled` to `false` or change `file.path` to an absolute directory writable by your user.

From another terminal, check the proxy:

```bash
curl http://127.0.0.1:8000/health
```

Host execution listens on all interfaces (`0.0.0.0`), unlike the Compose setups, which publish only `127.0.0.1:8000` by default. Keep this in mind on shared networks and restrict access with a firewall if necessary. Stop the foreground proxy with Ctrl-C.

### Using the proxy from a client

Point any OpenAI-compatible client at `http://127.0.0.1:8000/v1` instead of the upstream provider. With `curl`:

```bash
curl http://127.0.0.1:8000/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"model": "gpt-4o-mini", "messages": [{"role": "user", "content": "Hello"}]}'
```

With the OpenAI Python library, set `base_url` and use any placeholder `api_key`:

```python
from openai import OpenAI

client = OpenAI(base_url="http://127.0.0.1:8000/v1", api_key="placeholder")
response = client.chat.completions.create(
    model="gpt-4o-mini",
    messages=[{"role": "user", "content": "Hello"}],
)
print(response.choices[0].message.content)
```

The `Authorization` header is optional: the proxy always replaces it with the configured upstream Bearer API key (see [API key management](#api-key-management)), so clients never hold the real key. In `featherless` mode the opposite holds: each client must send its real featherless.ai key in `Authorization`, and the proxy relays it unchanged. The image modes accept `POST /v1/images/generations` the same way.

### Updating an existing installation

v1.4.0 changes the configuration format: the top-level `listener`, `upstream`, and `timeouts` blocks are replaced by a required top-level `listeners` array, and a pre-v1.4 file is rejected at startup with the error `Since v1.4 the configuration requires a top-level 'listeners' array. See the README for the migration guide.` To migrate, move `port` and `mode` together with the `upstream` and `timeouts` blocks into a single entry of the `listeners` array, as described in [Configuration](#configuration). An existing `config.json` left untouched is still picked up by the default path fallback but fails to start until it is rewritten.

Docker Compose from source:

```bash
git pull
docker compose up --build -d
```

Published Docker Hub image: update the image tag in your Compose file (for example `xhighhongo41/nichellm-proxy:1.4.3`), then:

```bash
docker compose pull
docker compose up -d
```

Local uv execution: stop the running proxy, then:

```bash
git pull
uv sync
```

and start it again.

## Configuration

### Configuration file

The proxy reads one mandatory configuration file. Five templates are included in the repository (fetch them from GitHub when you run only the published image):

- `config.example.jsonc` — `passthrough` mode (shown below)
- `config.multi-listener.example.jsonc` — all four modes on ports 8000–8003
- `config.grok-image.example.jsonc` — `grok-image` mode
- `config.gemini-image.example.jsonc` — `gemini-image` mode
- `config.featherless.example.jsonc` — `featherless` mode

Inside a container, the default path prefers `/app/config/config.jsonc` and falls back to `/app/config/config.json` when the JSONC file does not exist; the Docker Compose setups above mount your local `config.jsonc` at the preferred path. For host execution, set the path with `NICHELLM_CONFIG_PATH`.

Configuration files accept JSONC: `//` line comments and `/* */` block comments are allowed regardless of the file extension, and comment markers inside string values (for example in URLs) are preserved. The configuration has one required top-level `listeners` array. Each entry is one complete, independent listener: it binds one port and carries its own `mode`, `upstream`, `timeouts`, optional mode-specific block, and optional `features`. One process serves every entry concurrently; `config.multi-listener.example.jsonc` combines all four modes on ports 8000–8003. Ports must be unique; duplicate ports are rejected as a configuration error.

The `passthrough` example:

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

The `file.path` in the example is intended for the Docker volume. For host execution, either disable file logging or change it to a writable absolute directory (see [Run locally with uv](#run-locally-with-uv)).

Common keys:

|Key|Type / constraint|Default|
|---|---|---|
|`listeners[].port`|integer 1–65535 (required)|—|
|`listeners[].mode`|`passthrough`, `grok-image`, `gemini-image`, or `featherless` (required)|—|
|`listeners[].upstream.base_url`|http/https URL without query or fragment (required)|—|
|`listeners[].upstream.api_key_env`|non-empty string; the name of the environment variable holding the API key (required except in `featherless` mode, where it must be omitted)|—|
|`listeners[].timeouts.connect_seconds`|positive number|10.0|
|`listeners[].timeouts.read_seconds`|positive number or `null`|120.0|

The `timeouts` object itself is optional, and every key with a default value can be omitted. Multiple listeners may share one upstream provider by repeating the same `upstream` settings. The healthcheck in the bundled `docker-compose.yml` reads the configured listener ports from the mounted `config.jsonc` and probes each one, so it stays accurate for any port set.

Mode-specific keys (`grok_image`, `gemini_image`, `featherless` inside a listener entry) and the optional `features` logging feature are described in [Modes and features](#modes-and-features). The `grok-image`, `gemini-image`, and `featherless` modes require their mode-specific key: a listener entry in one of these modes whose mode-specific key is missing is not started, a startup warning naming the port, mode, and missing key is printed to the standard error output, and the remaining listeners start normally. `passthrough` needs no mode-specific key. When every listener is skipped this way, startup fails with a configuration error. Unknown keys inside the mode-specific objects and invalid values are rejected as startup configuration errors. A mode-specific block that does not belong to the entry's `mode`, unknown top-level keys, and `logging.file` settings ignored while `logging.file.enabled` is `false` are reported as startup warnings and ignored — the proxy keeps starting and prints each warning to the standard error output; check the key spelling (for example `timouts` instead of `timeouts`) if a setting seems to have no effect. Complete mode examples are available as `config.grok-image.example.jsonc`, `config.gemini-image.example.jsonc`, and `config.featherless.example.jsonc`.

### Environment variables

|Environment variable|Required|Purpose|
|---|---|---|
|`UPSTREAM_API_KEY`|Yes|API key sent to the upstream provider. Its name must match the `api_key_env` of the listener entry that uses it.|
|`XAI_API_KEY`|No|API key sent to xAI by the `grok-image` mode example. Its name must match `api_key_env`.|
|`GEMINI_API_KEY`|No|API key sent to Google by the `gemini-image` mode example. Its name must match `api_key_env`.|
|`NICHELLM_CONFIG_PATH`|No|Path to the configuration file. When unset, the container defaults to `/app/config/config.jsonc`, falling back to `/app/config/config.json` when the JSONC file does not exist; set it for host execution. When set, exactly that path is used with no fallback.|
|`NICHELLM_LANGUAGE`|No|Language for proxy-generated messages: `en` (default) or `ja`. Values such as `ja-JP` are treated as `ja`; unsupported values fall back to English.|

The three API key variables are the names referenced by `api_key_env` in the bundled configuration examples. The variable name itself is configurable: whatever `api_key_env` names must be set before the proxy starts, and each listener entry can name a different variable, so one process can serve several providers. The `featherless` mode reads no API key environment variable: each client sends its own `Authorization` header, which the proxy relays to featherless.ai unchanged.

These are the only environment variables the proxy reads. The configuration cannot be replaced or overridden wholesale through environment variables.

### API key management

Do not put an API key value in the configuration. Specify only the name of the environment variable that contains it (`upstream.api_key_env` in the listener entry), and set the actual value in the environment:

```bash
export UPSTREAM_API_KEY='your-upstream-api-key'
```

For Docker Compose, a `.env` file next to the Compose file is a convenient place for the real values; Compose reads it automatically. `.env.example` in the repository lists the API key and language variables from the table above.

The proxy replaces a client-supplied `Authorization` header with the configured upstream Bearer API key and does not forward the received value, so clients never need the real upstream key. In `featherless` mode this replacement does not happen: the proxy holds no upstream API key, must not define `api_key_env`, and relays each client's `Authorization` header unchanged, so every client needs its own featherless.ai API key (see [featherless mode](#featherless-mode)).

### Timeouts

Each listener entry has its own optional `timeouts` block. `connect_seconds` limits the time to establish an upstream connection. `read_seconds` limits the wait for the next byte from the upstream; it is not a limit on the total duration of a response that continues to deliver data. Keep the configured timeout for HTTP SSE and ordinary HTTP responses. Set `read_seconds` to `null` to disable the read timeout and wait for the upstream without a limit; `connect_seconds` must always be a positive number.

For background responses, batches, and fine-tuning jobs, create the job and poll its status from the client instead of holding one proxy connection indefinitely. Realtime and Responses WebSocket workloads require a separate bidirectional transport design and are not supported.

## Modes and features

Each listener entry's `mode` accepts `passthrough`, `grok-image`, `gemini-image`, or `featherless`; one process serves every configured listener concurrently. `GET /health` works in every mode and each port returns its own `status`, `version`, `mode`, and enabled feature names. At startup, the proxy prints one aggregate line to the standard output naming the proxy version and the listener count, followed by one line per listener naming its port, mode, and enabled feature names, and prints each configuration warning to the standard error output; per-listener warnings are prefixed with `[port N]`. The listener count and per-listener lines cover only the listeners that actually start; a listener skipped for a missing mode-specific key is reported only through its warning. Upstream errors are passed through with their status and body unchanged, and upstream connection and read failures return the same 502/504 responses as `passthrough`.

|Mode / feature|Function|Settings|
|---|---|---|
|`passthrough`|Relays OpenAI-compatible APIs without transformation|`upstream`|
|`grok-image`|Presents Grok (xAI) image generation through an OpenAI-compatible interface|`grok_image`|
|`gemini-image`|Presents Google Gemini image generation through an OpenAI-compatible interface|`gemini_image`|
|`featherless`|Relays featherless.ai with a model whitelist and per-key concurrent-request queueing|`featherless`|
|`logging` feature|Structured protocol logging, available in every mode|`features`|

The mode-specific key in the Settings column is required when the entry's `mode` is that mode; a listener entry that omits it is not started and a startup warning naming the port, mode, and missing key is printed. `passthrough` has no mode-specific key. The `upstream` block is common to every mode.

### passthrough mode

`passthrough` forwards requests and responses without transformation:

- Pass-through forwarding for `GET`, `POST`, `PUT`, `PATCH`, `DELETE`, `HEAD`, and `OPTIONS`, preserving the request method, path, query, and body.
- One raw HTTP pipeline for JSON, `text/event-stream` (SSE), multipart uploads, and binary request and response bodies. The proxy does not parse or reconstruct endpoint-specific bodies or SSE events.
- Pass-through upstream status codes, including HTTP errors and `206 Partial Content` range responses, plus end-to-end response headers. Duplicate end-to-end headers (such as multiple `Set-Cookie` headers) are retained.
- Replace the client `Authorization` header as described in [API key management](#api-key-management). Apart from `Host`, hop-by-hop headers, and the received `Authorization`, end-to-end request headers are forwarded.
- `GET /health` for proxy liveness checks, and safe proxy-generated 5xx responses for upstream connection and read failures.
- User-facing proxy messages in English (default) or Japanese.
- Optional JSON Lines protocol logs for request, upstream-response, completion, failure, and cancellation events. Logs go to stdout for `docker compose logs -f` and optionally to a rotating file.

#### Transport-tested API families

The following OpenAI API families are in the documented representative HTTP transport scope. This describes transport behavior only; acceptance of models, parameters, and feature semantics remains the responsibility of the upstream provider.

|API family|Representative paths|Transport forms|
|---|---|---|
|Chat Completions|`/v1/chat/completions` and saved-completion subresources|JSON, SSE|
|Responses|`/v1/responses` and response subresources|JSON, HTTP SSE|
|Conversations|`/v1/conversations` and item subresources|JSON, pagination query|
|Embeddings, Models, Moderations|`/v1/embeddings`, `/v1/models`, `/v1/moderations`|JSON|
|Images and Audio|generation, edit, speech, transcription, and translation paths|JSON, multipart, SSE, binary|
|Files and Uploads|`/v1/files`, `/v1/uploads`, and subresources|multipart, JSON, binary, Range/206|
|Batches and Fine-tuning Jobs|their collection and operation subresources|JSON, asynchronous polling|
|Vector Stores and Containers|their collection, file, and content subresources|JSON, multipart, binary|

Other non-deprecated OpenAI HTTP endpoints are not blocked by the wildcard route, but are not individually listed as transport-tested APIs.

HTTP pass-through does not guarantee the upstream API's semantic compatibility, authorization policy, model availability, or account eligibility. In particular, Fine-tuning Job eligibility is determined by the upstream account.

WebSocket, WebRTC, or SIP transport, including Realtime API and the Responses WebSocket mode, is not supported. HTTP SSE is supported, but it is not a bidirectional WebSocket replacement.

Some deprecated or legacy OpenAI endpoints (for example Assistants (`/v1/assistants`) and Legacy Completions) are not individually transport-tested. The wildcard route may mechanically forward such a path, but this does not make it a supported or recommended API.

### grok-image mode

`grok-image` presents Grok (xAI) image generation through an OpenAI-compatible interface:

|Path|Method|Behavior|
|---|---|---|
|`/v1/images/generations`|POST|OpenAI Images request translated to the xAI image generation API; response made OpenAI-compatible|
|`/v1/models`|GET|Forwarded to the upstream without transformation|
|`/v1/image-generation-models`|GET|Forwarded to the upstream without transformation|
|Any other path|Any|HTTP 404 with a localized error|
|An unsupported method on the paths above|—|HTTP 405 with a localized error|

In `grok-image` mode, the proxy removes the OpenAI-only parameters `size`, `quality`, `style`, `seed`, `background`, `moderation`, `output_format`, and `output_compression`; adds `response_format: "b64_json"` when the request omits it (explicit `b64_json` and `url` values pass through; other values are rejected with HTTP 400); validates that `n` is an integer between 1 and 10; rejects other invalid requests with HTTP 400 before they reach the upstream; and passes other keys such as `storage_options` through unchanged. The `logging` feature works in this mode as in `passthrough`.

The `grok_image` key is required inside a `grok-image` listener entry; an entry that omits it is not started and is reported with a startup warning. In other modes the key is ignored with a startup warning. It supplies defaults that a direct request value overrides:

- `default_model`: used when the request has no `model`. If neither is present, the proxy returns HTTP 400.
- `aspect_ratio`: added when the request omits `aspect_ratio` (for example `1:1` or `16:9`).
- `resolution`: added when the request omits `resolution`. It must be `1k` or `2k`.

Complete `grok-image` example:

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

### gemini-image mode

`gemini-image` presents Google Gemini image generation through an OpenAI-compatible interface, using the Gemini API's OpenAI compatibility layer:

|Path|Method|Behavior|
|---|---|---|
|`/v1/images/generations`|POST|Forwarded to `/v1beta/openai/images/generations`; response made OpenAI-compatible|
|`/v1/models`|GET|Forwarded to `/v1beta/openai/models` without transformation|
|Any other path|Any|HTTP 404 with a localized error|
|An unsupported method on the paths above|—|HTTP 405 with a localized error|

In `gemini-image` mode, the proxy adds `response_format: "b64_json"` when the request omits it; validates that `n` is omitted or equal to 1, and rejects any other `n` value with HTTP 400 because Gemini returns a single image per request; rejects `response_format` values other than `b64_json`, and other invalid requests, with HTTP 400 before they reach the upstream; passes other keys such as `size` and `quality` through unchanged; and adds the configured `aspect_ratio` default only when the request has neither `size` nor `aspect_ratio`. Image data always comes back as base64-encoded JPEG. The `logging` feature works in this mode as in `passthrough`.

Model availability for image generation through the OpenAI compatibility layer is restricted by Google to a whitelist. As of 2026-09-09, `gemini-3-pro-image-preview` is the only model verified to work, and `gemini-2.5-flash-image` is documented but reaches its end of life on 2026-10-02. The GA model names `gemini-3-pro-image` and `gemini-3.1-flash-image` currently return HTTP 404 through this layer and cannot be used.

The `gemini_image` key is required inside a `gemini-image` listener entry; an entry that omits it is not started and is reported with a startup warning. In other modes the key is ignored with a startup warning. It supplies defaults that a direct request value overrides:

- `default_model`: used when the request has no `model`. If neither is present, the proxy returns HTTP 400.
- `aspect_ratio`: added when the request has neither `size` nor `aspect_ratio` (for example `1:1` or `16:9`).

Complete `gemini-image` example:

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

### featherless mode

`featherless` presents [featherless.ai](https://featherless.ai) through an OpenAI-compatible interface with a model whitelist and concurrent-request queueing. The proxy holds no API key: each client's `Authorization` header is relayed to featherless.ai unchanged, and every client needs its own featherless.ai API key.

|Path|Method|Behavior|
|---|---|---|
|`/v1/models`|GET|Whitelisted models only, assembled from the proxy's model info cache; query parameters are ignored|
|`/v1/models/{model_id}`|GET|Whitelisted model detail from the cache; HTTP 404 with an OpenAI-compatible error otherwise|
|JSON requests with a `model` field|POST and others|Whitelist check, concurrency gate, then relayed with the client's `Authorization`|
|Other requests|any|Relayed without the gate|

Requests whose `model` is not in the whitelist are rejected with HTTP 404 before reaching the upstream. Non-JSON requests and JSON without a `model` field are relayed without the gate and rejected by the upstream as usual. Requests without an `Authorization` header bypass the gate entirely.

#### Concurrency control

featherless.ai meters concurrent requests in units: each in-flight request consumes a model-size-dependent cost (small models cost 1, mid-size 2, large 4 units; the exact value comes from each model's `concurrency_cost`). The proxy resolves the cost of each request's model from the model info cache and:

- obtains the plan's concurrency limit from `GET /v1/plan` at startup, or uses `concurrency_limit` when set;
- tracks actual usage with periodic `GET /account/concurrency` snapshots, which also covers requests made outside this proxy with the same API key;
- tracks its own reservations per API key, and queues a request when `max(local reservations, upstream usage) + cost` would exceed the limit — per API key. Waiting requests that fit the remaining budget are admitted in arrival order; a request that does not fit is skipped for the moment and later, smaller requests may pass it, until the skipped request either fits or times out;
- answers HTTP 429 with an OpenAI-compatible error when a queued request waits longer than `max_queue_wait_seconds` (default 60 seconds);
- releases the queue slot and reservation when the waiting client disconnects.

The `logging` feature works in this mode as in `passthrough`.

#### `featherless` settings

The `featherless` key is required in `featherless` mode; an entry that omits it is not started and is reported with a startup warning. In the other modes the key is ignored with a startup warning:

- `model_whitelist`: required. A non-empty list of exact model id strings (for example `moonshotai/Kimi-K2.6`). Only these models appear in `GET /v1/models` and are accepted in requests.
- `concurrency_limit`: optional positive integer. Overrides the plan limit fetched from `GET /v1/plan`. Omit it to track the plan automatically.
- `max_queue_wait_seconds`: optional positive number, default 60.
- `cache_ttl_seconds`: optional positive number, default 300. Model info (including availability) is cached for this long; an unavailable model is retried on the next refresh.

#### Adding new models

featherless.ai offers tens of thousands of models and the whitelist is maintained by hand. To add a model, confirm its id on the featherless.ai site or with:

```bash
curl -s 'https://api.featherless.ai/v1/models?per_page=100' | jq -r '.data[].id' | head -50
```

(no authentication needed; the list is paginated with `page`), then add the id to `model_whitelist` and restart the proxy. Model info refreshes every `cache_ttl_seconds`.

Complete `featherless` example:

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

### logging feature

The `features` array in a listener entry may be omitted to run that listener without protocol logging, or may contain one `logging` feature. The proxy rejects duplicate or unknown features and invalid logging settings at startup.

- `stdout: true` (the default) writes UTF-8 JSON Lines to the standard output. In a container, follow them with `docker compose logs -f nichellm-proxy`.
- `file.enabled: true` writes the same records to `file.path`. The path must be absolute. `max_bytes` and `backup_count` must both be positive; the default example retains the active 10 MiB file plus five rotated files (about 60 MiB total). If several listeners write protocol logs to the same path, the proxy prints a startup warning but continues.
- Every record includes a `listener_port` field naming the listener that handled the exchange, so entries from concurrent listeners can be told apart.
- `capture.bodies` is **false by default**. When true, the proxy captures at most `max_body_bytes` (default 1 MiB, maximum 10 MiB) of textual JSON, text, or SSE request and response bodies. It never delays or reconstructs the forwarded stream.
- Multipart and binary bodies are not stored. Their byte count, SHA-256 digest, and omission reason are recorded instead. Truncated textual bodies are marked in the record.
- `Authorization`, proxy authorization, cookies, API-key headers, and names containing `token`, `secret`, `password`, or `api_key` are redacted. Add project-specific names to the three `redaction` arrays. JSON redaction cannot reliably find secrets or personal data embedded in free-form prompts or tool output.

The `grok-image` and `gemini-image` modes additionally emit an `upstream_request_sent` event — with the byte count and SHA-256 digest of the transformed request — at the moment the request is sent upstream.

Enabling body capture intentionally stores user prompts and model output. Use it only in a trusted environment, restrict access to the log volume, and set an operational retention/deletion policy.

### Not supported

Besides the relay behavior described above, the proxy provides no protocol conversion or provider adapters other than the `grok-image` and `gemini-image` conversions. It also does not provide webhook receiving or signature verification, Administration API operations, rate limiting, proxy authentication, or TLS termination. The `featherless` mode manages API keys only on the client side; proxy-side key management and a whitelist administration API are not provided.

## Security

- nicheLLM Proxy has no proxy authentication or TLS termination. Anyone who can reach the proxy can use your upstream API key and its quota through it. Operate it only inside a trusted network.
- Do not expose it directly to the internet. To use it across networks, place your own reverse proxy with TLS termination and authentication in front of it. Note that host execution (uv) listens on all interfaces (`0.0.0.0`), while the bundled Compose setups publish only `127.0.0.1:8000` by default.
- Treat protocol logs as sensitive data. Body capture is opt-in but can contain prompts, model output, and personal data. Restrict access to the log volume and set an operational retention/deletion policy.

## Troubleshooting

Representative startup errors and what to check:

- `Since v1.4 the configuration requires a top-level 'listeners' array. See the README for the migration guide.` — the configuration uses the pre-v1.4 format. Rewrite it into the `listeners` format described in [Configuration](#configuration).
- `Configuration file was not found: {path}` — the configuration file is missing at that path. For host execution, check `NICHELLM_CONFIG_PATH`; in Docker, check the `config.jsonc` mount.
- `Upstream API key environment variable '{api_key_env}' is not set.` — the variable named by `api_key_env` is not set. Export it in your shell, or set it in the `.env` file next to the Compose file.
- `Skipped the listener on port {port} because mode '{mode}' requires a '{section}' section, which is missing from the configuration.` — the listener entry with that port is configured in a mode that requires its mode-specific key, but the key is missing, so that listener is not started. Add the missing key (see [Modes and features](#modes-and-features)) or change the entry's `mode` to `passthrough`. Other listeners start normally.
- A setting seems to have no effect — unknown top-level keys, a mode-specific block that does not belong to the entry's `mode`, and `logging.file` settings while `logging.file.enabled` is `false` are ignored with a startup warning on the standard error output, prefixed with `[port N]` for per-listener warnings. Check the warning lines shown at startup and the key spelling (for example `timouts` instead of `timeouts`).

Error messages the proxy generates are localized with `NICHELLM_LANGUAGE` (English by default, Japanese with `ja`).

## Changelog

### v1.4.3 (2026-09-16)

- Bug fixes: image generation relays (`gemini-image` and `grok-image` modes) no longer forward `Accept-Encoding` from the client or `Content-Encoding` from the upstream, so a gzip-compressed response is no longer relayed as a plain body declared as gzip (which made clients fail to decode a response the proxy itself had logged as 200 OK) and the HTTP client only advertises encodings it can decode. `gemini-image` mode now also strips the Azure OpenAI-style `api-version` query parameter that the Gemini endpoint rejects with HTTP 400; other query parameters are preserved and `grok-image` mode is unchanged.

### v1.4.2 (2026-09-16)

- Bug fix: image generation relays (`gemini-image` and `grok-image` modes) forwarded the client's original `Content-Length` header even when the request body was transformed, which could make the upstream reject the request with a protocol error and surface as a 500 response. The relay now lets the HTTP client recalculate `Content-Length` from the transformed body so it always matches the bytes sent.

### v1.4.1 (2026-09-15)

- Behavior change: a listener entry in the `grok-image`, `gemini-image`, or `featherless` mode whose mode-specific key (`grok_image`, `gemini_image`, or `featherless`) is missing is now skipped with a startup warning naming the port, mode, and missing key. Previously a missing `grok_image` or `gemini_image` key started the listener with defaults, and a missing `featherless` key crashed during startup. Other listeners start normally; when every listener is skipped, startup fails with a configuration error.
- Added a ruff lint gate to `tools/check.sh`; ruff is now a development dependency pinned in `uv.lock`.
- Added real-process startup smoke tests covering `/health`, the startup output, skip warnings, and SIGINT shutdown.

### v1.4.0 (2026-09-15)

- Breaking configuration change: the top-level `listener`, `upstream`, and `timeouts` blocks are replaced by a required top-level `listeners` array whose entries each describe one complete listener (port, mode, upstream, timeouts, mode-specific block, and features); pre-v1.4 files are rejected at startup with an error pointing to the migration guide in this README.
- One process now serves multiple listeners in different modes concurrently (ports must be unique), configuration files accept JSONC comments, `GET /health` is served per listener, protocol-logging records carry a `listener_port` field, and the startup output lists every listener.

Full history: [CHANGELOG.md](CHANGELOG.md)

## License

This project is licensed under the MIT License — see [LICENSE](LICENSE).