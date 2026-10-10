"""Finite Java selector acceptance, fixed independently of current verdicts.

The named fixtures supply concrete inputs/expected identities and full finite
positive/negative populations. Fingerprints retain their complete nested shape;
only ephemeral authored-fixture directory prefixes in assertion keys are folded.
Expected/actual values are never normalized or replaced by this acceptance gate.
"""
from pathlib import Path
import re

from common import stable_id

# Command options and nested graph/subtree actions are reviewed together. A new
# option, changed default, or command requires an explicit checklist review.
SURFACE_TYPES = ('Cli', 'Commands', 'SubtreeAction', 'GraphSymbolArgs', 'GraphAction')


def cli_surface():
    from audit import JAVA_EXCLUDED_FEATURES
    source = (Path(__file__).resolve().parents[2] / 'src/main.rs').read_text()
    bodies = {}
    for name in SURFACE_TYPES:
        body = re.split(r'(?:struct|enum) ' + name + r' \{', source, maxsplit=1)[1].split('\n}', 1)[0]
        # Ignore documentation/formatting, retain every advertised argument.
        body = re.sub(r'^\s*//[^\n]*', '', body, flags=re.MULTILINE)
        if name == 'Commands':
            blocks = re.split(r'(?=^    [A-Z][A-Za-z0-9]*\s*(?:\{|,))', body, flags=re.MULTILINE)
            applicable = []
            for block in blocks:
                match = re.match(r'    ([A-Z][A-Za-z0-9]*)', block)
                if match is None:
                    continue
                command = re.sub(r'([a-z0-9])([A-Z])', r'\1-\2',
                                 re.sub(r'(.)([A-Z][a-z]+)', r'\1-\2', match[1])).lower()
                if command not in JAVA_EXCLUDED_FEATURES:
                    applicable.append(block)
            body = ''.join(applicable)
        bodies[name] = re.sub(r'\s+', '', body)
    return stable_id(bodies)


def assertion_keys(samples, feature):
    prefixes = {'global:scope:java-file-views': 'file-scope-',
                'global:scope:java-module-roots': 'module-roots-'}
    prefix = prefixes.get(feature)
    if not prefix:
        return sorted(samples)
    pattern = r'/[^:]*?/[.]artifacts/[^:]*?/' + prefix + r'[^/:]+/'
    return sorted(re.sub(pattern, '$FIXTURE/', key) for key in samples)


def population_shape(samples, feature):
    def shape(value):
        if isinstance(value, dict):
            return {key: shape(item) for key, item in value.items()}
        if isinstance(value, (list, tuple)):
            return [shape(item) for item in value]
        return type(value).__name__
    keys = assertion_keys(samples, feature)
    # Map originals individually; reject a normalization collision rather than
    # silently collapsing distinct absolute/relative selector assertions.
    normalized = {assertion_keys({key: value}, feature)[0]: shape(value)
                  for key, value in samples.items()}
    if len(normalized) != len(samples) or len(set(keys)) != len(keys):
        raise ValueError('scope assertion identity normalization collision')
    return stable_id(normalized)


