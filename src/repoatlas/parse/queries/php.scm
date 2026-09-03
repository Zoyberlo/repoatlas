; Tag query for PHP.
;
; Uses the `php` grammar rather than `php_only`, because a `.php` file may
; open with HTML and switch in and out of `<?php`. Blade templates will need
; `php_only` injected into their directives, which is a separate concern.

; --- definitions ---------------------------------------------------------

(class_declaration
  name: (name) @name) @definition.class

(interface_declaration
  name: (name) @name) @definition.interface

(trait_declaration
  name: (name) @name) @definition.trait

(enum_declaration
  name: (name) @name) @definition.enum

(enum_case
  name: (name) @name) @definition.constant

(function_definition
  name: (name) @name) @definition.function

(method_declaration
  name: (name) @name) @definition.method

(property_declaration
  (property_element
    (variable_name
      (name) @name))) @definition.field

(const_declaration
  (const_element
    (name) @name)) @definition.constant

(namespace_definition
  name: (namespace_name) @name) @definition.module

; --- references ----------------------------------------------------------

(function_call_expression
  function: (name) @name) @reference.call

(function_call_expression
  function: (qualified_name
    (name) @name)) @reference.call

(member_call_expression
  name: (name) @name) @reference.call

(scoped_call_expression
  name: (name) @name) @reference.call

(object_creation_expression
  (name) @name) @reference.call

(object_creation_expression
  (qualified_name
    (name) @name)) @reference.call

(base_clause
  (name) @name) @reference.class

(base_clause
  (qualified_name
    (name) @name)) @reference.class

(class_interface_clause
  (name) @name) @reference.class

(class_interface_clause
  (qualified_name
    (name) @name)) @reference.class

; `use HasName;` inside a class body pulls in a trait, which is closer to
; inheritance than to an import.
(use_declaration
  (name) @name) @reference.class

(use_declaration
  (qualified_name
    (name) @name)) @reference.class

; --- imports -------------------------------------------------------------

; `use App\Contracts\Greeter as G;` puts the imported name inside a
; qualified_name and the alias beside it as a bare name, with no dedicated
; aliasing node. Both patterns below are wanted: the first records what was
; imported, the second the local name it was bound to.
(namespace_use_clause
  (qualified_name
    (name) @name)) @reference.import

(namespace_use_clause
  (name) @name) @reference.import
