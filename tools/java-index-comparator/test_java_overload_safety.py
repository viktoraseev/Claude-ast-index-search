"""Compiled negative contracts for overload resolution with absent dependencies.

These authored Java fixtures prove native CLI safety, not live MCP equivalence.
"""
import subprocess
import unittest

import test_call_tree_mcp_contracts as call_tree_tests


class JavaOverloadSafetyTests(unittest.TestCase):
    def setUp(self):
        call_tree_tests.CallTreeMcpTests.setUp(self)

    def test_wildcard_import_cannot_override_unindexed_same_package_type(self):
        dependencies = self.directory / 'dependencies'
        sources = []
        for package in ('app', 'external'):
            source = dependencies / package / 'Alpha.java'
            source.parent.mkdir(parents=True)
            source.write_text(f'package {package}; public class Alpha {{}}\n')
            sources.append(str(source))
        source = self.root / 'Probe.java'
        source.write_text('''package app;
import external.*;
class Probe {
 static int objectSeed() { return 1; }
 static int externalSeed() { return 2; }
 static int pick(Object input) { return objectSeed(); }
 static int pick(external.Alpha input) { return externalSeed(); }
 static void actualApp(Alpha input) { pick(input); }
 static void actualExternal(external.Alpha input) { pick(input); }
}
''')
        compiled = subprocess.run(
            ['javac', '-d', str(self.directory / 'compiled'), *sources, str(source)],
            capture_output=True, timeout=30)
        self.assertEqual(compiled.returncode, 0, 'authored package-shadow fixture must compile')
        self.fixture.cli('rebuild', '--force', '--max-files', '0')
        self.fixture.cli('graph', 'build')
        doc = self.fixture.cli('call-tree', 'externalSeed', '--depth', '2',
                               '--limit', '100', '--in-file', '.java')
        # Alpha in app belongs to app.Alpha, not external.Alpha. Missing
        # dependency metadata may keep this edge ambiguous; inventing the
        # external target is never a safe identity conversion.
        callers = [item['name'] for item in doc['items'] if item['depth'] == 2]
        self.assertNotIn('actualApp', callers)
        # Explicit qualification still proves the valid external identity;
        # passing by removing every overload edge would hide a regression.
        self.assertIn('actualExternal', callers)


if __name__ == '__main__':
    unittest.main()
