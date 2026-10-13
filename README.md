# Codex Usage Monitor

각 PC의 로컬 수집기가 **세션별 토큰 집계와 공유 계정 전체 사용량만** 중앙 서버로 보내고, 서버가 계속 보존·표시하는 경량 대시보드입니다. 외부 Python 패키지나 OpenAI API 키가 필요하지 않습니다.

## 수집하는 정보

- SHA-256으로 익명화한 세션 및 프로젝트 식별자
- 모델명
- 누적 `tokens_used`
- 생성·마지막 갱신 시각과 보관 여부
- Codex app-server가 반환하는 계정 전체 누적·일별 토큰 사용량
- 현재 사용 한도의 사용률·윈도우·초기화 시각
- 수집기 설치 이후 공유 계정으로 연속 확인된 구간의 세션별 분 단위 토큰 합계

세션 제목, 프롬프트, 응답, 미리보기, 파일 경로, 작업 경로와 원본 세션 ID는 전송하거나 저장하지 않습니다. 원본 Codex DB는 읽기 전용으로 엽니다. app-server 조회는 원본 프로필을 변경하거나 잠그지 않도록 `auth.json`과 설치 식별자만 권한이 제한된 임시 디렉터리에 복사한 뒤 계정 집계 전용 메서드만 호출합니다.

세션 기간 집계는 rollout JSONL에서 `event_msg/token_count`의 시각과 `last_token_usage.total_tokens`와 중복 확인용 `total_token_usage.total_tokens`만 로컬에서 읽습니다. 다른 이벤트의 대화 내용은 사용하거나 전송하지 않습니다. 최초 설치 전 기록, 2분을 넘는 수집 중단 구간과 계정 전환 경계는 공유 계정에 확정 귀속하지 않습니다.

계정 전체 누적값은 공유 계정 기준의 대표 수치이고, 세션별 수치는 각 서버·사용자·프로젝트에 귀속된 상세 수치입니다. 여러 PC가 동일한 공유 계정 전체값을 보내더라도 서버는 계정 키별 최신 스냅샷 하나만 저장하므로 중복 합산하지 않습니다.

대시보드는 **주간 사용량 / 월별 사용량 / 전체 누적 / 수집 상태·세션** 탭으로 나뉩니다. 주간·월별 날짜 분석은 계정 일별 기록과 같은 UTC 날짜를 사용합니다. 월 선택은 주간이나 전체 누적값에 영향을 주지 않습니다.

주간·월별 추이는 사용자별 누적 막대로 표시하며, 날짜 위에 마우스를 올리면 사용자별 정확한 토큰과 비중을 툴팁·도넛·표로 확인합니다. 날짜 클릭/날짜 선택 상자, 방향키·Enter도 지원합니다. 선택 날짜·월·주중/주말 필터는 자동 갱신 중 유지됩니다. 사용자의 색상은 노드 키에 고정됩니다. 계정 보고량이 사용자량보다 작거나 없으면 미귀속량을 계산하지 않고 **사용자 확인량 내 비중**이라고 명시합니다. 기록 없는 날은 점선과 `기록 없음`으로, 명시적으로 보고된 0과 구분합니다.

주간 추이 오른쪽 도넛·표는 기본적으로 **주간 전체**를 표시하며, 아래 주간 구성과 동일한 사용자량·미귀속량·비중을 사용합니다. 날짜 선택 상자에서 특정 날짜로 전환할 수 있습니다. 전체 계정량이 확인되지 않으면 주간 비중은 **계산 대기**로 표시합니다. 주간 구성이 일별 기록 대신 관측 증가분을 사용하는 경우, 날짜별 그래프도 해당 UTC 날짜 안의 계정 누적값 증가분을 최소량으로 반영합니다. 주간 경계일과 진행 중인 오늘도 포함하되, 자정을 넘는 관측 공백의 증가분은 특정 날짜에 임의로 배분하지 않습니다. 날짜를 확인하지 못한 사용자량은 날짜별 미귀속에 포함될 수 있으며, 주간 전체에서는 해당 사용자에게 귀속됩니다. 월별 집계에는 이 관측 증가분을 더하지 않습니다.

