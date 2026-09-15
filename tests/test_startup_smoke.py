"""Real-process startup smoke tests for the niche-llm-proxy CLI.

Launches the actual server process with ``python -m niche_llm_proxy.main`` and
verifies the observable startup behaviour: the startup announcement, the
``/health`` endpoint, skip warnings for listeners whose mode section is
missing, uvicorn's log output, graceful SIGINT shutdown, and the exit code
when every listener is skipped. These tests complement the in-process
integration tests by exercising the real command-line entry point.
"""

from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from niche_llm_proxy.app import PROXY_VERSION

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
_TIMEOUT_SECONDS = 10.0


def _free_port() -> int:
    """Find a TCP port that is currently free."""

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _listener(port: int, mode: str = "passthrough") -> dict[str, Any]:
    """Build one listener configuration entry for the given mode."""

    listener: dict[str, Any] = {
        "port": port,
        "mode": mode,
        "upstream": {"base_url": "https://upstream.example.test"},
    }
    if mode == "passthrough":
        listener["upstream"]["api_key_env"] = "UPSTREAM_API_KEY"
    return listener


def _write_config(path: Path, listeners: list[dict[str, Any]]) -> None:
    """Write a configuration file with the given listener entries."""

    path.write_text(json.dumps({"listeners": listeners}), encoding="utf-8")


def _launch(config_path: Path) -> subprocess.Popen[bytes]:
    """Start the real proxy process against the given configuration file."""

    env = {
        **os.environ,
        "NICHELLM_CONFIG_PATH": str(config_path),
        "NICHELLM_LANGUAGE": "en",
        "UPSTREAM_API_KEY": "dummy",
    }
    return subprocess.Popen(
        [sys.executable, "-m", "niche_llm_proxy.main"],
        cwd=_PROJECT_ROOT,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def _poll_health(port: int, process: subprocess.Popen[bytes]) -> dict[str, Any]:
    """Poll ``/health`` until it answers or the timeout expires."""

    deadline = time.monotonic() + _TIMEOUT_SECONDS
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise AssertionError(
                f"server process exited early with code {process.returncode}"
            )
        try:
            with urllib.request.urlopen(
                f"http://127.0.0.1:{port}/health", timeout=1.0
            ) as response:
                assert response.status == 200
                body: dict[str, Any] = json.load(response)
                return body
        except OSError as error:
            last_error = error
            time.sleep(0.1)
    raise AssertionError(f"/health on port {port} never became ready: {last_error}")


def _shutdown(process: subprocess.Popen[bytes]) -> tuple[bytes, bytes]:
    """Send SIGINT, wait for a graceful exit, and return the output."""

    assert process.poll() is None, "server process already exited before shutdown"
    process.send_signal(signal.SIGINT)
    try:
        return process.communicate(timeout=_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        process.kill()
        stdout, stderr = process.communicate()
        raise AssertionError(
            f"server did not exit after SIGINT; "
            f"stdout={stdout.decode(errors='replace')!r}, "
            f"stderr={stderr.decode(errors='replace')!r}"
        ) from None


def test_startup_smoke_serves_health_and_shuts_down(tmp_path: Path) -> None:
    port = _free_port()
    _write_config(tmp_path / "config.json", [_listener(port)])
    process = _launch(tmp_path / "config.json")
    try:
        body = _poll_health(port, process)
        assert body == {
            "status": "ok",
            "version": PROXY_VERSION,
            "mode": "passthrough",
            "features": [],
        }
    finally:
        stdout, stderr = _shutdown(process)

    assert process.returncode == 0, stderr.decode()
    out = stdout.decode()
    assert f"nicheLLM Proxy {PROXY_VERSION} starting with 1 listener." in out
    assert f"  - port {port}: mode 'passthrough' with no features." in out
    assert "GET /health HTTP/1.1\" 200 OK" in out
    assert f"Uvicorn running on http://0.0.0.0:{port}" in stderr.decode()


def test_startup_smoke_skips_listener_with_missing_mode_section(
    tmp_path: Path,
) -> None:
    kept_port = _free_port()
    skipped_port = _free_port()
    listeners = [_listener(kept_port), _listener(skipped_port, "featherless")]
    _write_config(tmp_path / "config.json", listeners)
    process = _launch(tmp_path / "config.json")
    try:
        body = _poll_health(kept_port, process)
        assert body["status"] == "ok"
    finally:
        stdout, stderr = _shutdown(process)

    assert process.returncode == 0, stderr.decode()
    out = stdout.decode()
    assert f"nicheLLM Proxy {PROXY_VERSION} starting with 1 listener." in out
    assert f"  - port {kept_port}: mode 'passthrough' with no features." in out
    assert f"port {skipped_port}" not in out
    assert (
        f"Skipped the listener on port {skipped_port} because mode 'featherless' "
        "requires a 'featherless' section, which is missing from the configuration.\n"
    ) in stderr.decode()
    assert f"Uvicorn running on http://0.0.0.0:{kept_port}" in stderr.decode()
    assert f"http://0.0.0.0:{skipped_port}" not in stderr.decode()


def test_startup_smoke_exits_with_configuration_error_when_all_skipped(
    tmp_path: Path,
) -> None:
    port = _free_port()
    _write_config(tmp_path / "config.json", [_listener(port, "featherless")])
    process = _launch(tmp_path / "config.json")
    stdout, stderr = process.communicate(timeout=_TIMEOUT_SECONDS)

    assert process.returncode == 2
    assert stdout == b""
    assert stderr.decode() == (
        "Configuration error: No listeners can be started because every "
        f"listener was skipped. Skipped ports: {port}.\n"
    )