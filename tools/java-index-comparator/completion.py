#!/usr/bin/env python3
"""Read-only final evidence gate: a green summary alone cannot authorize a PR."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from common import ToolError, adapter_digest, canonical_json, connect, file_sha256, source_snapshot
from mobile_contracts import inventory_snapshot


class StaleEvidence(ToolError):
    """A fresh audit is needed, not a parser repair or a fabricated pass."""


def verify(evidence: Path, root: Path, binary: Path) -> dict:
    from audit import JAVA_EXCLUDED_FEATURES, required_features, check_scope_filter
    from android_dependency_contracts import LEGACY_FEATURES

    root, binary, evidence = root.resolve(), binary.resolve(), evidence.resolve()
    snapshot, files = source_snapshot(root)
    fingerprints = {'project_root': str(root), 'snapshot_sha256': snapshot,
                    'inventory_sha256': inventory_snapshot(root),
                    'binary_sha256': file_sha256(binary), 'fixture_sha256': adapter_digest()}
    state = connect(evidence, read_only=True)
    try:
        state.execute('BEGIN')
        metadata = dict(state.execute('SELECT key,value FROM metadata'))
        for key, value in fingerprints.items():
            if metadata.get(key) != value:
                raise StaleEvidence('final audit fingerprint changed: ' + key)
        if metadata.get('audit_scope') != 'java' or not files:
            raise ToolError('final evidence is not a nonempty Java audit')
        if metadata.get('java_files') != str(len(files)):
            raise ToolError('final Java source population differs from evidence')
        scope, parameters = check_scope_filter(state)
        total = state.execute(f'SELECT count(*) FROM checks WHERE {scope}', parameters).fetchone()[0]
        unresolved = state.execute(f"SELECT count(*) FROM checks WHERE (status!='complete' OR verdict IS NOT 'pass') AND {scope}", parameters).fetchone()[0]
        if not total or unresolved:
            raise ToolError('final evidence has empty or unresolved checks')
        coverage = {row['feature']: row['status'] for row in state.execute('SELECT feature,status FROM coverage')}
        required = required_features() | JAVA_EXCLUDED_FEATURES | {'search:rank-presets'}
        if required - coverage.keys():
            raise ToolError('final evidence omits required CLI coverage contracts')
        if any(status not in {'implemented', 'inapplicable', 'out-of-scope'} for status in coverage.values()):
            raise ToolError('final evidence has unresolved coverage contracts')
        from parent_acceptance import readiness
        readiness(state, {**metadata, **fingerprints})
        from scope_acceptance import readiness as scope_readiness
        scope_readiness(state, {**metadata, **fingerprints})
        if any(status == 'out-of-scope' and feature not in JAVA_EXCLUDED_FEATURES
               for feature, status in coverage.items()):
            raise ToolError('Java-applicable contract was classified outside the task')
        if any(coverage[feature] != 'out-of-scope' for feature in JAVA_EXCLUDED_FEATURES):
            raise ToolError('non-Java contracts were included in final Java coverage')
        inapplicable = {feature for feature, status in coverage.items() if status == 'inapplicable'}
        if inapplicable - {'xml-usages:target', 'resource-usages:target'}:
            raise ToolError('required Java coverage cannot be dismissed as inapplicable')
        if inapplicable:
            from android_contracts import verify_absence_evidence
            verify_absence_evidence(state)
            absence_value = canonical_json({'absence': True})
            for feature in inapplicable:
                parent = feature.removesuffix(':target')
                absence = state.execute("""SELECT expected_json,actual_json FROM checks
                    WHERE feature=? AND subject='target-absence' AND status='complete' AND verdict='pass'""",
                                        (parent,)).fetchone()
                try:
                    proved = absence is not None and \
                        canonical_json(json.loads(absence['expected_json']).get('samples')) == absence_value and \
                        canonical_json(json.loads(absence['actual_json'])) == absence_value
                except (ValueError, TypeError, AttributeError):
                    proved = False
                if not proved:
                    raise ToolError('inapplicable target has no executed Android absence evidence')
        if state.execute("""SELECT 1 FROM coverage c WHERE c.status='implemented'
            AND NOT EXISTS (SELECT 1 FROM checks k WHERE k.feature=c.feature) LIMIT 1""").fetchone():
            raise ToolError('implemented coverage contract has no executed check')
        # Mixed legacy expectations stay immutable, including past verdicts.
        # Their new required Java projections are checked above; historical
        # XML/mixed rows do not contribute to Java counts or readiness.
        legacy = tuple(sorted(LEGACY_FEATURES))
        if state.execute("""SELECT 1 FROM checks k JOIN coverage c USING(feature)
            WHERE c.status='out-of-scope' AND k.verdict='pass'
            AND k.feature NOT IN (?,?) LIMIT 1""", legacy).fetchone():
            raise ToolError('foreign checks cannot count toward a final Java audit')
        if state.execute("""SELECT 1 FROM checks k LEFT JOIN coverage c USING(feature)
            WHERE c.feature IS NULL LIMIT 1""").fetchone():
            raise ToolError('executed check has no declared coverage contract')
        # No payload leaves this gate; the database is streamed and left intact.
        return {'verified': True, 'scope': 'java', 'checks': total, 'java_files': len(files),
                'implemented_features': sum(status == 'implemented' for status in coverage.values()),
                'inapplicable_features': sum(status == 'inapplicable' for status in coverage.values()),
                'out_of_scope_features': sum(status == 'out-of-scope' for status in coverage.values()),
                'evidence': str(evidence), **fingerprints}
    finally:
        state.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--evidence', type=Path, required=True)
    parser.add_argument('--project-root', type=Path, required=True)
    parser.add_argument('--ast-index', type=Path, required=True)
    arguments = parser.parse_args()
    try:
        print(canonical_json(verify(arguments.evidence, arguments.project_root, arguments.ast_index)))
        return 0
    except ToolError as error:
        print(canonical_json({'verified': False, 'error': str(error)}))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
