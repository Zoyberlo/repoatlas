; Import statements for TypeScript, TSX and JavaScript.
;
; The three clause shapes are separate patterns because the grammar gives
; each its own node: named specifiers, a default binding, and a namespace
; binding. A bare side-effect import matches the last pattern alone and
; binds nothing, which still records the file dependency.
;
; Note the child order: the clause is written before `source:` because a
; pattern's children must appear in the order the tree has them, and
; `import_clause` precedes the module string. Reversed, tree-sitter rejects
; the pattern as impossible rather than simply never matching.

(import_statement
  (import_clause
    (named_imports
      (import_specifier) @binding))
  source: (string (string_fragment) @module))

(import_statement
  (import_clause
    (identifier) @binding)
  source: (string (string_fragment) @module))

(import_statement
  (import_clause
    (namespace_import
      (identifier) @binding))
  source: (string (string_fragment) @module))

(import_statement
  source: (string (string_fragment) @module)) @side_effect

; require("./x") is a module reference too.
(call_expression
  function: (identifier) @_fn
  arguments: (arguments
    (string (string_fragment) @module))
  (#eq? @_fn "require")) @side_effect
