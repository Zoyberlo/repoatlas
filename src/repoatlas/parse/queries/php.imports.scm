; Import statements for PHP.
;
; `use App\Models\User;` binds the last segment, or the alias when one is
; given. The whole qualified name is the module path, which for PSR-4 maps
; onto a file through composer.json.
;
; A namespace declaration is captured too: it is what a same-namespace
; lookup needs, and PHP resolves unqualified names against it.

(namespace_use_clause) @binding

(namespace_definition
  name: (namespace_name) @namespace)
