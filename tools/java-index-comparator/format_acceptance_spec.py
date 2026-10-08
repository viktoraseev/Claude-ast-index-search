"""Finite Java format acceptance specification, independent of child verdicts.

Each criterion runs the entire named authored fixture, including its negatives.
The retained sample-key fingerprints protect every original assertion without
publishing thousands of identical keys. They are retention guards, not oracle
truth: acceptance also requires fresh executed production results and exact
source-authored expected/actual equality. Expanding a fixture requires reviewing
this specification; a changed population remains pending in the meantime.
"""
from pathlib import Path
import re

# The CLI contract is src/main.rs Commands/Cli/validate_command_scope; format
# identities/pages and error envelopes come from the named production commands.
# Fixtures contain concrete inputs, expected outputs and bounded stopping rules.
CONTRACTS = {
    'diagram-selection': ('module_format_contracts.exercise', 'src/commands/modules.rs',
        'module/deps/dependents --format json/text/mermaid/dot; module-route diagrams and invalid formats'),
    'agrep': ('project_format_contracts.exercise', 'src/commands/grep.rs',
        'agrep --format json/text combined with --json; provider output, empty, fallback and exit status'),
    'analysis': ('management_format_contracts.exercise', 'src/commands/analysis.rs',
        'unused-symbols export selection; --limit 0/1/full, empty/missing index, text/JSON identities'),
    'call-tree': ('caller_format_contracts.exercise', 'src/commands/grep.rs',
        'call-tree depth/limit pages, file-qualified same-name owners, branches, fresh/stale/absent graph'),
    'callers': ('caller_format_contracts.exercise', 'src/commands/grep.rs',
        'callers snippets, limits, empty/missing queries and JSON/text across roots'),
    'exploration': ('exploration_format_contracts.exercise', 'src/commands/explore.rs',
        'explore literal/RWR neighbours, file/kind/test filters, roots, limits and empty/missing data'),
    'file-views': ('file_view_contracts.exercise', 'src/commands/files.rs',
        'file/outline/imports/api source identities, signatures/import syntax, limits, errors and text/JSON'),
    'graph-ambiguity': ('graph_ambiguity_contracts.exercise', 'src/commands/graph/mod.rs',
        'bounded ambiguous seeds/edges, include-ambiguous, candidate metadata, traversal and JSON/text pages'),
    'intent-fallback': ('exploration_format_contracts.exercise', 'src/commands/mod.rs',
        'search intent fallback, ranked/unranked sections, limits, filters and JSON/text'),
    'installation': ('mutation_format_contracts.exercise', 'src/commands/management.rs',
        'installation mutations on disposable destinations only; dry-run, conflicts, JSON/text and flag positions'),
    'lifecycle': ('lifecycle_format_contracts.exercise', 'src/commands/index.rs',
        'rebuild/update/restore/clear/watch-status on disposable Java indexes, profiles and JSON/text results'),
    'management-read-only': ('management_format_contracts.exercise', 'src/commands/management.rs',
        'stats/query/schema/version/db-path/list-roots/subtree lists; empty/missing state and text/JSON'),
    'module-alias-errors': ('module_alias_contracts.exercise', 'src/commands/modules.rs',
        'module/deps/dependents/unused-deps alias ambiguity, attached roots and JSON/text error identities'),
    'modules': ('module_format_contracts.exercise', 'src/commands/modules.rs',
        'module/deps/dependents/unused-deps/module-route/detect-stacks identities, kinds, pages and JSON/text'),
    'navigation': ('navigation_format_contracts.exercise', 'src/commands/analysis.rs',
        'class/symbol/hierarchy/implementations/refs/usages kinds, snippets, filters, limits and attached roots'),
    'project-insights': ('project_format_contracts.exercise', 'src/commands/project_info.rs',
        'map/conventions identities/counts/order/pages; empty/missing index and unreadable sources'),
    'ranked-search': ('search_format_contracts.exercise', 'src/commands/mod.rs',
        'search --rank presets, all sections, literal OR, Unicode, references, zero/limited/full JSON/text pages'),
    'root-mutations': ('mutation_format_contracts.exercise', 'src/commands/management.rs',
        'add-root/remove-root/subtree add/remove on disposable roots, failure/success and JSON/text flags'),
    'search': ('search_format_contracts.exercise', 'src/commands/mod.rs',
        'literal search four sections, Unicode escaping, references, empty/missing input and JSON/text pages'),
    'text-search': ('format_contracts.exercise', 'src/commands/grep.rs',
        'todo/deprecated/suppress/annotations/inject/provides/deeplinks authored Java locations, snippets, filters and limits'),
    'text-search-roots': ('format_contracts.exercise', 'src/commands/grep.rs',
        'same Java lexical readers across colliding attached roots, selection and JSON/text pages'),
    'graph:metrics-rendering': ('graph_contracts.exercise', 'src/commands/graph/mod.rs',
        'graph metrics/top/status/build; source-authored rational PageRank, confidence, pages and text rendering'),
    'graph:traversal-rendering': ('graph_contracts.exercise', 'src/commands/graph/mod.rs',
        'graph dependencies/dependents/impact/path/cycles; explicit diamond/cycle hops, caps and text notices'),
    'module-route:rendering': ('route_contracts.exercise', 'src/commands/modules.rs',
        'module-route shortest/all paths, edge kinds, depth/path caps and JSON/text/mermaid/dot rendering'),
}
# Error criteria include every public shared selector/format guard and each
# operation-specific I/O path. Fault injection exercises CLI responses/recovery;
# hardware crash equivalence is not advertised by these commands.
ERROR_CONTRACTS = {
    'delegate-errors': ('operation_error_contracts', 'delegate output/exit/fallback errors'),
    'freshness-errors': ('freshness_error_contracts', 'foreground/background update and every graph refresh consumer'),
    'index-availability': ('selector_error_contracts', 'missing/unrecognizable indexes, structured graph queries, source fallbacks'),
    'publication-errors': ('publication_error_contracts', 'staged rebuild/restore/clear reader/writer contention, locks and marker errors'),
    'publication-recovery': ('publication_recovery_contracts', 'late marker write/sync/install, rollback, retained witnesses, repeated retries and housekeeping'),
    'query-errors': ('operation_error_contracts', 'read-only query/schema, malformed/damaged database and rejected mutations'),
    'restore-errors': ('operation_error_contracts', 'missing/corrupt backups and failed installation preserving the prior generation'),
    'root-errors': ('root_error_contracts', 'root registration, migration/read/write/locks, unavailable mounts and aliases'),
    'scan-errors': ('scan_error_contracts', 'selected Java lexical UTF-8/read/walk errors and empty/scope/page controls'),
    'selector-errors': ('selector_error_contracts', 'all Java CLI commands, format/root/conflict precedence, scope guards and version exception'),
    'source-errors': ('operation_error_contracts', 'file/outline/imports/api unreadable sources, limit and format composition'),
    'vcs-errors': ('watch_vcs_error_contracts', 'changed/hotspots Git process/history protocol failures, transaction preservation'),
    'watch-errors': ('watch_vcs_error_contracts', 'startup lock/index, contention/SQL and cleared-index recovery'),
    'watch-events': ('watch_event_contracts', 'notification backend/channel, create/rename/delete/coalescing and module descriptor refresh'),
    'watch-scope': ('watch_scope_contracts', 'attached-root notifications, live root/config changes, unavailable/recreated roots and filters'),
}
CONTRACTS.update({name: (module + '.exercise', 'src/main.rs and src/commands/',
                       detail + '; JSON/text, prefix/suffix format flags and state-preservation controls')
                  for name, (module, detail) in ERROR_CONTRACTS.items()})

