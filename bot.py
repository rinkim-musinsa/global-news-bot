"""
Global News Bot - 대만·일본 노동/HR 뉴스 클리핑

실행 방법
  python bot.py urgent    # 긴급: 정부 재해 발표 + 확실한 긴급 키워드 + 기업 파업·소송 (매시간)
                          #   휴일엔 정부 공식 발표만 바로, 나머지는 다음 평일에
  python bot.py daily     # 일간: 근무 영향 소식 묶음 (평일 아침 1번, 없으면 안 보냄)
  python bot.py weekly    # ②③④ + 정부 기관 발표 (매주)
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


def fetch(url):
    """URL 내용을 가져와요. 실패하면 None"""
    try:
        res = requests.get(url, headers=HEADERS, timeout=20)
        res.raise_for_status()
        return res.content
    except Exception as e:
        log(f"  ⚠️ 가져오기 실패: {url} ({e})")
        return None


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
    for e in parse_feed(url)[:limit]:
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
        items += google_news(country, kw, days, config.PER_QUERY, cat["label"])
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


def check_dgpa():
    """대만 인사행정총처 停班停課 표에서 '停止上班/停止上課(출근·등교 중단)'인 지역만 골라요.
    표 아래 고정 안내문에도 '停止上班'이 들어 있어서, 표의 [지역명 | 상태] 칸만 읽어요."""
    log("  🏛 [TW] 人事行政總處 停班停課")
    content = fetch(config.DGPA_URL)
    if not content:
        return []
    html = content.decode("utf-8", errors="ignore")
    clean = lambda x: re.sub(r"\s+", " ", re.sub(r"(?s)<[^>]+>", " ", x)).strip()
    groups = {}   # 상태 문구 → 지역 목록
    for city, status in re.findall(r'(?s)headers="city_Name[^"]*"[^>]*>(.*?)</td>\s*<td[^>]*>(.*?)</td>', html):
        city, status = clean(city), clean(status)
        if "停止上班" in status or "停止上課" in status:
            groups.setdefault(status, []).append(city)
    if not groups:
        return []
    parts = []
    for status, cities in groups.items():
        names = "、".join(cities[:6]) + (f" 외 {len(cities) - 6}곳" if len(cities) > 6 else "")
        parts.append(f"{status} ({len(cities)}곳: {names})")
    summary = " / ".join(parts)
    return [{
        "country": "TW", "category": GOV_URGENT, "query": "",
        "title": f"[人事行政總處] {summary}", "link": config.DGPA_URL,
        "source": "人事行政總處", "published": NOW, "also": [],
        "key": "dgpa:" + hashlib.md5(summary.encode()).hexdigest(),
    }]


def check_jma():
    """일본 기상청: 특별경보, 진도 5강 이상 지진"""
    items = []
    cutoff = NOW - timedelta(days=1)
    targets = [
        ("weather", lambda t: "特別警報" in t, "https://www.jma.go.jp/bosai/map.html#contents=warning"),
        ("quake", lambda t: any(k in t for k in config.JMA_QUAKE_MIN),
         "https://www.jma.go.jp/bosai/map.html#contents=earthquake_map"),
    ]
    for key, match, page in targets:
        log(f"  🏛 [JP] 気象庁 {key}")
        for e in parse_feed(config.JMA_FEEDS[key]):
            updated = to_dt(e.get("updated_parsed"))
            if updated and updated < cutoff:
                continue
            title = e.get("title", "")
            body = unicodedata.normalize("NFKC", e.get("summary", "") or e.get("content", [{}])[0].get("value", ""))
            if not match(unicodedata.normalize("NFKC", title) + body):
                continue
            items.append({
                "country": "JP", "category": GOV_URGENT, "query": "",
                "title": short(f"[気象庁] {title}: {body}", 200), "link": page,
                "source": "気象庁", "published": updated, "also": [],
                "key": "jma:" + e.get("id", title + body),
            })
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


def save_state(state):
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
    ko = translate(item["title"])
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
    translate_many([i["title"].replace("\n", " ") for i in items])   # 한 번에 미리 번역
    sections = []
    for label in order:
        group = [i for i in items if i["category"] == label]
        if not group:
            continue
        group.sort(key=lambda i: i["published"] or NOW, reverse=True)
        count = f"{len(group)}건" if len(group) <= MAX_PER_SECTION else f"최신 {MAX_PER_SECTION}건 / 총 {len(group)}건"
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
    매시간: 정부 공식 발표 + 확실한 긴급 키워드 + 모니터링 기업 파업·소송만 바로 보냄
    휴일(주말·한국 공휴일): 정부 공식 발표만 바로 보내고, 뉴스는 모아뒀다가 다음 평일에 보냄
    """
    state = load_state()
    workday = force or is_business_day()
    order = [GOV_URGENT, config.CATEGORIES["urgent"]["label"], config.INDUSTRY_LABEL]
    log(f"📅 오늘은 {'평일' if workday else '휴일'}이에요.")

    pending = [from_json(d) for d in state["pending"]]
    messages, hold = [], []
    for country, c in config.COUNTRIES.items():
        log(f"\n🚨 [{c['name']}] 긴급 확인")
        items = []
        items += check_dgpa() if country == "TW" else check_jma()
        items += collect_keywords(country, "urgent", days=1)
        items += collect_industry(country, config.INDUSTRY_URGENT_ISSUES[country], days=1)
        items = merge_duplicates(items)
        new = [i for i in items if not is_seen(i, state["seen"])]
        log(f"  → 새 소식 {len(new)}건 (전체 {len(items)}건)")
        remember(state, new, "긴급")

        if workday:
            held = [i for i in pending if i["country"] == country]
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
            state["pending"] = []
        else:
            state["pending"] = [to_json(i) for i in pending + hold]
            log(f"🗂 다음 평일에 보낼 소식: {len(state['pending'])}건")
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
    """평일 아침 1번: 근무에 영향 줄 수 있는 소식 묶음 (없으면 보내지 않음)"""
    state = load_state()
    today = NOW.strftime("%Y-%m-%d")
    if not force and (not is_business_day() or state.get("last_daily") == today):
        log("⏭ 오늘은 일간 알림을 보내는 날이 아니에요.")
        return
    days = days_since_last_business_day()
    label = config.CATEGORIES["daily"]["label"]
    messages = []
    sent_today = []
    for country, c in config.COUNTRIES.items():
        log(f"\n☀️ [{c['name']}] 일간 수집 (최근 {days}일)")
        items = merge_duplicates(collect_keywords(country, "daily", days=days))
        items = [i for i in items if not is_seen(i, state["seen"])]   # 긴급으로 이미 보낸 건 빼요
        sent_today += items
        if items:
            subtitle = f"{NOW.strftime('%m/%d')} 아침 · 최근 {days}일"
            messages.append(make_message(f"☀️ {c['flag']} {c['name']} 오늘의 근무 영향 소식",
                                         subtitle, build_sections(items, [label])))
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
             config.INDUSTRY_LABEL, "🏛 정부 기관 발표"]
    start = (NOW - timedelta(days=days)).strftime("%m/%d")
    messages = []
    for country, c in config.COUNTRIES.items():
        log(f"\n📋 [{c['name']}] 주간 수집")
        items = collect_gov_feeds(country, days=days)
        items += collect_keywords(country, "law", days=days)
        items += collect_keywords(country, "ruling", days=days)
        items += collect_industry(country, config.INDUSTRY_ISSUES[country], days=days)
        items = merge_duplicates(items)
        mark_already_sent(state, items)
        sections = build_sections(items, order) or ["이번 주는 해당 소식이 없어요."]
        messages.append(make_message(f"📋 {c['flag']} {c['name']} 주간 노동·HR 뉴스",
                                     f"{start} ~ {NOW.strftime('%m/%d')} · 카테고리별 최신 {MAX_PER_SECTION}건",
                                     sections))
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
    if send(messages, dry_run) and not force:
        state["last_monthly"] = month
        save_state(state)


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else ""
    dry = "--dry-run" in sys.argv
    force = "--force" in sys.argv   # 수동 실행 시 휴일·중복 체크 없이 바로 보내기
    runners = {"urgent": run_urgent, "daily": run_daily, "weekly": run_weekly, "monthly": run_monthly}
    if mode not in runners:
        print(__doc__)
        sys.exit(1)
    runners[mode](dry, force)
