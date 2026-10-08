"""Different Java folders must not share a cache merely because djb2 collides."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest


class JavaCacheHashCollision(unittest.TestCase):
    def test_colliding_java_folder_names_have_independent_indexes(self):
        artifacts = Path(__file__).resolve().parents[2] / '.artifacts/tests'
        artifacts.mkdir(parents=True, exist_ok=True)
        binary = Path(os.environ.get('AST_INDEX_TEST_BINARY', 'target/release/ast-index')).resolve()
        with tempfile.TemporaryDirectory(dir=artifacts) as temporary:
            directory = Path(temporary).resolve()
            environment = {**os.environ, 'AST_INDEX_CACHE_DIR': str(directory / 'cache'), 'NO_COLOR': '1'}
            environment.pop('AST_INDEX_DB_PATH', None)
            environment.pop('KOTLIN_INDEX_DB_PATH', None)
            projects = [(directory / 'Ab', 'CacheOwnerLeft'), (directory / 'BA', 'CacheOwnerRight')]
            for root, name in projects:
                root.mkdir()
                (root / 'Use.java').write_text(f'class {name} {{}}\n')
            # 33 * ord('A') + ord('b') == 33 * ord('B') + ord('A').
            # The shared prefix means these full root paths have equal djb2 keys.
            paths = []
            for root, name in projects:
                env = {**environment, 'AST_INDEX_ROOT': str(root)}
                result = subprocess.run([str(binary), 'rebuild', '--force', '--max-files', '0'],
                                        cwd=root, env=env, capture_output=True, text=True, timeout=15)
                self.assertEqual(result.returncode, 0, 'each Java root must be indexable')
                result = subprocess.run([str(binary), 'db-path'], cwd=root, env=env,
                                        capture_output=True, text=True, timeout=15)
                self.assertEqual(result.returncode, 0)
                paths.append(result.stdout.strip())
            self.assertNotEqual(paths[0], paths[1], 'colliding roots must have separate databases')
            for root, name in projects:
                result = subprocess.run([str(binary), '--format', 'json', 'class', 'CacheOwner*'],
                                        cwd=root, env={**environment, 'AST_INDEX_ROOT': str(root)},
                                        capture_output=True, text=True, timeout=15)
                self.assertEqual(result.returncode, 0)
                self.assertEqual([row['name'] for row in json.loads(result.stdout)['items']], [name])


if __name__ == '__main__':
    unittest.main()