# Fixed identities and full authored assertion populations retained from the
# original round input; no target names, paths or payloads are included.
# (feature, subject, assertion count, sorted assertion-key digest)
RETAINED = (
    ('global:format:diagram-selection', 'disposable-java-module-formats', 16, '50cb4ad1a3f7c936a98ec049862598b8bd222f929ec02800b8140f2f622b2b6d'),
    ('global:format:java-agrep', 'disposable-java-project-formats', 41, '2b40cd1dc82e653a278e1bccf3955602a43e405e0e199c9c4691a3d100adaf28'),
    ('global:format:java-analysis', 'disposable-java-analysis-management-formats', 28, '5a948e4ba157e8cfc579859018f525403eb8b22e9f1d588dacb5df4b6657c646'),
    ('global:format:java-call-tree', 'disposable-java-caller-formats', 118, 'f8c7633a9554cb5ecdf6834dfb8d3d5a56a9113ca74ff2e0f88df6801e71d246'),
    ('global:format:java-callers', 'disposable-java-caller-formats', 50, '33e39a1a7d308250df238734826864faa2013f4da9a97b59a5ad8d95b04b3569'),
    ('global:format:java-delegate-errors', 'disposable-java-operation-errors', 23, '1149cbeaa7c42ca2833715b6fdec0bb5c6f703b4e7d3bdd4ac4297ec5a4e48da'),
    ('global:format:java-exploration', 'disposable-java-exploration-formats', 74, '4714ff031382b12bd62538eecd344f24b54c10985558202019468b2255231d7e'),
    ('global:format:java-file-views', 'disposable-java-file-views', 121, '4e9abb31c64393af24ebe8ebef2c32b3f256a118ada1dd2e24914191ca6b1bb8'),
    ('global:format:java-freshness-errors', 'disposable-java-freshness-errors-v1', 501, '98585eb22656bcfbfb63d6714c7910a0dc05c78b4ea697d79dd4e4c2327e2ea5'),
    ('global:format:java-graph-ambiguity', 'disposable-java-graph-ambiguity', 87, '0745510df2c6ca9b5b9962d02f612d9f7ee3b9124c77a86a039fd1037eb86021'),
    ('global:format:java-index-availability', 'disposable-java-selector-errors', 180, '392e365cd710add28d8611a31ef86893393b933b577fe0c97713f82bdecedb32'),
    ('global:format:java-installation', 'disposable-java-management-mutation-formats', 91, 'fb9fb454e7be34091bda92f1c8d5215298d0bbe8139437fb1640b5a357f21416'),
    ('global:format:java-intent-fallback', 'disposable-java-exploration-formats', 167, 'f46e0d3f643406ba9c902a6c6d03a4464de57777019d83021f4815a81180acb4'),
    ('global:format:java-lifecycle', 'disposable-java-lifecycle-formats', 114, '003c4798ffa66671f09f6a44a006d7b8e642f852c95c8a929c4fe8e5935f9054'),
    ('global:format:java-management-read-only', 'disposable-java-analysis-management-formats', 153, 'ed66f914a9c296c59b6eb62241fd77f73e7139a9611ebaef79c99a532dc6356e'),
    ('global:format:java-module-alias-errors', 'disposable-java-module-aliases', 48, 'e4696a38132d2278456ff5cfa4ba9628828404471df973892cb46b038c9ca840'),
    ('global:format:java-modules', 'disposable-java-module-formats', 85, 'c92c8986eb576e7a624af9c0a80c919bfb302e453265e86aa11559a3fcfa8080'),
    ('global:format:java-navigation', 'disposable-java-navigation-formats', 1009, '5b239e954e762fce5c2dd3f25237f206557d2460809f3c3d981509a19291b290'),
    ('global:format:java-project-insights', 'disposable-java-project-formats', 59, '1b6b38e099e5e0b283765037ddb819744f9b18650deaefc715820a56e504cf9d'),
    ('global:format:java-publication-errors', 'disposable-java-publication-errors', 389, '39f18ce5680c1c14d55444e1641576e9859782fadfde1a9bb2ecc51286add0ef'),
    ('global:format:java-publication-recovery', 'disposable-java-publication-recovery', 4293, '3841d8fdaadb805a079de822daa4a036b248bff7546b3f96878f879f3ddf8a8e'),
    ('global:format:java-query-errors', 'disposable-java-operation-errors', 62, '3cee3b0412867805a3788e5c14ac02bb5fbd33bb6b5ae67fc876bf27513e0502'),
    ('global:format:java-ranked-search', 'disposable-java-search-formats', 3616, '1302ad2d6db414b9cd27bec7f2e12a9996fa21317f1f592e15667d1a900f6d4b'),
    ('global:format:java-restore-errors', 'disposable-java-operation-errors', 46, '49ee6902a4e200578611c169f7929b7445ee99cb440091d6238872989515b5cb'),
    ('global:format:java-root-errors', 'disposable-java-root-errors', 321, '0964c08a0a4b78c5b207b4efa0a006f60c9b046c572166be89c9cdb7b8cdbdd0'),
    ('global:format:java-root-mutations', 'disposable-java-management-mutation-formats', 103, 'ade268f4f4ad396e40424264943b958402074d6b7822a52d9c83d2c4e542abdb'),
    ('global:format:java-scan-errors', 'disposable-java-scan-errors', 461, '30318a9c7a7cc2af21d7da084fdcaaffc9f4e81e6acf32fae6da9597d615715e'),
    ('global:format:java-search', 'disposable-java-search-formats', 3616, '1302ad2d6db414b9cd27bec7f2e12a9996fa21317f1f592e15667d1a900f6d4b'),
    ('global:format:java-selector-errors', 'disposable-java-selector-errors', 1735, '1a918ead22538d318c60d6c2c3b105b42095a401775c0ec2e544954fb0daf209'),
    ('global:format:java-source-errors', 'disposable-java-operation-errors', 39, 'ee9f2c222629b1c01149ecb32b308702f5faa16d14205675b150d1f6f620b1f5'),
    ('global:format:java-text-search', 'disposable-java-text-search', 126, '8c56d576bef908c8ac0cd81babe83da3e9a3bc33ab03debd09e8404fd9a4104b'),
    ('global:format:java-text-search-roots', 'disposable-java-text-search', 378, 'e3d7e1cc81342bdf528d46151fec5effb7334aa86f9dd5ce0affdf8e90648021'),
    ('global:format:java-vcs-errors', 'disposable-java-watch-vcs-errors-v1', 312, '6d5ac80165eb3233c10f47202d89453d70082ccd26cd19246dbac79671e2afa1'),
    ('global:format:java-watch-errors', 'disposable-java-watch-vcs-errors-v1', 64, 'a5b0276d4901d45dec0cbde20c24569c056aa2168f98ea6765eb97417d93e050'),
    ('global:format:java-watch-events', 'disposable-java-watch-events-v1', 137, 'bf3c44086a74ea267c9e91616d616372f43768d16fb1bf21f923e61218a93d2c'),
    ('global:format:java-watch-scope', 'disposable-java-watch-scope-v1', 69, 'ac177c516778bd8594ea502a72d4e45fa3c1a2fad051f54a3c973da1773416ac'),
    ('graph:metrics-rendering', 'disposable-java-graph', 18, 'cd7d72cc34220478fca6cb97866165db4a37fde4e9e9152f0e46f08fd344de5a'),
    ('graph:traversal-rendering', 'disposable-java-graph', 39, 'ecfa2379f2a143897b489f2abd22933bc0e96720c327b7b46d7e78e6dfb8c3c2'),
    ('module-route:rendering', 'disposable-java-graph', 42, '19d58784b0bcd4544b8906665cc0d2e13f00297013b0de18f59baa7f7c49a67b'),
)


