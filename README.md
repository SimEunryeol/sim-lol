# 심은렬 다이아만들기 — 1단계 (데이터 수집 + 첫 진단 리포트)

솔로랭크 전적을 Riot API 에서 긁어와 SQLite 한 파일(`data/sim.db`)에 쌓고,
같은 서버 실버/골드 표본과 비교한 마크다운 진단 리포트를 만든다.

## 설치

**Windows** — `scripts\setup.bat` 을 더블클릭하면 가상환경·패키지·`.env` 까지 한 번에 만든다.
직접 치려면:

```bat
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
copy .env.example .env
notepad .env
```

> **`.env` 와 `.env.example` 은 다른 파일이다.** `.env.example` 은 git 이 관리하는 빈
> 템플릿이라 여기에 진짜 키를 넣으면 `git pull` 이 막히고, 실수로 커밋하면 키가 공개된다.
> 키는 `.env` 에만 넣는다. (`python -m sim_diamond.doctor` 가 이 실수를 잡아준다.)
>
> 메모장으로 `.env` 를 저장할 때 파일 형식을 **"모든 파일"** 로 두어야 한다.
> 안 그러면 `.env.txt` 로 저장돼서 키를 못 읽는다. (그 경우 프로그램이 알아서 알려준다.)

**macOS / Linux**

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env      # RIOT_API_KEY 를 채운다
```

`.env`

| 키 | 설명 |
| --- | --- |
| `RIOT_API_KEY` | Riot Developer Portal 키. 개발용 키는 24시간마다 만료된다 |
| `RIOT_ID` / `RIOT_TAG` | 내 Riot ID (기본 `F360` / `KR1`) |
| `SIM_DB_PATH` | (선택) DB 경로. 기본 `data/sim.db` |
| `SIM_SEASON_START_KST` | (선택) 수집 시작 시각. 기본 `2026-01-01T00:00:00` KST |

## 실행

```bash
python -m sim_diamond.collect_me        # 1) 내 계정 + 매치 + 타임라인 + 숙련도
python -m sim_diamond.collect_bench     # 2) 벤치마크 표본 (기본 SILVER/GOLD 각 15명 × 10판)
python -m sim_diamond.report            # 3) data/report_YYYYMMDD.md
```

`scripts/` 에 같은 명령의 얇은 래퍼가 있다 — Windows 는 `.bat`(`setup.bat`, `collect_me.bat`,
`collect_bench.bat`, `report.bat`, `selftest.bat`, `doctor.bat`), macOS/Linux 는 `.sh`
(`run_all.sh` 로 한 번에).

자주 쓰는 인자

```bash
python -m sim_diamond.collect_me --limit 20          # 매치 수 제한(첫 시험용)
python -m sim_diamond.collect_me --include-normals   # 일반(400/430)도 함께 수집
python -m sim_diamond.collect_me --recompute         # 지표 전체 재계산
python -m sim_diamond.collect_bench --tiers BRONZE SILVER GOLD --per-tier 15 --per-player 10
python -m sim_diamond.report --out data/여기에.md
```

## 탐색 단계 (5개 라인 전부 돌려보기)

주 라인을 확정하지 않고 5개 라인을 각각 **기준 챔프 2개로 15판씩, 총 75판** 돌려보며
어디가 맞는지 데이터로 고른다.

```bat
REM 1) 라인별 기준 챔프를 정하고 단계를 만든다 (영문 championName)
python -m sim_diamond.phase init-explore ^
  --top Garen Malphite --jungle Nocturne Rammus --middle Ahri Annie ^
  --bottom Caitlyn Ashe --utility Thresh Leona

REM 2) 경기가 끝날 때마다 재미 점수를 남긴다 (1~5)
python -m sim_diamond.rate 4 "갱 각이 잘 보였다"

