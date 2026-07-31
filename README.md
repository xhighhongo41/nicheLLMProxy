# nicheLLM Proxy

nicheLLM Proxy relays HTTP requests and responses without transformation between an OpenAI-compatible client and an upstream LLM provider. Version 1.0 supports one listener in a trusted network and adds opt-in structured protocol logging.

[日本語版 README](README_ja.md)

## HTTP transport supported in v1.0

- Pass-through forwarding for `GET`, `POST`, `PUT`, `PATCH`, `DELETE`, `HEAD`, and `OPTIONS`, preserving the request method, path, query, and body.
- One raw HTTP pipeline for JSON, `text/event-stream` (SSE), multipart uploads, and binary request and response bodies. The proxy does not parse or reconstruct endpoint-specific bodies or SSE events.
- Pass-through upstream status codes, including HTTP errors and `206 Partial Content` range responses, plus end-to-end response headers. Repeated end-to-end headers are retained.
- Replace a client-supplied `Authorization` header with the configured upstream Bearer API key. Apart from `Host`, hop-by-hop headers, and the received `Authorization`, end-to-end request headers are forwarded.
- `GET /health` for proxy liveness checks, and safe proxy-generated 5xx responses for upstream connection and read failures.
- User-facing proxy messages in English (default) or Japanese.
- Optional JSON Lines protocol logs for request, upstream-response, completion, failure, and cancellation events. Logs go to stdout for `docker compose logs -f` and optionally to a rotating file.

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
- Webhook receiving or signature verification, Administration API operations, request transformation, ruri mode, rate limiting, proxy authentication, TLS termination, or multiple listeners.
- Safe direct exposure to the public internet.

The following APIs are not newly recommended or individually transport-tested in v0.3: Assistants (`/v1/assistants`, `/v1/threads`, `/v1/runs`), Videos API / Sora 2, Reusable Prompts, Evals API / Agent Builder, Legacy Completions, and Images Variations. The wildcard route may mechanically forward a path, but this does not make it a supported or recommended API.

## Configuration

Do not put an API key value in the configuration JSON. Specify only the environment variable that contains it.

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

|Environment variable|Required|Purpose|
|---|---|---|
|`UPSTREAM_API_KEY`|Yes|API key sent to the upstream provider. Its name must match `api_key_env`.|
|`NICHELLM_CONFIG_PATH`|No|Path to the configuration JSON. The default is `/app/config/config.json`; set it for host execution.|
|`NICHELLM_LANGUAGE`|No|Language for proxy-generated messages: `en` (default) or `ja`. Values such as `ja-JP` are treated as `ja`; unsupported values fall back to English.|

The proxy replaces a client-supplied `Authorization` header with the configured upstream Bearer API key and does not forward the received value. It preserves repeated end-to-end headers. It does not apply a general credential-scrubbing policy to other provider-specific headers; operate it only in a trusted network.

## Protocol logging

`listener.features` may be omitted for v0.3-compatible operation, or may contain one `logging` feature. v1.0 rejects duplicate or unknown features and invalid logging settings at startup.

- `stdout: true` writes UTF-8 JSON Lines to the container standard output. Follow them with `docker compose logs -f nichellm-proxy`.
- `file.enabled: true` writes the same records to `file.path`. The path must be absolute. `max_bytes` and `backup_count` must both be positive; the default example retains the active 10 MiB file plus five rotated files (about 60 MiB total).
- `capture.bodies` is **false by default**. When true, the proxy captures at most `max_body_bytes` (default 1 MiB, maximum 10 MiB) of textual JSON, text, or SSE request and response bodies. It never delays or reconstructs the forwarded stream.
- Multipart and binary bodies are not stored. Their byte count, SHA-256 digest, and omission reason are recorded instead. Truncated textual bodies are marked in the record.
- `Authorization`, proxy authorization, cookies, API-key headers, and names containing `token`, `secret`, `password`, or `api_key` are redacted. Add project-specific names to the three `redaction` arrays. JSON redaction cannot reliably find secrets or personal data embedded in free-form prompts or tool output.

Enabling body capture intentionally stores user prompts and model output. Use it only in a trusted environment, restrict access to the log volume, and set an operational retention/deletion policy.

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

The example file path is intended for the Docker volume. For host execution, either set `file.enabled` to `false` or change `file.path` to an absolute directory writable by your user.

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

The `nichellm-proxy-logs` named volume persists `/var/log/nichellm` across container recreation. Inspect the current file without printing sensitive bodies to shared terminals:

```bash
docker compose logs -f nichellm-proxy
docker compose exec nichellm-proxy sh -c 'ls -lh /var/log/nichellm'
```

To delete retained logs deliberately, stop the service and remove the named volume. `docker compose down` alone does not remove it.

## Security

- Never commit API keys. Keep real values only in environment variables, `.env`, or Docker secrets.
- Do not add `.env` or local `config.json` files to Git.
- Treat protocol logs as sensitive data. Body capture is opt-in but can contain prompts, model output, and personal data; protect and delete the named volume according to your policy.
- v0.3 has no proxy authentication or TLS termination. Keep it inside a trusted network and do not expose it directly to the internet.
- An upstream API's semantic compatibility, authorization policy, model availability, and account eligibility are not guaranteed by HTTP pass-through. In particular, Fine-tuning Job eligibility is determined by the upstream account.

## Tests

The test suite uses a simulated upstream; it does not contact an external LLM provider or use a real API key. It covers representative JSON, Responses SSE, multipart, binary, Range/206, duplicate-header, error, lifecycle, redaction, bounded capture, and rotation cases.

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

Release tags are published as multi-platform (`linux/amd64`, `linux/arm64`) images at `<DOCKERHUB_USERNAME>/nichellm-proxy`. Use an exact version tag in production:

```bash
docker pull <DOCKERHUB_USERNAME>/nichellm-proxy:1.0.0
```

Maintainers: create a public Docker Hub repository named `nichellm-proxy`, create an expiring Read & Write Docker Hub personal access token, and store it as the GitHub Actions secret `DOCKERHUB_TOKEN`. Store the Docker Hub username as the GitHub Actions variable `DOCKERHUB_USERNAME`. Pushing an annotated `vX.Y.Z` Git tag runs tests and then publishes `X.Y.Z`, `X.Y`, and `latest`, including SBOM and provenance. Never commit the token.

## Release history

### v1.0.0 (2026-07-31)

- Added the opt-in structured logging feature with stdout JSON Lines, file output, size-based rotation, bounded body capture, and credential redaction.
- Added a persistent Docker Compose log volume and GitHub Actions workflows for tests, image builds, and Docker Hub multi-platform publication.

### v0.3.0 (2026-07-25)

- Added raw HTTP pass-through coverage for JSON, Responses HTTP SSE, multipart, binary, Range/206, and repeated end-to-end headers.
- Added the representative OpenAI API-family table and clear exclusions for bidirectional transports, protocol conversion, and deprecated or legacy APIs.
- Clarified Authorization replacement, read-timeout behavior, and the boundary between HTTP transport and upstream semantic compatibility.

### v0.2.0 (2026-07-24)

- Added English (default) and Japanese user-facing proxy messages through `gettext`.
- Added the English primary README and the equivalent Japanese README.
- Verified Docker Compose execution and the Japanese proxy-generated error response.
