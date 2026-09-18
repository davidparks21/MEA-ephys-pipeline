# Upstream capsule

Vendored from https://github.com/Varda006/aind-ecephys-nwb at `6bfcc77ae2f3ef504b487d003f7c4a20639dfbc4`.
Only runtime code is included; the upstream license is retained.

Local changes:

- Only convert unsigned integer recordings before filtering. Signed and
  floating-point inputs retain their original representation.
- Preserve the final fractional second of LFP data with the pinned resampler.
- Select LFP channels after filtering and binary materialization. Reading
  contiguous channels avoids large HDF5 selections for compressed NWB input;
  the per-channel filters and selected electrode IDs are unchanged. The
  temporary LFP binary contains all channels before the final selection.
