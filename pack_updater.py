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
Раз в сутки дополнительно ищет пропущенное, до которого Sonarr и Radarr сами не
возвращаются: старые сезоны с недостающими сериями (той же логикой) и вышедшие, но не
скачанные фильмы в Radarr.
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
# После старта ждём, пока Sonarr перечитает очередь из торрент-клиента (важно при перезапуске стека)
STARTUP_DELAY = int(os.environ.get("STARTUP_DELAY", "300"))
# Если Sonarr недоступен, следующий проход — через столько минут, а не через INTERVAL_HOURS
RETRY_MINUTES = float(os.environ.get("RETRY_MINUTES", "10"))
# Минимум сидов для раздач, которые Sonarr не сопоставил с сериалом (его собственная проверка до них не доходит)
MIN_SEEDERS = int(os.environ.get("MIN_SEEDERS", "3"))
# Radarr: включается, если задан RADARR_URL
RADARR_URL = os.environ.get("RADARR_URL", "").rstrip("/")
RADARR_API_KEY = os.environ.get("RADARR_API_KEY", "")
RADARR_CONFIG = os.environ.get("RADARR_CONFIG", "/radarr/config.xml")
# Поиск пропущенного: как часто и сколько за раз (чтобы не нагружать трекеры)
MISSING_SEARCH_HOURS = float(os.environ.get("MISSING_SEARCH_HOURS", "24"))
MISSING_SEASONS_PER_RUN = int(os.environ.get("MISSING_SEASONS_PER_RUN", "10"))
MISSING_MOVIES_PER_RUN = int(os.environ.get("MISSING_MOVIES_PER_RUN", "20"))

# Отказы, которые означают только «часть серий уже есть»
ALLOWED_REJECTION = re.compile(r"^(Existing file|Existing files)", re.I)
# Сообщения импорта, означающие «эта серия уже есть, не улучшение»
NOT_UPGRADE_MSG = re.compile(r"not an upgrade|already imported|equal or higher", re.I)


def log(msg):
    print(f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} {msg}", flush=True)


def api_key(explicit, config):
    if explicit:
        return explicit
    with open(config, encoding="utf-8") as f:
        m = re.search(r"<ApiKey>(\w+)</ApiKey>", f.read())
    if not m:
        sys.exit(f"Не нашёл ApiKey в {config}")
    return m.group(1)


KEY = None
RADARR_KEY = None


