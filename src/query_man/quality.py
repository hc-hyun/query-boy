from __future__ import annotations

import argparse
import asyncio
import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Annotated, Literal, cast

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from query_man.catalog import PostgresCatalog
from query_man.metadata import MetadataService
from query_man.registry import SourceRegistry

Identifier = Annotated[str, Field(pattern=r"^[a-z][a-z0-9-]{0,99}$")]
RelationName = Annotated[str, Field(pattern=r"^[A-Za-z_][A-Za-z0-9_$]*\.[A-Za-z_][A-Za-z0-9_$]*$")]
Answerability = Literal["best_effort", "low_confidence", "needs_clarification", "unsupported"]


class QualityConfigurationError(Exception):
    pass


class QualityGateError(Exception):
    def __init__(self, report: QualityReport) -> None:
        super().__init__("Metadata quality gates failed")
        self.report = report


@dataclass(frozen=True)
class QualityCase:
    case_id: str
    source_id: str
    question: str
    expected_relations: tuple[str, ...]
    expected_answerability: Answerability | None


@dataclass(frozen=True)
class QualityGates:
    min_relation_accuracy: float
    min_answerability_recall: float
    max_context_bytes: int


@dataclass(frozen=True)
class QualityReport:
    case_count: int
    relation_accuracy: float
    answerability_recall: float
    max_context_bytes: int
    average_context_bytes: int
    failures: tuple[str, ...]


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class _Gates(_StrictModel):
    min_relation_accuracy: float = Field(ge=0, le=1)
    min_answerability_recall: float = Field(ge=0, le=1)
    max_context_bytes: int = Field(ge=1_024, le=100 * 1_024 * 1_024)


class _Case(_StrictModel):
    case_id: Identifier
    source_id: Identifier
    question: str = Field(min_length=1, max_length=2_000)
    expected_relations: list[RelationName] = Field(min_length=1, max_length=100)
    expected_answerability: Answerability | None = None


class _QualityFile(_StrictModel):
    version: int
    gates: _Gates
    cases: list[_Case] = Field(min_length=1, max_length=10_000)

    @model_validator(mode="after")
    def valid_file(self) -> _QualityFile:
        if self.version != 1:
            raise ValueError("version must be 1")
        case_ids = [case.case_id for case in self.cases]
        if len(set(case_ids)) != len(case_ids):
            raise ValueError("case_id values must be unique")
        return self


class QualityEvaluation:
    def __init__(self, gates: QualityGates, cases: list[QualityCase]) -> None:
        self.gates = gates
        self.cases = tuple(cases)

    @classmethod
    def load(cls, path: Path, known_sources: set[str]) -> QualityEvaluation:
        try:
            with path.open(encoding="utf-8") as stream:
                parsed = _QualityFile.model_validate(yaml.safe_load(stream))
        except (OSError, yaml.YAMLError, ValidationError) as error:
            raise QualityConfigurationError(f"Invalid quality evaluation in {path}: {error}") from error
        unknown = {case.source_id for case in parsed.cases} - known_sources
        if unknown:
            raise QualityConfigurationError("Quality evaluation references an unknown source")
        return cls(
            QualityGates(**parsed.gates.model_dump()),
            [
                QualityCase(
                    case.case_id,
                    case.source_id,
                    case.question,
                    tuple(case.expected_relations),
                    case.expected_answerability,
                )
                for case in parsed.cases
            ],
        )

    async def evaluate(self, metadata: MetadataService) -> QualityReport:
        relation_matches = 0
        answerability_matches = 0
        answerability_cases = 0
        sizes: list[int] = []
        failures: list[str] = []
        for case in self.cases:
            response = await metadata.get_context(case.source_id, case.question)
            relation_values = response["relations"]
            assert isinstance(relation_values, list)
            assert all(isinstance(item, dict) for item in relation_values)
            relations = cast(list[dict[str, object]], relation_values)
            actual_relations = tuple(str(item["name"]) for item in relations)
            if actual_relations == case.expected_relations:
                relation_matches += 1
            else:
                failures.append(f"{case.case_id}: relation mismatch")
            if case.expected_answerability is not None:
                answerability_cases += 1
                answerability = response["answerability"]
                assert isinstance(answerability, dict)
                actual_status = cast(dict[str, object], answerability)["status"]
                if actual_status == case.expected_answerability:
                    answerability_matches += 1
                else:
                    failures.append(f"{case.case_id}: answerability mismatch")
            sizes.append(
                len(json.dumps(response, ensure_ascii=False, separators=(",", ":")).encode())
            )
        relation_accuracy = relation_matches / len(self.cases)
        answerability_recall = (
            answerability_matches / answerability_cases if answerability_cases else 1.0
        )
        maximum = max(sizes)
        if relation_accuracy < self.gates.min_relation_accuracy:
            failures.append("relation accuracy gate failed")
        if answerability_recall < self.gates.min_answerability_recall:
            failures.append("answerability recall gate failed")
        if maximum > self.gates.max_context_bytes:
            failures.append("context byte gate failed")
        report = QualityReport(
            case_count=len(self.cases),
            relation_accuracy=relation_accuracy,
            answerability_recall=answerability_recall,
            max_context_bytes=maximum,
            average_context_bytes=round(sum(sizes) / len(sizes)),
            failures=tuple(failures),
        )
        if failures:
            raise QualityGateError(report)
        return report


def main() -> None:
    parser = argparse.ArgumentParser(description="Evaluate metadata retrieval quality gates")
    parser.add_argument("--root", type=Path, default=Path.cwd())
    arguments = parser.parse_args()
    asyncio.run(_run(arguments.root.resolve()))


async def _run(root: Path) -> None:
    load_dotenv(root / ".env")
    registry = SourceRegistry.load(
        root / "config" / "sources",
        root / "config" / "budget-profiles.yaml",
    )
    evaluation = QualityEvaluation.load(
        root / "config" / "quality-evaluation.yaml",
        {source["source_id"] for source in registry.list()},
    )
    catalog = PostgresCatalog()
    metadata = MetadataService(registry, catalog)
    try:
        try:
            report = await evaluation.evaluate(metadata)
        except QualityGateError as error:
            print(json.dumps({"status": "failed", **asdict(error.report)}, sort_keys=True))
            raise SystemExit(1) from error
        print(json.dumps({"status": "ok", **asdict(report)}, sort_keys=True))
    finally:
        await catalog.close()
