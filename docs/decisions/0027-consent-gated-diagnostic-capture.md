# ADR 0027: Consent-Gated Diagnostic Capture

Status: Accepted

Date: 2026-08-28

## Context

Query Man의 일반 application/audit log는 SQL, question과 request body를 저장하지 않고 query ID,
fingerprint, 지연, 결과 크기와 bounded outcome만 기록해 왔다. 이 정보는 장애·자원 조사에는 충분하지만,
사용자가 동의한 질문과 생성 SQL의 품질을 사후 분석하는 요구에는 부족하다. 반대로 일반 log에 원문을
추가하면 SQL literal, 제3자 정보와 사용자가 실수로 붙여 넣은 secret까지 장기 collector와 검색 index로
복제될 수 있다.

사용자는 동의를 별도로 확보하고, `질문 원문 + literal 제거 SQL`, 7일 보존, 별도 암호화 저장소,
bounded/fail-open 처리와 가명 subject를 적용하는 변경을 승인했다. 이 결정은 DB-native 비용 귀속이나
여러 POST workflow trace를 활성화하지 않는다.

## Decision

1. 일반 `query_man`/audit/MCP log는 계속 question, SQL와 raw body를 기록하지 않는다. Diagnostic
   capture가 설정된 process의 일반 audit identity는 raw caller/tenant 대신 active key에서 파생한
   `subject_id`를 사용한다. Subject는 tenant와 caller를 environment-specific key로 HMAC한 144-bit
   URL-safe 값이고 metric label, authorization, quota나 mutable-state key가 아니다.
2. Consent authority는 server-side access policy다. Version 2 caller에 optional
   `diagnostic_consent {version: 1, receipt_id, expires_at}`를 명시한 경우에만 capture한다. Client header,
   body나 MCP argument로 consent를 주장할 수 없다. 만료 시각은 timezone-aware여야 하고 equality부터
   inactive다. Version 1 scope는 question 원문과 SQL diagnostic shape 두 가지로 고정한다.
3. Delivery는 인증과 active-source 확인을 통과한 `GatewayService.get_context/query`에서만 Runtime이
   제공한 `DiagnosticCapture` port를 호출한다. Unknown source, pre-auth failure, list/cancel/admin,
   header/body와 result row는 capture하지 않는다. HTTP와 MCP는 같은 Gateway path를 사용하며 wire
   request/response/tool schema는 바꾸지 않는다.
4. Question은 현재 bounded model을 통과한 UTF-8 원문을 저장한다. SQL은 PostgreSQL 18 parser가 exact
   single `SelectStmt`로 읽은 경우에만 모든 `A_Const`를 `NULL`로 바꿔 deparse한다. Comment, quoted,
   dollar-quoted, numeric, date/interval과 bit literal은 남지 않는다. Invalid, multi-statement와 non-SELECT는
   raw text 대신 byte 수와 `sql_parseable=false`만 저장한다. 이 helper는 diagnostic rendering일 뿐
   executable SQL, SQL-policy acceptance나 revision 재료가 아니다.
5. Runtime은 configured SQLite file 하나에 schema version 1 envelope를 저장한다. Plaintext payload는
   AES-GCM으로 암호화하고 `capture_id`, `key_id`, keyed consent handle, capture/expiry time를 associated
   data로 인증한다. Outer table에는 이 bounded envelope metadata와 nonce/ciphertext만 있으며 caller,
   tenant, receipt 원문, question과 SQL은 없다. File은 regular `0600`, parent volume은 private writable
   storage여야 한다.
6. Active key는 URL-safe Base64 32 bytes와 lowercase key ID로 설정한다. Encryption과 subject/consent
   HMAC key는 domain-separated derivation을 사용한다. Rotation은 새 key/key ID로 재배포하고, 이전
   record를 읽거나 receipt purge할 동안 이전 key를 protected key store에 유지한다. Source-generation
   encryption key와 재사용하지 않는다.
7. Capture TTL은 record별 최대 7일이며 consent expiry가 더 이르면 그 시각을 쓴다. Offline read는 만료 row를 반환하지 않고 worker는 새 write와
   매시간 sweep에서 물리 삭제한다. 기본 encrypted-payload ingestion budget은 UTC day당 100 MiB이며
   configuration bound는 1 MiB~10 GiB다. Queue는 process당 64개로 고정한다. Storage/submit 실패와
   queue/budget/shutdown drop은 bounded counter로 관찰하고 caller/source/subject를 metric label로
   사용하지 않는다.
8. Capture submit/storage/retention 실패는 query/context response, readiness와 source health를 바꾸지
   않는다. Explicit configuration의 key/shape 오류만 composition 전에 fail-closed한다. Runtime은 worker를
   serving 전에 시작하고 shutdown에서 최대 2초 또는 남은 configured grace 중 작은 값만 drain한 뒤
   query/catalog/metadata cleanup을 계속한다.
9. Decrypt와 consent purge는 repository가 제공하는 offline helper를 protected operator workflow에서만
   호출한다. Consent 철회는 traffic drain → policy에서 receipt 제거 → process 교체 → active/old key별
   receipt purge 순서다. Purge 전에도 expiry 뒤 logical read는 금지된다. Diagnostic DB와 그 raw export는
   evidence/change record가 아니며 일반 audit retention이나 immutable operational evidence와 섞지 않는다.

## Boundary And Compatibility Impact

- Module interface: Delivery `DiagnosticConsent`, expanded `CallerContext`, `caller_audit_fields`,
  `DiagnosticCapture` port와 optional `GatewayService` constructor input
- Persisted/versioned format: access policy v2 optional consent object와 local encrypted SQLite schema v1
- Policy/safety: audit identity allowlist, content capture allowlist, 7-day TTL, key rotation, byte/queue bounds와
  fail-open capture isolation
- Lifecycle/ownership: Runtime이 concrete encrypted sink와 worker를 조립·정리하고 Delivery가
  auth/source-after capture 시점과 content field를 소유
- Unchanged: HTTP/MCP schema/status, source visibility, SQL validation/revision/fingerprint, query result,
  Guarded Query cancel/rollback, Control DB와 DB-native cost/workflow trace

## Rollout And Rollback

1. Code를 capture disabled 상태로 배포한다. 이때 기존 HTTP/MCP 동작과 audit identity는 유지된다.
2. Private writable volume, dedicated capture key/key ID, daily byte budget과 collector/operator access를
   protected change record에 고정한다.
3. 실제 consent receipt와 expiry를 해당 caller policy에 추가하고 한 replica에서 encrypted write,
   decrypt, TTL, drop metric과 no-raw-log를 확인한 뒤 확대한다.
4. Rollback은 새 admission을 drain하고 capture environment를 제거해 code path를 disable한다. Consent
   철회면 policy에서도 receipt를 제거하고 offline purge한다. Control/source DB migration은 없으며 capture
   SQLite file은 TTL/purge 외에 자동으로 삭제하지 않는다.

## Verification

- Consent v1/timezone/expiry/strict-field와 no-consent compatibility
- HMAC subject stability/key separation, raw caller/tenant 일반 audit 부재
- String/dollar/numeric/date/interval/bit/comment literal redaction과 invalid/non-SELECT no-raw path
- AES-GCM ciphertext, associated-data tamper/key mismatch, `0600`, schema-version/shape rejection
- 7-day logical/physical retention, consent purge와 conservative daily budget
- Queue/storage/submit/shutdown failure가 HTTP/MCP/query 결과와 cleanup을 바꾸지 않음
- Ruff, mypy, full pytest와 container configuration/smoke regression
