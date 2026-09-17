"""multipart/form-data のパースとビルド(標準ライブラリのみ)。"""

from __future__ import annotations

import uuid
from dataclasses import dataclass


@dataclass
class FormPart:
    """multipart ボディ内の1パート。"""

    name: str
    data: bytes
    filename: str | None
    content_type: str | None


class MultipartError(ValueError):
    """multipart/form-data のパース失敗。"""


_CRLF = b"\r\n"
_CRLF2 = b"\r\n\r\n"


def parse_multipart(body: bytes, content_type: str) -> list[FormPart]:
    """multipart/form-dataボディをパースしてFormPart列を本文出現順に返す。"""

    boundary = _extract_boundary(content_type)
    delimiter = b"--" + boundary
    segments = body.split(delimiter)
    if not segments:
        raise MultipartError("ボディが空です。")

    parts: list[FormPart] = []
    closed = False
    for segment in segments[1:]:
        if segment.startswith(b"--"):
            closed = True
            break
        if not segment.startswith(_CRLF):
            raise MultipartError("デリミタ後に予期しないデータがあります。")

        part = segment[2:]
        if part.endswith(_CRLF):
            part = part[:-2]
        else:
            # close delimiter なしで末尾に来た場合等
            pass

        header_sep = part.find(_CRLF2)
        if header_sep == -1:
            raise MultipartError("ヘッダ部とコンテンツの区切りが見つかりません。")

        headers_bytes = part[:header_sep]
        content = part[header_sep + 4 :]
        headers = _parse_headers(headers_bytes)

        disposition = headers.get("content-disposition")
        if disposition is None:
            raise MultipartError("Content-Disposition ヘッダがありません。")

        params = _parse_disposition(disposition)
        name = params.get("name")
        if name is None:
            raise MultipartError("Content-Disposition に name パラメータがありません。")

        filename = params.get("filename")
        part_content_type = headers.get("content-type")
        parts.append(
            FormPart(
                name=name,
                data=content,
                filename=filename,
                content_type=part_content_type,
            )
        )

    if not closed:
        raise MultipartError("close delimiter が見つかりません。")

    return parts


def build_multipart(parts: list[FormPart]) -> tuple[bytes, str]:
    """FormPart列からmultipartボディとContent-Typeヘッダ値を組み立てる。"""

    boundary = _generate_boundary(parts)
    boundary_bytes = boundary.encode("ascii")

    body = b""
    for part in parts:
        body += b"--" + boundary_bytes + _CRLF
        body += _build_disposition_header(part).encode("ascii")
        if part.filename is not None:
            content_type = part.content_type or "application/octet-stream"
            body += f"Content-Type: {content_type}\r\n".encode("ascii")
        body += _CRLF
        body += part.data
        body += _CRLF
    body += b"--" + boundary_bytes + b"--\r\n"

    content_type = f"multipart/form-data; boundary={boundary}"
    return body, content_type


def detect_image_type(data: bytes) -> str | None:
    """マジックバイトでPNG/JPEG/WebPを判定する。"""

    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def _extract_boundary(content_type: str) -> bytes:
    """Content-Type ヘッダ値から boundary パラメータを抽出する。"""

    segments = content_type.split(";")
    media_type = segments[0].strip().lower()
    if media_type != "multipart/form-data":
        raise MultipartError("Content-Type が multipart/form-data ではありません。")

    for segment in segments[1:]:
        segment = segment.strip()
        if "=" not in segment:
            continue
        name, _, value = segment.partition("=")
        if name.strip().lower() == "boundary":
            value = value.strip()
            return _unquote_boundary(value).encode("latin-1")

    raise MultipartError("boundary パラメータがありません。")


def _unquote_boundary(value: str) -> str:
    """quoted/unquoted の boundary を正規化する。"""

    if len(value) >= 2 and value[0] == '"' and value[-1] == '"':
        result: list[str] = []
        i = 1
        end = len(value) - 1
        while i < end:
            ch = value[i]
            if ch == "\\" and i + 1 < end:
                result.append(value[i + 1])
                i += 2
            else:
                result.append(ch)
                i += 1
        return "".join(result)
    return value


def _parse_headers(headers_bytes: bytes) -> dict[str, str]:
    """ヘッダ部を辞書に変換する(ヘッダ名は小文字化)。"""

    headers: dict[str, str] = {}
    for line in headers_bytes.split(_CRLF):
        if not line:
            continue
        try:
            line_text = line.decode("latin-1")
        except UnicodeDecodeError as error:
            raise MultipartError("ヘッダのデコードに失敗しました。") from error
        if ":" not in line_text:
            continue
        name, _, value = line_text.partition(":")
        headers[name.strip().lower()] = value.strip()
    return headers


def _parse_disposition(disposition: str) -> dict[str, str]:
    """Content-Disposition のパラメータを辞書に変換する。"""

    params: dict[str, str] = {}
    if ";" not in disposition:
        return params

    for segment in disposition.split(";")[1:]:
        segment = segment.strip()
        if "=" not in segment:
            continue
        name, _, value = segment.partition("=")
        params[name.strip().lower()] = _unquote_boundary(value.strip())
    return params


def _build_disposition_header(part: FormPart) -> str:
    """Content-Disposition ヘッダ行を組み立てる。"""

    header = f'Content-Disposition: form-data; name="{_escape(part.name)}"'
    if part.filename is not None:
        header += f'; filename="{_escape(part.filename)}"'
    return header + "\r\n"


def _escape(value: str) -> str:
    """name/filename 中の " と \\ をバックスラッシュでエスケープする。"""

    return value.replace("\\", "\\\\").replace('"', '\\"')


def _generate_boundary(parts: list[FormPart]) -> str:
    """パートデータと衝突しない boundary を生成する。"""

    while True:
        boundary = uuid.uuid4().hex
        boundary_bytes = boundary.encode("ascii")
        if all(boundary_bytes not in part.data for part in parts):
            return boundary
