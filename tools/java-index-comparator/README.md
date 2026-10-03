# Automated Java differential repair

Index MCP Server is the reference for a read-only target project. The live
fixture executes ast-index itself; recognizing a saved JSON shape is not a
successful test.

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
Search reference aggregation, ranking, command options, and other command contracts
remain explicitly pending. A passing subset never establishes a complete audit.

The five Perl commands share per-file MCP text anchors, literal query filters,
exact import pragma exclusions, and ordered limit checks. They compare lexical
line locations, not semantic Perl navigation. A full file-type inventory proves
language absence independently, and the CLI must still return empty results.
Relevant links, mixed-case suffixes, and ignored source scopes remain unresolved.
Synthetic paginated-oracle tests validate the adapter and production CLI; they
do not establish live-project MCP equivalence.

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

Audits resume from durable checkpoints. Artifacts and agent transcripts stay
outside the target project, under a persistent directory excluded from Git.
Only aggregates are printed; target sources and MCP payloads are not printed
or committed. Source or binary changes invalidate evidence.

MCP configuration is discovered through Codex MCP. An explicit MCP URL is
available when the server is running but is absent from that configuration.
The target must be open in Index MCP Server.

Legacy collect.py, compare.py and case_fixture.py remain available locally.
They are not the live repair process; the legacy fixture validates stored
JSON contracts rather than executing current production behaviour.
