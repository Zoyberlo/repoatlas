; Tag query for Blade templates.
;
; A Blade file defines nothing that other files import; it is named by them.
; What it carries instead is the relationships no generic parser can see,
; and all of them are strings: a layout it extends, a partial it includes,
; a component it renders.
;
; The directive decides what the string beside it means, so each one gets
; its own pattern and its own reference kind. Pairing them in the query is
; what lets the extractor stay ignorant of Blade: it sees a reference of
; kind `extends`, and the Laravel plugin knows that means a view name.

((directive) @_directive
  .
  (parameter) @name
  (#eq? @_directive "@extends")) @reference.extends

((directive) @_directive
  .
  (parameter) @name
  (#eq? @_directive "@include")) @reference.include

((directive) @_directive
  .
  (parameter) @name
  (#eq? @_directive "@includeIf")) @reference.include

((directive) @_directive
  .
  (parameter) @name
  (#eq? @_directive "@each")) @reference.include

((directive) @_directive
  .
  (parameter) @name
  (#eq? @_directive "@component")) @reference.include

((directive) @_directive
  .
  (parameter) @name
  (#eq? @_directive "@livewire")) @reference.component

; `<x-alert />` and `<x-forms.input>` name a component class or view. The
; `x-` prefix is Laravel's, and the extractor keeps it: stripping it here
; would lose the one signal that says which convention resolves the name.
(self_closing_tag
  (tag_name) @name) @reference.component

(start_tag
  (tag_name) @name) @reference.component
