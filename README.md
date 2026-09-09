# VESSL workspace → Slack

VESSL workspace가 실행 중이었다가 `stopped`가 되면 Slack으로 알립니다.
매시간 17분에 GitHub Actions가 확인합니다. 서버를 직접 운영할 필요가 없습니다.

## 비용 0원 구성

- **공개 저장소 + 무료 GitHub 표준 `ubuntu-latest` 러너**만 사용합니다.
- 비공개 저장소로 변경하면 job을 건너뛰도록 설정했습니다.
- 유료 서버, 대형 러너, Actions cache/artifact 저장소를 사용하지 않습니다.
- 기존 VESSL workspace의 상태만 조회합니다. 생성·시작·중지 명령을 실행하지 않습니다.
- 기존 VESSL workspace 자체의 사용료는 이 알림 프로그램과 별개입니다.

[GitHub Actions 요금 문서](https://docs.github.com/en/actions/concepts/billing-and-usage)

## 설정

저장소 Settings → Secrets and variables → Actions에 다음 **Repository secrets**를 등록합니다.

| Secret | 값 |
| --- | --- |
| `VESSL_ACCESS_TOKEN` | VESSL API 인증 토큰 |
| `VESSL_DEFAULT_ORGANIZATION` | VESSL 조직 이름 |
| `SLACK_WEBHOOK_URL` | Slack Incoming Webhook URL |
| `STATE_ENCRYPTION_KEY` | 아래 명령으로 생성한 Fernet 암호화 키 |

같은 화면의 **Variables** 탭에서 **New repository variable**로 다음 항목을 등록합니다.
Variable은 저장 후에도 값을 확인하고 수정할 수 있습니다.

| Variable | 값 |
| --- | --- |
| `VESSL_WORKSPACE_IDS` | 감시할 workspace ID를 쉼표로 구분 |

```bash
python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())'
```

Slack 앱에서 Incoming Webhooks를 켜고 Add New Webhook to Workspace로 알림 채널을 선택합니다.
URL은 저장소 Secret 입력창에 직접 입력하세요. 코드나 이슈에 붙여 넣지 마세요.
[Slack 설정 가이드](https://docs.slack.dev/messaging/sending-messages-using-incoming-webhooks/)

Actions → VESSL workspace stop alerts → Run workflow에서 `dry_run`을 켜면
인증·상태 복호화·실제 workspace 조회만 검증합니다. 메시지와 상태 저장은 하지 않습니다.
일반 실행이 한 번 성공해야 현재 상태가 기준으로 저장되고 감시가 시작됩니다.

현재 로컬 VESSL CLI와 같은 버전의 공식 Python SDK를 사용합니다.
표 형식 CLI 출력을 파싱하지 않고 지정한 ID를 하나씩 조회합니다.

## 동작 및 한계

- 처음부터 중지된 workspace는 알리지 않습니다. 실행 중인 상태를 한 번 관측한 후 중지되면 알립니다.
- `running → stopping → stopped` 전환도 감지합니다.
- 조회 실패·권한 오류·찾을 수 없는 workspace를 중지로 오인하지 않습니다. 실패한 실행은 Actions에 표시됩니다.
- 감시 대상 하나라도 조회에 실패하면 그 실행의 상태는 갱신하지 않습니다. 삭제한 ID는 `VESSL_WORKSPACE_IDS` Variable에서 제거하세요.
- 전송할 알림을 먼저 저장하고, Slack 전송 성공 뒤 지웁니다. 전송 실패는 다음 실행에 재시도합니다.
- Slack 전송 직후 상태 저장 실패 또는 응답 유실이 발생하면 같은 알림이 다시 전송될 수 있습니다.
- 이전 상태와 미전송 알림은 `.monitor/state.enc`에 Fernet으로 암호화해 커밋합니다.
  감시 코드는 이름·상태를 공개 로그에 출력하지 않습니다. workspace ID는 Variable로 관리하며
  workflow 환경 정보에 표시될 수 있습니다. 암호문 갱신 시각과 workflow 성공/실패도 공개됩니다.
- 정기적인 상태 커밋이 저장소 활동을 유지합니다. 장기간 실패해 활동이 60일 없으면 공개 저장소의 예약 실행이 비활성화될 수 있습니다.
- 암호화 키를 잃어버리면 기존 상태를 복구할 수 없습니다. 키 교체 시 상태를 별도로 마이그레이션하거나
  `.monitor/state.enc`를 삭제해 기준을 다시 설정해야 하며, 삭제하면 미전송 알림도 사라집니다.
- 예약 실행은 지연·누락될 수 있습니다. 조회 사이에 중지됐다가 다시 실행된 경우 감지할 수 없습니다.
- workspace 중지만 감시하며, workspace 내부 학습 프로세스 종료는 감시하지 않습니다.

[GitHub 예약 실행 제약](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#schedule)

## 팀에서 같이 사용하기

공용 Slack 채널의 Webhook 하나를 연결하고, 참여자의 workspace ID를
`VESSL_WORKSPACE_IDS` Variable에 추가하면 됩니다. 알림에는 workspace 이름과 소유자가 표시됩니다.
조회에 사용하는 VESSL 계정은 등록된 모든 workspace에 접근할 수 있어야 합니다.
각자의 VESSL 토큰을 모을 필요는 없습니다. 다른 조직은 별도 저장소/인증으로 운영하세요.

새 workspace는 ID를 명시적으로 추가해야 합니다. 조직의 모든 workspace를 자동 감시하지 않습니다.
참여자는 운영자에게 ID만 전달하면 되고, Secrets 관리 권한은 운영자만 가지고 있어도 됩니다.
다른 팀은 이 저장소를 공개 저장소로 복사한 뒤 자체 Secrets와 Variable을 등록해 사용할 수 있습니다.

## 로컬 테스트

```bash
python -m pip install -r requirements.txt
python -m unittest discover -s tests -v
```

테스트는 가짜 상태와 가짜 전송 함수를 사용합니다. 실제 workspace나 Slack을 변경하지 않습니다.
