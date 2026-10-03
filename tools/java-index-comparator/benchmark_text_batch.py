#!/usr/bin/env python3
"""Prove per-file MCP text acquisition against recorded literal searches.

Experimental, NOT an audit replacement. Raw responses and differences stay in
the private output database. Sources/previous evidence are opened read-only.
"""
import argparse
import json
from pathlib import Path
import sys
import time

from audit import Fixture, SCHEMA, Unsupported, location_keys, relative_path
from common import (StreamableHttpMcpClient, ToolError, canonical_json, connect,
                    discover_mcp_url, source_snapshot, stable_id)


def capture_file(fixture, relative, maximum_bytes=8 * 1024 * 1024):
    path = fixture.root / relative
    if path.stat().st_size > maximum_bytes:
        raise Unsupported('batch prototype file exceeds bounded source budget')
    data = path.read_bytes()
    if len(data) > maximum_bytes:
        raise Unsupported('batch prototype file exceeds bounded source budget')
    # Strict decoding: replacing bytes could fabricate matches.
    lines = data.decode('utf-8').split('\n')
    lines = [line.removesuffix('\r') for line in lines]
    expected = {(relative, index) for index, line in enumerate(lines, 1) if line}
    identity = stable_id(['batch-text-file', relative])
    with fixture.state:
        fixture.state.execute("INSERT OR IGNORE INTO checks(id,feature,subject) VALUES (?,'batch-text',?)",
                              (identity, relative))
        fixture.state.execute('DELETE FROM pages WHERE check_id=?', (identity,))
        matches = fixture.paginated(identity, 'ide_search_text', {
            'project_path': str(fixture.root), 'paths': [relative],
            'filePattern': '*.java', 'query': '(?m)^.+$', 'regex': True,
            'caseSensitive': True, 'context': 'all', 'pageSize': 500,
        }, 'matches')
        actual = location_keys(matches, fixture.root)
        if actual != expected or any(relative_path(item.get('file', item.get('path')), fixture.root) != relative
                                     for item in matches):
            raise Unsupported('batch MCP did not confirm every nonempty source line exactly')
        # Position coverage alone cannot detect unsaved/stale IDE text. The
        # server's full-line regex previews must confirm source contents too.
        if any(item.get('context') != lines[item['line'] - 1].strip() for item in matches):
            raise Unsupported('batch MCP line contents differ from the source snapshot')
        if path.read_bytes() != data:
            raise Unsupported('source changed while capturing batch evidence')
        fixture.state.execute('DELETE FROM batch_lines WHERE path=?', (relative,))
        fixture.state.executemany('INSERT INTO batch_lines VALUES (?,?,?)',
                                  ((relative, index, line) for index, line in enumerate(lines, 1) if line))
        fixture.state.execute("UPDATE checks SET status='complete',verdict='pass' WHERE id=?", (identity,))
    return len(matches)