REM 3) 수집하고 현황을 본다
python -m sim_diamond.collect_me
python -m sim_diamond.explore
```

`explore` 가 보여주는 것

| 항목 | 뜻 |
| --- | --- |
| 진행 | 그 라인 판수 / 목표 15판 |
| 기준 챔프 외 | 정해둔 챔프가 아닌 걸로 한 판수(진행에서 제외) |
| 순서 위반 | 앞 라인이 목표를 못 채웠는데 뒤 라인을 한 판수(진행에는 포함) |
| 재미 | `rate` 로 남긴 점수 평균 (1~5) |
| 지표 | 낮은 티어 벤치 0%, 높은 티어 벤치 100% 축에서 내 위치 |
| 적합도 | 지표 70% + 재미 30% |

**지표를 왜 정규화하나**: 분당 CS(6.4)와 15분 골드 차이(-390)는 단위가 달라 그냥 더할 수
없다. BRONZE 벤치를 0, GOLD 벤치를 1로 두면 "브론즈에서 골드까지 얼마나 왔나"라는 이
프로젝트의 질문 그 자체가 축이 된다. 두 벤치가 사실상 같은 지표는 변별력이 없어 뺀다.
BRONZE 를 안 모았으면 IRON→SILVER 처럼 있는 티어로 축을 잡는다.

**탐색 순서는 탑 → 미드 → 원딜 → 서폿 → 정글로 고정**이다. 앞 라인이 15판을 채우기
전에 뒤 라인을 하면 "순서 위반"으로 표시된다(진행 판수에는 들어가되 따로 센다).
`explore` 와 코치 메모 둘 다 "지금 탐색 중인 라인"과 "그 라인 남은 판수"를 명시한다.

**75판을 채우기 전에는 라인을 결정하지 않는다.** 리포트와 `explore` 둘 다 남은 판수를
명시하고, 2단계 코치 메모도 이 규칙을 지키도록 시스템 프롬프트에 박아둔다.

`phase set-champs` 로 기준 챔프를 바꾸면 그 즉시 위반 집계가 다시 계산된다.

## 상태 공유 (복붙 없이)

```bat
scripts\send.bat
```

`doctor` + `check` + `explore` + git 상태를 `logs/status.txt` 하나로 모아 GitHub 에 올린다.
터미널 출력을 손으로 복사해 붙일 필요가 없다. **API 키로 보이는 문자열
(`RGAPI-…`, `sk-ant-…`, UUID)은 저장 전에 전부 가린다.**

## 잘 안 될 때: 진단부터

```bat
python -m sim_diamond.doctor        REM Windows: scripts\doctor.bat
```

`.env` 를 어디서 찾았는지, 키를 어떤 형식으로 읽었는지(전체는 절대 출력하지 않는다),
그리고 라이엇이 **실제로** 어떤 상태코드와 본문을 돌려주는지 그대로 보여준다.
`HTTP 401 / 403` 은 거의 항상 **개발용 키 만료**다 — 24시간마다 새로 발급받아야 한다.

## 수집한 데이터 점검

```bat
python -m sim_diamond.check              REM 요약 + 이상 징후
python -m sim_diamond.check --sample     REM 최근 한 경기 상세
```

지표가 통째로 비었는지(파싱 실패), 값이 물리적으로 불가능한지, 타임라인 데스 수와 매치 스탯의
데스 수가 어긋나는지 등을 점검한다. 이상 징후가 있으면 종료코드 1 을 낸다.

## Match-V5 `/replays` (2026-09 KR 확인)

문서에 없는 엔드포인트라 `collect_me` 가 후보 경로를 찔러본다. 실제 응답:

| 경로 | 결과 |
| --- | --- |
| `asia` `/lol/match/v5/matches/by-puuid/{puuid}/replays` | **200** |
| `asia` `/lol/match/v5/replays/by-puuid/{puuid}` | 403 |

```json
{"total": 5, "matchFileURLs": ["https://s3.ap-northeast-1.amazonaws.com/…/kr_8369802379/0.replay?X-Amz-Expires=3600&…"]}
```

- 서명된 S3 링크이고 **1시간 뒤 만료**된다. 쓰려면 받은 즉시 내려받아야 한다.
- **최근 5판만** 준다. 시즌 전체 소급은 안 된다.
- 내용은 `.replay`(ROFL) 파일이라 별도 파서가 필요하다. 다만 여기엔 와드의 **정확한 좌표**가
  들어 있어서, 지금 근사로 처리 중인 항목을 실측으로 바꿀 수 있는 유일한 경로다.

## 네트워크 없이 검증

합성 Riot 응답으로 파싱 → 지표 → 리포트 전 구간을 돌린다. API 키가 없어도 된다.

```bash
python tests/test_pipeline.py    # Windows: scripts\selftest.bat / macOS·Linux: scripts/selftest.sh
```

`data/report_SAMPLE.md` 가 이 합성 데이터로 만든 리포트다 (숫자는 전부 가짜).

## 구조

```
sim_diamond/
  config.py         .env 로딩, 라우팅/레이트리밋 상수
  riot_client.py    레이트리밋 + 429/5xx 재시도 + SQLite 캐시 클라이언트
  db.py             스키마 생성, upsert 헬퍼
  parse.py          raw JSON → matches/participants/frames/events
  geo.py            좌표 → 구역(탑/미드/봇/내정글/적정글/용둥지/바론둥지/강/기지)
  metrics.py        참가자 1명·1경기 지표 → participant_metrics, deaths_detail
  ddragon.py        챔피언/아이템 정적 데이터(ko_KR), 코어템 골드 판정
  collect_me.py     내 계정 수집 진입점
  collect_bench.py  벤치마크 표본 수집 진입점
  phase.py          탐색/집중 단계 관리 (phases 테이블)
  rate.py           경기별 재미 점수 기록 (self_rating 테이블)
  explore.py        탐색 진행 현황 · 적합도 계산
  report.py         마크다운 리포트 생성
  coach.py          코치 메모 생성 (Claude API)
  check.py          수집 데이터 점검
  doctor.py         .env / API 키 진단
