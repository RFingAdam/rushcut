# Timeline effects

Effects hang off `segments[].effects` in [edit.json](EDIT_JSON.md). This
page is the filter-graph reference.

## `zoom` — punch-in

Params: `scale`, `center` `[cx,cy]` (0–1) or `rect` `[x,y,w,h]` in source
pixels, `t_in`, `t_out`, `ease` seconds. Several punches in `punches: [...]`
are merged with `max()` so overlapping windows never dip back to 1.0.

Python builds a smoothstep envelope; FFmpeg applies it as:

```
crop=w='iw/z':h='ih/z':x='cx*iw-iw/z/2':y='cy*ih-ih/z/2',scale=W:H:flags=lanczos
```

`z`, `cx`, `cy` are expressions of `t`. VEGAS mapping: `event.set_motion`
Pan/Crop keyframes (`scale`, `pan_x`, `pan_y`).

Before: a 16:9 gameplay frame, HUD readable, subject small. After: the
subject fills the frame for `t_in…t_out`, eases in and out, and a second
punch that starts before the first eases out stays punched in (no 1.0 dip).

## `speed_ramp` — stepped speed

Params: `steps: [{start, end, factor}]` with `factor` in 1–8.
`tmix_above` (default 2) turns on `tmix` frame blending so 4×/6× does not
strobe. Audio:

- `atempo` — chained (`atempo=2,atempo=2` for 4×)
- `mute`
- `keep_original_length` — use `bed` (or silence) for the sped duration

```
setpts=PTS/factor, tmix=frames=N, fps=OUT
atempo=2.0,atempo=2.0
```

A 10 s segment at 4× renders to 2.5 s ± 1 frame.

VEGAS: no velocity-envelope RPC. The segment is pre-rendered with FFmpeg
and placed as media.

## `lens_zoom` — magnify inside a ring

`track_circle(path, start, duration, r_min, r_max)` writes per-frame
`[cx, cy, r, inliers]`. It fails when every frame has fewer than
`min_inliers` (default 12). Radial edge samples → RANSAC circle → median
+ Savitzky–Golay (moving average if SciPy is missing) → interpolate misses.

`lens_zoom` params: `track` (JSON path), `scale` (default 1.6),
`inset_ratio`, `feather` px, `ease` seconds (default 0.25), optional
`focus` track for the zoom centre. Lanczos remap, feathered alpha, optional
unsharp at full zoom. Frame-pipe: decode one RGB frame, write one frame,
never buffer the clip.

## `compose_layout` — vertical / square

| Layout | Result |
|---|---|
| `blur_fill` | sharp main crop over a blurred, darkened, cover-scaled background |
| `split` | main crop on top + inset (webcam) at `inset.x/y` with `border` / `border_color` |
| `center` | cover crop |
| `square` | 1:1 cover, then pad/scale to the output |

Rects are source pixels (`[x,y,w,h]` or `{x,y,w,h}`). Extra `strips` take
`rect`, `width`/`height`, and `y`. `crop_path` is a list of
`{t, x, y, w, h}` keys (linear or `ease: smoothstep`). Empty/black insets
are dropped when `drop_empty_inset` is true.

VEGAS: FFmpeg-only. Tracks + Pan/Crop cannot express a blurred fill.

## `blur_regions` / `blur_media`

List of `{rect, start, end, mode, strength, feather}`. Modes: `blur`
(gaussian `boxblur`), `pixelate`, `fill`. Rects may use the same keyframed
`keys` list as layout crop paths. `blur_media` is the file-level tool;
the same list is an `apply_timeline` effect. Detector JSON is accepted as-is
— Rushcut does not detect what to hide.

Lossless intermediates (`lossless=true` → ffv1) keep pixels outside the
rect bit-identical.

## `overlay` + `make_text_card`

`make_text_card(lines, style)` writes a PNG: 1–3 lines, per-line colour,
stroke, drop shadow, rounded translucent box, auto-shrink to `max_width`.
Font fallback is the bundled OFL Liberation Sans
(`server/rushcut/edit/fonts/`, SIL OFL 1.1).

`overlay` takes a PNG, PNG sequence, or ProRes 4444 / VP9-alpha file at
`x/y/scale/opacity` over `[start, end]` with optional fades.

9:16 safe-zone anchors: `safe_title`, `safe_lower`, `logo`, `top_left`,
`top_center`, `top_right`, `bottom_left`, `bottom_center`, `bottom_right`,
`center`. They sit clear of typical platform chrome (username / CTA).
