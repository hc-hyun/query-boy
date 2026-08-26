# Verified Query: 결과가 달라지지 않았는지 확인하는 회귀검사

Status: ADR 0025 static launch에서 사용하는 9개 검사 항목; managed 저장소는 현재 비활성

## 30초 설명

Verified query는 이미 검토한 질문과 SQL을 다시 실행해 결과가 예전과 같은지 확인하는
**회귀검사 항목**이다. 데이터 구조, metadata, 실행 정책이나 예제 데이터가 바뀌어 기존 답이 달라지면
배포 전에 발견하는 것이 목적이다.

> Verified query는 실행을 허용할 SQL 목록이 아니다. 이 file에 없는 SQL도 현재 안전 정책과
> source 권한, 자원 제한을 통과하면 실행할 수 있다. 반대로 이 file에 있다는 이유로 안전
> 검사를 건너뛰지도 않는다.

현재 기준은 [ADR 0025](decisions/0025-static-non-rls-first-launch.md)다. RLS source는 전면
격리하므로 성공하는 verified-query case가 없다.

## 현재 9개 검사

실행 데이터는 [`config/verified-queries.yaml`](../config/verified-queries.yaml)에 있다.

| Source | 개수 | 확인하는 질문 |
|---|---:|---|
| `development-issues` | 4 | 최근 모델별 문제, 원인 없는 중요 문제, 사용자 활동, HW/SW별 최다 문제 유형 |
| `market-voc` | 5 | 모델별 VOC 비율, VOC 없는 기기, NURI 힌지, 제조 lot별 배터리·과열 비율, 지역·월별 미해결 추이 |

각 검사 항목은 다음을 함께 보존한다.

- 고유한 query ID와 source ID
- 사용자가 물은 질문과 결정적인 read-only SQL
- SQL을 만들 때 사용한 정확한 metadata revision과 relation 집합
- 결과 column 순서, row 수와 결과 값으로 만든 SHA-256 지문(hash)

결과 값은 통째로 복사하지 않고 지문만 baseline에 저장한다. 시간 상대 질문은 회귀 결과가 실행
날짜에 따라 움직이지 않도록 SQL의 기준일을 고정한다. 실제 Text-to-SQL 요청은 현재 시각을 사용한다.

## 무엇을 비교하나요?

`uv run query-man-verify`는 검사 항목마다 다음 순서로 확인한다. Metadata revision은 SQL을 만들 때 본
데이터 구조 설명의 정확한 버전이다.

1. 현재 발행된 metadata revision이 기록된 revision과 같은지 확인한다.
2. SQL을 다시 검증하고 실제 참조 relation이 기록과 같은지 확인한다.
3. 일반 요청과 같은 Guarded Query 경로로 SQL을 실행한다.
4. 결과가 잘리지 않았는지 확인한 뒤 column 순서, row 수와 result hash를 비교한다.
5. 하나라도 다르면 command가 실패한다.

실패는 “새 결과가 틀렸다”는 자동 판정이 아니다. 데이터 구조·예제 데이터·metadata·SQL·정책 중 무엇이
바뀌었는지 조사하라는 신호다. 의도한 변경이라도 기존 값을 자동 갱신하지 않고 새 revision과
expected hash를 검토한다. 통과 역시 모든 질문의 정답이나 production 데이터 전체의 정확성을
보증하지 않는다.

```bash
uv run query-man-verify
```

이 command는 repository의 static dataset만 검사한다. Managed inventory를 검사하거나 Control DB로
import하지 않는다.

## 결과가 같은지 판단하는 기술 기준

Result hash는 Guarded Query가 반환한 canonical JSON scalar와 ordered columns/rows로 계산한다.
현재 final result는 PostgreSQL base OID `20, 21, 23, 25, 1082, 1184, 1700`만 허용한다.
`numeric`은 scale을 보존한 문자열이고, aware datetime은 `Z`가 아닌 UTC `+00:00` ISO 문자열이다.
Date는 기존 ISO 표현을 유지한다. Boolean, bytea, JSON, float, array 등 다른 final OID는 첫 fetch 전에
거부된다.

Source execution budget이나 revision-scoped source policy가 바뀌면 metadata revision도 바뀐다.
Canonical-time material은 SQL policy와 모든 metadata revision에 포함된다. 이 의미가 바뀌면 새 exact
revision에서 bootstrap 9개 전체와 managed current/rollback-preserved baseline 전체를 다시 실행한다.
기존 immutable record를 수정·삭제하거나 membership을 자동 승계하지 않는다. Application 전역 SQL
policy 변경도 별도 release regression으로 검증한다.

## Static 저장소와 비활성 managed 저장소

현재 static launch는 Git이 immutable history와 rollback을 제공하는 version 1 file만 읽는다. 별도로
managed mode를 활성화하면 이 file을 읽거나 합치지 않고 Control DB의 immutable
`control.verified_query_contracts`만 L2 evidence로 사용한다.

두 저장소 사이에는 startup 자동 import, fallback, merge나 write-back이 없다. 향후 기존 검사 항목을
managed source로 옮길 때도 traffic 밖에서 source를 L0/L1으로 먼저 publish하고, 정확한 revision의
record를 admin endpoint로 저장한다. 같은 Guarded Query 경로로 결과를 확인한 뒤에만 L2로 승격한다.
자세한 소유 경계는
[Assurance module](modules/assurance/README.md)을 따른다.
