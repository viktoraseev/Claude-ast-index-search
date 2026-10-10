"""Source result ownership on disposable Java classpaths; not MCP equivalence."""
from pathlib import Path
import shutil
import subprocess
import tempfile

from common import ToolError, stable_id
from root_contracts import Runner

FEATURES = {'unused-deps:java-source-results'}
REASON = ('independent source/javac/CLI: explicit class generic result substitution, '
          'fields/records/inheritance and initializer-site var chains with access, '
          'ambiguity, class-variable formals/arrays/boxing, nominal variable-arity '
          'invocation phases, scalar method-variable inference, explicit witnesses, '
          'bounds, shadowing, array/spread formals and attached classpath guards; '
          'not MCP equivalence')

# Each input has an authored declaring owner. Invalid-Java guards must remain
# uncredited; VALID_NEGATIVE_CASES separately compile valid ownership controls.
CASES = {
    'method-inferred': ('class Box { <T> T get(T value){return value;} }', 'Box b, shared.Child c', 'b.get(c).instance()', True),
    'method-bounded': ('class Box { <T extends shared.Child> T get(T value){return value;} }', 'Box b, shared.Child c', 'b.get(c).instance()', True),
    'method-null-bound': ('class Box { <T extends shared.Child> T get(T value){return value;} }', 'Box b', 'b.get(null).instance()', True),
    'method-unused-bound': ('class Box { <T extends shared.Child> T get(){return null;} }', 'Box b', 'b.get().instance()', True),
    'method-multiple': ('class Box { <A,B> B get(A ignored,B value){return value;} }', 'Box b, shared.Child c', 'b.get("x",c).instance()', True),
    'method-shadow': ('class Box<T> { <T> T get(T value){return value;} }', 'Box<String> b, shared.Child c', 'b.get(c).instance()', True),
    'method-array': ('class Box { <T> T get(T[] value){return null;} }', 'Box b, shared.Child[] c', 'b.get(c).instance()', True),
    'method-postfix-array': ('class Box { <T> T get(T value[]){return null;} }', 'Box b, shared.Child[] c', 'b.get(c).instance()', True),
    'method-spread': ('class Box { <T> T get(T... value){return null;} }', 'Box b, shared.Child c', 'b.get(c,c).instance()', True),
    'method-fixed-spread': ('class Box { <T> T get(T... value){return null;} }', 'Box b, shared.Child[] c', 'b.get(c).instance()', True),
    'method-empty-bound-spread': ('class Box { <T extends shared.Child> T get(T... value){return null;} }', 'Box b', 'b.get().instance()', True),
    'method-nested-result': ('class Wrap<U> { U get(){return null;} } class Box { <T> Wrap<T> get(T value){return null;} }', 'Box b, shared.Child c', 'b.get(c).get().instance()', True),
    'method-inherited': ('class Parent { <T> T get(T value){return value;} } class Box extends Parent {}', 'Box b, shared.Child c', 'b.get(c).instance()', True),
    'method-class-bound': ('class Box<T> { <U extends T> U get(U value){return value;} }', 'Box<shared.Child> b, shared.Child c', 'b.get(c).instance()', True),
    'method-explicit': ('class Box { <T> T get(){return null;} }', 'Box b', 'b.<shared.Child>get().instance()', True),
    'method-explicit-null': ('class Box { <T> T get(T value){return value;} }', 'Box b', 'b.<shared.Child>get(null).instance()', True),
    'method-var-capture': ('class Box { <T> T get(T value){return value;} }', 'Box b, shared.Child c', 'var value=b.get(c); return ((java.util.function.IntSupplier)value::instance).getAsInt()', True),
    'method-private-guard': ('class Box { private <T> T get(T value){return value;} }', 'Box b, shared.Child c', 'b.get(c).instance()', False),
    'method-bound-guard': ('class Box { <T extends String> T get(T value){return value;} }', 'Box b, shared.Child c', 'b.get(c).instance()', False),
    'method-unbound-guard': ('class Box { <T> T get(){return null;} }', 'Box b', 'b.get().instance()', False),
    'method-null-guard': ('class Box { <T> T get(T value){return value;} }', 'Box b', 'b.get(null).instance()', False),
    'method-mixed-guard': ('class Box { <T> T get(T first,T second){return first;} }', 'Box b, shared.Child c', 'b.get(c,"x").instance()', False),
    'method-array-rank-guard': ('class Box { <T> T get(T[][] value){return null;} }', 'Box b, shared.Child[] c', 'b.get(c).instance()', False),
    'method-explicit-bound-guard': ('class Box { <T extends String> T get(){return null;} }', 'Box b', 'b.<shared.Child>get().instance()', False),
    'method-explicit-type-guard': ('class Box { <T> T get(T value){return value;} }', 'Box b', 'b.<shared.Child>get("x").instance()', False),
    'method-explicit-arity-guard': ('class Box { <T> T get(){return null;} }', 'Box b', 'b.<shared.Child,String>get().instance()', False),
    'method-overload-guard': ('class Box { <T> T get(T value){return value;} Object get(shared.Child value){return null;} }', 'Box b, shared.Child c', 'b.get(c).instance()', False),
    'formal-class-bounded': ('class Box<T extends shared.Child> { T get(T value){return value;} }', 'Box<shared.Child> b, shared.Child c', 'b.get(c).instance()', True),
    'formal-class-raw-bounded': ('class Box<T extends shared.Child> { T get(T value){return value;} }', 'Box b, shared.Child c', 'b.get(c).instance()', True),
    'formal-class-overridden': ('class Parent<T> { T get(T value){return value;} } class Box extends Parent<shared.Child> { shared.Child get(shared.Child value){return value;} }', 'Box b, shared.Child c', 'b.get(c).instance()', True),
    'formal-class-boxed': ('class Box<T> { shared.Child get(T value){return null;} }', 'Box<Integer> b', 'b.get(0).instance()', True),
    'formal-class-boxed-array': ('class Box<T> { shared.Child get(T[] value){return null;} }', 'Box<Integer> b, Integer[] c', 'b.get(c).instance()', True),
    'formal-class-boxed-spread': ('class Box<T> { shared.Child get(T... value){return null;} }', 'Box<Integer> b', 'b.get(0,1).instance()', True),
    'formal-class-platform-string': ('class Box<T> { shared.Child get(T value){return null;} }', 'Box<String> b', 'b.get("x").instance()', True),
    'formal-class-platform-object': ('class Box<T> { shared.Child get(T value){return null;} }', 'Box<Object> b', 'b.get("x").instance()', True),
    'formal-class-boxed-type-guard': ('class Box<T> { shared.Child get(T value){return null;} }', 'Box<Integer> b', 'b.get("x").instance()', False),
    'formal-class-boxed-array-guard': ('class Box<T> { shared.Child get(T[] value){return null;} }', 'Box<Integer> b, int[] c', 'b.get(c).instance()', False),
    'formal-class-strict-phase-guard': ('class Box<T> { shared.Child get(T value){return null;} Object get(long value){return null;} }', 'Box<Integer> b', 'b.get(0).instance()', False),
    'formal-class-local-wrapper-guard': ('class Integer {} class Box<T> { shared.Child get(T value){return null;} }', 'Box<Integer> b', 'b.get(0).instance()', False),
    'formal-class-result': ('class Box<T> { T get(T value){return value;} }', 'Box<shared.Child> b, shared.Child c', 'b.get(c).instance()', True),
    'formal-class-null': ('class Box<T> { T get(T value){return value;} }', 'Box<shared.Child> b', 'b.get(null).instance()', True),
    'formal-class-array': ('class Box<T> { T get(T[] value){return null;} }', 'Box<shared.Child> b, shared.Child[] c', 'b.get(c).instance()', True),
    'formal-class-postfix-array': ('class Box<T> { T get(T value[]){return null;} }', 'Box<shared.Child> b, shared.Child[] c', 'b.get(c).instance()', True),
    'formal-class-spread': ('class Box<T> { T get(T... value){return null;} }', 'Box<shared.Child> b, shared.Child c', 'b.get(c,c).instance()', True),
    'formal-class-empty-spread': ('class Box<T> { T get(T... value){return null;} }', 'Box<shared.Child> b', 'b.get().instance()', True),
    'formal-class-fixed-spread': ('class Box<T> { T get(T... value){return null;} }', 'Box<shared.Child> b, shared.Child[] c', 'b.get(c).instance()', True),
    'formal-class-inherited': ('class Parent<T> { T get(T value){return value;} } class Box<U> extends Parent<U> {}', 'Box<shared.Child> b, shared.Child c', 'b.get(c).instance()', True),
    'formal-class-reordered': ('class Other {} class Parent<A,B> { B get(A ignored,B value){return value;} } class Box<U,V> extends Parent<V,U> {}', 'Box<shared.Child,Other> b, Other a, shared.Child c', 'b.get(a,c).instance()', True),
    'formal-class-other-slot': ('class Box<A,B> { B get(B value){return value;} }', 'Box<String,shared.Child> b, shared.Child c', 'b.get(c).instance()', True),
    'formal-class-specific': ('class Box<T extends shared.Child> { T get(T value){return value;} Object get(Object value){return null;} }', 'Box<shared.Child> b, shared.Child c', 'b.get(c).instance()', True),
    'formal-class-type-guard': ('class Box<T> { T get(T value){return value;} }', 'Box<shared.Child> b', 'b.get("x").instance()', False),
    'formal-class-array-guard': ('class Box<T> { T get(T[] value){return null;} }', 'Box<shared.Child> b, shared.Child c', 'b.get(c).instance()', False),
    'formal-class-spread-guard': ('class Box<T> { T get(T... value){return null;} }', 'Box<shared.Child> b', 'b.get("x").instance()', False),
    'formal-class-private-guard': ('class Box<T> { private T get(T value){return value;} }', 'Box<shared.Child> b, shared.Child c', 'b.get(c).instance()', False),
    'formal-class-shadow-guard': ('class Box<T> { <T> Object get(T value){return value;} }', 'Box<shared.Child> b, shared.Child c', 'b.get(c).instance()', False),
    'formal-class-shadow-array-guard': ('class T extends shared.Child {} class Box<T> { <T extends String> shared.Child get(T[] value){return null;} }', 'Box<shared.Child> b, T[] c', 'b.get(c).instance()', False),
    'formal-class-wildcard-guard': ('class Box<T> { T get(T value){return value;} }', 'Box<?> b, shared.Child c', 'b.get(c).instance()', False),
    'formal-class-raw-guard': ('class Box<T> { T get(T value){return value;} }', 'Box b, shared.Child c', 'b.get(c).instance()', False),
    'formal-class-arity-guard': ('class Box<T> { T get(T value){return value;} }', 'Box<shared.Child> b, shared.Child c', 'b.get(c,c).instance()', False),
    'generic': ('class Box<T> { T get(){return null;} }', 'Box<shared.Child> b', 'b.get().instance()', True),
    'inferred': ('class Box { shared.Child get(){return null;} }', 'Box b', 'var c=b.get(); return c.instance()', True),
    'generic-var': ('class Box<T> { T get(){return null;} }', 'Box<shared.Child> b', 'var c=b.get(); return c.instance()', True),
    'generic-field': ('class Box<T> { T value; }', 'Box<shared.Child> b', 'b.value.instance()', True),
    'nested-result': ('class Box<T> { T get(){return null;} } class Wrap<U> { Box<U> get(){return null;} }', 'Wrap<shared.Child> b', 'b.get().get().instance()', True),
    'ordered-parameters': ('class Box<A,B> { B get(){return null;} }', 'Box<String,shared.Child> b', 'b.get().instance()', True),
    'inherited-result': ('class Box<T> { T get(){return null;} } class Wrap<U> extends Box<U> {}', 'Wrap<shared.Child> b', 'b.get().instance()', True),
    'record-result': ('record Box<T>(T value) {}', 'Box<shared.Child> b', 'b.value().instance()', True),
    'record-projection': ('class Box<T> { T get(){return null;} } record Wrap<U>(Box<U> value) {}', 'Wrap<shared.Child> b', 'b.value().get().instance()', True),
    'inherited-overload': ('', 'shared.Child b', 'b.choose("x")', True),
    'jdk-list-projection': ('record Box(java.util.List<shared.Child> values) {}', 'Box b', 'b.values().get(0).instance()', True),
    'boxed-result': ('class Box { shared.Child get(Object value){return null;} }', 'Box b', 'b.get(0).instance()', True),
    'unboxed-result': ('class Box { shared.Child get(int value){return null;} }', 'Box b, Integer value', 'b.get(value).instance()', True),
    'array-covariant-result': ('class Box { shared.Child get(Object[] value){return null;} }', 'Box b, String[] value', 'b.get(value).instance()', True),
    'boxed-exact-result': ('class Box { shared.Child get(Integer value){return null;} }', 'Box b', 'b.get(0).instance()', True),
    'unboxed-widened-result': ('class Box { shared.Child get(long value){return null;} }', 'Box b, Integer value', 'b.get(value).instance()', True),
    'array-nested-result': ('class Box { shared.Child get(Object[] value){return null;} }', 'Box b, int[][] value', 'b.get(value).instance()', True),
    'primitive-array-object-result': ('class Box { shared.Child get(Object value){return null;} }', 'Box b, int[] value', 'b.get(value).instance()', True),
    'strict-before-boxing-result': ('class Box { shared.Child get(long value){return null;} Object get(Integer value){return null;} }', 'Box b', 'b.get(0).instance()', True),
    'strict-before-unboxing-result': ('class Box { shared.Child get(Object value){return null;} Object get(int value){return null;} }', 'Box b, Integer value', 'b.get(value).instance()', True),
    'varargs-empty-result': ('class Box { shared.Child get(String... values){return null;} }', 'Box b', 'b.get().instance()', True),
    'varargs-single-result': ('class Box { shared.Child get(String... values){return null;} }', 'Box b', 'b.get("x").instance()', True),
    'varargs-many-result': ('class Box { shared.Child get(String... values){return null;} }', 'Box b', 'b.get("x","y").instance()', True),
    'varargs-array-result': ('class Box { shared.Child get(String... values){return null;} }', 'Box b, String[] values', 'b.get(values).instance()', True),
    'varargs-null-result': ('class Box { shared.Child get(String... values){return null;} }', 'Box b', 'b.get(null).instance()', True),
    'varargs-prefix-result': ('class Box { shared.Child get(int prefix, String... values){return null;} }', 'Box b', 'b.get(0,"x","y").instance()', True),
    'varargs-boxing-result': ('class Box { shared.Child get(Integer... values){return null;} }', 'Box b', 'b.get(0,1).instance()', True),
    'varargs-unboxing-result': ('class Box { shared.Child get(long... values){return null;} }', 'Box b, Integer value', 'b.get(value,value).instance()', True),
    'varargs-specific-result': ('class Box { shared.Child get(String... values){return null;} Object get(Object... values){return null;} }', 'Box b', 'b.get("x","y").instance()', True),
    'varargs-empty-specific-result': ('class Box { shared.Child get(String... values){return null;} Object get(Object... values){return null;} }', 'Box b', 'b.get().instance()', True),
    'varargs-inherited-result': ('class Parent { shared.Child get(String... values){return null;} } class Box extends Parent { Object get(int value){return null;} }', 'Box b', 'b.get("x","y").instance()', True),
    'varargs-inherited-owner': ('', 'shared.Child b', 'b.gather("x","y")', True),
    'fixed-before-varargs-result': ('class Box { shared.Child get(Object value){return null;} Object get(String... values){return null;} }', 'Box b', 'b.get("x").instance()', True),
    'loose-before-varargs-result': ('class Box { shared.Child get(Integer value){return null;} Object get(int... values){return null;} }', 'Box b', 'b.get(0).instance()', True),
    'varargs-capture-result': ('class Box { shared.Child get(String... values){return null;} }', 'Box b', 'var c=b.get("x","y"); return ((java.util.function.IntSupplier)c::instance).getAsInt()', True),
    'fixed-before-varargs-guard': ('class Box { Object get(Object value){return null;} shared.Child get(String... values){return null;} }', 'Box b', 'b.get("x").instance()', False),
    'varargs-prefix-guard': ('class Box { shared.Child get(int prefix, String... values){return null;} }', 'Box b', 'b.get().instance()', False),
    'varargs-type-guard': ('class Box { shared.Child get(String... values){return null;} }', 'Box b', 'b.get(0,1).instance()', False),
    'varargs-private-guard': ('class Box { private shared.Child get(String... values){return null;} }', 'Box b', 'b.get("x","y").instance()', False),
    'varargs-ambiguous-guard': ('class Box { shared.Child get(String... values){return null;} shared.Child get(Integer... values){return null;} }', 'Box b', 'b.get().instance()', False),
    'strict-before-boxing-guard': ('class Box { Object get(long value){return null;} shared.Child get(Integer value){return null;} }', 'Box b', 'b.get(0).instance()', False),
    'primitive-array-covariance-guard': ('class Box { shared.Child get(Object[] value){return null;} }', 'Box b, int[] value', 'b.get(value).instance()', False),
    'array-element-boxing-guard': ('class Box { shared.Child get(Integer[] value){return null;} }', 'Box b, int[] value', 'b.get(value).instance()', False),
    'widen-before-boxing-guard': ('class Box { shared.Child get(Long value){return null;} }', 'Box b', 'b.get(0).instance()', False),
    'unboxing-narrowing-guard': ('class Box { shared.Child get(int value){return null;} }', 'Box b, Long value', 'b.get(value).instance()', False),
    'shadowed-wrapper-guard': ('class Integer {} class Box { shared.Child get(int value){return null;} }', 'Box b, Integer value', 'b.get(value).instance()', False),
    'inherited-overload-variable': ('', 'shared.Child b, String value', 'b.choose(value)', True),
    'inherited-overload-null': ('', 'shared.Child b', 'b.choose(null)', True),
    'child-overload-control': ('', 'shared.Child b', 'b.choose(0)', False),
    'overloaded-result': ('class Box { shared.Child get(String value){return null;} Object get(int value){return null;} }', 'Box b', 'b.get("x").instance()', True),
    'overload-private-sibling': ('class Box { shared.Child get(String value){return null;} private Object get(int value){return null;} }', 'Box b', 'b.get("x").instance()', True),
    'argument-declaration-site': ('class Arg {} class Box { shared.Child get(Arg value){return null;} Object get(int value){return null;} }', 'Box b, Arg value', 'class Arg {} return b.get(value).instance()', True),
    'argument-local-shadow-guard': ('class Arg {} class Box { shared.Child get(Arg value){return null;} Object get(int value){return null;} }', 'Box b', 'class Arg {} Arg value=new Arg(); return b.get(value).instance()', False),
    'inherited-overloaded-result': ('class Parent<T> { T get(String value){return null;} } class Box<T> extends Parent<T> { Object get(int value){return null;} }', 'Box<shared.Child> b', 'b.get("x").instance()', True),
    'inherited-arity-result': ('class Parent<T> { T get(int value){return null;} } class Box<T> extends Parent<T> { Object get(){return null;} }', 'Box<shared.Child> b', 'b.get(0).instance()', True),
    'jdk-imported-list': ('import java.util.List; record Box(List<shared.Child> values) {}', 'Box b', 'var c=b.values().get(0); return c.instance()', True),
    'jdk-wildcard-list': ('import java.util.*; record Box(List<shared.Child> values) {}', 'Box b', 'b.values().get(0).instance()', True),
    'jdk-nested-projection': ('record Box(java.util.Map<String,java.util.List<shared.Child>> values) {}', 'Box b', 'b.values().get("x").get(0).instance()', True),
    'jdk-optional-projection': ('record Box(java.util.Optional<shared.Child> value) {}', 'Box b', 'b.value().get().instance()', True),
    'jdk-supplier-projection': ('record Box(java.util.function.Supplier<shared.Child> value) {}', 'Box b', 'b.value().get().instance()', True),
    'inherited-overload-arity-guard': ('', 'shared.Child b', 'b.choose()', False),
    'inherited-overload-type-guard': ('', 'shared.Child b', 'b.choose(new Object())', False),
    'overloaded-result-type-guard': ('class Box { shared.Child get(String value){return null;} Object get(int value){return null;} }', 'Box b', 'b.get(0).instance()', False),
    'overload-private-guard': ('class Box { public Object get(String value){return null;} private shared.Child get(int value){return null;} }', 'Box b', 'b.get(0).instance()', False),
    'jdk-index-type-guard': ('record Box(java.util.List<shared.Child> values) {}', 'Box b', 'b.values().get(0L).instance()', False),
    'jdk-list-arity-guard': ('record Box(java.util.List<shared.Child> values) {}', 'Box b', 'b.values().get().instance()', False),
    'jdk-list-wildcard-guard': ('record Box(java.util.List<?> values) {}', 'Box b', 'b.values().get(0).instance()', False),
    'jdk-list-raw-guard': ('record Box(java.util.List values) {}', 'Box b', 'b.values().get(0).instance()', False),
    'jdk-shadow-guard': ('class List<T> { Object get(int value){return null;} } record Box(List<shared.Child> values) {}', 'Box b', 'b.values().get(0).instance()', False),
    'jdk-static-guard': ('record Box(java.util.List<shared.Child> values) {}', 'Box b', 'java.util.List.get(0).instance()', False),
    'capture': ('class Box<T> { T get(){return null;} }', 'Box<shared.Child> b', 'var c=b.get(); return ((java.util.function.IntSupplier)()->c.instance()).getAsInt()', True),
    'reference': ('class Box<T> { T get(){return null;} }', 'Box<shared.Child> b', 'var c=b.get(); return ((java.util.function.IntSupplier)c::instance).getAsInt()', True),
    'later-shadow': ('class Box<T> { T get(){return null;} }', 'Box<shared.Child> b', 'var c=b.get(); class Child {} return c.instance()', True),
    'declaration-site': ('import shared.Child; class Box<T> { T get(){return null;} }', 'Box<Child> b', 'var c=b.get(); class Child {} class Box<T> {} return c.instance()', True),
    'bounded-result': ('class Box<T extends shared.Child> { T get(){return null;} }', 'Box<shared.Child> b', 'b.get().instance()', True),
    'raw-bounded-result': ('class Box<T extends shared.Child> { T get(){return null;} }', 'Box b', 'b.get().instance()', True),
    'bounded-record': ('record Box<T extends shared.Child>(T value) {}', 'Box<shared.Child> b', 'b.value().instance()', True),
    'bounded-record-field': ('record Box<T extends shared.Child>(T value) { int bound(){return value.instance();} }', 'Box<shared.Child> b', '0', True),
    'sibling-bound-guard': ('class Box<T> { T get(){return null;} } class Other<T extends shared.Child> {}', 'Box b', 'b.get().instance()', False),
    'raw-guard': ('class Box<T> { T get(){return null;} }', 'Box b', 'b.get().instance()', False),
    'wildcard-guard': ('class Box<T> { T get(){return null;} }', 'Box<?> b', 'b.get().instance()', False),
    'type-arity-guard': ('class Box<T> { T get(){return null;} }', 'Box<shared.Child,String> b', 'b.get().instance()', False),
    'method-shadow-guard': ('class Box<T> { <T> T get(){return null;} }', 'Box<shared.Child> b', 'b.get().instance()', False),
    'array-guard': ('class Box<T> { T[] get(){return null;} }', 'Box<shared.Child> b', 'b.get().instance()', False),
    'private-guard': ('class Box<T> { private T get(){return null;} }', 'Box<shared.Child> b', 'b.get().instance()', False),
    'arity-guard': ('class Box<T> { T get(int n){return null;} }', 'Box<shared.Child> b', 'b.get().instance()', False),
    'ambiguous-guard': ('class Box<T> { T get(Integer n){return null;} T get(String n){return null;} }', 'Box<shared.Child> b', 'b.get(null).instance()', False),
    'block-guard': ('class Box<T> { T get(){return null;} }', 'Box<shared.Child> b', '{var c=b.get();} return c.instance()', False),
    'sibling-guard': ('class Box<T> { T get(){return null;} }', 'Box<shared.Child> b', 'var c=b.get(); return 0; } int other(){return c.instance()', False),
}

