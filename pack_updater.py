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
Для сериалов, которые трекеры нумеруют иначе, чем Sonarr (обычно аниме: «Steel Ball Run (S7)» —
это 6-й сезон JoJo в Sonarr), есть таблица соответствий REMAP_FILE: по ней раздача берётся с явно
указанными сериями, а файлы импортируются вручную, если Sonarr не поймёт их сам.
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
import urllib.parse
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
# Если Sonarr/Radarr запущены меньше STARTUP_DELAY секунд назад, ждём остаток: им нужно время,
# чтобы перечитать очередь из торрент-клиента (иначе она выглядит пустой). Если они работают
# дольше — проход начинается сразу, так что сам сервис можно перезапускать в любой момент.
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
# Таблица соответствий для сериалов, которые трекеры нумеруют не так, как Sonarr (обычно аниме)
REMAP_FILE = os.environ.get("REMAP_FILE", "/data/remap.json")
# Необязательно: адрес push-монитора (например, Uptime Kuma), который дёргается после каждого прохода
PUSH_URL = os.environ.get("PUSH_URL", "")

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
    state["loops"] = {k: v for k, v in state.get("loops", {}).items() if v > cutoff}
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
        names = {norm(bare)} | {norm(re.sub(r"\s*\(\d{4}\)\s*$", "", a.get("title", "")))
                                for a in series.get("alternateTitles") or []}
        _profiles[series_id] = {"title": norm(bare), "year": str(series.get("year") or ""),
                                "allowed": allowed, "names": {n for n in names if len(n) >= 4},
                                "full_title": series.get("title", ""),
                                "anime": series.get("seriesType") == "anime"}
    return _profiles[series_id]


# --- Таблица соответствий (remap.json) ---------------------------------------------------
# {"JoJo's Bizarre Adventure (2012)": [
#     {"names": ["Steel Ball Run", "Стальной шар"], "tracker_season": 7, "season": 6}]}
# Работает только для перечисленных сериалов и сезонов и только когда Sonarr сам ничего не нашёл.

_remap = {"mtime": None, "data": {}}


def remap_rules(series_id, season):
    """Правила таблицы для этого сезона сериала (пустой список — сериала в таблице нет)."""
    try:
        mtime = os.path.getmtime(REMAP_FILE)
    except OSError:
        return []
    if mtime != _remap["mtime"]:
        try:
            with open(REMAP_FILE, encoding="utf-8") as f:
                raw = json.load(f)
            _remap["data"] = {norm(k): v for k, v in raw.items() if isinstance(v, list)}
            log(f"Таблица соответствий: {REMAP_FILE}, сериалов {len(_remap['data'])}")
        except Exception as e:
            log(f"Таблица соответствий {REMAP_FILE} не читается, пропускаю: {e}")
            _remap["data"] = {}
        _remap["mtime"] = mtime
    rules = _remap["data"].get(norm(series_profile(series_id)["full_title"]), [])
    return [r for r in rules if r.get("season") == season and r.get("names")]


NOT_VIDEO = re.compile(r"\b(manga|манга|главы|глава|chapters?|cbr|cbz|ранобэ|light novel|артбук|artbook|ost)\b", re.I)
# Отказы Sonarr, которые означают только «не понял, что это за сериал/сезон/серии»
REMAP_REJECTION = re.compile(r"^(Unknown Series|Wrong season|Episode wasn't requested|.*not requested"
                             r"|Unable to (parse|identify)|Existing file)", re.I)
TITLE_SEASON = re.compile(r"(?:\bS|\bSeason\s*|\bсезон\s*|\bTV-?)(\d{1,2})(?=E\d|\b|\))", re.I)


def title_episodes(title):
    """Номера серий из названия раздачи: S7E1-8, [01-08 из XX], (1—7), [08 из 12]."""
    nums = set()
    for a, b in re.findall(r"\bS\d{1,2}E(\d{1,3})(?:\s*-\s*E?(\d{1,3}))?", title, re.I):
        nums |= set(range(int(a), int(b or a) + 1))
    # «из XX» Prowlarr иногда отдаёт без «из» (кириллица вырезана): [01-07  XX]
    of = r"(?:\s*(?:из|of)\s*|\s+)[\dXxх?]+\s*"
    for a, b in re.findall(r"[\[(]\s*(\d{1,3})\s*[-–—]\s*(\d{1,3})\s*(?:" + of + r")?[\])]", title):
        nums |= set(range(int(a), int(b) + 1))
    if not nums:
        for a in re.findall(r"\[\s*(\d{1,3})" + of + r"\]", title):
            nums.add(int(a))
    return nums


