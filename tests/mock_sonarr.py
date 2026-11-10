"""A tiny fake Sonarr and Radarr API used by the smoke test.

It serves one airing season with an updated pack that Sonarr would reject only
because part of the season is on disk, a few releases that must be ignored, and
a queue with one finished download that should be cleaned up.
Writes every POST/DELETE it receives to stderr so the test can assert on them.
"""
import json
import os
import sys
import threading
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlparse

KEY = os.environ.get("MOCK_API_KEY", "testkey")
PORT = int(os.environ.get("MOCK_PORT", "18989"))
RADARR_PORT = int(os.environ.get("MOCK_RADARR_PORT", "17878"))
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
           + [ep(5, 2, 4, -3, "Busy Show")]              # already downloading: skipped
           + [ep(26, 1, e, -30 + 7 * e, "Treasure Island (2026)") for e in range(1, 5)]
           + [ep(8, 6, e, -50 + 6 * e, "JoJo's Bizarre Adventure (2012)") for e in range(2, 9)])
EPISODES = {(22, 18): list(range(1, 11)), (26, 1): list(range(1, 7)), (22, 5): list(range(1, 11)),
            (8, 6): list(range(1, 13)), (8, 5): list(range(1, 39))}
HAS_FILE = {(8, 6, 1), (8, 5, 1)}
SERIES = {22: {"title": "It's Always Sunny in Philadelphia", "year": 2005, "qualityProfileId": 7},
          26: {"title": "Treasure Island (2026)", "year": 2026, "qualityProfileId": 7},
          8: {"title": "JoJo's Bizarre Adventure (2012)", "year": 2012, "qualityProfileId": 7,
              "seriesType": "anime", "alternateTitles": [{"title": "JoJo no Kimyou na Bouken"}], "path": "/tv/JoJo"}}
PROFILES = {7: {"items": [
    {"quality": {"name": "WEBDL-720p"}, "items": [], "allowed": False},
    {"name": "1080p-2160p", "allowed": True, "items": [
        {"quality": {"name": n}, "items": [], "allowed": True}
        for n in ("WEBDL-2160p", "WEBRip-2160p", "Bluray-2160p", "Bluray-1080p", "WEBDL-1080p", "WEBRip-1080p")]}]}}
