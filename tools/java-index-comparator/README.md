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
implementations, and hierarchy. Imports are checked against source statements;
stats, query, schema, and db-path are checked against the isolated database.
Universal search currently checks its exact declaration results. Its other
sections, ranking, command options, and independent constructor outline
coverage remain explicitly pending, along with the other command contracts.
A passing subset is never reported as a complete audit. MCP Go-to-Symbol kinds
are coarse and constructors are outside its navigation scope. Anonymous-class
methods have no Java qualified name, so their identity uses name, file and line.

Replay streams the first 100 failures in capture order plus every unsupported
or error contract. Each check executes the current CLI against a rebuilt index;
all recorded oracle operations and pagination requests are bound to their
original scope. Reference checks are planned only for confirmed project
declarations, rather than lexical keywords and unresolved external names.

The cycle driver automates build, audit, agent work, original-batch replay,
workspace tests, commit and push. It requires a feature branch and a clean
worktree, except for local AGENTS.md instructions. Its coding-agent command is
replaceable. After a complete audit it reruns workspace tests and creates or
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
