#!/usr/bin/env python3
"""
Sonarr pack updater.

Русские трекеры обновляют раздачу сезона по мере выхода серий (S01E1-5 -> S01E1-6).
Sonarr отклоняет обновлённую раздачу целиком, потому что часть серий в ней уже есть
("Existing file on disk is of equal or higher preference"). Этот сервис:
  1. находит отслеживаемые сезоны с вышедшими, но не скачанными сериями;
  2. ищет по ним раздачи и берёт ту, что отклонена ТОЛЬКО из-за уже имеющихся файлов
     и при этом содержит недостающие серии;
  3. отправляет её в Sonarr на скачивание (как ручной выбор в интерактивном поиске);
  4. убирает из очереди загрузки, у которых новые серии импортированы, а старые
     висят с «Not an upgrade».
Только стандартная библиотека Python.
"""
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

SONARR_URL = os.environ.get("SONARR_URL", "http://sonarr:8989").rstrip("/")
SONARR_API_KEY = os.environ.get("SONARR_API_KEY", "")
SONARR_CONFIG = os.environ.get("SONARR_CONFIG", "/sonarr/config.xml")
INTERVAL_HOURS = float(os.environ.get("INTERVAL_HOURS", "3"))
LOOKBACK_DAYS = int(os.environ.get("LOOKBACK_DAYS", "60"))
MAX_SEASONS_PER_RUN = int(os.environ.get("MAX_SEASONS_PER_RUN", "15"))
PAUSE_BETWEEN_SEARCHES = int(os.environ.get("PAUSE_BETWEEN_SEARCHES", "20"))
STATE_FILE = os.environ.get("STATE_FILE", "/data/state.json")
HEARTBEAT_FILE = os.environ.get("HEARTBEAT_FILE", "/tmp/heartbeat")
RUN_ONCE = os.environ.get("RUN_ONCE", "false").lower() in ("1", "true", "yes")

# Отказы, которые означают только «часть серий уже есть»
ALLOWED_REJECTION = re.compile(r"^(Existing file|Existing files)", re.I)
# Сообщения импорта, означающие «эта серия уже есть, не улучшение»
NOT_UPGRADE_MSG = re.compile(r"not an upgrade|already imported|equal or higher", re.I)


def log(msg):
    print(f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} {msg}", flush=True)


def api_key():
    if SONARR_API_KEY:
        return SONARR_API_KEY
    with open(SONARR_CONFIG, encoding="utf-8") as f:
        m = re.search(r"<ApiKey>(\w+)</ApiKey>", f.read())
    if not m:
        sys.exit(f"Не нашёл ApiKey в {SONARR_CONFIG}")
    return m.group(1)


KEY = None


def call(path, data=None, method="GET", timeout=120):
    body = None if data is None else json.dumps(data).encode()
    req = urllib.request.Request(
        SONARR_URL + "/api/v3" + path, data=body, method=method,
        headers={"X-Api-Key": KEY, "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read()
        return json.loads(raw) if raw else None


def load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"grabbed": {}}


def save_state(state):
    # держим историю 120 дней
    cutoff = time.time() - 120 * 86400
    state["grabbed"] = {k: v for k, v in state["grabbed"].items() if v.get("ts", 0) > cutoff}
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=1)
    os.replace(tmp, STATE_FILE)


def parse_dt(s):
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


def rejection_texts(release):
    out = []
    for r in release.get("rejections") or []:
        out.append(r if isinstance(r, str) else (r.get("reason") or r.get("message") or str(r)))
    return out


def release_episodes(release, season_episode_numbers):
    """Номера серий, которые покрывает раздача."""
    if release.get("fullSeason"):
        return set(season_episode_numbers)
    info = release.get("mappedEpisodeInfo") or []
    nums = {e.get("episodeNumber") for e in info if e.get("episodeNumber") is not None}
    if not nums:
        nums = set(release.get("mappedEpisodeNumbers") or release.get("episodeNumbers") or [])
    return nums


def missing_seasons():
    """{(seriesId, season): {"title":..., "missing": set(episodeNumber)}}"""
    now = datetime.now(timezone.utc)
    since = now - timedelta(days=LOOKBACK_DAYS)
    result, page = {}, 1
    while True:
        data = call(f"/wanted/missing?page={page}&pageSize=250&monitored=true&includeSeries=true")
        records = data.get("records", [])
        for ep in records:
            aired = parse_dt(ep.get("airDateUtc"))
            if not aired or aired > now or aired < since or ep.get("hasFile"):
                continue
            if ep.get("seasonNumber", 0) == 0:  # спецвыпуски пропускаем
                continue
            k = (ep["seriesId"], ep["seasonNumber"])
            title = (ep.get("series") or {}).get("title") or f"series {ep['seriesId']}"
            result.setdefault(k, {"title": title, "missing": set()})["missing"].add(ep["episodeNumber"])
        if page * 250 >= data.get("totalRecords", 0) or not records:
            break
        page += 1
    return result


def queue_records():
    return call("/queue?pageSize=1000&includeEpisode=true&includeSeries=false").get("records", [])