def remap_ok(r, rules, series_id, season_nums):
    """Серии раздачи по таблице соответствий или None, если раздача не подходит под правило."""
    title = r.get("title", "")
    rej = rejection_texts(r)
    if not rej or not all(REMAP_REJECTION.search(x) for x in rej):
        return None, None   # отказ по качеству, размеру, чёрному списку и т.п. — не трогаем
    if NOT_VIDEO.search(title):
        return None, None
    prof = series_profile(series_id)
    if (r.get("quality") or {}).get("quality", {}).get("name") not in prof["allowed"]:
        return None, None
    if (r.get("seeders") or 0) < MIN_SEEDERS:
        return None, None
    ntitle = norm(title)
    for rule in rules:
        if not any(norm(n) and norm(n) in ntitle for n in rule["names"]):
            continue
        seasons = {int(s) for s in TITLE_SEASON.findall(title)}
        if seasons - {rule.get("tracker_season"), rule["season"]}:
            continue   # в названии другой сезон
        eps = title_episodes(title)
        if eps and eps <= set(season_nums):
            return eps, rule
    return None, None


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

    # аниме Sonarr ищет по каждой серии отдельно, под всеми названиями — это долго
    timeout = 1200 if series_profile(series_id).get("anime") else 600
    releases = call(f"/release?seriesId={series_id}&seasonNumber={season}", timeout=timeout)
    topics = grabbed_topics(series_id, season)
    rules = remap_rules(series_id, season)

    candidates, reasons, foreign = [], {}, 0
    for r in releases:
        rej = rejection_texts(r)
        remap_eps = rule = None
        if r.get("approved") or (rej and all(ALLOWED_REJECTION.search(x) for x in rej)):
            matched = 1   # Sonarr распознал и проверил раздачу сам
        elif rej and all(UNKNOWN_SERIES.search(x) for x in rej) and unknown_series_ok(r, season, series_id):
            matched = 0   # сериал указываем вручную
        elif rules and (remap_eps := remap_ok(r, rules, series_id, season_nums))[0]:
            remap_eps, rule = remap_eps
            matched = -1  # по таблице соответствий: только если обычных вариантов нет
        else:
            for x in rej:
                key = re.split(r"[:(]", x, maxsplit=1)[0].strip()
                reasons[key] = reasons.get(key, 0) + 1
            if rej and all(REMAP_REJECTION.search(x) and not ALLOWED_REJECTION.search(x) for x in rej) \
                    and any(n in norm(r.get("title", "")) for n in series_profile(series_id)["names"]):
                foreign += 1   # название сериала есть, а Sonarr не понял сезон/сериал
            continue
        # Sonarr ставит downloadAllowed=false всем раздачам, которые не сопоставил с сериалом;
        # при скачивании с явно указанным seriesId он этот флаг не проверяет
        if matched == 1 and r.get("downloadAllowed") is False:
            continue
        covers = (remap_eps if matched == -1 else release_episodes(r, season_nums)) & missing
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
            r, covers, version, remap_eps))

    if not candidates:
        top = sorted(reasons.items(), key=lambda kv: -kv[1])[:3]
        why = "; ".join(f"{k} ×{v}" for k, v in top) if top else "раздач не найдено"
        log(f"{label}: подходящих раздач нет (найдено {len(releases)}; частые отказы: {why})")
        if foreign and not rules:
            log(f"{label}: похоже, трекеры нумеруют этот сезон иначе, чем Sonarr — раздач с названием "
                f"сериала, отклонённых как другой сезон или неизвестный сериал: {foreign}. Если так, добавьте "
                f"правило в {REMAP_FILE} (см. README)")
        return False

    candidates.sort(key=lambda c: c[:7], reverse=True)
    matched, n, same, score, _ru, _res, seeds, r, covers, version, remap_eps = candidates[0]
    body = {"guid": r["guid"], "indexerId": r["indexerId"]}
    if matched == 0:
        body["seriesId"] = series_id   # Sonarr сам сопоставит серии по разобранным номерам
    elif matched == -1:
        # как «выбрать сериал и серии вручную» в интерактивном поиске
        ids = [e["id"] for e in episodes if e["episodeNumber"] in remap_eps]
        body.update(seriesId=series_id, episodeIds=ids, shouldOverride=True,
                    quality=r.get("quality"), languages=r.get("languages") or [])
    what = (f"{label}: «{r['title'][:140]}» [{r.get('indexer')}] — новые серии {sorted(covers)}, "
            f"очки {score}, сиды {seeds}{', та же тема' if same else ''}"
            f"{', сериал указан вручную' if matched == 0 else ''}"
            f"{', по таблице соответствий' if matched == -1 else ''}")
    call("/release", body, "POST", timeout=180)
    state["grabbed"][version] = {"ts": time.time(), "title": r["title"], "series": label}
    save_state(state)
    log("Отправлено на скачивание: " + what)
    return True