def compare_recorded(baseline, output, root, selected):
    """One recorded check at a time; never load all replies/source into RAM."""
    totals = {'cases': 0, 'pass': 0, 'mismatch': 0, 'unsupported': 0,
              'recorded_pages': 0, 'recorded_requests': 0}
    for check in baseline.execute("SELECT id,feature,subject,verdict FROM checks WHERE status='complete' "
                                  "AND feature IN ('search:content','annotations') ORDER BY id"):
        expected, pages, unsupported = set(), 0, check['verdict'] in {'unsupported', 'error'}
        query = '@' + check['subject'].lstrip('@') if check['feature'] == 'annotations' else check['subject']
        for page in baseline.execute('SELECT request_json,response_json FROM pages WHERE check_id=? ORDER BY page',
                                      (check['id'],)):
            request, response = json.loads(page[0]), json.loads(page[1])
            pages += 1
            # Only the exact literal contract being optimized is eligible.
            if (('cursor' not in request and
                 (request.get('query') != query or request.get('regex', False) or
                  not request.get('caseSensitive', False) or request.get('context') != 'all' or
                  request.get('filePattern') != '*.java')) or
                response.get('stale') or response.get('truncated') or
                (response.get('hasMore') and not response.get('nextCursor'))):
                unsupported = True
            try:
                keys = location_keys(response['matches'], root)
            except (KeyError, TypeError, Unsupported):
                unsupported = True
                continue
            expected.update(key for key in keys if key[0] in selected)
            if 'cursor' not in request:
                output.execute('INSERT OR IGNORE INTO batch_requests VALUES (?)', (canonical_json(request),))
            # Same ambiguity as production pagination: absent cursor at the
            # hard collection cap must not be reported as complete evidence.
            if len(response['matches']) >= 5000 and not response.get('nextCursor'):
                unsupported = True
        totals['cases'] += 1
        totals['recorded_pages'] += pages
        if unsupported or not pages or not query:
            verdict = 'unsupported'
            difference = {}
        else:
            actual = {(row[0], row[1]) for row in output.execute(
                'SELECT path,line FROM batch_lines WHERE instr(content,?)>0', (query,))}
            verdict = 'pass' if actual == expected else 'mismatch'
            difference = {'missing': sorted(expected - actual), 'extra': sorted(actual - expected)}
        totals[verdict] += 1
        with output:
            output.execute('INSERT OR REPLACE INTO batch_comparisons VALUES (?,?,?)',
                           (check['id'], verdict, canonical_json(difference)))
    totals['recorded_requests'] = output.execute('SELECT count(*) FROM batch_requests').fetchone()[0]
    return totals


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--project-root', required=True, type=Path)
    parser.add_argument('--baseline', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--files', default=10, type=int)
    parser.add_argument('--mcp-url')
    parser.add_argument('--mcp-name', default='intellij-index')
    args = parser.parse_args()
    root, destination, baseline_path = args.project_root.resolve(), args.output.resolve(), args.baseline.resolve()
    if args.files < 1 or destination == baseline_path or destination == root or root in destination.parents:
        parser.error('positive file count and a distinct output outside the target are required')
    baseline = connect(baseline_path, read_only=True)
    state = connect(destination)
    try:
        snapshot, sources = source_snapshot(root)
        metadata = dict(baseline.execute('SELECT key,value FROM metadata'))
        if metadata.get('project_root') != str(root) or metadata.get('snapshot_sha256') != snapshot:
            raise ToolError('baseline target/source fingerprint differs')
        state.executescript(SCHEMA + '''
            CREATE TABLE IF NOT EXISTS batch_lines(path TEXT,line INTEGER,content TEXT,PRIMARY KEY(path,line));
            CREATE TABLE IF NOT EXISTS batch_requests(request_json TEXT PRIMARY KEY);
            CREATE TABLE IF NOT EXISTS batch_comparisons(id TEXT PRIMARY KEY,verdict TEXT,difference_json TEXT);
        ''')
        # A rerun captures fresh MCP truth, never inherits a prior file answer.
        with state:
            for table in ('batch_lines', 'batch_requests', 'batch_comparisons'):
                state.execute('DELETE FROM ' + table)
        client = StreamableHttpMcpClient(args.mcp_url or discover_mcp_url(args.mcp_name))
        client.initialize()
        fixture = Fixture(root, destination, destination, state, client)
        selected = {entry['path'] for entry in sources[:args.files]}
        started = time.perf_counter()
        hits = 0
        for relative in sorted(selected):
            hits += capture_file(fixture, relative)
        acquisition = time.perf_counter() - started
        result = compare_recorded(baseline, state, root, selected)
        if source_snapshot(root)[0] != snapshot:
            raise ToolError('source changed during benchmark')
        result.update(files=len(selected), confirmed_lines=hits,
                      acquisition_seconds=round(acquisition, 3),
                      batch_requests=state.execute("SELECT count(*) FROM pages WHERE tool='ide_search_text'").fetchone()[0])
        with state:
            state.execute("INSERT OR REPLACE INTO metadata VALUES ('batch_result',?)", (canonical_json(result),))
        print(canonical_json(result))
        return 0 if not result['mismatch'] and not result['unsupported'] else 1
    finally:
        state.close()
        baseline.close()


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except (ToolError, UnicodeError):
        print(canonical_json({'status': 'unsupported', 'stage': 'batch text proof'}), file=sys.stderr)
        raise SystemExit(2)
