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

    static String importIdentifier(Tree tree) {
        // Tree.toString() pretty-prints non-ASCII names as Unicode escapes;
        // identifier names retain the actual Java spelling and source anchor.
        if (tree instanceof IdentifierTree identifier) return identifier.getName().toString();
        if (tree instanceof MemberSelectTree member)
            return importIdentifier(member.getExpression()) + "." + member.getIdentifier();
        throw new IllegalStateException("Unexpected import identifier tree");
    }

    static String parse(Path file) throws Exception {
        JavaCompiler compiler = ToolProvider.getSystemJavaCompiler();
        if (compiler == null) throw new IllegalStateException("A JDK is required");
        String source = Files.readString(file);
        DiagnosticCollector<JavaFileObject> diagnostics = new DiagnosticCollector<>();
        try (StandardJavaFileManager manager = compiler.getStandardFileManager(diagnostics, null, null)) {
            JavacTask task = (JavacTask) compiler.getTask(null, manager, diagnostics,
                List.of("-proc:none", "--enable-preview", "--source", Integer.toString(Runtime.version().feature())),
                null, manager.getJavaFileObjects(file));
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
                    if (tree instanceof VariableTree variable && variable.getInitializer() != null
                        && start(variable.getInitializer()) > begin)
                        finish = start(variable.getInitializer());
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
                    emit(kind, name, position, position);
                }
                void emit(String kind, String name, int position, int finish) {
                    emit(kind, name, position, finish, "");
                }
                void emit(String kind, String name, int position, int finish, String extra) {
                    String qualified = qualifiedName(kind, name);
                    boolean api = !List.of("import", "usage", "annotation", "component").contains(kind)
                        && publicApi(getCurrentPath(), kind.equals("accessor"));
                    entries.add("{\"kind\":" + quote(kind) + ",\"name\":" + quote(name)
                        + ",\"line\":" + unit.getLineMap().getLineNumber(position)
                        + ",\"column\":" + (position - source.lastIndexOf('\n', position - 1))
                        + ",\"end_line\":" + unit.getLineMap().getLineNumber(Math.max(position, finish - 1))
                        + (qualified == null ? "" : ",\"qualified_name\":" + quote(qualified))
                        + ",\"public_api\":" + api + extra + "}");
                }
                boolean publicApi(TreePath path, boolean accessor) {
                    Tree declaration = path.getLeaf();
                    if (accessor && declaration instanceof ClassTree)
                        return publicApi(path, false);
                    ModifiersTree modifiers;
                    if (declaration instanceof ClassTree type) {
                        if (type.getSimpleName().length() == 0) return false;
                        modifiers = type.getModifiers();
                    } else if (declaration instanceof MethodTree method) modifiers = method.getModifiers();
                    else if (declaration instanceof VariableTree variable) modifiers = variable.getModifiers();
                    else return false;
                    TreePath parent = path.getParentPath();
                    if (parent == null) return false;
                    Tree owner = parent.getLeaf();
                    boolean implicit = owner instanceof ClassTree type
                        && (type.getKind() == Tree.Kind.INTERFACE || type.getKind() == Tree.Kind.ANNOTATION_TYPE
                            || (type.getKind() == Tree.Kind.ENUM && declaration instanceof VariableTree variable
                                && variable.getInitializer() instanceof NewClassTree
                                && source.substring(start(variable), end(variable)).stripLeading()
                                    .startsWith(variable.getName().toString())));
                    var flags = modifiers.getFlags();
                    if (flags.contains(javax.lang.model.element.Modifier.PRIVATE)
                        || flags.contains(javax.lang.model.element.Modifier.PROTECTED)
                        || (!flags.contains(javax.lang.model.element.Modifier.PUBLIC) && !implicit)) return false;
                    return owner instanceof CompilationUnitTree || (owner instanceof ClassTree && publicApi(parent, false));
                }
                String qualifiedName(String kind, String name) {
                    if (List.of("import", "usage", "annotation").contains(kind)) return null;
                    List<String> owners = new ArrayList<>();
                    TreePath current = getCurrentPath();
                    for (TreePath path = current; path != null; path = path.getParentPath()) {
                        Tree tree = path.getLeaf();
                        if (tree instanceof ClassTree type) {
                            if (type.getSimpleName().length() == 0) return null;
                            owners.add(type.getSimpleName().toString());
                        } else if (path != current && (tree instanceof MethodTree || tree instanceof BlockTree)) {
                            return null; // Local and anonymous declarations have no Java qualified name.
                        }
                    }
                    Collections.reverse(owners);
                    if (!List.of("class", "interface", "enum").contains(kind)) owners.add(name);
                    String prefix = unit.getPackageName() == null ? "" : unit.getPackageName() + ".";
                    return prefix + String.join(".", owners);
                }
                @Override public Void visitMethod(MethodTree tree, Void unused) {
                    if (tree.getName().contentEquals("<init>")) {
                        ClassTree owner = (ClassTree) getCurrentPath().getParentPath().getLeaf();
                        String name = owner.getSimpleName().toString();
                        emit("constructor", name, namePosition(tree, name, "\\s*[({]"), end(tree));
                    } else {
                        String name = tree.getName().toString();
                        boolean overrides = tree.getModifiers().getAnnotations().stream()
                            .anyMatch(annotation -> List.of("Override", "java.lang.Override")
                                .contains(annotation.getAnnotationType().toString()));
                        emit("method", name, namePosition(tree, name, "\\s*\\("), end(tree),
                            ",\"overrides\":" + overrides);
                    }
                    return super.visitMethod(tree, unused);
                }
                @Override public Void visitCompilationUnit(CompilationUnitTree tree, Void unused) {
                    scan(tree.getPackageAnnotations(), unused);
                    scan(tree.getImports(), unused);
                    scan(tree.getTypeDecls(), unused);
                    return null;
                }
                @Override public Void visitImport(ImportTree tree, Void unused) {
                    String full = importIdentifier(tree.getQualifiedIdentifier());
                    if (!full.endsWith(".*")) {
                        String name = full.substring(full.lastIndexOf('.') + 1);
                        emit("import", name, namePosition(tree, name, ""), end(tree));
                    }
                    return null;
                }
                @Override public Void visitIdentifier(IdentifierTree tree, Void unused) {
                    String name = tree.getName().toString();
                    int begin = start(tree), finish = end(tree);
                    // javac synthesizes the enum type at constant constructor
                    // sites. A lexical reference must have an actual source token.
                    if (!name.equals("this") && !name.equals("super") && begin >= 0 && finish >= begin
                        && source.substring(begin, finish).equals(name)) emitUsage(tree, name, begin);
                    return super.visitIdentifier(tree, unused);
                }
                @Override public Void visitMemberSelect(MemberSelectTree tree, Void unused) {
                    String name = tree.getIdentifier().toString();
                    if (!name.equals("class") && !name.equals("this") && !name.equals("super"))
                        emitUsage(tree, name, end(tree) - name.length());
                    return super.visitMemberSelect(tree, unused);
                }
                @Override public Void visitMemberReference(MemberReferenceTree tree, Void unused) {
                    if (tree.getMode() != MemberReferenceTree.ReferenceMode.NEW) {
                        String name = tree.getName().toString();
                        emitUsage(tree, name, end(tree) - name.length());
                    }
                    return super.visitMemberReference(tree, unused);
                }
                void emitUsage(Tree tree, String name, int position) {
                    TreePath parent = getCurrentPath().getParentPath();
                    Tree reference = tree;
                    while (parent != null && parent.getLeaf() instanceof ParameterizedTypeTree type
                        && type.getType() == reference) {
                        reference = parent.getLeaf();
                        parent = parent.getParentPath();
                    }
                    String syntax = tree instanceof MemberReferenceTree ? "method_reference"
                        : parent != null && parent.getLeaf() instanceof NewClassTree creation
                          && creation.getIdentifier() == reference ? "constructor_call"
                        : parent != null && parent.getLeaf() instanceof MemberReferenceTree member
                          && member.getMode() == MemberReferenceTree.ReferenceMode.NEW
                          && member.getQualifierExpression() == reference ? "constructor_reference"
                        : parent != null && parent.getLeaf() instanceof MethodInvocationTree call
                          && call.getMethodSelect() == tree ? "call" : "value";
                    String extra = ",\"usage_kind\":" + quote(syntax);
                    if (tree instanceof MemberReferenceTree) {
                        int begin = start(tree);
                        extra += ",\"reference_line\":" + unit.getLineMap().getLineNumber(begin)
                            + ",\"reference_column\":" + (begin - source.lastIndexOf('\n', begin - 1));
                    }
                    emit("usage", name, position, position, extra);
                }
                @Override public Void visitMethodInvocation(MethodInvocationTree tree, Void unused) {
                    if (tree.getMethodSelect() instanceof IdentifierTree identifier
                        && (identifier.getName().contentEquals("this") || identifier.getName().contentEquals("super"))) {
                        int position = start(identifier);
                        emit("usage", identifier.getName().toString(), position, position,
                            ",\"usage_kind\":\"constructor_call\"");
                    }
                    return super.visitMethodInvocation(tree, unused);
                }
                @Override public Void visitVariable(VariableTree tree, Void unused) {
                    if (getCurrentPath().getParentPath().getLeaf() instanceof ClassTree owner
                        && (owner.getKind() != Tree.Kind.RECORD || tree.getModifiers().getFlags().contains(javax.lang.model.element.Modifier.STATIC))) {
                        String name = tree.getName().toString();
                        // javac gives enum constants a synthetic type without
                        // an end position, anchored at the identifier. With
                        // arguments, NewClassTree starts at '(', not the name;
                        // annotations may start VariableTree earlier still.
                        boolean constant = owner.getKind() == Tree.Kind.ENUM && tree.getInitializer() instanceof NewClassTree
                            && end(tree.getType()) < 0;
                        emit(constant ? "constant" : "property", name,
                            constant ? start(tree.getType()) : namePosition(tree, name, ""), end(tree));
                        if (constant) {
                            // javac's enum creation type is synthetic: the real
                            // constructor reference is anchored at the constant.
                            emit("usage", owner.getSimpleName().toString(), start(tree.getType()),
                                start(tree.getType()), ",\"usage_kind\":\"constructor_call\",\"implicit\":true");
                        }
                    }
                    return super.visitVariable(tree, unused);
                }
                @Override public Void visitClass(ClassTree tree, Void unused) {
                    String typeName = tree.getSimpleName().toString();
                    if (!typeName.isEmpty()) {
                        // Java comments are whitespace between declaration tokens.
                        Matcher anchor = Pattern.compile("(?:class|interface|enum|record)"
                            + "(?:\\s|/\\*.*?\\*/|//[^\\r\\n]*(?:\\r\\n|\\r|\\n))+("
                            + Pattern.quote(typeName) + ")(?![\\w$])", Pattern.DOTALL)
                            .matcher(source.substring(start(tree), end(tree)));
                        if (!anchor.find()) throw new IllegalStateException("Missing type anchor");
                        String kind = switch (tree.getKind()) {
                            case INTERFACE, ANNOTATION_TYPE -> "interface";
                            case ENUM -> "enum";
                            default -> "class";
                        };
                        emit(kind, typeName, start(tree) + anchor.start(1), end(tree));
                    }
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
                    String simple = name.substring(name.lastIndexOf('.') + 1);
                    String extra = "";
                    TreePath parent = getCurrentPath().getParentPath();
                    if (List.of("Inject", "Autowired").contains(simple)
                        && parent != null && parent.getLeaf() instanceof ModifiersTree) {
                        Tree owner = parent.getParentPath().getLeaf();
                        List<Tree> types = new ArrayList<>();
                        if (owner instanceof VariableTree variable) types.add(variable.getType());
                        if (owner instanceof MethodTree method)
                            for (VariableTree parameter : method.getParameters()) types.add(parameter.getType());
                        List<String> targets = new ArrayList<>();
                        for (Tree type : types) {
                            if (type == null || start(type) < 0 || end(type) < start(type)) continue;
                            new TreeScanner<Void, Void>() {
                                void target(String name, int position) {
                                    targets.add("{\"name\":" + quote(name) + ",\"line\":"
                                        + unit.getLineMap().getLineNumber(position) + "}");
                                }
                                @Override public Void visitIdentifier(IdentifierTree identifier, Void unused) {
                                    target(identifier.getName().toString(), start(identifier));
                                    return null;
                                }
                                @Override public Void visitMemberSelect(MemberSelectTree member, Void unused) {
                                    String name = member.getIdentifier().toString();
                                    target(name, end(member) - name.length());
                                    return super.visitMemberSelect(member, unused);
                                }
                                @Override public Void visitPrimitiveType(PrimitiveTypeTree primitive, Void unused) {
                                    target(primitive.toString(), start(primitive));
                                    return null;
                                }
                                @Override public Void visitAnnotation(AnnotationTree annotation, Void unused) {
                                    return null; // Type-use annotation names/arguments are not injected types.
                                }
                            }.scan(type, null);
                        }
                        extra = ",\"injection_targets\":[" + String.join(",", targets) + "]";
                    }
                    if (List.of("Provides", "Binds").contains(simple)
                        && parent != null && parent.getLeaf() instanceof ModifiersTree
                        && parent.getParentPath().getLeaf() instanceof MethodTree method
                        && method.getReturnType() != null) {
                        Tree type = method.getReturnType();
                        while (true) {
                            if (type instanceof ParameterizedTypeTree parameterized) type = parameterized.getType();
                            else if (type instanceof AnnotatedTypeTree annotated) type = annotated.getUnderlyingType();
                            else if (type instanceof ArrayTypeTree array) type = array.getType();
                            else break;
                        }
                        extra += ",\"method_name\":" + quote(method.getName().toString())
                            + ",\"method_position\":" + start(method)
                            + ",\"return_type\":" + quote(type.toString());
                    }
                    emit("annotation", "@" + simple, start(tree), start(tree), extra);
                    return super.visitAnnotation(tree, unused);
                }
            }.scan(unit, null);
            List<String> imports = new ArrayList<>();
            for (ImportTree declaration : unit.getImports())
                imports.add(quote(importIdentifier(declaration.getQualifiedIdentifier())));
            return "{\"entries\":[" + String.join(",", entries)
                + "],\"imports\":[" + String.join(",", imports) + "]}";
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