PARTIAL_PACK_MSG = re.compile(r"expected in this release were not imported or missing from the release", re.I)


def cleanup_queue():
    """Убрать из очереди законченные загрузки, у которых всё, что можно, уже импортировано:
    остальные файлы — «не улучшение», а серии, которых в раздаче нет (раздача на часть
    сезона, а Sonarr ждёт весь сезон по имени торрента), просто отсутствуют."""
    by_download = {}
    for q in queue_records():
        if q.get("status") != "completed":
            continue
        if q.get("trackedDownloadState") not in ("importBlocked", "importPending"):
            continue
        by_download.setdefault(q.get("downloadId") or q["id"], []).append(q)
    for dl, items in by_download.items():
        # statusMessages одинаковые у всех записей загрузки: берём у первой
        status = items[0].get("statusMessages") or []
        if not status:
            continue   # Sonarr ещё не пытался импортировать
        partial, ok = False, True
        for sm in status:
            title, msgs = sm.get("title") or "", sm.get("messages") or []
            if PARTIAL_PACK_MSG.search(title) and not msgs:
                partial = True                      # общая строка «не все серии из раздачи»
            elif msgs and all(NOT_UPGRADE_MSG.search(m) for m in msgs):
                continue                            # файл не импортирован как «не улучшение»
            else:
                ok = False                          # любая другая проблема — оставляем человеку
                break
        if not ok:
            continue
        has_files = [bool((q.get("episode") or {}).get("hasFile")) for q in items]
        # без строки о неполной раздаче все серии загрузки должны быть на диске;
        # с ней достаточно, чтобы хотя бы одна серия из неё была на диске
        if not (any(has_files) if partial else all(has_files)):
            continue
        name = items[0].get("title", dl)
        call("/queue/bulk?removeFromClient=false&blocklist=false&skipRedownload=true",
             {"ids": [q["id"] for q in items]}, "DELETE")
        log(f"Убрано из очереди (раздача продолжает раздаваться): {name}")


SAMPLE_MSG = re.compile(r"sample", re.I)
NOT_EPISODE_NUMBERS = {480, 540, 576, 720, 264, 265}   # разрешения и кодеки, а не номера серий


def file_episode(name, allowed_seasons):
    """Номер серии из имени файла: S07E05, «Steel Ball Run - 05 [1080p]»; None — не понять."""
    base = os.path.splitext(os.path.basename(name))[0]
    m = re.findall(r"\bS(\d{1,2})E(\d{1,3})\b", base, re.I)
    if m:
        if len(m) != 1 or int(m[0][0]) not in allowed_seasons:
            return None
        return int(m[0][1])
    base = re.sub(r"\[[^\]]*\]|\([^)]*\)|\{[^}]*\}", " ", base)
    nums = [int(n) for n in re.findall(r"(?<![0-9A-Za-z])(\d{1,3})(?:v\d)?(?![0-9A-Za-z])", base)]
    nums = [n for n in nums if n not in NOT_EPISODE_NUMBERS]
    return nums[-1] if nums else None


