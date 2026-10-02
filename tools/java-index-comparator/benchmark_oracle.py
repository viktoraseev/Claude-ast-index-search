#!/usr/bin/env python3
"""Compare local oracle bookkeeping on archived cases, without network calls."""
import argparse
import hashlib
import json
from pathlib import Path
import tempfile
import time

from audit import InvocationOracle, SCHEMA
from common import StreamableHttpMcpClient, ToolError, canonical_json, connect, source_snapshot, stable_id
from oracle_store import Metrics, OracleStore


LEGACY_SCHEMA = """
CREATE TABLE checks(id TEXT PRIMARY KEY,feature TEXT,subject TEXT);
CREATE TABLE pages(check_id TEXT,page INTEGER,request_json TEXT,response_json TEXT,tool TEXT,
 PRIMARY KEY(check_id,page));
CREATE TABLE invocation_cache(request_key TEXT PRIMARY KEY,response_json TEXT);
"""


class CapturedClient:
    def __init__(self):
        self.row, self.calls = None, 0

    def call(self, tool, arguments):
        if self.row['tool'] != tool or json.loads(self.row['request_json']) != arguments:
            raise ToolError('benchmark request does not match the recorded operation')
        self.calls += 1
        return json.loads(self.row['response_json'])


def digest_pages(connection, cases):
    digest = hashlib.sha256()
    count = 0
    for case in cases:
        for row in connection.execute('SELECT * FROM pages WHERE check_id=? ORDER BY page', (case['id'],)):
            for key in ('check_id', 'page', 'tool', 'request_json', 'response_json'):
                digest.update(str(row[key]).encode())
                digest.update(b'\0')
            count += 1
    return count, digest.hexdigest()


def benchmark(evidence, output, limit=10, feature='outline'):
    output.mkdir(parents=True, exist_ok=True)
    directory = Path(tempfile.mkdtemp(prefix='oracle-benchmark-', dir=output))
    source = connect(evidence, read_only=True)
    try:
        source.execute('BEGIN')
        cases = source.execute("SELECT id,feature,subject FROM checks WHERE feature=? AND status='complete' ORDER BY rowid LIMIT ?",
                               (feature, limit)).fetchall()
        if not cases:
            raise ToolError('no completed cases available for this benchmark')
        reference = digest_pages(source, cases)
        if not reference[0]:
            raise ToolError('benchmark cases have no oracle operations')
        results = {}
        for mode in ('legacy', 'optimized'):
            database = directory / (mode + '.sqlite')
            state = connect(database)
            try:
                state.executescript(LEGACY_SCHEMA if mode == 'legacy' else SCHEMA)
                with state:
                    state.executemany('INSERT INTO checks(id,feature,subject) VALUES (?,?,?)',
                                      (tuple(case) for case in cases))
                client = CapturedClient()
                oracle = InvocationOracle(client, state) if mode == 'optimized' else None
                store = OracleStore(state, oracle.metrics) if oracle is not None else None
                started = time.perf_counter()
                for case in cases:
                    for row in source.execute('SELECT * FROM pages WHERE check_id=? ORDER BY page', (case['id'],)):
                        client.row = row
                        arguments = json.loads(row['request_json'])
                        if oracle is not None:
                            response = oracle.call(row['tool'], arguments)
                            store.page(case['id'], row['page'], row['tool'], arguments, response)
                        else:
                            key = stable_id({'tool': row['tool'], 'arguments': arguments})
                            reusable = row['tool'] in InvocationOracle.READ_ONLY and 'cursor' not in arguments
                            cached = state.execute('SELECT response_json FROM invocation_cache WHERE request_key=?', (key,)).fetchone() if reusable else None
                            response = json.loads(cached[0]) if cached else client.call(row['tool'], arguments)
                            if cached is None and reusable and isinstance(response, dict) and not any(response.get(flag) for flag in ('stale', 'truncated', 'hasMore', 'nextCursor')):
                                with state:
                                    state.execute('INSERT OR REPLACE INTO invocation_cache VALUES (?,?)', (key, canonical_json(response)))
                            with state:
                                state.execute('INSERT INTO pages VALUES (?,?,?,?,?)',
                                              (case['id'], row['page'], canonical_json(arguments), canonical_json(response), row['tool']))
                    with state:
                        if oracle is not None:
                            oracle.metrics.flush()
                seconds = time.perf_counter() - started
                if digest_pages(state, cases) != reference:
                    raise ToolError('benchmark changed an oracle response or operation order')
                payload_bytes = state.execute('SELECT coalesce(sum(length(response_json)),0) FROM ' +
                                              ('pages' if mode == 'legacy' else 'oracle_responses')).fetchone()[0]
                results[mode] = {'seconds': round(seconds, 6), 'simulated_network_calls': client.calls,
                                 'payload_bytes': payload_bytes}
            finally:
                state.close()
            results[mode]['database_bytes'] = database.stat().st_size
        if results['legacy']['simulated_network_calls'] != results['optimized']['simulated_network_calls']:
            raise ToolError('optimized benchmark changed network request coverage')
        return {'cases': len(cases), 'operations': reference[0], 'identical_operations': True,
                'network_used': False, 'local_speedup': round(results['legacy']['seconds'] / results['optimized']['seconds'], 2),
                'results': results, 'artifacts': str(directory)}
    finally:
        source.close()


