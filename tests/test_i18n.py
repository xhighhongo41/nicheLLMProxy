"""Tests for user-facing message localization."""

from __future__ import annotations

import pytest

from niche_llm_proxy.i18n import get_language, normalize_language, translate
from niche_llm_proxy.main import main


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, "en"),
        ("", "en"),
        ("en", "en"),
        ("ja", "ja"),
        ("ja-JP", "ja"),
        ("ja_JP", "ja"),
        ("fr", "en"),
    ],
)
def test_normalize_language_uses_supported_language_or_english_fallback(
    value: str | None,
    expected: str,
) -> None:
    """Supported language values are normalized and all others fall back to English."""
    assert normalize_language(value) == expected


def test_japanese_catalog_translates_config_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """The included Japanese catalog translates a representative configuration message."""
    monkeypatch.setenv("NICHELLM_LANGUAGE", "ja-JP")

    assert get_language() == "ja"
    assert (
        translate("listener.mode must be 'passthrough'.")
        == "listener.mode は 'passthrough' である必要があります。"
    )


def test_unsupported_language_uses_english_message(monkeypatch: pytest.MonkeyPatch) -> None:
    """An unsupported process language returns the English message ID."""
    monkeypatch.setenv("NICHELLM_LANGUAGE", "fr")

    assert translate("The upstream provider timed out.") == "The upstream provider timed out."


def test_main_localizes_configuration_error_prefix(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CLI startup errors use the configured language without exposing a secret."""
    monkeypatch.setenv("NICHELLM_LANGUAGE", "ja")

    def raise_config_error() -> None:
        from niche_llm_proxy.config import ConfigError

        raise ConfigError("設定を読み込めません。")

    monkeypatch.setattr("niche_llm_proxy.main.load_config", raise_config_error)

    with pytest.raises(SystemExit, match="2"):
        main()

    assert capsys.readouterr().err == "設定エラー: 設定を読み込めません。\n"
