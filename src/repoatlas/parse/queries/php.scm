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

; PHP 8 promotes a constructor parameter to a property in one stroke:
; `__construct(private string $prefix)`. It is a field of the class, and
; the extractor hoists it there.
(property_promotion_parameter
  name: (variable_name
    (name) @name)) @definition.attribute

(namespace_definition
  name: (namespace_name) @name) @definition.module

; `new class(...) { ... }` has no name; the keyword stands in for it and
; the extractor calls the type `class@anonymous`, as PHP does. It is a type
; like any other: its members belong to it, and `$this` inside them is it.
(anonymous_class
  "class" @name) @definition.class

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

; `new User()` names the class; what runs is its constructor.
(object_creation_expression
  (name) @name) @reference.construct

(object_creation_expression
  (qualified_name
    (name) @name)) @reference.construct

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

; A property read that is not a call: `$this->email`.
(member_access_expression
  name: (name) @name) @reference.member

; --- receivers -----------------------------------------------------------

; The variable a member is reached through. `$this` the resolver already
; knows; for any other variable the declared parameter type, or the class
; it was constructed from, says which class holds the member.
(member_call_expression
  object: (variable_name
    (name) @receiver)
  name: (name) @name) @reference.call

(member_access_expression
  object: (variable_name
    (name) @receiver)
  name: (name) @name) @reference.member

; A static call names its class outright, and that is the receiver.
(scoped_call_expression
  scope: (name) @receiver
  name: (name) @name) @reference.call

(simple_parameter
  type: (named_type
    (name) @vtype)
  name: (variable_name
    (name) @var)) @binding

(simple_parameter
  type: (optional_type
    (named_type
      (name) @vtype))
  name: (variable_name
    (name) @var)) @binding

(property_promotion_parameter
  type: (named_type
    (name) @vtype)
  name: (variable_name
    (name) @var)) @binding

(assignment_expression
  left: (variable_name
    (name) @var)
  right: (object_creation_expression
    (name) @vtype)) @binding

; --- scoped access -------------------------------------------------------

; `Greeter::DEFAULT_PREFIX`, `Util::helper()`, `Config::$instance`: the
; scope names a type and the name after `::` is a member of it. It is a
; type reference, not a `class` one: `class` is reserved for base clauses,
; because the resolver reads those as what the enclosing type extends.
; The grammar gives the constant form no field names, so anchors pick the
; first and last children apart.
(class_constant_access_expression
  . (name) @name) @reference.type

(class_constant_access_expression
  . (qualified_name
    (name) @name .)) @reference.type

(class_constant_access_expression
  (name) @name .) @reference.member

(scoped_call_expression
  scope: (name) @name) @reference.type

(scoped_call_expression
  scope: (qualified_name
    (name) @name)) @reference.type

(scoped_property_access_expression
  scope: (name) @name) @reference.type

; `$x instanceof Ad` names a type as well.
(binary_expression
  "instanceof"
  right: (name) @name) @reference.type

; `self::`, `static::` and `parent::` name the enclosing class or its base
; without spelling either. The resolver knows which class it is in; the
; query only has to say that a scope was written.
(class_constant_access_expression
  (relative_scope) @name) @reference.scope

(scoped_call_expression
  scope: (relative_scope) @name) @reference.scope

(scoped_property_access_expression
  scope: (relative_scope) @name) @reference.scope

; --- types ---------------------------------------------------------------

; A declared type, wherever it stands: a parameter, a return, a property,
; a catch. Primitives are a different node and are not references.
(named_type
  (name) @name) @reference.type

(named_type
  (qualified_name
    (name) @name)) @reference.type

; A constant used by name inside a call.
(arguments
  (argument
    (name) @name)) @reference.value

; `view('users.index')` names a Blade template, and `route('users.show')`
; names a route definition. Neither is a symbol any parser can see: both are
; strings that mean a file only under a framework's conventions, which is
; why they carry their own reference kinds for a plugin to resolve.
(function_call_expression
  function: (name) @_fn
  arguments: (arguments
    (argument
      (string) @name))
  (#eq? @_fn "view")) @reference.view

(function_call_expression
  function: (name) @_fn
  arguments: (arguments
    (argument
      (string) @name))
  (#eq? @_fn "route")) @reference.route

; --- receivers through properties -----------------------------------------

; `$this->service->handle()`: the receiver is a property of the enclosing
; class, and its declared type says where `handle` lives.
(member_call_expression
  object: (member_access_expression
    object: (variable_name
      (name) @_this)
    name: (name) @receiver_field)
  name: (name) @name
  (#eq? @_this "this")) @reference.call

(member_access_expression
  object: (member_access_expression
    object: (variable_name
      (name) @_this)
    name: (name) @receiver_field)
  name: (name) @name
  (#eq? @_this "this")) @reference.member

; A typed property binds `this.name` for the whole class.
(property_declaration
  type: (named_type
    (name) @vtype)
  (property_element
    name: (variable_name
      (name) @var))) @binding

(property_declaration
  type: (optional_type
    (named_type
      (name) @vtype))
  (property_element
    name: (variable_name
      (name) @var))) @binding

; `self::make()`, `static::make()`, `parent::make()`: the receiver is the
; enclosing class or what it extends.
(scoped_call_expression
  scope: (relative_scope) @receiver
  name: (name) @name) @reference.call

; A member of an expression, `make()->run()` or `$a[0]->run()`: nothing
; names the receiver, and the resolver must not guess one.
(member_call_expression
  object: (_) @chained
  name: (name) @name) @reference.call

; --- receivers of constants and static properties ---------------------------

; `Greeter::DEFAULT_PREFIX`, `self::DEFAULT_PREFIX`, `Config::$instance`:
; the scope is the receiver of the member after `::`. `Foo::class` names
; no member and is left out.
(class_constant_access_expression
  . (name) @receiver
  (name) @name .
  (#not-eq? @name "class")) @reference.member

(class_constant_access_expression
  . (relative_scope) @receiver
  (name) @name .
  (#not-eq? @name "class")) @reference.member

(class_constant_access_expression
  . (qualified_name
    (name) @receiver .)
  (name) @name .
  (#not-eq? @name "class")) @reference.member

(scoped_property_access_expression
  scope: (name) @receiver
  name: (variable_name
    (name) @name)) @reference.member

(scoped_property_access_expression
  scope: (relative_scope) @receiver
  name: (variable_name
    (name) @name)) @reference.member

(scoped_call_expression
  scope: (qualified_name
    (name) @receiver .)
  name: (name) @name) @reference.call

; --- types by call ---------------------------------------------------------

; `$greeter = $this->build()`: the local is whatever `build()` returns,
; which its signature says. The resolver reads it there.
(assignment_expression
  left: (variable_name
    (name) @var)
  right: (function_call_expression
    function: (name) @vcall)) @binding

(assignment_expression
  left: (variable_name
    (name) @var)
  right: (member_call_expression
    object: (variable_name
      (name) @vcall_receiver)
    name: (name) @vcall)) @binding

(assignment_expression
  left: (variable_name
    (name) @var)
  right: (scoped_call_expression
    scope: (name) @vcall_receiver
    name: (name) @vcall)) @binding

(assignment_expression
  left: (variable_name
    (name) @var)
  right: (object_creation_expression
    (qualified_name
      (name) @vtype .))) @binding

; `$x instanceof \App\Models\Schedule`
(binary_expression
  "instanceof"
  right: (qualified_name
    (name) @name)) @reference.type
