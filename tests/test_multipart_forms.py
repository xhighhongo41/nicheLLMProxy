"""Tests for multipart/form-data parsing and building."""

from __future__ import annotations

import uuid

import pytest

from niche_llm_proxy.multipart_forms import (
    FormPart,
    MultipartError,
    build_multipart,
    detect_image_type,
    parse_multipart,
)


def _build_body(
    boundary: bytes,
    parts: list[tuple[bytes, bytes]],
    close: bool = True,
    trailing_crlf: bool = True,
    preamble: bytes = b"",
    epilogue: bytes = b"",
) -> bytes:
    """テスト用の multipart ボディを組み立てる。"""

    body = preamble
    for headers, content in parts:
        body += b"--" + boundary + b"\r\n"
        body += headers
        body += b"\r\n\r\n"
        body += content
        body += b"\r\n"
    body += b"--" + boundary
    if close:
        body += b"--"
    if trailing_crlf:
        body += b"\r\n"
    body += epilogue
    return body


class TestParseMultipart:
    """parse_multipart のテスト。"""

    def test_single_text_field(self) -> None:
        """テキストフィールド1個のパース。"""
        body = (
            b"--B\r\n"
            b'Content-Disposition: form-data; name="prompt"\r\n'
            b"\r\n"
            b"a cat\r\n"
            b"--B--\r\n"
        )

        parts = parse_multipart(body, "multipart/form-data; boundary=B")

        assert parts == [FormPart(name="prompt", data=b"a cat", filename=None, content_type=None)]

    def test_multiple_text_fields_preserves_order(self) -> None:
        """複数テキストフィールドの順序を維持する。"""
        body = (
            b"--B\r\n"
            b'Content-Disposition: form-data; name="first"\r\n'
            b"\r\n"
            b"1\r\n"
            b"--B\r\n"
            b'Content-Disposition: form-data; name="second"\r\n'
            b"\r\n"
            b"2\r\n"
            b"--B\r\n"
            b'Content-Disposition: form-data; name="third"\r\n'
            b"\r\n"
            b"3\r\n"
            b"--B--\r\n"
        )

        parts = parse_multipart(body, "multipart/form-data; boundary=B")

        assert [part.name for part in parts] == ["first", "second", "third"]
        assert parts == [
            FormPart(name="first", data=b"1", filename=None, content_type=None),
            FormPart(name="second", data=b"2", filename=None, content_type=None),
            FormPart(name="third", data=b"3", filename=None, content_type=None),
        ]

    def test_file_part_with_filename_and_content_type(self) -> None:
        """filename と Content-Type 付きファイルパートのパース。"""
        body = (
            b"--B\r\n"
            b'Content-Disposition: form-data; name="image"; filename="cat.png"\r\n'
            b"Content-Type: image/png\r\n"
            b"\r\n"
            b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\r\n"
            b"--B--\r\n"
        )

        parts = parse_multipart(body, "multipart/form-data; boundary=B")

        assert parts == [
            FormPart(
                name="image",
                data=b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR",
                filename="cat.png",
                content_type="image/png",
            )
        ]

    def test_repeated_field_names_are_not_collapsed(self) -> None:
        """同名フィールドの重複は集約せず出現順に並べる。"""
        body = (
            b"--B\r\n"
            b'Content-Disposition: form-data; name="image[]"\r\n'
            b"\r\n"
            b"first\r\n"
            b"--B\r\n"
            b'Content-Disposition: form-data; name="image[]"\r\n'
            b"\r\n"
            b"second\r\n"
            b"--B--\r\n"
        )

        parts = parse_multipart(body, "multipart/form-data; boundary=B")

        assert len(parts) == 2
        assert parts[0].name == "image[]"
        assert parts[0].data == b"first"
        assert parts[1].name == "image[]"
        assert parts[1].data == b"second"

    def test_payload_with_crlf_and_dashes(self) -> None:
        """ペイロードに CRLF と -- を含むバイナリを正しくパースする。"""
        content = b"line1\r\nline2\r\n--not-a-boundary\r\nline3"
        body = (
            b"--B\r\n"
            b'Content-Disposition: form-data; name="binary"\r\n'
            b"\r\n"
            + content
            + b"\r\n"
            b"--B--\r\n"
        )

        parts = parse_multipart(body, "multipart/form-data; boundary=B")

        assert parts == [
            FormPart(name="binary", data=content, filename=None, content_type=None)
        ]

    def test_quoted_boundary(self) -> None:
        """quoted boundary をパースする。"""
        boundary = "boundary with space"
        body = _build_body(
            boundary.encode("utf-8"),
            [
                (
                    b'Content-Disposition: form-data; name="prompt"',
                    b"hello",
                )
            ],
        )

        parts = parse_multipart(body, f'multipart/form-data; boundary="{boundary}"')

        assert parts == [FormPart(name="prompt", data=b"hello", filename=None, content_type=None)]

    def test_unquoted_boundary(self) -> None:
        """unquoted boundary をパースする。"""
        body = _build_body(
            b"simple",
            [
                (
                    b'Content-Disposition: form-data; name="prompt"',
                    b"hello",
                )
            ],
        )

        parts = parse_multipart(body, "multipart/form-data; boundary=simple")

        assert parts == [FormPart(name="prompt", data=b"hello", filename=None, content_type=None)]

    def test_ignores_preamble_and_epilogue(self) -> None:
        """preamble と epilogue を無視する。"""
        body = (
            b"This is preamble.\r\n"
            b"--B\r\n"
            b'Content-Disposition: form-data; name="prompt"\r\n'
            b"\r\n"
            b"hi\r\n"
            b"--B--\r\n"
            b"This is epilogue."
        )

        parts = parse_multipart(body, "multipart/form-data; boundary=B")

        assert parts == [FormPart(name="prompt", data=b"hi", filename=None, content_type=None)]

    def test_close_delimiter_without_trailing_crlf(self) -> None:
        """末尾 CRLF なしの close delimiter で終端してもパースできる。"""
        body = (
            b"--B\r\n"
            b'Content-Disposition: form-data; name="prompt"\r\n'
            b"\r\n"
            b"hi\r\n"
            b"--B--"
        )

        parts = parse_multipart(body, "multipart/form-data; boundary=B")

        assert parts == [FormPart(name="prompt", data=b"hi", filename=None, content_type=None)]

    def test_missing_boundary_parameter_raises(self) -> None:
        """boundary パラメータ無しは MultipartError。"""
        with pytest.raises(MultipartError):
            parse_multipart(b"--B--\r\n", "multipart/form-data")

    def test_non_multipart_content_type_raises(self) -> None:
        """multipart/form-data 以外の Content-Type は MultipartError。"""
        with pytest.raises(MultipartError):
            parse_multipart(b"body", "application/json")

    def test_missing_content_disposition_raises(self) -> None:
        """content-disposition 無しパートは MultipartError。"""
        body = (
            b"--B\r\n"
            b"X-Custom: value\r\n"
            b"\r\n"
            b"data\r\n"
            b"--B--\r\n"
        )

        with pytest.raises(MultipartError):
            parse_multipart(body, "multipart/form-data; boundary=B")

    def test_missing_name_in_content_disposition_raises(self) -> None:
        """name 無し content-disposition は MultipartError。"""
        body = (
            b"--B\r\n"
            b'Content-Disposition: form-data\r\n'
            b"\r\n"
            b"data\r\n"
            b"--B--\r\n"
        )

        with pytest.raises(MultipartError):
            parse_multipart(body, "multipart/form-data; boundary=B")

    def test_header_name_case_insensitive(self) -> None:
        """ヘッダ名の大小文字を無視する。"""
        body = (
            b"--B\r\n"
            b'CONTENT-DISPOSITION: form-data; name="prompt"\r\n'
            b"content-type: text/plain\r\n"
            b"\r\n"
            b"hi\r\n"
            b"--B--\r\n"
        )

        parts = parse_multipart(body, "multipart/form-data; boundary=B")

        assert parts == [
            FormPart(name="prompt", data=b"hi", filename=None, content_type="text/plain")
        ]

    def test_missing_close_delimiter_raises(self) -> None:
        """close delimiter 無しは MultipartError。"""
        body = (
            b"--B\r\n"
            b'Content-Disposition: form-data; name="prompt"\r\n'
            b"\r\n"
            b"hi\r\n"
            b"--B\r\n"
        )

        with pytest.raises(MultipartError):
            parse_multipart(body, "multipart/form-data; boundary=B")

    def test_garbage_after_delimiter_raises(self) -> None:
        """delimiter 後の segment が CRLF で始まらない場合は MultipartError。"""
        body = (
            b"--Bgarbage\r\n"
            b'Content-Disposition: form-data; name="prompt"\r\n'
            b"\r\n"
            b"hi\r\n"
            b"--B--\r\n"
        )

        with pytest.raises(MultipartError):
            parse_multipart(body, "multipart/form-data; boundary=B")

    def test_segment_without_header_body_separator_raises(self) -> None:
        """ヘッダ部とコンテンツの区切り \r\n\r\n が無い場合は MultipartError。"""
        body = (
            b"--B\r\n"
            b'Content-Disposition: form-data; name="prompt"\r\n'
            b"missing separator\r\n"
            b"--B--\r\n"
        )

        with pytest.raises(MultipartError):
            parse_multipart(body, "multipart/form-data; boundary=B")