월 합계는 해당 월의 **보고된 UTC 날짜별 계정량 합계**이며 반영 날짜 수를 함께 표시합니다. 누락 날짜·진행 중인 오늘·아직 오지 않은 날짜를 완전한 월 집계로 표시하지 않습니다. 여러 날에 걸친 세션 누적값은 날짜별로 임의 분배하지 않습니다. 생성과 마지막 활동이 같은 UTC 날짜인 세션은 그날의 근거로 사용하고, 같은 세션의 이벤트 합계와는 큰 값만 반영합니다. 주간 확인량 중 날짜를 배정하지 못한 양은 별도로 안내합니다.

**계정 한도와 주간 구성**의 사용률 그래프는 현재 10,080분 한도 윈도우를 표시합니다. 마우스 위치에 해당하는 UTC 날짜의 사용자 토큰도 확인할 수 있지만, 사용률(%)을 사용자별 토큰으로 환산하지는 않습니다. 서버가 `resets_at` 변경을 감지하면 이전 주기 샘플을 화면에서 제외하고 새 윈도우 시작점 0%부터 다시 그립니다. 한도 샘플은 장애 진단을 위해 최대 14일만 보관됩니다.

같은 주간 그래프 아래 **사용자별 토큰 구성**은 계정 일별 기록에서 이번 주간 창에 완전히 들어가는 날짜의 토큰을 합산해 **최소 주간 사용량**을 표시합니다. 일별 날짜는 UTC 날짜로 비교하며 리셋 경계일·진행 중인 오늘·누락된 날짜를 임의로 채우지 않습니다. 서버가 주중에 재시작해도 이미 보고된 일별 기록은 포함됩니다. 일별 기록이 없으면 계정 관측 이후 증가분을 최소량으로 사용하며, 사용자 집계 기간은 주간으로 유지합니다. 계정량이 부족하거나 사용자량보다 작으면 미추적량을 **확인 불가**로 표시하고 비중 막대를 그리지 않습니다. 사용 인원은 토큰이 확인된 서버·OS 사용자 조합 수입니다.

사용자별 주간량은 **세션별로** 다음 두 근거의 큰 값을 사용합니다. 같은 사용량을 두 번 더하지 않습니다.

- 주간 창 안에 완전히 들어가는 확정 분 단위 이벤트 합계. 진행 중인 분과 리셋 경계에 걸친 분은 제외합니다.
- 세션 기준 확인량: 이번 주에 생성된 세션은 수집기가 해당 계정에 귀속한 누적량, 오래된 세션은 주간 내 최초 관측값 이후 증가분만 사용합니다. 주간 시작 시각에 기준값이 있으면 그 값 이후 증가분을 사용합니다. 리셋 이전 기준값으로 주간량을 부풀리지 않습니다.

신규 사용자에게 이벤트 기록이 아직 없어도 이번 주 세션 사용량은 주간 집계에 포함됩니다. 보관 세션도 주간 합계에 포함하며, 하단 표의 기본 누적량은 보관 제외, 함께 표시하는 사용자 주간량은 보관 포함입니다. 미추적량은 계정 최소량에서 확인된 사용자량을 뺀 값으로, 미설치 사용자뿐 아니라 기존 사용자의 미확인 구간도 포함될 수 있어 실제 미등록 사용자의 정확한 사용량은 아닙니다. 이벤트량과 세션 기준 보완량을 화면에서 별도로 표시합니다.

**수집 상태 → 수집 진단**의 기존 주중·주말 그래프는 최근 14일의 서버 현지 날짜를 월–금과 토–일로 나눠 표시합니다. 주간·월별 UTC 분석과 구분된 진단용입니다. 세션 이벤트는 중복 보고해도 한 번만 집계합니다. 중앙 서버의 분 단위 이벤트 보관 기간은 월별 비교를 위해 **93일**로 확대했습니다. 기존 15일 보관 정책으로 이미 삭제된 이력은 복원되지 않으며, 수집기 설치 전·중단 구간도 완전한 데이터로 간주하지 않습니다.

