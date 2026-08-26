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

세션 기간 집계는 rollout JSONL에서 `event_msg/token_count`의 시각과 `last_token_usage.total_tokens`만 로컬에서 읽습니다. 다른 이벤트의 대화 내용은 사용하거나 전송하지 않습니다. 최초 설치 전 기록, 2분을 넘는 수집 중단 구간과 계정 전환 경계는 공유 계정에 확정 귀속하지 않습니다.

계정 전체 누적값은 공유 계정 기준의 대표 수치이고, 세션별 수치는 각 서버·사용자·프로젝트에 귀속된 상세 수치입니다. 여러 PC가 동일한 공유 계정 전체값을 보내더라도 서버는 계정 키별 최신 스냅샷 하나만 저장하므로 중복 합산하지 않습니다.

대시보드의 **주간 사용량 추이**는 현재 10,080분 한도 윈도우의 사용률만 표시합니다. 서버가 `resets_at` 변경을 감지하면 이전 주기 샘플을 화면에서 제외하고 새 윈도우 시작점 0%부터 다시 그립니다. 원본 샘플은 장애 진단을 위해 최대 14일만 보관됩니다.

상단 **TOTAL PROCESSED**는 세션 합계가 아니라 app-server가 보고한 계정 전체 `lifetime_tokens`입니다. 계정의 실제 전체 누적 처리량이므로 주간 한도가 초기화되어도 이 숫자는 초기화되지 않습니다. 주간 `resets_at` 변경에 따른 초기화는 **주간 사용량 추이와 권장 속도**에만 적용됩니다.

**계정 전체값과 세션 귀속 대조**는 같은 관측 구간의 계정 증가량과 수집된 세션 증가량을 비교해 미귀속 차이를 표시합니다. 수집기 설치 전 사용, 수집기 중지·지연, 수집되지 않은 장비 또는 세션에 귀속되지 않은 사용은 원인 후보로 안내됩니다. **수집 사용자별 주간 사용량 비교**는 서버·OS 사용자별 세션 귀속 증가량, 전체 비중과 평균 대비 배수를 보여줍니다. 주기 중간에 처음 등록되어 주간 시작 기준값이 없는 세션은 0으로 단정하지 않고, 서버가 확인한 최소 증가량부터 초기 누적분을 포함한 가능 최대량까지 범위로 표시하며 전체 귀속 누적량도 함께 보여줍니다.

주간 그래프의 점선은 주간 한도를 하루 약 14.3%씩 균등하게 쓰는 권장선입니다. 실제 사용률과 권장선의 차이로 빠름·권장 범위·여유를 판정하고, 현재 속도가 유지될 때의 주기 종료 예상 사용률과 남은 기간의 일일 권장량을 함께 표시합니다.

### 사용 속도 알림

계정의 실제 주간 사용률이 현재 경과 시간 권장선의 150% 이상이 되면, 서버는 확인된 주간 세션 사용량이 가장 많은 수집 노드에 경고를 전달합니다. 같은 주간 창에서는 사용률 10% 구간마다 한 번만 보냅니다. Windows는 해당 사용자에게 `msg.exe` 알림을 표시하고, Linux 데스크톱은 `notify-send`를 사용합니다. GUI 알림을 사용할 수 없는 서버 환경에서도 수집기 로그에는 경고가 남습니다.

### 수집기 자동 업데이트

0.6.0부터 수집기는 매 보고 응답에서 중앙 서버 버전을 확인합니다. 새 버전이 있으면 서버가 제공한 Python 파일의 응답 HMAC, SHA-256과 Python 구문을 모두 검증한 뒤 기존 파일을 `.previous`로 보존하고 원자적으로 교체합니다. 검증이나 교체에 실패하면 기존 실행 파일을 복구합니다. Linux 사용자 서비스, Windows 사용자 작업과 Windows SYSTEM 설치판을 지원합니다.

