; Classes
(class_declaration
  name: (identifier) @class_name) @class_node @definition

; Records (index as class-like types)
(record_declaration
  name: (identifier) @class_name) @class_node @definition

; Record components (header parameters in `record Foo(Type x, Type y)`)
(record_declaration
  parameters: (formal_parameters
    (formal_parameter
      name: (identifier) @record_component_name) @record_component_node @definition))

(record_declaration
  parameters: (formal_parameters
    (spread_parameter
      (variable_declarator
        name: (identifier) @record_component_name)) @record_component_node @definition))

; Interfaces
(interface_declaration
  name: (identifier) @interface_name) @interface_node @definition

; Annotation declarations are Java interface types, distinct from annotation uses.
(annotation_type_declaration
  name: (identifier) @interface_name) @interface_node @definition

; Enums
(enum_declaration
  name: (identifier) @enum_name) @enum_node @definition

; Enum constants, including constants with arguments or anonymous bodies
(enum_constant
  name: (identifier) @enum_constant_name) @definition

; Methods
(method_declaration
  name: (identifier) @method_name) @method_node @definition

; Annotation elements are callable declarations, including elements with defaults.
(annotation_type_element_declaration
  name: (identifier) @method_name) @method_node @definition

; Constructors
(constructor_declaration
  name: (identifier) @constructor_name) @constructor_node @definition

; Compact record constructors have no parameter list.
(compact_constructor_declaration
  name: (identifier) @constructor_name) @constructor_node @definition

; Fields
(field_declaration
  declarator: (variable_declarator
    name: (identifier) @field_name)) @field_node @definition

; Interface and annotation fields use a different grammar node from class fields.
(constant_declaration
  declarator: (variable_declarator
    name: (identifier) @field_name)) @field_node @definition

; Annotations (marker - no arguments, like @Override)
(marker_annotation
  name: [(identifier) (scoped_identifier)] @annotation_name) @definition

; Annotations (with arguments, like @GetMapping("/users"))
(annotation
  name: [(identifier) (scoped_identifier)] @annotation_call_name) @definition

; Imports are indexed separately from usages, under the imported simple name.
(import_declaration) @import_node @definition
