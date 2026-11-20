#!/usr/bin/env python3
"""매일 뉴스 요약 팟캐스트 생성기 (전부 무료 구성)

RSS(무료) -> Gemini 무료 티어(대본 요약, 하루 1~3회 호출) -> edge-tts(무료 TTS)
-> mp3(4MB 이하로 분할) + podcast RSS(feed.xml) + index.html
"""
import asyncio
import datetime as dt
import html
import json
import os
import re
import sys
import time
from email.utils import format_datetime
from pathlib import Path
from urllib.parse import quote
from xml.sax.saxutils import escape

import edge_tts
import feedparser
import requests

# ───────────── 설정 (환경변수로 덮어쓸 수 있음) ─────────────
KST = dt.timezone(dt.timedelta(hours=9))
OUT = Path(os.environ.get("OUT_DIR", "docs"))
EP = OUT / "episodes"
TITLE = os.environ.get("PODCAST_TITLE", "뉴스 브리핑")  # 피드 전체 이름
VOICES = {
    "A": os.environ.get("VOICE_A", "ko-KR-SunHiNeural"),
    "B": os.environ.get("VOICE_B", "ko-KR-InJoonNeural"),
}
VOICES_EN = {
    "A": os.environ.get("VOICE_EN_A", "en-US-AriaNeural"),
    "B": os.environ.get("VOICE_EN_B", "en-US-GuyNeural"),
}
TARGET_CHARS_EN = int(os.environ.get("TARGET_CHARS_EN", "7000"))  # 영어 약 9분 분량
RATE = os.environ.get("TTS_RATE", "+5%")
TARGET_CHARS = int(os.environ.get("TARGET_CHARS", "4000"))  # 약 11~12분 분량
MAX_PART_BYTES = int(os.environ.get("MAX_PART_BYTES", str(4_000_000)))  # 파일 1개 최대 ~4MB
KEEP_DAYS = int(os.environ.get("KEEP_DAYS", "7"))
GEMINI_MODELS = os.environ.get("GEMINI_MODELS", "gemini-2.5-flash,gemini-2.5-flash-lite")

def gsearch(q, lang="ko"):
    tail = "hl=ko&gl=KR&ceid=KR:ko" if lang == "ko" else "hl=en-US&gl=US&ceid=US:en"
    return f"https://news.google.com/rss/search?q={quote('(' + q + ') when:2d')}&{tail}"


HAS_KEY = bool(os.environ.get("GEMINI_API_KEY", "").strip())
BODY_CHARS = int(os.environ.get("BODY_CHARS", "2500"))  # 기사 1건당 가져올 본문 길이
PROMPT_EXCERPT = 1500  # AI에게 줄 때는 이만큼만
FETCH_BUDGET = int(os.environ.get("FETCH_BUDGET", "240"))  # 본문 수집 제한시간(초)
G = "https://news.google.com/rss/headlines/section/topic/{}?hl=ko&gl=KR&ceid=KR:ko"
EDITIONS = [  # 하루에 에디션마다 mp3가 따로 만들어진다
    {
        "id": "korea",
        "title": os.environ.get("TITLE_KR", "한국 뉴스 브리핑"),
        "focus": "한국 전반의 주요 뉴스(정치·사회·경제·국제·IT)",
        "english": False,
        "needs_ai": False,
        "max_chars": int(os.environ.get("MAX_CHARS_KO", "16000")),  # AI 없이 읽을 때 본문 총량 (약 45분)
        "feeds": [  # (섹션 이름, RSS 주소, 최대 기사 수, 본문까지 읽을 기사 수)
            ("주요 뉴스", "https://news.google.com/rss?hl=ko&gl=KR&ceid=KR:ko", 8, 6),
            ("국내", G.format("NATION"), 6, 4),
            ("세계", G.format("WORLD"), 5, 3),
            ("경제", G.format("BUSINESS"), 5, 3),
            ("IT·과학", G.format("TECHNOLOGY"), 4, 2),
            ("연합뉴스", "https://www.yna.co.kr/rss/news.xml", 6, 3),
        ],
    },
    {
        "id": "sea",
        "title": os.environ.get("TITLE_SEA", "Southeast Asia News Briefing"),
        "focus": "Southeast Asian politics and society, especially Indonesia",
        "lang": "en",  # 번역 없이 영어 그대로 방송
        "english": True,
        "needs_ai": False,
        "max_chars": int(os.environ.get("MAX_CHARS_EN", "32000")),  # 약 40분
        "feeds": [
            ("Indonesia politics", gsearch("Indonesia politics OR Prabowo OR Jakarta government", "en"), 7, 5),
            ("Indonesia society", gsearch("Indonesia protest OR court OR corruption OR police OR education OR social", "en"), 6, 4),
            ("Southeast Asia politics", gsearch("ASEAN OR Thailand politics OR Philippines politics OR Vietnam politics OR Malaysia politics", "en"), 7, 4),
            ("Southeast Asia society", gsearch("Southeast Asia society OR Myanmar OR Thailand court OR Philippines protest", "en"), 5, 3),
        ],
    },
]
ED_TITLES = {e["id"]: e["title"] for e in EDITIONS}
UA = {"User-Agent": "Mozilla/5.0 (news-podcast)"}
WEEK = "월화수목금토일"


