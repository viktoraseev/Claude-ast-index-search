#!/usr/bin/env python3
"""Capture actual MCP capabilities privately; stdout contains field names only."""
import argparse
import os
from pathlib import Path
import sys

from common import StreamableHttpMcpClient, ToolError, canonical_json, connect, discover_mcp_url


def inspect(client, root, state):
    server = client.initialize()
    tools = client.tools()
    status = client.call('ide_project_status', {'project_path': str(root)}) if 'ide_project_status' in tools else {}
    index_status = client.call('ide_index_status', {'project_path': str(root)}) if 'ide_index_status' in tools else {}
    projects = [project for project in status.get('projects', [])
                if os.path.normpath(project.get('path', '')) == str(root)]
    state.execute('CREATE TABLE IF NOT EXISTS capabilities(key TEXT PRIMARY KEY,value TEXT NOT NULL)')
    with state:
        state.executemany('INSERT OR REPLACE INTO capabilities VALUES (?,?)',
                          [('server', canonical_json(server)), ('tools', canonical_json(tools)),
                           ('target_status', canonical_json(projects)),
                           ('index_status', canonical_json(index_status))])
    return {'tools': {name: sorted(tool.get('inputSchema', {}).get('properties', {}))
                      for name, tool in sorted(tools.items())},
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
