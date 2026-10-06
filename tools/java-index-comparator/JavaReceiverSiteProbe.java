package sites;
import java.util.*;
class Probe {
 List<Leaf> fieldItems;
 int chain(Leaf input) {
  class Leaf { int decoy() { return 2; } }
  return input.next().marker();
 }
 int field(Holder input) {
  class Holder { Object leaf; }
  return input.leaf.marker();
 }
 int list(List<Leaf> input) {
  class Leaf { int decoy() { return 3; } }
  return input.get(0).marker();
 }
 int optional(Optional<Leaf> input) {
  class Leaf { int decoy() { return 4; } }
  return input.orElseThrow().marker();
 }
 int nested(Map<String, List<Leaf>> input) {
  class Leaf { int decoy() { return 5; } }
  return input.get("key").get(0).marker();
 }
 int box(Box<Leaf> input) {
  class Leaf { int decoy() { return 6; } }
  return input.get().marker();
 }
 int capture(List<Leaf> input) {
  class Worker {
   int invoke() {
    class Leaf { int decoy() { return 7; } }
    return input.get(0).marker();
   }
  }
  return 0;
 }
 int reference(List<Leaf> input) {
  class Leaf { int decoy() { return 8; } }
  java.util.function.IntSupplier task = input.get(0)::marker;
  return 0;
 }
 int variable() {
  List<Leaf> input = null;
  class Leaf { int decoy() { return 9; } }
  return input.get(0).marker();
 }
 int fieldGeneric() {
  class Leaf { int decoy() { return 10; } }
  return this.fieldItems.get(0).marker();
 }
}
