"""
Global News Bot - 대만·일본 노동/HR 뉴스 클리핑

실행 방법
  python bot.py urgent    # 긴급(매시간): ① 공식 발표(도쿄 기상청 경보 / 대만 태풍경보·타이베이 휴무) + 기업 파업·소송
                          #   휴일엔 공식 발표만 바로, 파업·소송 뉴스는 다음 평일 08시 이후에
  python bot.py watch     # 저녁 집중 확인(20~23시, 15분마다): 대만 태풍경보 중일 때 타이베이 휴무 발표
  python bot.py daily     # 일간: 무신사 언급 (평일 아침 1번, 없으면 안 보냄)
  python bot.py weekly    # ②③④⑥ + 정부 기관 발표 (매주)
  python bot.py monthly   # ⑤ 보상 시장 지표 (매월)
  --dry-run : 슬랙으로 보내지 않고 화면에만 출력
  --force   : 휴일·이미 보냄 여부와 상관없이 바로 실행 (수동 테스트용)

키워드·기업·소스는 config.py 에서 고치세요.
"""
import hashlib
import json
import os
import re
import sys
import time
import unicodedata
import urllib.parse
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher

import feedparser
import requests

import config

KST = timezone(timedelta(hours=9))
NOW = datetime.now(KST)
STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "state.json")
GOV_URGENT = "🏛 정부 공식 발표"   # 휴일에도 바로 보내는 카테고리
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; GlobalNewsBot/1.0)"}
INDUSTRY_PER_QUERY = 3      # 기업별 검색은 기사 수를 줄여서 가져와요
MAX_PER_SECTION = 5         # 한 섹션에 최대 몇 건까지 보여줄지 (나머지는 "외 N건")
SECTION_LIMIT = 2900        # 슬랙 구역(블록) 1개당 최대 글자 수
TITLE_MAX = 70              # 제목이 이보다 길면 …로 줄여요
TOP_SECTIONS = [config.CATEGORIES["musinsa"]["label"]]   # 최신순 대신 "많이 보도된 순" Top 5

SLACK_TOKEN = os.getenv("SLACK_TOKEN")
SLACK_CHANNEL = os.getenv("SLACK_CHANNEL_HRBP")


# =====================================================================
# 공통 도구
# =====================================================================
def log(msg):
    print(msg, flush=True)


def norm(text):
    """비교용으로 제목을 정리: 전각→반각, 공백·기호 제거, 소문자"""
    text = unicodedata.normalize("NFKC", text or "").lower()
    return re.sub(r"[\s\W_]+", "", text)


def similar(a, b):
    """두 제목이 같은 사건인지 대략 판단"""
    if not a or not b:
        return False
    if a in b or b in a:
        return True
    return SequenceMatcher(None, a, b).ratio() >= 0.75


def to_dt(struct_time):
    if not struct_time:
        return None
    return datetime(*struct_time[:6], tzinfo=timezone.utc).astimezone(KST)


FETCH_STATS = {"total": 0, "fail": 0, "streak": 0}   # 뉴스 검색 실패 통계 (streak = 연속 실패 수)


def fetch(url):
    """URL 내용을 가져와요. 사이트가 잠시 거절하면(429·5xx) 쉬었다가 다시 시도, 끝내 실패하면 None"""
    last = None
    waits = (0,) if FETCH_STATS["streak"] >= 4 else (0, 3, 10)   # 계속 막히면 재시도 생략
    for wait in waits:
        if wait:
            time.sleep(wait)
        try:
            res = requests.get(url, headers=HEADERS, timeout=20)
            if res.status_code == 429 or res.status_code >= 500:
                last = f"{res.status_code}"
                continue
            res.raise_for_status()
            return res.content
        except Exception as e:
            last = e
    log(f"  ⚠️ 가져오기 실패: {url[:90]} ({last})")
    return None


def collection_failed():
    """뉴스 검색의 절반 넘게 실패했으면 True → 이번엔 보내지 않고 다음 실행 때 다시 시도"""
    t, f = FETCH_STATS["total"], FETCH_STATS["fail"]
    if t and f / t > 0.5:
        log(f"\n⚠️ 뉴스 검색 {t}건 중 {f}건 실패 → 이번엔 보내지 않고 다음 실행 때 다시 시도해요.")
        return True
    return False


def parse_feed(url):
    content = fetch(url)
    return feedparser.parse(content).entries if content else []


