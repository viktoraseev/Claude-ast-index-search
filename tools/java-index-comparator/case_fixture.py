#!/usr/bin/env python3
"""Stream comparison cases through one strict, extensible test fixture.

"Supported" means that a feature handler consumed both stored sides and every
diff atom without a fallback.  It does not mean that ast-index already agrees
with the MCP reference result: mismatch cases are expected to remain failures
until the implementation is fixed.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
from pathlib import Path
import sqlite3
import sys
from typing import Any, Callable, Iterable, Iterator

from common import ToolError, canonical_json


Json = Any
Validator = Callable[[Json, str], None]


@dataclass(frozen=True)
class DatabaseCase:
    case_id: str
    feature: str
    subject: str
    verdict: str
    missing_count: int
    unexpected_count: int
    oracle: Json
    actual: Json
    diff: Json


@dataclass(frozen=True)
class AssertionAtom:
    direction: str
    value: Json


@dataclass(frozen=True)
class CaseOutcome:
    case_id: str
    feature: str
    subject: str
    supported: bool
    handler: str | None
    assertion_count: int
    reason: str | None
    shape: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "case_id": self.case_id,
            "feature": self.feature,
            "subject": self.subject,
            "supported": self.supported,
            "handler": self.handler,
            "assertion_count": self.assertion_count,
            "reason": self.reason,
            "shape": self.shape,
        }


class UnsupportedCase(ValueError):
    pass


def require(condition: bool, message: str) -> None:
    if not condition:
        raise UnsupportedCase(message)


def require_list(value: Json, path: str) -> list[Json]:
    require(isinstance(value, list), f"{path} must be an array")
    return value


def require_object(value: Json, path: str) -> dict[str, Json]:
    require(isinstance(value, dict), f"{path} must be an object")
    return value


def validate_string(value: Json, path: str) -> None:
    require(isinstance(value, str), f"{path} must be a string")


def validate_location(value: Json, path: str) -> None:
    item = require_list(value, path)
    require(len(item) == 2, f"{path} must be [file, line]")
    require(isinstance(item[0], str), f"{path}[0] must be a file string")
    require(isinstance(item[1], int) and item[1] > 0, f"{path}[1] must be a positive line")


def validate_declaration(value: Json, path: str) -> None:
    item = require_list(value, path)
    require(len(item) == 5, f"{path} must be [name, kind, file, line, qualified_name]")
    require(all(isinstance(item[index], str) for index in (0, 1, 2)), f"{path} has invalid strings")
    require(isinstance(item[3], int) and item[3] > 0, f"{path}[3] must be a positive line")
    require(item[4] is None or isinstance(item[4], str), f"{path}[4] must be string or null")


def validate_ref(value: Json, path: str) -> None:
    item = require_list(value, path)
    require(len(item) == 3, f"{path} must be [role, file, line]")
    require(item[0] in {"definition", "import", "usage"}, f"{path}[0] has unknown ref role")
    require(isinstance(item[1], str), f"{path}[1] must be a file string")
    require(isinstance(item[2], int) and item[2] > 0, f"{path}[2] must be a positive line")


def validate_hierarchy(value: Json, path: str) -> None:
    item = require_list(value, path)
    require(bool(item), f"{path} must not be empty")
    if item[0] == "parent":
        require(len(item) == 2 and isinstance(item[1], str), f"{path} has invalid parent edge")
    elif item[0] == "child":
        require(len(item) == 3, f"{path} must be [child, file, line]")
        require(isinstance(item[1], str), f"{path}[1] must be a file string")
        require(isinstance(item[2], int) and item[2] > 0, f"{path}[2] must be a positive line")
    else:
        raise UnsupportedCase(f"{path}[0] has unknown hierarchy role")


def validate_object_array(
    value: Json,
    path: str,
    required_any: tuple[tuple[str, ...], ...],
) -> None:
    for index, raw in enumerate(require_list(value, path)):
        item = require_object(raw, f"{path}[{index}]")
        require(
            any(all(key in item for key in alternative) for alternative in required_any),
            f"{path}[{index}] does not match a known object shape",
        )


def validate_declaration_side(value: Json, path: str) -> None:
    validate_object_array(value, path, (("name", "kind", "file", "line"), ("name", "kind", "path", "line")))


def validate_location_side(value: Json, path: str) -> None:
    validate_object_array(value, path, (("file", "line"), ("path", "line")))


def validate_callers_actual(value: Json, path: str) -> None:
    for index, item in enumerate(require_list(value, path)):
        validate_location(item, f"{path}[{index}]")


def validate_refs_side(value: Json, path: str) -> None:
    item = require_object(value, path)
    require(set(item) == {"definitions", "usages"}, f"{path} has unknown keys: {sorted(item)}")
    validate_declaration_side(item["definitions"], f"{path}.definitions")
    validate_location_side(item["usages"], f"{path}.usages")


def validate_hierarchy_side(value: Json, path: str) -> None:
    item = require_object(value, path)
    require(set(item) == {"parents", "children"}, f"{path} has unknown keys: {sorted(item)}")
    for index, parent in enumerate(require_list(item["parents"], f"{path}.parents")):
        validate_string(parent, f"{path}.parents[{index}]")
    for index, child in enumerate(require_list(item["children"], f"{path}.children")):
        validate_location(child, f"{path}.children[{index}]")


@dataclass(frozen=True)
class FeatureHandler:
    name: str
    features: frozenset[str]
    oracle_validator: Validator
    actual_validator: Validator
    atom_validator: Validator

    def assertions(self, case: DatabaseCase) -> list[AssertionAtom]:
        self.oracle_validator(case.oracle, "oracle_json")
        self.actual_validator(case.actual, "ast_index_json")
        diff = require_object(case.diff, "diff_json")
        require(set(diff) == {"missing", "unexpected"}, f"diff_json has unknown keys: {sorted(diff)}")
        missing = require_list(diff["missing"], "diff_json.missing")
        unexpected = require_list(diff["unexpected"], "diff_json.unexpected")
        require(len(missing) == case.missing_count, "missing_count does not match diff_json")
        require(len(unexpected) == case.unexpected_count, "unexpected_count does not match diff_json")
        require(bool(missing or unexpected), "mismatch case contains no diff atoms")
        result: list[AssertionAtom] = []
        for direction, values in (("missing", missing), ("unexpected", unexpected)):
            for index, value in enumerate(values):
                self.atom_validator(value, f"diff_json.{direction}[{index}]")
                result.append(AssertionAtom(direction, value))
        return result


HANDLERS = (
    FeatureHandler(
        "file-inventory-v1", frozenset({"file"}),
        lambda value, path: [validate_string(item, f"{path}[{index}]") for index, item in enumerate(require_list(value, path))],
        lambda value, path: [validate_string(item, f"{path}[{index}]") for index, item in enumerate(require_list(value, path))],
        validate_string,
    ),
    FeatureHandler(
        "java-declaration-v1", frozenset({"symbol", "class", "outline"}),
        validate_declaration_side, validate_declaration_side, validate_declaration,
    ),
    FeatureHandler(
        "java-location-v1", frozenset({"usages", "imports", "implementations"}),
        validate_location_side, validate_location_side, validate_location,
    ),
    FeatureHandler(
        "java-callers-v1", frozenset({"callers"}),
        validate_location_side, validate_callers_actual, validate_location,
    ),
    FeatureHandler(
        "java-refs-v1", frozenset({"refs"}),
        validate_refs_side, validate_refs_side, validate_ref,
    ),
    FeatureHandler(
        "java-hierarchy-v1", frozenset({"hierarchy"}),
        validate_hierarchy_side, validate_hierarchy_side, validate_hierarchy,
    ),
)


HANDLER_BY_FEATURE = {
    feature: handler
    for handler in HANDLERS
    for feature in handler.features
}


def json_shape(value: Json) -> str:
    if isinstance(value, dict):
        return "object(" + ",".join(sorted(value)) + ")"
    if isinstance(value, list):
        kinds = sorted({json_shape(item) for item in value[:20]})
        return "array(" + "|".join(kinds) + ")"
    if value is None:
        return "null"
    return type(value).__name__


def parse_case(row: sqlite3.Row) -> DatabaseCase:
    try:
        oracle = json.loads(row["oracle_json"])
        actual = json.loads(row["ast_index_json"])
        diff = json.loads(row["diff_json"])
    except json.JSONDecodeError as error:
        raise UnsupportedCase(f"invalid JSON: {error}") from error
    return DatabaseCase(
        case_id=str(row["id"]),
        feature=str(row["feature"]),
        subject=str(row["subject_key"]),
        verdict=str(row["verdict"]),
        missing_count=int(row["missing_count"]),
        unexpected_count=int(row["unexpected_count"]),
        oracle=oracle,
        actual=actual,
        diff=diff,
    )


def process_case(case: DatabaseCase) -> CaseOutcome:
    shape = f"oracle={json_shape(case.oracle)} actual={json_shape(case.actual)} diff={json_shape(case.diff)}"
    handler = HANDLER_BY_FEATURE.get(case.feature)
    if handler is None:
        return CaseOutcome(case.case_id, case.feature, case.subject, False, None, 0, "no feature handler", shape)
    try:
        require(case.verdict == "mismatch", f"unsupported verdict: {case.verdict}")
        atoms = handler.assertions(case)
    except UnsupportedCase as error:
        return CaseOutcome(
            case.case_id, case.feature, case.subject, False, handler.name, 0, str(error), shape
        )
    return CaseOutcome(
        case.case_id, case.feature, case.subject, True, handler.name, len(atoms), None, shape
    )


def open_cases(path: Path) -> sqlite3.Connection:
    if not path.is_file():
        raise ToolError(f"comparison database does not exist: {path}")
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    columns = {
        str(row[1]) for row in connection.execute("PRAGMA table_info(cases)")
    }
    required = {
        "id", "feature", "subject_key", "verdict", "missing_count",
        "unexpected_count", "oracle_json", "ast_index_json", "diff_json",
    }
    if not required <= columns:
        connection.close()
        raise ToolError(f"cases table is missing columns: {sorted(required - columns)}")
    return connection


def iter_cases(
    connection: sqlite3.Connection,
    *,
    after_id: str | None = None,
    limit: int | None = None,
) -> Iterator[sqlite3.Row]:
    sql = "SELECT * FROM cases"
    parameters: list[Any] = []
    if after_id is not None:
        sql += " WHERE id > ?"
        parameters.append(after_id)
    sql += " ORDER BY id"
    if limit is not None:
        sql += " LIMIT ?"
        parameters.append(limit)
    cursor = connection.execute(sql, parameters)
    while rows := cursor.fetchmany(1000):
        yield from rows


def run_fixture(
    database: Path,
    *,
    after_id: str | None = None,
    limit: int | None = None,
) -> list[CaseOutcome]:
    connection = open_cases(database)
    try:
        outcomes: list[CaseOutcome] = []
        for row in iter_cases(connection, after_id=after_id, limit=limit):
            try:
                outcome = process_case(parse_case(row))
            except UnsupportedCase as error:
                outcome = CaseOutcome(
                    str(row["id"]), str(row["feature"]), str(row["subject_key"]),
                    False, None, 0, str(error), "unparseable",
                )
            outcomes.append(outcome)
        return outcomes
    finally:
        connection.close()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--comparison-db", required=True)
    parser.add_argument("--limit", type=int)
    parser.add_argument("--after-id")
    parser.add_argument("--json", action="store_true")
    return parser.parse_args()


def main() -> int:
    arguments = parse_args()
    try:
        outcomes = run_fixture(
            Path(arguments.comparison_db).expanduser().resolve(),
            after_id=arguments.after_id,
            limit=arguments.limit,
        )
    except (ToolError, sqlite3.Error) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2

    supported = sum(outcome.supported for outcome in outcomes)
    assertions = sum(outcome.assertion_count for outcome in outcomes)
    summary = {
        "total": len(outcomes),
        "supported": supported,
        "unsupported": len(outcomes) - supported,
        "assertions": assertions,
    }
    if arguments.json:
        print(canonical_json({"cases": [outcome.as_dict() for outcome in outcomes], "summary": summary}))
    else:
        for outcome in outcomes:
            status = "SUPPORTED" if outcome.supported else "UNSUPPORTED"
            detail = (
                f"handler={outcome.handler} assertions={outcome.assertion_count}"
                if outcome.supported
                else f"handler={outcome.handler or '-'} reason={outcome.reason}"
            )
            print(f"{status} {outcome.case_id} feature={outcome.feature} subject={outcome.subject!r} {detail}")
        print(
            f"SUMMARY total={summary['total']} supported={summary['supported']} "
            f"unsupported={summary['unsupported']} assertions={summary['assertions']}"
        )
    return 0 if supported == len(outcomes) else 1


if __name__ == "__main__":
    raise SystemExit(main())
