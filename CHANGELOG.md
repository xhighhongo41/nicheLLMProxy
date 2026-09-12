# Changelog

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