# Each entry is (fixture module, production path, concrete contract).
CONTRACTS = {
    'navigation_scope_contracts': ('src/commands/analysis.rs and src/commands/mod.rs',
        'class/symbol/implementations/hierarchy/refs/usages/search Probe declarations: cwd/file/module literal intersections and roots; exact source identities, pre-limit totals, zero/one/full pages and text'),
    'file_scope_contracts': ('src/commands/files.rs',
        'file/outline/imports/api View.java in primary/attached/other roots: relative/absolute paths and aliases, symlink/overlap ownership, exact import/API identities and zero/one/full pages'),
    'caller_scope_contracts': ('src/commands/grep.rs',
        'callers/call-tree ping on colliding Java roots: literal file/cwd intersection, owning method trees, bounded depth/limits, stale/missing graphs and JSON/text'),
    'insight_scope_contracts': ('src/commands/project_info.rs',
        'map/conventions Alpha/Beta/Gamma Java sources: cwd/root/module intersection, aggregate module counts, framework/naming identities, per-dir limits and empty selectors'),
    'analysis_scope_contracts': ('src/commands/analysis.rs and src/commands/explore.rs',
        'unused-symbols/explore ScopeProbe on colliding roots: module/cwd/root ownership, exact selected declarations/neighbours/tests, export/kind filters and unbuilt graphs'),
    'module_scope_contracts': ('src/commands/modules.rs',
        'module/deps/dependents/unused-deps/module-route scope_/app Maven modules: induced directory intersection; selected node/edge identities, relative/absolute paths, empty/zero/full pages'),
    'module_root_contracts': ('src/commands/modules.rs',
        'same app/live/dead Maven coordinates in three roots: selected owner and coordinate binding, induced dependency/reverse/routes, strict classifications, aliases, schema migration and pages'),
    'module_alias_contracts': ('src/commands/modules.rs',
        'colliding Java module aliases: qualified selected-root aliases resolve; unqualified ambiguity and missing/empty selectors retain exact error candidates and roots'),
    'graph_root_contracts': ('src/commands/graph/mod.rs',
        'dependencies/dependents/impact/path/cycles/top/metrics Probe on colliding Java roots: selected seeds/edges, induced traversal, JSON/text identities and zero/one/full pages'),
    'graph_directory_contracts': ('src/commands/graph/mod.rs',
        'graph Probe/Bridge cycles across scope_/outside: cwd/root/file/kind intersections, induced traversal never leaves scope; counts/seeds/path endpoints and zero/one/full pages'),
    'root_contracts': ('src/main.rs and src/commands/management.rs',
        'RootProbe in primary/named/legacy roots: registration/removal/dedup/forced overlaps, local/subtree/walk-up/explicit-root precedence, declarations and lexical source sites before limits'),
    'format_contracts': ('src/commands/grep.rs',
        'todo/deprecated/suppress/inject/provides/annotations/deeplinks Java fixture: colliding roots select the exact lexical sites before limit in JSON/text; foreign-only readers excluded'),
    'selector_error_contracts': ('src/main.rs',
        'every advertised Java command: local+subtree conflicts, changed/hotspots subtree rejection, graph build/status rejection, root/version precedence and missing index; rejected requests preserve sources/index'),
    'root_error_contracts': ('src/main.rs and src/db.rs',
        'Java explicit-root, unavailable mount, root aliases and damaged root metadata: bounded error/preference controls preserve source/index ownership'),
    'scan_error_contracts': ('src/commands/grep.rs',
        'selected Java scans: unreadable/invalid UTF-8/walk errors inside selection, outside-scope errors do not leak; empty and zero-page controls'),
    'freshness_error_contracts': ('src/commands/graph/mod.rs and src/commands/index.rs',
        'every Java graph refresh consumer: missing/stale indexes, selected root errors, foreground/background contention; bounded recovery preserves established generation'),
    'management_format_contracts': ('src/commands/management.rs',
        'stats/query/schema/version/db-path/list-roots/subtree list: explicit project index ownership, empty/missing indexes; SQL/schema are whole-index APIs, not implicit row filtering'),
    'mutation_format_contracts': ('src/commands/management.rs',
        'add/remove root and subtree mutations: disposable root metadata only; root/error precedence and JSON/text ownership, no mutations on target or real installers'),
    'watch_scope_contracts': ('src/commands/watch.rs',
        'Java attached-root create/change/delete events and unavailable/recreated roots: owner identities, live root/config filters and bounded notification waits'),
    'search_format_contracts': ('src/commands/mod.rs',
        'search Java literal/rank presets on colliding roots: type/file/module/test/root intersections, all sections and exact zero/one/full pages'),
    'exploration_format_contracts': ('src/commands/explore.rs and src/commands/mod.rs',
        'explore and search intent fallback SignalProbe: root/file/kind/test intersections, selected seeds/neighbours/source/outline and bounded budgets'),
    'java_module_binding_contracts': ('src/commands/modules.rs',
        'unused-deps selected Java app/lib classpaths: owner metadata for direct/package/member/static imports, API exports, visibility/local precedence and strict JSON/text classifications'),
    'java_inherited_import_contracts': ('src/commands/modules.rs',
        'unused-deps Java inherited/member/type/value imports and nominal chains in colliding roots: declaring provenance, lexical/value/record/local-subclass access and hiding guards, options/refresh'),
    'java_nested_array_contracts': ('src/commands/modules.rs and src/parsers/treesitter/java.rs',
        'unused-deps nested Java array signature slots: fixed/class/method variables, invariant rank and primitive guards, ancestor projection and attached provider ownership after refresh; unresolved semantic parents remain pending'),
    'java_dependency_result_contracts': ('src/commands/modules.rs',
        'unused-deps Java nominal field/return chains across attached modules: declaring identity, private/protected/this/super access, arity/ambiguous/array/static-context guards, class-variable formals through inheritance/arrays/varargs/boxing, scalar and invariant parameterized method-variable inference and explicit witnesses with separate bounds/shadowing/overload guards and option/refresh controls'),
    'android_dependency_contracts': ('src/commands/android.rs and src/commands/modules.rs',
        'Java resource ownership: same resource names across attached modules, dependency R modes and literal namespace metadata; declaring root and unused-deps positives/negatives; XML-only syntax excluded'),
}

