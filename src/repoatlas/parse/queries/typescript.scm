; Tag query for TypeScript and TSX.
;
; The two grammars differ only in how they resolve angle brackets, so the
; node types named here exist in both and one file serves them. JavaScript
; needs its own file: a query naming `interface_declaration` will not
; compile against a grammar that has no such node.

; --- definitions ---------------------------------------------------------

(class_declaration
  name: (type_identifier) @name) @definition.class

(abstract_class_declaration
  name: (type_identifier) @name) @definition.class

(interface_declaration
  name: (type_identifier) @name) @definition.interface

(type_alias_declaration
  name: (type_identifier) @name) @definition.type

(enum_declaration
  name: (identifier) @name) @definition.enum

(function_declaration
  name: (identifier) @name) @definition.function

(generator_function_declaration
  name: (identifier) @name) @definition.function

(method_definition
  name: (property_identifier) @name) @definition.method

(abstract_method_signature
  name: (property_identifier) @name) @definition.method

(public_field_definition
  name: (property_identifier) @name) @definition.field

; Signatures are scoped to an interface body on purpose. The same node types
; appear inside an inline object type, as in `({ label }: { label: string })`,
; where the members are shape rather than named symbols anyone can navigate
; to; capturing those would fill the index with parameter noise.
(interface_declaration
  body: (interface_body
    (method_signature
      name: (property_identifier) @name) @definition.method))

(interface_declaration
  body: (interface_body
    (property_signature
      name: (property_identifier) @name) @definition.field))

; A function assigned to a name is a function, not a variable. This pattern
; is more specific than the plain declarator below and wins the tie.
(variable_declarator
  name: (identifier) @name
  value: [(arrow_function) (function_expression)]) @definition.function

(variable_declarator
  name: (identifier) @name) @definition.constant

; --- references ----------------------------------------------------------

(call_expression
  function: (identifier) @name) @reference.call

(call_expression
  function: (member_expression
    property: (property_identifier) @name)) @reference.call

(new_expression
  constructor: (identifier) @name) @reference.call

(extends_clause
  value: (identifier) @name) @reference.class

(implements_clause
  (type_identifier) @name) @reference.class

(type_annotation
  (type_identifier) @name) @reference.type

(type_annotation
  (generic_type
    name: (type_identifier) @name)) @reference.type

; --- imports -------------------------------------------------------------

(import_specifier
  name: (identifier) @name) @reference.import

(namespace_import
  (identifier) @name) @reference.import

(import_clause
  (identifier) @name) @reference.import
