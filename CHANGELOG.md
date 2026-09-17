# Changelog

### v1.5.0 (2026-09-17)

- Added the `grok-image-edit` mode: presents Grok (xAI) image editing (`POST /v1/images/edits`) through an OpenAI-compatible interface. OpenAI-style `multipart/form-data` edit requests are translated to the xAI JSON edit API, with file parts converted to base64 data URIs (`image`/`image[]` → `image`/`images`), the usual OpenAI-only parameter removal, `aspect_ratio`/`resolution` defaults, and `created` completion. `GET /v1/models` and `GET /v1/image-generation-models` pass through. This lets OpenAI SDK clients (including `images.edit()`, which the xAI API rejects natively for being multipart) and Open WebUI edit images via Grok. Masks are rejected with HTTP 400 because xAI has no mask support.
- No `gemini-image-edit` mode: the Gemini API OpenAI compatibility layer has no image edit endpoint; `POST /v1beta/openai/images/edits` answered HTTP 404 when verified on 2026-09-17.

### v1.4.3 (2026-09-16)

- Bug fix: image generation relays (`gemini-image` and `grok-image` modes) read the upstream response with automatic decompression but still forwarded the upstream `Content-Encoding` header, so a gzip-compressed response was relayed as a plain body declared as gzip; clients that trust the header (for example Open WebUI) failed to decode a response the proxy itself had logged as 200 OK. The relays now omit `Content-Encoding` from the forwarded response headers, matching the decompressed bytes they actually send; `Content-Length` continues to be recalculated from the final body.
- Bug fix: the same relays forwarded the client's `Accept-Encoding` header verbatim, so an advertised but undecodable encoding (for example `br` or `zstd` without the optional codecs installed) made the HTTP client return undecoded bytes. The relays now omit `Accept-Encoding` from forwarded request headers so the HTTP client advertises exactly the encodings it can decode.
- Bug fix: `gemini-image` mode now strips the Azure OpenAI-style `api-version` query parameter (case-insensitive; other query parameters are preserved) before forwarding, because the Gemini endpoint rejects unknown query parameters with HTTP 400 INVALID_ARGUMENT ("Cannot bind query parameter"). `grok-image` mode keeps forwarding the query string unchanged because xAI accepts it.

### v1.4.2 (2026-09-16)

- Bug fix: image generation relays (`gemini-image` and `grok-image` modes) forwarded the client's original `Content-Length` header even when the request body was rewritten for the upstream (adding `response_format`, `model`, or `aspect_ratio` fields), so the declared length could disagree with the bytes actually sent, making the upstream abort the request with a protocol error that surfaced as a 500 response. The relay now forwards headers without `Content-Length` so the HTTP client derives it from the transformed body, and both README changelogs document the fix.

### v1.4.1 (2026-09-15)

- Behavior change: a listener entry in the `grok-image`, `gemini-image`, or `featherless` mode whose mode-specific key (`grok_image`, `gemini_image`, or `featherless`) is missing is now skipped with a startup warning naming the port, mode, and missing key. Previously a missing `grok_image` or `gemini_image` key started the listener with default settings, and a missing `featherless` key crashed the process during startup. Other listeners start normally; when every listener is skipped this way, startup fails with a configuration error listing the skipped ports.
- Added a ruff lint gate to `tools/check.sh`: ruff is now a development dependency pinned in `uv.lock`, and CI runs the same check through `tools/check.sh`.
- Added real-process startup smoke tests (`tests/test_startup_smoke.py`) covering the `/health` response, the startup output, skip warnings, and graceful SIGINT shutdown.

### v1.4.0 (2026-09-15)

- Breaking configuration change: the top-level `listener`, `upstream`, and `timeouts` blocks were replaced by a required top-level `listeners` array whose entries each describe one port with its own complete settings (port, mode, upstream, timeouts, mode-specific blocks, and features). Pre-v1.4 files are rejected at startup with an error pointing to the README migration guide instead of being migrated automatically.
- Added multi-listener support: one proxy process can now serve several ports in different modes at the same time, sharing a single event loop. Listener ports must be unique; duplicate ports fail startup.
- Added JSONC support to configuration files: `//` line comments and `/* */` block comments are accepted in any configuration file regardless of its extension, including comments inside string values such as URLs, which are preserved. The default configuration path now prefers `/app/config/config.jsonc` and falls back to `/app/config/config.json`; setting `NICHELLM_CONFIG_PATH` explicitly uses exactly that path.
- Extended `GET /health` per listener: each port now reports its own `status`, `version`, `mode`, and `features`.
- Added the `listener_port` field to every protocol-logging record so entries from concurrent listeners can be told apart. Multiple listeners logging to the same file produce a startup warning instead of an error.
- Reworked the startup output: the proxy prints one aggregate line with the version and listener count followed by one line per listener (port, mode, features), and prefixes per-listener configuration warnings with `[port N]` on the standard error output.
- Updated the bundled examples to the new schema as commented `.jsonc` files and added `config.multi-listener.example.jsonc` combining all four modes; the bundled `docker-compose.yml` now mounts `config.jsonc`, publishes one port mapping per listener, and its healthcheck probes every listener port read from the configuration. The `Dockerfile` no longer pins `NICHELLM_CONFIG_PATH` to `config.json` and no longer declares `EXPOSE 8000`, so the container adapts to any configured port set.

