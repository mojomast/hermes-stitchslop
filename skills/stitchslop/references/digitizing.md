# Digitizing playbook

Turning flat art into something that sews. Written from a full badge build:
a 2-colour circular emblem with a traced figure and ring lettering.

## 1. Prep before tracing

The tracer is faithful — it reproduces every artifact you feed it. A highlight
sliver in hair becomes a hole in the embroidery; an image cropped at the hoop
edge becomes a straight gouge.

- Square canvas, subject centred, transparent or white background.
- Scale so the subject's furthest pixel lands ~1 mm inside the ring it lives in:
  measure the ink's max radius, compare with the inner ring radius, scale by the
  ratio, re-centre, then re-measure. Do this in the image, not with `transform` —
  the tracer sees the image.
- Fill enclosed specks: flood the exterior white, label the unreached components,
  fill the ones under a few thousand pixels. **Do not fill everything** — the face
  is a large enclosed white region and will be swallowed.
- Close hairline gaps (a broken highlight band across hair) by scanning columns
  inside the hair and filling white runs of 2–34 px bounded by ink. A big square
  kernel does this too, but erodes thin features like nose and lips.
- Save the result as PNG at 2× the tracing canvas (≈1108 px for 89 mm) so the
  tracer has detail to follow.

`scripts/prep_art.py` implements all four steps.

## 2. Trace

```
background.set {image: "data:image/png;base64,…", xMm, yMm, wMm, hMm, opacity}
background.digitize {colors: 2..12, detail: "auto"|"normal"|"fine", keepBackground?}
background.set {remove: true}
```

- `colors` below 2 is refused; each colour is a thread change at the machine.
- Only enclosed regions fill; anything reaching the image border is background.
- Detail `fine` merges less — good for faces, more objects.
- Digitize **appends**; to re-trace one part, delete the old objects by id first
  or you get a double figure.
- A 554 px source traces coarse; 1108 px traces cleanly.

## 3. Stitch types

Per object, with explicit ids (ordinals shift):

```
treatment.set {target:{ids:[…]}, to:"tatami"|"satinArea"|"satinStroke"|"…"}
```

- Large masses → tatami (also rescales density sensibly).
- Thin forms → satin.
- Letter shapes from a trace → satin fill is the default and correct.
- `treatment.set` **refuses on a text object's letters** (a retype rebuilds them);
  change the text's own numbers instead (`capMm`, `trackingMm`, `pathOffsetMm`,
  `baselineAxis`) via `text.set {id}`.

## 4. Geometry from measurement

Sample the reference image rather than eyeballing:

- radial ink profile → ring radii (the profile's peaks are circles)
- text band → the radius range with letters
- angular gap → histogram angles of ink in the band, find the widest empty run;
  that is where the sentence starts and ends

Then draw to those numbers. On the reference badge: outer circle at 94 % of the
emblem radius, inner at 75 %, text band centred at 84 %, sentence gap 72°–123°.

## 5. Lettering that sews

The catalog font is single-line: stems thinner than a stitch at any size that fits
an arc. Rendered as glyph objects they look like hairlines. Instead:

1. `scripts/arc_text.py` typesets the sentence with a bold condensed TTF onto an
   arc: give it the band radii, the desired span, and the start angle. It solves
   the point size so the sentence fills exactly that arc, and prints the cap.
2. Open the PNG and check orientation, spelling and the gap position.
3. `background.set` → `background.digitize` → remove the background. The letters
   come back as solid satin shapes, consistent with traced artwork.

SVG import is not a substitute: `design.import` traces paths as strokes, so filled
glyphs arrive hollow and simplified.

If you must use app text: `text.add` with `arcRadiusMm` anchors on the **start
point**, not the centre (it walks off the hoop). Draw an open circular path with
`object.draw {points, closed:false}` and pass `path:{ids:[…]}`. The text then
centres on the path, so a path shorter/longer than the letters changes the gap.

## 6. Verify, then export

```
scene.describe            → objects, stitches, hoopFit.fits
problems.list             → target 0
view.set {mode:"3d", fit:true, grid:false}
design.export {format:"dst"|"pes"|"jef"|"exp"|"vp3", deliver:"data"}
```

- Judge fills in 3D with the background removed: a figure can look solid in stitch
  view only because the source photo shows through at low opacity.
- Trim count ≈ separate islands. Dozens of trims is a slow sew; merge islands or
  accept it deliberately and tell the user.
- One physical thread per colour: a two-blue trace is a thread change even when it
  reads as "blue" on screen.
- Exports come back as `files[]` with per-file `dataUrl` plus a shared `.col`
  sidecar; the plugin writes them for you and returns `savedFiles`.
- Confirm `hoopFit.fits` and the mm size before handing files over.

## Traps seen in practice

| Symptom | Cause |
|---|---|
| Text looks short and airy | single-line font at small cap — switch to render-then-trace |
| Letters hollow | SVG import strokes paths |
| Figure duplicated | re-trace appended over the old objects |
| Rings vanished | delete by ordinal list after ordinals shifted |
| Face erased | flood-fill treated the face as a hole |
| Design "exceeds hoop" | band + text outside the hoop; check `hoopFit` |
| Looks sparse in stitch view | background image at low opacity was doing the work |
