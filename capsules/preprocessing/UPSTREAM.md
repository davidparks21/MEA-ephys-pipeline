# Upstream capsule

Vendored from https://github.com/Varda006/aind-ephys-preprocessing at `827ee9b018247325e93f375ffe166c59dd64a19d`.
Only runtime code is included; the upstream license is retained.

Local change: only convert unsigned integer recordings before filtering.
Signed and floating-point inputs must retain their original representation.
