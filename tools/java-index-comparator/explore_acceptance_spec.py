"""Fixed Java exploration checklist, independently reviewed from the pending obligations.

Every owning fixture supplies concrete authored input, expected source identities
and bounded stopping conditions. Retain all Java cases, including failed result
slots; graph/format/scope acceptance alone cannot establish exploration semantics.
No equivalent MCP explore API exists. These are source/javac/CLI contracts.
"""
from pathlib import Path
import re
from common import stable_id


def cli_surface():
    source = (Path(__file__).resolve().parents[2] / 'src/main.rs').read_text()
    cli = source.split('struct Cli {', 1)[1].split('\n}', 1)[0]
    explore = source.split('    Explore {', 1)[1].split('\n    },', 1)[0]
    return stable_id([re.sub(r'\s+', '', re.sub(r'^\s*//[^\n]*', '', body, flags=re.MULTILINE))
                      for body in (cli, explore)])


def criteria():
    return [dict(item) for item in RETAINED]


RETAINED = ({'feature': 'explore',
  'subject': 'disposable-java-context',
  'samples_count': 17,
  'sample_keys_sha256': '8e9ad20c6458a6e75627f1ba4905c87656fa197ba62c3e6e9f13b050bd07aad6',
  'population_sha256': 'cfe6b7cd4706d068ded26801532a3e349ad7d2cdf25fb911ce3fd3f0c9c7a744',
  'contract': 'independent source/state: disposable Java invocations, caller ownership, depth/filter/limits '
              'and exploration source/outline/scoped neighbours; not MCP equivalence',
  'fixture': 'context_contracts.exercise',
  'production': 'src/commands/explore.rs; src/commands/graph/{mod,resolve,java}.rs',
  'input': 'authored Java fixture context_contracts',
  'expected': 'complete authored declaration identities, caller/subclass roles, source/root ownership, '
              'ranking and option envelopes',
  'stop': 'all retained assertion keys and nested population branches executed against the current CLI'},
 {'feature': 'explore:java-array-receivers',
  'subject': 'disposable-java-array-receivers-v1',
  'samples_count': 6,
  'sample_keys_sha256': '1e4e1d5f82e89a30dcda962acdf8e84fb8a38f15362e2349e585d344565b2602',
  'population_sha256': 'd7864d487743950bd88aa7f08814f89be469d88c4ee7e45249021360951931e9',
  'contract': 'independent source/javac/CLI: array rank and explicit generic element projections across '
              'fields, inherited owners, method/record results, declaration sites, captures and attached '
              'roots; not MCP equivalence',
  'fixture': 'java_array_receiver_contracts.exercise',
  'production': 'src/commands/explore.rs; src/commands/graph/{mod,resolve,java}.rs',
  'input': 'authored Java fixture java_array_receiver_contracts',
  'expected': 'complete authored declaration identities, caller/subclass roles, source/root ownership, '
              'ranking and option envelopes',
  'stop': 'all retained assertion keys and nested population branches executed against the current CLI'},
 {'feature': 'explore:java-chained-generic-receiver-sites',
  'subject': 'disposable-java-local-class-shadows',
  'samples_count': 1,
  'sample_keys_sha256': 'ff4b26ba788bce69afd23688df56c47deebcf51dbc2bd06397e6e40cf71b87c3',
  'population_sha256': '54baf9b4d13115a329f644a5b72e00b68034e771355881d81dae0c291b5347b6',
  'contract': 'independent source/state: disposable javac-validated Java local classes, '
              'member/package/import shadows, declaration/block/sibling boundaries, nested types and static '
              'qualifiers, graph pages/reverse/path and exploration; direct/chained nominal and generic '
              'receiver declaration byte sites across later local shadows; not MCP equivalence or '
              'compiler-wide receiver dispatch',
  'fixture': 'java_local_class_contracts.exercise',
  'production': 'src/commands/explore.rs; src/commands/graph/{mod,resolve,java}.rs',
  'input': 'authored Java fixture java_local_class_contracts',
  'expected': 'complete authored declaration identities, caller/subclass roles, source/root ownership, '
              'ranking and option envelopes',
  'stop': 'all retained assertion keys and nested population branches executed against the current CLI'},
 {'feature': 'explore:java-colliding-type-sites',
  'subject': 'disposable-java-colliding-type-sites',
  'samples_count': 19,
  'sample_keys_sha256': '41ea5a5b642d20f9a20fd08da700a178a2efc9dd80a71386af1c63360b6e51f0',
  'population_sha256': '3b27e8c07d4f422674a713feb27e3f29453bca8fabf6ad2460d9ffe5e0337655',
  'contract': 'independent source/state: javac-validated same-line local type occurrences, member ownership, '
              'receiver sites, block/sibling boundaries, distinct parents, generic class/method parameter '
              'order, return variables and bounds, implicit record accessor sites, graph pages/reverse/path '
              'and RWR callers; not MCP equivalence',
  'fixture': 'java_colliding_type_contracts.exercise',
  'production': 'src/commands/explore.rs; src/commands/graph/{mod,resolve,java}.rs',
  'input': 'authored Java fixture java_colliding_type_contracts',
  'expected': 'complete authored declaration identities, caller/subclass roles, source/root ownership, '
              'ranking and option envelopes',
  'stop': 'all retained assertion keys and nested population branches executed against the current CLI'},
 {'feature': 'explore:java-exception-resource-scopes',
  'subject': 'disposable-java-exception-resource-scopes',
  'samples_count': 3,
  'sample_keys_sha256': 'ef07249792af0e56e82496f9ccdbbda9b5575ca203d705a4d6f83b9509eadf63',
  'population_sha256': '33eb3b753300b5eb316aafe0e82c52894f4a8a8444c620e722fa0174f84e12ba',
  'contract': 'independent source/state: disposable javac-validated Java catch parameters, typed/inferred '
              'resources, shadowing, catch/finally boundaries, graph pages/reverse edges and explore '
              'callers; multi-catch unresolved-type guards; not MCP equivalence or compiler-wide dispatch',
  'fixture': 'java_exception_scope_contracts.exercise',
  'production': 'src/commands/explore.rs; src/commands/graph/{mod,resolve,java}.rs',
  'input': 'authored Java fixture java_exception_scope_contracts',
  'expected': 'complete authored declaration identities, caller/subclass roles, source/root ownership, '
              'ranking and option envelopes',
  'stop': 'all retained assertion keys and nested population branches executed against the current CLI'},
 {'feature': 'explore:java-generic-fields',
  'subject': 'disposable-java-generic-fields-v1',
  'samples_count': 12,
  'sample_keys_sha256': '525ae6e13e635181320587b68d1492038222a36d7a8de43de4b094b6ba21cfce',
  'population_sha256': '60434b7458bbfe87fda59e5afc67133bc9213895c09315c76d84e1f9e54b85b7',
  'contract': 'independent source/state: javac-validated explicit generic field parameters, ordered/nested '
              'projections, bounds, same-line declaration ownership, captures, references, attached roots '
              'and graph pages/reverse/path/RWR callers; inherited class field mappings, raw erasure, hiding '
              'and declaration-site parent arguments, bare/this/super/lambda receivers and '
              'static/local-shadow guards; generic arrays and other semantic obligations remain pending; not '
              'MCP equivalence',
  'fixture': 'java_generic_field_contracts.exercise',
  'production': 'src/commands/explore.rs; src/commands/graph/{mod,resolve,java}.rs',
  'input': 'authored Java fixture java_generic_field_contracts',
  'expected': 'complete authored declaration identities, caller/subclass roles, source/root ownership, '
              'ranking and option envelopes',
  'stop': 'all retained assertion keys and nested population branches executed against the current CLI'},
 {'feature': 'explore:java-inherited-parent-bindings',
  'subject': 'disposable-java-inherited-parent-bindings',
  'samples_count': 4,
  'sample_keys_sha256': '96008c0f30db676b82bdd41a19b817e0290aff5c4c79e318269d3bfd57147b77',
  'population_sha256': 'a42112ca35ce1064b0da8f792eb217dcf792c708d44187c7fb2c772242d60bb7',
  'contract': 'independent source/state: disposable javac-validated Java inherited parent aliases, hidden '
              'enclosing owners, qualified/static/lexical bindings, precedence and access guards, opposite '
              'file orders, graph pages/reverse edges and explore callers; not MCP equivalence or '
              'compiler-wide dispatch',
  'fixture': 'java_parent_contracts.exercise',
  'production': 'src/commands/explore.rs; src/commands/graph/{mod,resolve,java}.rs',
  'input': 'authored Java fixture java_parent_contracts',
  'expected': 'complete authored declaration identities, caller/subclass roles, source/root ownership, '
              'ranking and option envelopes',
  'stop': 'all retained assertion keys and nested population branches executed against the current CLI'},
 {'feature': 'explore:java-inherited-types',
  'subject': 'disposable-java-inherited-types',
  'samples_count': 2,
  'sample_keys_sha256': 'f84572abeffd92a44e183519998d84c8cf5b7da5facc432dd137934fbe129c19',
  'population_sha256': '0a5346dbb4d863831c45bb69711a0589d58144258b4a28c61a75c91cfb4c89a4',
  'contract': 'independent source/state: disposable Java inherited member types, qualified/lexical names, '
              'explicit/on-demand static imports, hidden declaring owners, hiding, diamond identity and '
              'access guards; graph pages/reverse traversal and exploration; not MCP equivalence or '
              'compiler-wide binding',
  'fixture': 'java_inherited_type_contracts.exercise',
  'production': 'src/commands/explore.rs; src/commands/graph/{mod,resolve,java}.rs',
  'input': 'authored Java fixture java_inherited_type_contracts',
  'expected': 'complete authored declaration identities, caller/subclass roles, source/root ownership, '
              'ranking and option envelopes',
  'stop': 'all retained assertion keys and nested population branches executed against the current CLI'},
 {'feature': 'explore:java-local-class-receiver-sites',
  'subject': 'disposable-java-local-class-shadows',
  'samples_count': 1,
  'sample_keys_sha256': '03b1fb7d8d54d016c6ef1b316c3907b4147f15ff4370a6d7b73976367746770c',
  'population_sha256': '73730cd091efea59920ce7778288f75c72646f936f2d66384f536bb132351392',
  'contract': 'independent source/state: disposable javac-validated Java local classes, '
              'member/package/import shadows, declaration/block/sibling boundaries, nested types and static '
              'qualifiers, graph pages/reverse/path and exploration; direct/chained nominal and generic '
              'receiver declaration byte sites across later local shadows; not MCP equivalence or '
              'compiler-wide receiver dispatch',
  'fixture': 'java_local_class_contracts.exercise',
  'production': 'src/commands/explore.rs; src/commands/graph/{mod,resolve,java}.rs',
  'input': 'authored Java fixture java_local_class_contracts',
  'expected': 'complete authored declaration identities, caller/subclass roles, source/root ownership, '
              'ranking and option envelopes',
  'stop': 'all retained assertion keys and nested population branches executed against the current CLI'},
 {'feature': 'explore:java-local-class-shadows',
  'subject': 'disposable-java-local-class-shadows',
  'samples_count': 1,
  'sample_keys_sha256': 'ff4b26ba788bce69afd23688df56c47deebcf51dbc2bd06397e6e40cf71b87c3',
  'population_sha256': 'b9d9a3ba97812b47eb2a7347bccc65fe7bbc41de773ebf84c0b77a04aa8aa529',
  'contract': 'independent source/state: disposable javac-validated Java local classes, '
              'member/package/import shadows, declaration/block/sibling boundaries, nested types and static '
              'qualifiers, graph pages/reverse/path and exploration; direct/chained nominal and generic '
              'receiver declaration byte sites across later local shadows; not MCP equivalence or '
              'compiler-wide receiver dispatch',
  'fixture': 'java_local_class_contracts.exercise',
  'production': 'src/commands/explore.rs; src/commands/graph/{mod,resolve,java}.rs',
  'input': 'authored Java fixture java_local_class_contracts',
  'expected': 'complete authored declaration identities, caller/subclass roles, source/root ownership, '
              'ranking and option envelopes',
  'stop': 'all retained assertion keys and nested population branches executed against the current CLI'},
 {'feature': 'explore:java-local-interface-scopes',
  'subject': 'disposable-java-local-interface-scopes',
  'samples_count': 1,
  'sample_keys_sha256': 'ff4b26ba788bce69afd23688df56c47deebcf51dbc2bd06397e6e40cf71b87c3',
  'population_sha256': 'b7c6f91fc405de6293acfb5d39a15b0bac143197674747ce17a453afcefe1ffb',
  'contract': 'independent source/state: disposable javac-validated Java local interfaces, '
              'declaration/block/method boundaries, sibling identities, nested types and static members, '
              'graph pages/reverse/path and exploration; not MCP equivalence or compiler-wide receiver '
              'dispatch',
  'fixture': 'java_local_interface_contracts.exercise',
  'production': 'src/commands/explore.rs; src/commands/graph/{mod,resolve,java}.rs',
  'input': 'authored Java fixture java_local_interface_contracts',
  'expected': 'complete authored declaration identities, caller/subclass roles, source/root ownership, '
              'ranking and option envelopes',
  'stop': 'all retained assertion keys and nested population branches executed against the current CLI'},
 {'feature': 'explore:java-pattern-flow-scopes',
  'subject': 'disposable-java-pattern-flow-scopes',
  'samples_count': 9,
  'sample_keys_sha256': 'd8b3589c74996bb43bda88c77b36ab64121f66a598bac6f081166e0875f275b5',
  'population_sha256': '2d2f4d43e5a709903c0a5e0b9c28ea583a8effbdf03e23290afcff71d76752cc',
  'contract': 'independent source/state: disposable javac-validated Java instanceof receivers, boolean '
              'short-circuit scopes, negated/ternary branches, loop bodies/updates and field shadow '
              'boundaries, nested abrupt guards, labelled/unlabelled jumps and condition-exit continuations '
              'through graph pages/reverse/path, explore and call-tree; not MCP equivalence or compiler-wide '
              'flow/dispatch',
  'fixture': 'java_pattern_scope_contracts.exercise',
  'production': 'src/commands/explore.rs; src/commands/graph/{mod,resolve,java}.rs',
  'input': 'authored Java fixture java_pattern_scope_contracts',
  'expected': 'complete authored declaration identities, caller/subclass roles, source/root ownership, '
              'ranking and option envelopes',
  'stop': 'all retained assertion keys and nested population branches executed against the current CLI'},
 {'feature': 'explore:java-resolved-receivers',
  'subject': 'disposable-java-receiver-bindings',
  'samples_count': 2,
  'sample_keys_sha256': 'eeb2b8bc3dbf8c1bd78aa2ced97ed92334152c0edf958d5ce8291013d56fde0f',
  'population_sha256': '439f8912c79648d49f2fb62cde734ed8446fd9ae7e460b0129bdddc2a8a3e19e',
  'contract': 'independent source/state: disposable Java explicit parameter types, nonstatic '
              'import/package/nested binding, overload arity, unresolved receiver guards and graph-backed '
              'exploration; not MCP equivalence or compiler-wide dispatch',
  'fixture': 'java_receiver_contracts.exercise',
  'production': 'src/commands/explore.rs; src/commands/graph/{mod,resolve,java}.rs',
  'input': 'authored Java fixture java_receiver_contracts',
  'expected': 'complete authored declaration identities, caller/subclass roles, source/root ownership, '
              'ranking and option envelopes',
  'stop': 'all retained assertion keys and nested population branches executed against the current CLI'},
 {'feature': 'explore:java-same-line-receiver-sites',
  'subject': 'disposable-java-local-class-shadows',
  'samples_count': 6,
  'sample_keys_sha256': 'b981b4db3a7ffd096edddb7f0ee1291be16b426a853170f99623b51aac6f5113',
  'population_sha256': '6abae0ba20267cffa3cefaa25edb1e4a6a496f704b2af01b0cc0d2255c57182e',
  'contract': 'independent source/state: disposable javac-validated Java local classes, '
              'member/package/import shadows, declaration/block/sibling boundaries, nested types and static '
              'qualifiers, graph pages/reverse/path and exploration; direct/chained nominal and generic '
              'receiver declaration byte sites across later local shadows; not MCP equivalence or '
              'compiler-wide receiver dispatch',
  'fixture': 'java_local_class_contracts.exercise',
  'production': 'src/commands/explore.rs; src/commands/graph/{mod,resolve,java}.rs',
  'input': 'authored Java fixture java_local_class_contracts',
  'expected': 'complete authored declaration identities, caller/subclass roles, source/root ownership, '
              'ranking and option envelopes',
  'stop': 'all retained assertion keys and nested population branches executed against the current CLI'},
 {'feature': 'explore:java-type-access',
  'subject': 'disposable-java-type-access',
  'samples_count': 2,
  'sample_keys_sha256': '07fc2ab72f91cb9fc13a9f25ac78c6290961ca9cb69948cd874d1305e626b4db',
  'population_sha256': '84e203e6a6d806a8cd889a835a50c78b1832bf645a2656c01893f2fcd1d8776f',
  'contract': 'independent source/state: disposable Java public/package/private/protected type '
              'accessibility, enclosing type access and direct static nested type imports, graph traversal '
              'and exploration; not MCP equivalence or compiler-wide binding',
  'fixture': 'java_type_access_contracts.exercise',
  'production': 'src/commands/explore.rs; src/commands/graph/{mod,resolve,java}.rs',
  'input': 'authored Java fixture java_type_access_contracts',
  'expected': 'complete authored declaration identities, caller/subclass roles, source/root ownership, '
              'ranking and option envelopes',
  'stop': 'all retained assertion keys and nested population branches executed against the current CLI'},
 {'feature': 'explore:java-type-bindings',
  'subject': 'disposable-java-type-bindings',
  'samples_count': 3,
  'sample_keys_sha256': '2f4197bdd525c57202705fb613789bbd17a0241151f3a9323e5862b0065cfb0a',
  'population_sha256': 'd8890e6dd6e571b2d266bb22c4f54db5d49353de64a60353682429e3db61bbcd',
  'contract': 'independent source/state: disposable Java syntax type references, explicit/on-demand imports, '
              'package/nested/qualified names, external/generic/collision guards and graph '
              'traversal/exploration; not MCP equivalence or compiler-wide binding',
  'fixture': 'java_type_binding_contracts.exercise',
  'production': 'src/commands/explore.rs; src/commands/graph/{mod,resolve,java}.rs',
  'input': 'authored Java fixture java_type_binding_contracts',
  'expected': 'complete authored declaration identities, caller/subclass roles, source/root ownership, '
              'ranking and option envelopes',
  'stop': 'all retained assertion keys and nested population branches executed against the current CLI'},
 {'feature': 'explore:ranking-budgets',
  'subject': 'disposable-java-exploration-budgets',
  'samples_count': 43,
  'sample_keys_sha256': '8a3b5a3e0a18603c565c2438cc73c0571536bea04cc7328cdbabd448db2b9adb',
  'population_sha256': 'bb872ea5c3435c5cc655af2bb9927c9c142cf2b2dbba7ee29773e03edb861854',
  'contract': 'independent source/state: disposable Java lexical seed budgets, ranking scores, RWR blend, '
              'root identities and nearest exact test candidates; not MCP equivalence or Java semantic '
              'dispatch',
  'fixture': 'explore_contracts.exercise',
  'production': 'src/commands/explore.rs; src/commands/graph/{mod,resolve,java}.rs',
  'input': 'authored Java fixture explore_contracts',
  'expected': 'complete authored declaration identities, caller/subclass roles, source/root ownership, '
              'ranking and option envelopes',
  'stop': 'all retained assertion keys and nested population branches executed against the current CLI'},
 {'feature': 'global:format:java-exploration',
  'subject': 'disposable-java-exploration-formats',
  'samples_count': 74,
  'sample_keys_sha256': '4714ff031382b12bd62538eecd344f24b54c10985558202019468b2255231d7e',
  'population_sha256': '14c07dd13cb4a893ca6a52d2d7defa275685983f6fd8b6d09a74c7fa9223fbe9',
  'contract': 'independent source/state: disposable Java exploration and ranked/unranked intent fallback '
              'identities, kind filters, budgets, source/outline truncation, empty/missing index, test '
              'ownership and colliding-root JSON/text rendering; not MCP equivalence or compiler-wide '
              'dispatch',
  'fixture': 'exploration_format_contracts.exercise',
  'production': 'src/commands/explore.rs; src/commands/graph/{mod,resolve,java}.rs',
  'input': 'authored Java fixture exploration_format_contracts',
  'expected': 'complete authored declaration identities, caller/subclass roles, source/root ownership, '
              'ranking and option envelopes',
  'stop': 'all retained assertion keys and nested population branches executed against the current CLI'},
 {'feature': 'global:format:java-intent-fallback',
  'subject': 'disposable-java-exploration-formats',
  'samples_count': 167,
  'sample_keys_sha256': 'f46e0d3f643406ba9c902a6c6d03a4464de57777019d83021f4815a81180acb4',
  'population_sha256': '6fc934dc391832324fa3b16831bd870125cbcc8729865a8709a4f6fddb30e470',
  'contract': 'independent source/state: disposable Java exploration and ranked/unranked intent fallback '
              'identities, kind filters, budgets, source/outline truncation, empty/missing index, test '
              'ownership and colliding-root JSON/text rendering; not MCP equivalence or compiler-wide '
              'dispatch',
  'fixture': 'format_contracts.exercise',
  'production': 'src/commands/explore.rs; src/commands/graph/{mod,resolve,java}.rs',
  'input': 'authored Java fixture format_contracts',
  'expected': 'complete authored declaration identities, caller/subclass roles, source/root ownership, '
              'ranking and option envelopes',
  'stop': 'all retained assertion keys and nested population branches executed against the current CLI'},
 {'feature': 'global:scope:java-explore',
  'subject': 'disposable-java-analysis-exploration-scope',
  'samples_count': 122,
  'sample_keys_sha256': 'a6482b4d51d5a24042bef8b36bca31aa288c356241b75c8581b09b7048e88056',
  'population_sha256': '375f02fd5dbedd71dd424e70ea74da3860f94c18d67081e789f6f70631d7746e',
  'contract': 'independent source/state: disposable javac-validated Java analysis/exploration directory/root '
              'intersections, literal module aliases, export selection, ordered limits, source/test '
              'ownership, fresh/unbuilt graph neighbours and text/JSON identities; unused analysis retains '
              'its indexed name-reference heuristic; not MCP equivalence or compiler-wide dispatch',
  'fixture': 'analysis_scope_contracts.exercise',
  'production': 'src/commands/explore.rs; src/commands/graph/{mod,resolve,java}.rs',
  'input': 'authored Java fixture analysis_scope_contracts',
  'expected': 'complete authored declaration identities, caller/subclass roles, source/root ownership, '
              'ranking and option envelopes',
  'stop': 'all retained assertion keys and nested population branches executed against the current CLI'})