상단 **TOTAL PROCESSED**는 세션 합계가 아니라 app-server가 보고한 계정 전체 `lifetime_tokens`입니다. 계정의 실제 전체 누적 처리량이므로 주간 한도가 초기화되어도 이 숫자는 초기화되지 않습니다. 주간 `resets_at` 변경에 따른 초기화는 **주간 사용량 추이와 권장 속도**에만 적용됩니다.

**계정 전체값과 세션 귀속 대조**는 관측 구간의 계정 증가량과 세션 귀속량을 비교합니다. 이는 일별 기록을 반영하는 주간 전체 구성과 기간이 다를 수 있습니다. **사용자별 주간 사용 비중**과 **사용자별 주간 사용 비중 비교**는 같은 사용자량·계정량을 사용하며 미추적 행도 표시합니다. 사용자별 비중(%)을 주요 값으로, 토큰 수를 보조 값으로 표시합니다. 비중은 사용자 주간 확인량 ÷ 계정 주간 집계량 × 100으로 계산하며, 사용자 합계를 100%로 정규화하지 않습니다. 계정 집계량이 부족하거나 0이면 비중은 **계산 대기**로 표시합니다. 최소 계정량 대비 비중은 확인된 사용자량의 비중이며 실제 전체 주간 점유율과 다를 수 있습니다. 주간 구성·사용자 비교·하단 표·알림은 같은 주간 귀속 함수를 사용합니다. 정확한 사용 날짜를 알 수 없는 세션 보완량은 주중·주말 일별 그래프에 임의로 배분하지 않습니다.

### 집계 모듈과 검증

- `usage_aggregation.py`: `UsageWindow`로 기간을 표현하고 `UsageRepository`에서 읽기 전용 조회를 수행합니다. `attribute_sessions`, `aggregate_users`, `build_weekly_breakdown`은 HTTP와 분리된 계산 함수입니다.
- `usage_analytics.py`: UTC 달력 월/날짜별 귀속, 월 합계, 보고 범위와 누락 상태를 계산합니다.
- `server.py`: 수집·저장·HTTP 처리와 모듈 호출을 담당합니다. 한 대시보드 응답의 주간 귀속 결과를 재사용합니다.
- `static/usage-view.js`: 사용자·프로젝트별 화면 데이터 그룹화를 DOM과 분리합니다. `static/app.js`는 표시를 담당합니다.
- `static/analytics-view.js`: 공통 날짜 그래프·도넛·상세 표와 마우스/키보드 상호작용을 구현합니다. `segments`, `chartMarkup`, `donutMarkup`은 DOM 없이 테스트할 수 있습니다.
- `make test`: 신규 사용자 이벤트 누락, 보관 세션, 중복 보고, 주간 경계, 기존 세션 누적량 제외, 다른 노드의 같은 세션 ID, 계정 집계 지연과 화면 표시를 검증합니다.

주간 그래프의 점선은 주간 한도를 하루 약 14.3%씩 균등하게 쓰는 권장선입니다. 실제 사용률과 권장선의 차이로 빠름·권장 범위·여유를 판정하고, 현재 속도가 유지될 때의 주기 종료 예상 사용률과 남은 기간의 일일 권장량을 함께 표시합니다.

### 사용 속도 알림

계정의 실제 주간 사용률이 현재 경과 시간 권장선의 150% 이상이 되면, 서버는 확인된 주간 세션 사용량이 가장 많은 수집 노드에 경고를 전달합니다. 같은 주간 창에서는 사용률 10% 구간마다 한 번만 보냅니다. Windows는 해당 사용자에게 `msg.exe` 알림을 표시하고, Linux 데스크톱은 `notify-send`를 사용합니다. GUI 알림을 사용할 수 없는 서버 환경에서도 수집기 로그에는 경고가 남습니다.

