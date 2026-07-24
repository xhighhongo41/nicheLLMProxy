# nicheLLM Proxy

nicheLLM Proxy relays HTTP requests and responses without transformation between an OpenAI-compatible client and an upstream LLM provider. Version 0.2 supports one listener in a trusted network.

[日本語版 README](README_ja.md)

## Supported in v0.2

- Pass-through forwarding for `GET`, `POST`, `PUT`, `PATCH`, `DELETE`, `HEAD`, and `OPTIONS`, preserving the request path, query, and body.
- Relaying ordinary HTTP responses and streaming `text/event-stream` (SSE) responses without buffering or reconstruction.
- Passing through upstream HTTP error status codes and bodies; returning safe 5xx responses for upstream connection and read failures.
- `GET /health` for proxy liveness checks.
- User-facing proxy messages in English (default) or Japanese.

## Not supported

- Protocol conversion, request transformation, ruri mode, logging, rate limiting, proxy authentication, TLS termination, or multiple listeners.
- Safe direct exposure to the public internet.

The semantic compatibility of individual upstream endpoints and features depends on the upstream provider.

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

The proxy replaces a client-supplied `Authorization` header with the configured upstream API key. It does not translate or modify upstream response bodies, SSE events, headers, or status codes.

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

Compose mounts `config.json` read-only at `/app/config/config.json` and publishes the service only on `127.0.0.1:8000`. Dockerfile builds and Docker Compose startup have been verified in a Docker-capable environment. Stop the service with:

```bash
docker compose down
```

## Security

- Never commit API keys. Keep real values only in environment variables, `.env`, or Docker secrets.
- Do not add `.env` or local `config.json` files to Git.
- v0.2 has no proxy authentication or TLS termination. Keep it inside a trusted network and do not expose it directly to the internet.

## Tests

Tests do not contact an external LLM provider or use a real API key.

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

v0.2 does not publish an image to Docker Hub. Container registry publication, tags, and CI-based distribution are planned for v1.0.

## Release history

### v0.2.0 (2026-07-24)

- Added English (default) and Japanese user-facing proxy messages through `gettext`.
- Added the English primary README and the equivalent Japanese README.
- Verified Docker Compose execution and the Japanese proxy-generated error response.