def unmapped_commands():
    """Prevent a newly advertised Java CLI command from vanishing from formats."""
    from audit import JAVA_EXCLUDED_FEATURES
    from selector_error_contracts import READS, MUTATIONS
    body = (Path(__file__).resolve().parents[2] / 'src/main.rs').read_text().split('enum Commands {', 1)[1].split('\n}', 1)[0]
    variants = re.findall(r'^    ([A-Z][A-Za-z0-9]*)\s*(?:\{|,)', body, re.MULTILINE)
    commands = {re.sub(r'([a-z0-9])([A-Z])', r'\1-\2',
                       re.sub(r'(.)([A-Z][a-z]+)', r'\1-\2', variant)).lower()
                for variant in variants}
    covered = {args[0] for args in READS + MUTATIONS}
    return sorted(commands - JAVA_EXCLUDED_FEATURES - covered)


def criteria():
    result = []
    for feature, subject, count, digest in RETAINED:
        name = feature.removeprefix('global:format:java-')
        if feature == 'global:format:diagram-selection':
            name = 'diagram-selection'
        fixture, production, contract = CONTRACTS[name]
        result.append(dict(feature=feature, subject=subject, samples_count=count,
                           sample_keys_sha256=digest, fixture=fixture,
                           production=production, contract=contract))
    return result
