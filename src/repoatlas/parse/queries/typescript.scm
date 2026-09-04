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

; `new User()` names the class, but what runs is its constructor. The
; resolver prefers a constructor member when the class declares one, which
; is why this is its own kind rather than a plain call.
(new_expression
  constructor: (identifier) @name) @reference.construct

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

; A member read that is not a call: `this.label`, `user.name`. The call
; pattern above captures the same token when it is being invoked, and the
; extractor keeps the more specific kind when both fire on one span.
(member_expression
  property: (property_identifier) @name) @reference.member

; --- receivers -----------------------------------------------------------

; The object a member is read from, when it is a plain name. The local's
; declared type, or the class it was constructed from, says which class the
; member belongs to.
(member_expression
  object: (identifier) @receiver
  property: (property_identifier) @name) @reference.member

(call_expression
  function: (member_expression
    object: (identifier) @receiver
    property: (property_identifier) @name)) @reference.call

; --- locals --------------------------------------------------------------

; Names bound inside a function. A bare use of one is a use of the local,
; never of a repository symbol sharing the name.
(required_parameter
  pattern: (identifier) @local)

(optional_parameter
  pattern: (identifier) @local)

(variable_declarator
  name: (identifier) @local)

; What a local's type is: an annotation on the parameter or declarator, or
; the class a `new` expression constructed.
(required_parameter
  pattern: (identifier) @var
  type: (type_annotation
    (type_identifier) @vtype)) @binding

(optional_parameter
  pattern: (identifier) @var
  type: (type_annotation
    (type_identifier) @vtype)) @binding

(variable_declarator
  name: (identifier) @var
  type: (type_annotation
    (type_identifier) @vtype)) @binding

(variable_declarator
  name: (identifier) @var
  value: (new_expression
    constructor: (identifier) @vtype)) @binding

; A value used by name: an argument, a returned name, an initialiser. These
; are the uses of a constant or a function that is passed rather than
; called, which no call or member pattern sees.
(arguments
  (identifier) @name) @reference.value

(return_statement
  (identifier) @name) @reference.value

(variable_declarator
  value: (identifier) @name) @reference.value

(template_substitution
  (identifier) @name) @reference.value
