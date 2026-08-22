# Metadata Quality Verification — 2026-08-23

## Scope

Revision-scoped token/IDF retrieval index를 전체 golden 질문, word-order가 다른 paraphrase,
unsupported/clarification 규칙과 context byte gate로 검증했다.

## Versioned Gates

| Metric | Gate | Observed |
|---|---:|---:|
| Relation exact-match accuracy | 1.0 | 1.0 (16/16) |
| Unsupported/clarification recall | 1.0 | 1.0 (3/3) |
| Maximum context bytes | 65,536 이하 | 13,488 |
| Average context bytes | 관측 | 7,871 |

16개 case에는 verified golden question 9개, paraphrase와 answerability negative case가 포함된다.
잘못된 relation/status와 70KB context를 반환하는 negative test는 세 gate가 모두 실패하고
`QualityGateError`와 CLI non-zero 종료로 이어지는지 확인한다.

## Commands

```text
uv run query-man-evaluate
{"status":"ok","case_count":16,"relation_accuracy":1.0,
 "answerability_recall":1.0,"max_context_bytes":13488,
 "average_context_bytes":7871,"failures":[]}
```

`.github/workflows/ci.yml`은 locked uv environment와 deterministic PostgreSQL fixture를 만든
뒤 unit/integration, quality gate와 9/9 verified SQL을 모두 실행한다.
