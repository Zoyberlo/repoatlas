; Tag query for Vue single-file components.
;
; The script block is not covered here. It is TypeScript or JavaScript, and
; it is parsed by that language's own query through the embedded-region
; machinery, so everything this file needs to describe is the template.
;
; A component used in a template is a reference to a definition somewhere
; else, usually an import in the script block beside it. Vue accepts both
; `<MyButton>` and `<my-button>` for the same component, which the extractor
; normalises.

(element
  (start_tag
    (tag_name) @name)) @reference.component

(element
  (self_closing_tag
    (tag_name) @name)) @reference.component

(template_element
  (start_tag
    (tag_name) @name)) @reference.component