def benchmark_live(evidence, output, url, limit_requests=16):
    """Compare bounded concurrency on exactly the archived first-page queries."""
    source = connect(evidence, read_only=True)
    try:
        metadata = dict(source.execute('SELECT key,value FROM metadata'))
        root = Path(metadata['project_root'])
        if source_snapshot(root)[0] != metadata.get('snapshot_sha256'):
            raise ToolError('benchmark project changed since the archived evidence')
        requests = [json.loads(row[0]) for row in source.execute("""SELECT DISTINCT request_json FROM pages
            WHERE tool='ide_find_symbol' AND length(json_extract(request_json,'$.query'))=1
            AND json_extract(request_json,'$.cursor') IS NULL ORDER BY request_json LIMIT ?""", (limit_requests,))]
        if not requests:
            raise ToolError('no captured first-page symbol requests available')
        output.mkdir(parents=True, exist_ok=True)
        directory = Path(tempfile.mkdtemp(prefix='live-oracle-profile-', dir=output))
        results, fingerprints = {}, {}
        # Parallel first: do not credit it with caches warmed by the serial run.
        for mode, workers in [('parallel', 4), ('serial', 1)]:
            state = connect(directory / (mode + '.sqlite'))
            try:
                state.executescript(SCHEMA)
                metrics = Metrics(state)
                client = StreamableHttpMcpClient(url, metrics=metrics)
                client.initialize()
                oracle = InvocationOracle(client, state, metrics=metrics)
                started = time.perf_counter()
                oracle.prefetch('ide_find_symbol', requests, workers=workers)
                seconds = time.perf_counter() - started
                digest = hashlib.sha256()
                for arguments in requests:
                    response = oracle.call('ide_find_symbol', arguments)
                    if not isinstance(response, dict) or not isinstance(response.get('symbols'), list) or any(response.get(flag) for flag in ('stale', 'truncated', 'hasMore', 'nextCursor')) or len(response['symbols']) >= 500:
                        raise ToolError('profile oracle response is incomplete; no speed result accepted')
                    # Navigation compares a multiset, not Go-to-Symbol tie order.
                    normalized = dict(response)
                    normalized['symbols'] = sorted(response['symbols'], key=canonical_json)
                    digest.update(canonical_json(normalized).encode())
                fingerprints[mode] = digest.hexdigest()
                results[mode] = {'seconds': round(seconds, 6), 'workers': workers,
                                 'performance': metrics.summary()}
            finally:
                state.close()
        if fingerprints['serial'] != fingerprints['parallel'] or source_snapshot(root)[0] != metadata['snapshot_sha256']:
            raise ToolError('oracle/project changed during profiling; no speed result accepted')
        return {'requests': len(requests), 'identical_navigation': True, 'network_used': True,
                'wall_speedup': round(results['serial']['seconds'] / results['parallel']['seconds'], 2),
                'results': results, 'artifacts': str(directory)}
    finally:
        source.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--evidence', required=True, type=Path)
    parser.add_argument('--output-dir', required=True, type=Path)
    parser.add_argument('--limit', default=10, type=int)
    parser.add_argument('--feature', default='outline')
    parser.add_argument('--live-mcp-url', help='Profile 1 vs 4 workers on captured requests against this oracle')
    parser.add_argument('--request-limit', type=int, default=16)
    args = parser.parse_args()
    if args.limit < 1 or args.request_limit < 1:
        parser.error('limit must be positive')
    try:
        result = benchmark_live(args.evidence, args.output_dir, args.live_mcp_url, args.request_limit) if args.live_mcp_url else benchmark(args.evidence, args.output_dir, args.limit, args.feature)
        print(canonical_json(result))
        return 0
    except (ToolError, OSError) as error:
        print(canonical_json({'error': str(error)}))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