### 수집기 자동 업데이트

0.6.1은 지연된 계정 보고의 그래프 반영, 일부 계정 조회 실패 시 정상값 유실, 로컬 수집기와 화면의 기준값 불일치, 여러 장비의 동일 세션 기준값 충돌, 30초 안의 계정 증가량 누락을 수정합니다. 기존 관측 이력은 노드별 키를 추가하는 DB 전환으로 보존합니다. 반복 토큰 이벤트는 누적값으로 중복을 확인하고, 업데이트 검증에 실패하면 현재 파일과 기존 백업을 유지합니다.

0.6.0부터 수집기는 매 보고 응답에서 중앙 서버 버전을 확인합니다. 새 버전이 있으면 서버가 제공한 Python 파일의 응답 HMAC, SHA-256과 Python 구문을 모두 검증한 뒤 기존 파일을 `.previous`로 보존하고 원자적으로 교체합니다. 검증이나 교체에 실패하면 기존 실행 파일을 복구합니다. Linux 사용자 서비스, Windows 사용자 작업과 Windows SYSTEM 설치판을 지원합니다.

0.5.x 이하 수집기는 서버 응답을 읽는 기능이 없으므로 **0.6.0 이상 설치 파일을 한 번 직접 덮어 설치해야 합니다.** 이후 코드 업데이트는 자동 적용됩니다. Python 런타임, 작업 스케줄러나 설치 권한 구조가 바뀌는 버전은 전체 설치 파일을 다시 배포해야 할 수 있습니다.

대시보드의 계정 카드에서 연필 아이콘을 누르면 서버·OS 사용자 조합의 표시 이름을 변경할 수 있습니다. 실제 OS 사용자명은 식별용으로 유지되며, 표시 이름은 중앙 DB에 별도로 저장되므로 이후 수집 결과가 들어와도 유지됩니다. 빈 이름으로 저장하면 OS 사용자명으로 초기화됩니다.

## 실행

기본 실행은 모든 네트워크 인터페이스의 `8765` 포트에 바인딩합니다.

```bash
python3 server.py
```

익명 ID용 솔트를 지정하려면:

```bash
CODEX_MONITOR_HASH_SALT='충분히-긴-임의의-문자열' \
python3 server.py
```

브라우저에서 `http://서버주소:8765`로 접속합니다. 방화벽이나 리버스 프록시를 거치지 않는 상태에서는 신뢰하는 내부망에서만 `0.0.0.0` 바인딩을 사용하세요.

대시보드는 HTTP Basic 인증으로 보호됩니다. 기본 사용자명은 `admin`이고 자동 생성된 비밀번호는 서버의 `data/dashboard.password`에 권한 `0600`으로 저장됩니다.

```bash
cat data/dashboard.password
```

HTTP Basic 인증은 전송 내용을 암호화하지 않으므로 신뢰하지 않는 네트워크에 공개할 때는 HTTPS 리버스 프록시를 추가하세요.

로컬에서만 접속하도록 되돌리려면 `python3 server.py --host 127.0.0.1`을 사용합니다.

## systemd 서비스와 UFW

sudo 권한 없이 현재 사용자 서비스로 등록:

```bash
mkdir -p ~/.config/systemd/user
cp deploy/codex-usage-monitor.user.service ~/.config/systemd/user/codex-usage-monitor.service
systemctl --user daemon-reload
systemctl --user enable --now codex-usage-monitor.service
```

로그아웃 이후에도 실행하고 부팅 시 로그인 없이 시작하려면 `loginctl enable-linger "$USER"`를 한 번 실행합니다(환경에 따라 관리자 승인이 필요합니다).

서비스는 프로세스가 정상/비정상 종료되면 3초 후 자동 재시작하며, 반복 실패로 재시도를 중단하지 않습니다. 지속적인 설정 오류도 계속 재시도하므로 아래 로그로 원인을 확인하세요. `systemctl --user stop codex-usage-monitor.service`로 명시적으로 중지한 경우에는 다시 실행하지 않습니다. 프로세스가 살아 있지만 응답하지 않는 상태는 자동 재시작 대상이 아닙니다. 로그는 버퍼링 없이 즉시 journal에 기록됩니다.