def import_remapped():
    """Загрузки, взятые по таблице соответствий, Sonarr часто не может импортировать сам:
    в файлах «S07E05», а такого сезона у него нет. Импортируем вручную по той же таблице,
    только если номер каждого файла читается однозначно и все номера есть в сезоне."""
    groups = {}
    for q in queue_records():
        if q.get("status") != "completed" or not q.get("statusMessages"):
            continue
        if q.get("trackedDownloadState") not in ("importBlocked", "importPending"):
            continue
        sid, season = q.get("seriesId"), q.get("seasonNumber")
        if sid is None or season is None or not remap_rules(sid, season):
            continue
        groups.setdefault(q.get("downloadId"), q)
    for dl, q in groups.items():
        sid, season = q["seriesId"], q["seasonNumber"]
        rule = remap_rules(sid, season)[0]
        label = f"{series_profile(sid)['full_title']} S{season:02d}"
        allowed = {rule["season"], rule.get("tracker_season"), 1} - {None}
        episodes = {e["episodeNumber"]: e for e in call(f"/episode?seriesId={sid}&seasonNumber={season}")}
        # только downloadId: с seriesId Sonarr показывает файлы всей папки сериала в библиотеке
        items = call(f"/manualimport?downloadId={dl}&filterExistingFiles=true", timeout=300) or []
        library = (call(f"/series/{sid}").get("path") or "").rstrip("/") + "/"
        items = [it for it in items if library == "/" or not str(it.get("path", "")).startswith(library)]
        files, problem = [], None
        for it in items:
            if any(SAMPLE_MSG.search(x) for x in rejection_texts(it)):
                continue
            num = file_episode(it.get("relativePath") or it.get("path", ""), allowed)
            if num is None or num not in episodes:
                problem = f"не понял номер серии у «{os.path.basename(it.get('path', ''))}»"
                break
            files.append((num, it))
        nums = [n for n, _ in files]
        if not problem and len(set(nums)) != len(nums):
            problem = f"повторяются номера серий {sorted(nums)}"
        if problem:
            log(f"{label}: «{q.get('title')}» не импортирую по таблице — {problem}; разберите вручную")
            continue
        todo = [(n, it) for n, it in files if not episodes[n].get("hasFile")]
        if not todo:
            if files and all(episodes[n].get("hasFile") for n in nums):
                call("/queue/bulk?removeFromClient=false&blocklist=false&skipRedownload=true",
                     {"ids": [x["id"] for x in queue_records() if x.get("downloadId") == dl]}, "DELETE")
                log(f"Убрано из очереди (все серии уже есть, раздача продолжает раздаваться): {q.get('title')}")
            continue
        call("/command", {"name": "ManualImport", "importMode": "auto", "files": [{
            "path": it["path"], "folderName": it.get("folderName"), "seriesId": sid,
            "episodeIds": [episodes[n]["id"]], "quality": it.get("quality"),
            "languages": it.get("languages") or [], "releaseGroup": it.get("releaseGroup"),
            "indexerFlags": it.get("indexerFlags", 0), "releaseType": it.get("releaseType", "singleEpisode"),
            "downloadId": dl} for n, it in todo]}, "POST")
        log(f"{label}: импортирую по таблице соответствий серии {sorted(n for n, _ in todo)} из «{q.get('title')}»")


FILE_DELETED = ("movieFileDeleted", "episodeFileDeleted")
LOOP_WINDOW_HOURS = 48


