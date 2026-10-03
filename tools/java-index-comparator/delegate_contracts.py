"""External Java search delegation; no claim about a third-party parser or MCP truth."""
import json
from pathlib import Path
import sys
import tempfile

from common import ToolError
from root_contracts import Runner


FEATURES = {'agrep'}
REASON = ('internal CLI: Java ast-grep delegation arguments, executable fallback, output and errors '
          'on disposable source; external parser and MCP equivalence not claimed')
STUB = '''#!{python}
import json, os, sys
name = os.path.basename(sys.argv[0])
with open(os.environ['AUDIT_DELEGATE_LOG'], 'a') as out:
    out.write(json.dumps([name, os.getcwd(), *sys.argv[1:]]) + '\\n')
if sys.argv[1:] == ['--version']:
    print('ast-grep 0.0.0-public-fixture')
    key = 'AUDIT_SG_VERSION' if name == 'sg' else 'AUDIT_AST_GREP_VERSION'
    sys.exit(int(os.environ[key]))
print(os.environ['AUDIT_DELEGATE_OUTPUT'])
sys.exit(int(os.environ['AUDIT_DELEGATE_EXIT']))
'''


def exercise(binary, base):
    boundary = Path(__file__).resolve().parents[2] / '.artifacts'
    base = Path(base).resolve()
    if not base.is_relative_to(boundary.resolve()):
        raise ToolError('delegation artifacts must stay inside repository .artifacts')
    base.mkdir(parents=True, exist_ok=True)
    directory = Path(tempfile.mkdtemp(prefix='delegate-', dir=base)).resolve()
    runner = Runner(binary, directory)
    runner.root.mkdir()
    (runner.root / '.git').mkdir()
    (runner.root / 'Probe.java').write_text('class Probe { void probe() {} void use() { probe(); } }\n')
    scripts = directory / 'bin'
    scripts.mkdir()
    for name in ('sg', 'ast-grep'):
        path = scripts / name
        path.write_text(STUB.format(python=sys.executable))
        path.chmod(0o755)
    log = directory / 'external.jsonl'
    # No system sg or ast-grep is eligible, including on machines that have it.
    runner.environment.update(PATH=str(scripts), AUDIT_DELEGATE_LOG=str(log))
    pattern = 'probe($$$)'
    expected, actual = {'agrep': {}}, {'agrep': {}}
    for label, flags, sg, alternate, external_exit, output, provider, exit_code in (
            ('java-json', ['--lang', 'java', '--json'], 0, 0, 0, '[{"fixture":true}]', 'sg', 0),
            ('autodetect-text', [], 0, 0, 0, 'public source match', 'sg', 0),
            ('no-matches', ['--lang', 'java'], 0, 0, 1, '', 'sg', 0),
            ('external-error', ['--lang', 'java'], 0, 0, 7, '', 'sg', 1),
            ('failed-probe-fallback', ['--lang', 'java'], 7, 0, 0, 'public fallback', 'ast-grep', 0),
            ('missing-dependency', ['--lang', 'java'], 127, 127, 0, '', None, 1)):
        before = len(log.read_text().splitlines()) if log.exists() else 0
        code, text = runner.command('agrep', pattern, *flags, acceptable=(0, 1), environment={
            'AUDIT_SG_VERSION': str(sg), 'AUDIT_AST_GREP_VERSION': str(alternate),
            'AUDIT_DELEGATE_EXIT': str(external_exit), 'AUDIT_DELEGATE_OUTPUT': output})
        records = [json.loads(line) for line in log.read_text().splitlines()[before:]]
        runs = [row for row in records if row[2:3] == ['run']]
        args = ['run', '--pattern', pattern]
        if '--lang' in flags:
            args += ['--lang', 'java']
        if '--json' in flags:
            args.append('--json=compact')
        expected['agrep'][label] = {'exit': exit_code, 'runs': [[provider, str(runner.root), *args]] if provider else [],
                                    'output': output + '\n' if provider else ''}
        actual['agrep'][label] = {'exit': code, 'runs': runs, 'output': text}
    return expected, actual
