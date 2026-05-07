# Illo Mascot Animation Assets

This folder contains first-pass vector rigs for the selected Illo mascot.

Files:

- `illo-mascot-light-rig.svg`: import-friendly light mascot rig with named groups.
- `illo-mascot-dark-rig.svg`: import-friendly dark mascot rig with the same layer structure.
- `illo-mascot-light-preview.svg`: polished SVG preview with gradients, glow, and a dark Constellation backdrop.

The import rigs are intentionally simpler than the generated 3D character sheets. They are meant to be clean animation inputs, not final rendered stills.

## Layer Map

Use these groups as animation parts:

- `body`: main squash/stretch target.
- `feet`: secondary bounce and tiny step motion.
- `eyes`: blink, look direction, curiosity/determination expression.
- `eye_left`, `eye_right`: separate eye controls.
- `orbit_back`: ring segment behind the body.
- `orbit_front`: ring segment in front of the body.
- `signal_nodes`: glowing orbit nodes.
- `node_left`, `node_top`, `node_lower`: separate node controls.
- `body_speckles`: optional subtle twinkle layer.

Each major group includes `data-pivot-x` and `data-pivot-y` attributes as suggested transform origins. Some import tools ignore these, but they are useful when rebuilding the rig.

## Rive Strategy

Recommended Rive setup:

1. Import `illo-mascot-light-rig.svg` or `illo-mascot-dark-rig.svg`.
2. Recreate pivots using the `data-pivot` values in the SVG.
3. Group `body`, `feet`, and `eyes` under a root bone or parent group.
4. Keep `orbit_back`, `orbit_front`, and `signal_nodes` as sibling layers so the ring can drift independently.
5. Rebuild glow with Rive effects or duplicated low-opacity shapes.

Good first animations:

- Idle: body moves up/down 4-6 px, feet lag 1-2 frames, orbit rotates 2-4 degrees.
- Blink: scale eye groups on Y to 8-12 percent for 2 frames, then restore.
- Listening: body tilts 3 degrees, eyes shift toward the speaker, top node pulses once.
- Working: orbit core brightens, nodes travel a few degrees along the ring, speckles twinkle.
- Completion: body squash/stretch once, ring settles, one node pings.

## Lottie Strategy

Best path for Lottie:

1. Import the SVG into Illustrator or Figma.
2. Split the named groups into separate layers.
3. Bring the layers into After Effects.
4. Convert imported vector art to shape layers where possible.
5. Export with Bodymovin.

Avoid relying on SVG filters for production Lottie. The preview file uses glow filters for presentation; rebuild those as AE shape duplicates, blur effects, or simple opacity pulses.

## Notes

For production animation, keep the mascot simple. The ring and eyes should do most of the emotional work. The body should stay calm and compact so Illo feels focused, serious, and trustworthy rather than bouncy or childish.
