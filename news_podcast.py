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
TITLE = os.environ.get("PODCAST_TITLE", "동남아 뉴스 브리핑")
VOICES = {
    "A": os.environ.get("VOICE_A", "ko-KR-SunHiNeural"),
    "B": os.environ.get("VOICE_B", "ko-KR-InJoonNeural"),
}
RATE = os.environ.get("TTS_RATE", "+5%")
TARGET_CHARS = int(os.environ.get("TARGET_CHARS", "4000"))  # 약 11~12분 분량
MAX_PART_BYTES = int(os.environ.get("MAX_PART_BYTES", str(4_000_000)))  # 파일 1개 최대 ~4MB
KEEP_DAYS = int(os.environ.get("KEEP_DAYS", "14"))
GEMINI_MODELS = os.environ.get("GEMINI_MODELS", "gemini-2.5-flash,gemini-2.5-flash-lite")

def gsearch(q, lang="ko"):
    tail = "hl=ko&gl=KR&ceid=KR:ko" if lang == "ko" else "hl=en-US&gl=US&ceid=US:en"
    return f"https://news.google.com/rss/search?q={quote('(' + q + ') when:2d')}&{tail}"


HAS_KEY = bool(os.environ.get("GEMINI_API_KEY", "").strip())
BODY_CHARS = int(os.environ.get("BODY_CHARS", "1500"))  # 기사 1건당 본문 발췌 길이
FETCH_BUDGET = int(os.environ.get("FETCH_BUDGET", "300"))  # 본문 수집 제한시간(초)
FEEDS = [  # (섹션 이름, RSS 주소, 최대 기사 수, 본문까지 읽을 기사 수, 영문(AI 번역 필요) 여부)
    ("인도네시아", gsearch("인도네시아 OR 자카르타 OR 발리"), 10, 8, False),
    ("동남아", gsearch("동남아 OR 베트남 OR 태국 OR 필리핀 OR 말레이시아 OR 싱가포르"), 8, 4, False),
    ("인도네시아 영문 보도", gsearch("Indonesia", "en"), 6, 5, True),
    ("동남아 영문 보도", gsearch("Southeast Asia", "en"), 4, 3, True),
]
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


def collect():
    seen, sections = set(), []
    deadline = time.time() + FETCH_BUDGET
    for name, url, n, deep, ai_only in FEEDS:
        if ai_only and not HAS_KEY:
            print(f"[안내] '{name}'은 AI 번역이 필요해 건너뜀 (GEMINI_API_KEY 없음)")
            continue
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
            print(f"[수집] {name}: {len(items)}건, 본문 {sum(1 for i in items if i[2])}건")
            sections.append((name, items))
    return sections


