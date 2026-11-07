"""A tiny fake Sonarr API used by the smoke test.

It serves one airing season with an updated pack that Sonarr would reject only
because part of the season is on disk, a few releases that must be ignored, and
a queue with one finished download that should be cleaned up.
Writes every POST/DELETE it receives to stderr so the test can assert on them.
"""
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlparse

KEY = os.environ.get("MOCK_API_KEY", "testkey")
PORT = int(os.environ.get("MOCK_PORT", "18989"))
now = datetime.now(timezone.utc)


def iso(days):
    return (now + timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")


def ep(sid, season, num, days, title):
    return {"id": sid * 10000 + season * 100 + num, "seriesId": sid, "seasonNumber": season,
            "episodeNumber": num, "airDateUtc": iso(days), "hasFile": False, "monitored": True,
            "series": {"title": title}}


MISSING = ([ep(22, 18, e, -30 + e, "Always Sunny") for e in range(6, 11)]
           + [ep(22, 5, 3, -4000, "Always Sunny")]      # too old: ignored
           + [ep(22, 19, 1, 20, "Always Sunny")]         # not aired yet: ignored
           + [ep(5, 2, 4, -3, "Busy Show")])             # already downloading: skipped
EPISODES = {(22, 18): list(range(1, 11))}
EXISTING = "Existing file on disk is of equal or higher preference: WEBDL-1080p v1"
RELEASES = {(22, 18): [
    {"guid": "https://tracker/topic?t=111", "indexerId": 1, "indexer": "Tracker A",
     "title": "Sunny / S18E1-8 of 10 [WEB-DL 1080p] RUS", "approved": False, "fullSeason": False,
     "episodeNumbers": list(range(1, 9)), "rejections": [EXISTING],
     "customFormatScore": 20000, "seeders": 40, "size": 9000},
    {"guid": "https://tracker/topic?t=222", "indexerId": 2, "indexer": "Tracker B",
     "title": "Sunny S18E01-10 720p RUS", "approved": False, "fullSeason": True, "episodeNumbers": [],
     "rejections": ["WEBRip-720p is not wanted in profile", EXISTING],
     "customFormatScore": 20000, "seeders": 99, "size": 7000},
    {"guid": "https://tracker/topic?t=333", "indexerId": 3, "indexer": "Tracker C",
     "title": "Sunny S18E01-10 1080p RUS", "approved": False, "fullSeason": True, "episodeNumbers": [],
     "rejections": ["Not enough seeders: 0. Minimum seeders: 3", EXISTING],
     "customFormatScore": 20000, "seeders": 0, "size": 12000},
]}
HISTORY = {(22, 18): [{"eventType": "grabbed", "data": {"guid": "https://tracker/topic?t=111"}}]}
QUEUE = (
    [{"id": 100 + e, "seriesId": 30, "seasonNumber": 1, "downloadId": "HASH1", "title": "Old Show S01",
      "status": "completed", "trackedDownloadState": "importBlocked",
      "statusMessages": [{"title": "f", "messages": ["Not an upgrade for existing episode file(s)"]}],
      "episode": {"hasFile": True}} for e in range(1, 4)]
    + [{"id": 200, "seriesId": 31, "seasonNumber": 2, "downloadId": "HASH2", "title": "Waiting Show S02",
        "status": "completed", "trackedDownloadState": "importPending", "statusMessages": [],
        "episode": {"hasFile": True}}]
    + [{"id": 300, "seriesId": 5, "seasonNumber": 2, "downloadId": "HASH3", "title": "Busy Show S02",
        "status": "downloading", "trackedDownloadState": "downloading", "statusMessages": [],
        "episode": {"hasFile": False}}])


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def send(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.headers.get("X-Api-Key") != KEY:
            return self.send({"error": "unauthorized"}, 401)
        url = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(url.query).items()}
        path = url.path
        key = (int(q.get("seriesId", 0)), int(q.get("seasonNumber", 0)))
        if path.endswith("/wanted/missing"):
            page, size = int(q["page"]), int(q["pageSize"])
            self.send({"page": page, "totalRecords": len(MISSING),
                       "records": MISSING[(page - 1) * size:page * size]})
        elif path.endswith("/episode"):
            self.send([{"episodeNumber": e} for e in EPISODES.get(key, [])])
        elif path.endswith("/release"):
            self.send(RELEASES.get(key, []))
        elif path.endswith("/history/series"):
            self.send(HISTORY.get(key, []))
        elif path.endswith("/queue"):
            self.send({"records": QUEUE})
        else:
            self.send({"error": path}, 404)

    def _record(self):
        length = int(self.headers.get("Content-Length") or 0)
        sys.stderr.write(f"{self.command} {self.path} {self.rfile.read(length).decode()}\n")
        sys.stderr.flush()

    def do_POST(self):
        self._record()
        self.send({})

    def do_DELETE(self):
        self._record()
        self.send(None)


if __name__ == "__main__":
    HTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