# ───────────── 1. 뉴스 수집 ─────────────
def clean(s):
    s = re.sub(r"<[^>]+>", " ", s or "")
    return re.sub(r"\s+", " ", html.unescape(s)).strip()


def article_text(link):
    """기사 본문 발췌. 실패하면 빈 문자열(제목만 사용)."""
    try:
        import trafilatura

        if "news.google.com" in link:
            from googlenewsdecoder import gnewsdecoder

            res = gnewsdecoder(link, interval=1)
            if not res.get("status"):
                return ""
            link = res["decoded_url"]
        r = requests.get(link, headers=UA, timeout=15)
        r.raise_for_status()
        return clean(trafilatura.extract(r.content, include_comments=False) or "")[:BODY_CHARS]
    except Exception:
        return ""


def collect(ed):
    seen, sections = set(), []
    deadline = time.time() + FETCH_BUDGET
    for name, url, n, deep in ed["feeds"]:
        try:
            r = requests.get(url, headers=UA, timeout=20)
            r.raise_for_status()
            d = feedparser.parse(r.content)
        except Exception as e:
            print(f"[경고] {name} 수집 실패: {e}")
            continue
        items = []
        for e in d.entries:
            title = re.sub(r"\s-\s[^-]+$", "", clean(e.get("title", "")))  # ' - 매체명' 제거
            key = re.sub(r"\W", "", title)[:18]
            if not title or key in seen:
                continue
            seen.add(key)
            body = ""
            if len(items) < deep and time.time() < deadline:
                body = article_text(e.get("link", ""))
            items.append((title, "", body))
            if len(items) >= n:
                break
        if items:
            print(f"[수집:{ed['id']}] {name}: {len(items)}건, 본문 {sum(1 for i in items if i[2])}건")
            sections.append((name, items))
    return sections


# ───────────── 2. 대본 만들기 ─────────────
def make_prompt(ed, sections, today):
    if ed.get("lang") == "en":
        return make_prompt_en(ed, sections, today)
    date = f"{today.year}년 {today.month}월 {today.day}일 {WEEK[today.weekday()]}요일"
    news = "\n".join(
        f"[{name}]\n" + "\n".join(f"- {t}" + (f"\n  본문 발췌: {b[:PROMPT_EXCERPT]}" if b else "") for t, _, b in items)
        for name, items in sections
    )
    return f"""너는 한국어 뉴스 팟캐스트 '{ed["title"]}'의 작가다. 이번 방송의 주제는 {ed["focus"]}이다. 아래 [오늘의 뉴스]만을 근거로 진행자 두 명(A, B)의 대화형 대본을 써라.
규칙:
- 오늘 날짜는 {date}. 첫 대사는 인사와 날짜 소개.
- 분량은 한국어 약 {TARGET_CHARS}자(낭독 약 11분).
- 중요한 이슈 6~8개를 골라 각각 4~6문장으로 배경까지 풀어 설명하고, 비슷한 기사는 하나로 묶는다. {'인도네시아 소식을 가장 비중 있게 다루고, 정치와 사회 이슈를 중심으로 한다.' if ed['english'] else '정치·사회·경제 등 분야를 골고루 다룬다.'}
- '본문 발췌'가 있는 기사는 그 내용을 이해해 자기 말로 다시 설명한다(문장을 그대로 옮기지 않는다). {'원문은 영어이므로 자연스러운 한국어로 번역해 현지 보도에 따르면처럼 소개하고, 인명·지명은 한국어 표기 뒤에 필요하면 영문을 병기하지 말고 한국어로만 말한다.' if ed['english'] else ''}
- 제공된 헤드라인·본문에 없는 사실·수치·인용을 지어내지 않는다. 불확실하면 '보도에 따르면'처럼 표현한다.
- 모든 줄은 반드시 'A: 내용' 또는 'B: 내용' 형식. 마크다운, 이모지, 괄호 지문, 제목은 금지.
- 마지막은 짧은 마무리 인사.

[오늘의 뉴스]
{news}"""


