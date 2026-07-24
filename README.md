# nicheLLM Proxy

nicheLLM Proxy relays HTTP requests and responses without transformation between an OpenAI-compatible client and an upstream LLM provider. Version 0.3 supports one listener in a trusted network.

[日本語版 README](README_ja.md)

## HTTP transport supported in v0.3

- Pass-through forwarding for `GET`, `POST`, `PUT`, `PATCH`, `DELETE`, `HEAD`, and `OPTIONS`, preserving the request method, path, query, and body.
- One raw HTTP pipeline for JSON, `text/event-stream` (SSE), multipart uploads, and binary request and response bodies. The proxy does not parse or reconstruct endpoint-specific bodies or SSE events.
- Pass-through upstream status codes, including HTTP errors and `206 Partial Content` range responses, plus end-to-end response headers. Repeated end-to-end headers are retained.
- Replace a client-supplied `Authorization` header with the configured upstream Bearer API key. Apart from `Host`, hop-by-hop headers, and the received `Authorization`, end-to-end request headers are forwarded.
- `GET /health` for proxy liveness checks, and safe proxy-generated 5xx responses for upstream connection and read failures.
- User-facing proxy messages in English (default) or Japanese.

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

## Not supported

- WebSocket, WebRTC, or SIP transport, including Realtime API and the Responses WebSocket mode. HTTP SSE is supported, but it is not a bidirectional WebSocket replacement.
- Protocol conversion or provider adapters, including OpenAI/Anthropic protocol conversion and Azure, Gemini, or other provider-specific authentication or URL conversion.
- Webhook receiving or signature verification, Administration API operations, request transformation, ruri mode, logging, rate limiting, proxy authentication, TLS termination, or multiple listeners.
- Safe direct exposure to the public internet.

The following APIs are not newly recommended or individually transport-tested in v0.3: Assistants (`/v1/assistants`, `/v1/threads`, `/v1/runs`), Videos API / Sora 2, Reusable Prompts, Evals API / Agent Builder, Legacy Completions, and Images Variations. The wildcard route may mechanically forward a path, but this does not make it a supported or recommended API.

## Configuration

Do not put an API key value in the configuration JSON. Specify only the environment variable that contains it.

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

|Environment variable|Required|Purpose|
|---|---|---|
|`UPSTREAM_API_KEY`|Yes|API key sent to the upstream provider. Its name must match `api_key_env`.|
|`NICHELLM_CONFIG_PATH`|No|Path to the configuration JSON. The default is `/app/config/config.json`; set it for host execution.|
|`NICHELLM_LANGUAGE`|No|Language for proxy-generated messages: `en` (default) or `ja`. Values such as `ja-JP` are treated as `ja`; unsupported values fall back to English.|

The proxy replaces a client-supplied `Authorization` header with the configured upstream Bearer API key and does not forward the received value. It preserves repeated end-to-end headers. It does not apply a general credential-scrubbing policy to other provider-specific headers; operate it only in a trusted network.

## Timeouts and long-running work

`connect_seconds` limits the time to establish an upstream connection. `read_seconds` limits the wait for the next byte from the upstream; it is not a limit on the total duration of a response that continues to deliver data. Keep the configured timeout for HTTP SSE and ordinary HTTP responses.

For background responses, batches, and fine-tuning jobs, create the job and poll its status from the client instead of holding one proxy connection indefinitely. Realtime and Responses WebSocket workloads require a separate bidirectional transport design and are not supported by v0.3.

## Run locally with uv

[uv](https://docs.astral.sh/uv/) creates and uses a project-local virtual environment.

```bash
cp config.example.json config.json
# Set upstream.base_url in config.json for the provider you use.
export UPSTREAM_API_KEY='your-upstream-api-key'
export NICHELLM_CONFIG_PATH="$PWD/config.json"
export NICHELLM_LANGUAGE=ja  # optional; English is the default
uv sync --dev
uv run niche-llm-proxy
```

From another terminal, check the proxy:

```bash
curl http://127.0.0.1:8000/health
```

## Run with Docker Compose

The Docker image does not contain an API key or configuration JSON. Create them on the host before starting the service.

```bash
cp config.example.json config.json
# Set upstream.base_url in config.json.
export UPSTREAM_API_KEY='your-upstream-api-key'
export NICHELLM_LANGUAGE=ja  # optional; English is the default
docker compose up --build -d
curl http://127.0.0.1:8000/health
```

Compose mounts `config.json` read-only at `/app/config/config.json` and publishes the service only on `127.0.0.1:8000`. Stop the service with:

```bash
docker compose down
```

## Security

- Never commit API keys. Keep real values only in environment variables, `.env`, or Docker secrets.
- Do not add `.env` or local `config.json` files to Git.
- v0.3 has no proxy authentication or TLS termination. Keep it inside a trusted network and do not expose it directly to the internet.
- An upstream API's semantic compatibility, authorization policy, model availability, and account eligibility are not guaranteed by HTTP pass-through. In particular, Fine-tuning Job eligibility is determined by the upstream account.

## Tests

The test suite uses a simulated upstream; it does not contact an external LLM provider or use a real API key. It covers representative JSON, Responses SSE, multipart, binary, Range/206, duplicate-header, error, and lifecycle transport cases.

```bash
uv sync --dev
uv run pytest
```

## Translation catalogs

The runtime uses Python's standard `gettext` module. English message IDs are the fallback, and the Japanese catalog is stored in `src/niche_llm_proxy/locales/ja/LC_MESSAGES/`. After editing the `.po` file, regenerate the versioned `.mo` catalog with GNU gettext:

```bash
msgfmt --check \
  --output-file src/niche_llm_proxy/locales/ja/LC_MESSAGES/niche_llm_proxy.mo \
  src/niche_llm_proxy/locales/ja/LC_MESSAGES/niche_llm_proxy.po
```

## Docker Hub

v0.3 does not publish an image to Docker Hub. Container registry publication, tags, and CI-based distribution are planned for v1.0.

## Release history

### v0.3.0 (2026-07-25)

- Added raw HTTP pass-through coverage for JSON, Responses HTTP SSE, multipart, binary, Range/206, and repeated end-to-end headers.
- Added the representative OpenAI API-family table and clear exclusions for bidirectional transports, protocol conversion, and deprecated or legacy APIs.
- Clarified Authorization replacement, read-timeout behavior, and the boundary between HTTP transport and upstream semantic compatibility.

### v0.2.0 (2026-07-24)

- Added English (default) and Japanese user-facing proxy messages through `gettext`.
- Added the English primary README and the equivalent Japanese README.
- Verified Docker Compose execution and the Japanese proxy-generated error response.