0.5.x 이하 수집기는 서버 응답을 읽는 기능이 없으므로 **0.6.0 설치 파일을 한 번 직접 덮어 설치해야 합니다.** 이후 코드 업데이트는 자동 적용됩니다. Python 런타임, 작업 스케줄러나 설치 권한 구조가 바뀌는 버전은 전체 설치 파일을 다시 배포해야 할 수 있습니다.

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

Codex 내부 DB에는 세션별 계정 ID가 없으므로 수집기 설치 전의 혼합 계정 이력을 완벽히 소급 분리할 수는 없습니다. 첫 수집 당시 마지막 로그인보다 오래된 기존 기록은 초기값에서 제외하고, 이후 30초 간격으로 관찰되는 증가량부터 정확히 구분합니다. 수집기가 정지된 동안 여러 차례 계정 전환과 사용이 발생하면 그 구간 역시 정확히 분리할 수 없습니다.

### Linux 단일 설치 패키지

중앙 서버에서 인증키가 포함된 사용자용 패키지를 생성합니다.

```bash
make bundle
```

생성 파일:

```text
dist/codex-usage-collector-0.6.0-linux-user-provisioned.tar.gz
```

이 파일 하나를 대상 서버에 안전하게 전송한 뒤 해당 사용자로 설치합니다.

```bash
tar -xzf codex-usage-collector-0.6.0-linux-user-provisioned.tar.gz
cd codex-usage-collector-0.6.0
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
dist/codex-usage-collector-0.6.0-windows-user-provisioned.zip
```

대상 Windows PC에서 ZIP을 푼 후 `install.cmd`를 더블클릭합니다. 관리자 권한은 필요하지 않으며, 현재 Windows 사용자 이름으로 작업 스케줄러에 등록되어 로그인할 때 자동으로 실행됩니다. 제거할 때는 같은 폴더의 `uninstall.cmd`를 실행합니다.

Windows 설치 프로그램은 Python 3을 먼저 탐색하고, 없으면 `winget`을 통해 현재 사용자 범위로 자동 설치합니다. `winget`도 없는 장비에서만 Python 3을 먼저 설치해야 합니다. Codex 상태 파일은 해당 사용자의 `%USERPROFILE%\.codex`에서 읽습니다.

### Windows 관리자 EXE 설치 — 권장

```text
dist/codex-usage-collector-0.6.0-windows-machine-setup.exe
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

이 저장소는 `v0.6.0`처럼 `v`로 시작하는 태그가 push되면 테스트, Linux/Windows 패키지 생성, Windows 관리자 EXE 생성 및 GitHub Release 게시를 자동으로 수행합니다. provisioned 패키지와 관리자 EXE에는 수집 인증키가 포함되므로 워크플로는 **비공개 저장소에서만** 실행됩니다.

저장소 설정에 다음 값을 등록해야 합니다.

- Actions secret `COLLECTOR_TOKEN`: 서버의 `data/collector.token` 내용
- Actions secret `TARGET_ACCOUNT_KEY`: 이 모니터에 등록된 12자리 익명 계정 키
- Actions variable `MONITOR_SERVER_URL`: 예: `http://143.248.136.95:8765`

릴리스할 때 `VERSION` 값을 올리고 동일한 버전의 태그를 push합니다.

```bash
git tag v0.6.0
git push origin main --tags
```

GitHub에 올라간 provisioned 설치 파일은 저장소 접근 권한이 있는 사용자에게만 전달하세요. 노출되었다면 수집 토큰을 즉시 교체하고 모든 수집기를 다시 배포해야 합니다. 자세한 내용은 `SECURITY.md`를 참고하세요.

## 테스트

```bash
python3 -m unittest discover -s tests -v
```

## 현재 범위

이 버전은 Linux 사용자 수집기, Windows 사용자 ZIP, Windows 관리자 EXE 수집기를 지원합니다. 중앙 서버는 계정 전체 최신 사용량과 각 서버·사용자·프로젝트·세션의 익명 상세 집계를 함께 표시합니다. Codex app-server가 제공하지 않는 과거 혼합 계정의 세션별 귀속은 소급 복원하지 않으며, 설치 이후 관측된 공유 계정 사용 구간부터 로컬 상세값에 반영합니다.
