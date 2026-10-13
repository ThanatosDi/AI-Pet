# AI Pet Live2D motion format (`aipet.json`)

AI Pet shows a Live2D model as a desktop pet that reflects what Claude Code is doing.
Most VTube Studio models have no motion files (they are driven by face tracking), so the
pet animates the model by setting its **parameters** every frame. `aipet.json` describes,
for each state, which parameters move and how. It lives next to the model's `.model3.json`.

## What you are given

- `info.json`: every parameter of the model: `id`, the author's display `name` (often the
  best hint of what it does), `group`, `min`, `max`, `default`. Also the model's own motion
  groups and expressions, if any (informational only).
- `sheet.png`: for every parameter, the model rendered at that parameter's **min** and **max**
  with everything else at default. Use it to see what each parameter actually does (which
  way a head angle turns, whether a "hand" parameter raises an arm, etc.). Names can lie;
  the picture does not.
- Some parameters only show an effect while a toggle in the same group is on. VTube Studio
  hand-tracking models are the common case: `HandRightPositionX/Y` do nothing unless
  `…RightFound` / `HandRightFound` is 1 (the hand only appears when "found"). The sheet renders
  such parameters with their toggle on and says so in the label; in `aipet.json` set the toggle
  to 1 (`const`) in every state that uses the hand, and leave it at 0 where the hand should hide.

## Format

```json
{
  "version": 1,
  "request": "<the user's wishes, copied verbatim>",
  "states": {
    "<state>": {
      "params": [
        {"param": "<parameter id>", "wave": "sin", "amp": 10, "base": 0, "period": 2.0,
         "phase": 0.0, "once": 2.0}
      ],
      "body": [
        {"prop": "y", "wave": "bounce", "amp": -6, "period": 0.4}
      ],
      "blink": true
    }
  }
}
```

- `params[].param` must be an `id` from `info.json`. value = base + amp * wave(time / period + phase).
  `base` defaults to the parameter's `default`. Values are clamped to [min, max], so stay
  inside the range on purpose; going beyond it does nothing.
- Several entries for the same parameter add up (their offsets from the default are summed).
- Parameters not listed in a state ease back to their default. The pet smooths changes, so
  switching states never snaps.
- `body[]` moves the whole rendered model in the window, pivoting at the feet:
  `prop` is `x` / `y` (pixels; the model is ~140 px tall; negative y is up), `rotate`
  (degrees), `scale`, `scale_x`, `scale_y`, `opacity`. Defaults: 0 for x/y/rotate, 1 otherwise.
- `blink`: automatic blinking (every ~4 s). Omit it to blink unless the state controls an
  eye-open parameter itself; set `false` for states with eyes deliberately wide open or closed.
- Waves:
  - `sin`: -1..1 smooth.
  - `bounce`: |sin|, 0..1..0 once per period (with negative amp on body `y` = hopping).
  - `blink`: 1 for the first 5% of each period, else 0.
  - `shake`: fast vibration bursts in the first quarter of each period.
  - `spin`: 0..1 ramp.
  - `const`: just `base` (a held pose, e.g. raised hand).
- `once`: play only for this many seconds from the state start (time restarts at the state
  start), then rest at `base`. Use a whole multiple of `period` so it ends at rest.
- Define ALL seven states: idle, thinking, working, waiting, done, compacting, sleeping.

## Good animation practice

- Combine head angle, body angle and eyes for each state rather than moving one parameter.
  Body angles moving a little with the head looks natural; physics (hair, ears) follows on
  its own when the head and body move.
- Typical ranges: head angles ±10–20, body angles ±3–8, eyeballs ±0.3–0.8. Use small
  amplitudes for idle, bigger and faster ones for working / waiting.
- Use different speeds for different parameters (e.g. head 0.6 Hz, body 0.4 Hz) so the motion
  does not look mechanical.
- Keep it readable at ~140 px and not seizure-inducing (no flashing, no frantic full-range
  shaking for long periods).

## Default behavior per state

The user's wishes override these; any state the user did not mention uses its default.

- idle: gentle head sway, slight body sway, blinking.
- thinking: looks up, eyes up, slow side-to-side head turn, brows slightly raised if available.
- working: looks down, quick small head and body motion; if there are hand parameters, move
  the hands alternately like typing.
- waiting (needs the user's approval): eyes wide open (blink false), brows up, mouth open if
  available, one hand raised and waving if possible, body shakes in bursts.
- done: happy face (eye smile / mouth smile parameters if available), a few cheerful sways or
  hops that settle (use `once`).
- compacting: head down, eyes half closed, body squashed (body scale_y down, scale_x up).
- sleeping: eyes closed (blink false), head tilted and drooping, very slow breathing.
