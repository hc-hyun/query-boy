# ADR 0028: Interactive Operator Shell

Status: Accepted

Date: 2026-08-28

## Context

Query Man의 상태·metric, container log, consent-gated diagnostic capture와 managed source API는 서로 다른
명령과 문서에 흩어져 있다. 안전 경계를 아는 운영자는 사용할 수 있지만, 처음 접한 사내 사용자는 endpoint,
header, expected generation/state와 mutation receipt를 모두 알아야 한다. 반대로 source 관리 로직을 shell
script나 직접 SQL로 다시 구현하면 Control Plane의 validation, transaction, credential encryption과
idempotency를 우회한다.

사용자는 사내용 대화형 shell, Tab 자동완성, 부족한 입력의 즉시 안내, 짧은 `diag` 명령과 managed·로그
조회를 하나의 초보자용 surface로 제공하는 변경을 승인했다.

## Decision

1. 새 external CLI는 `qm`이다. 인자 없이 실행하면 `qm>` 대화형 prompt를 열고, 뒤에 명령을
   주면 같은 parser를 one-shot으로 실행한다. Python 표준 `cmd`/readline을 사용해 새 dependency 없이
   command와 subcommand Tab completion을 제공한다.
2. Top-level 명령은 `status`, `logs`, `diag`, `source`, `help`, `clear`, `exit`로 제한한다. 빈 줄은 이전
   명령을 반복하지 않고 빠른 안내를 표시한다. Unknown 또는 필수 인자가 부족한 입력은 mutation이나
   broad read를 실행하지 않고 한국어 usage와 copyable example을 표시한다.
3. `status`는 public readiness와 operator health/metric API를 읽는다. `source` read/mutation은 기존
   managed HTTP API만 사용한다. CLI가 Control DB나 `SourceAdminService` private 구현에 직접 접근하지
   않으므로 server-side operator authorization, staging, validation, CAS, transaction과 receipt 의미가
   유지된다.
4. Source mutation은 현재 generation/state를 먼저 읽고 새 UUID receipt와 bounded reason을 전송한다.
   Credential은 command argument가 아니라 no-echo prompt로만 받는다. 대상·현재 state·reason을 보여준 뒤
   exact confirmation phrase를 요구하며, 결과가 불명확하면 새 key로 retry하지 않고 `source receipt`를
   안내한다.
5. `logs`는 argument list로 고정한 `docker compose logs`를 shell 없이 실행한다. 기본은 최근 30분 50줄,
   최대 31일/1,000줄이며 level, event, query ID와 pseudonymous subject로 local filtering한다. SQL,
   question 또는 token을 새로 수집하지 않는다. 이는 local Compose provider이고 durable collector나
   multi-host log search를 대신하지 않는다.
6. `diag list`는 최근 1시간 summary 20건이 기본이고 최대 7일/100건만 복호화한다. Summary는 content를
   제외하고 question byte 수와 bounded metadata만 표시한다. `diag show`는 reason과 exact confirmation 뒤
   한 capture의 동의 기반 payload를 표시하고, `diag purge`도 reason과 destructive confirmation을
   요구한다. Compose host에서는 running application container 안의 같은 CLI를 통해 private volume을
   읽으며 key나 plaintext를 subprocess argument로 전달하지 않는다.
7. Operator token은 explicit `QM_TOKEN`/`--token-env` 또는 trusted access policy의
   `operator:true` caller token environment에서 찾는다. Token, credential, diagnostic key와 raw HTTP
   failure body는 출력하지 않는다. Base local Compose에는 query caller와 다른 `operator-local` token을
   추가하지만 managed source route 자체는 ADR 0025에 따라 계속 비활성이다.
8. `qm` 추가는 managed authority 활성화, protected source mutation, diagnostic read/purge 또는
   production log collector 설치의 실행 승인이 아니다. 실제 protected action은 target, access, reason,
   stop condition과 change-record 책임을 별도로 확인한다.

## Interface And Compatibility Impact

- External CLI: `qm [global-options] [status|logs|diag|source|help|clear|exit ...]`; 이전 CLI 이름과
  `QUERY_MANCTL_*` 환경변수는 제공하지 않음
- Delivery/access configuration: base Compose policy에 별도 operator caller/token environment 추가
- Protected diagnostic helper: aware `since`, 최대 100건과 optional capture UUID를 받는 bounded read 추가
- Unchanged: HTTP/MCP/admin wire, Control DB schema와 mutation semantics, general logging content policy,
  diagnostic SQLite schema/TTL/encryption, current static source authority

## Rollout And Rollback

Application image를 다시 빌드해야 container 내부 `qm`과 operator policy가 함께 반영된다. 먼저
`qm help`, `status`, filtered `logs`, content-free `diag list`를 확인한다. Managed 환경에서는
read-only source 명령을 먼저 확인하고 별도 승인된 mutation만 수행한다. Rollback은 CLI entrypoint와
operator caller/token injection을 제거한 이전 image/config로 되돌린다. Control DB나 diagnostic DB
migration은 없으며 CLI를 제거해도 기존 HTTP API와 stored record는 그대로다.

## Verification

- 빈 줄 non-repeat, unknown/missing input guide와 one-shot exit
- command/subcommand/cached source Tab completion
- structured log parsing/filtering과 no-match guide
- diagnostic summary content 비노출, bounded query, show/purge reason·confirmation
- source expected-state/receipt/reason header와 credential command-line 비노출
- operator token discovery, nested public error envelope와 secret 비출력
- Ruff, mypy, full pytest, Compose configuration와 container build
