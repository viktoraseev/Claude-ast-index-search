"""The external-import guard must use a legal Java compilation-unit scope."""
from pathlib import Path
import subprocess
import tempfile
import unittest

from java_receiver_contracts import SOURCES
from java_structure import JavaStructure


class ReceiverFixtureValidityTests(unittest.TestCase):
    def test_external_import_shadows_a_decoy_in_another_compilation_unit(self):
        artifacts = Path(__file__).resolve().parents[2] / '.artifacts/tests'
        artifacts.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix='receiver-validity-', dir=artifacts) as temporary:
            directory = Path(temporary).resolve()
            probe = directory / 'Probe.java'
            probe.write_text(SOURCES['local/Probe.java'], encoding='utf-8')
            structure = JavaStructure(directory)
            try:
                declared = [entry['name'] for entry in structure.read(probe)
                            if entry['kind'] == 'class']
                self.assertEqual(declared, ['Probe'])
            finally:
                structure.close()

            # Other source guards intentionally have unresolved/ambiguous
            # calls. Compile this exact external-import method independently,
            # preserving its package, import and same-package decoy.
            lines = SOURCES['local/Probe.java'].splitlines()
            methods = [line for line in lines if 'int external(' in line]
            self.assertEqual(len(methods), 1)
            context = [line for line in lines if line.startswith('package ')
                       or line == 'import java.util.List;']
            self.assertEqual(len(context), 2)
            probe.write_text('\n'.join(context + ['class Probe {', methods[0], '}']), encoding='utf-8')
            decoy = directory / 'List.java'
            decoy.write_text(SOURCES['local/List.java'], encoding='utf-8')
            classes = directory / 'classes'
            compile_result = subprocess.run(
                ['javac', '-proc:none', '-d', str(classes), str(probe), str(decoy)],
                capture_output=True, timeout=60)
            self.assertEqual(compile_result.returncode, 0, 'external-import Java scope is invalid')
            disassembly = subprocess.run(
                ['javap', '-c', '-p', '-classpath', str(classes), 'fixture.local.Probe'],
                capture_output=True, text=True, timeout=60)
            self.assertEqual(disassembly.returncode, 0)
            self.assertIn('InterfaceMethod java/util/List.size:()I', disassembly.stdout)
            self.assertNotIn('Method fixture/local/List.size', disassembly.stdout)


if __name__ == '__main__':
    unittest.main()