data/               sim.db, 리포트
scripts/            셸 래퍼
tests/              합성 픽스처 + 전 구간 자체 검증
```

## 동작 원칙

- **라우팅**: Account-V1 / Match-V5 → `asia.api.riotgames.com`, Summoner-V4 / League-V4 /
  Champion-Mastery-V4 → `kr.api.riotgames.com`.
- **레이트리밋**: 20 req/1s, 100 req/2min 을 슬라이딩 윈도우로 지킨다
  (시계 오차 여유로 `config.RATE_SAFETY = 0.9` 를 곱해 실제로는 18/1s, 90/2min).
- **재시도**: 429 는 `Retry-After` 만큼 대기 후 재시도(최대 5회), 5xx 와 네트워크 오류는
  1→2→4초 지수 백오프로 3회.
- **캐시**: 모든 응답은 먼저 SQLite 에 저장된다. 매치/타임라인은 `matches_raw` /
  `timelines_raw`(영구), 나머지는 `api_cache`(엔드포인트별 TTL). 이미 있으면 네트워크를
  타지 않으므로 스크립트를 몇 번 다시 돌려도 안전하고, 중간에 끊겨도 이어서 진행된다.
- **재생성 가능**: 파싱 테이블과 지표는 raw 만 보고 언제든 다시 만들 수 있다.
  `parse.reparse_all(conn)` / `metrics.compute_all(conn, recompute=True)`.

벤치 비교 열은 **수집한 티어만큼 자동으로 생긴다**. 순서는 알파벳이 아니라 실력 순
(IRON→CHALLENGER)이므로 `--tiers` 를 아무 순서로 줘도 리포트는 낮은 티어부터 나온다.

## DB 스키마

| 테이블 | 내용 |
| --- | --- |
| `players` | puuid, 이름, 티어/랭크/LP, `is_me`, `is_bench` |
| `matches_raw` / `timelines_raw` | 원본 JSON (영구 보존) |
| `api_cache` | 그 외 모든 API 응답 캐시 |
| `matches` | match_id, 시작시각, 게임시간, 패치, 큐 |
| `participants` | 경기×소환사 기본 스탯, 룬, 최종 아이템, 소환사 주문 |
| `frames` | 분 단위 위치/CS/정글CS/골드/레벨/경험치 |
| `events` | 타임라인 이벤트 전부 (행위자·피해자·좌표·아이템·와드·몬스터 + `extra` JSON) |
| `participant_metrics` | 계산된 지표 (아래) |
| `deaths_detail` | 데스 1건 단위: 시각, 좌표, 구역, 와드 여부 |
| `champion_mastery` | 숙련도 top 20 |
| `phases` | 탐색/집중 단계: 단계명, 시작 시각, 라인, 기준 챔프(json), 목표 판수, 탐색 순서 |
| `self_rating` | 경기별 재미 점수(1~5)와 메모 |
| `coach_memo` | Claude API 로 생성한 코치 메모 |
| `static_data` | Data Dragon 캐시 |
| `collect_state` | 수집 진행 상태 |

## 계산되는 지표

기본 승패/KDA/판당 데스/분당 CS/10·15분 CS/15분 골드, 라인 상대와의 10·15분 CS·골드·레벨
차이, 분당 시야점수·컨트롤 와드 구매 수·첫 컨트롤 와드 시각, 데스별 시각/좌표/구역/와드
여부와 15분 이전 데스, 오브젝트(용·전령·바론·유충) 반경 2500 참여율, 정글 전용(첫 풀캠프,
첫 갱, 첫 카정 피해, 5·10분 상대 정글과의 골드·레벨 차, 적정글 체류 분), 첫 코어템 시각,
귀환 횟수·시각.

**첫 갱**은 3분 이후 라인 구역(탑/미드/봇)에서 내가 딴 첫 킬/어시다. 정글·강에서 난 교전과
내 데스는 세지 않는다. 내 정글에서 죽은 첫 시각은 **첫 카정 피해**(`first_counter_jungled_min`)
로 따로 잡으며, 초반 인베이드를 놓치지 않으려고 시간 하한을 두지 않았다.

### 근사가 들어가는 항목 (중요)

Riot 타임라인이 주지 않는 정보라 추정한 값이다. 리포트 하단에도 같은 주석이 붙는다.

| 항목 | 왜 근사인가 | 어떻게 |
| --- | --- | --- |
| 데스 시 아군 와드 유무 | `WARD_PLACED` 이벤트에 **좌표가 없다** | 설치자의 그 시각 위치로 대체. 위치는 분 단위 프레임 + 킬 이벤트의 정확 좌표를 선형보간 |
| 오브젝트 참여 | 몬스터 처치 시점의 내 좌표가 없다 | 처치 시각 ±30초 안에서 가장 가까웠던 거리로 판정. 기본 반경 **1200**(2500은 "근처에 있었다"가 "참여했다"로 뭉개진다). 리포트에 r800/r2500 도 함께 표시. 처치자 본인·어시스트 기록자는 무조건 참여 |
| 첫 풀캠프 완료 | 캠프별 이벤트가 없다 | 정글 몬스터 처치 수가 20에 도달하는 시각을 프레임 사이 보간. 6캠프 전체가 두꺼비1+블루1+늑대3+칼날부리6+레드1+돌거북7 ≈ 20마리다 |
| 귀환 횟수·시각 | 귀환 이벤트가 없다 | 상점 구매를 20초 간격으로 묶어 기지 방문으로 본다. 직전 방문 이후 죽은 적이 있으면 부활로 돌아온 것이라 자발적 귀환에서 뺀다 (`back_count` = 총 방문, `voluntary_back_count` = 자발적 귀환) |

리포트에서 근사 항목은 표에 `_(추정)_` 로 표시된다. 나중에 리플레이 파일을 붙이면 와드 좌표는
실측으로 대체할 수 있다.

상수는 전부 `metrics.py` 상단에 모여 있다 (`WARD_LOOKBACK_MS`, `WARD_RADIUS`,
`OBJECTIVE_RADIUS`, `FULL_CLEAR_JUNGLE_CS`, `FIRST_GANK_AFTER_MS`, `LANE_ZONES`,
`BACK_CLUSTER_GAP_MS`). 리메이크 기준은 `config.REMAKE_MAX_S`.

**리메이크 제외**: 5분 미만으로 끝난 판은 리포트의 모든 집계에서 뺀다. 74초짜리 패배 한 판이
20판 표본의 승률을 5%p 흔들기 때문이다. 제외한 판수는 리포트 요약에 표시된다.

## 코치 메모 (2단계)

리포트 7번 섹션을 Claude API 로 채운다. `.env` 에 `ANTHROPIC_API_KEY` 가 필요하다.

```bat
python -m sim_diamond.coach --dry-run   REM 프롬프트만 확인 (API 호출 없음, 무료)
python -m sim_diamond.coach             REM 생성 → DB 저장 → 리포트 갱신
python -m sim_diamond.coach --show      REM 저장된 최신 메모 보기
```

모델은 `claude-opus-5`, adaptive thinking + effort high. 리포트 전문을 입력으로 준다.
생성된 메모는 `coach_memo` 테이블에 남고, 이후 `report` 를 다시 돌려도 7번 섹션에 실린다.

시스템 프롬프트에 박아둔 규칙:

1. **탐색 75판이 끝나기 전에는 어떤 라인도 추천하지 않는다.** 남은 판수를 명시하고
   라인 판단은 유보한다고 쓰게 한다. 지금 탐색 중인 라인과 그 라인 남은 판수도 필수다.
2. 표본 15판 미만(⚠) 라인으로 결론 내지 않는다.
3. `_(추정)_` 지표는 추정임을 밝히고, 절대 수치보다 벤치와의 차이를 근거로 삼는다.
4. 모든 주장에 리포트의 실제 숫자를 붙이고, 없는 수치를 지어내지 않는다.

출력은 한 줄 요약 / 탐색 단계 진행 현황 / **이번 주 과제(딱 1개, 측정 가능한 목표치 포함)** /
우선순위 진단 3개(2·3번은 "다음 주 이후 후보"로 명시) / 주의 순서이고, **900자 이내**다.
전부 존댓말로 쓰게 하며 시스템 프롬프트에 좋은 예/나쁜 예를 넣어 고정했다.

## 다음 단계

와드 좌표 실측(리플레이 파싱), 탐색 75판 완료 후 라인 결정, 결정된 라인의 집중 단계.