def make_prompt_en(ed, sections, today):
    date = today.strftime("%A, %B %d, %Y")
    news = "\n".join(
        f"[{name}]\n" + "\n".join(f"- {t}" + (f"\n  Excerpt: {b[:PROMPT_EXCERPT]}" if b else "") for t, _, b in items)
        for name, items in sections
    )
    return f"""You write the English-language news podcast '{ed["title"]}'. Topic: {ed["focus"]}. Write a two-host dialogue script (hosts A and B) based ONLY on the news below.
Rules:
- Today is {date}. Open with a greeting and today's date.
- Length: about {TARGET_CHARS_EN} characters of English (roughly nine minutes spoken).
- Pick 6-8 important stories and explain each in 4-6 sentences with background; merge similar stories. Focus on politics and society, with Indonesia most prominent.
- Where an excerpt is given, understand it and retell it in your own words; do not copy sentences.
- Do not invent facts, figures or quotes that are not in the material. If unsure, say 'according to reports'.
- Every line must be exactly 'A: text' or 'B: text'. No markdown, emojis, stage directions or titles.
- End with a short sign-off.

[Today's news]
{news}"""


def gemini(prompt):
    """무료 티어 키만 사용. 호출은 하루 최대 몇 번뿐이며 실패하면 None."""
    key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not key:
        print("[안내] GEMINI_API_KEY 없음 → AI 없이 헤드라인 낭독 모드")
        return None
    for model in [m.strip() for m in GEMINI_MODELS.split(",") if m.strip()]:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
        body = {
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {"temperature": 0.6, "maxOutputTokens": 8192},
        }
        try:
            r = requests.post(url, headers={"x-goog-api-key": key}, json=body, timeout=120)
        except Exception as e:
            print(f"[경고] {model} 호출 실패: {e}")
            continue
        if r.status_code == 200:
            try:
                return r.json()["candidates"][0]["content"]["parts"][0]["text"]
            except Exception:
                print(f"[경고] {model} 응답 해석 실패")
                continue
        print(f"[경고] {model} HTTP {r.status_code}: {r.text[:150]}")
        if r.status_code == 429:  # 무료 한도 초과 → 과금 없이 다음 모델/대체 모드로
            time.sleep(15)
    return None


def parse_script(text):
    lines = []
    for raw in (text or "").splitlines():
        m = re.match(r"^\s*[*\-#\s]*(A|B)\s*[:：]\s*\**\s*(.+?)\s*$", raw)
        if m:
            t = m.group(2).replace("*", "").strip()
            if t:
                lines.append((m.group(1), t))
    return lines if len(lines) >= 6 else None


def chunk(text, maxlen=350):
    """본문을 문장 단위로 끊어 TTS가 안정적으로 읽을 길이로 나눈다."""
    out, cur = [], ""
    for sn in re.split(r"(?<=[.!?])\s+", text):
        if cur and len(cur) + len(sn) + 1 > maxlen:
            out.append(cur)
            cur = sn
        else:
            cur = (cur + " " + sn).strip()
    if cur:
        out.append(cur)
    return out


