# Frontend Design System

This directory is the stable import target for design-system code as the frontend moves away from legacy page-local presentation.

`constellation/` currently re-exports the live Constellation primitives from `frontend/src/lib/components/constellation`. The physical move should happen only after feature extraction has reduced import churn.