# Valid calls selecting the child overload must not borrow the parent's owner.
# This is a positive Java compilation with an authored negative ownership result.
VALID_NEGATIVE_CASES = {'child-overload-control'}


def plan_results(state, root):
    if root is None:
        return
    with state:
        for feature in FEATURES:
            subject = 'disposable-java-source-result-ownership'
            state.execute('INSERT OR REPLACE INTO coverage VALUES (?,?,?)', (feature, 'implemented', REASON))
            state.execute('INSERT OR IGNORE INTO checks(id,feature,subject) VALUES (?,?,?)',
                          (stable_id({'feature': feature, 'subject': subject}), feature, subject))
        # This finite child never closes the full shared/semantic parents.
        note = ('; executed scalar method-variable checklist covers inferred/explicit/bounded '
                'results, class/method shadow separation, array/postfix/spread formals, nested '
                'result projections, inheritance, capture, access/type/arity/overload guards '
                'and attached declaring imports with JSON/text/options/update/rebuild; '
                'parameterized formal and target-dependent inference, intersection/common '
                'bound inference, overload erasure and classpath ordering remain pending; '
                'independent source/javac/CLI, not MCP equivalence')
        state.execute("UPDATE coverage SET reason=reason || ? WHERE feature='unused-deps:semantic-resolution' "
                      "AND status='pending' AND instr(reason,'executed scalar method-variable checklist')=0", (note,))