def fallback_script(ed, sections, today):
    """AI 없이: 제목(진행자 A) 뒤에 본문을 그대로 낭독(진행자 B). 총량은 max_chars로 제한."""
    en = ed.get("lang") == "en"
    if en:
        lines = [("A", f"Hello, this is the {ed['title']}. Here are the top stories for {today.strftime('%B %d, %Y')}.")]
    else:
        lines = [("A", f"안녕하세요, {ed['title']}입니다. {today.month}월 {today.day}일 {WEEK[today.weekday()]}요일, 주요 소식을 전해드립니다.")]
    per_section = ed["max_chars"] // max(len(sections), 1)
    for name, items in sections:
        lines.append(("A", f"Now, {name}." if en else f"{name} 소식입니다."))
        used = 0
        for t, _, b in items:
            lines.append(("A", t + "."))
            if b and used < per_section:
                lines += [("B", c) for c in chunk(b[: per_section - used])]
                used += len(b)
    lines.append(("A", "That's all for today's briefing. Thank you for listening." if en else "오늘 브리핑은 여기까지입니다. 감사합니다."))
    return lines


# ───────────── 3. 음성(TTS) ─────────────
async def synth(text, voice):
    for _ in range(3):
        try:
            data = bytearray()
            async for ch in edge_tts.Communicate(text, voice, rate=RATE).stream():
                if ch["type"] == "audio":
                    data.extend(ch["data"])
            if data:
                return bytes(data)
        except Exception as e:
            print(f"[경고] TTS 재시도: {e}")
            await asyncio.sleep(3)
    return b""


async def make_parts(lines, voices):
    sem = asyncio.Semaphore(3)  # 동시에 3줄씩 변환해 시간 단축

    async def one(spk, text):
        async with sem:
            return await synth(text, voices[spk])

    datas = await asyncio.gather(*(one(s, t) for s, t in lines))
    parts, cur = [], bytearray()
    for data in datas:
        if not data:
            continue
        if cur and len(cur) + len(data) > MAX_PART_BYTES:  # 큰 파일 방지: 분할
            parts.append(bytes(cur))
            cur = bytearray()
        cur.extend(data)
    if cur:
        parts.append(bytes(cur))
    return parts


# ───────────── 4. 저장 · 피드 · 웹페이지 ─────────────
def load_state():
    f = OUT / "episodes.json"
    return json.loads(f.read_text(encoding="utf-8")) if f.exists() else []


def site_url():
    if os.environ.get("SITE_URL"):
        return os.environ["SITE_URL"].rstrip("/")
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    if "/" in repo:
        o, r = repo.split("/", 1)
        return f"https://{o}.github.io/{r}"
    return "http://localhost:8000"


def ed_of(e):
    return e.get("ed", "legacy")  # 예전 버전에서 만든 에피소드


def ep_title(e):
    t = f"{e['date']} {ED_TITLES.get(ed_of(e), '뉴스 브리핑')}"
    return t + (f" ({e['part']}/{e['parts']})" if e["parts"] > 1 else "")


def txt_name(e):
    return f"{e['date']}_{e['ed']}.txt" if "ed" in e else f"{e['date']}.txt"


def drop_files(e):
    for n in (e["file"], txt_name(e)):
        (EP / n).unlink(missing_ok=True)


def publish(today, ed, parts, script_text):
    EP.mkdir(parents=True, exist_ok=True)
    ds, eid = today.strftime("%Y-%m-%d"), ed["id"]
    state = load_state()
    for e in [e for e in state if e["date"] == ds and ed_of(e) in (eid, "legacy")]:
        drop_files(e)  # 같은 날 같은 에디션(및 예전 버전)은 덮어쓰기
        state.remove(e)
    for i, b in enumerate(parts, 1):
        fn = f"{ds}_{eid}_{i}.mp3"
        (EP / fn).write_bytes(b)
        state.append({"date": ds, "ed": eid, "part": i, "parts": len(parts), "file": fn, "bytes": len(b)})
    (EP / f"{ds}_{eid}.txt").write_text(script_text, encoding="utf-8")

    keep = sorted({e["date"] for e in state}, reverse=True)[:KEEP_DAYS]
    for e in [e for e in state if e["date"] not in keep]:
        drop_files(e)
        state.remove(e)
    order = {x["id"]: i for i, x in enumerate(EDITIONS)}
    state.sort(key=lambda e: e["part"])
    state.sort(key=lambda e: order.get(ed_of(e), 9))
    state.sort(key=lambda e: e["date"], reverse=True)
    (OUT / "episodes.json").write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")
    write_feed(state)
    write_index(state)