### v1.3.2 (2026-09-15)

- Announced the startup configuration: the proxy now prints a single line to the standard output naming the proxy version, the listener mode, and the enabled features (stating `no features` when none are enabled), and prints each configuration warning to the standard error output.
- Extended `GET /health` to report the proxy version, the listener mode, and the enabled feature names in the `version`, `mode`, and `features` fields, in addition to `status`.
- Relaxed the handling of mode-mismatched settings, a behavior change: a mode-specific block (`listener.grok_image`, `listener.gemini_image`, or `listener.featherless`) that does not belong to the configured `listener.mode` is now warned about and ignored instead of failing startup with a configuration error.
- Warned about ignored configuration instead of silently ignoring it: unknown top-level keys, and `logging.file` `path`, `max_bytes`, or `backup_count` set while `logging.file.enabled` is `false`, now produce a startup warning.
- Changed the bundled `docker-compose.yml` healthcheck to read `listener.port` from the mounted `config.json` and probe that port, so it stays accurate for any listener port.
- Parameterized the bundled `docker-compose.yml` `ports` mapping with the `NICHELLM_PORT` environment variable (default 8000); behavior is unchanged when the variable is not set.

### v1.3.1 (2026-09-12)

- Changed the `featherless` mode concurrency queue from strict first-in-first-out to skip-queueing: a waiting request that does not fit the remaining budget no longer blocks later, smaller requests, while every queued request still answers HTTP 429 after `max_queue_wait_seconds`.
- Allowed `timeouts.read_seconds` to be set to `null`, which disables the upstream read timeout; `connect_seconds` remains a required positive number.
- Restricted `n` in `gemini-image` mode to omitted or 1: Gemini returns a single image per request, so larger values are now rejected with HTTP 400 instead of silently returning one image.
- Documented the `upstream_request_sent` protocol-logging event emitted by the image modes.

### v1.3.0 (2026-09-12)

- Added the `featherless` listener mode, which presents featherless.ai through an OpenAI-compatible interface with a model whitelist: `GET /v1/models` lists only the configured `model_whitelist`, and other endpoints are relayed with the client's `Authorization` header (the proxy holds no API key).
- Added per-API-key concurrent-request gating for `featherless` mode: the plan limit is fetched from `GET /v1/plan` (overridable with `concurrency_limit`), actual usage is tracked through `GET /account/concurrency` snapshots, and requests that would exceed the limit are queued first-in-first-out up to `max_queue_wait_seconds` (default 60) before answering HTTP 429.
- Added the optional `listener.featherless` settings: required `model_whitelist`, and optional `concurrency_limit`, `max_queue_wait_seconds`, and `cache_ttl_seconds`.

### v1.2.2 (2026-09-10)

- Extracted the full release history into `CHANGELOG.md` and condensed the README changelog to the latest entry with a link to it.
- Added GitHub repository topics and a homepage link, and set the Docker Hub repository full description.
- Removed an obsolete unbuilt mode from the Not supported list.

### v1.2.1 (2026-09-09)

- Restructured the README for general users: installation (Docker Compose from source, the published Docker Hub image, local execution with uv), configuration, modes and features, security, and this changelog.
- Added a requirements overview, a client usage example, a troubleshooting section, update instructions for existing installations, and a Compose example for the published Docker Hub image.
- Removed developer-facing content: test instructions, translation catalog maintenance, and maintainer image publication steps.

### v1.2.0 (2026-09-09)

- Added the `gemini-image` listener mode, which presents Google Gemini image generation through an OpenAI-compatible interface using the Gemini API's OpenAI compatibility layer: `POST /v1/images/generations` is forwarded to `/v1beta/openai/images/generations` and the response is made OpenAI-compatible, while `GET /v1/models` is forwarded to `/v1beta/openai/models` without transformation.
- Added the optional `listener.gemini_image` settings for `default_model` and `aspect_ratio` defaults.
- Extended the protocol logging feature to the `gemini-image` mode and added the `XAI_API_KEY` and `GEMINI_API_KEY` environment pass-through to the Docker Compose environment.

### v1.1.0 (2026-09-08)

- Added the `grok-image` listener mode, which presents Grok (xAI) image generation through an OpenAI-compatible interface: `POST /v1/images/generations` is translated from OpenAI Images to the xAI image generation API, while `GET /v1/models` and `GET /v1/image-generation-models` are forwarded without transformation.
- Added the optional `listener.grok_image` settings for `default_model`, `aspect_ratio`, and `resolution` defaults.
- Extended the protocol logging feature to the `grok-image` mode.

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
