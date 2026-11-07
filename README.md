# sonarr-pack-updater

Gets new episodes into [Sonarr](https://sonarr.tv) from season packs that a tracker keeps updating as a season airs (`S01E1-5 of 10` → `S01E1-6 of 10`), the way most Russian trackers publish TV.

Sonarr rejects such an updated pack as a whole, because the episodes it already has are not upgrades (`Existing file on disk is of equal or higher preference`), so new episodes never arrive. Sonarr has no setting for this. Every few hours this service:

1. finds monitored seasons with aired episodes that are still missing (only recent ones, see `LOOKBACK_DAYS`);
2. runs Sonarr's interactive search for each such season and keeps releases that contain a missing episode and are either approved or rejected **only** because part of the season is on disk — releases rejected for quality, language, size, seeders and so on are left alone;
3. picks the one with the most missing episodes (then the same tracker topic as before, then custom format score, then seeders) and sends it to Sonarr's download client, exactly like pressing download in interactive search;
4. removes finished downloads from Sonarr's queue when every episode in them is on disk and the only complaint left is "Not an upgrade". The torrent keeps seeding.

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
      INTERVAL_HOURS: "3"
      LOOKBACK_DAYS: "60"
    volumes:
      - ./pack-updater:/data
      - /path/to/sonarr/config.xml:/sonarr/config.xml:ro
    restart: unless-stopped
```

Every pass logs each season it checks, each release it sends to Sonarr (`Отправлено на скачивание: …`) and each queue entry it removes (`Убрано из очереди: …`): `docker logs -f sonarr-pack-updater`.

| Variable | Default | Meaning |
| --- | --- | --- |
| `SONARR_URL` | `http://sonarr:8989` | Sonarr address as seen from this container |
| `SONARR_API_KEY` | — | API key; if empty it is read from `SONARR_CONFIG` |
| `SONARR_CONFIG` | `/sonarr/config.xml` | Sonarr's `config.xml`, mounted read-only |
| `INTERVAL_HOURS` | `3` | Hours between passes |
| `LOOKBACK_DAYS` | `60` | Only episodes that aired within this many days |
| `MAX_SEASONS_PER_RUN` | `15` | Upper limit of season searches per pass |
| `PAUSE_BETWEEN_SEARCHES` | `20` | Seconds between season searches, to be gentle with trackers |
| `STARTUP_DELAY` | `300` | Seconds to wait after start before the first pass, so Sonarr has re-read its download client queue (matters when the whole stack restarts) |
| `RETRY_MINUTES` | `10` | Minutes until the next pass when Sonarr could not be reached |
| `RUN_ONCE` | `false` | Do one pass and exit |

State (what has been grabbed) is kept in `/data/state.json`. The container reports unhealthy if passes stop starting.

## Things to know

- Every update of a pack is grabbed as a whole release. If the tracker renames the pack, it is downloaded again in full.
- After a tracker updates a torrent, the old one becomes unregistered. Tools like qbit_manage remove unregistered torrents; they keep the files when another torrent with the same name uses them.
- Releases that Sonarr mis-parses (for example a whole season read as episode 1) are only taken if the parsed episodes include a missing one.
