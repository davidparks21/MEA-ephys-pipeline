# Upstream capsule

Vendored runtime code from
https://github.com/AllenNeuralDynamics/aind-ephys-spikesort-kilosort4 at
`dbe0c9683433f0d10057b9db800c62654ee1abf5`, the existing `SPIKESORT_KS4` pin.
The upstream MIT license is retained.

Local change: recognize Kilosort's explicit zero-spike detector outcome and save
a loadable zero-unit sorting, status metadata and the original detector log.
Both the exact terminal exception and zero-detection log evidence are required;
other errors retain the original failure behavior. Detection thresholds and
nonempty sorting behavior are unchanged.
