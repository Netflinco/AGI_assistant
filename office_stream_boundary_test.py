#!/usr/bin/env python3
"""Actual streaming 40 MB / 3×40 MB Office ingress regression (GATE-205/206/509).

This test uses synthetic, harmless ZIP_STORED OOXML containers.  It exercises
the HTTP streaming parser and asset-policy boundary without invoking a parser,
model, Worker, or any external service.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import tempfile
import threading
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
import zipfile


CHUNK = 1024 * 1024
LIMIT = 40 * 1024 * 1024


def create_exact_ooxml(path: Path, target_size: int) -> None:
    """Create a harmless XLSX-shaped ZIP whose outer size is exactly target."""
    blob_size = target_size - 1024
    for _attempt in range(3):
        with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_STORED) as archive:
            archive.writestr("[Content_Types].xml", b"x")
            archive.writestr("xl/workbook.xml", b"x")
            with archive.open("xl/media/payload.bin", "w") as payload:
                remaining = blob_size
                while remaining:
                    size = min(CHUNK, remaining)
                    payload.write(b"x" * size)
                    remaining -= size
        delta = target_size - path.stat().st_size
        if delta == 0:
            return
        blob_size += delta
    raise AssertionError(f"could not create exact {target_size} byte fixture; got {path.stat().st_size}")


def send_raw(port: int, path: Path, *, content_length: int | None = None) -> tuple[int, dict]:
    connection = HTTPConnection("127.0.0.1", port, timeout=90)
    size = int(content_length if content_length is not None else path.stat().st_size)
    connection.putrequest("POST", "/api/office/assets")
    connection.putheader("X-User-Id", "u_admin")
    connection.putheader("Content-Type", "application/octet-stream")
    connection.putheader("X-File-Name", path.name)
    connection.putheader("Content-Length", str(size))
    connection.endheaders()
    if content_length is None:
        with path.open("rb") as source:
            while chunk := source.read(CHUNK):
                connection.send(chunk)
    response = connection.getresponse()
    payload = json.loads(response.read().decode("utf-8"))
    connection.close()
    return response.status, payload


def send_multipart(port: int, paths: list[Path]) -> tuple[int, dict]:
    boundary = b"----agi-40mb-boundary"
    parts = []
    for index, path in enumerate(paths):
        delimiter = (b"--" if index == 0 else b"\r\n--") + boundary
        header = delimiter + b"\r\nContent-Disposition: form-data; name=\"files\"; filename=\"" + path.name.encode() + b"\"\r\nContent-Type: application/vnd.openxmlformats-officedocument.spreadsheetml.sheet\r\n\r\n"
        parts.append((header, path))
    closing = b"\r\n--" + boundary + b"--\r\n"
    # ``closing``/the next part's boundary already starts with CRLF, so do not
    # append a second CRLF after every binary file (that would change an exact
    # 40 MB part into 40 MB + 2 bytes).
    length = sum(len(header) + path.stat().st_size for header, path in parts) + len(closing)
    connection = HTTPConnection("127.0.0.1", port, timeout=180)
    connection.putrequest("POST", "/api/office/assets")
    connection.putheader("X-User-Id", "u_admin")
    connection.putheader("Content-Type", f"multipart/form-data; boundary={boundary.decode()}")
    connection.putheader("Content-Length", str(length))
    connection.endheaders()
    for header, path in parts:
        connection.send(header)
        with path.open("rb") as source:
            while chunk := source.read(CHUNK):
                connection.send(chunk)
    connection.send(closing)
    response = connection.getresponse()
    payload = json.loads(response.read().decode("utf-8"))
    connection.close()
    return response.status, payload


with tempfile.TemporaryDirectory(prefix="agi-office-stream-") as _tmp:
    root = Path(_tmp)
    os.environ["AGI_INSPECTION_DB"] = str(root / "test.db")
    os.environ["AGI_OFFICE_ASSET_DIR"] = str(root / "assets")
    os.environ["AGI_OFFICE_UPLOAD_STAGING_DIR"] = str(root / "staging")
    import server

    server.init_db(reset=True)
    fixture = root / "exact-40mb.xlsx"
    create_exact_ooxml(fixture, LIMIT)
    assert fixture.stat().st_size == LIMIT
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.AppHandler)
    port = httpd.server_address[1]
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        status, accepted = send_raw(port, fixture)
        assert status == 201 and accepted["data"]["assets"][0]["byte_size"] == LIMIT
        assert not list(server.OFFICE_UPLOAD_STAGING_DIR.glob("*.part"))
        # The Content-Length guard rejects before consuming/staging the body.
        status, rejected = send_raw(port, fixture, content_length=LIMIT + 1)
        assert status == 413 and rejected["error"]["code"] == "OFFICE_FILE_TOO_LARGE"
        copies = [root / f"batch-{index}.xlsx" for index in range(3)]
        for copy in copies:
            shutil.copyfile(fixture, copy)
        status, batch = send_multipart(port, copies)
        assert status == 201 and len(batch["data"]["assets"]) == 3
        assert not list(server.OFFICE_UPLOAD_STAGING_DIR.glob("*.part"))
    finally:
        httpd.shutdown(); httpd.server_close(); thread.join(timeout=5)

print("PASS Office streaming boundaries: 40 MB raw, 120 MB multipart batch, pre-staging rejection")
