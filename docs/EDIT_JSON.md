# edit.json — declarative multi-segment timeline

`apply_timeline(edit_json, output)` renders a JSON timeline in one FFmpeg
pass (or builds the same events on VEGAS / Resolve). The CLI is
`rushcut apply edit.json --output out.mp4`. The schema file is
`server/rushcut/edit/schema.json`. Validation errors name the bad field
(`segments[1].out: must be greater than in`).

```json
{
  "schema": "rushcut.edit/v1",
  "output": { "preset": "vertical", "fps": 30 },
  "join_fade_ms": 8,
  "segments": [
    {
      "source": "gameplay.mp4",
      "in": 12.0,
      "out": 16.4,
      "audio_source": "gameplay.mp4",
      "audio_offset": 1.2,
      "effects": [
        { "type": "zoom", "scale": 1.45, "center": [0.52, 0.4], "t_in": 0.2, "t_out": 1.8, "ease": 0.15 }
      ]
    }
  ]
}
```

## Fields

| Field | Role |
|---|---|
| `schema` | `rushcut.edit/v1` |
| `output.preset` | `vertical` (1080×1920), `1080p`, `4k` |
| `output.width` / `height` / `fps` | override the preset |
| `join_fade_ms` | 2–20 ms micro-fades on each segment edge so jump cuts do not click (default 8) |
| `segments[].source` | media path |
| `segments[].in` / `out` | source-file times, seconds |
| `segments[].audio_source` / `audio_offset` | J/L cut: take audio from another file, or another time in the same file |
| `segments[].effects` | `zoom`, `speed_ramp`, `lens_zoom`, `compose_layout`, `blur_regions`, `overlay` |
| `effects` | global effects after concat |

Times on effects are seconds from the start of that trimmed segment.

## Frame-rate conform

Each segment is conformed with an `fps=` filter, not output `-r`. Converting
60 fps → 30 fps with `-r 30` alone duplicates frames and leaves picture behind
audio; the `fps` filter holds A/V within one output frame.

## Sidecar round trip

`apply_timeline` writes `<output>.edit.json` with the same `segments` /
`output` / `join_fade_ms` it rendered. That sidecar can be passed back into
`apply_timeline`. `write_edit_sidecar(..., timeline=edit)` stores the same
shape.

## VEGAS / Resolve

`apply_timeline(..., backend="vegas")` places a trimmed event per segment
and maps `zoom` to `event.set_motion` Pan/Crop keyframes. Speed ramps and
the compositor / lens / blur / overlay effects have no host RPC — those
segments are pre-rendered with FFmpeg and the result is placed.

Manual check: open the project, confirm each event's take offset equals
`segment.in` and its length equals `out - in` (or the sped length). Zoom
punches should appear as VideoMotion keyframes. Speed-ramped segments
should be pre-rendered media, not a live velocity envelope (the host has
no velocity-envelope RPC).
