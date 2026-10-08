"""Shared cache identity on authored Java roots; no MCP equivalent."""
import json
import shutil
from pathlib import Path
import tempfile

from common import ToolError, connect, stable_id
import mobile_contracts
from root_contracts import Runner

FEATURE = 'global:cache:java-hash-collisions'
FEATURES = {FEATURE}
REASON = ('independent source/state and internal CLI: collision-safe cache identities, '
          'legacy discovery, Java declarations, aliases and publication isolation; '
          'corrupt/foreign/linked artifact rejection; not MCP equivalence')
ROOTS = ('AbAb', 'AbBA', 'BAAb', 'BABA')
SOURCES = {f'{root}/Use.java': f'class CacheOwner{number} {{}}\n'
           for number, root in enumerate(ROOTS)}
SOURCES.update({'inventory.xml': '<fixture/>\n', 'Inventory.kt': '// inventory only\n'})
INVENTORY = {name: Path(name).suffix for name in SOURCES}
NEGATIVES = ('corrupt-owner', 'foreign-owner', 'linked-owner', 'linked-db', 'linked-directory')


def acceptance_keys():
    return {'inventory', 'applicable-java', 'distinct-paths', 'legacy-discovery',
            'alias', 'publication-isolation', 'clear-isolation', 'missing-primary-bucket'} | {
        f'java-root:{number}' for number in range(len(ROOTS))} | set(NEGATIVES)


def acceptance_complete(expected, actual):
    return all(acceptance_keys() <= section.keys() and section.get('inventory') == INVENTORY
               and section.get('applicable-java') is True for section in (expected, actual))


def plan_cache(state, root):
    if root is None:
        return
    with state:
        state.execute('INSERT OR REPLACE INTO coverage VALUES (?,?,?)', (FEATURE, 'implemented', REASON))
        subject = 'disposable-java-cache-collisions-v1'
        state.execute('INSERT OR IGNORE INTO checks(id,feature,subject) VALUES (?,?,?)',
                      (stable_id({'feature': FEATURE, 'subject': subject}), FEATURE, subject))


def exercise(binary, base):
    base = Path(base).resolve()
    if not base.is_relative_to((Path(__file__).resolve().parents[2] / '.artifacts').resolve()):
        raise ToolError('cache collision fixtures must stay inside repository .artifacts')
    base.mkdir(parents=True, exist_ok=True)
    directory = Path(tempfile.mkdtemp(prefix='cache-collision-', dir=base)).resolve()
    runner = Runner(binary, directory)
    for name, content in SOURCES.items():
        path = directory / 'sources' / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    state = connect(directory / 'inventory.sqlite')
    try:
        state.executescript('CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT);' + mobile_contracts.SCHEMA)
        mobile_contracts.inventory(state, directory / 'sources')
        inventory = dict(state.execute("SELECT path,extension FROM file_inventory WHERE kind='file'"))
        if inventory != INVENTORY:
            raise ToolError('cache collision fixture full inventory incomplete')
    finally:
        state.close()
    expected, actual = {}, {}

    def record(key, want, got):
        expected[key], actual[key] = want, got
        checkpoint = directory / 'results.next.json'
        checkpoint.write_text(json.dumps({'expected': expected, 'actual': actual}))
        checkpoint.replace(directory / 'results.json')

    def select(root):
        runner.root = root
        runner.environment['AST_INDEX_ROOT'] = str(root)

    def declarations():
        return [row['name'] for row in runner.json('class', 'CacheOwner*')['items']]

    record('inventory', INVENTORY, inventory)
    record('applicable-java', True, '.java' in inventory.values())
    paths = []
    for number, root in enumerate(ROOTS):
        select(directory / 'sources' / root)
        runner.command('rebuild', '--force', '--max-files', '0')
        paths.append(Path(runner.command('db-path')[1].strip()))
    record('distinct-paths', True, len(set(paths)) == len(ROOTS))
    for number, root in enumerate(ROOTS):
        select(directory / 'sources' / root)
        record(f'java-root:{number}', [f'CacheOwner{number}'], declarations())
    # Old unmanifested caches remain discoverable without stealing their DB.
    paths[0].parent.joinpath('.ast-index-owner-v1.json').unlink()
    select(directory / 'sources' / ROOTS[1])
    record('legacy-discovery', True, Path(runner.command('db-path')[1].strip()) == paths[1]
           and declarations() == ['CacheOwner1'])
    alias = directory / 'alias'
    alias.symlink_to(directory / 'sources' / ROOTS[3], target_is_directory=True)
    select(alias)
    record('alias', True, Path(runner.command('db-path')[1].strip()) == paths[3]
           and declarations() == ['CacheOwner3'])
    select(directory / 'sources' / ROOTS[1])
    (runner.root / 'Use.java').write_text('class CacheOwnerReplacement {}\n')
    runner.command('rebuild', '--force')
    select(directory / 'sources' / ROOTS[0])
    record('publication-isolation', ['CacheOwner0'], declarations())
    select(directory / 'sources' / ROOTS[1])
    runner.command('clear')
    select(directory / 'sources' / ROOTS[0])
    record('clear-isolation', True, not paths[1].exists() and declarations() == ['CacheOwner0'])
    shutil.rmtree(paths[0].parent)
    select(directory / 'sources' / ROOTS[3])
    record('missing-primary-bucket', True, Path(runner.command('db-path')[1].strip()) == paths[3]
           and declarations() == ['CacheOwner3'])

    for case in NEGATIVES:
        probe = directory / case
        probe.mkdir()
        guard = Runner(binary, probe)
        guard.root.mkdir()
        (guard.root / 'Use.java').write_text('class CacheOwnerGuard {}\n')
        guard.environment['AST_INDEX_ROOT'] = str(guard.root)
        guard.command('rebuild', '--force')
        database = Path(guard.command('db-path')[1].strip())
        owner = database.parent / '.ast-index-owner-v1.json'
        sentinel = probe / 'sentinel'
        sentinel.write_text('must survive\n')
        if case == 'corrupt-owner':
            owner.write_text('{invalid-json')
        elif case == 'foreign-owner':
            owner.write_text(json.dumps({'version': 1, 'normalized_root': '/synthetic/foreign',
                                         'raw_root': '/synthetic/foreign'}))
        elif case == 'linked-owner':
            owner.unlink()
            owner.symlink_to(sentinel)
        elif case == 'linked-db':
            database.unlink()
            database.symlink_to(sentinel)
        else:
            moved = probe / 'moved-cache'
            database.parent.rename(moved)
            database.parent.symlink_to(moved, target_is_directory=True)
        code, _ = guard.command('db-path', acceptable=(0, 1))
        record(case, True, code == 1 and sentinel.read_text() == 'must survive\n')
    return {FEATURE: expected}, {FEATURE: actual}
