"""Request boundaries and response writers for the local Weather Edge UI."""

import csv
from datetime import date
import gzip
from http.server import SimpleHTTPRequestHandler
import io
from itertools import chain
import json
from shutil import copyfileobj
from urllib.parse import urlsplit


def market_filters(query, venues):
    filters = {key: query[key][0].strip() for key in ("venue", "family", "place", "day", "status", "text") if key in query}
    if any(len(value) > 200 for value in filters.values()):
        raise ValueError("Market search terms must be 200 characters or shorter")
    if filters.get("venue") and filters["venue"] not in venues:
        raise ValueError("Unknown venue")
    if filters.get("day"):
        filters["day"] = date.fromisoformat(filters["day"]).isoformat()
    return filters


class LocalHTTPHandler(SimpleHTTPRequestHandler):
    def end_headers(self):
        if urlsplit(self.path).path in {"/", "/index.html"}:
            self.send_header("Cache-Control", "no-cache")
        super().end_headers()

    def send_json(self, data, status=200, *, filename=None):
        body = json.dumps(data, allow_nan=False, separators=(",", ":")).encode()
        encoding = None
        preferences = (part.strip().lower() for part in self.headers.get("Accept-Encoding", "").split(","))
        if len(body) >= 128 * 1024 and "gzip" in preferences:
            body = gzip.compress(body, compresslevel=1, mtime=0)
            encoding = "gzip"
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Vary", "Accept-Encoding")
        if encoding is not None:
            self.send_header("Content-Encoding", encoding)
        if filename is not None:
            self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def local_host(self):
        port = self.server.server_port
        return self.headers.get("Host") in {f"127.0.0.1:{port}", f"localhost:{port}"}

    def local_write(self):
        expected_origin = "http://" + self.headers.get("Host", "")
        return self.local_host() and self.headers.get("Origin", expected_origin) == expected_origin

    def read_json_object(self):
        length = int(self.headers.get("Content-Length", "0"))
        if self.headers.get_content_type() != "application/json" or not 0 < length <= 4096:
            raise ValueError("Expected a small JSON request")
        request = json.loads(self.rfile.read(length))
        if not isinstance(request, dict):
            raise ValueError("Expected a JSON object")
        return request

    def send_gzip_attachment(self, path, filename):
        with path.open("rb") as body:
            self.send_response(200)
            self.send_header("Content-Type", "application/gzip")
            self.send_header("Content-Length", str(path.stat().st_size))
            self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            try:
                copyfileobj(body, self.wfile)
            except (BrokenPipeError, ConnectionResetError):
                pass

    def send_market_export(self, form, rows, columns, csv_row, encode_row):
        try:
            first = next(rows, None)
            stream = chain(() if first is None else (first,), rows)
            self.send_response(200)
            self.send_header("Content-Type", ("text/csv" if form == "csv" else "application/json") + "; charset=utf-8")
            self.send_header("Content-Disposition", f'attachment; filename="weather-edge-markets.{form}"')
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "close")
            self.end_headers()
            if form == "csv":
                buffer = io.StringIO(newline="")
                writer = csv.DictWriter(buffer, fieldnames=columns)
                writer.writeheader()
                self.wfile.write(buffer.getvalue().encode())
                for row in stream:
                    buffer.seek(0)
                    buffer.truncate()
                    writer.writerow(csv_row(row))
                    self.wfile.write(buffer.getvalue().encode())
            else:
                self.wfile.write(b"[")
                prefix = b""
                for row in stream:
                    self.wfile.write(prefix + encode_row(row).encode())
                    prefix = b",\n"
                self.wfile.write(b"]\n")
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            rows.close()