사용자 서비스 상태·종료 이력 확인 및 수동 재시작:

```bash
systemctl --user status codex-usage-monitor.service
journalctl --user -u codex-usage-monitor.service --since '24 hours ago' --no-pager
systemctl --user restart codex-usage-monitor.service
```

서비스 파일을 수정하거나 새 버전으로 복사한 뒤에는 `systemctl --user daemon-reload`와 `systemctl --user restart codex-usage-monitor.service`를 실행해야 적용됩니다. journal 보관 기간 이전의 종료 이력이나 서비스 외부에서 수동 실행한 프로세스의 종료 원인은 이 로그만으로 확인할 수 없습니다.

시스템 서비스로 등록하려면:

```bash
sudo cp deploy/codex-usage-monitor.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now codex-usage-monitor.service
sudo ufw allow 8765/tcp comment 'Codex Usage Monitor'
```

상태 확인:

```bash
systemctl status codex-usage-monitor.service
sudo ufw status
```

## 옵션

```text
--source-db PATH     Codex state_5.sqlite 위치
--monitor-db PATH    추이 샘플 저장 DB 위치
--interval SECONDS   수집 주기(기본 10초)
--active-window SEC  활성 세션 판정 시간(기본 300초)
```

환경변수 `CODEX_STATE_DB`, `CODEX_MONITOR_DB`, `CODEX_MONITOR_HOST`, `CODEX_MONITOR_PORT`, `CODEX_MONITOR_INTERVAL`도 사용할 수 있습니다.

## 다른 서버 수집기 설치

수집기는 root 권한 없이 설치한 OS 사용자의 `~/.codex`만 읽습니다. 중앙 서버에서 `collector.py`, `deploy/codex-usage-collector.service`, `deploy/install-collector.sh`와 `data/collector.token`을 대상 서버 사용자에게 안전하게 복사합니다. 수집 키는 인증 정보이므로 저장 권한을 `0600`으로 유지하세요.

대상 서버에서:

```bash
install -Dm600 collector.token ~/.config/codex-usage-collector.token
printf '%s\n' 'CODEX_MONITOR_SERVER=http://143.248.136.95:8765' > ~/.config/codex-usage-collector.env
chmod 600 ~/.config/codex-usage-collector.env
./deploy/install-collector.sh
```

한 번만 연결 시험:

```bash
python3 collector.py \
  --server http://143.248.136.95:8765 \
  --token-file ~/.config/codex-usage-collector.token \
  --once
```

수집 항목은 서버명, OS 사용자명, 계정 표시 이름·이메일·플랜, 프로젝트 폴더명, 모델, 세션별 토큰 수와 시각, 계정 전체 누적·일별 토큰, 사용 한도입니다. 인증 토큰, 대화 제목·본문, 전체 경로와 원본 세션 ID는 전송하지 않습니다.

Codex가 설치된 환경에서는 수집기가 `codex app-server`의 `account/read`, `account/usage/read`, `account/rateLimits/read`를 30초 간격으로 조회합니다. 실행 파일이나 계정 사용량 조회가 일시적으로 실패해도 기존 로컬 세션 집계는 중단되지 않으며, 서버에는 마지막으로 성공한 계정 전체 스냅샷이 유지됩니다. 공유 계정에서 로그아웃된 동안에는 전체값이 갱신되지 않고 마지막 동기화 시각으로 상태를 확인할 수 있습니다.

배포 패키지에는 중앙 서버에서 현재 로그인한 공유 Codex 계정의 익명 계정 키가 포함됩니다. 각 수집기는 이 키와 일치하는 계정만 전송하며, 중앙 서버도 다른 계정의 수집 요청을 다시 거부합니다. 따라서 같은 PC에 다른 Codex 계정이 있어도 이 대시보드에는 등록되지 않습니다.

