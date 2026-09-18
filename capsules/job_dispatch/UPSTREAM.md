# Upstream capsule

Vendored runtime code from
https://github.com/AllenNeuralDynamics/aind-ephys-job-dispatch at
`bc73404c574d04d456684fd0abad5373a257d4fe`, the existing `JOB_DISPATCH` pin.
The upstream MIT license is retained.

Local changes: recognize Neo recording extractors by their base class, then
select each enumerated stream and block explicitly. The original membership
test compared a class against a dictionary's string keys, so multiwell Maxwell
input fell through to a single unspecified stream and failed. Preserve supplied
probe annotations on the returned recording and in generic-reader job JSON, so
combined NWB electrode and unit exports retain well identity. SpikeInterface
0.103.2's `set_probegroup` annotates the original recording rather than its
returned clone. The NWB-specific loader and default parameters are unchanged.