UNKNOWN = ["Unknown Series"]
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
], (22, 5): [
    # old season: an ordinary full-season pack Sonarr approves -> picked by the daily missing search
    {"guid": "https://tracker/topic?t=888", "indexerId": 1, "indexer": "Tracker A",
     "title": "Sunny / S5E1-10 of 10 [WEB-DL 1080p] RUS", "approved": True, "fullSeason": True,
     "episodeNumbers": [], "rejections": [], "customFormatScore": 20000, "seeders": 12, "size": 15000},
], (26, 1): [
    # Sonarr не сопоставил из-за года в названии сериала -> должен быть выбран этот
    {"guid": "https://tracker/topic?t=444", "indexerId": 5, "indexer": "Tracker D",
     "title": "Остров сокровищ / Treasure Island / S1E1-3 of 6 (2026) WEB-DL [H.264/1080p] RUS",
     "approved": False, "fullSeason": False, "seasonNumber": 1, "episodeNumbers": [1, 2, 3],
     "rejections": UNKNOWN, "downloadAllowed": False, "customFormatScore": 0, "seeders": 37, "size": 10000,
     "quality": {"quality": {"name": "WEBDL-1080p", "resolution": 1080}}},
    # качество не из профиля
    {"guid": "https://tracker/topic?t=555", "indexerId": 1, "indexer": "Tracker A",
     "title": "Остров сокровищ / Treasure Island / S1E1-4 of 6 [2026, WEBRip] RUS",
     "approved": False, "fullSeason": False, "seasonNumber": 1, "episodeNumbers": [1, 2, 3, 4],
     "rejections": UNKNOWN, "downloadAllowed": False, "customFormatScore": 0, "seeders": 70, "size": 2900,
     "quality": {"quality": {"name": "WEBRip-480p", "resolution": 480}}},
    # другой сериал с тем же названием (другой год)
    {"guid": "https://tracker/topic?t=666", "indexerId": 1, "indexer": "Tracker A",
     "title": "Остров сокровищ / Treasure Island / S1E1-4 of 4 [2012, WEB-DL 1080p] RUS",
     "approved": False, "fullSeason": True, "seasonNumber": 1, "episodeNumbers": [],
     "rejections": UNKNOWN, "downloadAllowed": False, "customFormatScore": 0, "seeders": 300, "size": 9000,
     "quality": {"quality": {"name": "WEBDL-1080p", "resolution": 1080}}},
    # мало сидов
    {"guid": "https://tracker/topic?t=777", "indexerId": 3, "indexer": "Tracker C",
     "title": "Treasure Island [S01E01-04 of 06] (2026) WEB-DL 2160p RUS",
     "approved": False, "fullSeason": False, "seasonNumber": 1, "episodeNumbers": [1, 2, 3, 4],
     "rejections": UNKNOWN, "downloadAllowed": False, "customFormatScore": 0, "seeders": 1, "size": 20000,
     "quality": {"quality": {"name": "WEBDL-2160p", "resolution": 2160}}},
]}
Q1080 = {"quality": {"name": "WEBDL-1080p", "resolution": 1080}}
RELEASES[(8, 6)] = [
    # трекер считает Steel Ball Run 7-м сезоном, Sonarr — 6-м: берём по таблице соответствий
    {"guid": "https://tracker/topic?t=901", "indexerId": 1, "indexer": "Tracker A",
     "title": "Невероятные приключения ДжоДжо: Гонка «Стальной шар» (S7) / Steel Ball Run: JoJo no Kimyou na Bouken "
              "[ONA] [01-08 из XX] [UKR, ENG, JAP] & [01-07 из XX] [RUS(int)] [2026, WEB-DL] [1080p] RUS",
     "approved": False, "seasonNumber": 7, "episodeNumbers": [], "rejections": UNKNOWN, "downloadAllowed": False,
     "customFormatScore": 0, "seeders": 77, "size": 9500, "quality": Q1080, "languages": [{"id": 11, "name": "Russian"}]},
    # манга с тем же названием — нельзя
    {"guid": "https://tracker/topic?t=902", "indexerId": 1, "indexer": "Tracker A",
     "title": "Удивительные приключения ДжоДжо, часть 7 / JoJo Part 7: Steel Ball Run [manga][главы 1-95][2004] (01-12)",
     "approved": False, "seasonNumber": 7, "episodeNumbers": [], "rejections": UNKNOWN, "downloadAllowed": False,
     "customFormatScore": 0, "seeders": 500, "size": 1200, "quality": Q1080},
    # другой сезон по трекеру — нельзя
    {"guid": "https://tracker/topic?t=903", "indexerId": 1, "indexer": "Tracker A",
     "title": "Steel Ball Run: JoJo no Kimyou na Bouken S5E1-12 WEB-DL 1080p RUS",
     "approved": False, "seasonNumber": 5, "episodeNumbers": [], "rejections": ["Wrong season"],
     "customFormatScore": 0, "seeders": 300, "size": 9000, "quality": Q1080},
    # мало сидов и запрещённое качество — нельзя
    {"guid": "https://tracker/topic?t=904", "indexerId": 1, "indexer": "Tracker A",
     "title": "Steel Ball Run: JoJo no Kimyou na Bouken - S7E1-8 - 2026 WEBDL 1080p - RUSSIAN",
     "approved": False, "seasonNumber": 7, "episodeNumbers": [], "rejections": UNKNOWN,
     "customFormatScore": 0, "seeders": 1, "size": 9400, "quality": Q1080},
    {"guid": "https://tracker/topic?t=905", "indexerId": 1, "indexer": "Tracker A",
     "title": "Steel Ball Run: JoJo no Kimyou na Bouken (1—8) [480p] RUS",
     "approved": False, "seasonNumber": 0, "episodeNumbers": [], "rejections": UNKNOWN,
     "customFormatScore": 0, "seeders": 90, "size": 3000, "quality": {"quality": {"name": "WEBRip-480p", "resolution": 480}}},
    # отказ по чёрному списку — нельзя, даже по таблице
    {"guid": "https://tracker/topic?t=906", "indexerId": 1, "indexer": "Tracker A",
     "title": "Steel Ball Run: JoJo no Kimyou na Bouken [01-08 из 12] WEB-DL 1080p RUS",
     "approved": False, "seasonNumber": 7, "episodeNumbers": [], "rejections": ["Release is blocklisted"],
     "customFormatScore": 0, "seeders": 200, "size": 9600, "quality": Q1080},
]
# скачанное по таблице: Sonarr не понял «S06E03» (у него Stone Ocean — 5-й сезон), импортируем сами
MANUAL_IMPORT = {"HASH8": [
    {"path": "/data/torrents/SO/[Kazoku] Stone Ocean - 02 [1080p].mkv", "relativePath": "[Kazoku] Stone Ocean - 02 [1080p].mkv",
     "folderName": "SO", "quality": Q1080, "languages": [{"id": 11}], "rejections": [{"reason": "Invalid season or episode"}]},
    {"path": "/data/torrents/SO/Stone Ocean S06E03 1080p.mkv", "relativePath": "Stone Ocean S06E03 1080p.mkv",
     "folderName": "SO", "quality": Q1080, "languages": [{"id": 11}], "rejections": [{"reason": "Invalid season or episode"}]},
    {"path": "/data/torrents/SO/Stone Ocean - 01 [1080p].mkv", "relativePath": "Stone Ocean - 01 [1080p].mkv",
     "folderName": "SO", "quality": Q1080, "languages": [], "rejections": [{"reason": "Invalid season or episode"}]},
    {"path": "/data/torrents/SO/sample.mkv", "relativePath": "sample.mkv", "folderName": "SO",
     "quality": Q1080, "languages": [], "rejections": [{"reason": "Sample"}]},
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
    # partial pack: torrent name says S01 (6 episodes), the release had only 1-3; all of them imported
    + [{"id": 600 + e, "seriesId": 40, "seasonNumber": 1, "downloadId": "HASH6", "title": "Partial.Show.S01",
        "status": "completed", "trackedDownloadState": "importBlocked",
        "statusMessages": [{"title": "One or more episodes expected in this release were not imported or missing from the release", "messages": []}],
        "episode": {"hasFile": e <= 3}} for e in range(1, 7)]
    # partial pack where a file really failed to import -> must stay
    + [{"id": 700 + e, "seriesId": 41, "seasonNumber": 1, "downloadId": "HASH7", "title": "Broken.Partial.S01",
        "status": "completed", "trackedDownloadState": "importBlocked",
        "statusMessages": [{"title": "One or more episodes expected in this release were not imported or missing from the release", "messages": []},
                           {"title": "e02.mkv", "messages": ["Sample", "Not an upgrade for existing episode file(s)"]}],
        "episode": {"hasFile": e == 1}} for e in range(1, 4)]
    + [{"id": 800, "seriesId": 8, "seasonNumber": 5, "downloadId": "HASH8", "title": "Stone Ocean [01-03]",
        "status": "completed", "trackedDownloadState": "importBlocked",
        "statusMessages": [{"title": "Stone Ocean S06E03 1080p.mkv", "messages": ["Invalid season or episode"]}],
        "episode": {"hasFile": False}}]
    + [{"id": 300, "seriesId": 5, "seasonNumber": 2, "downloadId": "HASH3", "title": "Busy Show S02",
        "status": "downloading", "trackedDownloadState": "downloading", "statusMessages": [],
        "episode": {"hasFile": False}}])


# Radarr: one available missing movie, one not yet available, one already downloading
MOVIES = [{"id": 50, "title": "Kung Fu Soccer", "year": 2026, "isAvailable": True, "hasFile": False},
          {"id": 77, "title": "Other Mommy", "year": 2026, "isAvailable": False, "hasFile": False},
          {"id": 60, "title": "Busy Movie", "year": 2025, "isAvailable": True, "hasFile": False}]
RADARR_QUEUE = [
    {"id": 1, "movieId": 60, "title": "Busy.Movie.2025.mkv"},
    # скачан, но не лучше файла на диске -> убрать
    {"id": 2, "movieId": 61, "title": "Spider-Man (2026) WEB-DLRip 2160p.mkv", "status": "completed",
     "trackedDownloadState": "importPending", "statusMessages": [{"title": "Spider-Man (2026) WEB-DLRip 2160p.mkv", "messages": [
         "Not a Custom Format upgrade for existing movie file(s). New: [2160p] (6000) do not improve on Existing: [2160p, Russian Audio] (26000)"]}]},
    # другая причина -> оставить человеку
    {"id": 3, "movieId": 62, "title": "Odd.Movie.2026.mkv", "status": "completed", "trackedDownloadState": "importPending",
     "statusMessages": [{"title": "Odd.Movie.2026.mkv", "messages": ["Not an upgrade for existing movie file(s)", "Sample"]}]},
    # ещё качается -> не трогать
    {"id": 4, "movieId": 63, "title": "Slow.Movie.2026.mkv", "status": "downloading", "trackedDownloadState": "downloading",
     "statusMessages": []}]


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
        if self.path.startswith("/api/push/"):   # push-монитор (Uptime Kuma)
            sys.stderr.write(f"PUSH {self.path}\n")
            sys.stderr.flush()
            return self.send({"ok": True})
        if self.headers.get("X-Api-Key") != KEY:
            return self.send({"error": "unauthorized"}, 401)
        url = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(url.query).items()}
        path = url.path
        if path.endswith("/system/status"):
            started = now - timedelta(seconds=int(os.environ.get("MOCK_UPTIME", "3600")))
            return self.send({"startTime": started.strftime("%Y-%m-%dT%H:%M:%SZ")})
        if self.server.server_port == RADARR_PORT:
            if path.endswith("/wanted/missing"):
                return self.send({"page": 1, "totalRecords": len(MOVIES), "records": MOVIES})
            if path.endswith("/queue"):
                return self.send({"records": RADARR_QUEUE})
            return self.send({"error": path}, 404)
        key = (int(q.get("seriesId", 0)), int(q.get("seasonNumber", 0)))
        if path.endswith("/wanted/missing"):
            page, size = int(q["page"]), int(q["pageSize"])
            self.send({"page": page, "totalRecords": len(MISSING),
                       "records": MISSING[(page - 1) * size:page * size]})
        elif path.endswith("/episode"):
            self.send([{"id": key[0] * 10000 + key[1] * 100 + e, "episodeNumber": e,
                        "hasFile": (key[0], key[1], e) in HAS_FILE} for e in EPISODES.get(key, [])])
        elif path.endswith("/manualimport"):
            if "seriesId" in q:   # как настоящий Sonarr: с seriesId отдаёт файлы папки сериала, а не загрузки
                return self.send([{"path": "/tv/JoJo/Season 1/JoJo - S01E13 - Title.mkv",
                                   "relativePath": "JoJo - S01E13 - Title.mkv", "rejections": []}])
            self.send(MANUAL_IMPORT.get(q.get("downloadId"), []))
        elif path.endswith("/release"):
            self.send(RELEASES.get(key, []))
        elif "/series/" in path and path.rsplit("/", 1)[-1].isdigit():
            sid = int(path.rsplit("/", 1)[-1])
            self.send(SERIES.get(sid, {"title": f"Show {sid}", "year": 2020, "qualityProfileId": 7}))
        elif "/qualityprofile/" in path:
            self.send(PROFILES[int(path.rsplit("/", 1)[-1])])
        elif path.endswith("/history/series"):
            self.send(HISTORY.get(key, []))
        elif path.endswith("/queue"):
            self.send({"records": QUEUE})
        else:
            self.send({"error": path}, 404)

    def _record(self):
        length = int(self.headers.get("Content-Length") or 0)
        app = "RADARR " if self.server.server_port == RADARR_PORT else ""
        sys.stderr.write(f"{app}{self.command} {self.path} {self.rfile.read(length).decode()}\n")
        sys.stderr.flush()

    def do_POST(self):
        self._record()
        self.send({})

    def do_DELETE(self):
        self._record()
        self.send(None)


if __name__ == "__main__":
    threading.Thread(target=HTTPServer(("0.0.0.0", RADARR_PORT), Handler).serve_forever, daemon=True).start()
    HTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
