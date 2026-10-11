"""Finite Java unused-dependency checklist. No verdict-derived selection of criteria.

The retained child population includes every existing Java dependency case.
Separate new executable criteria retain the formerly vague inference gaps.
The CLI promises source dependency analysis, not a new binary-loader API.
"""
from pathlib import Path
import re
from common import stable_id


def cli_surface():
    source = (Path(__file__).resolve().parents[2] / 'src/main.rs').read_text()
    cli = source.split('struct Cli {', 1)[1].split('\n}', 1)[0]
    command = source.split('    UnusedDeps {', 1)[1].split('\n    },', 1)[0]
    return stable_id([re.sub(r'\s+', '', re.sub(r'^\s*//[^\n]*', '', body, flags=re.MULTILINE))
                      for body in (cli, command)])


def criteria():
    return [dict(item) for item in RETAINED]


# Preserve the recorded source-classpath ordering obligation. The current CLI
# has root selectors but no compiler classpath-order contract. Execute the
# explicit ambiguity alternative rather than inventing an order or skipping it.
API_DECISIONS = ({
    'id': 'duplicate-source-classpath-order',
    'criterion': 'unused-deps:java-source-classpath-ambiguity',
    'input': 'two declared dependency modules export the same Java qualified type; consumer uses that type; reverse descriptor and source-path order',
    'expected': 'declaring dependency ownership must follow an independently specified source-classpath order or an explicitly unresolved ambiguity contract',
    'production': 'src/commands/modules.rs java_dependency_source rejects equally preferred owners',
    'verification': 'java_classpath_contracts executes both descriptor/path orderings, javac order sensitivity, JSON/text ambiguity and local/scope/refresh controls',
    'stop': 'every ambiguity and control branch must execute and match; no claimed compiler order or new classpath API',
},)


