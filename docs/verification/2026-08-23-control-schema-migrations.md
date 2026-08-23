# Control Schema Migration And Test Isolation Audit

Status: Complete

## Scope

`CTRL-01`의 numbered Control DB migration, checksum drift 거부, 기존 unversioned development
schema의 data-preserving adoption, repeatable security reconciliation과 disposable integration-test
database lifecycle을 검증했다. Production source authority precedence와 zero-bootstrap startup은
다음 `CTRL-02` 범위다.

## Evidence

| Contract | Evidence | Result |
|---|---|---|
| Deterministic migration order | Runner가 연속된 `NNNN_name.sql`만 허용하고 repository file을 정렬한다. | PASS |
| Fresh database apply | 임시 empty database가 `0001_baseline.sql`과 5개 authority table을 생성하고 ledger에 filename/SHA-256을 1회 기록한다. | PASS |
| Idempotent reapply | Sentinel metadata row, table OID와 ledger signature가 두 번째 apply 전후 동일하다. | PASS |
| Drift and DB-ahead failure | Applied checksum mismatch와 checkout에 없는 version 2가 migration/ACL 변경 전에 non-zero로 거부된다. | PASS |
| Existing baseline adoption | Local development authority counts `143|8|458|4|38`이 version 1 채택 전후 동일했다. | PASS |
| Restore security | `--no-owner --no-privileges` archive restore 후 같은 runner가 role/ACL을 복구하고 runtime writer의 ledger/immutable-table mutation을 거부한다. | PASS |
| Test isolation | 다섯 Control DB mutation scenarios가 UUID database를 각각 사용하고 pool 종료 뒤 database를 삭제한다. | PASS |
| Development history isolation | 각 disposable fixture 전후 5개 development authority table의 count와 row digest가 동일하다. | PASS |
| Secret boundary | Migration command와 failure에는 password, DSN, manifest, ciphertext 또는 SQL payload를 출력하지 않는다. | PASS |

Migration DDL과 ledger insert는 같은 transaction에 있고 database advisory lock으로 직렬화된다.
Global `query_man_control_writer` 생성/hardening과 DB별 ACL은 numbered history와 분리된
`reconcile-security.sql`을 매번 실행한다. Restore archive가 이미 ledger를 포함해 numbered
migration을 건너뛰더라도 role/ACL은 복구된다.

## Commands

```text
bash -n docker/postgres/init/05-control-plane.sh \
  docker/postgres/init/control-migrations/apply.sh \
  scripts/apply-control-schema.sh scripts/apply-db.sh scripts/control-plane-drill.sh
uv run ruff check .
uv run mypy src
uv run pytest
uv run pytest -m integration
./scripts/apply-db.sh
./scripts/control-plane-drill.sh
```

검증 시 migration 전후 development authority row count는 각각
`143|8|458|4|38`이었고 ledger만 version 1로 추가됐다. Restore drill은 migration ledger를
포함한 6개 table, 4개 FK, 3개 immutable trigger, 실제 immutable UPDATE 거부와 writer ACL을
통과했다. 최종 결과는 기본 suite `313 passed, 32 deselected`, integration suite
`20 passed, 325 deselected`이며 evaluation 16 cases와 9개 source verification도 모두
`status: ok`였다.

전체 integration 재검증에서 replica가 metadata revision과 quality만 먼저 관측해 새 source
generation 적용까지 끝난 것으로 오판하는 test convergence race를 발견했다. Hot-add 검증의
수렴 조건에 실제 `control_generation` 일치를 포함했고, 해당 multi-replica scenario를 3회
연속 통과한 뒤 전체 integration suite를 다시 통과했다.

## Deliberate Limits And Future Triggers

- 현재 numbered migration은 하나이므로 실제 N-1→N data migration과 rolling application
  compatibility는 아직 증명 대상이 아니다. `0002`를 추가하는 변경에서 upgrade, 실패 rollback과
  concurrent pending apply를 함께 검증한다.
- Test process crash로 남은 scratch database가 반복 관측되기 전에는 prefix/age cleanup service를
  만들지 않는다. CI 비정상 종료 잔여물은 ephemeral Compose volume 폐기로 정리한다.
- Existing local development history는 사용자 데이터로 취급해 자동 삭제하지 않았다.
- Cross-host/version restore, encryption-key decrypt, zero-bootstrap과 multi-replica service
  recovery는 `CTRL-09`에서 검증한다.
