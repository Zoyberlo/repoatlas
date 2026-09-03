; Tag query for JavaScript and JSX.
;
; A trimmed TypeScript query: same shapes, minus every node the JS grammar
; does not have. Keeping it separate rather than sharing typescript.scm is
; not duplication for its own sake, since a query that names a node type the
; grammar lacks fails to compile outright.

; --- definitions ---------------------------------------------------------

(class_declaration
  name: (identifier) @name) @definition.class

(function_declaration
  name: (identifier) @name) @definition.function

(generator_function_declaration
  name: (identifier) @name) @definition.function

(method_definition
  name: (property_identifier) @name) @definition.method

(field_definition
  property: (property_identifier) @name) @definition.field

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

(class_heritage
  (identifier) @name) @reference.class

; --- imports -------------------------------------------------------------

(import_specifier
  name: (identifier) @name) @reference.import

(namespace_import
  (identifier) @name) @reference.import

(import_clause
  (identifier) @name) @reference.import
