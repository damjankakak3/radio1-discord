# Bot avatar

The avatar is a cream pixel "1" with two mint signal strokes on a dark green
background. It is original artwork, drawn in a standalone HTML canvas and
captured from Chromium, with no external images or fonts. The source grid is
32 × 32 with exactly three flat colors, and the exports use whole-number
scaling with pixel smoothing turned off.

Upload `radio1-logo-512.png` in the Discord application's Bot settings. There
is a 1024-pixel PNG too. Both are square, and the artwork fits inside Discord's
circular crop.

Open `avatar.html` to view or change the artwork. The colors are CSS
variables, and the numeral and signal strokes are drawn on whole pixels.

## Capture again

The capture script needs Node.js, Playwright, and a Chromium browser. Point it
at existing installations, or install Playwright into a separate tools folder:

```bash
npm install --prefix /tmp/radio1-avatar-tools playwright
PLAYWRIGHT_MODULE=/tmp/radio1-avatar-tools/node_modules/playwright \
  BROWSER_EXECUTABLE=/path/to/chromium \
  node assets/avatar/capture.mjs
```

Run it from the repository root. The script captures both PNG sizes and checks
their dimensions. It fails on a rendering error or if smoothing has added
colors to the source palette. It also saves a circular preview at 32 and 64
pixels under `artifacts/avatar/`, which Git ignores. The browser it opens is
isolated and does not use your personal profile.
