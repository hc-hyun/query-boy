# Operations Verification — 2026-08-23

## Evidence

| Control | Result |
|---|---|
| JSON log redaction | bearer, secret assignment, SQL literal과 exception detail 비노출 PASS |
| Public health/readiness | source inventory 비노출 PASS |
| Operator health/metrics | source 상태와 metadata/query/shutdown metric 제공 PASS |
| Query correlation | response/audit query ID와 PostgreSQL `application_name` 일치 PASS |
| Graceful drain | 신규 query 거부, grace 이후 active query cancel/rollback PASS |
| Alert thresholds/dashboard fields | [`operations.md`](../operations.md)에 고정 |
| Control backup/restore drill | 5개 table count 일치, 임시 DB 정리 PASS |
| Security automation | dependency, git secret, filesystem/config와 PostgreSQL image scan CI 추가 |
| Update automation | Python, GitHub Actions와 Docker weekly Dependabot 추가 |

```text
./scripts/control-plane-drill.sh PASS
pip-audit: No known vulnerabilities found
gitleaks: no leaks found
Trivy filesystem: 0 vulnerabilities
Trivy PostgreSQL image: OS 0; gosu CVE-2025-68121은 path/만료일이 있는 비도달 예외
ruff: PASS
mypy: 24 source files PASS
pytest: 117 passed, 13 deselected
pytest -m integration: 13 passed, 117 deselected
```
