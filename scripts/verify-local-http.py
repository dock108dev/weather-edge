"""Exercise HTTP boundaries against a disposable Weather Edge listener."""
import csv
import gzip
import json
import sys
from hashlib import sha256
from http.client import HTTPConnection
from io import StringIO
from pathlib import Path

from weather_edge.venue_archive import EXPORT_COLUMNS


def request(port, path, *, body=None, headers=None, status=200, kind="application/json"):
    connection = HTTPConnection("127.0.0.1", port, timeout=10)
    try:
        connection.request("GET" if body is None else "POST", path, body, headers or {})
        response = connection.getresponse()
        content = response.read()
        assert response.status == status, (path, response.status, content)
        assert response.getheader("Cache-Control") == "no-store", path
        assert response.getheader("Content-Type").split(";")[0] == kind, path
        return json.loads(content) if kind == "application/json" else content
    finally:
        connection.close()


def mutable_files(data):
    return {path.relative_to(data): sha256(path.read_bytes()).hexdigest()
            for directory in ("backtests", "paper-runs", "trade-paper", "collector")
            for path in (data / directory).rglob("*") if path.is_file()}


def verify(port, data):
    model = request(port, "/api/model", headers={"Host": f"localhost:{port}"})
    assert model == {"legacy": None, "study": None}, model
    session = request(port, "/api/session?mode=live")
    assert not session["cached"] and session["events"] == []
    trade = request(port, "/api/trade")
    assert trade["ready"] == trade["watch"] == trade["prices"] == []
    assert request(port, "/api/backtests")["runs"] == []
    assert request(port, "/api/research") == {"records": [], "issues": []}
    collector = request(port, "/api/collection")
    assert not collector["running"] and collector["pid"] is None
    assert request(port, "/api/model", headers={"Host": "outside.invalid"}, status=403)["error"]

    # Snapshot after legitimate read-only initialization and before rejected writes.
    before = mutable_files(data)
    payload = b'{"fixture":true,"strategy":"two-sided","size":25,"reaction":5}'
    headers = {"Content-Type": "application/json"}
    for extra, invalid_body, status, message in (
        ({"Host": "outside.invalid"}, payload, 403, "local Weather Edge"),
        ({"Origin": f"http://localhost:{port}"}, payload, 403, "local Weather Edge"),
        ({}, b" " * 4097, 400, "small JSON request"),
        ({}, b"", 400, "small JSON request"),
        ({}, b"[]", 400, "JSON object"),
        ({}, b"{", 400, "Expecting"),
        ({"Content-Type": "text/plain"}, payload, 400, "small JSON request"),
    ):
        denied = request(port, "/api/backtests", body=invalid_body, headers=headers | extra, status=status)
        assert message in denied["error"], denied
    assert before == mutable_files(data)
    padded = b"{}" + b" " * 4094
    for host in ("127.0.0.1", "localhost"):
        local = f"{host}:{port}"
        assert request(port, "/api/trade/practice", body=padded,
                       headers=headers | {"Host": local, "Origin": f"http://{local}"})
    assert request(port, "/api/trade/practice", body=b"{}", headers=headers)
    assert before == mutable_files(data)

    assert request(port, "/api/markets/export.json?venue=kalshi") == []
    csv_body = request(port, "/api/markets/export.csv?venue=kalshi", kind="text/csv")
    assert list(csv.reader(StringIO(csv_body.decode()))) == [list(EXPORT_COLUMNS)]
    assert request(port, "/api/markets/export.json?venue=invalid", status=400)["error"]
    assert request(port, "/api/markets/export.csv?text=" + "x" * 201, status=400)["error"]

    research = data / "research"
    models = research / "models"
    models.mkdir(parents=True, exist_ok=True)
    artifact = gzip.compress(b'{"offline":true}', mtime=0)
    inside = research / "verification.json.gz"
    outside = data / "verification-outside.json.gz"
    inside.write_bytes(artifact)
    outside.write_bytes(artifact)
    (research / "verification-link.json.gz").symlink_to(outside)
    outside_record = data / "verification-record.json"
    outside_record.write_text('{"id":"linked-record"}\n')
    (models / "linked-record.json").symlink_to(outside_record)
    assert request(port, "/api/research/models/linked-record", status=404)["error"]
    assert all(row["id"] != "linked-record" for row in request(port, "/api/research")["records"])
    for ident, target in {"safe": "research/verification.json.gz",
                          "outside": outside.name,
                          "symlink": "research/verification-link.json.gz",
                          "missing": "research/absent.json.gz"}.items():
        record = {"id": ident, "provenance": {"frozen_inputs": target}}
        (models / f"{ident}.json").write_text(json.dumps(record))
        route = f"/api/research/models/{ident}"
        reply = request(port, route)
        assert reply["id"] == ident and reply["evidence_download"] == (ident == "safe"), reply
        if ident == "safe":
            assert request(port, route + "/evidence.gz", kind="application/gzip") == artifact
        else:
            assert request(port, route + "/evidence.gz", status=404)["error"]
    print("HTTP passed: empty archive/model, local Host/Origin, JSON bounds, no rejected writes, exports and evidence paths")


if __name__ == "__main__":
    port, data = int(sys.argv[1]), Path(sys.argv[2]).resolve()
    assert port != 8765, "Use a disposable listener and data root"
    verify(port, data)
