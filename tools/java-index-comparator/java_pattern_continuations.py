"""Finite authored Java continuation scopes, shared by production fixtures.

These are source/CLI criteria, not MCP truth or general compiler equivalence.
Each entry declares whether the continuation sees the matched value or the
unshadowed field/import. Java compilation independently verifies that choice.
"""


def cases(ty, name, matched, unmatched):
    condition = f'value instanceof {ty} {name}'
    # Blocks/jumps must be interpreted in their own lexical target scope.
    templates = (
        ('nested-if', True, 'if (!(COND)) { if (flag) return 0; else throw new IllegalArgumentException(); } return EXPR;'),
        ('positive-else', True, 'if (COND) {} else { if (flag) return 0; else return 1; } return EXPR;'),
        ('continue', True, 'while (flag) { if (!(COND)) continue; return EXPR; } return 0;'),
        ('break', True, 'while (flag) { if (!(COND)) break; return EXPR; } return 0;'),
        ('labelled-break', True, 'outer: while (flag) { if (!(COND)) break outer; return EXPR; } return 0;'),
        ('labelled-continue', True, 'outer: while (flag) { if (!(COND)) continue outer; return EXPR; } return 0;'),
        ('synchronized', True, 'if (!(COND)) { synchronized (this) { throw new IllegalArgumentException(); } } return EXPR;'),
        ('try-finally', True, 'if (!(COND)) { try { if (flag) return 0; else throw new IllegalArgumentException(); } finally { flag = false; } } return EXPR;'),
        ('while-exit', True, 'while (!(COND)) { value = null; } return EXPR;'),
        ('for-exit', True, 'for (; !(COND); value = null) {} return EXPR;'),
        ('do-exit', True, 'do { value = null; } while (!(COND)); return EXPR;'),
        ('nested-loop-break', True, 'while (!(COND)) { while (flag) { break; } value = null; } return EXPR;'),
        ('consumed-label', False, 'if (!(COND)) { stop: { break stop; } } return EXPR;'),
        ('partial-return', False, 'if (!(COND)) { if (flag) return 0; } return EXPR;'),
        ('while-break-exit', False, 'while (!(COND)) { if (flag) break; value = null; } return EXPR;'),
        ('for-break-exit', False, 'for (; !(COND); value = null) { if (flag) break; } return EXPR;'),
        ('do-break-exit', False, 'do { if (flag) break; value = null; } while (!(COND)); return EXPR;'),
        ('labelled-loop-exit', False, 'outer: while (!(COND)) { if (flag) break outer; value = null; } return EXPR;'),
        ('local-return', False, 'if (!(COND)) { class Worker { int read() { return 0; } } } return EXPR;'),
        ('finally-resumes', False, 'if (!(COND)) { stop: { try { return 0; } finally { break stop; } } } return EXPR;'),
    )
    return [(label, 'int run(Object value, boolean flag) { ' +
             body.replace('COND', condition).replace('EXPR', matched if shadows else unmatched) + ' }', shadows)
            for label, shadows, body in templates]