def slack_escape(text):
    return (text or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def short(text, n):
    text = (text or "").strip()
    return text if len(text) <= n else text[:n] + "…"


# =====================================================================
# 번역 (무료 Google 번역, 실패하면 원문 그대로)
# =====================================================================
#   제목을 하나씩 보내면 요청이 너무 많아 차단돼요(TooManyRequests).
#   그래서 여러 제목을 줄바꿈으로 묶어 한 번에 번역해요.
_cache = {}
_fails = 0
TRANSLATE_URL = "https://translate.googleapis.com/translate_a/single"


def _gtx(text):
    """Google 번역 1회 요청. 차단되면 잠깐 쉬었다가 최대 3번 시도"""
    last = "TooManyRequests"
    for wait in (0, 5, 15):
        if wait:
            time.sleep(wait)
        try:
            res = requests.post(TRANSLATE_URL, timeout=20, headers=HEADERS,
                                params={"client": "gtx", "sl": "auto", "tl": "ko", "dt": "t"},
                                data={"q": text})
            if res.status_code == 429:
                continue
            res.raise_for_status()
            return "".join(seg[0] for seg in res.json()[0] if seg and seg[0])
        except Exception as e:
            last = e
    raise RuntimeError(f"번역 요청 실패 ({last})")


def translate_many(texts):
    """여러 제목을 묶어서 번역해 캐시에 저장"""
    global _fails
    todo = [t for t in dict.fromkeys(texts) if t and t not in _cache]
    chunks, cur = [], []
    for t in todo:
        if cur and sum(len(x) + 1 for x in cur) + len(t) > 3000:
            chunks.append(cur)
            cur = []
        cur.append(t)
    if cur:
        chunks.append(cur)
    for chunk in chunks:
        if _fails >= 3:   # 계속 실패하면 이번 실행에선 원문으로 보내요
            return
        try:
            out = _gtx("\n".join(chunk)).split("\n")
            if len(out) != len(chunk):          # 줄 수가 어긋나면 하나씩 번역
                out = [_gtx(t) for t in chunk]
            for src, ko in zip(chunk, out):
                _cache[src] = ko.strip() or src
            _fails = 0
            time.sleep(1)
        except Exception as e:
            _fails += 1
            log(f"  ⚠️ 번역 실패, 원문 사용 ({len(chunk)}건): {e}")


def roc_to_ad(text):
    """대만 민국 연도(예: 115年, 民國115年)를 서기 연도(2026年)로 바꿔요. 번역기가 '2015년'으로 잘못 옮기는 문제 방지"""
    return re.sub(r"(?:民國|民国)?(?<!\d)(1[01]\d)(?=\s*年)", lambda m: str(int(m.group(1)) + 1911), text or "")


def source_text(item):
    """번역에 넣을 제목 (대만 기사는 연도를 서기로 바꿔서)"""
    t = item["title"].replace("\n", " ")
    return roc_to_ad(t) if item.get("country") == "TW" else t


def translate(text):
    if not text:
        return text
    key = text.replace("\n", " ")
    if key not in _cache:
        translate_many([key])
    return _cache.get(key, text)


# =====================================================================
# 수집: Google 뉴스
# =====================================================================
def google_news(country, query, days, limit, category):
    c = config.COUNTRIES[country]
    q = urllib.parse.quote(f"{query} when:{days}d")
    url = f"https://news.google.com/rss/search?q={q}&hl={c['hl']}&gl={c['gl']}&ceid={c['ceid']}"
    cutoff = NOW - timedelta(days=days)
    items = []
    content = fetch(url)
    FETCH_STATS["total"] += 1
    if content is None:
        FETCH_STATS["fail"] += 1
        FETCH_STATS["streak"] += 1
    else:
        FETCH_STATS["streak"] = 0
    entries = feedparser.parse(content).entries if content else []
    for e in entries[:limit]:
        source = (e.get("source") or {}).get("title", "")
        title = e.get("title", "")
        if source and title.endswith(f" - {source}"):
            title = title[: -len(f" - {source}")]
        published = to_dt(e.get("published_parsed"))
        if published and published < cutoff:
            continue
        items.append({
            "country": country, "category": category, "query": query,
            "title": title, "link": e.get("link", ""), "source": source,
            "published": published, "also": [],
        })
    time.sleep(1)  # 너무 빠르게 요청하지 않도록
    return items


def collect_keywords(country, category_key, days):
    cat = config.CATEGORIES[category_key]
    items = []
    for kw in cat.get(country, []):
        log(f"  🔍 [{country}] {kw}")
        found = google_news(country, kw, days, cat.get("limit", config.PER_QUERY), cat["label"])
        if cat.get("title_only"):   # 키워드(띄어쓰기로 나뉜 단어 모두)가 제목에 있어야 통과
            words = [norm(w) for w in kw.split()]
            found = [i for i in found if all(w in norm(i["title"]) for w in words)]
        items += found
    return items


def collect_industry(country, issues, days):
    items = []
    companies = config.COMPANIES.get(country, []) + config.COMMON_COMPANIES
    issue_query = "(" + " OR ".join(issues) + ")"
    words = config.INDUSTRY_TITLE_WORDS.get(country, [])
    for company in companies:
        log(f"  🔍 [{country}] {company} {issue_query}")
        found = google_news(country, f'"{company}" {issue_query}', days,
                            INDUSTRY_PER_QUERY, config.INDUSTRY_LABEL)
        # 제목에 노사 관련 단어가 없는 기사(신제품, 세일 소식 등)는 버려요
        items += [i for i in found if not words or any(w in i["title"] for w in words)]
    return items


# =====================================================================
# 수집: 정부 기관
# =====================================================================
def collect_gov_feeds(country, days):
    cutoff = NOW - timedelta(days=days)
    items = []
    for feed in config.GOV_FEEDS:
        if feed["country"] != country:
            continue
        log(f"  🏛 {feed['name']}")
        for e in parse_feed(feed["url"]):
            published = to_dt(e.get("published_parsed") or e.get("updated_parsed"))
            if published and published < cutoff:
                continue
            title = e.get("title", "")
            if feed["filter"] and not any(w in title for w in feed["filter"]):
                continue
            if any(w in title for w in feed.get("exclude", [])):
                continue
            items.append({
                "country": country, "category": "🏛 정부 기관 발표", "query": "",
                "title": title, "link": e.get("link", ""), "source": feed["name"].split(" (")[0],
                "published": published, "also": [],
            })
    return items


def clean_html(x):
    return re.sub(r"\s+", " ", re.sub(r"(?s)<[^>]+>", " ", x)).strip()


def official_item(country, text_ko, link, source, key):
    """공식 발표 알림 1건 (이미 한국어로 만든 문장이라 번역하지 않아요)"""
    return {
        "country": country, "category": GOV_URGENT, "query": "",
        "title": text_ko, "ko": text_ko, "link": link,
        "source": source, "published": NOW, "also": [], "key": key,
    }


# ---------------------------------------------------------------------
# 대만 (1) 예비 알림: 중앙기상서 태풍경보
# ---------------------------------------------------------------------
def fetch_cwa_typhoon():
    """중앙기상서 특보 데이터에서 태풍경보를 찾아요.
    반환: None(가져오기 실패) / {} (발령 없음) / {"title","issued","content"}"""
    content = fetch(config.CWA_WARN_DATA)
    if not content:
        return None
    js = content.decode("utf-8", errors="ignore")
    pat = (r"'title'\s*:\s*'([^']*)'\s*,\s*'issued'\s*:\s*'([^']*)'.*?"
           r"'content'\s*:\s*'((?:[^'\\]|\\.)*)'")
    for title, issued, body in re.findall(pat, js, re.S):
        if "颱風警報" in title:
            return {"title": title, "issued": issued, "content": body.replace("\\n", "\n")}
    return {}


def check_cwa(state):
    """해상/육상 태풍경보가 타이베이를 경계 구역에 포함하면 예비 알림 (단계가 바뀔 때만)"""
    log("  🏛 [TW] 中央氣象署 颱風警報")
    ty = fetch_cwa_typhoon()
    if ty is None:
        return []   # 실패하면 상태를 바꾸지 않고 다음 실행 때 다시 확인
    prev = state.get("cwa_phase", "")
    phase = ""
    if ty and "解除" not in ty["title"]:
        text = ty["title"] + ty["content"]
        land = "陸上" in ty["title"]
        taipei_land = config.TW_CITY in text or "台北市" in text
        taipei_sea = any(a in text for a in config.TW_SEA_AREAS)
        if land and taipei_land:
            phase = "land"
        elif taipei_sea or taipei_land:
            phase = "sea"
    state["cwa_phase"] = phase
    log(f"    → 태풍경보 단계: {phase or '없음'} (이전: {prev or '없음'})")
    if phase == prev:
        return []
    link = config.CWA_WARN_PAGE
    key = f"cwa:{phase or 'end'}:{NOW.isoformat()}"
    if not phase:
        if not prev:
            return []
        return [official_item("TW", "ℹ️ [해제] 중앙기상서 태풍경보가 해제됐거나 타이베이가 경계 구역에서 빠졌어요.",
                              link, "中央氣象署", key)]
    name = "육상 태풍경보" if phase == "land" else "해상 태풍경보"
    issued = ty["issued"][5:] if len(ty["issued"]) > 10 else ty["issued"]
    text = (f"⚠️ [예비] 중앙기상서 {name} 발령 – 타이베이가 경계 구역에 포함됐어요 ({issued} 발표). "
            f"휴무 가능성 있음, 보통 전날 저녁(대만 20~22시 = 한국 21~23시)에 확정 예정이에요.")
    return [official_item("TW", text, link, "中央氣象署", key)]


# ---------------------------------------------------------------------
# 대만 (2) 확정 알림: 인사행정총처 정반정과 현황 – 臺北市 행만
# ---------------------------------------------------------------------
def taipei_target(status):
    """원문 상태에서 대상(출근/등교)을 뽑아요"""
    t = []
    if "停止上班" in status:
        t.append("출근 중단")
    if "停止上課" in status:
        t.append("등교 중단")
    return "·".join(t)


def check_dgpa(state):
    """臺北市 행에 停止上班/停止上課가 게시되면 확정 알림, 내용이 바뀌면 정정 알림"""
    log("  🏛 [TW] 人事行政總處 停班停課 (臺北市)")
    content = fetch(config.DGPA_URL)
    if not content:
        return []
    html = content.decode("utf-8", errors="ignore")
    rows = re.findall(r'(?is)headers="city_Name[^"]*"[^>]*>(.*?)</td>\s*<td[^>]*>(.*?)</td>', html)
    statuses = [clean_html(s) for c, s in rows if clean_html(c).replace("台", "臺") == config.TW_CITY]
    status = " / ".join(dict.fromkeys(s for s in statuses if s))
    stop = "停止上班" in status or "停止上課" in status
    # '明天(내일)' 발표가 다음 날 '今天(오늘)'로 바뀌는 건 같은 발표라서, 날짜로 바꿔 비교해요
    d = NOW.date()
    canon = status.replace("今天", d.strftime("%m/%d")).replace("明天", (d + timedelta(days=1)).strftime("%m/%d"))
    prev = state.get("dgpa_taipei", "")
    log(f"    → 臺北市: {status or '게시 없음'} (이전: {prev or '없음'})")

    if not stop:
        state["dgpa_taipei"] = ""
        if prev and status:   # 휴무 발표가 '정상 근무'로 바뀐 경우
            text = f"🔁 [정정] 타이베이시 발표가 바뀌었어요: 휴무 → 정상 근무·등교 / 원문: {status}"
            return [official_item("TW", text, config.DGPA_URL, "人事行政總處", f"dgpa:{NOW.isoformat()}")]
        return []
    state["dgpa_taipei"] = canon
    if canon == prev:
        return []
    when = translate(status)
    if prev:
        text = f"🔁 [정정] 타이베이시 휴무 발표가 바뀌었어요 – {taipei_target(status)} / 적용: {when} (원문: {status})"
    else:
        text = f"✅ [확정] 타이베이시 휴무 발표 – {taipei_target(status)} / 적용: {when} (원문: {status})"
    return [official_item("TW", text, config.DGPA_URL, "人事行政總處", f"dgpa:{NOW.isoformat()}")]


# ---------------------------------------------------------------------
# 일본: 기상청 도쿄 경보 (주의보 제외)
# ---------------------------------------------------------------------
def fetch_json(url):
    content = fetch(url)
    if content is None:
        return None
    try:
        return json.loads(content.decode("utf-8"))
    except Exception as e:
        log(f"  ⚠️ JSON 해석 실패: {url} ({e})")
        return None


def check_jma(state):
    """東京地方에 태풍 관련 경보·특별경보가 새로 나오면 알림 (같은 경보가 이어지면 다시 안 보냄)"""
    log("  🏛 [JP] 気象庁 東京地方 警報")
    data = fetch_json(config.JMA_WARN_DATA)
    if data is None:
        return []
    reports = data if isinstance(data, list) else [data]
    reports.sort(key=lambda r: r.get("reportDatetime", ""))
    latest, heads, last_time = {}, [], ""
    for r in reports:
        heads.append(r.get("headlineText") or "")
        for area in (r.get("warning") or {}).get("class10Items", []):
            if area.get("areaCode") != config.JMA_AREA_CODE:
                continue
            last_time = r.get("reportDatetime", last_time)
            for k in area.get("kinds", []):
                if k.get("code"):
                    latest[k["code"]] = k.get("status", "")
    active = {c for c, s in latest.items() if s != "解除" and c in config.JMA_WARN_CODES}
    tc = fetch_json(config.JMA_TC_DATA) or []
    typhoon = bool(tc) or any("台風" in h for h in heads)
    head_typhoon = any("台風" in h for h in heads)
    relevant = {c for c in active
                if c in config.JMA_SPECIAL_CODES
                or head_typhoon
                or (typhoon and c in config.JMA_TYPHOON_TYPICAL)}
    prev = set(state.get("jma_tokyo", []))
    state["jma_tokyo"] = sorted(relevant)
    log(f"    → 발령 중 경보 {sorted(active)} / 태풍 관련 {sorted(relevant)} (이전: {sorted(prev)})")
    when = last_time[5:16].replace("-", "/").replace("T", " ") if last_time else ""
    key = f"jma:{','.join(sorted(relevant)) or 'end'}:{NOW.isoformat()}"
    if relevant - prev:   # 새로 나왔거나 한 단계 올라간 경보가 있을 때만
        names = ", ".join(config.JMA_WARN_CODES[c] for c in sorted(relevant, key=lambda c: c not in config.JMA_SPECIAL_CODES))
        head = next((h for h in reversed(heads) if "台風" in h), "") or (heads[-1] if heads else "")
        text = f"🌀 [{config.JMA_AREA_NAME}] 기상청 경보 발령: {names}" + (f" ({when} 발표)" if when else "")
        if head:
            text += f" – {translate(head)}"
        return [official_item("JP", text, config.JMA_WARN_PAGE, "気象庁", key)]
    if prev and not relevant:
        return [official_item("JP", f"ℹ️ [해제] {config.JMA_AREA_NAME}의 태풍 관련 경보가 모두 해제됐어요.",
                              config.JMA_WARN_PAGE, "気象庁", key)]
    return []


QUAKE_KO = {"5-": "5약", "5+": "5강", "6-": "6약", "6+": "6강", "7": "7"}


def check_quake():
    """도쿄 23구·다마에서 진도 5강 이상이 관측된 지진 (지진 1건당 1번만 알림)"""
    log("  🏛 [JP] 気象庁 地震 (東京)")
    data = fetch_json(config.JMA_QUAKE_DATA)
    if not data:
        return []
    cutoff = (NOW - timedelta(days=1)).isoformat()
    items, done = [], set()
    for e in data:
        eid = e.get("eid")
        if not eid or eid in done or (e.get("at") or "") < cutoff:
            continue
        tokyo = [p for p in e.get("int", []) if p.get("code") == "13"]
        if not tokyo:
            continue
        cities = tokyo[0].get("city", [])
        mainland = [c for c in cities if str(c.get("code", "")).startswith(config.JMA_QUAKE_CITY_PREFIX)]
        if mainland:
            level = max((c.get("maxi", "") for c in mainland), key=lambda m: list(QUAKE_KO).index(m) if m in QUAKE_KO else -1)
        else:   # 시·구 정보가 없는 속보는 도쿄도 전체 최대 진도로 판단
            level = tokyo[0].get("maxi", "")
        if level not in config.JMA_QUAKE_MIN:
            continue
        done.add(eid)
        at = (e.get("at") or "")[5:16].replace("-", "/").replace("T", " ")
        place = translate(e.get("anm", "")) if e.get("anm") else "진원 정보 확인 중"
        mag = f" M{e['mag']}" if e.get("mag") else ""
        text = f"🔶 [{config.JMA_AREA_NAME}] 지진 진도 {QUAKE_KO[level]} 관측 – 진원: {place}{mag} ({at} 발생)"
        items.append(official_item("JP", text, config.JMA_QUAKE_PAGE, "気象庁", f"quake:{eid}"))
    return items


# =====================================================================
# 중복 기사 병합 (같은 사건을 여러 매체가 쓴 경우)
# =====================================================================
def merge_duplicates(items):
    merged = []
    for item in items:
        n = norm(item["title"])
        for kept in merged:
            if kept["country"] == item["country"] and similar(norm(kept["title"]), n):
                if item["source"] and item["source"] != kept["source"] and item["source"] not in kept["also"]:
                    kept["also"].append(item["source"])
                break
        else:
            merged.append(item)
    return merged


# =====================================================================
# 평일 판단 (주말·한국 공휴일 제외)
# =====================================================================
def is_business_day(d=None):
    d = (d or NOW).date()
    if d.weekday() >= 5:
        return False
    import holidays
    return d not in holidays.KR(years=d.year)


# =====================================================================
# 상태 기록 (state.json)
#   seen: 이미 보낸 기사 (즉시 알림이 매시간 반복되지 않도록)
#   pending: 휴일에 모아둔 소식 (다음 평일에 보냄)
#   last_weekly / last_monthly: 이번 주·이번 달에 이미 보냈는지
# =====================================================================
def load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            state = json.load(f)
    except Exception:
        state = {}
    state.setdefault("seen", {})
    state.setdefault("pending", [])
    state.setdefault("last_weekly", "")
    state.setdefault("last_monthly", "")
    state.setdefault("last_daily", "")
    state.setdefault("sent_kind", {})
    return state


DRY_RUN = False   # --dry-run 이면 상태 기록(state.json)도 바꾸지 않아요


def save_state(state):
    if DRY_RUN:
        log("🧪 시험 실행이라 상태 기록은 저장하지 않아요.")
        return
    cutoff = (NOW - timedelta(days=config.SEEN_KEEP_DAYS)).isoformat()
    state["seen"] = {k: v for k, v in state["seen"].items() if v >= cutoff}
    state["sent_kind"] = {k: v for k, v in state.get("sent_kind", {}).items() if k in state["seen"]}
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=1, sort_keys=True)


