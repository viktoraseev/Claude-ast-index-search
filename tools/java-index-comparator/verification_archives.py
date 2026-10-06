"""Bounded, request-bound oracle regressions required before automatic commits."""
import json
from pathlib import Path
import re

from common import ToolError, connect


def preserved_expectations(evidence: Path, verification: Path):
    """Recorded failed-case truth must survive replay, not merely turn green."""
    source, current = connect(evidence, read_only=True), connect(verification, read_only=True)
    compared, changed = 0, 0
    try:
        for row in source.execute("SELECT id,feature,subject,expected_json FROM checks WHERE verdict='fail'"):
            actual = current.execute('SELECT feature,subject,status,expected_json FROM checks WHERE id=?',
                                     (row['id'],)).fetchone()
            compared += 1
            if (actual is None or actual['status'] != 'complete'
                    or actual['feature'] != row['feature'] or actual['subject'] != row['subject']
                    or json.loads(actual['expected_json'] or 'null') != json.loads(row['expected_json'] or 'null')):
                changed += 1
        return {'compared': compared, 'changed': changed, 'preserved': changed == 0}
    finally:
        source.close()
        current.close()


def verify_registered(output: Path, root: Path, binary: Path, directory: Path, replay):
    manifest = output / 'verification-inputs.json'
    if not manifest.exists():
        return None
    with manifest.open('rb') as stream:
        payload = stream.read(1024 * 1024 + 1)
    if len(payload) > 1024 * 1024:
        raise ToolError('verification archive manifest exceeds its bound')
    try:
        value = json.loads(payload)
    except (ValueError, UnicodeError) as error:
        raise ToolError('invalid verification archive manifest') from error
    archives = value.get('archives') if isinstance(value, dict) else None
    if not isinstance(value, dict) or value.get('schema') != 1 or not isinstance(archives, list) or not 1 <= len(archives) <= 20:
        raise ToolError('invalid verification archive list')
    requests, labels = [], set()
    # Validate every request before running any native command.
    for archive in archives:
        if not isinstance(archive, dict) or set(archive) != {'label', 'evidence'}:
            raise ToolError('invalid verification archive entry')
        label, source = archive['label'], archive['evidence']
        if not isinstance(label, str) or not re.fullmatch(r'[a-zA-Z0-9_-]{1,48}', label) or label in labels:
            raise ToolError('invalid or duplicate verification archive label')
        if not isinstance(source, str):
            raise ToolError('invalid verification archive path')
        evidence = Path(source).resolve()
        if not evidence.is_relative_to(output.resolve()) or not evidence.is_file():
            raise ToolError('verification archive escaped the target artifact directory')
        wal = Path(str(evidence) + '-wal')
        if wal.exists() and wal.stat().st_size:
            raise ToolError('verification archive is not a completed snapshot')
        state = connect(evidence, read_only=True)
        try:
            recorded = state.execute("SELECT value FROM metadata WHERE key='project_root'").fetchone()
            if recorded is None or Path(recorded[0]).resolve() != root.resolve():
                raise ToolError('verification archive belongs to another target')
            count = state.execute("SELECT count(*) FROM checks WHERE verdict IN ('fail','unsupported','error')").fetchone()[0]
            if not 1 <= count <= 100:
                raise ToolError('verification archive must contain a bounded problem batch')
        finally:
            state.close()
        labels.add(label)
        requests.append((label, evidence))
    results = []
    for label, evidence in requests:
        result = replay(evidence, root, binary, directory / label)
        verification = Path(result.get('verification', '')).resolve()
        if not verification.is_relative_to((directory / label).resolve()) or not verification.is_file():
            raise ToolError('verification replay did not produce a bounded result database')
        golden = preserved_expectations(evidence, verification)
        result = {**result, 'expected_preservation': golden,
                  'verified': result.get('verified') is True and golden['preserved']}
        results.append({'label': label, 'source_evidence': str(evidence), **result})
    return {'verified': all(result.get('verified') is True for result in results),
            'stage': 'registered-oracle-regressions', 'archives': results,
            'manifest': str(manifest.resolve())}
