# Upstream capsule

Vendored runtime code from
https://github.com/AllenNeuralDynamics/aind-units-nwb at
`94e2eefda3480c4d15cc11a78ea0942bc6ee8757`, the existing `NWB_UNITS` pin.
The upstream MIT license is retained.

Local change: do not append unit rows for an empty sorting. The pinned NeuroConv
writer infers property types from `data[0]` and cannot accept zero units. The base
NWB's electrodes and LFP remain intact, as do units already added from other
streams. Nonempty unit export is unchanged. Per-recording detector status and
original logs remain in sorting/analyzer/report artifacts.
