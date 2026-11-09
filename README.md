# sonarr-pack-updater

Gets new episodes into [Sonarr](https://sonarr.tv) from season packs that a tracker keeps updating as a season airs (`S01E1-5 of 10` → `S01E1-6 of 10`), the way most Russian trackers publish TV.

Sonarr rejects such an updated pack as a whole, because the episodes it already has are not upgrades (`Existing file on disk is of equal or higher preference`), so new episodes never arrive. Sonarr has no setting for this. Every few hours this service:

1. finds monitored seasons with aired episodes that are still missing (only recent ones, see `LOOKBACK_DAYS`);
2. runs Sonarr's interactive search for each such season and keeps releases that contain a missing episode and are either approved or rejected **only** because part of the season is on disk — releases rejected for quality, language, size, seeders and so on are left alone;
3. picks the one with the most missing episodes (then the same tracker topic as before, then custom format score, then seeders) and sends it to Sonarr's download client, exactly like pressing download in interactive search;
   Releases that Sonarr rejects as `Unknown Series` — typically because the series is called "Name (2026)" in Sonarr while trackers write just "Name" — are also considered, only if nothing Sonarr matched itself fits: the title must contain the series name **and** its year, the parsed season must match, the quality must be allowed by the series' profile and there must be at least `MIN_SEEDERS` seeders (Sonarr stops checking such releases, so the service checks this itself). They are grabbed with the series set explicitly, like choosing the series by hand in interactive search.
4. at the start of every pass, removes finished downloads from Sonarr's queue when everything importable has been imported: the remaining files are "Not an upgrade", and episodes that were simply not in a partial pack (Sonarr expects the whole season from a torrent named `S01`) are missing. The torrent keeps seeding. Anything with another import problem is left alone. Without this a partial pack would stay in the queue and keep its season "busy".

5. for series that trackers number differently from Sonarr — typically anime, where trackers count each part as its own season (JoJo's *Steel Ball Run* is "S7" on trackers and season 6 in Sonarr) — uses a remap table you write yourself (see below). Only listed series and seasons are affected, and only when nothing Sonarr matched itself fits. If a season keeps getting rejected releases that carry the series name ("Unknown Series", "Wrong season"), the log suggests adding a rule.

6. stops download loops in Sonarr and Radarr: when the very same release (same torrent hash) is grabbed again after it was already imported for that movie or episode, it is removed from the queue (or, if it already left the queue, marked failed) and blocklisted. Files and the torrent stay. This happens when a release scores more custom format points by its title than the imported file does by its file name (the title says `RUS, HDR10+`, the file name doesn't), so the same release looks like an upgrade every time. A grab after the file was deleted by hand or went missing is left alone; only grabs from the last 48 hours are considered.

Once a day (`MISSING_SEARCH_HOURS`) it also goes after missing items that Sonarr and Radarr never search for again on their own — both only watch RSS after an item is added:

- **Sonarr:** monitored seasons whose missing episodes aired before `LOOKBACK_DAYS`, a few per day (`MISSING_SEASONS_PER_RUN`), least recently checked first, with the same logic as above.
- **Radarr** (when `RADARR_URL` is set): monitored movies that are available but have no file, a few per day (`MISSING_MOVIES_PER_RUN`), least recently searched first, using Radarr's own search. Movies already in Radarr's queue are skipped.

When the updated pack lands in the same folder as the previous one, qBittorrent checks the files already there and downloads only the new episodes. Seasons that already have something in Sonarr's queue are skipped, and the same version of a release (guid + size) is never grabbed twice.

Logs are in Russian. Not affiliated with Sonarr.

## Run

The image is built for amd64 and arm64 on every change and rebuilt weekly.

```yaml
services:
  sonarr-pack-updater:
    image: ghcr.io/dmmedakin/sonarr-pack-updater:latest
    container_name: sonarr-pack-updater
    user: "1000:1000"
    environment:
      SONARR_URL: http://sonarr:8989
      SONARR_CONFIG: /sonarr/config.xml   # or SONARR_API_KEY: ...
      RADARR_URL: http://radarr:7878      # optional: daily search for missing movies
      RADARR_CONFIG: /radarr/config.xml   # or RADARR_API_KEY: ...
      INTERVAL_HOURS: "3"
      LOOKBACK_DAYS: "60"
    volumes:
      - ./pack-updater:/data
      - /path/to/sonarr/config.xml:/sonarr/config.xml:ro
      - /path/to/radarr/config.xml:/radarr/config.xml:ro
    restart: unless-stopped
```

Every pass logs each season it checks (with the most common rejection reasons when nothing fits), each release it sends to Sonarr (`Отправлено на скачивание: …`) and each queue entry it removes (`Убрано из очереди: …`): `docker logs -f sonarr-pack-updater`.

| Variable | Default | Meaning |
| --- | --- | --- |
| `SONARR_URL` | `http://sonarr:8989` | Sonarr address as seen from this container |
| `SONARR_API_KEY` | — | API key; if empty it is read from `SONARR_CONFIG` |
| `SONARR_CONFIG` | `/sonarr/config.xml` | Sonarr's `config.xml`, mounted read-only |
| `INTERVAL_HOURS` | `3` | Hours between passes |
| `LOOKBACK_DAYS` | `60` | Only episodes that aired within this many days |
| `MAX_SEASONS_PER_RUN` | `15` | Upper limit of season searches per pass |
| `PAUSE_BETWEEN_SEARCHES` | `20` | Seconds between season searches, to be gentle with trackers |
| `STARTUP_DELAY` | `300` | If Sonarr or Radarr started less than this many seconds ago, wait until they have been up that long (they need time to re-read the download client queue). Otherwise the first pass starts right away, so the container itself can be restarted at any time; progress is kept in `/data/state.json` |
| `RETRY_MINUTES` | `10` | Minutes until the next pass when Sonarr could not be reached |
| `MIN_SEEDERS` | `3` | Minimum seeders for `Unknown Series` releases |
| `RADARR_URL` | — | Radarr address; leave empty to skip movies |
| `RADARR_API_KEY` | — | API key; if empty it is read from `RADARR_CONFIG` |
| `RADARR_CONFIG` | `/radarr/config.xml` | Radarr's `config.xml`, mounted read-only |
| `MISSING_SEARCH_HOURS` | `24` | Hours between searches for older missing seasons and missing movies |
| `MISSING_SEASONS_PER_RUN` | `10` | Older seasons checked per missing search |
| `MISSING_MOVIES_PER_RUN` | `20` | Movies searched per missing search |
| `PUSH_URL` | — | Optional push monitor (e.g. an Uptime Kuma push URL) called after every successful pass |
| `REMAP_FILE` | `/data/remap.json` | Remap table for series that trackers number differently; optional, re-read when it changes |
| `RUN_ONCE` | `false` | Do one pass and exit |

State (what has been grabbed) is kept in `/data/state.json`. The container reports unhealthy if passes stop starting.

## Remap table

`/data/remap.json`, keyed by the series title exactly as in Sonarr:

```json
{
  "JoJo's Bizarre Adventure (2012)": [
    {"names": ["Steel Ball Run", "Стальной шар"], "tracker_season": 7, "season": 6}
  ]
}
```

- `season` — the season in Sonarr; `tracker_season` — how trackers number it (optional); `names` — what release titles contain.
- A release is taken by a rule only if Sonarr rejected it **only** for not understanding the series, season or episodes; its title contains one of `names`; any season number in the title is `tracker_season` or `season`; the episodes can be read from the title (`S7E1-8`, `[01-08 из XX]`, `(1—7)`, `[08 из 12]`) and include a missing one; it is video (not manga or OST); its quality is allowed by the series profile and it has at least `MIN_SEEDERS` seeders.
- It is grabbed with the series and episodes set explicitly, like "override" in interactive search. If Sonarr then cannot import the files (they are named `S07E05`), the service imports them itself — only when every file's episode number can be read unambiguously (`S07E05`, `S01E05` or a bare `- 05`), all of them exist in the season and none repeat; episodes already on disk are not replaced. Otherwise the download is left in the queue for you.
- Anime searches are slow (Sonarr searches every episode under every title), so the service waits up to 20 minutes for them.

## Things to know

- Every update of a pack is grabbed as a whole release. If the tracker renames the pack, it is downloaded again in full.
- After a tracker updates a torrent, the old one becomes unregistered. Tools like qbit_manage remove unregistered torrents; they keep the files when another torrent with the same name uses them.
- Releases that Sonarr mis-parses (for example a whole season read as episode 1) are only taken if the parsed episodes include a missing one.
