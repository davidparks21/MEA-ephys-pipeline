# Upstream capsule

Vendored runtime code from
https://github.com/AllenNeuralDynamics/aind-ephys-visualization at
`0f11e62f4811c5d1876736351362db811ef55def`, the existing `VISUALIZATION` pin.
The upstream MIT license is retained; the interactive notebook is not runtime code.

Local change: use SpikeInterface 0.103's public peak detection/localization APIs
for drift plots when a recording has no spike-location extension or motion data.
The original imports referenced removed internal classes, crashing visualization
of zero-unit analyzers. Detection and center-of-mass settings are unchanged.
Zero detected peaks skip the drift map; trace plots still run. Failures in this
optional drift fallback now log their cause. The sorted-spike and motion-data
paths are unchanged.

Classifier probabilities are converted explicitly to a numeric array before
rounding. Empty CSV columns otherwise cause NumPy to return an array where the
original code assumes a pandas Series and accesses `.values`. Nonempty numeric
values and rounding precision are unchanged.
