# Canonical Time Stability Verification

Date: 2026-08-25

Status: Repository acceptance complete; production cutover pending environment evidence

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

SQL policy version 2 revision은
`sha256:6b68458319a21416e51bf4be059fc55c4e053b45e38e7219956c4ac3725637a6`다.
R1 SQL policy revision
`sha256:83729139d7ccedbe8e299b0c4a8bdefb97d42ca870d5fc3b9c227578c65855d9`와
다르며 old policy 또는 metadata token은 executor 호출 전에 거부된다.

| Source | Pre-R2 revision | R2 revision | Contracts |
|---|---|---|---:|
| `development-issues` | `sha256:1949ba52b30030eefcd1facc5815a38c80798972afcda3a68c60ea9d0d5eee0d` | `sha256:bc8f0e8463bbde03749d7aed3f1d50210f31462f6578814aa6bbaba89753d50b` | 4 |
| `market-voc` | `sha256:117aa15ec40cd00fe897944094aeaeeb90222ccaf6793e098ae31abab394d9a8` | `sha256:5a28fecb7616e9cc35d936b15aa6b903508d84941e5e9317ecb81030632bb799` | 5 |
| `support-tickets` L2 fixture | `sha256:a44d8390727ce5e4bd4778fe000697ba07d728165849cc728331bca68509bfc6` | `sha256:c05ff189380ba5a381f0db48acbde051dbf6349517dc90f7d015d87954ae9ec0` | 1 |
| `commerce-edges` L2 fixture | `sha256:9b7e709a7e1bc035456c8f53e743e0b837e0e27d4dbd8c9b332c216e3f18f1ff` | `sha256:79d17649a865313ded5d07f95a5cb6d1a126cf7fb929ee5a5abc5e6192d568f4` | 1 |

Repository fixture 11개를 모두 R2 code path로 재실행했다. Ordered columns와 row count는 전부
유지됐다. Aware datetime을 직접 반환하는 두 bootstrap hash만 승인된 UTC representation으로
바뀌었다.

| Query | Rows | R2 result hash | Change |
|---|---:|---|---|
| `development-recent-model-issues` | 6 | `sha256:1b98b84944a15bf8f99ea02d65cb42bbf265dbe9a061fa1bc6f041af3e3aaf1a` | same |
| `development-critical-without-cause` | 62 | `sha256:f0fd4d0d5eefe8f39149629c1598bb4a3b0244280ca1a4096852ca08af6ad79a` | aware datetime UTC |
| `development-user-activity` | 18 | `sha256:b5d005c989220ef0ff470a874ea402af3bcdc37d6a05c6bf974b7e240672b698` | same |
| `development-top-issue-type-by-version` | 25 | `sha256:b5361376f8365c7cabfd88c16265822490e4d64417c22c03956bbb35f813dc31` | same |
| `market-model-voc-rate` | 8 | `sha256:d962d4b7293e88654fbecea95896f53ad17a525b4f366f61d2307d70ad44652b` | same |
| `market-devices-without-voc` | 1 | `sha256:d014cdedfb430395e71a57c020b297d9c1c80107af6bd4190b3fbadb1e650990` | same |
| `market-nuri-hinge-voc` | 2 | `sha256:7b255690c9ba4d0aa65837a61ca749e9d4e647f7a722371ff1083502cbb281dc` | same |
| `market-lot-battery-overheat-ratio` | 15 | `sha256:25f43b1690378c680b9e4b790db289384745b94bd3fd1653dcf4f8e96339d33d` | same |
| `market-region-month-unresolved` | 186 | `sha256:c4615c32951acb3e38feab040594d099de2abb79eb165872542c87a3336de732` | Seoul month instant in UTC |
| `support-tickets-by-queue` | 3 | `sha256:fa5cb073ba13fe3a4db2687a82165e4ce7dad74d80f828a817c6ad22c543787d` | same |
| `commerce-order-edge-values` | 4 | `sha256:5cf7f3fb6843b221d22e10e64df21e37341b2471a93429793704a5ac4ee1d4b9` | same |

Reader transaction order는 두 경로 모두 `BEGIN → local UTC setter → existing settings → common
UTC/reader verifier → catalog 또는 resolved-object/EXPLAIN/query`다. Unit failure injection은 setter나
verifier가 실패하면 downstream SQL이 실행되지 않고 비공개 오류/rollback으로 끝남을 고정한다.
Disposable DB는 role default UTC/서울/뉴욕에서 spring/fall DST aware datetime을 같은 UTC value와
byte count 및 exact verified hash
`sha256:20c9ca4c43400d44c101727ec987b0ae379e086146db1f092da13ac737676549`로 만들고 stored naive
datetime/date/time/timetz를 그대로 유지했다. Success, user-SQL
rollback, timeout, task cancel과 operator cancel 뒤 pooled connection은 원래 role default를 되찾았다.

Control DB integration은 같은 query ID의 old/new metadata revision contract 두 row가 함께 존재하고
R1 generation rollback 뒤에도 둘 다 보존됨을 검증했다. Control schema migration은 없었다.

Local Compose cutover에서는 두 R1 replica를 모두 stop하고 `query-man*` source connection 0을 확인한
뒤 R2 image 하나로 두 replica를 함께 recreate했다. 두 `/health`와 MCP exact/load/soak 12개가
통과했다. 이는 operator가 관찰한 manual local sequence이며 별도 production deployment transcript가
아니다. 이 local 증거는 production inventory, backup, route와 fleet drain을 대신하지 않는다.

| Command | Result |
|---|---|
| `uv run ruff check .` | PASS |
| `uv run mypy src` | PASS — 29 source files |
| `uv run pytest` | PASS — 642 passed, 73 deselected |
| `uv run pytest -m integration` | PASS — 61 passed, 654 deselected |
| `uv run pytest tests/test_catalog.py tests/test_query.py tests/test_result_encoding.py tests/test_revision.py tests/test_sql_validation.py -q` | PASS — 195 tests, 2 deselected |
| `uv run query-man-evaluate --root .` | PASS — 16 cases |
| `uv run query-man-verify --root .` | PASS — bootstrap 9/9 |
| Onboarding integration selection | PASS — support/commerce 2/2 |
| `uv run pytest -m integration tests/test_source_database_corners.py -q` | PASS — 6 tests |
| Verified old/new Control row rollback integration | PASS — 1 test |
| `uv run pytest -m mcp_server tests/test_mcp_server.py tests/test_mcp_server_load.py tests/test_mcp_server_soak.py -q` | PASS — 12 tests |
| `scripts/verify-container.sh` | PASS — ready, unauthenticated 401, non-root/read-only image, exact MCP query |

Production cutover는 protected managed contract inventory, 실제 R1 DB migration artifact/ref, old fleet
connection 0과 rollback drill을 환경별 change record로 증명해야 한다. 이 repository 작업은 그
절차와 stop condition을 제공하지만 production external state를 완료로 주장하지 않는다.
