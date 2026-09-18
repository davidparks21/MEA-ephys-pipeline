# Upstream capsule

Vendored runtime code from
https://github.com/AllenNeuralDynamics/aind-ephys-postprocessing at
`2ebaa8180ba33a54a8378b9dd56b5147a535b5a8`, the existing `POSTPROCESSING` pin.
The upstream MIT license is retained.

Local change: retain a valid zero-unit analyzer when detection finds no spikes.
Select all zero spikes instead of random-sampling an empty unit list, and omit
spike-amplitude/location and PCA nodes that require observations. Retain the full
recording reference, empty per-unit outputs and explicit status metadata, listing
quality metrics that are not applicable without spikes.
Nonempty sorting analysis and scientific settings are unchanged.