def to_json(item):
    d = dict(item)
    d["published"] = item["published"].isoformat() if item["published"] else None
    return d


def from_json(d):
    d = dict(d)
    d["published"] = datetime.fromisoformat(d["published"]) if d.get("published") else None
    return d


def item_key(item):
    return item.get("key") or "t:" + norm(item["title"])


def find_seen_key(item, seen):
    """이미 보낸 기사면 그 기록의 키를, 아니면 None"""
    key = item_key(item)
    if key in seen:
        return key
    if key.startswith("t:"):
        n = key[2:]
        for k in seen:
            if k.startswith("t:") and similar(k[2:], n):
                return k
    return None


def is_seen(item, seen):
    return find_seen_key(item, seen) is not None


def remember(state, items, kind):
    """보낸 기사를 기록 (kind: 긴급 / 일간)"""
    for i in items:
        k = item_key(i)
        state["seen"][k] = NOW.isoformat()
        state["sent_kind"][k] = kind


def mark_already_sent(state, items):
    """주간·월간에서 긴급/일간으로 이미 보낸 기사에 표시를 달아요 (빼지는 않음)"""
    for i in items:
        k = find_seen_key(i, state["seen"])
        if k:
            i["sent"] = state["sent_kind"].get(k, "발송")


# =====================================================================
# 슬랙 메시지 만들기
# =====================================================================
def format_item(item):
    ko = item.get("ko") or translate(source_text(item))   # 공식 발표 알림은 이미 한국어
    tag = f"_[{item['sent']} 발송]_ " if item.get("sent") else ""   # 이미 긴급/일간으로 보낸 기사 표시
    limit = 400 if item["category"] == GOV_URGENT else TITLE_MAX   # 정부 공식 발표는 지역 목록이 잘리지 않게
    line = f"• {tag}<{item['link']}|{slack_escape(short(ko, limit))}>"
    meta = []
    if item["source"]:
        src = item["source"]
        if item["also"]:
            src += f" 외 {len(item['also'])}개 매체"
        meta.append(src)
    if item["published"]:
        meta.append(item["published"].strftime("%m/%d %H:%M"))
    if config.SHOW_ORIGINAL and ko != item["title"]:
        line += f"\n     _{slack_escape(short(item['title'], 60))}_"
    if meta and getattr(config, "SHOW_SOURCE", False):   # 매체명·날짜 줄 (기본: 숨김)
        line += f"\n     {slack_escape(' · '.join(meta))}"
    return line


