; Tag query for Python.
;
; Capture convention, shared by every language in this directory:
;   @definition.<kind>  the whole construct, which becomes full_range
;   @name               the identifier alone, which becomes name_range
;   @reference.<kind>   a use site
;
; Two definitions may share a @name node when one pattern is more specific
; than another. The extractor keeps the more specific kind rather than the
; first match, because query results arrive in tree order, not pattern order.

; --- definitions ---------------------------------------------------------

(class_definition
  name: (identifier) @name) @definition.class

(function_definition
  name: (identifier) @name) @definition.function

; A module-level binding. Python has no const, so the distinction between a
; constant and a variable is a naming convention the extractor applies.
; Note that an assignment sits directly under the module here: this grammar
; has no expression_statement wrapper.
(module
  (assignment
    left: (identifier) @name) @definition.constant)

; Class-level bindings are fields whether or not they carry an annotation.
(class_definition
  body: (block
    (assignment
      left: (identifier) @name) @definition.field))

; An instance attribute is defined by its first assignment through `self`,
; wherever in the class that happens. The extractor hoists it out of the
; method into the class, since that is where every reader and every oracle
; puts it, and treats later assignments as uses rather than redefinitions.
(assignment
  left: (attribute
    object: (identifier) @_self
    attribute: (identifier) @name)
  (#eq? @_self "self")) @definition.attribute

; --- references ----------------------------------------------------------

(call
  function: (identifier) @name) @reference.call

(call
  function: (attribute
    attribute: (identifier) @name)) @reference.call

; A bare decorator is a use of the decorating function. A decorator that
; is itself a call, `@dec()`, is already covered by the call pattern
; above and is not repeated here.
(decorator
  (identifier) @name) @reference.call

; Base classes. `class A(B)` parses the base as a plain identifier in an
; argument list, the same shape as a call argument, so the superclasses
; field is what distinguishes it.
(class_definition
  superclasses: (argument_list
    (identifier) @name)) @reference.class

(class_definition
  superclasses: (argument_list
    (attribute
      attribute: (identifier) @name))) @reference.class

; --- imports -------------------------------------------------------------

(import_statement
  name: (dotted_name
    (identifier) @name)) @reference.import

(import_from_statement
  name: (dotted_name
    (identifier) @name)) @reference.import

(aliased_import
  alias: (identifier) @name) @reference.import

; An attribute read that is not a call: `self.value`, `config.debug`.
(attribute
  attribute: (identifier) @name) @reference.member

; A value used by name: an argument, a returned name, a default. These are
; the uses of a constant or a function passed rather than called.
(argument_list
  (identifier) @name) @reference.value

(return_statement
  (identifier) @name) @reference.value

(default_parameter
  value: (identifier) @name) @reference.value

(typed_default_parameter
  value: (identifier) @name) @reference.value

; --- receivers -----------------------------------------------------------

; The object a member is read from, when it is a plain name. `greeter.greet`
; tells the resolver which local the call goes through, and the local's
; declared type says which class answers.
(attribute
  object: (identifier) @receiver
  attribute: (identifier) @name) @reference.member

(call
  function: (attribute
    object: (identifier) @receiver
    attribute: (identifier) @name)) @reference.call

; --- locals --------------------------------------------------------------

; Names bound inside a function: parameters and assignments. A bare use of
; one of these is a use of the local, never of a repository symbol that
; happens to share the name, so the extractor drops those before the
; resolver can be tempted.
(parameters
  (identifier) @local)

(typed_parameter
  (identifier) @local)

(default_parameter
  name: (identifier) @local)

(typed_default_parameter
  name: (identifier) @local)

(function_definition
  body: (block
    (assignment
      left: (identifier) @local)))

; A parameter with an annotation binds a name to a type. What the resolver
; needs from `greeter: Greeter` is that `greeter.greet` goes to `Greeter`.
(typed_parameter
  (identifier) @var
  type: (type
    (identifier) @vtype)) @binding

(typed_default_parameter
  name: (identifier) @var
  type: (type
    (identifier) @vtype)) @binding

; --- types ---------------------------------------------------------------

; An annotation names a type: a parameter's, a return's, an attribute's.
; The `type` node wraps the expression, so one pattern reaches all three;
; a union and a generic keep their names one level further in.
(type
  (identifier) @name) @reference.type

(type
  (binary_operator
    (identifier) @name)) @reference.type

(type
  (attribute
    attribute: (identifier) @name)) @reference.type

; --- chained calls ----------------------------------------------------------

; A member of an expression, `make().run()`: nothing names the receiver.
(call
  function: (attribute
    object: (_) @chained
    attribute: (identifier) @name)) @reference.call

; --- types by call ---------------------------------------------------------

; `greeter = build()` is whatever `build` returns, and `admin = Admin()`
; is an Admin: Python constructs by calling, and the resolver tells the
; two apart by what the callee turns out to be.
(assignment
  left: (identifier) @var
  right: (call
    function: (identifier) @vcall)) @binding

(assignment
  left: (identifier) @var
  right: (call
    function: (attribute
      object: (identifier) @vcall_receiver
      attribute: (identifier) @vcall))) @binding

; --- what `self` holds -----------------------------------------------------------

; `self.client = client` in `__init__`: the attribute takes the
; parameter's type.
(assignment
  left: (attribute
    object: (identifier) @_self
    attribute: (identifier) @var)
  right: (identifier) @src
  (#eq? @_self "self")) @this_binding

; `self.client.fetch()`: a member of an attribute of the instance, typed by
; what `__init__` assigned to it.
(call
  function: (attribute
    object: (attribute
      object: (identifier) @_self
      attribute: (identifier) @receiver_field)
    attribute: (identifier) @name)
  (#eq? @_self "self")) @reference.call

(attribute
  object: (attribute
    object: (identifier) @_self
    attribute: (identifier) @receiver_field)
  attribute: (identifier) @name
  (#eq? @_self "self")) @reference.member
