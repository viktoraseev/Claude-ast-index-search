import com.sun.source.tree.*;
import com.sun.source.util.*;
import javax.tools.*;
import java.io.*;
import java.nio.file.*;
import java.util.*;
import java.util.regex.*;

/** Parse Java syntax with the JDK, independently of ast-index and its database. */
public class JavaStructure {
    static String quote(String text) {
        StringBuilder out = new StringBuilder("\"");
        for (char c : text.toCharArray()) {
            switch (c) {
                case '"' -> out.append("\\\"");
                case '\\' -> out.append("\\\\");
                case '\n' -> out.append("\\n");
                case '\r' -> out.append("\\r");
                case '\t' -> out.append("\\t");
                default -> { if (c < 32) out.append(String.format("\\u%04x", (int)c)); else out.append(c); }
            }
        }
        return out.append('"').toString();
    }

    static String parse(Path file) throws Exception {
        JavaCompiler compiler = ToolProvider.getSystemJavaCompiler();
        if (compiler == null) throw new IllegalStateException("A JDK is required");
        String source = Files.readString(file);
        DiagnosticCollector<JavaFileObject> diagnostics = new DiagnosticCollector<>();
        try (StandardJavaFileManager manager = compiler.getStandardFileManager(diagnostics, null, null)) {
            JavacTask task = (JavacTask) compiler.getTask(null, manager, diagnostics,
                List.of("-proc:none"), null, manager.getJavaFileObjects(file));
            CompilationUnitTree unit = task.parse().iterator().next();
            if (diagnostics.getDiagnostics().stream().anyMatch(d -> d.getKind() == Diagnostic.Kind.ERROR))
                return "{\"error\":\"JDK cannot parse this source version\"}";
            SourcePositions positions = Trees.instance(task).getSourcePositions();
            List<String> entries = new ArrayList<>();
            new TreePathScanner<Void, Void>() {
                int start(Tree tree) { return (int) positions.getStartPosition(unit, tree); }
                int end(Tree tree) { return (int) positions.getEndPosition(unit, tree); }
                int namePosition(Tree tree, String name, String suffix) {
                    int begin = start(tree), finish = end(tree);
                    if (begin < 0 || finish < begin) throw new IllegalStateException("Missing source range");
                    if (tree instanceof MethodTree method) {
                        if (method.getBody() != null) finish = start(method.getBody()) + 1;
                        if (!method.getParameters().isEmpty() && start(method.getParameters().get(0)) > begin)
                            finish = Math.min(finish, start(method.getParameters().get(0)));
                    }
                    Matcher matcher = Pattern.compile("(?<![\\w$])" + Pattern.quote(name) + "(?![\\w$])" + suffix)
                        .matcher(source.substring(begin, finish));
                    int found = -1;
                    while (matcher.find()) found = begin + matcher.start();
                    if (found < 0) throw new IllegalStateException("Missing declaration anchor");
                    return found;
                }
                void emit(String kind, String name, int position) {
                    entries.add("{\"kind\":" + quote(kind) + ",\"name\":" + quote(name)
                        + ",\"line\":" + unit.getLineMap().getLineNumber(position) + "}");
                }
                @Override public Void visitMethod(MethodTree tree, Void unused) {
                    if (tree.getName().contentEquals("<init>")) {
                        ClassTree owner = (ClassTree) getCurrentPath().getParentPath().getLeaf();
                        String name = owner.getSimpleName().toString();
                        emit("constructor", name, namePosition(tree, name, "\\s*[({]"));
                    } else {
                        String name = tree.getName().toString();
                        emit("method", name, namePosition(tree, name, "\\s*\\("));
                    }
                    return super.visitMethod(tree, unused);
                }
                @Override public Void visitClass(ClassTree tree, Void unused) {
                    List<Tree> parents = new ArrayList<>(tree.getImplementsClause());
                    if (tree.getExtendsClause() != null) parents.add(tree.getExtendsClause());
                    for (Tree parent : parents) {
                        String text = parent.toString();
                        StringBuilder erased = new StringBuilder();
                        int depth = 0;
                        for (char c : text.toCharArray()) {
                            if (c == '<') depth++; else if (c == '>') depth--;
                            else if (depth == 0 && !Character.isWhitespace(c)) erased.append(c);
                        }
                        entries.add("{\"kind\":\"parent\",\"owner\":" + quote(tree.getSimpleName().toString())
                            + ",\"name\":" + quote(erased.toString()) + "}");
                    }
                    if (tree.getKind() == Tree.Kind.RECORD) {
                        // Records cannot declare instance fields outside their header.
                        for (Tree member : tree.getMembers()) {
                            if (!(member instanceof VariableTree field)
                                || field.getModifiers().getFlags().contains(javax.lang.model.element.Modifier.STATIC)) continue;
                            String component = field.getName().toString();
                            int position = namePosition(field, component, "");
                            emit("component", component, position);
                            boolean explicit = tree.getMembers().stream().anyMatch(m -> m instanceof MethodTree method
                                && method.getName().contentEquals(component) && method.getParameters().isEmpty());
                            if (!explicit) emit("accessor", component, position);
                        }
                    }
                    return super.visitClass(tree, unused);
                }
                @Override public Void visitAnnotation(AnnotationTree tree, Void unused) {
                    String name = tree.getAnnotationType().toString();
                    emit("annotation", "@" + name.substring(name.lastIndexOf('.') + 1), start(tree));
                    return super.visitAnnotation(tree, unused);
                }
            }.scan(unit, null);
            return "{\"entries\":[" + String.join(",", entries) + "]}";
        }
    }

    public static void main(String[] args) throws Exception {
        try (BufferedReader input = new BufferedReader(new InputStreamReader(System.in))) {
            for (String path; (path = input.readLine()) != null;) {
                try { System.out.println(parse(Path.of(path))); }
                catch (Exception error) { System.out.println("{\"error\":\"Independent Java structure parse failed\"}"); }
                System.out.flush();
            }
        }
    }
}
