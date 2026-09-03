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

; A value used by name: an argument, a returned name. These are the uses of
; a constant or a function passed rather than called.
(argument_list
  (identifier) @name) @reference.value

(return_statement
  (identifier) @name) @reference.value