def grabbed_topics(series_id, season):
    """guid раздач, которые уже скачивались для этого сезона (та же тема трекера)."""
    guids = set()
    try:
        hist = call(f"/history/series?seriesId={series_id}&seasonNumber={season}&eventType=grabbed")
    except urllib.error.HTTPError:
        hist = call(f"/history/series?seriesId={series_id}&seasonNumber={season}")
    for h in hist or []:
        if str(h.get("eventType")).lower() not in ("grabbed", "1"):
            continue
        for k, v in (h.get("data") or {}).items():
            if k.lower() == "guid" and v:
                guids.add(v)
    return guids


def process_season(series_id, season, info, state):
    title, missing = info["title"], info["missing"]
    label = f"{title} S{season:02d}"
    episodes = call(f"/episode?seriesId={series_id}&seasonNumber={season}")
    season_nums = [e["episodeNumber"] for e in episodes]
    log(f"{label}: не хватает серий {sorted(missing)}, ищу раздачи…")

    releases = call(f"/release?seriesId={series_id}&seasonNumber={season}", timeout=600)
    topics = grabbed_topics(series_id, season)

    candidates = []
    for r in releases:
        rej = rejection_texts(r)
        # Берём одобренные раздачи и те, что отклонены ТОЛЬКО из-за уже имеющихся серий
        if not r.get("approved") and not (rej and all(ALLOWED_REJECTION.search(x) for x in rej)):
            continue
        if r.get("downloadAllowed") is False:
            continue
        covers = release_episodes(r, season_nums) & missing
        if not covers:
            continue
        version = f"{r.get('guid')}|{r.get('size')}"
        if version in state["grabbed"]:
            continue
        candidates.append((
            len(covers),
            1 if r.get("guid") in topics else 0,
            r.get("customFormatScore") or 0,
            r.get("seeders") or 0,
            r, covers, version))

    if not candidates:
        log(f"{label}: подходящих обновлённых раздач нет")
        return False

    candidates.sort(key=lambda c: c[:4], reverse=True)
    n, same, score, seeds, r, covers, version = candidates[0]
    what = (f"{label}: «{r['title'][:140]}» [{r.get('indexer')}] — новые серии {sorted(covers)}, "
            f"очки {score}, сиды {seeds}{', та же тема' if same else ''}")
    call("/release", {"guid": r["guid"], "indexerId": r["indexerId"]}, "POST", timeout=180)
    state["grabbed"][version] = {"ts": time.time(), "title": r["title"], "series": label}
    save_state(state)
    log("Отправлено на скачивание: " + what)
    return True


def cleanup_queue():
    """Убрать загрузки, где всё нужное импортировано, а остальное «не улучшение»."""
    by_download = {}
    for q in queue_records():
        if q.get("status") != "completed":
            continue
        if q.get("trackedDownloadState") not in ("importBlocked", "importPending"):
            continue
        by_download.setdefault(q.get("downloadId") or q["id"], []).append(q)
    for dl, items in by_download.items():
        msgs = [m for q in items for s in (q.get("statusMessages") or []) for m in (s.get("messages") or [])]
        # Убираем только если Sonarr прямо пишет «не улучшение» и других проблем нет
        if not msgs or not all(NOT_UPGRADE_MSG.search(m) for m in msgs):
            continue
        all_have_files = all((q.get("episode") or {}).get("hasFile") for q in items)
        if not all_have_files:
            continue
        name = items[0].get("title", dl)
        call("/queue/bulk?removeFromClient=false&blocklist=false&skipRedownload=true",
             {"ids": [q["id"] for q in items]}, "DELETE")
        log(f"Убрано из очереди (раздача продолжает раздаваться): {name}")


def run_once(state):
    seasons = missing_seasons()
    busy = {(q.get("seriesId"), q.get("seasonNumber")) for q in queue_records()}
    todo = [(k, v) for k, v in seasons.items() if k not in busy]
    skipped = len(seasons) - len(todo)
    log(f"Сезонов с недостающими сериями: {len(seasons)} (уже качаются: {skipped}), "
        f"проверяю до {MAX_SEASONS_PER_RUN}")
    for i, ((sid, season), info) in enumerate(todo[:MAX_SEASONS_PER_RUN]):
        if i:
            time.sleep(PAUSE_BETWEEN_SEARCHES)
        try:
            process_season(sid, season, info, state)
        except Exception as e:
            log(f"{info['title']} S{season:02d}: ошибка — {e}")
    try:
        cleanup_queue()
    except Exception as e:
        log(f"Очистка очереди: ошибка — {e}")


def beat():
    """Отметка для HEALTHCHECK: контейнер жив и проходы идут."""
    try:
        with open(HEARTBEAT_FILE, "w") as f:
            f.write(str(time.time()))
    except OSError:
        pass


def main():
    global KEY
    KEY = api_key()
    log(f"Старт: Sonarr {SONARR_URL}, раз в {INTERVAL_HOURS} ч, серии за {LOOKBACK_DAYS} дн.")
    state = load_state()
    while True:
        beat()
        try:
            run_once(state)
        except Exception as e:
            log(f"Ошибка прохода: {e}")
        if RUN_ONCE:
            break
        time.sleep(INTERVAL_HOURS * 3600)


if __name__ == "__main__":
    main()