def _request(base, key, path, data=None, method="GET", timeout=120):
    body = None if data is None else json.dumps(data).encode()
    req = urllib.request.Request(
        base + "/api/v3" + path, data=body, method=method,
        headers={"X-Api-Key": key, "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read()
        return json.loads(raw) if raw else None


def call(path, data=None, method="GET", timeout=120):
    """Запрос к Sonarr."""
    return _request(SONARR_URL, KEY, path, data, method, timeout)


def rcall(path, data=None, method="GET", timeout=120):
    """Запрос к Radarr."""
    return _request(RADARR_URL, RADARR_KEY, path, data, method, timeout)


def load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_state(state):
    # держим историю 120 дней
    cutoff = time.time() - 120 * 86400
    state["grabbed"] = {k: v for k, v in state["grabbed"].items() if v.get("ts", 0) > cutoff}
    state["searched"] = {k: v for k, v in state["searched"].items() if v > cutoff}
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


def missing_seasons(lookback_days=None):
    """{(seriesId, season): {"title":..., "missing": set(episodeNumber)}}.
    lookback_days=None — все вышедшие серии, иначе только вышедшие за столько дней."""
    now = datetime.now(timezone.utc)
    since = now - timedelta(days=lookback_days) if lookback_days else datetime.min.replace(tzinfo=timezone.utc)
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


UNKNOWN_SERIES = re.compile(r"^Unknown Series", re.I)
RUSSIAN_MARK = re.compile(r"\b(RUS|Russian|MVO|DVO|AVO|Dub|Дубляж|Дублированный)\b|[А-Яа-яЁё]", re.I)


def norm(text):
    """Только буквы и цифры в нижнем регистре: «Treasure Island (2026)» -> «treasureisland2026»."""
    return re.sub(r"[^0-9a-zа-яё]+", "", text.lower())


_profiles = {}


def series_profile(series_id):
    """Название без года, год и разрешённые качества профиля сериала."""
    if series_id not in _profiles:
        series = call(f"/series/{series_id}")
        profile = call(f"/qualityprofile/{series['qualityProfileId']}")
        allowed = set()
        for item in profile.get("items", []):
            if not item.get("allowed"):
                continue
            if item.get("quality"):
                allowed.add(item["quality"]["name"])
            for sub in item.get("items") or []:
                allowed.add(sub["quality"]["name"])
        bare = re.sub(r"\s*\(\d{4}\)\s*$", "", series.get("title", ""))
        _profiles[series_id] = {"title": norm(bare), "year": str(series.get("year") or ""),
                                "allowed": allowed}
    return _profiles[series_id]


def unknown_series_ok(r, season, series_id):
    """Раздача, которую Sonarr не сопоставил с сериалом (обычно из-за года в названии сериала).
    Sonarr её дальше не проверял, поэтому проверяем сами: тот же сериал и год, сезон, качество, сиды."""
    prof = series_profile(series_id)
    title = norm(r.get("title", ""))
    if not prof["title"] or prof["title"] not in title:
        return False
    if prof["year"] and prof["year"] not in r.get("title", ""):
        return False   # защита от одноимённых сериалов других лет
    if r.get("seasonNumber") != season:
        return False
    if (r.get("quality") or {}).get("quality", {}).get("name") not in prof["allowed"]:
        return False
    return (r.get("seeders") or 0) >= MIN_SEEDERS


def process_season(series_id, season, info, state):
    title, missing = info["title"], info["missing"]
    label = f"{title} S{season:02d}"
    episodes = call(f"/episode?seriesId={series_id}&seasonNumber={season}")
    season_nums = [e["episodeNumber"] for e in episodes]
    log(f"{label}: не хватает серий {sorted(missing)}, ищу раздачи…")

    releases = call(f"/release?seriesId={series_id}&seasonNumber={season}", timeout=600)
    topics = grabbed_topics(series_id, season)

    candidates, reasons = [], {}
    for r in releases:
        rej = rejection_texts(r)
        if r.get("approved") or (rej and all(ALLOWED_REJECTION.search(x) for x in rej)):
            matched = 1   # Sonarr распознал и проверил раздачу сам
        elif rej and all(UNKNOWN_SERIES.search(x) for x in rej) and unknown_series_ok(r, season, series_id):
            matched = 0   # сериал указываем вручную
        else:
            for x in rej:
                key = re.split(r"[:(]", x, maxsplit=1)[0].strip()
                reasons[key] = reasons.get(key, 0) + 1
            continue
        # Sonarr ставит downloadAllowed=false всем раздачам, которые не сопоставил с сериалом;
        # при скачивании с явно указанным seriesId он этот флаг не проверяет
        if matched and r.get("downloadAllowed") is False:
            continue
        covers = release_episodes(r, season_nums) & missing
        if not covers:
            continue
        version = f"{r.get('guid')}|{r.get('size')}"
        if version in state["grabbed"]:
            continue
        quality = (r.get("quality") or {}).get("quality", {})
        candidates.append((
            matched,
            len(covers),
            1 if r.get("guid") in topics else 0,
            r.get("customFormatScore") or 0,
            1 if RUSSIAN_MARK.search(r.get("title", "")) else 0,
            quality.get("resolution") or 0,
            r.get("seeders") or 0,
            r, covers, version))

    if not candidates:
        top = sorted(reasons.items(), key=lambda kv: -kv[1])[:3]
        why = "; ".join(f"{k} ×{v}" for k, v in top) if top else "раздач не найдено"
        log(f"{label}: подходящих раздач нет (найдено {len(releases)}; частые отказы: {why})")
        return False

    candidates.sort(key=lambda c: c[:7], reverse=True)
    matched, n, same, score, _ru, _res, seeds, r, covers, version = candidates[0]
    body = {"guid": r["guid"], "indexerId": r["indexerId"]}
    if not matched:
        body["seriesId"] = series_id   # Sonarr сам сопоставит серии по разобранным номерам
    what = (f"{label}: «{r['title'][:140]}» [{r.get('indexer')}] — новые серии {sorted(covers)}, "
            f"очки {score}, сиды {seeds}{', та же тема' if same else ''}"
            f"{', сериал указан вручную' if not matched else ''}")
    call("/release", body, "POST", timeout=180)
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


def busy_seasons():
    return {(q.get("seriesId"), q.get("seasonNumber")) for q in queue_records()}


def search_old_seasons(state, recent):
    """Сезоны, где серии вышли давно и так и не скачаны: та же логика, по несколько за раз,
    начиная с тех, что дольше всего не проверялись."""
    seasons = missing_seasons()
    busy = busy_seasons()
    todo = [(k, v) for k, v in seasons.items() if k not in recent and k not in busy]
    todo.sort(key=lambda kv: state["searched"].get(f"sonarr:{kv[0][0]}:{kv[0][1]}", 0))
    log(f"Пропущенное в Sonarr: старых сезонов с недостающими сериями {len(todo)}, "
        f"проверяю до {MISSING_SEASONS_PER_RUN}")
    for i, ((sid, season), info) in enumerate(todo[:MISSING_SEASONS_PER_RUN]):
        time.sleep(PAUSE_BETWEEN_SEARCHES)
        try:
            if (sid, season) in busy_seasons():
                continue
            process_season(sid, season, info, state)
        except Exception as e:
            log(f"{info['title']} S{season:02d}: ошибка — {e}")
        state["searched"][f"sonarr:{sid}:{season}"] = time.time()
        save_state(state)


def search_missing_movies(state):
    """Вышедшие, но не скачанные фильмы: Radarr ищет их только при добавлении и по RSS,
    поэтому раз в сутки запускаем его собственный поиск по нескольким фильмам."""
    movies, page = [], 1
    while True:
        data = rcall(f"/wanted/missing?page={page}&pageSize=250&monitored=true")
        records = data.get("records", [])
        movies += [m for m in records if m.get("isAvailable") and not m.get("hasFile")]
        if page * 250 >= data.get("totalRecords", 0) or not records:
            break
        page += 1
    queued = {q.get("movieId") for q in rcall("/queue?pageSize=1000").get("records", [])}
    movies = [m for m in movies if m["id"] not in queued]
    movies.sort(key=lambda m: state["searched"].get(f"radarr:{m['id']}", 0))
    batch = movies[:MISSING_MOVIES_PER_RUN]
    log(f"Пропущенное в Radarr: вышедших фильмов без файла {len(movies)}, ищу {len(batch)}")
    if not batch:
        return
    rcall("/command", {"name": "MoviesSearch", "movieIds": [m["id"] for m in batch]}, "POST")
    log("Radarr ищет: " + ", ".join(f"{m.get('title')} ({m.get('year')})" for m in batch))
    now = time.time()
    for m in batch:
        state["searched"][f"radarr:{m['id']}"] = now
    save_state(state)


def run_missing(state, recent):
    if time.time() - state.get("last_missing_run", 0) < MISSING_SEARCH_HOURS * 3600:
        return
    try:
        search_old_seasons(state, recent)
    except Exception as e:
        log(f"Пропущенное в Sonarr: ошибка — {e}")
    if RADARR_URL:
        try:
            search_missing_movies(state)
        except Exception as e:
            log(f"Пропущенное в Radarr: ошибка — {e}")
    state["last_missing_run"] = time.time()
    save_state(state)


def run_once(state):
    seasons = missing_seasons(LOOKBACK_DAYS)
    todo = [(k, v) for k, v in seasons.items() if k not in busy_seasons()]
    skipped = len(seasons) - len(todo)
    log(f"Сезонов с недостающими сериями: {len(seasons)} (уже качаются: {skipped}), "
        f"проверяю до {MAX_SEASONS_PER_RUN}")
    for i, ((sid, season), info) in enumerate(todo[:MAX_SEASONS_PER_RUN]):
        if i:
            time.sleep(PAUSE_BETWEEN_SEARCHES)
        try:
            # очередь могла измениться, пока шли поиски по предыдущим сезонам
            if (sid, season) in busy_seasons():
                log(f"{info['title']} S{season:02d}: уже качается, пропускаю")
                continue
            process_season(sid, season, info, state)
        except Exception as e:
            log(f"{info['title']} S{season:02d}: ошибка — {e}")
    try:
        cleanup_queue()
    except Exception as e:
        log(f"Очистка очереди: ошибка — {e}")
    run_missing(state, set(seasons))


def beat():
    """Отметка для HEALTHCHECK: контейнер жив и проходы идут."""
    try:
        with open(HEARTBEAT_FILE, "w") as f:
            f.write(str(time.time()))
    except OSError:
        pass


def main():
    global KEY, RADARR_KEY
    KEY = api_key(SONARR_API_KEY, SONARR_CONFIG)
    if RADARR_URL:
        RADARR_KEY = api_key(RADARR_API_KEY, RADARR_CONFIG)
    log(f"Старт: Sonarr {SONARR_URL}, раз в {INTERVAL_HOURS} ч, серии за {LOOKBACK_DAYS} дн.; "
        f"пропущенное раз в {MISSING_SEARCH_HOURS:g} ч"
        + (f", Radarr {RADARR_URL}" if RADARR_URL else ", Radarr не подключён"))
    state = load_state()
    state.setdefault("grabbed", {})
    state.setdefault("searched", {})
    beat()
    if STARTUP_DELAY and not RUN_ONCE:
        log(f"Жду {STARTUP_DELAY} с, пока Sonarr перечитает очередь…")
        time.sleep(STARTUP_DELAY)
    while True:
        beat()
        try:
            run_once(state)
            pause = INTERVAL_HOURS * 3600
        except Exception as e:
            pause = RETRY_MINUTES * 60
            log(f"Ошибка прохода: {e}. Повтор через {RETRY_MINUTES:g} мин.")
        if RUN_ONCE:
            break
        time.sleep(pause)


if __name__ == "__main__":
    main()
