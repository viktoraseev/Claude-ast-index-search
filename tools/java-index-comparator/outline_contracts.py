"""Finite Java outline ranges and multiplicity; independent source/CLI evidence."""
import json
from pathlib import Path
import re
import subprocess
import tempfile

from common import ToolError, connect, stable_id
import mobile_contracts
from root_contracts import Runner


FEATURES = {'outline:java-spans'}
REASON = ('independent source/javac/CLI: Java declaration ranges, same-line multiplicity, '
          'constructors, record components/accessors, local/anonymous/nested types, '
          'annotation elements, enums and default/full JSON/text views without an index; '
          'not MCP equivalence')
SOURCE = '''package fixture;
import java.util.List;
@Deprecated
class Outline {
    Outline() {
    }
    Outline(int value) {} Outline(String value) {}
    int first, second;
    void work() {} void work(int value) {}
    Runnable callback = new Runnable() {
        @Override public void run() {
        }
    };
    void local() {
        class Local { int value; }
    }
    record Pair(int value,
                String text) {
        Pair {}
        public int value() { return value; }
        int text(int suffix) { return suffix; }
    }
    interface Face {
        int FLAG=1;
        void call();
    }
    @interface Option {
        String value() default "x";
    }
    enum Mode {
        ON { void extra() {} },
        OFF;
        int value;
    }
}
'''
# Authored declaration spans, never derived from native rows or output.
ROWS = [
    ('Outline', 'class', 4, 35), ('Outline', 'function', 5, 6),
    ('Outline', 'function', 7, 7), ('Outline', 'function', 7, 7),
    ('first', 'property', 8, 8), ('second', 'property', 8, 8),
    ('work', 'function', 9, 9), ('work', 'function', 9, 9),
    ('callback', 'property', 10, 13), ('run', 'function', 11, 12),
    ('local', 'function', 14, 16), ('Local', 'class', 15, 15),
    ('value', 'property', 15, 15), ('Pair', 'class', 17, 22),
    ('value', 'property', 17, 17), ('text', 'property', 18, 18),
    ('text', 'function', 18, 18), ('Pair', 'function', 19, 19),
    ('value', 'function', 20, 20), ('text', 'function', 21, 21),
    ('Face', 'interface', 23, 26), ('FLAG', 'property', 24, 24),
    ('call', 'function', 25, 25), ('Option', 'interface', 27, 29),
    ('value', 'function', 28, 28), ('Mode', 'enum', 30, 34),
    ('ON', 'constant', 31, 31), ('extra', 'function', 31, 31),
    ('OFF', 'constant', 32, 32), ('value', 'property', 33, 33),
]


def plan_spans(state, root):
    if root is None:
        return
    with state:
        for feature in FEATURES:
            state.execute('INSERT OR REPLACE INTO coverage VALUES (?,?,?)',
                          (feature, 'implemented', REASON))
            subject = 'disposable-java-outline-spans'
            state.execute('INSERT OR IGNORE INTO checks(id,feature,subject) VALUES (?,?,?)',
                          (stable_id({'feature': feature, 'subject': subject}), feature, subject))


def observation(output, format, file, missing):
    """Keep multiplicity and every rendered row, including invalid metadata."""
    if format == 'json':
        try:
            document = json.loads(output)
        except ValueError:
            return {'invalid': True}
        if not isinstance(document, dict) or not isinstance(document.get('symbols'), list):
            return {'invalid': True}
        rows = document['symbols']
        if any(not isinstance(row, dict) for row in rows):
            return {'invalid': True}
        entries = [(row.get('name'), row.get('kind'), row.get('line'), row.get('end_line'))
                   for row in rows]
        metadata = {'schema_version': document.get('schema_version'),
                    'file': document.get('file'), 'skipped': document.get('skipped')}
    else:
        lines = output.splitlines()
        if missing:
            return {'missing': output == f'File not found: {file}\n'}
        if not lines or lines[0] != f'Outline of {file}:':
            return {'invalid': True}
        entries = []
        for line in lines[1:]:
            if line == '  No symbols found.' and len(lines) == 2:
                continue
            match = re.fullmatch(r'  :(\d+)(?:-(\d+))? (.+) \[(\w+)\]', line)
            if not match:
                return {'invalid': True}
            entries.append((match[3], match[4], int(match[1]), int(match[2] or match[1])))
        metadata = {}
    valid = all(isinstance(start, int) and isinstance(end, int) and 1 <= start <= end
                for _, _, start, end in entries)
    order = [(start, -end) for _, _, start, end in entries] if valid else None
    return {**metadata, 'rows': sorted(entries, key=repr),
            'ordered': valid and order == sorted(order)}


def exercise(binary, base):
    base = Path(base).resolve()
    boundary = Path(__file__).resolve().parents[2] / '.artifacts'
    if not base.is_relative_to(boundary.resolve()):
        raise ToolError('outline fixtures must stay inside repository .artifacts')
    base.mkdir(parents=True, exist_ok=True)
    # Retain command logs for failed evidence; sources are small synthetic inputs.
    directory = Path(tempfile.mkdtemp(prefix='java-outline-spans-', dir=base))
    runner = Runner(binary, directory)
    runner.root.mkdir()
    runner.environment['AST_INDEX_ROOT'] = str(runner.root)
    (runner.root / 'Outline.java').write_text(SOURCE)
    (runner.root / 'Empty.java').write_text('// no declarations\n')
    state = connect(directory / 'inventory.sqlite')
    try:
        state.executescript('CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT);' + mobile_contracts.SCHEMA)
        mobile_contracts.inventory(state, runner.root)
        if state.execute("SELECT count(*) FROM file_inventory WHERE extension='.java' AND kind='file'").fetchone()[0] != 2:
            raise ToolError('applicable Java outline inventory incomplete')
    finally:
        state.close()
    with (directory / 'javac.stdout.log').open('wb') as stdout, \
            (directory / 'javac.stderr.log').open('wb') as stderr:
        result = subprocess.run(['javac', '-d', str(directory / 'classes'), str(runner.root / 'Outline.java')],
                                stdout=stdout, stderr=stderr, timeout=60)
    if result.returncode:
        raise ToolError('Java outline fixture failed javac validation; see private logs')
    expected, actual = {}, {}
    for format in ('json', 'text'):
        for full in (False, True):
            for file, rows, missing in (('Outline.java', ROWS, False),
                                        ('Empty.java', [], False), ('Missing.java', [], True)):
                key = f'{file}:{format}:{full}'
                _, output = runner.command('outline', file, *(['--full'] if full else []), '--format', format)
                expected[key] = ({'missing': True} if missing and format == 'text' else {
                    **({'schema_version': 1, 'file': file, 'skipped': 'not_found' if missing else None}
                       if format == 'json' else {}),
                    'rows': sorted(rows, key=repr), 'ordered': True})
                actual[key] = observation(output, format, file, missing)
    feature = next(iter(FEATURES))
    return {feature: expected}, {feature: actual}