# Reviewed command ownership: never inferred from whichever children pass.
COMMAND_GROUPS = {
    'navigation_scope_contracts': 'class symbol implementations hierarchy refs usages search',
    'file_scope_contracts': 'file outline imports api',
    'caller_scope_contracts': 'callers call-tree',
    'insight_scope_contracts': 'map conventions',
    'analysis_scope_contracts': 'unused-symbols explore',
    'module_scope_contracts': 'module deps dependents unused-deps module-route',
    'graph_root_contracts': 'graph',
    'root_contracts': 'add-root remove-root subtree detect-stacks',
    'format_contracts': 'todo deprecated suppress inject provides annotations deeplinks',
    'management_format_contracts': 'stats query schema version db-path list-roots watch-status',
    'mutation_format_contracts': 'install-claude-plugin install-codex-mcp install-git-hooks',
    'freshness_error_contracts': 'rebuild update restore clear',
    'watch_scope_contracts': 'watch',
    'java_resource_scope_contracts': 'resource-usages xml-usages',
    'vcs_contracts': 'changed hotspots',
    'project_format_contracts': 'agrep',
}
CONTRACTS['vcs_contracts'] = ('src/commands/grep.rs',
    'changed/hotspots disposable Java Git history: cwd/path/exclude-test selectors, primary-only history and rejected subtree guards; exact file ownership')
CONTRACTS['project_format_contracts'] = ('src/commands/grep.rs',
    'agrep explicit-root selection: disposable Java provider invocation and fallback/exit propagation; root errors and shared conflicts execute separately')
CONTRACTS['java_resource_scope_contracts'] = ('src/commands/android.rs',
    'Java R usages and Java Widget layout references in colliding Android modules/roots: local/subtree/cwd/module/type intersections before 10/100 caps; unused definitions stay used by references outside cwd')


def required_acceptance_features():
    import audit
    selected = {feature for feature in audit.required_features() - audit.JAVA_EXCLUDED_FEATURES
                if feature.startswith('global:scope:')}
    # Explicit shared roots/errors and Java ownership contracts, independent of
    # statuses. Public mutation probes stay inside disposable .artifacts roots.
    selected.update(EXTRA_FEATURES)
    return selected


def unmapped_commands():
    from audit import JAVA_EXCLUDED_FEATURES
    source = (Path(__file__).resolve().parents[2] / 'src/main.rs').read_text()
    body = source.split('enum Commands {', 1)[1].split('\n}', 1)[0]
    names = {re.sub(r'([a-z0-9])([A-Z])', r'\1-\2',
                    re.sub(r'(.)([A-Z][a-z]+)', r'\1-\2', variant)).lower()
             for variant in re.findall(r'^    ([A-Z][A-Za-z0-9]*)\s*(?:\{|,)', body, re.MULTILINE)}
    mapped = {name for names in COMMAND_GROUPS.values() for name in names.split()}
    return sorted(names - JAVA_EXCLUDED_FEATURES - mapped)


def criteria():
    result = []
    for feature, subject, count, keys_digest, shape_digest, module in RETAINED:
        production, contract = CONTRACTS[module]
        result.append(dict(feature=feature, subject=subject, samples_count=count,
                           sample_keys_sha256=keys_digest, population_sha256=shape_digest,
                           fixture=module + '.exercise', production=production, contract=contract))
    return result


