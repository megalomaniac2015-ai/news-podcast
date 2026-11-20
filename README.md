# 오늘의 뉴스 브리핑 (완전 무료 AI 뉴스 팟캐스트)

매일 아침 6시(KST)에 자동으로: 뉴스 RSS 수집 → AI 요약 대본 → TTS 음성(mp3) → 팟캐스트 피드.

| 단계 | 사용 서비스 | 비용 |
|---|---|---|
| 뉴스 수집 | 구글뉴스·연합뉴스 RSS | 0원 |
| 요약(AI) | Gemini API **무료 티어** (하루 1~3회 호출) | 0원 |
| 음성 | edge-tts (키·가입 불필요) | 0원 |
| 실행/호스팅 | GitHub Actions + Pages (공개 저장소) | 0원 |

## 설치 (한 번만, 약 10분)
1. https://aistudio.google.com/apikey 에서 API 키 발급 (카드 등록 없이 가능).
   - **⚠️ 이 프로젝트에 결제(Billing)를 절대 연결하지 마세요.** 결제를 연결하지 않으면 한도 초과 시 과금되지 않고 오류만 납니다. 오류가 나면 프로그램이 알아서 'AI 없는 헤드라인 낭독' 모드로 대체합니다.
2. GitHub에서 **Public** 저장소를 만들고 이 폴더의 파일을 모두 올립니다. (`.github` 폴더 포함)
3. 저장소 Settings → Secrets and variables → Actions → New repository secret
   이름 `GEMINI_API_KEY`, 값에 발급받은 키.
4. Actions 탭 → daily-news-podcast → **Run workflow** 로 첫 실행. (이후 매일 자동)
5. Settings → Pages → Source: *Deploy from a branch*, Branch: `gh-pages` / `(root)`.
6. 접속: `https://내아이디.github.io/저장소이름/`
   - 스마트폰 브라우저에서 바로 재생하거나,
   - 팟캐스트 앱(Pocket Casts, AntennaPod 등)에 `…/feed.xml` 주소를 구독 등록.

## PC에서 직접 실행
```
pip install -r requirements.txt
set GEMINI_API_KEY=키        (맥/리눅스: export GEMINI_API_KEY=키)
python news_podcast.py
```
결과는 `docs/` 폴더(mp3, feed.xml, index.html)에 생깁니다.

## 용량·옵션
- mp3 한 파일은 약 4MB 이하로 자동 분할(앱 멈춤 방지), 보통 8분 ≈ 3MB.
- 최근 14일치만 보관, 이전 것은 자동 삭제.
- 환경변수: `TARGET_CHARS`(분량), `VOICE_A`/`VOICE_B`(목소리), `PODCAST_TITLE`, `GEMINI_MODELS`(모델명이 바뀌면 여기만 수정).
- 뉴스 출처 추가/변경은 `news_podcast.py` 맨 위 `FEEDS` 목록 수정.

## 참고
- 공개 저장소이므로 요약 대본·음성이 공개됩니다. 개인 청취용으로 쓰세요.
- GitHub 예약 실행은 몇 분~수십 분 늦을 수 있습니다.
