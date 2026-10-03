#!/usr/bin/env python3
"""Capture actual MCP capabilities privately; stdout contains field names only."""
import argparse
import os
from pathlib import Path
import sys

from common import StreamableHttpMcpClient, ToolError, McpRemoteError, canonical_json, connect, discover_mcp_url


def inspect(client, root, state):
    state.execute('CREATE TABLE IF NOT EXISTS capabilities(key TEXT PRIMARY KEY,value TEXT NOT NULL)')
    with state:
        # An interrupted retry cannot leave stale target availability visible.
        state.execute("DELETE FROM capabilities WHERE key IN ('target_status','index_status','failure')")
        state.execute("INSERT OR REPLACE INTO capabilities VALUES ('stage',?)", (canonical_json('initialize'),))

    def checkpoint(key, value, next_stage):
        with state:
            state.executemany('INSERT OR REPLACE INTO capabilities VALUES (?,?)',
                              [(key, canonical_json(value)), ('stage', canonical_json(next_stage))])

    stage = 'initialize'
    try:
        server = client.initialize()
        checkpoint('server', server, 'tools')
        stage = 'tools'
        tools = client.tools()
        checkpoint('tools', tools, 'target_status')
        stage = 'target_status'
        status = client.call('ide_project_status', {'project_path': str(root)}) if 'ide_project_status' in tools else {}
        projects = [project for project in status.get('projects', [])
                    if os.path.normpath(project.get('path', '')) == str(root)]
        checkpoint('target_status', projects, 'index_status')
        stage = 'index_status'
        index_status = client.call('ide_index_status', {'project_path': str(root)}) if 'ide_index_status' in tools else {}
        checkpoint('index_status', index_status, 'complete')
    except ToolError as error:
        diagnostic = {'error_type': type(error).__name__}
        if isinstance(error, McpRemoteError):
            diagnostic.update(kind=error.kind, response=error.response)
        checkpoint('failure', diagnostic, 'failed:' + stage)
        raise
    return {'tools': {name: sorted(tool.get('inputSchema', {}).get('properties', {}))
                      for name, tool in sorted(tools.items())},
            'complete': True,
            'target_status_fields': sorted({key for project in projects for key in project}),
            'index_status_fields': sorted(index_status)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--project-root', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--mcp-url')
    parser.add_argument('--mcp-name', default='intellij-index')
    args = parser.parse_args()
    root = args.project_root.resolve()
    output = args.output.resolve()
    if output == root or root in output.parents:
        parser.error('capability artifacts must be outside the target')
    state = connect(output)
    try:
        client = StreamableHttpMcpClient(args.mcp_url or discover_mcp_url(args.mcp_name))
        print(canonical_json(inspect(client, root, state)))
    finally:
        state.close()


if __name__ == '__main__':
    try:
        main()
    except ToolError:
        print(canonical_json({'status': 'error', 'stage': 'MCP capability discovery'}), file=sys.stderr)
        raise SystemExit(2)