수집기는 세션 본문이나 원본 ID 대신 세션별 마지막 토큰 수와 공유 계정에 귀속된 누적 토큰 수만 로컬 `collector-state.json`에 기록합니다. Codex 로그인을 다른 계정으로 전환해도 기존 공유 계정 집계는 중앙 서버에 유지됩니다. 매 수집 시점의 토큰 증가분은 그때 로그인된 계정에 귀속되므로, 다른 계정으로 사용한 구간은 제외되고 공유 계정으로 다시 로그인한 뒤 사용한 증가분은 같은 세션이라도 계속 누적됩니다.

Codex 내부 DB에는 세션별 계정 ID가 없으므로 수집기 설치 전의 혼합 계정 이력을 완벽히 소급 분리할 수는 없습니다. 첫 수집 당시 마지막 로그인보다 오래된 기존 기록은 초기값에서 제외하고, 이후 30초 간격으로 관찰되는 증가량부터 정확히 구분합니다. 2분을 넘는 수집 중단 구간과 시계가 뒤로 이동한 구간은 계정을 확정할 수 없으므로 누적 귀속 증가량에서도 제외합니다. 계정 전환 중 처음 발견한 세션은 해당 로그인 이후 생성된 경우에만 초기량을 귀속합니다.

### Linux 단일 설치 패키지

중앙 서버에서 인증키가 포함된 사용자용 패키지를 생성합니다.

```bash
make bundle
```

생성 파일:

```text
dist/codex-usage-collector-0.6.1-linux-user-provisioned.tar.gz
```

이 파일 하나를 대상 서버에 안전하게 전송한 뒤 해당 사용자로 설치합니다.

```bash
tar -xzf codex-usage-collector-0.6.1-linux-user-provisioned.tar.gz
cd codex-usage-collector-0.6.1
sha256sum -c SHA256SUMS
./install.sh
```

root 권한은 필요하지 않습니다. 인증키가 포함된 provisioned 패키지는 권한 `0600`으로 생성되며 공개 저장소나 메신저로 전달하면 안 됩니다. 인증키 없는 범용 패키지는 `make bundle-generic`으로 만들 수 있으며 설치할 때 `./install.sh --token-file PATH`를 사용합니다.

### Windows 단일 설치 패키지

중앙 서버에서 Windows용 ZIP도 함께 생성할 수 있습니다.

```bash
make bundle-windows
```

생성 파일:

```text
dist/codex-usage-collector-0.6.1-windows-user-provisioned.zip
```

대상 Windows PC에서 ZIP을 푼 후 `install.cmd`를 더블클릭합니다. 관리자 권한은 필요하지 않으며, 현재 Windows 사용자 이름으로 작업 스케줄러에 등록되어 로그인할 때 자동으로 실행됩니다. 제거할 때는 같은 폴더의 `uninstall.cmd`를 실행합니다.

Windows 설치 프로그램은 Python 3을 먼저 탐색하고, 없으면 `winget`을 통해 현재 사용자 범위로 자동 설치합니다. `winget`도 없는 장비에서만 Python 3을 먼저 설치해야 합니다. Codex 상태 파일은 해당 사용자의 `%USERPROFILE%\.codex`에서 읽습니다.

### Windows 관리자 EXE 설치 — 권장

```text
dist/codex-usage-collector-0.6.1-windows-machine-setup.exe
```

EXE를 실행하고 UAC 관리자 권한 요청을 승인하면 다음 작업이 모두 자동으로 이루어집니다.

- 내장된 Python 런타임과 수집기를 `Program Files`에 설치
- 인증키를 `ProgramData`에 저장하고 SYSTEM·Administrators만 읽도록 ACL 설정
- SYSTEM 권한의 부팅 작업 등록 및 즉시 실행
- `C:\Users\*\.codex`를 읽어 이 PC의 모든 Codex 사용자 집계
- 중앙 서버 8765 포트로 나가는 프로그램별 Windows 방화벽 규칙 추가
- Windows 앱 제거 목록에 제거 프로그램 등록