def write_feed(state):
    base = site_url()
    items = []
    for k, e in enumerate(state):
        d = dt.datetime.strptime(e["date"], "%Y-%m-%d").replace(hour=6, tzinfo=KST)
        d += dt.timedelta(minutes=-k)
        items.append(
            f"<item><title>{escape(ep_title(e))}</title>"
            f"<description>{escape(e['date'] + ' 주요 뉴스 요약')}</description>"
            f"<pubDate>{format_datetime(d)}</pubDate><guid isPermaLink=\"false\">{e['file']}</guid>"
            f"<enclosure url=\"{base}/episodes/{e['file']}\" length=\"{e['bytes']}\" type=\"audio/mpeg\"/></item>"
        )
    xml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<rss version="2.0" xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd"><channel>'
        f"<title>{escape(TITLE)}</title><link>{base}/</link><language>ko</language>"
        "<description>매일 아침 주요 뉴스를 요약해 들려주는 AI 팟캐스트</description>"
        "<itunes:explicit>false</itunes:explicit>" + "".join(items) + "</channel></rss>"
    )
    (OUT / "feed.xml").write_text(xml, encoding="utf-8")


def write_index(state):
    rows = []
    for e in state:
        txt = ""
        if e["part"] == 1 and (EP / txt_name(e)).exists():
            body = escape((EP / txt_name(e)).read_text(encoding="utf-8"))
            txt = f"<details><summary>대본 보기</summary><pre>{body}</pre></details>"
        rows.append(
            f"<section><h3>{escape(ep_title(e))}</h3>"
            f'<audio controls preload="none" src="episodes/{e["file"]}"></audio>'
            f"<small> {e['bytes'] / 1e6:.1f}MB</small>{txt}</section>"
        )
    page = (
        '<!doctype html><html lang="ko"><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        f"<title>{escape(TITLE)}</title>"
        "<style>body{font-family:sans-serif;max-width:640px;margin:auto;padding:12px}"
        "audio{width:100%}pre{white-space:pre-wrap}section{border-bottom:1px solid #8884;padding:8px 0}</style>"
        f'<h2>{escape(TITLE)}</h2><p><a href="feed.xml">팟캐스트 구독 주소(feed.xml)</a></p>'
        + "".join(rows)
    )
    (OUT / "index.html").write_text(page, encoding="utf-8")


# ───────────── 실행 ─────────────
def build_edition(ed, today):
    sections = collect(ed)
    if not sections:
        print(f"[경고] '{ed['title']}' 뉴스 수집 실패")
        return False
    lines = parse_script(gemini(make_prompt(ed, sections, today)))
    if not lines:
        print("[안내] AI 없이 본문을 그대로 낭독하는 모드")
        lines = fallback_script(ed, sections, today)
    script_text = "\n".join(f"{s}: {t}" for s, t in lines)
    parts = asyncio.run(make_parts(lines, VOICES_EN if ed.get("lang") == "en" else VOICES))
    if not parts:
        print(f"[경고] '{ed['title']}' 음성 생성 실패")
        return False
    publish(today, ed, parts, script_text)
    print(f"완료 [{ed['id']}]: {len(lines)}줄, mp3 {len(parts)}개, {sum(map(len, parts)) / 1e6:.1f}MB")
    return True


def main():
    today = dt.datetime.now(KST)
    ok = [build_edition(ed, today) for ed in EDITIONS]
    if not any(ok):
        sys.exit("오늘 만든 방송이 없습니다. 위 경고 메시지를 확인하세요.")


if __name__ == "__main__":
    main()
