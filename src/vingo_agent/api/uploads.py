"""Nhận đúng một SVG từ proxy chat, giới hạn byte trước khi phân tích multipart."""

from email import policy
from email.parser import BytesParser

from fastapi import HTTPException, Request

MAX_FILE_BYTES = 10 * 1024 * 1024
MAX_MULTIPART_BYTES = MAX_FILE_BYTES + 16 * 1024


async def svg_upload(request: Request) -> bytes:
    content_type = request.headers.get("content-type", "")
    if not content_type.lower().startswith("multipart/form-data;") or any(char in content_type for char in "\r\n"):
        raise HTTPException(415, "UNSUPPORTED_MEDIA_TYPE")
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > MAX_MULTIPART_BYTES:
            raise HTTPException(413, "PAYLOAD_TOO_LARGE")
        body.extend(chunk)
    header = b"Content-Type: " + content_type.encode("latin-1") + b"\r\n\r\n"
    message = BytesParser(policy=policy.default).parsebytes(header + body)
    parts = list(message.iter_parts())
    if message.defects or len(parts) != 1:
        raise HTTPException(400, "VALIDATION_ERROR")
    part = parts[0]
    if part.get_content_disposition() != "form-data" or part.get_param("name", header="content-disposition") != "file":
        raise HTTPException(400, "VALIDATION_ERROR")
    if part.get_content_type() != "image/svg+xml":
        raise HTTPException(415, "UNSUPPORTED_MEDIA_TYPE")
    content = part.get_payload(decode=True)
    if not isinstance(content, bytes) or not content.strip() or part.defects:
        raise HTTPException(400, "VALIDATION_ERROR")
    if len(content) > MAX_FILE_BYTES:
        raise HTTPException(413, "PAYLOAD_TOO_LARGE")
    return content