EXTRA_FEATURES = (
    'add-root',
    'changed',
    'global:explicit-root',
    'global:format:java-agrep',
    'global:format:java-analysis',
    'global:format:java-exploration',
    'global:format:java-freshness-errors',
    'global:format:java-index-availability',
    'global:format:java-installation',
    'global:format:java-intent-fallback',
    'global:format:java-management-read-only',
    'global:format:java-ranked-search',
    'global:format:java-root-errors',
    'global:format:java-root-mutations',
    'global:format:java-scan-errors',
    'global:format:java-search',
    'global:format:java-selector-errors',
    'global:format:java-text-search-roots',
    'global:format:java-watch-scope',
    'global:local',
    'global:subtree',
    'global:walk-up',
    'hotspots',
    'remove-root',
    'resource-usages:java-namespace-ownership',
    'search:rank-history',
    'subtree',
    'unused-deps:java-android-ownership',
    'unused-deps:java-inherited-imports',
    'unused-deps:java-module-binding',
    'unused-deps:java-nested-array-slots',
    'unused-deps:java-source-results',
)

# Original finite authored populations, including every positive and negative.
RETAINED = (
    ('unused-deps:java-nested-array-slots', 'disposable-java-nested-array-slots-v1', 115, 'b2ba9f302d1de814c2b8bc6c565523f435a3be18a43aa2256485b4bb7f106158', '23ec1b8949bc8a53f8a1dae7a345b63ef48e92e2fec80f80ec0868b308521789', 'java_nested_array_contracts'),
    ('global:scope:java-resources', 'disposable-java-resource-scope', 382, '290846ebc42579b01b99cb543985a637a51798dfefa4cd8aa5457cae8f22a3bc', 'da6f373dae87def8a0d146a24f01231f352dc94ccdb56f9edca8fea7988fa82b', 'java_resource_scope_contracts'),
    ('add-root', 'disposable-fixture', 6, 'ed3704dc590443853bd083846eaa529cc0ad56e2228a02107fb66fabf2e82192', '65e62e8e7e63290b58be557d95cc336bab0ff28598343e525fbbfaf890dbea64', 'root_contracts'),
    ('changed', 'disposable-java-git', 9, 'f0364c003ad6079ba6f94a432c15a28084850c1592ec088ea1ae549ac5286f2f', 'b48235c334440f3db428e1d26054fcfcd5fa1147c14b0448d83d72fb71764e47', 'vcs_contracts'),
    ('global:explicit-root', 'disposable-fixture', 9, '321546b08c54772d1e9a825d5bb085d2c3d39206a40004945883e601315c19da', 'c6f6bf3467e3489d67d3c053ad3dc5e027bd78287d1e202726b67e3b5aeeaf77', 'root_contracts'),
    ('global:format:java-agrep', 'disposable-java-project-formats', 41, '2b40cd1dc82e653a278e1bccf3955602a43e405e0e199c9c4691a3d100adaf28', '5b1195cd8978c6a7c893c570922d94771a541f8b315b6cb1050111fd7b5a4df2', 'project_format_contracts'),
    ('global:format:java-analysis', 'disposable-java-analysis-management-formats', 28, '5a948e4ba157e8cfc579859018f525403eb8b22e9f1d588dacb5df4b6657c646', '9d1c441c153338621270e7eeb21a31f585f4032b2d689fd2a0ee2eaaa39c3720', 'management_format_contracts'),
    ('global:format:java-exploration', 'disposable-java-exploration-formats', 74, '4714ff031382b12bd62538eecd344f24b54c10985558202019468b2255231d7e', '14c07dd13cb4a893ca6a52d2d7defa275685983f6fd8b6d09a74c7fa9223fbe9', 'exploration_format_contracts'),
    ('global:format:java-freshness-errors', 'disposable-java-freshness-errors-v1', 501, '98585eb22656bcfbfb63d6714c7910a0dc05c78b4ea697d79dd4e4c2327e2ea5', '7034a0b7b94fd4d10a3aa90a4d5611db2688408dd8efeb7025021fc4db55c7e5', 'freshness_error_contracts'),
    ('global:format:java-index-availability', 'disposable-java-selector-errors', 180, '392e365cd710add28d8611a31ef86893393b933b577fe0c97713f82bdecedb32', '42502053d5e7e432021a9186985cbe3737b6d3902e8b4863c1c530ecea2be2f2', 'selector_error_contracts'),
    ('global:format:java-installation', 'disposable-java-management-mutation-formats', 91, 'fb9fb454e7be34091bda92f1c8d5215298d0bbe8139437fb1640b5a357f21416', '559d0152e8f642985136d324f9eea64b8ec7d664fea01fbcf9c58801e9236871', 'mutation_format_contracts'),
    ('global:format:java-intent-fallback', 'disposable-java-exploration-formats', 167, 'f46e0d3f643406ba9c902a6c6d03a4464de57777019d83021f4815a81180acb4', '6fc934dc391832324fa3b16831bd870125cbcc8729865a8709a4f6fddb30e470', 'exploration_format_contracts'),
    ('global:format:java-management-read-only', 'disposable-java-analysis-management-formats', 153, 'ed66f914a9c296c59b6eb62241fd77f73e7139a9611ebaef79c99a532dc6356e', '8901c2c9e168bd2330cfc1fbd60c33239af8e6c0fa794a4f4666da588222b5cd', 'management_format_contracts'),
    ('global:format:java-ranked-search', 'disposable-java-search-formats', 3616, '1302ad2d6db414b9cd27bec7f2e12a9996fa21317f1f592e15667d1a900f6d4b', '4f44547b245e6fe815a059d09775ebfc743b36c1e16411411cadbd3fde9a65f2', 'search_format_contracts'),
    ('global:format:java-root-errors', 'disposable-java-root-errors', 321, '0964c08a0a4b78c5b207b4efa0a006f60c9b046c572166be89c9cdb7b8cdbdd0', '1aeff0332e3ad98b62b56100a58b8fe6c12ac927c553ebc848f7e0b88429b0dd', 'root_error_contracts'),
    ('global:format:java-root-mutations', 'disposable-java-management-mutation-formats', 103, 'ade268f4f4ad396e40424264943b958402074d6b7822a52d9c83d2c4e542abdb', '47bf20dc240e688842463fc4c28581e0435b0c44315747a2cb5a24ac13d57bb5', 'mutation_format_contracts'),
    ('global:format:java-scan-errors', 'disposable-java-scan-errors', 461, '30318a9c7a7cc2af21d7da084fdcaaffc9f4e81e6acf32fae6da9597d615715e', 'f8785fc644fd590607d53a9d7bb13b9e45d9fd53be2e5798a0ede149eca9d98e', 'scan_error_contracts'),
    ('global:format:java-search', 'disposable-java-search-formats', 3616, '1302ad2d6db414b9cd27bec7f2e12a9996fa21317f1f592e15667d1a900f6d4b', '4f44547b245e6fe815a059d09775ebfc743b36c1e16411411cadbd3fde9a65f2', 'search_format_contracts'),
    ('global:format:java-selector-errors', 'disposable-java-selector-errors', 1735, '1a918ead22538d318c60d6c2c3b105b42095a401775c0ec2e544954fb0daf209', '58516dab373aefd6b7dfd8e19cbee67c9369b35c03c5fac2e591627bd4510d8b', 'selector_error_contracts'),
    ('global:format:java-text-search-roots', 'disposable-java-text-search', 378, 'e3d7e1cc81342bdf528d46151fec5effb7334aa86f9dd5ce0affdf8e90648021', '4b1fc92807b78c5ec7bf6667c42486dc8b4b76e34089cf0f4373d1e22e8dc22f', 'format_contracts'),
    ('global:format:java-watch-scope', 'disposable-java-watch-scope-v1', 69, 'ac177c516778bd8594ea502a72d4e45fa3c1a2fad051f54a3c973da1773416ac', '5e1e5cf1834873aef69979c6b1384ac9128157edecc7edc2331b45243a069baf', 'watch_scope_contracts'),
    ('global:local', 'disposable-fixture', 1, '285e41f90d6b1a12352f0b3deb948486fd26cd2e7c8a440373b6953d9b9f6632', '66f4c15b449741aa28c7a44a7d42c428ca57e7b0e40db748561aaa7870336f3e', 'root_contracts'),
    ('global:scope:java-analysis', 'disposable-java-analysis-exploration-scope', 603, '9d4fae54ab3330b406225e6f945dfcf592f422037ae8e72d317c0c466029ed80', 'f4a9419782bb6645e758c55131e2f2eb5a07da8cee54bbe2e0753903e7ea5fd7', 'analysis_scope_contracts'),
    ('global:scope:java-call-tree', 'disposable-java-caller-scope', 656, 'e09dc695efb82aabbb52c7c0296ca0ad8f2ff9c40f29dae17042fa991a697f4c', '038fc3c573a1414602b12f7469f4e1cd044eaa565faa212f2932a2ad5bbce807', 'caller_scope_contracts'),
    ('global:scope:java-callers', 'disposable-java-caller-scope', 296, 'b7a435a14144da417c8029d83891256ae9a5b6bd3d10592023549ab6cd519d2a', 'c63e91bbb0ea7d518b71e762662a50ff3db9db50efa0503dcb6b9e14a358b2f5', 'caller_scope_contracts'),
    ('global:scope:java-conventions', 'disposable-java-insight-scope', 20, 'd3e980297c62e79d20d36f295d1bfb4b1a3ab5d34a3927e8fa63e673876573f6', '1fd5173d36c6f015459287dfc80ffb205e94192de9fbe81258f0eb6cc0e672e3', 'insight_scope_contracts'),
    ('global:scope:java-explore', 'disposable-java-analysis-exploration-scope', 122, 'a6482b4d51d5a24042bef8b36bca31aa288c356241b75c8581b09b7048e88056', '375f02fd5dbedd71dd424e70ea74da3860f94c18d67081e789f6f70631d7746e', 'analysis_scope_contracts'),
    ('global:scope:java-file-views', 'disposable-java-file-view-scope', 392, '504f1fd00a8d2248dcc502edfa7e55505ef18e68e372fa45526fd892890668c2', '2d245b1427f9584d6c58bece2ae6a8010c2f9767b45ce04d2f56edc1a846cf5b', 'file_scope_contracts'),
    ('global:scope:java-graph-directories', 'disposable-java-graph-directory-scope', 364, '758420687361eee9684806901120bec362855e092ca261e1a6c03e22f8c9427b', 'e22f4978d2a7f2a37a3c12496de6ea5235c2fa6ceac08216458d39feb0fdfdc5', 'graph_directory_contracts'),
    ('global:scope:java-graph-roots', 'disposable-java-graph-root-scope', 128, '71e11e095e47041e3d2b54aacfe082e15046d30f2072f2c1bacdb89fff9cb764', '31e8b7673f8b217139ca64d5a9c2565fc4970d93227ab3b66c533bf982485f93', 'graph_root_contracts'),
    ('global:scope:java-map', 'disposable-java-insight-scope', 623, '55899c3c97c26d20919bfc6abf601ea68254f9ae7816d51e0c3c1415be165f21', 'ea398982d86f63a11ca47cb7f5ccec64f1a5429c7265e583d92fd3077cb370c0', 'insight_scope_contracts'),
    ('global:scope:java-map-module-count', 'disposable-java-insight-scope', 151, 'b509f84eecb018fe15893d7880bc00cf86fd67096becedf189b994d84c386373', 'fc5e5c420d11497e0de31c40f5c7f8a477c59478315ac26e3e418997a664c28d', 'insight_scope_contracts'),
    ('global:scope:java-module-aliases', 'disposable-java-module-aliases', 308, '3fcba19d0c2ea54ff7942b392c27d3e5d33a83d3a51fea08c6691aae257a98f8', 'a4a4d7ebc2a69262050133b3ba7f259a4410cc736ad502eed72e4716cea5b1cd', 'module_alias_contracts'),
    ('global:scope:java-module-directories', 'disposable-java-module-directory-scope', 1350, '15dbe91d91a6f6099074077a3f8635d05e79c9c5acc53d1664ad1534bafdd8a7', '1e5b74f884b558358c0cd10fe445cd5ae3021c446a34f337a19ada676cb7edbc', 'module_scope_contracts'),
    ('global:scope:java-module-roots', 'disposable-java-module-root-scope', 437, 'f934aa0115421bb51413da418bae66201d2f396e4ca7b026951b55a31dd0560d', '130bc0b538b6ec521e8a7196ccefeb4339c0616adcd7a9058be0bb67965fb908', 'module_root_contracts'),
    ('global:scope:java-navigation', 'disposable-java-navigation-scope', 1762, 'b3fbc42c136727488c01a0de70999b80529bc96387c664e793e8862e7f7c9ffe', '732353bc5b198a2595aeeb5e6aef57fe19ee7d3fdd69afff76e6550f9330c8c1', 'navigation_scope_contracts'),
    ('global:subtree', 'disposable-fixture', 3, '1a2805ec863ef7f0c912a5d8d94d9d9cfd67d81b616e94557a683669b048e641', 'e9a577d7f51ff16701b52db044cb79d1eefad3a3226fb1fadff25a29921c55a0', 'root_contracts'),
    ('global:walk-up', 'disposable-fixture', 7, 'b661043982763744be52e800ceb731a9588b3b0028d348bd148363717d7e6a7a', '5306e1d8c4278b489ff286a434208ba30686c41abf8819619109ea19f3d2445f', 'root_contracts'),
    ('hotspots', 'disposable-java-git', 122, 'b3c290abe7e9ca1749df343284ce704f7b1f0b187ecd91a6539137211dd28318', 'f175a791213483690079f52aba7b23a19364673b5770699ba47af4a0f4795e78', 'vcs_contracts'),
    ('remove-root', 'disposable-fixture', 2, '570963b70382dba8d83966028c825847ff5a27442c2a6d4d2a7c8634c948db64', 'db2a9f50a6bb77873707cbb5bff3f78053a408ad48b5affa2ece80fd3800b0d6', 'root_contracts'),
    ('resource-usages:java-namespace-ownership', 'disposable-java-android-dependency-ownership', 6, '6bb467ad5c8caa481bca3a55f985d387c67835cf3b478678f1154a248c8b3e82', '68ef7bc9f00e2b33ccde35b583f0c93586aa349fcfaa38bf820ca9de50d868ca', 'android_dependency_contracts'),
    ('search:rank-history', 'disposable-java-git', 65, 'd99d67ba880355b38a5242c75e3d20760075e704dd277571db5e46f6e47d0993', '1b7b4cca25bb1caf18655baf3fe12627b0be96556b50074af605386dc434cb71', 'vcs_contracts'),
    ('subtree', 'disposable-fixture', 9, '523f78a2e4bbe7e3337832042200a38ee74b42ba36cd125854ed2995836ab0f8', '04cdf4408543c457dffbf99e386bc11e5999c3af8fe0615e099c26a14bc29781', 'root_contracts'),
    ('unused-deps:java-android-ownership', 'disposable-java-android-dependency-ownership', 12, '8b3c7b819e98ed2f073d975f0d1f70cbcd7b652d0ae5e83e508e3b58fe468c2e', '5488cf2683acdeda0d432040b2de75e3a0608ab6a8cbaa87d8ddc10db94cca90', 'android_dependency_contracts'),
    ('unused-deps:java-inherited-imports', 'disposable-java-inherited-import-ownership', 3440, '307cc200bf538c16d8c19713a7884705119f38ccc9644072e5fc5c6c8d49bd16', '75fdb58f3f730581c4d2582f2a4ea973fd719525b42fd8d9255fd0c673c6d70e', 'java_inherited_import_contracts'),
    ('unused-deps:java-module-binding', 'disposable-java-module-declaration-binding', 151, '2f652ff6f93dc209f858d5994eb79063c391c04cd2fb6ff0e90196b3d941e9c0', '013a9a726cb3e0f837d870fbf532e70af59aa021cf0aac70e6d9fd2e5be402e4', 'java_module_binding_contracts'),
    # Retain the added variable-arity phases, guards, option pages and attached
    # refresh assertions alongside the original source-result population.
    ('unused-deps:java-source-results', 'disposable-java-source-result-ownership', 798, '919f9930b2c12afb5d0781348244805abe9bcddda21c64d430032dbd17ee21f9', '6d1342ca7667bb47e8b66bbfdb5919225d036b4336869394473d357b6227c499', 'java_dependency_result_contracts'),
)

REVIEWED_SURFACE_SHA256 = 'f5a010676e71d715489eb595292d83e21e93ba4d9e8f2015279f9c55fb34a3df'
