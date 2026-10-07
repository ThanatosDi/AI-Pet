# AI Pet mascot format

An AI Pet mascot is a folder with two files that the app renders and animates:
`mascot.svg` (the drawing, split into parts) and `rig.json` (how parts move in each state).
The app only reads these data files; it never executes code.

## 1. `mascot.svg`

A clean, cute vector redraw of the mascot that stays faithful to the source picture
(shapes, colors, characteristic features).

- Must have a `viewBox`, roughly square (e.g. `0 0 200 200`), transparent background,
  the character standing on / touching the bottom edge and filling most of the width.
- Split the character into separately animatable parts. Every part is a TOP-LEVEL
  `<g id="...">` directly under `<svg>` (no nesting of parts), listed in drawing order
  (back to front). Typical parts: body, head, arm_left, arm_right, leg_left, leg_right,
  tail, eyes_open, eyes_closed, mouth, mouth_open. Adapt to the actual character.
- Provide alternative expression parts (e.g. eyes_closed for sleeping, a happy face for
  done, a surprised mouth for waiting) as separate groups that are hidden by default.
- Renderer is QtSvg (SVG Tiny 1.2 subset): use only path, rect, circle, ellipse, line,
  polyline, polygon, g, linearGradient, radialGradient, and presentation attributes
  (fill, stroke, stroke-width, opacity...). Do NOT use `<style>`/CSS classes, filter, mask,
  clipPath, pattern, `<text>`, `<image>`, `<use>`, or transform attributes on the
  top-level part groups.

## 2. `rig.json`

Use ONLY this schema:

```json
{
  "version": 1,
  "parts": ["<every top-level group id, in drawing order>"],
  "hidden": ["<parts hidden unless a state shows them>"],
  "states": {
    "<state>": {
      "show": ["<part ids to show in this state>"],
      "hide": ["<part ids to hide in this state>"],
      "anims": [
        {"part": "all or <part id>",
         "prop": "x | y | rotate | scale | scale_x | scale_y | opacity",
         "wave": "const | sin | bounce | blink | shake | spin",
         "amp": 0, "base": 0, "period": 1.0, "phase": 0.0,
         "origin": [100, 200], "once": 2.0}
      ]
    }
  }
}
```

- value = base + amp * wave(time / period + phase). `base` defaults to 0 for x/y/rotate
  and 1 for scale*/opacity. x/y are in viewBox units, rotate in degrees.
- Waves:
  - `sin`: -1..1 smooth.
  - `bounce`: |sin|, 0..1..0 once per period (use negative amp on y to jump up).
  - `blink`: 1 for the first 5% of each period, else 0 (e.g. eyes scale_y base 1,
    amp -0.9, period 4 → blink every 4 s).
  - `shake`: fast vibration bursts in the first quarter of each period.
  - `spin`: 0..1 ramp (rotate amp 360 = full turn).
  - `const`: just base (static pose, e.g. raised arm: rotate base -120).
- `origin` = pivot for rotate/scale in viewBox coordinates (shoulder for arms, bottom
  center of the character for "all" squash/breathing). Defaults: the part's bounding-box
  center, or the bottom center of the viewBox for "all". Each anim uses its own origin.
- `once` = play only for this many seconds from the state start (its time restarts at
  the state start), then rest at `base`. Use a whole multiple of `period` so it stops at
  the starting pose (e.g. spin period 1, once 1 = exactly one turn; bounce period 0.4,
  once 1.2 = three jumps). Good for one-shot reactions in "done".
- Several anims on the same part combine (x/y first, then rotate/scale, then opacity).
- Define ALL seven states: idle, thinking, working, waiting, done, compacting, sleeping.
  Keep motions readable at ~150 px size and not seizure-inducing.

## Default behavior per state

The user's wishes override these; any state the user did not mention uses its default.

- idle: slow breathing (whole-body scale_y gently rising and falling), blink every few seconds
- thinking: body sways left/right, eyes look up or head tilts slightly
- working: quick up-and-down bouncing, arms/legs moving alternately
- waiting (needs the user's approval): one arm raised and waving, the whole body shakes in bursts, surprised face
- done: happily jumps a few times then settles (use `once`), happy face
- compacting: whole body squashed flat and springing back (scale_y down, scale_x up)
- sleeping: eyes closed, very slow breathing
