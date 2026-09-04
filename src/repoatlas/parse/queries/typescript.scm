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

; Functions and variables are symbols only at module level. A `const`
; inside a function is that function's business: a compiler-backed index
; files it as a local, an agent never navigates to it, and a real front end
; had more of them than it had exported names. They are captured as locals
; further down, so that uses of them shadow any symbol sharing the name.
(program
  (function_declaration
    name: (identifier) @name) @definition.function)

(export_statement
  declaration: (function_declaration
    name: (identifier) @name) @definition.function)

(program
  (generator_function_declaration
    name: (identifier) @name) @definition.function)

(export_statement
  declaration: (generator_function_declaration
    name: (identifier) @name) @definition.function)

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
(program
  (lexical_declaration
    (variable_declarator
      name: (identifier) @name
      value: [(arrow_function) (function_expression)]) @definition.function))

(export_statement
  declaration: (lexical_declaration
    (variable_declarator
      name: (identifier) @name
      value: [(arrow_function) (function_expression)]) @definition.function))

(program
  (lexical_declaration
    (variable_declarator
      name: (identifier) @name) @definition.constant))

(export_statement
  declaration: (lexical_declaration
    (variable_declarator
      name: (identifier) @name) @definition.constant))

(program
  (variable_declaration
    (variable_declarator
      name: (identifier) @name) @definition.constant))

(export_statement
  declaration: (variable_declaration
    (variable_declarator
      name: (identifier) @name) @definition.constant))

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

; Names bound inside a function: parameters, declarations, catch bindings,
; destructured names, and functions declared inside functions. A bare use
; of one is a use of the local, never of a repository symbol sharing the
; name. The extractor keys each on the function that binds it, so a local
; declared inside an anonymous callback shadows there too.
(required_parameter
  pattern: (identifier) @local)

(optional_parameter
  pattern: (identifier) @local)

(arrow_function
  parameter: (identifier) @local)

(variable_declarator
  name: (identifier) @local)

(catch_clause
  parameter: (identifier) @local)

(for_in_statement
  left: (identifier) @local)

(object_pattern
  (shorthand_property_identifier_pattern) @local)

(pair_pattern
  value: (identifier) @local)

(array_pattern
  (identifier) @local)

(rest_pattern
  (identifier) @local)

(assignment_pattern
  left: (identifier) @local)

(function_declaration
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

; --- values --------------------------------------------------------------

; Every bare identifier is a use of something. The ones that are not, the
; binding sites, are removed by the extractor: definitions by their span,
; parameters and locals by the captures above. Property names have their
; own node type and never match here. Listing the contexts one by one, as
; this query once did, missed `export default x`, `export { x }`, `a = x`,
; `{ x }` and every operator expression, and a real front end showed it.
(identifier) @name @reference.value

(shorthand_property_identifier) @name @reference.value

; --- receivers through this -----------------------------------------------

(member_expression
  object: (this) @receiver
  property: (property_identifier) @name) @reference.member

(call_expression
  function: (member_expression
    object: (this) @receiver
    property: (property_identifier) @name)) @reference.call

(member_expression
  object: (super) @receiver
  property: (property_identifier) @name) @reference.member

(call_expression
  function: (member_expression
    object: (super) @receiver
    property: (property_identifier) @name)) @reference.call

; `this.service.handle()`: a property of the enclosing class, typed by its
; declaration or by the constructor parameter that promoted it.
(member_expression
  object: (member_expression
    object: (this)
    property: (property_identifier) @receiver_field)
  property: (property_identifier) @name) @reference.member

(call_expression
  function: (member_expression
    object: (member_expression
      object: (this)
      property: (property_identifier) @receiver_field)
    property: (property_identifier) @name)) @reference.call

(public_field_definition
  name: (property_identifier) @var
  type: (type_annotation
    (type_identifier) @vtype)) @binding

; A member of an expression: nothing names the receiver.
(call_expression
  function: (member_expression
    object: (_) @chained
    property: (property_identifier) @name)) @reference.call

; --- types by call ---------------------------------------------------------

; `const user = makeUser()`, `const data = await this.api.load()`: the
; local is whatever the call returns, read off the callee's signature.
(variable_declarator
  name: (identifier) @var
  value: (call_expression
    function: (identifier) @vcall)) @binding

(variable_declarator
  name: (identifier) @var
  value: (call_expression
    function: (member_expression
      object: [(identifier) (this)] @vcall_receiver
      property: (property_identifier) @vcall))) @binding

(variable_declarator
  name: (identifier) @var
  value: (await_expression
    (call_expression
      function: (identifier) @vcall))) @binding

(variable_declarator
  name: (identifier) @var
  value: (await_expression
    (call_expression
      function: (member_expression
        object: [(identifier) (this)] @vcall_receiver
        property: (property_identifier) @vcall)))) @binding
