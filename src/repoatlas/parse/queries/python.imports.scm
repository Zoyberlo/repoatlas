; Import statements for Python.
;
; Separate from the tag query because an import binds names *and* names a
; module, and the tag query's one-name-per-match shape cannot carry both.
; Captures mark where imports are; the extractor reads the node's fields for
; the small structural detail of alias versus original name.

; from a.b import C as D, E
(import_from_statement
  module_name: (dotted_name) @module
  name: (_) @binding)

; from . import sib   /   from ..pkg import Deep
(import_from_statement
  module_name: (relative_import) @module
  name: (_) @binding)

; import os.path   /   import numpy as np
; No module capture: the bound name *is* the module path.
(import_statement
  name: (_) @binding) @self_module