def build_sections(items, order):
    """카테고리별로 묶고 최신순 정렬 → 섹션 글 목록"""
    translate_many([source_text(i) for i in items if not i.get("ko")])   # 한 번에 미리 번역
    sections = []
    for label in order:
        group = [i for i in items if i["category"] == label]
        if not group:
            continue
        top = label in TOP_SECTIONS   # 많이 보도된 순(여러 매체가 다룬 기사 먼저)으로 고르는 구역
        if top:
            group.sort(key=lambda i: (len(i["also"]), i["published"] or NOW), reverse=True)
        else:
            group.sort(key=lambda i: i["published"] or NOW, reverse=True)
        word = "주요" if top else "최신"
        count = f"{len(group)}건" if len(group) <= MAX_PER_SECTION else f"{word} {MAX_PER_SECTION}건 / 총 {len(group)}건"
        text = f"*{label}*  ({count})"
        for item in group[:MAX_PER_SECTION]:
            line = format_item(item)
            if len(text) + len(line) + 1 > SECTION_LIMIT:   # 슬랙 블록 1개 글자 수 제한
                break
            text += "\n" + line
        sections.append(text)
    return sections


def make_message(title, subtitle, sections):
    """나라별 메시지 1개: 큰 제목 + 부제 + 카테고리 구역(구분선으로 나눔)"""
    blocks = [{"type": "header", "text": {"type": "plain_text", "text": short(title, 140), "emoji": True}}]
    if subtitle:
        blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text": subtitle}]})
    for i, s in enumerate(sections[:20]):
        if i:
            blocks.append({"type": "divider"})
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": s}})
    return {"text": title, "blocks": blocks}


