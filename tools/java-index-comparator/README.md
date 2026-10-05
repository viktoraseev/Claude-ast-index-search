# Automated Java differential repair

Index MCP Server is the reference for a read-only target project. The live
fixture executes ast-index itself; recognizing a saved JSON shape is not a
successful test.

The repair scope is Java, including shared commands exercised on Java sources.
Non-Java-only commands are explicitly out of scope: they neither count as
passing checks nor block completion of the Java audit. Mixed-language output
is normalized to the Java source scope without repairing other parsers.

The intended repair process collects up to 100 confirmed problems, adds compact
regression tests and fixes, validates the original problem batch, commits and
pushes to a feature branch, then rebuilds and starts a full audit again.
Full coverage and zero unresolved problems are required before the work is
ready for an upstream PR.

Live handlers compare class identities, Java qualified names, file navigation,
symbol and outline declaration identities, references, usages, callers,
implementations, and hierarchy. Import statements are anchored by MCP code search;
stats, query, schema, and db-path are checked against the isolated database.
Universal search checks declaration results, file matches and Java content locations;
annotation grep checks all text contexts against MCP. Constructor, record component,
implicit accessor and tracked annotation checks use an independent JDK syntax parser
and execute both outline and indexed symbol lookup. This requires JDK 17+ (or a
newer JDK supporting the target's Java syntax). The parser only reads sources;
its generated files stay in the artifact directory.

MCP Go-to-Symbol omits constructors and implicit record accessors. Navigation
normalization accounts for that narrower scope; separate structure checks detect
missing constructors or accessors. Anonymous-class methods have no Java qualified
name, so their navigation identity uses name, file and line. Hierarchy compares
explicit source parent edges, including external parents, and MCP project children.
Search reference aggregation, base ranking and navigation options have executable
checks. Advanced ranking and other unresolved command contracts remain explicitly
pending. A passing subset never establishes a complete audit.

The inherited Java member-type fixture executes graph dependency pages, reverse
traversal and RWR exploration against compact authored sources. Javac validates
positive imports/qualified names/lexical inheritance and rejects access and
nonstatic-import guards. Hiding and diamond identities are checked directly.
It also checks public member exports through accessible subclasses of hidden
owners, retaining declaring identities and rejecting direct hidden-owner access.
This is independent source/state coverage, not MCP equivalence; member aliases
in parent declarations, local-class shadows and attached roots remain pending
in the broader semantic contracts.

Commands without an equivalent Index MCP operation use separately labelled
independent source/state or internal CLI checks. Synthetic Java fixtures complement
live project checks; they do not replace unresolved target or syntax contracts.

The Java file-view format fixture executes `file`, `outline`, `imports` and
`api` against small authored Java sources. It checks source identities, import
syntax, snippets, limits, empty/missing inputs and text/JSON rendering without
using native DB rows as expected declarations. This is independent source/state
coverage, not MCP equivalence. Other command formats, diagram-format selection
and attached-root API scope remain pending.

The Java navigation-scope fixture executes class, symbol, implementations,
refs, usages, hierarchy and search on disposable sources in colliding roots.
It checks literal file/module/cwd selector intersections, case, wildcard
characters in paths, indexed and lexical usages, root ownership, pagination
and rendered identities. This is independent source/state coverage, not MCP
equivalence. Caller/call-tree selector composition and module/map/analysis/
graph/conventions/explore scope remain pending in the overall scope matrix.

Replay streams the first 100 failures in capture order plus every unsupported
or error contract. Each check executes the current CLI against a rebuilt index;
all recorded oracle operations and pagination requests are bound to their
original scope. Reference checks are planned only for confirmed project
declarations, rather than lexical keywords and unresolved external names.

The cycle driver automates build, audit, agent work, original-batch replay,
workspace tests, commit and push. It requires a feature branch and a clean
worktree, except for local AGENTS.md instructions. Its coding-agent command is
replaceable. Failed repair tests or builds return to the agent with the original
evidence and failure logs, without committing. After a complete audit it reruns workspace tests and creates or
verifies an upstream PR at the tested commit. Missing contracts prevent that
final step.

For several folders, use `cycle_projects.py` with one `--project-root` per folder
in the desired repair order. All folders must pass at the same commit before one
PR is opened. A fix for a later folder makes earlier results obsolete and requires
fresh verification. An already-running per-folder cycle is allowed to finish
without launching a duplicate. `--target-output` reuses existing per-folder
artifacts, and `--defer-pr` verifies the whole set without opening a PR.
The entry point's `--help` lists invocation options. Coordinator artifacts belong
inside this repository's `.artifacts/`, outside every read-only target folder.

A saved green summary is insufficient for a PR. The final gate rechecks the
actual evidence, coverage, source inventory, binary and fixture fingerprints,
plus the audited commit and clean worktree. An obsolete audit requires a fresh
scan; it cannot authorize publication.

Audits resume from durable checkpoints. Artifacts and agent transcripts stay
outside the target project, under a persistent directory excluded from Git.
Only aggregates are printed; target sources and MCP payloads are not printed
or committed. Source or binary changes invalidate evidence.

MCP configuration is discovered through Codex MCP. An explicit MCP URL is
available when the server is running but is absent from that configuration.
Every target must be open in Index MCP Server.

Legacy collect.py, compare.py and case_fixture.py remain available locally.
They are not the live repair process; the legacy fixture validates stored
JSON contracts rather than executing current production behaviour.
