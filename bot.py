"""
Global News Bot - 대만·일본 노동/HR 뉴스 클리핑

실행 방법
  python bot.py urgent    # ① 긴급 + ④ 파업·집단소송 + 정부 재해 정보 (매시간)
                          #   휴일엔 정부 공식 발표만 바로, 나머지는 다음 평일에
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
MAX_PER_SECTION = 15        # 한 섹션에 최대 몇 건까지 보여줄지
MSG_LIMIT = 3500            # 슬랙 메시지 1개당 최대 글자 수 (넘으면 나눠서 보냄)

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
_translator = None
_cache = {}
_fails = 0


def translate(text):
    global _translator, _fails
    if not text or _fails >= 3:   # 연속 3번 실패하면 이번 실행에선 번역을 건너뛰어요
        return text
    if text in _cache:
        return _cache[text]
    result = text
    try:
        if _translator is None:
            from deep_translator import GoogleTranslator
            _translator = GoogleTranslator(source="auto", target="ko")
        translated = _translator.translate(text[:4500])
        if translated:
            result = translated
        _fails = 0
    except Exception as e:
        _fails += 1
        log(f"  ⚠️ 번역 실패, 원문 사용: {short(text, 30)} ({type(e).__name__})")
    _cache[text] = result
    return result


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
    for company in companies:
        log(f"  🔍 [{country}] {company} {issue_query}")
        items += google_news(country, f'"{company}" {issue_query}', days,
                             INDUSTRY_PER_QUERY, config.INDUSTRY_LABEL)
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
            items.append({
                "country": country, "category": "🏛 정부 기관 발표", "query": "",
                "title": title, "link": e.get("link", ""), "source": feed["name"].split(" (")[0],
                "published": published, "also": [],
            })
    return items


def check_dgpa():
    """대만 인사행정총처 停班停課 페이지에서 '停止上班(출근 중단)' 발표가 있는지 확인"""
    log("  🏛 [TW] 人事行政總處 停班停課")
    content = fetch(config.DGPA_URL)
    if not content:
        return []
    html = content.decode("utf-8", errors="ignore")
    html = re.sub(r"(?is)<(script|style).*?</\1>", " ", html)
    text = re.sub(r"(?s)<[^>]+>", "\n", html)
    lines = [re.sub(r"\s+", " ", l).strip() for l in text.split("\n")]
    hits = [l for l in lines if "停止上班" in l or "停止上課" in l]
    if not hits:
        return []
    summary = " / ".join(dict.fromkeys(hits))  # 중복 줄 제거
    return [{
        "country": "TW", "category": GOV_URGENT, "query": "",
        "title": short(f"[人事行政總處] {summary}", 300), "link": config.DGPA_URL,
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
    return state


def save_state(state):
    cutoff = (NOW - timedelta(days=config.SEEN_KEEP_DAYS)).isoformat()
    state["seen"] = {k: v for k, v in state["seen"].items() if v >= cutoff}
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


def is_seen(item, seen):
    key = item_key(item)
    if key in seen:
        return True
    if key.startswith("t:"):
        n = key[2:]
        return any(k.startswith("t:") and similar(k[2:], n) for k in seen)
    return False


# =====================================================================
# 슬랙 메시지 만들기
# =====================================================================
def format_item(item):
    ko = translate(item["title"])
    line = f"• <{item['link']}|{slack_escape(short(ko, 90))}>"
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
    if meta:
        line += f"\n     {slack_escape(' · '.join(meta))}"
    return line


def build_sections(items, order):
    """카테고리별로 묶고 최신순 정렬"""
    lines = []
    for label in order:
        group = [i for i in items if i["category"] == label]
        if not group:
            continue
        group.sort(key=lambda i: i["published"] or NOW, reverse=True)
        lines.append(f"\n*{label}* ({len(group)}건)")
        for item in group[:MAX_PER_SECTION]:
            lines.append(format_item(item))
        if len(group) > MAX_PER_SECTION:
            lines.append(f"     …외 {len(group) - MAX_PER_SECTION}건")
    return lines


def split_messages(header, lines):
    """긴 메시지는 슬랙 제한에 맞게 여러 개로 나눠요"""
    messages, current = [], header
    for line in lines:
        if len(current) + len(line) + 1 > MSG_LIMIT:
            messages.append(current)
            current = header + " (이어서)"
        current += "\n" + line
    messages.append(current)
    return messages


def send(messages, dry_run):
    if dry_run or not SLACK_TOKEN or not SLACK_CHANNEL:
        if not dry_run:
            log("⚠️ SLACK_TOKEN 또는 SLACK_CHANNEL_HRBP 가 없어 화면에만 출력해요.")
        for m in messages:
            log("\n" + "=" * 60 + "\n" + m)
        return True
    from slack_sdk import WebClient
    client = WebClient(token=SLACK_TOKEN)
    ok = True
    for m in messages:
        try:
            client.chat_postMessage(channel=SLACK_CHANNEL, text=m, mrkdwn=True,
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
    평일: 새 소식을 바로 보내고, 휴일에 모아둔 소식도 함께 보냄
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
        for i in new:
            state["seen"][item_key(i)] = NOW.isoformat()

        if workday:
            held = [i for i in pending if i["country"] == country]
            send_now = merge_duplicates(held + new)
            title = "긴급 알림" if not held else f"긴급 알림 (휴일 동안 모인 소식 {len(held)}건 포함)"
        else:
            send_now = [i for i in new if i["category"] == GOV_URGENT]
            hold += [i for i in new if i["category"] != GOV_URGENT]
            title = "휴일 긴급 알림 (정부 공식 발표)"
        if send_now:
            header = f"🚨 *{c['flag']} {c['name']} {title}* · {NOW.strftime('%m/%d %H:%M')} KST"
            messages += split_messages(header, build_sections(send_now, order))

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


def run_weekly(dry_run, force=False):
    """매주 첫 평일에 1번 (월요일이 공휴일이면 화요일)"""
    state = load_state()
    week = "%d-W%02d" % NOW.isocalendar()[:2]
    if not force and (not is_business_day() or state["last_weekly"] == week):
        log("⏭ 오늘은 주간 다이제스트를 보내는 날이 아니에요.")
        return
    days = 7 + NOW.weekday()   # 월요일이 휴일이라 늦게 보내도 빠지는 날이 없도록
    order = ["🏛 정부 기관 발표", config.CATEGORIES["law"]["label"],
             config.CATEGORIES["ruling"]["label"], config.INDUSTRY_LABEL]
    start = (NOW - timedelta(days=days)).strftime("%m/%d")
    messages = []
    for country, c in config.COUNTRIES.items():
        log(f"\n📋 [{c['name']}] 주간 수집")
        items = collect_gov_feeds(country, days=days)
        items += collect_keywords(country, "law", days=days)
        items += collect_keywords(country, "ruling", days=days)
        items += collect_industry(country, config.INDUSTRY_ISSUES[country], days=days)
        items = merge_duplicates(items)
        header = f"📋 *{c['flag']} {c['name']} 주간 노동·HR 뉴스* · {start}~{NOW.strftime('%m/%d')}"
        lines = build_sections(items, order) or ["이번 주는 해당 소식이 없어요."]
        messages += split_messages(header, lines)
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
        header = f"💰 *{c['flag']} {c['name']} 월간 보상 시장 지표* · {NOW.strftime('%Y년 %m월')}"
        lines = build_sections(items, [label]) or ["이번 달은 해당 소식이 없어요."]
        messages += split_messages(header, lines)
    if send(messages, dry_run) and not force:
        state["last_monthly"] = month
        save_state(state)


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else ""
    dry = "--dry-run" in sys.argv
    force = "--force" in sys.argv   # 수동 실행 시 휴일·중복 체크 없이 바로 보내기
    runners = {"urgent": run_urgent, "weekly": run_weekly, "monthly": run_monthly}
    if mode not in runners:
        print(__doc__)
        sys.exit(1)
    runners[mode](dry, force)