def exercise(binary, base):
    base = Path(base).resolve()
    if not base.is_relative_to((Path(__file__).resolve().parents[2] / '.artifacts').resolve()):
        raise ToolError('source result fixtures must stay inside repository .artifacts')
    base.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='java-results-', dir=base) as temporary:
        runner = Runner(binary, Path(temporary).resolve())
        runner.root.mkdir()
        runner.environment['AST_INDEX_ROOT'] = str(runner.root)
        expected, actual = ({feature: {} for feature in FEATURES} for _ in range(2))
        feature = next(iter(FEATURES))

        def write(relative, source, root=None):
            path = (root or runner.root) / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(source + '\n')
            return path

        write('base/build.gradle', 'plugins {}')
        write('lib/build.gradle', 'dependencies { api(project(":base")) }')
        base_source = write('base/Base.java', 'package shared; public class Base { public int instance(){return 1;} public int choose(String value){return 2;} public int gather(String... values){return 4;} }')
        child_source = write('lib/Child.java', 'package shared; public class Child extends Base { public int choose(int value){return 3;} }')
        valid, invalid = [], []
        for label, (declarations, parameter, body, used) in CASES.items():
            body = body if 'return ' in body else 'return ' + body
            source = 'package fixture.' + label.replace('-', '_') + '; ' + declarations + ' class Use { int run(' + parameter + '){' + body + ';} }'
            path = write(label + '/Use.java', source)
            write(label + '/build.gradle', 'dependencies { implementation(project(":base")); implementation(project(":lib")) }')
            if used or label in VALID_NEGATIVE_CASES:
                valid.append(path)
            else:
                invalid.append(path)
        javac = shutil.which('javac')
        if javac is None:
            raise ToolError('source result validation requires javac')
        with (runner.directory / 'javac.log').open('wb') as log:
            result = subprocess.run([javac, '-proc:none', '-d', str(runner.directory / 'classes'),
                                     str(base_source), str(child_source), *map(str, valid)],
                                    stdout=log, stderr=log, timeout=30)
        if result.returncode:
            raise ToolError('authored positive result fixtures failed javac; see private log')
        for path in invalid:
            with (runner.directory / (path.parent.name + '.javac.log')).open('wb') as log:
                result = subprocess.run([javac, '-proc:none', '-d', str(runner.directory / 'guard-classes'),
                    str(base_source), str(child_source), str(path)], stdout=log, stderr=log, timeout=30)
            if result.returncode == 0:
                raise ToolError('a claimed ambiguity/access guard compiled; retain as a pending Java obligation')
        runner.command('rebuild', '--force', '--max-files', '0')

        def record(label, want, got):
            expected[feature][label], actual[feature][label] = want, got

        def classifications(module, flags=(), cwd=None):
            doc = runner.json('unused-deps', module, '--verbose', *flags, cwd=cwd)
            if not isinstance(doc.get('items'), list) or doc.get('error'):
                raise ToolError('source results did not execute unused-deps')
            return sorted((row['name'], row['category'], row['examples']['direct']) for row in doc['items'])

        for label, (declarations, parameter, body, used) in CASES.items():
            # Even a guard has the explicitly written Child type as a direct
            # dependency. Its downstream Base must never be inferred by name.
            child = ['Child'] if 'Child' in parameter + declarations + (body if label.startswith('method-') else '') else []
            want = [('base', 'direct' if used else 'unused', ['Base'] if used else []),
                    ('lib', 'direct' if child else 'unused', child)]
            for flags in ((), ('--strict',)):
                record(label + ':' + str(bool(flags)), want, classifications(label, flags))
            _, text = runner.command('unused-deps', label, '--verbose', '--strict')
            record(label + ':text', True, ('Base' in text) == used)
            if 'varargs' in label:
                for flags in (('--no-transitive',), ('--no-xml',), ('--no-resources',),
                              ('--no-transitive', '--no-xml', '--no-resources')):
                    record(label + ':options:' + ','.join(flags), want, classifications(label, flags))
                _, text = runner.command('unused-deps', label, '--verbose')
                record(label + ':default-text', True, ('Base' in text) == used)

        # A provider result is resolved in its own imports, across the selected
        # attached classpath. Colliding primary definitions are a negative
        # control, never a reason to borrow the primary root's declaring owner.
        attached = runner.directory / 'attached'
        attached.mkdir()
        for relative, source in {
            'base/build.gradle': 'plugins {}',
            'base/Base.java': 'package shared; public class Base { public int instance(){return 2;} }',
            'lib/build.gradle': 'dependencies { api(project(":base")) }',
            'lib/Child.java': 'package shared; public class Child extends Base {}',
            'box/build.gradle': 'plugins {}',
            'box/Box.java': 'package api; public class Box<T> { public T get(){return null;} }',
            'consumer/build.gradle': 'dependencies { implementation(project(":base")); implementation(project(":lib")); implementation(project(":box")) }',
            'consumer/Use.java': 'package fixture; class Use { int run(api.Box<shared.Child> b){var c=b.get(); return c.instance();} }',
        }.items():
            write(relative, source, attached)
        runner.command('subtree', 'add', 'attached', '../attached')
        with (runner.directory / 'attached.javac.log').open('wb') as log:
            result = subprocess.run([javac, '-proc:none', '-d', str(runner.directory / 'attached-classes'),
                *map(str, sorted(attached.rglob('*.java')))], stdout=log, stderr=log, timeout=30)
        if result.returncode:
            raise ToolError('attached source result fixtures failed javac; see private log')
        runner.command('rebuild', '--force', '--max-files', '0')
        want = [('attached::base', 'direct', ['Base']), ('attached::box', 'direct', ['Box']), ('attached::lib', 'direct', ['Child'])]
        for flags in ((), ('--strict',)):
            record('attached:' + str(bool(flags)), want, classifications('consumer', flags, attached))
        # Keep exact source expectations after incremental graph/module refresh.
        write('box/Box.java', 'package api; public class Box<T> { public T get(){return null;} public int unused(){return 0;} }', attached)
        runner.command('update')
        record('attached:update', want, classifications('consumer', ('--strict',), attached))
        write('box/Wrap.java', 'package api; public class Wrap<U> extends Box<U> {}', attached)
        write('consumer/Use.java', 'package fixture; class Use { int run(api.Wrap<shared.Child> b){var c=b.get(); return c.instance();} }', attached)
        runner.command('update')
        record('attached:inherited-result', [('attached::base', 'direct', ['Base']),
            ('attached::box', 'direct', ['Box', 'Wrap']), ('attached::lib', 'direct', ['Child'])],
            classifications('consumer', ('--strict',), attached))
        write('box/Box.java', 'package api; public class Box<T> { public T get(String... values){return null;} }', attached)
        write('consumer/Use.java', 'package fixture; class Use { int run(api.Wrap<shared.Child> b){var c=b.get("x","y"); return c.instance();} }', attached)
        with (runner.directory / 'attached.varargs.javac.log').open('wb') as log:
            result = subprocess.run([javac, '-proc:none', '-d', str(runner.directory / 'attached-classes'),
                *map(str, sorted(attached.rglob('*.java')))], stdout=log, stderr=log, timeout=30)
        if result.returncode:
            raise ToolError('attached varargs source fixture failed javac; see private log')
        runner.command('update')
        varargs_want = [('attached::base', 'direct', ['Base']), ('attached::box', 'direct', ['Box', 'Wrap']),
                        ('attached::lib', 'direct', ['Child'])]
        for flags in ((), ('--strict',), ('--no-transitive', '--no-xml', '--no-resources')):
            record('attached:varargs:' + ','.join(flags), varargs_want, classifications('consumer', flags, attached))
        runner.command('rebuild', '--force', '--max-files', '0')
        record('attached:varargs:rebuild', varargs_want, classifications('consumer', ('--strict',), attached))
        write('consumer/Use.java', 'package fixture; class Use { int run(api.Box<shared.Child> b){var c=b.get(); return c.instance();} }', attached)
        write('box/Box.java', 'package api; public class Box<T> { public Object get(){return null;} }', attached)
        runner.command('update')
        record('attached:changed-result', [('attached::base', 'unused', []), ('attached::box', 'direct', ['Box']), ('attached::lib', 'direct', ['Child'])],
               classifications('consumer', ('--strict',), attached))
        # Class formals bind in the declaring provider, including inherited
        # parameter reordering. Keep primary/attached ownership distinct.
        write('box/Box.java', 'package api; public class Box<T> { public T get(T... values){return null;} }', attached)
        write('consumer/Use.java', 'package fixture; class Use { int run(api.Wrap<shared.Child> b, shared.Child c){var value=b.get(c,c); return value.instance();} }', attached)
        with (runner.directory / 'attached.formals.javac.log').open('wb') as log:
            result = subprocess.run([javac, '-proc:none', '-d', str(runner.directory / 'attached-formal-classes'),
                *map(str, sorted(attached.rglob('*.java')))], stdout=log, stderr=log, timeout=30)
        if result.returncode:
            raise ToolError('attached generic formal fixture failed javac; see private log')
        runner.command('update')
        formal_want = [('attached::base', 'direct', ['Base']), ('attached::box', 'direct', ['Box', 'Wrap']),
                       ('attached::lib', 'direct', ['Child'])]
        for flags in ((), ('--strict',), ('--no-transitive',), ('--no-xml',), ('--no-resources',),
                      ('--no-transitive', '--no-xml', '--no-resources')):
            record('attached:formals:' + ','.join(flags), formal_want, classifications('consumer', flags, attached))
        _, text = runner.command('unused-deps', 'consumer', '--verbose', '--strict', cwd=attached)
        record('attached:formals:text', True, 'Base' in text)
        runner.command('rebuild', '--force', '--max-files', '0')
        record('attached:formals:rebuild', formal_want, classifications('consumer', ('--strict',), attached))
        write('box/Box.java', 'package api; public class Box<T> { public Object get(T... values){return null;} }', attached)
        runner.command('update')
        record('attached:formals:changed-result', [('attached::base', 'unused', []), ('attached::box', 'direct', ['Box', 'Wrap']),
            ('attached::lib', 'direct', ['Child'])], classifications('consumer', ('--strict',), attached))
        # Method variables bind from consumer arguments, while provider bounds
        # bind in provider imports. Neither may borrow Box's class parameter.
        method_cases = (
            ('inferred', '<U> U get(U value){return value;}', 'shared.Child c', 'b.get(c)'),
            ('shadow', '<T> T get(T value){return value;}', 'shared.Child c', 'b.get(c)'),
            ('explicit', '<U> U get(){return null;}', 'shared.Child c', 'b.<shared.Child>get()'),
            ('bounded', '<U extends Child> U get(){return null;}', 'shared.Child c', 'b.get()'),
            ('array', '<U> U get(U[] value){return null;}', 'shared.Child[] c', 'b.get(c)'),
            ('spread', '<U> U get(U... value){return null;}', 'shared.Child c', 'b.get(c,c)'),
        )
        for label, declaration, parameter, expression in method_cases:
            write('box/Box.java', 'package api; import shared.Child; public class Box<T> { public ' + declaration + ' }', attached)
            write('box/build.gradle', 'dependencies { api(project(":lib")) }', attached)
            write('consumer/Use.java', 'package fixture; class Use { int run(api.Wrap<String> b, ' + parameter + '){var value=' + expression + '; return value.instance();} }', attached)
            with (runner.directory / ('attached.method-' + label + '.javac.log')).open('wb') as log:
                result = subprocess.run([javac, '-proc:none', '-d', str(runner.directory / 'attached-method-classes'),
                    *map(str, sorted(attached.rglob('*.java')))], stdout=log, stderr=log, timeout=30)
            if result.returncode:
                raise ToolError('attached method-variable fixture failed javac; see private log')
            runner.command('update')
            for flags in ((), ('--strict',), ('--no-transitive',), ('--no-xml',), ('--no-resources',),
                          ('--no-transitive', '--no-xml', '--no-resources')):
                record('attached:method-' + label + ':' + ','.join(flags), formal_want,
                       classifications('consumer', flags, attached))
            _, text = runner.command('unused-deps', 'consumer', '--verbose', '--strict', cwd=attached)
            record('attached:method-' + label + ':text', True, 'Base' in text)
            runner.command('rebuild', '--force', '--max-files', '0')
            record('attached:method-' + label + ':rebuild', formal_want,
                   classifications('consumer', ('--strict',), attached))
        write('box/Box.java', 'package api; public class Box<T> { public <U> Object get(U... value){return null;} }', attached)
        runner.command('update')
        record('attached:method:changed-result', [('attached::base', 'unused', []), ('attached::box', 'direct', ['Box', 'Wrap']),
            ('attached::lib', 'direct', ['Child'])], classifications('consumer', ('--strict',), attached))
        return expected, actual