RETAINED = ({'feature': 'global:format:java-modules',
  'subject': 'disposable-java-module-formats',
  'samples_count': 85,
  'sample_keys_sha256': 'c92c8986eb576e7a624af9c0a80c919bfb302e453265e86aa11559a3fcfa8080',
  'population_sha256': '8c3f5b5d776a63730d80290b63676095839aebe5d55466b7bcd94c7dd77f244a',
  'fixture': 'module_format_contracts.exercise',
  'production': 'src/commands/modules.rs; src/parsers/treesitter/java.rs',
  'contract': 'complete JSON/text dependency classification, examples, options, totals and missing-state '
              'envelopes',
  'input': 'authored Java cases in module_format_contracts.exercise',
  'expected': 'complete authored dependency categories, declaring identities and option envelopes',
  'stop': 'every retained assertion and nested branch executed and equal; failures or omissions block '
          'acceptance'},
 {'feature': 'global:scope:java-module-aliases',
  'subject': 'disposable-java-module-aliases',
  'samples_count': 308,
  'sample_keys_sha256': '3fcba19d0c2ea54ff7942b392c27d3e5d33a83d3a51fea08c6691aae257a98f8',
  'population_sha256': 'a4a4d7ebc2a69262050133b3ba7f259a4410cc736ad502eed72e4716cea5b1cd',
  'fixture': 'module_alias_contracts.exercise',
  'production': 'src/commands/modules.rs; src/parsers/treesitter/java.rs',
  'contract': 'exact/suffix/ambiguous module aliases and selected dependency graph',
  'input': 'authored Java cases in module_alias_contracts.exercise',
  'expected': 'complete authored dependency categories, declaring identities and option envelopes',
  'stop': 'every retained assertion and nested branch executed and equal; failures or omissions block '
          'acceptance'},
 {'feature': 'global:scope:java-module-directories',
  'subject': 'disposable-java-module-directory-scope',
  'samples_count': 1350,
  'sample_keys_sha256': '15dbe91d91a6f6099074077a3f8635d05e79c9c5acc53d1664ad1534bafdd8a7',
  'population_sha256': '1e5b74f884b558358c0cd10fe445cd5ae3021c446a34f337a19ada676cb7edbc',
  'fixture': 'module_scope_contracts.exercise',
  'production': 'src/commands/modules.rs; src/parsers/treesitter/java.rs',
  'contract': 'module-directory/cwd selector intersection and ownership',
  'input': 'authored Java cases in module_scope_contracts.exercise',
  'expected': 'complete authored dependency categories, declaring identities and option envelopes',
  'stop': 'every retained assertion and nested branch executed and equal; failures or omissions block '
          'acceptance'},
 {'feature': 'global:scope:java-module-roots',
  'subject': 'disposable-java-module-root-scope',
  'samples_count': 437,
  'sample_keys_sha256': 'f934aa0115421bb51413da418bae66201d2f396e4ca7b026951b55a31dd0560d',
  'population_sha256': '130bc0b538b6ec521e8a7196ccefeb4339c0616adcd7a9058be0bb67965fb908',
  'fixture': 'module_root_contracts.exercise',
  'production': 'src/commands/modules.rs; src/parsers/treesitter/java.rs',
  'contract': 'local/attached root module identities and dependency ownership',
  'input': 'authored Java cases in module_root_contracts.exercise',
  'expected': 'complete authored dependency categories, declaring identities and option envelopes',
  'stop': 'every retained assertion and nested branch executed and equal; failures or omissions block '
          'acceptance'},
 {'feature': 'unused-deps',
  'subject': 'disposable-java-unused-dependencies',
  'samples_count': 22,
  'sample_keys_sha256': 'e780673ca92fda72e20255821c512dfc0e00eb403f66c7abbd1f196141f3cc09',
  'population_sha256': 'ef8fc2cf7fcbaaca3db9b6fd7cddbd4fc1e79b012362fb77fcf7b71da808a0b7',
  'fixture': 'unused_dep_contracts.exercise',
  'production': 'src/commands/modules.rs; src/parsers/treesitter/java.rs',
  'contract': 'module missing/edgeless states, strict and independent option products, verbose '
              'classifications',
  'input': 'authored Java cases in unused_dep_contracts.exercise',
  'expected': 'complete authored dependency categories, declaring identities and option envelopes',
  'stop': 'every retained assertion and nested branch executed and equal; failures or omissions block '
          'acceptance'},
 {'feature': 'unused-deps:java-android-ownership',
  'subject': 'disposable-java-android-dependency-ownership',
  'samples_count': 12,
  'sample_keys_sha256': '8b3c7b819e98ed2f073d975f0d1f70cbcd7b652d0ae5e83e508e3b58fe468c2e',
  'population_sha256': '5488cf2683acdeda0d432040b2de75e3a0608ab6a8cbaa87d8ddc10db94cca90',
  'fixture': 'android_dependency_contracts.exercise',
  'production': 'src/commands/modules.rs; src/parsers/treesitter/java.rs',
  'contract': 'Java layout-class and R resource dependency ownership; XML-only parsing excluded',
  'input': 'authored Java cases in android_dependency_contracts.exercise',
  'expected': 'complete authored dependency categories, declaring identities and option envelopes',
  'stop': 'every retained assertion and nested branch executed and equal; failures or omissions block '
          'acceptance'},
 {'feature': 'unused-deps:java-array-results',
  'subject': 'disposable-java-array-results-v1',
  'samples_count': 170,
  'sample_keys_sha256': '9e170da3e65aae87e0ec0d5f87e8e3d2cc675430d4a714a649f420981bd048d1',
  'population_sha256': '405b660778f4b5b4fa9b64768267f2e85a7a93c5a030e8e30138fbf0dcaab0ae',
  'fixture': 'java_array_result_contracts.exercise',
  'production': 'src/commands/modules.rs; src/parsers/treesitter/java.rs',
  'contract': 'indexed nominal/generic/method/record arrays, integral indexes and access/rank guards',
  'input': 'authored Java cases in java_array_result_contracts.exercise',
  'expected': 'complete authored dependency categories, declaring identities and option envelopes',
  'stop': 'every retained assertion and nested branch executed and equal; failures or omissions block '
          'acceptance'},
 {'feature': 'unused-deps:java-flow-shadows',
  'subject': 'disposable-java-import-type-identities',
  'samples_count': 94,
  'sample_keys_sha256': 'f1b9309d6ef6abc1a2526aac8003fbd6476192c8fe7e3188a16c2a3f9cdc1d01',
  'population_sha256': 'd7fe7dc09f46ed93cb88766b6ff66c83dda0975c541a72e7f49b2b98d2ea2d78',
  'fixture': 'java_dependency_contracts.exercise',
  'production': 'src/commands/modules.rs; src/parsers/treesitter/java.rs',
  'contract': 'boolean/pattern/loop/resource flow and abrupt-exit scopes',
  'input': 'authored Java cases in java_dependency_contracts.exercise',
  'expected': 'complete authored dependency categories, declaring identities and option envelopes',
  'stop': 'every retained assertion and nested branch executed and equal; failures or omissions block '
          'acceptance'},
 {'feature': 'unused-deps:java-imports',
  'subject': 'disposable-java-import-type-identities',
  'samples_count': 6,
  'sample_keys_sha256': '7b0f2f1018d49ebcef0bbb2d06e91ee6d7f8de2f87e5a95e704ce3f4fd9ac71a',
  'population_sha256': '5f32f6d86c4c74ef86a7ebf1dbc0379ae418ff2a4970119145db99b96a103cd4',
  'fixture': 'java_dependency_contracts.exercise',
  'production': 'src/commands/modules.rs; src/parsers/treesitter/java.rs',
  'contract': 'explicit/static/import-only and on-demand Java import identities',
  'input': 'authored Java cases in java_dependency_contracts.exercise',
  'expected': 'complete authored dependency categories, declaring identities and option envelopes',
  'stop': 'every retained assertion and nested branch executed and equal; failures or omissions block '
          'acceptance'},
 {'feature': 'unused-deps:java-inherited-imports',
  'subject': 'disposable-java-inherited-import-ownership',
  'samples_count': 3440,
  'sample_keys_sha256': '307cc200bf538c16d8c19713a7884705119f38ccc9644072e5fc5c6c8d49bd16',
  'population_sha256': '75fdb58f3f730581c4d2582f2a4ea973fd719525b42fd8d9255fd0c673c6d70e',
  'fixture': 'java_inherited_import_contracts.exercise',
  'production': 'src/commands/modules.rs; src/parsers/treesitter/java.rs',
  'contract': 'inherited/static/member type aliases, protected lexical/value access, local subclasses, '
              'record accessors and declaring classpath',
  'input': 'authored Java cases in java_inherited_import_contracts.exercise',
  'expected': 'complete authored dependency categories, declaring identities and option envelopes',
  'stop': 'every retained assertion and nested branch executed and equal; failures or omissions block '
          'acceptance'},
 {'feature': 'unused-deps:java-lexical-shadows',
  'subject': 'disposable-java-import-type-identities',
  'samples_count': 32,
  'sample_keys_sha256': '9b3d20290d4ff91903b3d7f0363c7a3ef2bd87c7c03708a21e896b99fc6b68ff',
  'population_sha256': '4eda1be420d536c5a1edc6fdac0fa1f84b105e6975d91ae51b3be8e061ecc049',
  'fixture': 'java_dependency_contracts.exercise',
  'production': 'src/commands/modules.rs; src/parsers/treesitter/java.rs',
  'contract': 'lexical type/variable shadow and declaration boundaries',
  'input': 'authored Java cases in java_dependency_contracts.exercise',
  'expected': 'complete authored dependency categories, declaring identities and option envelopes',
  'stop': 'every retained assertion and nested branch executed and equal; failures or omissions block '
          'acceptance'},
 {'feature': 'unused-deps:java-module-binding',
  'subject': 'disposable-java-module-declaration-binding',
  'samples_count': 151,
  'sample_keys_sha256': '2f652ff6f93dc209f858d5994eb79063c391c04cd2fb6ff0e90196b3d941e9c0',
  'population_sha256': '013a9a726cb3e0f837d870fbf532e70af59aa021cf0aac70e6d9fd2e5be402e4',
  'fixture': 'java_module_binding_contracts.exercise',
  'production': 'src/commands/modules.rs; src/parsers/treesitter/java.rs',
  'contract': 'selected source classpath, declaring roots, local/import/API-export precedence and '
              'absent-owner guards',
  'input': 'authored Java cases in java_module_binding_contracts.exercise',
  'expected': 'complete authored dependency categories, declaring identities and option envelopes',
  'stop': 'every retained assertion and nested branch executed and equal; failures or omissions block '
          'acceptance'},
 {'feature': 'unused-deps:java-nested-array-slots',
  'subject': 'disposable-java-nested-array-slots-v1',
  'samples_count': 115,
  'sample_keys_sha256': 'b2ba9f302d1de814c2b8bc6c565523f435a3be18a43aa2256485b4bb7f106158',
  'population_sha256': '23ec1b8949bc8a53f8a1dae7a345b63ef48e92e2fec80f80ec0868b308521789',
  'fixture': 'java_nested_array_contracts.exercise',
  'production': 'src/commands/modules.rs; src/parsers/treesitter/java.rs',
  'contract': 'nested array formal slots, rank/primitive/reference/witness and declaring ownership',
  'input': 'authored Java cases in java_nested_array_contracts.exercise',
  'expected': 'complete authored dependency categories, declaring identities and option envelopes',
  'stop': 'every retained assertion and nested branch executed and equal; failures or omissions block '
          'acceptance'},
 {'feature': 'unused-deps:java-on-demand-members',
  'subject': 'disposable-java-on-demand-imports',
  'samples_count': 49,
  'sample_keys_sha256': '728ae3e2ebe0cf0f0065f856f105e68bed2f88aa00abdf03ca830d49fc559004',
  'population_sha256': '97a9b489e9d5cc3075192bc038bcf45ee36128f0ef90ad828ac238fdcfd742c6',
  'fixture': 'java_on_demand_contracts.exercise',
  'production': 'src/commands/modules.rs; src/parsers/treesitter/java.rs',
  'contract': 'static on-demand member ownership, accessibility and ambiguity',
  'input': 'authored Java cases in java_on_demand_contracts.exercise',
  'expected': 'complete authored dependency categories, declaring identities and option envelopes',
  'stop': 'every retained assertion and nested branch executed and equal; failures or omissions block '
          'acceptance'},
 {'feature': 'unused-deps:java-on-demand-types',
  'subject': 'disposable-java-on-demand-imports',
  'samples_count': 46,
  'sample_keys_sha256': '5c8ef528476177fb87d9a3e16ed3b0989904199be5b20db7904b956d545cfece',
  'population_sha256': 'ce407b6161d82d347c802c9f554ba4fe9654c31f0a6a632f75926574b8940cdb',
  'fixture': 'java_on_demand_contracts.exercise',
  'production': 'src/commands/modules.rs; src/parsers/treesitter/java.rs',
  'contract': 'package/member on-demand type imports with accessibility and ambiguity',
  'input': 'authored Java cases in java_on_demand_contracts.exercise',
  'expected': 'complete authored dependency categories, declaring identities and option envelopes',
  'stop': 'every retained assertion and nested branch executed and equal; failures or omissions block '
          'acceptance'},
 {'feature': 'unused-deps:java-ownership',
  'subject': 'disposable-java-unused-dependencies',
  'samples_count': 3,
  'sample_keys_sha256': '388f2a92458b2cb85978fbc28cc0c501fecde6506fe5eef9798e5f542ba4acb3',
  'population_sha256': '27ea17071b26db8975fff350e72f9028754759ae393983ea27538a4adad74b92',
  'fixture': 'unused_dep_contracts.exercise',
  'production': 'src/commands/modules.rs; src/parsers/treesitter/java.rs',
  'contract': 'literal module boundaries, nested/root owner and sibling decoys',
  'input': 'authored Java cases in unused_dep_contracts.exercise',
  'expected': 'complete authored dependency categories, declaring identities and option envelopes',
  'stop': 'every retained assertion and nested branch executed and equal; failures or omissions block '
          'acceptance'},
 {'feature': 'unused-deps:java-qualified-types',
  'subject': 'disposable-java-import-type-identities',
  'samples_count': 10,
  'sample_keys_sha256': '9c248e145db2a8fe7d1c02ffe1d36f04e888071a08c245242500b252fa52ff97',
  'population_sha256': '8364973d002ae93cf22322a808ed34ada4719c19d3c912f27fad576acf61a071',
  'fixture': 'java_dependency_contracts.exercise',
  'production': 'src/commands/modules.rs; src/parsers/treesitter/java.rs',
  'contract': 'qualified source types, annotations, generic and package collision guards',
  'input': 'authored Java cases in java_dependency_contracts.exercise',
  'expected': 'complete authored dependency categories, declaring identities and option envelopes',
  'stop': 'every retained assertion and nested branch executed and equal; failures or omissions block '
          'acceptance'},
 {'feature': 'unused-deps:java-source-results',
  'subject': 'disposable-java-source-result-ownership',
  'samples_count': 798,
  'sample_keys_sha256': '919f9930b2c12afb5d0781348244805abe9bcddda21c64d430032dbd17ee21f9',
  'population_sha256': '6d1342ca7667bb47e8b66bbfdb5919225d036b4336869394473d357b6227c499',
  'fixture': 'java_dependency_result_contracts.exercise',
  'production': 'src/commands/modules.rs; src/parsers/treesitter/java.rs',
  'contract': 'nominal/generic/record/field/method results, source overload phases and supported JDK '
              'projections, captures and result-site ownership',
  'input': 'authored Java cases in java_dependency_result_contracts.exercise',
  'expected': 'complete authored dependency categories, declaring identities and option envelopes',
  'stop': 'every retained assertion and nested branch executed and equal; failures or omissions block '
          'acceptance'},
 {'feature': 'unused-deps:java-types',
  'subject': 'disposable-java-unused-dependencies',
  'samples_count': 1,
  'sample_keys_sha256': 'c44d36128a44554b1f33f4b4cf59ce4824f7d413f5a51b2dc1ed28ce99f81368',
  'population_sha256': '7023f523e361635f8acceda736d490cb049b8e3619e94060aca4732a3fc136dc',
  'fixture': 'unused_dep_contracts.exercise',
  'production': 'src/commands/modules.rs; src/parsers/treesitter/java.rs',
  'contract': 'class/interface/enum/record declarations and untruncated type population',
  'input': 'authored Java cases in unused_dep_contracts.exercise',
  'expected': 'complete authored dependency categories, declaring identities and option envelopes',
  'stop': 'every retained assertion and nested branch executed and equal; failures or omissions block '
          'acceptance'},
 {'feature': 'unused-deps:java-wildcard-formals',
  'subject': 'disposable-java-wildcard-formals-v1',
  'samples_count': 193,
  'sample_keys_sha256': 'b9f2870d5e0ad35a8ac38acff7372c3d783459fdcad4103e64b7e0b9381a7805',
  'population_sha256': 'd1ed3699cc2acc60d80d020c4cc6fb4d2ad923928bbc8c2a00f9ba6c753d132f',
  'fixture': 'java_wildcard_formal_contracts.exercise',
  'production': 'src/commands/modules.rs; src/parsers/treesitter/java.rs',
  'contract': 'formal wildcard containment, lower constraints, invocation phases and attached imports',
  'input': 'authored Java cases in java_wildcard_formal_contracts.exercise',
  'expected': 'complete authored dependency categories, declaring identities and option envelopes',
  'stop': 'every retained assertion and nested branch executed and equal; failures or omissions block '
          'acceptance'},
 {'feature': 'unused-deps:java-wildcard-results',
  'subject': 'disposable-java-wildcard-results-v1',
  'samples_count': 115,
  'sample_keys_sha256': 'ac92d3b213059d1e0bab300d3f210b7016c47d88c3efa46b287aa396a871c016',
  'population_sha256': '64b89e18ed4aa08d20e9b21225a6c5d4764823911f281c6b3a93d522cf3829e2',
  'fixture': 'java_wildcard_result_contracts.exercise',
  'production': 'src/commands/modules.rs; src/parsers/treesitter/java.rs',
  'contract': 'wildcard upper-bound read projections and write/invariant/access guards',
  'input': 'authored Java cases in java_wildcard_result_contracts.exercise',
  'expected': 'complete authored dependency categories, declaring identities and option envelopes',
  'stop': 'every retained assertion and nested branch executed and equal; failures or omissions block '
          'acceptance'},
 {'feature': 'unused-deps:transitive',
  'subject': 'disposable-java-unused-dependencies',
  'samples_count': 6,
  'sample_keys_sha256': 'd7443e2d9b84ff5415ff01c256f1108f6d2feefda4452392e3760fb6bb0027b0',
  'population_sha256': 'fd7626be37c5bcb633d5a59035ac52db7cd327c5fd1cf849fe4b5fa8cb524156',
  'fixture': 'unused_dep_contracts.exercise',
  'production': 'src/commands/modules.rs; src/parsers/treesitter/java.rs',
  'contract': 'source usage through API exports, deep chains, cycle and diamond termination, implementation '
              'guard',
  'input': 'authored Java cases in unused_dep_contracts.exercise',
  'expected': 'complete authored dependency categories, declaring identities and option envelopes',
  'stop': 'every retained assertion and nested branch executed and equal; failures or omissions block '
          'acceptance'},
 {'feature': 'unused-deps:java-common-bound-inference',
  'subject': 'disposable-java-common-bound-inference-v1',
  'samples_count': 44,
  'sample_keys_sha256': '5f4d140d4965a792c7928b00b2378e78e3e6bf4b3ba2706378dc6b0016cac0d7',
  'population_sha256': '2fdc6de7e15324ec38cd0a68c43a8dcbe19248551b5fdb246d6c8faeb6032149',
  'fixture': 'java_common_bound_contracts.exercise',
  'production': 'src/commands/modules.rs JavaDependencyLookup; src/parsers/treesitter/java.rs',
  'contract': 'independent source/javac/CLI: common source superclass inference, bounded and unbounded '
              'method results, arrays, nested slots, var captures, argument order and incompatible/invariant '
              'guards; not MCP equivalence',
  'input': 'every javac-validated CASES entry in java_common_bound_contracts',
  'expected': 'Base direct only for valid inherited instance() calls; Child direct; invalid '
              'capture/invariant/bound guards leave Base unused',
  'stop': 'all CASES and JSON/text/options/refresh assertions execute and match; unresolved positives remain '
          'failures'},
 {'feature': 'unused-deps:java-retained-inference',
  'subject': 'disposable-java-retained-dependency-inference-v1',
  'samples_count': 24,
  'sample_keys_sha256': '5f49a398856afa45949bad5b0bfd74820a241365d9a0dda18995770dcdcbc41a',
  'population_sha256': 'ed84727a73a93c34bac5ba33a787ce6baa6a2fa2aa2032fe8405b4d2973ee502',
  'fixture': 'java_dependency_gap_contracts.exercise',
  'production': 'src/commands/modules.rs JavaDependencyLookup; src/parsers/treesitter/java.rs',
  'contract': 'independent source/javac/CLI: retained target-dependent invocation, intersection-bound, '
              'invariant capture, generic override and raw unchecked dependency ownership obligations; not '
              'MCP equivalence',
  'input': 'every javac-validated CASES entry in java_dependency_gap_contracts',
  'expected': 'Base direct only for valid inherited instance() calls; Child direct; invalid '
              'capture/invariant/bound guards leave Base unused',
  'stop': 'all CASES and JSON/text/options/refresh assertions execute and match; unresolved positives remain '
          'failures'})

RETAINED += ({
    'feature': 'unused-deps:java-source-classpath-ambiguity',
    'subject': 'duplicate-source-classpath-order',
    'samples_count': 182,
    'sample_keys_sha256': '1d81e282edebaf269e15e27a67b9e98a1d18f0a12246d9be3067409d431103e1',
    'population_sha256': 'fccc7958233f6fb7fd1e1c63d340ee68bd49d2c88aa8f7190f038cd796a8573e',
    'fixture': 'java_classpath_contracts.exercise',
    'production': 'src/commands/modules.rs java_dependency_source',
    'contract': 'explicit unresolved source ownership for equally preferred duplicate Java types',
    'input': 'both descriptor/path orderings, attached roots and API exports, import/qualified/member/receiver sites; javac classpath sensitivity and full inventories',
    'expected': 'nonzero ambiguity without dependency classification for duplicate lookups; unique/local/hidden/unreferenced controls retain authored ownership; refresh rechecks ambiguity',
    'stop': 'every retained ordering, option, format, inventory, javac and refresh branch executes and matches; this is not MCP equivalence',
},)

REVIEWED_SURFACE = 'fa3898263c794d8a021d2b2a75a6520f9dd130c14bd851cf09b0cb48bc22b059'