class TestBuildMultipart:
    """build_multipart のテスト。"""

    def test_builds_text_part(self) -> None:
        """テキストパートを正しく組み立てる。"""
        parts = [FormPart(name="prompt", data=b"a cat", filename=None, content_type=None)]

        body, content_type = build_multipart(parts)

        assert content_type.startswith("multipart/form-data; boundary=")
        parsed = parse_multipart(body, content_type)
        assert parsed == parts

    def test_builds_file_part(self) -> None:
        """ファイルパートを正しく組み立てる。"""
        parts = [
            FormPart(
                name="image",
                data=b"\x89PNG\r\n\x1a\n",
                filename="cat.png",
                content_type="image/png",
            )
        ]

        body, content_type = build_multipart(parts)

        parsed = parse_multipart(body, content_type)
        assert parsed == parts

    def test_builds_mixed_parts(self) -> None:
        """テキストとファイルの混在パートを組み立てる。"""
        parts = [
            FormPart(name="prompt", data=b"a cat", filename=None, content_type=None),
            FormPart(
                name="image",
                data=b"\xff\xd8\xffdata",
                filename="cat.jpg",
                content_type="image/jpeg",
            ),
        ]

        body, content_type = build_multipart(parts)

        parsed = parse_multipart(body, content_type)
        assert parsed == parts

    def test_escapes_quotes_and_backslashes(self) -> None:
        """name/filename 中の " と \\ をエスケープする。"""
        parts = [
            FormPart(
                name='say "hi"',
                data=b"x",
                filename='back\\slash.png',
                content_type="image/png",
            )
        ]

        body, content_type = build_multipart(parts)

        parsed = parse_multipart(body, content_type)
        assert parsed == parts

    def test_build_uses_octet_stream_when_content_type_missing(self) -> None:
        """ファイルパートで content_type が None の場合 application/octet-stream を使う。"""
        parts = [
            FormPart(name="file", data=b"content", filename="file.bin", content_type=None)
        ]

        body, content_type = build_multipart(parts)

        parsed = parse_multipart(body, content_type)
        assert parsed == [
            FormPart(
                name="file",
                data=b"content",
                filename="file.bin",
                content_type="application/octet-stream",
            )
        ]
        assert b"Content-Type: application/octet-stream\r\n" in body

    def test_build_avoids_boundary_collision(self) -> None:
        """境界文字列がパートデータと衝突する場合は再生成する。"""
        parts = [FormPart(name="data", data=b"content", filename=None, content_type=None)]
        body, content_type = build_multipart(parts)
        boundary = content_type.split("boundary=", 1)[1].encode("ascii")
        assert boundary not in parts[0].data
        assert parse_multipart(body, content_type) == parts


