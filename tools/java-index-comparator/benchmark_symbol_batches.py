#!/usr/bin/env python3
"""Measure qualified Go-to-Symbol batches against captured outline truth.

Experimental only: never replaces outline checks. Requests, payloads and
differences stay in a private database; stdout is aggregate-only.
"""
import argparse
from collections import Counter
import json
from pathlib import Path
import sys
import time

from audit import Fixture, SCHEMA, Unsupported, relative_path
from common import (StreamableHttpMcpClient, ToolError, canonical_json, connect,
                    discover_mcp_url, source_snapshot, stable_id)


def keys(items, root, owner):
    return Counter((item['name'], relative_path(item.get('file', item.get('path')), root), item['line'],
                    item.get('qualifiedName', '')) for item in items
                   if (item.get('qualifiedName') or '').startswith(owner + '.'))


def owners(baseline, root):
    for check in baseline.execute("SELECT subject,expected_json FROM checks WHERE feature='outline' AND verdict='pass' ORDER BY subject"):
        source = baseline.execute('SELECT entries_json FROM source_structures WHERE path=?', (check['subject'],)).fetchone()
        if source is None:
            raise ToolError('independent declaration inventory is missing')
        expected = json.loads(check['expected_json'])
        for entry in json.loads(source[0]):
            if entry['kind'] in {'class', 'interface', 'enum'} and entry.get('qualified_name'):
                owner = entry['qualified_name']
                member_keys = keys(expected, root, owner)
                if member_keys:
                    yield owner, member_keys


def measure(fixture, baseline, maximum):
    results = {kind: {'owners': 0, 'pass': 0, 'mismatch': 0, 'unsupported': 0,
                      'seconds': 0.0, 'members': 0} for kind in ('simple-qualified', 'fully-qualified')}
    selected = 0
    for owner, expected in owners(baseline, fixture.root):
        if selected >= maximum:
            break
        selected += 1
        for kind, query in (('simple-qualified', owner.rsplit('.', 1)[-1] + '.*'),
                            ('fully-qualified', owner + '.*')):
            identity = stable_id([kind, owner])
            with fixture.state:
                fixture.state.execute("INSERT OR IGNORE INTO checks(id,feature,subject) VALUES (?,'symbol-batch-probe',?)",
                                      (identity, kind + ':' + owner))
                fixture.state.execute('DELETE FROM pages WHERE check_id=?', (identity,))
            started = time.perf_counter()
            try:
                found = fixture.paginated(identity, 'ide_find_symbol', {
                    'project_path': str(fixture.root), 'query': query, 'language': 'Java',
                    'scope': 'project_files', 'includeGenerated': False, 'pageSize': 500,
                }, 'symbols')
                actual = keys(found, fixture.root, owner)
                verdict = 'pass' if expected == actual else 'mismatch'
                difference = {'missing': list((expected - actual).elements()),
                              'extra': list((actual - expected).elements())}
            except Unsupported:
                verdict, difference = 'unsupported', {}
            elapsed = time.perf_counter() - started
            item = results[kind]
            item['owners'] += 1
            item[verdict] += 1
            item['members'] += sum(expected.values())
            item['seconds'] += elapsed
            with fixture.state:
                fixture.state.execute("UPDATE checks SET status='complete',verdict=?,diff_json=? WHERE id=?",
                                      (verdict, canonical_json(difference), identity))
    for item in results.values():
        item['seconds'] = round(item['seconds'], 3)
    return results


