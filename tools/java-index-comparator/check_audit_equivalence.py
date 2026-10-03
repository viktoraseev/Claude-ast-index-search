#!/usr/bin/env python3
"""Check that an optimized audit preserves the prior case population and truth.

Inputs are read-only. All case-level differences go into a private report;
stdout contains counts only. This is not a claim of full feature coverage when
the original audit itself still had pending functionality.
"""
import argparse
import json
from pathlib import Path
import sys

from audit import Unsupported, location_keys
from common import ToolError, canonical_json, connect


SCHEMA = '''
CREATE TABLE IF NOT EXISTS equivalence_metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS equivalence_issues(
 category TEXT NOT NULL,case_id TEXT NOT NULL,detail_json TEXT NOT NULL,
 PRIMARY KEY(category,case_id)
);
'''


def compare(reference_path, evidence_path, output_path):
    reference_path, evidence_path, output_path = (Path(path).resolve() for path in
                                                (reference_path, evidence_path, output_path))
    if output_path in {reference_path, evidence_path}:
        raise ToolError('equivalence report must not overwrite either input')
    reference, current = connect(reference_path, read_only=True), connect(evidence_path, read_only=True)
    report = None
    try:
        before = dict(reference.execute('SELECT key,value FROM metadata'))
        after = dict(current.execute('SELECT key,value FROM metadata'))
        if any(before.get(key) != after.get(key) or not before.get(key)
               for key in ('project_root', 'snapshot_sha256')):
            raise ToolError('audits have different target/source fingerprints')
        root = Path(after['project_root'])
        if output_path == root or root in output_path.parents:
            raise ToolError('equivalence report must be outside the target')
        current.execute('ATTACH DATABASE ? AS reference', (f'file:{reference_path}?mode=ro',))
        report = connect(output_path)
        report.executescript(SCHEMA)
        with report:
            report.execute('DELETE FROM equivalence_issues')
            report.execute("INSERT OR REPLACE INTO equivalence_metadata VALUES ('status','running')")

        def issue(category, identity, detail=None):
            report.execute('INSERT OR REPLACE INTO equivalence_issues VALUES (?,?,?)',
                           (category, identity, canonical_json(detail or {})))

        originals, present = 0, 0
        for row in current.execute('''SELECT r.id,r.feature,r.subject,r.status AS prior_status,r.verdict AS prior_verdict,
            c.id AS current_id,c.feature AS current_feature,c.subject AS current_subject,c.status,c.verdict
            FROM reference.checks r LEFT JOIN main.checks c ON c.id=r.id ORDER BY r.id'''):
            originals += 1
            if row['prior_status'] != 'complete':
                issue('incomplete_reference', row['id'])
            if row['current_id'] is None:
                issue('missing_case', row['id'])
                continue
            present += 1
            if row['feature'] != row['current_feature'] or row['subject'] != row['current_subject']:
                issue('changed_case_contract', row['id'])
            if row['status'] != 'complete':
                issue('unfinished_case', row['id'])
            elif row['verdict'] in {'unsupported', 'error'}:
                issue('unsupported_case', row['id'], {'verdict': row['verdict']})
            elif row['prior_verdict'] == 'pass' and row['verdict'] != 'pass':
                issue('regressed_verdict', row['id'], {'verdict': row['verdict']})
            if originals % 250 == 0:
                report.commit()
        for row in current.execute('''SELECT r.feature,c.status FROM reference.coverage r
            LEFT JOIN main.coverage c ON c.feature=r.feature WHERE r.status='implemented' '''):
            if row['status'] != 'implemented':
                issue('reduced_feature_coverage', row['feature'])

        text_cases = 0
        for row in current.execute('''SELECT r.id,r.expected_json AS original,c.expected_json AS optimized,c.status
            FROM reference.checks r JOIN main.checks c ON c.id=r.id
            WHERE r.feature IN ('annotations','search:content') ORDER BY r.id'''):
            if row['status'] != 'complete':
                continue
            text_cases += 1
            try:
                original, optimized = json.loads(row['original']), json.loads(row['optimized'])
                if not isinstance(original, list) or not isinstance(optimized, list):
                    raise Unsupported('unknown text truth contract')
                expected, actual = location_keys(original, root), location_keys(optimized, root)
                if expected != actual:
                    issue('changed_text_truth', row['id'], {'missing': sorted(expected - actual),
                                                          'extra': sorted(actual - expected)})
            except (TypeError, ValueError, Unsupported):
                issue('unknown_text_truth', row['id'])
            if text_cases % 100 == 0:
                report.commit()
        problems = dict(report.execute('SELECT category,count(*) FROM equivalence_issues GROUP BY category'))
        remaining = current.execute("SELECT count(*) FROM checks WHERE status!='complete'").fetchone()[0]
        result = {'original_cases': originals, 'present_cases': present,
                  'additional_cases': current.execute('''SELECT count(*) FROM main.checks c
                    LEFT JOIN reference.checks r ON r.id=c.id WHERE r.id IS NULL''').fetchone()[0],
                  'text_truth_cases': text_cases, 'issues': problems, 'remaining_checks': remaining,
                  'pending_features_after': current.execute("SELECT count(*) FROM coverage WHERE status='pending'").fetchone()[0],
                  'verified': bool(originals) and not problems and not remaining}
        with report:
            report.execute("INSERT OR REPLACE INTO equivalence_metadata VALUES ('result',?)", (canonical_json(result),))
            report.execute("INSERT OR REPLACE INTO equivalence_metadata VALUES ('status','complete')")
        return result
    finally:
        if report is not None:
            report.close()
        reference.close()
        current.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--reference', required=True, type=Path)
    parser.add_argument('--evidence', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    result = compare(args.reference, args.evidence, args.output)
    print(canonical_json(result))
    return 0 if result['verified'] else 1


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except ToolError:
        print(canonical_json({'status': 'error', 'stage': 'audit equivalence'}), file=sys.stderr)
        raise SystemExit(2)
