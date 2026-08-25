# Canonical Time Stability Verification

Date: 2026-08-25

Decision: [ADR 0019](../decisions/0019-canonical-time-stability.md)

## R1 Business Calendar Baseline

R1은 reader/runtime를 바꾸기 전에 한국 업무 달력을 SQL에 명시했다.

| Source | R0 revision | R1 revision | Verified contracts |
|---|---|---|---:|
| `development-issues` | `sha256:83c8918b9a9d0eda9a394a93063a679dd3c513efc192ba5ead65afd078122464` | `sha256:1949ba52b30030eefcd1facc5815a38c80798972afcda3a68c60ea9d0d5eee0d` | 4 |
| `market-voc` | `sha256:d62177e5798a405308e698e550d5350f7c295e543ecc652e10a7b6784d855f2e` | `sha256:117aa15ec40cd00fe897944094aeaeeb90222ccaf6793e098ae31abab394d9a8` | 5 |

Fresh catalog에서 두 revision이 view definition hash 때문에 바뀌었다. Old runtime과 encoder로
9개 contract를 모두 실행했으며 column, row count와 result hash는 R0 값을 그대로 유지했다.
특히 `market-region-month-unresolved`는 186행과
`sha256:fe472f87adc70e7c5ec0778ee62f0e2c48099bb4d2b2f902b9c78be71d99bc45`를 유지했다.
Support/commerce fixture와 rollback 자료는 변경하거나 삭제하지 않았다.

| Command | Result |
|---|---|
| `uv run query-man-evaluate --root .` | PASS — 16 cases, failure 0 |
| `uv run query-man-verify --root .` | PASS — R1 9/9, 기존 result hash 유지 |
| `uv run pytest tests/test_verified.py tests/test_assurance_cli.py tests/test_revision.py tests/test_quality_level.py -q` | PASS — 26 tests |

## R2 Acceptance

R2의 reader UTC, canonical encoding, revision migration, 11 fixture와 managed current/rollback
재검증 결과는 R2 구현 commit에서 이 문서에 추가한다. Production inventory와 drain 증거가 없는
개발 환경 검증을 production cutover 완료로 해석하지 않는다.