def send(messages, dry_run):
    if dry_run or not SLACK_TOKEN or not SLACK_CHANNEL:
        if not dry_run:
            log("⚠️ SLACK_TOKEN 또는 SLACK_CHANNEL_HRBP 가 없어 화면에만 출력해요.")
        for m in messages:
            parts = []
            for b in m["blocks"]:
                if b["type"] == "divider":
                    parts.append("-" * 40)
                elif b["type"] == "context":
                    parts.append(b["elements"][0]["text"])
                else:
                    parts.append(b["text"]["text"])
            log("\n" + "=" * 60 + "\n" + "\n".join(parts))
        return True
    from slack_sdk import WebClient
    client = WebClient(token=SLACK_TOKEN)
    ok = True
    for m in messages:
        try:
            client.chat_postMessage(channel=SLACK_CHANNEL, text=m["text"], blocks=m["blocks"],
                                    unfurl_links=False, unfurl_media=False)
            time.sleep(1)
        except Exception as e:
            log(f"❌ 슬랙 전송 실패: {e}")
            ok = False
    return ok


# =====================================================================
# 실행 모드
# =====================================================================
def run_urgent(dry_run, force=False):
    """
    매시간: ① 공식 발표(기상청·중앙기상서·인사행정총처) + 모니터링 기업 파업·소송만 바로 보냄
    휴일(주말·한국 공휴일): 정부 공식 발표만 바로 보내고, 뉴스는 모아뒀다가 다음 평일 08시 이후에 보냄
    """
    state = load_state()
    workday = force or is_business_day()
    order = [GOV_URGENT, config.INDUSTRY_LABEL]
    log(f"📅 오늘은 {'평일' if workday else '휴일'}이에요.")

    pending = [from_json(d) for d in state["pending"]]
    release_held = force or NOW.hour >= 8   # 휴일에 모은 소식은 평일 08시 이후에만 보내요 (새벽 발송 X)
    messages, hold = [], []
    for country, c in config.COUNTRIES.items():
        log(f"\n🚨 [{c['name']}] 긴급 확인")
        items = []
        items += (check_cwa(state) + check_dgpa(state)) if country == "TW" else (check_jma(state) + check_quake())
        items += collect_industry(country, config.INDUSTRY_URGENT_ISSUES[country], days=1)
        items = merge_duplicates(items)
        new = [i for i in items if not is_seen(i, state["seen"])]
        log(f"  → 새 소식 {len(new)}건 (전체 {len(items)}건)")
        remember(state, new, "긴급")

        if workday:
            held = [i for i in pending if i["country"] == country] if release_held else []
            send_now = merge_duplicates(held + new)
            sub = "휴일 동안 모인 소식 포함" if held else ""
            title = f"🚨 {c['flag']} {c['name']} 긴급 알림"
        else:
            send_now = [i for i in new if i["category"] == GOV_URGENT]
            hold += [i for i in new if i["category"] != GOV_URGENT]
            sub = "휴일이라 정부 공식 발표만 보내요"
            title = f"🚨 {c['flag']} {c['name']} 휴일 긴급 알림"
        if send_now:
            subtitle = f"{NOW.strftime('%m/%d %H:%M')} KST" + (f" · {sub}" if sub else "")
            messages.append(make_message(title, subtitle, build_sections(send_now, order)))

    ok = send(messages, dry_run) if messages else True
    if not messages:
        log("\n✅ 지금 보낼 긴급 소식 없음")
    if ok:
        if workday:
            state["pending"] = [] if release_held else [to_json(i) for i in pending]
        else:
            state["pending"] = [to_json(i) for i in pending + hold]
            log(f"🗂 다음 평일에 보낼 소식: {len(state['pending'])}건")
        save_state(state)