def stop_regrab_loops(name, request, state):
    """Одна и та же раздача (тот же хэш торрента) скачивается по кругу. Так бывает, когда
    раздача по названию набирает больше очков, чем уже скачанный из неё файл по имени файла
    (в названии «RUS, HDR10+», в имени файла — нет), и Radarr/Sonarr каждый раз считает её улучшением.
    Повторное скачивание той же раздачи после её импорта — признак такой петли: убираем её из
    очереди и вносим в чёрный список. Файлы и раздача в торрент-клиенте остаются."""
    data = request("/history?page=1&pageSize=1000&sortKey=date&sortDirection=descending")
    records = data.get("records", []) if isinstance(data, dict) else (data or [])
    item_key = "movieId" if name == "Radarr" else "episodeId"
    by_item = {}
    for h in records:
        if h.get(item_key) is not None:
            by_item.setdefault(h[item_key], []).append(h)
    since = datetime.now(timezone.utc) - timedelta(hours=LOOP_WINDOW_HOURS)
    loops = {}
    for events in by_item.values():
        imported = set()
        for h in sorted(events, key=lambda x: x.get("date") or ""):
            kind, dl = str(h.get("eventType")), (h.get("downloadId") or "").upper()
            if kind in FILE_DELETED and (h.get("data") or {}).get("reason") != "Upgrade":
                imported.clear()   # файл удалили или он пропал — скачать заново законно
            elif kind == "downloadFolderImported" and dl:
                imported.add(dl)
            elif kind == "grabbed" and dl in imported and (parse_dt(h.get("date")) or since) > since:
                loops[dl] = h
    if not loops:
        return
    handled = state.setdefault("loops", {})
    queue = request("/queue?pageSize=1000").get("records", [])
    for dl, h in loops.items():
        mark = f"{name}:{dl}:{h.get('id')}"
        if mark in handled:
            continue
        ids = [q["id"] for q in queue if (q.get("downloadId") or "").upper() == dl]
        if ids:
            request("/queue/bulk?removeFromClient=false&blocklist=true&skipRedownload=true", {"ids": ids}, "DELETE")
        else:
            request(f"/history/failed/{h['id']}", {}, "POST")
        log(f"{name}: «{h.get('sourceTitle')}» уже скачана и импортирована, но взята снова — скачивание по кругу. "
            + ("Убрал из очереди и внёс в чёрный список" if ids else "Внёс в чёрный список")
            + "; файлы и раздача остаются")
        handled[mark] = time.time()
    save_state(state)


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
    due = state.get("last_missing_run", 0) + MISSING_SEARCH_HOURS * 3600
    if time.time() < due:
        log("Поиск пропущенного: следующий " + datetime.fromtimestamp(due).strftime("%d.%m %H:%M"))
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
    for name, request in [("Sonarr", call)] + ([("Radarr", rcall)] if RADARR_URL else []):
        try:
            stop_regrab_loops(name, request, state)
        except Exception as e:
            log(f"{name}: проверка скачивания по кругу — ошибка: {e}")
    # сначала чистим очередь: зависшая неполная раздача иначе держит сезон «занятым»
    try:
        import_remapped()
    except Exception as e:
        log(f"Импорт по таблице соответствий: ошибка — {e}")
    try:
        cleanup_queue()
    except Exception as e:
        log(f"Очистка очереди: ошибка — {e}")
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
    run_missing(state, set(seasons))


def app_uptime(request):
    """Сколько секунд приложение уже работает (по startTime из /system/status); None — неизвестно."""
    try:
        started = parse_dt(request("/system/status").get("startTime"))
    except Exception:
        return None   # недоступно — тогда тоже ждём
    return (datetime.now(timezone.utc) - started).total_seconds() if started else None


def wait_for_apps():
    apps = [("Sonarr", call)] + ([("Radarr", rcall)] if RADARR_URL else [])
    while True:
        waits = []
        for name, request in apps:
            up = app_uptime(request)
            if up is None:
                waits.append((name, 60, "недоступен"))
            elif up < STARTUP_DELAY:
                waits.append((name, STARTUP_DELAY - up, f"запущен {int(up)} с назад"))
        if not waits:
            return
        pause = max(30, min(w for _, w, _ in waits))
        log("Жду " + ", ".join(f"{n} ({why})" for n, _, why in waits)
            + f": следующая проверка через {int(pause)} с")
        beat()
        time.sleep(pause)


def push(msg):
    """Отметка во внешний мониторинг: «проход прошёл». Ошибка здесь проход не ломает."""
    if not PUSH_URL:
        return
    sep = "&" if "?" in PUSH_URL else "?"
    url = PUSH_URL if "msg=" in PUSH_URL else PUSH_URL + sep + urllib.parse.urlencode({"status": "up", "msg": msg})
    try:
        urllib.request.urlopen(url, timeout=30).read()
    except Exception as e:
        log(f"Отметка в мониторинг не ушла: {e}")


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
        wait_for_apps()
    while True:
        beat()
        try:
            started = time.time()
            run_once(state)
            pause = INTERVAL_HOURS * 3600
            push(f"проход за {int(time.time() - started)} с")
        except Exception as e:
            pause = RETRY_MINUTES * 60
            log(f"Ошибка прохода: {e}. Повтор через {RETRY_MINUTES:g} мин.")
        if RUN_ONCE:
            break
        time.sleep(pause)


if __name__ == "__main__":
    main()