# ───────────── 2. 대본 만들기 ─────────────
def make_prompt(sections, today):
    date = f"{today.year}년 {today.month}월 {today.day}일 {WEEK[today.weekday()]}요일"
    news = "\n".join(
        f"[{name}]\n" + "\n".join(f"- {t}" + (f"\n  본문 발췌: {b}" if b else "") for t, _, b in items)
        for name, items in sections
    )
    return f"""너는 한국어 뉴스 팟캐스트 '{TITLE}'의 작가다. 아래 [오늘의 뉴스]만을 근거로 진행자 두 명(A, B)의 대화형 대본을 써라.
규칙:
- 오늘 날짜는 {date}. 첫 대사는 인사와 날짜 소개.
- 분량은 한국어 약 {TARGET_CHARS}자(낭독 7~8분).
- 중요한 이슈 6~8개를 골라 각각 4~6문장으로 배경까지 풀어 설명하고, 비슷한 기사는 하나로 묶는다. 인도네시아 소식을 가장 비중 있게 다룬다.
- '본문 발췌'가 있는 기사는 그 내용을 이해해 자기 말로 다시 설명한다(문장을 그대로 옮기지 않는다). 영문 헤드라인·본문은 자연스러운 한국어로 번역해 '현지 보도에 따르면'처럼 소개한다.
- 제공된 헤드라인·본문에 없는 사실·수치·인용을 지어내지 않는다. 불확실하면 '보도에 따르면'처럼 표현한다.
- 모든 줄은 반드시 'A: 내용' 또는 'B: 내용' 형식. 마크다운, 이모지, 괄호 지문, 제목은 금지.
- 마지막은 짧은 마무리 인사.

[오늘의 뉴스]
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


def fallback_script(sections, today):
    """AI를 못 쓸 때: 헤드라인을 두 진행자가 번갈아 읽는다."""
    lines = [
        ("A", f"안녕하세요, {TITLE}입니다. {today.month}월 {today.day}일 {WEEK[today.weekday()]}요일, 주요 소식을 전해드립니다."),
    ]
    flip = 0
    for name, items in sections:
        lines.append(("B" if flip else "A", f"{name} 소식입니다."))
        for t, _, b in items[:4]:
            flip ^= 1
            lines.append(("B" if flip else "A", t + "."))
            if b:  # AI가 없을 때는 본문 앞부분 두 문장만 덧붙임
                flip ^= 1
                lines.append(("B" if flip else "A", " ".join(re.split(r"(?<=[.다])\s+", b)[:2])))
    lines.append(("B", "오늘 브리핑은 여기까지입니다. 감사합니다."))
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


async def make_parts(lines):
    parts, cur = [], bytearray()
    for spk, text in lines:
        data = await synth(text, VOICES[spk])
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


def ep_title(e):
    t = f"{e['date']} {TITLE}"
    return t + (f" ({e['part']}/{e['parts']})" if e["parts"] > 1 else "")


def publish(today, parts, script_text):
    EP.mkdir(parents=True, exist_ok=True)
    ds = today.strftime("%Y-%m-%d")
    state = [e for e in load_state() if e["date"] != ds]
    for p in EP.glob(f"{ds}_*"):
        p.unlink()
    for i, b in enumerate(parts, 1):
        fn = f"{ds}_{i}.mp3"
        (EP / fn).write_bytes(b)
        state.append({"date": ds, "part": i, "parts": len(parts), "file": fn, "bytes": len(b)})
    (EP / f"{ds}.txt").write_text(script_text, encoding="utf-8")

    keep = sorted({e["date"] for e in state}, reverse=True)[:KEEP_DAYS]
    for d in {e["date"] for e in state} - set(keep):
        for p in list(EP.glob(f"{d}_*")) + list(EP.glob(f"{d}.txt")):
            p.unlink()
    state = [e for e in state if e["date"] in keep]
    state.sort(key=lambda e: e["part"])
    state.sort(key=lambda e: e["date"], reverse=True)
    (OUT / "episodes.json").write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")
    write_feed(state)
    write_index(state)


def write_feed(state):
    base = site_url()
    items = []
    for e in state:
        d = dt.datetime.strptime(e["date"], "%Y-%m-%d").replace(hour=6, tzinfo=KST)
        d += dt.timedelta(minutes=e["parts"] - e["part"])
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
        if e["part"] == 1:
            f = EP / f"{e['date']}.txt"
            if f.exists():
                txt = f"<details><summary>대본 보기</summary><pre>{escape(f.read_text(encoding='utf-8'))}</pre></details>"
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
def main():
    today = dt.datetime.now(KST)
    sections = collect()
    if not sections:
        sys.exit("뉴스 수집에 실패했습니다 (네트워크/RSS 확인).")
    text = gemini(make_prompt(sections, today))
    lines = parse_script(text)
    if not lines:
        print("[안내] 대체 대본(헤드라인 낭독) 사용")
        lines = fallback_script(sections, today)
    script_text = "\n".join(f"{s}: {t}" for s, t in lines)
    parts = asyncio.run(make_parts(lines))
    if not parts:
        sys.exit("음성 생성에 실패했습니다.")
    publish(today, parts, script_text)
    print(f"완료: {len(lines)}줄, mp3 {len(parts)}개, {sum(map(len, parts)) / 1e6:.1f}MB → {OUT}/")


if __name__ == "__main__":
    main()