def run_watch(dry_run, force=False):
    """저녁 집중 확인: 대만 태풍경보가 타이베이에 걸려 있을 때만 휴무 발표를 15분마다 확인"""
    state = load_state()
    items = check_cwa(state)
    if state.get("cwa_phase") or force:
        items += check_dgpa(state)
    else:
        log("  → 타이베이 관련 태풍경보가 없어 휴무 발표 확인은 건너뛰어요 (매시간 확인은 계속)")
    ok = True
    if items:
        c = config.COUNTRIES["TW"]
        msg = make_message(f"🚨 {c['flag']} {c['name']} 긴급 알림", f"{NOW.strftime('%m/%d %H:%M')} KST",
                           build_sections(items, [GOV_URGENT]))
        ok = send([msg], dry_run)
        if ok:
            remember(state, items, "긴급")
    if ok:
        save_state(state)


def days_since_last_business_day():
    """오늘 아침 기준, 직전 평일 아침 이후 며칠이 지났는지 (월요일=3, 연휴 뒤엔 더 길게)"""
    d = NOW
    n = 0
    while True:
        d -= timedelta(days=1)
        n += 1
        if is_business_day(d) or n >= 10:
            return n


def run_daily(dry_run, force=False):
    """평일 아침 1번: 무신사 언급 (없으면 보내지 않음). ①은 공식 발표 즉시 알림으로 옮겼어요"""
    state = load_state()
    today = NOW.strftime("%Y-%m-%d")
    if not force and (not is_business_day() or state.get("last_daily") == today):
        log("⏭ 오늘은 일간 알림을 보내는 날이 아니에요.")
        return
    days = days_since_last_business_day()
    order = [config.CATEGORIES["musinsa"]["label"]]
    messages = []
    sent_today = []
    for country, c in config.COUNTRIES.items():
        log(f"\n☀️ [{c['name']}] 일간 수집 (최근 {days}일)")
        items = collect_keywords(country, "musinsa", days=days)
        items = merge_duplicates(items)
        items = [i for i in items if not is_seen(i, state["seen"])]   # 긴급으로 이미 보낸 건 빼요
        sent_today += items
        if items:
            subtitle = f"{NOW.strftime('%m/%d')} 아침 · 최근 {days}일"
            messages.append(make_message(f"☀️ {c['flag']} {c['name']} 아침 소식",
                                         subtitle, build_sections(items, order)))
    if collection_failed():
        return   # 오늘 표시를 남기지 않으니 다음 예약 시간(오전 10:37)에 다시 시도해요
    ok = send(messages, dry_run) if messages else True
    if not messages:
        log("\n✅ 오늘은 보낼 소식이 없어요.")
    if ok:
        remember(state, sent_today, "일간")
        if not force:
            state["last_daily"] = today
        save_state(state)