def measure_names(fixture, baseline, maximum):
    result = {'cases': 0, 'pass': 0, 'mismatch': 0, 'unsupported': 0, 'seconds': 0.0}
    for check in baseline.execute("SELECT id,subject,expected_json FROM checks WHERE feature='symbol' AND verdict='pass' ORDER BY subject"):
        if result['cases'] >= maximum:
            break
        expected = json.loads(check['expected_json'])
        if not expected:
            continue
        name = check['subject']
        def exact(values):
            return Counter((item['name'], relative_path(item.get('file', item.get('path')), fixture.root), item['line'],
                            item.get('qualifiedName') or '') for item in values if item.get('name') == name)
        identity = stable_id(['full-name-probe', name])
        with fixture.state:
            fixture.state.execute("INSERT OR IGNORE INTO checks(id,feature,subject) VALUES (?,'symbol-name-probe',?)", (identity, name))
            fixture.state.execute('DELETE FROM pages WHERE check_id=?', (identity,))
        started = time.perf_counter()
        try:
            found = fixture.paginated(identity, 'ide_find_symbol', {
                'project_path': str(fixture.root), 'query': name, 'language': 'Java',
                'scope': 'project_files', 'includeGenerated': False, 'pageSize': 500,
            }, 'symbols')
            wanted, actual = exact(expected), exact(found)
            verdict = 'pass' if wanted == actual else 'mismatch'
            difference = {'missing': list((wanted - actual).elements()), 'extra': list((actual - wanted).elements())}
        except Unsupported:
            verdict, difference = 'unsupported', {}
        result['seconds'] += time.perf_counter() - started
        result['cases'] += 1
        result[verdict] += 1
        with fixture.state:
            fixture.state.execute("UPDATE checks SET status='complete',verdict=?,diff_json=? WHERE id=?", (verdict, canonical_json(difference), identity))
    result['seconds'] = round(result['seconds'], 3)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--project-root', required=True, type=Path)
    parser.add_argument('--baseline', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--owners', default=10, type=int)
    parser.add_argument('--timeout', default=30.0, type=float)
    parser.add_argument('--strategy', choices=('qualified-wildcard', 'full-names'), default='qualified-wildcard')
    parser.add_argument('--mcp-url')
    parser.add_argument('--mcp-name', default='intellij-index')
    args = parser.parse_args()
    root, output, original = args.project_root.resolve(), args.output.resolve(), args.baseline.resolve()
    if args.owners < 1 or output == original or output == root or root in output.parents:
        parser.error('positive owner count and a distinct output outside the target are required')
    baseline, state = connect(original, read_only=True), connect(output)
    try:
        snapshot = source_snapshot(root)[0]
        metadata = dict(baseline.execute('SELECT key,value FROM metadata'))
        if metadata.get('project_root') != str(root) or metadata.get('snapshot_sha256') != snapshot:
            raise ToolError('baseline target/source fingerprint differs')
        state.executescript(SCHEMA)
        client = StreamableHttpMcpClient(args.mcp_url or discover_mcp_url(args.mcp_name), timeout=args.timeout)
        client.initialize()
        try:
            fixture = Fixture(root, output, output, state, client)
            result = (measure_names(fixture, baseline, args.owners) if args.strategy == 'full-names'
                      else measure(fixture, baseline, args.owners))
        except ToolError as error:
            with state:
                state.execute("INSERT OR REPLACE INTO metadata VALUES ('symbol_batch_error',?)", (canonical_json({
                    'error_type': type(error).__name__, 'cause_type': type(error.__cause__).__name__,
                    'message': str(error)}),))
            raise
        if source_snapshot(root)[0] != snapshot:
            raise ToolError('source changed during symbol batch proof')
        with state:
            state.execute("INSERT OR REPLACE INTO metadata VALUES ('symbol_batch_result',?)", (canonical_json(result),))
        print(canonical_json(result))
        if args.strategy == 'full-names':
            return 0 if result['cases'] and result['pass'] == result['cases'] else 1
        return 0 if any(item['owners'] and item['pass'] == item['owners'] for item in result.values()) else 1
    finally:
        baseline.close()
        state.close()


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except ToolError:
        print(canonical_json({'status': 'error', 'stage': 'symbol batch proof'}), file=sys.stderr)
        raise SystemExit(2)