REVIEWED_SURFACE = 'a4309e28a955dc386f0e8542da541dc173b22078892997a0266218f67763158b'

RETAINED += ({'feature': 'explore:java-graph-state-ownership',
  'subject': 'disposable-java-explore-graph-state-ownership-v1',
  'samples_count': 84,
  'sample_keys_sha256': '0bda1b11ed16c1f7fe62214b4f04b14ae23e2b7305ff0e686482ee818e91e592',
  'population_sha256': 'e6d1ae1ed14462c426fadcd89c6313f74b4701f1159545495c563b5de68d9aa2',
  'fixture': 'explore_freshness_contracts.exercise',
  'production': 'src/commands/explore.rs; src/commands/graph/{mod,resolve,java}.rs',
  'contract': 'independent source/javac/CLI: Java RWR resolved and empty caller ownership with '
              'absent/fresh/stale graphs, nominal/generic/overload/reference receivers, directory and '
              'attached-root intersections, source refresh and read-only graph state; not MCP equivalence',
  'input': 'each javac-validated source/receiver case in explore_freshness_contracts',
  'expected': 'exact authored Java caller identities and graph-state envelopes; colliding out-of-directory '
              'and foreign-root decoys excluded',
  'stop': 'all source cases in absent/fresh graph states, retained failed Java cases must execute and pass'},
 {'feature': 'explore:java-inferred-result-slots',
  'subject': 'disposable-java-explore-inferred-result-slots-v1',
  'samples_count': 14,
  'sample_keys_sha256': '76a9d377c410573690dc01f6176125d00f7f8df9df26ad06f7ca90749493eafd',
  'population_sha256': 'bb59cde711e7e3292738274888e75dd8f52cc0fd9a3034117db9c6b7fce6ae9d',
  'fixture': 'explore_result_contracts.exercise',
  'production': 'src/commands/explore.rs; src/commands/graph/{mod,resolve,java}.rs',
  'contract': 'independent source/javac/CLI: Java inferred array/matrix/parameterized/nested method results, '
              'repeated common-bound and assignment-target inference, same-line overload seed unions and '
              'directory-owned RWR neighbours; not MCP equivalence',
  'input': 'each javac-validated source/receiver case in explore_result_contracts',
  'expected': 'exact authored Java caller identities and graph-state envelopes; colliding out-of-directory '
              'and foreign-root decoys excluded',
  'stop': 'all source cases in absent/fresh graph states, retained failed Java cases must execute and pass'})