def run_weekly(dry_run, force=False):
    """매주 첫 평일에 1번 (월요일이 공휴일이면 화요일)"""
    state = load_state()
    week = "%d-W%02d" % NOW.isocalendar()[:2]
    if not force and (not is_business_day() or state["last_weekly"] == week):
        log("⏭ 오늘은 주간 다이제스트를 보내는 날이 아니에요.")
        return
    days = 7 + NOW.weekday() if not force else 7   # 월요일이 휴일이라 늦게 보내도 빠지는 날이 없도록
    order = [config.CATEGORIES["law"]["label"], config.CATEGORIES["ruling"]["label"],
             config.INDUSTRY_LABEL, config.CATEGORIES["musinsa"]["label"], "🏛 정부 기관 발표"]
    start = (NOW - timedelta(days=days)).strftime("%m/%d")
    messages = []
    for country, c in config.COUNTRIES.items():
        log(f"\n📋 [{c['name']}] 주간 수집")
        items = collect_gov_feeds(country, days=days)
        items += collect_keywords(country, "law", days=days)
        items += collect_keywords(country, "ruling", days=days)
        items += collect_industry(country, config.INDUSTRY_ISSUES[country], days=days)
        items += collect_keywords(country, "musinsa", days=days)
        items = merge_duplicates(items)
        mark_already_sent(state, items)
        sections = build_sections(items, order) or ["이번 주는 해당 소식이 없어요."]
        messages.append(make_message(f"📋 {c['flag']} {c['name']} 주간 노동·HR 뉴스",
                                     f"{start} ~ {NOW.strftime('%m/%d')} · 카테고리별 최신 {MAX_PER_SECTION}건",
                                     sections))
    if collection_failed():
        return   # 다음 평일 아침에 다시 시도해요
    if send(messages, dry_run) and not force:
        state["last_weekly"] = week
        save_state(state)