기존 버전이 설치되어 있으면 설치기가 예약 작업과 실행 중인 수집기를 먼저 종료한 후 프로그램 파일을 교체하고 새 작업을 등록합니다. `ProgramData`의 `collector-state.json`과 회전 로그는 유지되므로 계정별 누적 사용량 추적 상태가 초기화되지 않습니다. 별도 제거 없이 새 EXE를 덮어 설치하면 됩니다.

별도 Python 설치나 사용자별 수집기 설정은 필요하지 않습니다. 제거할 때는 Windows의 **설치된 앱**에서 `Codex Usage Collector`를 제거합니다.

Windows 수집기 로그는 기본 5 MiB에서 회전하며 `collector.log.1`부터 최대 3개까지 보관합니다. 관리자 EXE 설치판은 다음 레지스트리 값을 바꾼 뒤 작업 등록 스크립트를 다시 실행해 조절할 수 있습니다.

```powershell
Set-ItemProperty "HKLM:\Software\CodexUsageCollector" -Name LogMaxBytes -Value 10485760
Set-ItemProperty "HKLM:\Software\CodexUsageCollector" -Name LogBackups -Value 5
& "$env:ProgramFiles\Codex Usage Collector\register_machine_task.ps1"
```

위 예시는 로그당 10 MiB, 백업 5개입니다. 사용자 ZIP 설치판은 `install.ps1 -LogMaxBytes 10485760 -LogBackups 5`처럼 지정할 수 있고, Linux에서는 `CODEX_COLLECTOR_LOG_MAX_BYTES`와 `CODEX_COLLECTOR_LOG_BACKUPS` 환경변수를 사용할 수 있습니다.

현재 EXE는 코드 서명 인증서로 서명되지 않아 Windows에서 게시자를 `Unknown`으로 표시하거나 SmartScreen 경고를 낼 수 있습니다. 배포 전에 조직의 코드 서명 인증서로 서명하는 것을 권장합니다.

Linux와 Windows의 provisioned 및 generic 패키지를 한 번에 생성하려면:

```bash
make packages
```

## 비공개 GitHub Release

이 저장소는 `v0.6.1`처럼 `v`로 시작하는 태그가 push되면 테스트, Linux/Windows 패키지 생성, Windows 관리자 EXE 생성 및 GitHub Release 게시를 자동으로 수행합니다. provisioned 패키지와 관리자 EXE에는 수집 인증키가 포함되므로 워크플로는 **비공개 저장소에서만** 실행됩니다.

저장소 설정에 다음 값을 등록해야 합니다.

- Actions secret `COLLECTOR_TOKEN`: 서버의 `data/collector.token` 내용
- Actions secret `TARGET_ACCOUNT_KEY`: 이 모니터에 등록된 12자리 익명 계정 키
- Actions variable `MONITOR_SERVER_URL`: 예: `http://143.248.136.95:8765`

릴리스할 때 `VERSION` 값을 올리고 동일한 버전의 태그를 push합니다.

```bash
git tag v0.6.1
git push origin main --tags
```

GitHub에 올라간 provisioned 설치 파일은 저장소 접근 권한이 있는 사용자에게만 전달하세요. 노출되었다면 수집 토큰을 즉시 교체하고 모든 수집기를 다시 배포해야 합니다. 자세한 내용은 `SECURITY.md`를 참고하세요.

## 테스트

```bash
python3 -m unittest discover -s tests -v
```

## 현재 범위

이 버전은 Linux 사용자 수집기, Windows 사용자 ZIP, Windows 관리자 EXE 수집기를 지원합니다. 중앙 서버는 계정 전체 최신 사용량과 각 서버·사용자·프로젝트·세션의 익명 상세 집계를 함께 표시합니다. Codex app-server가 제공하지 않는 과거 혼합 계정의 세션별 귀속은 소급 복원하지 않으며, 설치 이후 관측된 공유 계정 사용 구간부터 로컬 상세값에 반영합니다.
