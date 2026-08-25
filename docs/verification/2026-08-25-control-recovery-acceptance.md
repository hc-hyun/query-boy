# Control Recovery Acceptance — 2026-08-25

Status: Complete

## Scope

`CTRL-09`는 기존 Control Plane 계약을 바꾸지 않고 production recovery 절차의 Control 부분을
하나의 실행 가능한 격리 fixture 흐름으로 연결한다. 대상은 migration ledger 1개, core authority
6개와 bounded operational projection 6개를 합친 현재 `control` schema의 13개 table이다. Source
business database,
새 retention 기간, key rotation/versioning과 새 public API는 이 작업 범위가 아니다.

## Recovery Topology

- `recovery` Compose profile의 격리 PostgreSQL 18.4 source는 tmpfs만 사용하며 기존 service가 있으면
  덮어쓰지 않는다.
- Test 전용 random-prefix Control DB에 L0→L1→verified→L2 authority, terminal receipt, 두 stable
  replica, resource observation, gateway cursor/rollup과 31일 밖 aged rollup을 만든다.
- Source cluster의 custom archive를 `--no-owner --no-privileges`로 생성하고 SHA-256을 계산한다.
  Archive는 mode 0600의 임시 파일이며 성공·실패와 무관하게 삭제한다.
- 현재 PostgreSQL 18.6 cluster의 완전히 빈 random-prefix database에
  `--single-transaction --exit-on-error`로 restore한 뒤 production migration runner를 두 번
  적용해 ledger와 최소 권한 ACL을 reconcile한다.
- 두 cluster의 runtime LOGIN은 archive 밖에서 각각 생성한다. LOGIN은 유한 connection limit와
  `query_man_control_writer` membership 하나만 가지며 runtime 종료 뒤 session 0을 확인하고 제거한다.

## Acceptance Matrix

| Boundary | Result |
|---|---|
| PostgreSQL 18.4의 다른 service에서 18.6 fresh database로 custom archive restore | PASS |
| 13개 table 모두 non-empty이며 UTC/C canonical row count와 SHA-256 fingerprint가 restore 전후 동일 | PASS |
| 원래 32-byte master key로 세 generation의 AES-GCM credential decrypt | PASS |
| 다른 key로 모든 generation decrypt 거부 | PASS |
| 같은 key에서 파생된 HMAC으로 기존 idempotency receipt exact replay; 새 generation 없음 | PASS |
| Restore된 receipt sequence로 다음 deterministic rejection event가 기존 maximum 다음 값에 기록 | PASS |
| Source/verified directory가 존재하지 않는 managed zero-bootstrap | PASS |
| 원래 두 stable slot이 새 incarnation으로 `available`, `drift=[]`에 수렴 | PASS |
| 두 replica의 metadata L2 revision과 guarded verified query columns/row-count/result hash | PASS |
| 31일 밖 under-cap rollup은 restore 뒤 물리 보존되고 `/usage`에서는 제외 | PASS |
| Recovery service, archive, database, LOGIN과 connection의 leak-free cleanup | PASS |

## Commands And Results

| Command | Result |
|---|---|
| `uv run ruff check tests/test_control_recovery.py tests/control_database.py` | PASS |
| `uv run pytest -m integration -q tests/test_control_recovery.py` | PASS — 2 passed in 29.94s |
| `uv run ruff check .` | PASS |
| `uv run mypy src` | PASS — 29 source files |
| `uv run pytest` | PASS — 625 passed, 66 deselected in 18.39s |
| `uv run pytest -m integration` | PASS — 54 passed, 637 deselected in 175.81s |
| `bash -n scripts/control-plane-drill.sh` | PASS |
| `docker compose --profile recovery config --quiet` | PASS |
| `./scripts/control-plane-drill.sh` | PASS — 13 tables, 15 FKs, 4 triggers, immutable authority/ACL checks |

## Security And Data Boundaries

Test evidence와 subprocess output에는 key, credential, ciphertext, DSN, question, SQL 또는 result
literal을 기록하지 않는다. Archive checksum 값도 test process 안에서 형식과 생성만 확인하고 이
문서에 secret-bearing artifact 식별자로 남기지 않는다. Wrong-key failure는 filesystem fallback이나
부분 source 적용으로 바뀌지 않는다.

31일은 여전히 DB-clock logical visibility/input window이며 age-only physical deletion 기한이 아니다.
Source당 최신 1,000행 physical cap, immutable history/receipt, ever-registered replica와 cursor 의미도
변경하지 않았다.

## Deliberate Limits

- Repository acceptance는 on-demand fixture archive를 검증한다. 실제 production backup schedule,
  archive age, 보관소 access audit와 RPO 24시간 이내 달성은 deployment change record가 증명한다.
- Test 완료 시간은 production RTO 측정이 아니다. 실제 network, archive 크기와 secret manager를
  포함한 Control DB restore/runtime secret 주입의 60분 RTO는 운영 훈련에서 측정한다. Source
  business DB의 RTO와 복구는 각 source owner의 별도 정책을 따른다.
- Test LOGIN은 local password authentication을 사용한다. Production TLS, IAM/certificate와 backup
  identity 수명·감사는 배포 환경의 별도 authority다.
- PostgreSQL 18.4→18.6 logical restore는 현재 major 안의 version 이동 증거다. PostgreSQL major
  upgrade 지원 범위를 새로 약속하지 않는다.
- Source와 target 모두 current migration set을 사용하므로 실제 N-1 schema archive의 upgrade
  compatibility를 증명하지 않는다.
- Master-key online rotation, dual-key decrypt, re-encryption과 archive retention 정책을 추가하려면
  별도 module contract/운영 정책 승인이 필요하다.
