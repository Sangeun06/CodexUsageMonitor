# Codex Usage Monitor

로컬 Codex 상태 DB에서 **토큰 사용량만** 읽어 보여주는 경량 대시보드입니다. 외부 패키지나 OpenAI API 키가 필요하지 않습니다.

## 수집하는 정보

- SHA-256으로 익명화한 세션 및 프로젝트 식별자
- 모델명
- 누적 `tokens_used`
- 생성·마지막 갱신 시각과 보관 여부

세션 제목, 프롬프트, 응답, 미리보기, 파일 경로, 작업 경로와 원본 세션 ID는 읽거나 저장하지 않습니다. 원본 Codex DB는 읽기 전용으로 엽니다.

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

수집 항목은 서버명, OS 사용자명, 계정 표시 이름·이메일·플랜, 프로젝트 폴더명, 모델, 토큰 수와 시각입니다. 인증 토큰, 대화 제목·본문, 전체 경로와 원본 세션 ID는 전송하지 않습니다.

배포 패키지에는 중앙 서버에서 현재 로그인한 공유 Codex 계정의 익명 계정 키가 포함됩니다. 각 수집기는 이 키와 일치하는 계정만 전송하며, 중앙 서버도 다른 계정의 수집 요청을 다시 거부합니다. 따라서 같은 PC에 다른 Codex 계정이 있어도 이 대시보드에는 등록되지 않습니다.

### Linux 단일 설치 패키지

중앙 서버에서 인증키가 포함된 사용자용 패키지를 생성합니다.

```bash
make bundle
```

생성 파일:

```text
dist/codex-usage-collector-0.4.1-linux-user-provisioned.tar.gz
```

이 파일 하나를 대상 서버에 안전하게 전송한 뒤 해당 사용자로 설치합니다.

```bash
tar -xzf codex-usage-collector-0.4.1-linux-user-provisioned.tar.gz
cd codex-usage-collector-0.4.1
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
dist/codex-usage-collector-0.4.1-windows-user-provisioned.zip
```

대상 Windows PC에서 ZIP을 푼 후 `install.cmd`를 더블클릭합니다. 관리자 권한은 필요하지 않으며, 현재 Windows 사용자 이름으로 작업 스케줄러에 등록되어 로그인할 때 자동으로 실행됩니다. 제거할 때는 같은 폴더의 `uninstall.cmd`를 실행합니다.

Windows 설치 프로그램은 Python 3을 먼저 탐색하고, 없으면 `winget`을 통해 현재 사용자 범위로 자동 설치합니다. `winget`도 없는 장비에서만 Python 3을 먼저 설치해야 합니다. Codex 상태 파일은 해당 사용자의 `%USERPROFILE%\.codex`에서 읽습니다.

### Windows 관리자 EXE 설치 — 권장

```text
dist/codex-usage-collector-0.4.1-windows-machine-setup.exe
```

EXE를 실행하고 UAC 관리자 권한 요청을 승인하면 다음 작업이 모두 자동으로 이루어집니다.

- 내장된 Python 런타임과 수집기를 `Program Files`에 설치
- 인증키를 `ProgramData`에 저장하고 SYSTEM·Administrators만 읽도록 ACL 설정
- SYSTEM 권한의 부팅 작업 등록 및 즉시 실행
- `C:\Users\*\.codex`를 읽어 이 PC의 모든 Codex 사용자 집계
- 중앙 서버 8765 포트로 나가는 프로그램별 Windows 방화벽 규칙 추가
- Windows 앱 제거 목록에 제거 프로그램 등록

별도 Python 설치나 사용자별 수집기 설정은 필요하지 않습니다. 제거할 때는 Windows의 **설치된 앱**에서 `Codex Usage Collector`를 제거합니다.

현재 EXE는 코드 서명 인증서로 서명되지 않아 Windows에서 게시자를 `Unknown`으로 표시하거나 SmartScreen 경고를 낼 수 있습니다. 배포 전에 조직의 코드 서명 인증서로 서명하는 것을 권장합니다.

Linux와 Windows의 provisioned 및 generic 패키지를 한 번에 생성하려면:

```bash
make packages
```

## 비공개 GitHub Release

이 저장소는 `v0.4.1`처럼 `v`로 시작하는 태그가 push되면 테스트, Linux/Windows 패키지 생성, Windows 관리자 EXE 생성 및 GitHub Release 게시를 자동으로 수행합니다. provisioned 패키지와 관리자 EXE에는 수집 인증키가 포함되므로 워크플로는 **비공개 저장소에서만** 실행됩니다.

저장소 설정에 다음 값을 등록해야 합니다.

- Actions secret `COLLECTOR_TOKEN`: 서버의 `data/collector.token` 내용
- Actions secret `TARGET_ACCOUNT_KEY`: 이 모니터에 등록된 12자리 익명 계정 키
- Actions variable `MONITOR_SERVER_URL`: 예: `http://143.248.136.95:8765`

릴리스할 때 `VERSION` 값을 올리고 동일한 버전의 태그를 push합니다.

```bash
git tag v0.4.1
git push origin main --tags
```

GitHub에 올라간 provisioned 설치 파일은 저장소 접근 권한이 있는 사용자에게만 전달하세요. 노출되었다면 수집 토큰을 즉시 교체하고 모든 수집기를 다시 배포해야 합니다. 자세한 내용은 `SECURITY.md`를 참고하세요.

## 테스트

```bash
python3 -m unittest discover -s tests -v
```

## 현재 범위

이 버전은 서버의 `~/.codex/state_5.sqlite`를 모니터링합니다. 다른 기기의 사용량은 이후 각 기기에 개인정보 제외 수집기를 설치하고 이 서버로 전송하는 방식으로 확장할 수 있습니다.