class TestDetectImageType:
    """detect_image_type のテスト。"""

    def test_detects_png(self) -> None:
        """PNG マジックバイトを検出する。"""
        assert detect_image_type(b"\x89PNG\r\n\x1a\n") == "image/png"

    def test_detects_jpeg(self) -> None:
        """JPEG マジックバイトを検出する。"""
        assert detect_image_type(b"\xff\xd8\xff\xe0") == "image/jpeg"

    def test_detects_webp(self) -> None:
        """WebP マジックバイトを検出する。"""
        assert detect_image_type(b"RIFF\x00\x00\x00\x00WEBP") == "image/webp"

    def test_unknown_image_type_returns_none(self) -> None:
        """未知の画像形式は None を返す。"""
        assert detect_image_type(b"GIF87a") is None

    def test_empty_data_returns_none(self) -> None:
        """空データは None を返す。"""
        assert detect_image_type(b"") is None


class TestRoundTrip:
    """build と parse の往復テスト。"""

    def test_roundtrip_preserved_parts(self) -> None:
        """build→parse で FormPart 列が保存される。"""
        original = [
            FormPart(name="prompt", data=b"a cat", filename=None, content_type=None),
            FormPart(
                name="image",
                data=b"\x89PNG\r\n\x1a\n",
                filename="cat.png",
                content_type="image/png",
            ),
        ]

        body, content_type = build_multipart(original)
        parsed = parse_multipart(body, content_type)

        assert parsed == original

    def test_roundtrip_with_content_type_header(self) -> None:
        """build 戻り値の Content-Type ヘッダ値を parse へ渡して往復できる。"""
        original = [
            FormPart(name="model", data=b"dall-e", filename=None, content_type=None),
            FormPart(
                name="image",
                data=b"\xff\xd8\xffbinary",
                filename="image.jpg",
                content_type="image/jpeg",
            ),
        ]

        parsed = parse_multipart(*build_multipart(original))

        assert parsed == original
