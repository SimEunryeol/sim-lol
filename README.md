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
python -m sim_diamond.collect_me --recompute         # 지표 전체 재계산
python -m sim_diamond.collect_bench --tiers SILVER GOLD PLATINUM --per-tier 15 --per-player 10
python -m sim_diamond.report --out data/여기에.md
```

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
  report.py         마크다운 리포트 생성
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
| 오브젝트 참여 | 몬스터 처치 시점의 내 좌표가 없다 | 같은 선형보간. 단 처치자 본인·어시스트 기록자는 무조건 참여 |
| 첫 풀캠프 완료 | 캠프별 이벤트가 없다 | 정글 몬스터 처치 수가 20에 도달하는 시각을 프레임 사이 보간. 6캠프 전체가 두꺼비1+블루1+늑대3+칼날부리6+레드1+돌거북7 ≈ 20마리다 |
| 귀환 횟수·시각 | 귀환 이벤트가 없다 | 상점 구매를 20초 간격으로 묶어 기지 방문으로 본다. 직전 방문 이후 죽은 적이 있으면 부활로 돌아온 것이라 자발적 귀환에서 뺀다 (`back_count` = 총 방문, `voluntary_back_count` = 자발적 귀환) |

리포트에서 근사 항목은 표에 `_(추정)_` 로 표시된다. 나중에 리플레이 파일을 붙이면 와드 좌표는
실측으로 대체할 수 있다.

상수는 전부 `metrics.py` 상단에 모여 있다 (`WARD_LOOKBACK_MS`, `WARD_RADIUS`,
`OBJECTIVE_RADIUS`, `FULL_CLEAR_JUNGLE_CS`, `FIRST_GANK_AFTER_MS`, `LANE_ZONES`,
`BACK_CLUSTER_GAP_MS`). 리메이크 기준은 `config.REMAKE_MAX_S`.

**리메이크 제외**: 5분 미만으로 끝난 판은 리포트의 모든 집계에서 뺀다. 74초짜리 패배 한 판이
20판 표본의 승률을 5%p 흔들기 때문이다. 제외한 판수는 리포트 요약에 표시된다.

## 다음 단계

리포트 7번 "코치 메모" 섹션은 TODO 로 비어 있다. 2단계에서 Claude API 로 채운다.
