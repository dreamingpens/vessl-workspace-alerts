# VESSL workspace → Slack

VESSL workspace가 실행 중이었다가 `stopped`가 되면 Slack으로 알립니다.
새로 등록한 workspace가 첫 확인부터 `stopped`여도 한 번 알립니다.
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
| `VESSL_WORKSPACE_IDS` | workspace ID 또는 `소유자/이름`을 쉼표로 구분. 혼합 가능 |

예: `123456,alice/gpu-pod,bob/cpu-pod`처럼 입력합니다. 소유자는 VESSL의
`Creator`에 표시되는 사용자 이름이며, workspace 이름은 대소문자까지 정확히 일치해야 합니다.
같은 workspace를 ID와 `소유자/이름`으로 중복 등록해도 한 번만 감시합니다.
Variable 이름은 기존 설정과 호환되도록 `VESSL_WORKSPACE_IDS`를 그대로 사용합니다.

암호화 키 생성:

```bash
python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())'
```

Slack 앱에서 Incoming Webhooks를 켜고 Add New Webhook to Workspace로 알림 채널을 선택합니다.
URL은 저장소 Secret 입력창에 직접 입력하세요. 코드나 이슈에 붙여 넣지 마세요.
[Slack 설정 가이드](https://docs.slack.dev/messaging/sending-messages-using-incoming-webhooks/)

Actions → VESSL workspace stop alerts → Run workflow에서 `dry_run`을 켜면
인증·상태 복호화·실제 workspace 조회만 검증합니다. 메시지와 상태 저장은 하지 않습니다.
일반 실행이 한 번 성공해야 현재 상태가 기준으로 저장되고 감시가 시작됩니다.

Slack 연결만 테스트하려면 **Run workflow → `test_slack` 체크 → 실행**합니다.
`dry_run`은 체크하지 마세요. 연결 확인용 메시지를 한 건 전송하며,
workspace를 조회하거나 이전 감시 상태를 바꾸지 않습니다.

현재 로컬 VESSL CLI와 같은 버전의 공식 Python SDK를 사용합니다.
표 형식 CLI 출력을 파싱하지 않습니다. 숫자 ID는 직접 조회하고, `소유자/이름`은
본인·다른 사용자 목록을 모두 확인해 ID로 해석한 다음 상세 조회합니다.

## 동작 및 한계

- 새 대상이 첫 확인부터 `stopped`이면 **등록 후 첫 확인에서 이미 중지된 상태**라고 한 번 알립니다.
  계속 중지 상태인 동안에는 반복하지 않으며, 이후 `running`을 확인한 뒤 다시 중지되면 새로 알립니다.
  기존 버전에서 이미 기준 상태를 저장한 중지 대상은 소급해서 알리지 않습니다.
  감시에서 제거되어 상태가 정리된 뒤 재등록하면 새 대상의 첫 확인으로 처리합니다.
- `running → stopping → stopped` 전환도 감지합니다.
- 조회 실패·권한 오류·찾을 수 없는 workspace를 중지로 오인하지 않습니다. 실패한 실행은 Actions에 표시됩니다.
- 감시 대상 하나라도 조회에 실패하면 그 실행의 상태는 갱신하지 않습니다. 삭제하거나 이름을 바꾼 대상은 `VESSL_WORKSPACE_IDS` Variable에서 수정하세요.
- `소유자/이름`이 없거나 여러 workspace와 일치하면 해당 항목 번호와 함께 오류를 표시합니다. 중복되는 경우 숫자 ID를 사용하세요.
- 상태는 항상 ID로 저장하므로 같은 workspace의 등록 형식을 바꿔도 감지 이력이 유지됩니다.
  같은 `소유자/이름`으로 새 workspace를 만들면 새 ID의 기준 상태부터 감시합니다.
- 전송할 알림을 먼저 저장하고, Slack 전송 성공 뒤 지웁니다. 전송 실패는 다음 실행에 재시도합니다.
- Slack 전송 직후 상태 저장 실패 또는 응답 유실이 발생하면 같은 알림이 다시 전송될 수 있습니다.
- 이전 상태와 미전송 알림은 `.monitor/state.enc`에 Fernet으로 암호화해 커밋합니다.
  감시 코드는 조회 결과의 이름·상태를 공개 로그에 출력하지 않습니다. 등록한 ID 또는 `소유자/이름`은 Variable로 관리하며
  workflow 환경 정보에 표시될 수 있습니다. 암호문 갱신 시각과 workflow 성공/실패도 공개됩니다.
- 정기적인 상태 커밋이 저장소 활동을 유지합니다. 장기간 실패해 활동이 60일 없으면 공개 저장소의 예약 실행이 비활성화될 수 있습니다.
- 암호화 키를 잃어버리면 기존 상태를 복구할 수 없습니다. 키 교체 시 상태를 별도로 마이그레이션하거나
  `.monitor/state.enc`를 삭제해 기준을 다시 설정해야 하며, 삭제하면 미전송 알림도 사라집니다.
- 예약 실행은 지연·누락될 수 있습니다. 조회 사이에 중지됐다가 다시 실행된 경우 감지할 수 없습니다.
- workspace 중지만 감시하며, workspace 내부 학습 프로세스 종료는 감시하지 않습니다.

[GitHub 예약 실행 제약](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#schedule)

## 팀에서 같이 사용하기

공용 Slack 채널의 Webhook 하나를 연결하고, 참여자의 workspace ID 또는 `소유자/이름`을
`VESSL_WORKSPACE_IDS` Variable에 추가하면 됩니다. 알림에는 workspace 이름과 소유자가 표시됩니다.
조회에 사용하는 VESSL 계정은 등록된 모든 workspace에 접근할 수 있어야 합니다.
각자의 VESSL 토큰을 모을 필요는 없습니다. 다른 조직은 별도 저장소/인증으로 운영하세요.

새 이름의 workspace는 감시 목록에 명시적으로 추가해야 합니다. 조직의 모든 workspace를 자동 감시하지 않습니다.
참여자는 아래 등록 폼으로 신청하면 됩니다. 저장소 수정 권한이나 별도 토큰은 필요 없습니다.
다른 팀은 이 저장소를 공개 저장소로 복사한 뒤 자체 Secrets와 Variable을 등록해 사용할 수 있습니다.

## 등록 폼으로 신청하기

[**Workspace 알림 등록 폼 열기**](https://github.com/dreamingpens/vessl-workspace-alerts/issues/new?template=workspace-registration.yml)

1. GitHub에 로그인하고 폼에 **workspace ID 또는 `소유자/이름` 하나**를 입력합니다.
2. 공개 신청 및 공용 Slack 알림 안내를 확인하고 이슈를 제출합니다.
3. 저장소 소유자가 대상을 확인한 뒤 **새 댓글**에 `/watch alice/gpu-pod` 또는
   `/watch 123456`을 한 줄로 남기면 승인됩니다. 이 저장소에서는 `dreamingpens`만 승인할 수 있습니다.
4. 다음 정기 실행부터 기존 Variable의 대상과 합쳐서 감시합니다. 정기 실행은 매시간 17분이며 지연될 수 있습니다.
   첫 확인에서 이미 중지된 상태면 한 번 알립니다. 이후 실행 중인 상태를 확인한 뒤 다시 중지되면 또 알립니다.

이슈가 **열려 있고** `workspace-registration` 라벨이 있어야 신청이 유지됩니다.
취소하려면 이슈를 닫거나 운영자가 새 댓글에 `/unwatch`를 적습니다.
이슈를 다시 열면 이전 승인이 다시 적용됩니다. 완전히 승인을 철회할 때는 `/unwatch`를 사용하세요.
같은 대상이 다른 승인 이슈나 기존 Variable에도 있다면 해당 등록을 모두 제거해야 감시가 해제됩니다.

**운영자 승인 규칙:** 신청 본문이 아닌 **승인 댓글에 적은 대상**을 감시합니다.
신청자가 본문을 수정해도 대상은 바뀌지 않습니다. 변경할 때는 새 `/watch ...` 댓글을 남기세요.
소유자의 명령 중 작성 순서상 마지막 명령이 적용됩니다. 일반 대화 댓글은 무시합니다.
수정된 명령 댓글이나 잘못된 명령은 승인으로 인정하지 않으므로, 댓글을 고치지 말고 새 댓글을 작성하세요.
승인 명령은 코드 블록 없이 한 줄로 쓰며, 한 명령에 대상 하나만 허용합니다.
승인 댓글 삭제 시 그 이전 명령이 다시 적용될 수 있으므로 취소는 `/unwatch`로 처리하세요.

등록 결과는 Actions 실행 로그의 `Registration check completed` 승인 이슈 수로 확인할 수 있습니다.
승인 전 신청에는 별도 자동 댓글이나 Slack 메시지를 보내지 않습니다.
존재하지 않거나 접근할 수 없는 대상을 승인하면 해당 실행이 실패합니다. 승인 전에 대상을 확인하고,
문제가 있으면 이슈를 닫거나 올바른 숫자 ID로 새 승인 댓글을 남기세요.

신청과 승인 댓글은 **공개**입니다. VESSL 토큰과 Slack Webhook URL은 적지 마세요.
폼 등록은 `VESSL_WORKSPACE_IDS` Variable 자체를 수정하지 않고, 매 실행 시 승인된 신청을 합칩니다.
Variable이 비어 있어도 폼만으로 사용할 수 있습니다. 추가 서버, PAT, GitHub App은 필요 없습니다.
공개 저장소의 기존 표준 Actions 러너와 `issues: read` 권한을 사용합니다.
다른 저장소로 복사할 때는 `workspace-registration` 라벨도 만들어 주세요.
현재 승인자는 개인 저장소 소유자이므로 조직 소유 저장소로 옮기면 승인자 정책을 별도로 수정해야 합니다.

## 로컬 테스트

```bash
python -m pip install -r requirements.txt
python -m unittest discover -s tests -v
```

테스트는 가짜 상태와 가짜 전송 함수를 사용합니다. 실제 workspace나 Slack을 변경하지 않습니다.
