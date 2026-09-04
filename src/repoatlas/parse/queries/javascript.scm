; Tag query for JavaScript and JSX.
;
; A trimmed TypeScript query: same shapes, minus every node the JS grammar
; does not have. Keeping it separate rather than sharing typescript.scm is
; not duplication for its own sake, since a query that names a node type the
; grammar lacks fails to compile outright.

; --- definitions ---------------------------------------------------------

(class_declaration
  name: (identifier) @name) @definition.class

; Functions and variables are symbols only at module level; see the
; TypeScript query for why. Inside a function they are locals, captured
; further down.
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

(field_definition
  property: (property_identifier) @name) @definition.field

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

; `new User()` names the class; what runs is its constructor.
(new_expression
  constructor: (identifier) @name) @reference.construct

(class_heritage
  (identifier) @name) @reference.class

; --- imports -------------------------------------------------------------

(import_specifier
  name: (identifier) @name) @reference.import

(namespace_import
  (identifier) @name) @reference.import

(import_clause
  (identifier) @name) @reference.import

; A member read that is not a call: `this.label`, `user.name`.
(member_expression
  property: (property_identifier) @name) @reference.member

; --- receivers -----------------------------------------------------------

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
(formal_parameters
  (identifier) @local)

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

; JavaScript has no annotations; what a local is comes from what it was
; constructed as.
(variable_declarator
  name: (identifier) @var
  value: (new_expression
    constructor: (identifier) @vtype)) @binding

; --- values --------------------------------------------------------------

; Every bare identifier is a use of something; binding sites are removed by
; the extractor. See the TypeScript query.
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