def run_monthly(dry_run, force=False):
    """매월 첫 평일에 1번"""
    state = load_state()
    month = NOW.strftime("%Y-%m")
    if not force and (not is_business_day() or state["last_monthly"] == month):
        log("⏭ 오늘은 월간 지표를 보내는 날이 아니에요.")
        return
    label = config.CATEGORIES["comp"]["label"]
    messages = []
    for country, c in config.COUNTRIES.items():
        log(f"\n💰 [{c['name']}] 월간 수집")
        items = merge_duplicates(collect_keywords(country, "comp", days=31))
        mark_already_sent(state, items)
        sections = build_sections(items, [label]) or ["이번 달은 해당 소식이 없어요."]
        messages.append(make_message(f"💰 {c['flag']} {c['name']} 월간 보상 시장 지표",
                                     NOW.strftime("%Y년 %m월"), sections))
    if collection_failed():
        return   # 다음 날 아침에 다시 시도해요
    if send(messages, dry_run) and not force:
        state["last_monthly"] = month
        save_state(state)


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else ""
    dry = "--dry-run" in sys.argv
    force = "--force" in sys.argv   # 수동 실행 시 휴일·중복 체크 없이 바로 보내기
    DRY_RUN = dry
    runners = {"urgent": run_urgent, "watch": run_watch, "daily": run_daily,
               "weekly": run_weekly, "monthly": run_monthly}
    if mode not in runners:
        print(__doc__)
        sys.exit(1)
    runners[mode](dry, force)
