# ADR 0033: Explicit Source TLS Modes

Status: Accepted

Decision ID: `QB-SOURCE-TLS-MODES-20260831`

## Context

Boolean TLS 설정은 PostgreSQL의 암호화, CA 검증과 hostname 검증 차이를 표현하지 못합니다. 암묵적
driver 기본값도 배포마다 다른 transport를 만들 수 있습니다.

## Decision

각 source manifest는 `connection.sslmode`를 명시하며 다음 값만 허용합니다.

- `disable`: 암호화하지 않는 승인된 local/test 경계
- `require`: TLS를 요구하지만 hostname을 검증하지 않는 compatibility 경계
- `verify-full`: CA chain과 hostname을 모두 검증하는 protected 기본 목표

Runtime은 선택한 mode를 metadata/query pool 모두에 동일하게 전달합니다. `verify-full`에는 읽을 수 있는
CA bundle이 필요하고, `require` 사용은 hostname-risk 승인과 CA/SAN 개선 조건을 남겨야 합니다. Unknown
mode나 필요한 CA가 없으면 startup/direct admission을 fail-closed합니다.

## 변경과 rollback

TLS mode, CA 또는 target hostname 변경은 traffic 밖에서 PostgreSQL identity, encryption과 negative
certificate/hostname probe를 확인합니다. 실패하면 연결을 평문이나 약한 mode로 자동 downgrade하지 않고
직전 approved config/route로 rollback합니다. Password와 CA private material은 repository나 log에 넣지
않습니다.
