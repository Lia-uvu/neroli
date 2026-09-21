# NEROLI DESIGN HISTORY

These pages are translated, privacy-edited editions of Neroli's original design
documents. The private originals contain real conversational material and are
intentionally not published. The public editions retain the original presentation of
the architectural reasoning, experiments, measurements, rejected alternatives, and
decision sequence while removing or replacing personal examples.

The documents are historical context, not specifications for the current system:

1. [v1: Event memory and vector-space clustering](design-v1.html) preserves the
   original Card model, rolling freeze/refeed generation, temporal projections,
   viewer-specific memory, experiments, and rejected clustering paths.
2. [v2: Leiden graph index and Midlayer](design-leiden-v2.html) preserves the shift
   from vector thresholds to a rebuildable graph index, including entity resolution,
   recursive community detection, operational results, and the first Midlayer design.

The sequence matters. Rejected approaches remain here because their failure modes
explain several otherwise non-obvious properties of the current architecture.

For implemented behavior, use the [architecture](../ARCHITECTURE.md),
[schema](../schema.md), code, and tests.
