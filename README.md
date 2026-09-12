# nicheLLM Proxy

nicheLLM Proxy relays HTTP requests and responses between an OpenAI-compatible client and an upstream LLM provider. The `passthrough` mode forwards without transformation, the `grok-image` mode added in v1.1 presents Grok (xAI) image generation through an OpenAI-compatible interface, the `gemini-image` mode added in v1.2 presents Google Gemini image generation through an OpenAI-compatible interface, and the `featherless` mode added in v1.3 presents featherless.ai through a model whitelist and per-key concurrent-request queueing. All modes support one listener in a trusted network and opt-in structured protocol logging.

[日本語版 README](README_ja.md)

## Installation

### Requirements

- Docker with the `docker compose` plugin (recommended), or
- Python 3.11 or later with [uv](https://docs.astral.sh/uv/) for host execution.
- Git, to clone and update the repository.

An upstream LLM provider account with an API key is required in every setup. See [Configuration](#configuration) for the configuration JSON file. In `featherless` mode the proxy itself holds no API key: each client sends its own `Authorization` header (see [featherless mode](#featherless-mode)).

### Run with Docker Compose (from source)

The Docker image contains no API key or configuration JSON. Create them on the host before starting the service.

```bash
git clone https://github.com/xhighhongo41/nicheLLMProxy.git
cd nicheLLMProxy
cp config.example.json config.json
# If you use a provider other than OpenAI, change upstream.base_url in config.json.
export UPSTREAM_API_KEY='your-upstream-api-key'
export NICHELLM_LANGUAGE=ja  # optional; English is the default
docker compose up --build -d
```

The bundled `docker-compose.yml` requires `UPSTREAM_API_KEY` to be set even when `api_key_env` names a different variable (for example `XAI_API_KEY`); set any value for it, or adjust the `environment` entries to your configuration. In `featherless` mode no API key variable is needed at all; set any value for `UPSTREAM_API_KEY`, or adjust the `environment` entries. The Compose file includes a healthcheck that probes `GET /health` every 30 seconds; `docker compose ps` shows the service as `healthy` once it is ready. Check the proxy:

```bash
curl http://127.0.0.1:8000/health
```

Compose mounts `config.json` read-only at `/app/config/config.json` and publishes the service only on `127.0.0.1:8000`; the proxy has no authentication, so keep this binding on trusted networks. To let other machines on your network connect, change the host-side binding in the `ports` mapping (for example `"8000:8000"`), only on networks you trust. Stop the service with:

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
docker pull xhighhongo41/nichellm-proxy:1.3.1
```

The image contains no API key or configuration JSON. Create a `.env` file next to the Compose file with the API key variables your configuration uses (see [API key management](#api-key-management)), and put a `config.json` (start from the full example in [Configuration](#configuration)) and this Compose file in a working directory:

```yaml
services:
  nichellm-proxy:
    image: xhighhongo41/nichellm-proxy:1.3.1
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

Then start it:

```bash
docker compose up -d
curl http://127.0.0.1:8000/health
```

This minimal example omits the healthcheck included in the bundled `docker-compose.yml`; the `curl` above can fail while the container is still starting — retry after a few seconds. The API key variables above are optional references; the proxy verifies at startup that the variable named by `api_key_env` in your `config.json` is actually set. The named log volume behaves the same as in the Compose-from-source setup, including removal with `docker compose down -v`.

### Run locally with uv

[uv](https://docs.astral.sh/uv/) creates and uses a project-local virtual environment.

```bash
git clone https://github.com/xhighhongo41/nicheLLMProxy.git
cd nicheLLMProxy
cp config.example.json config.json
# If you use a provider other than OpenAI, change upstream.base_url in config.json.
export UPSTREAM_API_KEY='your-upstream-api-key'
export NICHELLM_CONFIG_PATH="$PWD/config.json"
export NICHELLM_LANGUAGE=ja  # optional; English is the default
uv sync
uv run niche-llm-proxy
```

The `file.path` in the example is intended for the Docker volume. For host execution, either set `file.enabled` to `false` or change `file.path` to an absolute directory writable by your user.

From another terminal, check the proxy:

```bash
curl http://127.0.0.1:8000/health
```

Host execution listens on all interfaces (`0.0.0.0`), unlike the Compose setups, which publish only `127.0.0.1:8000`. Keep this in mind on shared networks and restrict access with a firewall if necessary. Stop the foreground proxy with Ctrl-C.

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

The configuration JSON format is unchanged in v1.3.1; existing `config.json` files keep working. v1.3.1 allows `timeouts.read_seconds: null` to disable the upstream read timeout; see [Timeouts](#timeouts).

Docker Compose from source:

```bash
git pull
docker compose up --build -d
```

Published Docker Hub image: update the image tag in your Compose file (for example `xhighhongo41/nichellm-proxy:1.3.1`), then:

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

The proxy reads one mandatory configuration JSON file. Four templates are included in the repository (fetch them from GitHub when you run only the published image):

- `config.example.json` — `passthrough` mode (shown below)
- `config.grok-image.example.json` — `grok-image` mode
- `config.gemini-image.example.json` — `gemini-image` mode
- `config.featherless.example.json` — `featherless` mode

Inside a container, the default path is `/app/config/config.json`; the Docker Compose setups above mount your local `config.json` there. For host execution, set the path with `NICHELLM_CONFIG_PATH`.

The `passthrough` example:

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

The `file.path` in the example is intended for the Docker volume. For host execution, either disable file logging or change it to a writable absolute directory (see [Run locally with uv](#run-locally-with-uv)).

Common keys:

|Key|Type / constraint|Default|
|---|---|---|
|`listener.port`|integer 1–65535 (required)|—|
|`listener.mode`|`passthrough`, `grok-image`, `gemini-image`, or `featherless` (required)|—|
|`upstream.base_url`|http/https URL without query or fragment (required)|—|
|`upstream.api_key_env`|non-empty string; the name of the environment variable holding the API key (required except in `featherless` mode, where it must be omitted)|—|
|`timeouts.connect_seconds`|positive number|10.0|
|`timeouts.read_seconds`|positive number or `null`|120.0|

The `timeouts` object itself is optional, and every key with a default value can be omitted. If you change `listener.port`, also update the Compose `ports` mapping so the container-side port matches `listener.port` (for example `"127.0.0.1:8001:8001"`); the healthcheck in the bundled `docker-compose.yml` probes container port 8000, so it stays accurate only while `listener.port` is 8000.

Mode-specific keys (`listener.grok_image`, `listener.gemini_image`, `listener.featherless`) and the optional `listener.features` logging feature are described in [Modes and features](#modes-and-features). Unknown keys inside the mode-specific objects, invalid values, and mode-mismatched settings are rejected as startup configuration errors. Outside the mode-specific objects and the logging feature settings, unknown keys are silently ignored — check the key spelling (for example `timouts` instead of `timeouts`) if a setting seems to have no effect. Complete mode examples are available as `config.grok-image.example.json`, `config.gemini-image.example.json`, and `config.featherless.example.json`.

### Environment variables

|Environment variable|Required|Purpose|
|---|---|---|
|`UPSTREAM_API_KEY`|Yes|API key sent to the upstream provider. Its name must match `api_key_env`.|
|`XAI_API_KEY`|No|API key sent to xAI by the `grok-image` mode example. Its name must match `api_key_env`.|
|`GEMINI_API_KEY`|No|API key sent to Google by the `gemini-image` mode example. Its name must match `api_key_env`.|
|`NICHELLM_CONFIG_PATH`|No|Path to the configuration JSON. The default is `/app/config/config.json`; set it for host execution.|
|`NICHELLM_LANGUAGE`|No|Language for proxy-generated messages: `en` (default) or `ja`. Values such as `ja-JP` are treated as `ja`; unsupported values fall back to English.|

The three API key variables are the names referenced by `api_key_env` in the bundled configuration examples. The variable name itself is configurable: whatever `api_key_env` names must be set before the proxy starts. The `featherless` mode reads no API key environment variable: each client sends its own `Authorization` header, which the proxy relays to featherless.ai unchanged.

These are the only environment variables the proxy reads. The configuration JSON cannot be replaced or overridden wholesale through environment variables.

### API key management

Do not put an API key value in the configuration JSON. Specify only the name of the environment variable that contains it (`upstream.api_key_env`), and set the actual value in the environment:

```bash
export UPSTREAM_API_KEY='your-upstream-api-key'
```

For Docker Compose, a `.env` file next to the Compose file is a convenient place for the real values; Compose reads it automatically. `.env.example` in the repository lists the API key and language variables from the table above.

The proxy replaces a client-supplied `Authorization` header with the configured upstream Bearer API key and does not forward the received value, so clients never need the real upstream key. In `featherless` mode this replacement does not happen: the proxy holds no upstream API key, must not define `api_key_env`, and relays each client's `Authorization` header unchanged, so every client needs its own featherless.ai API key (see [featherless mode](#featherless-mode)).

### Timeouts

`connect_seconds` limits the time to establish an upstream connection. `read_seconds` limits the wait for the next byte from the upstream; it is not a limit on the total duration of a response that continues to deliver data. Keep the configured timeout for HTTP SSE and ordinary HTTP responses. Set `read_seconds` to `null` to disable the read timeout and wait for the upstream without a limit; `connect_seconds` must always be a positive number.

For background responses, batches, and fine-tuning jobs, create the job and poll its status from the client instead of holding one proxy connection indefinitely. Realtime and Responses WebSocket workloads require a separate bidirectional transport design and are not supported.

## Modes and features

`listener.mode` accepts `passthrough`, `grok-image`, `gemini-image`, or `featherless`. `GET /health` works in every mode. Upstream errors are passed through with their status and body unchanged, and upstream connection and read failures return the same 502/504 responses as `passthrough`.

|Mode / feature|Function|Settings|
|---|---|---|
|`passthrough`|Relays OpenAI-compatible APIs without transformation|`upstream`|
|`grok-image`|Presents Grok (xAI) image generation through an OpenAI-compatible interface|`listener.grok_image`|
|`gemini-image`|Presents Google Gemini image generation through an OpenAI-compatible interface|`listener.gemini_image`|
|`featherless`|Relays featherless.ai with a model whitelist and per-key concurrent-request queueing|`listener.featherless`|
|`logging` feature|Structured protocol logging, available in every mode|`listener.features`|

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

`listener.grok_image` is optional and only accepted in `grok-image` mode. It supplies defaults that a direct request value overrides:

- `default_model`: used when the request has no `model`. If neither is present, the proxy returns HTTP 400.
- `aspect_ratio`: added when the request omits `aspect_ratio` (for example `1:1` or `16:9`).
- `resolution`: added when the request omits `resolution`. It must be `1k` or `2k`.

Complete `grok-image` example:

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

`listener.gemini_image` is optional and only accepted in `gemini-image` mode. It supplies defaults that a direct request value overrides:

- `default_model`: used when the request has no `model`. If neither is present, the proxy returns HTTP 400.
- `aspect_ratio`: added when the request has neither `size` nor `aspect_ratio` (for example `1:1` or `16:9`).

Complete `gemini-image` example:

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

#### `listener.featherless` settings

`listener.featherless` is required in `featherless` mode and rejected in the other modes:

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
  "listener": {
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
    "base_url": "https://api.featherless.ai"
  },
  "timeouts": {
    "connect_seconds": 10,
    "read_seconds": 120
  }
}
```

### logging feature

`listener.features` may be omitted to run without protocol logging, or may contain one `logging` feature. The proxy rejects duplicate or unknown features and invalid logging settings at startup.

- `stdout: true` (the default) writes UTF-8 JSON Lines to the standard output. In a container, follow them with `docker compose logs -f nichellm-proxy`.
- `file.enabled: true` writes the same records to `file.path`. The path must be absolute. `max_bytes` and `backup_count` must both be positive; the default example retains the active 10 MiB file plus five rotated files (about 60 MiB total).
- `capture.bodies` is **false by default**. When true, the proxy captures at most `max_body_bytes` (default 1 MiB, maximum 10 MiB) of textual JSON, text, or SSE request and response bodies. It never delays or reconstructs the forwarded stream.
- Multipart and binary bodies are not stored. Their byte count, SHA-256 digest, and omission reason are recorded instead. Truncated textual bodies are marked in the record.
- `Authorization`, proxy authorization, cookies, API-key headers, and names containing `token`, `secret`, `password`, or `api_key` are redacted. Add project-specific names to the three `redaction` arrays. JSON redaction cannot reliably find secrets or personal data embedded in free-form prompts or tool output.

The `grok-image` and `gemini-image` modes additionally emit an `upstream_request_sent` event — with the byte count and SHA-256 digest of the transformed request — at the moment the request is sent upstream.

Enabling body capture intentionally stores user prompts and model output. Use it only in a trusted environment, restrict access to the log volume, and set an operational retention/deletion policy.

### Not supported

Besides the relay behavior described above, the proxy provides no protocol conversion or provider adapters other than the `grok-image` and `gemini-image` conversions. It also does not provide webhook receiving or signature verification, Administration API operations, rate limiting, proxy authentication, TLS termination, or multiple listeners. The `featherless` mode manages API keys only on the client side; proxy-side key management and a whitelist administration API are not provided.

## Security

- nicheLLM Proxy has no proxy authentication or TLS termination. Anyone who can reach the proxy can use your upstream API key and its quota through it. Operate it only inside a trusted network.
- Do not expose it directly to the internet. To use it across networks, place your own reverse proxy with TLS termination and authentication in front of it. Note that host execution (uv) listens on all interfaces (`0.0.0.0`), while the bundled Compose setups publish only `127.0.0.1:8000`.
- Treat protocol logs as sensitive data. Body capture is opt-in but can contain prompts, model output, and personal data. Restrict access to the log volume and set an operational retention/deletion policy.

## Troubleshooting

Representative startup errors and what to check:

- `Configuration file was not found: {path}` — the configuration JSON is missing at that path. For host execution, check `NICHELLM_CONFIG_PATH`; in Docker, check the `config.json` mount.
- `Upstream API key environment variable '{api_key_env}' is not set.` — the variable named by `api_key_env` is not set. Export it in your shell, or set it in the `.env` file next to the Compose file.
- A setting seems to have no effect — unknown keys outside the mode-specific objects and the logging feature settings are silently ignored. Check the key spelling (for example `timouts` instead of `timeouts`).

Error messages the proxy generates are localized with `NICHELLM_LANGUAGE` (English by default, Japanese with `ja`).

## Changelog

### v1.3.0 (2026-09-12)

- Added the `featherless` listener mode, which presents featherless.ai through an OpenAI-compatible interface with a model whitelist: `GET /v1/models` lists only the configured `model_whitelist`, and other endpoints are relayed with the client's `Authorization` header (the proxy holds no API key).
- Added per-API-key concurrent-request gating for `featherless` mode: the plan limit is fetched from `GET /v1/plan` (overridable with `concurrency_limit`), actual usage is tracked through `GET /account/concurrency` snapshots, and requests that would exceed the limit are queued first-in-first-out up to `max_queue_wait_seconds` (default 60) before answering HTTP 429.

Full history: [CHANGELOG.md](CHANGELOG.md)

## License

This project is licensed under the MIT License — see [LICENSE](LICENSE).