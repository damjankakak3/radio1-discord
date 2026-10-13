import assert from 'node:assert/strict';
import { readFile, mkdir } from 'node:fs/promises';
import { createRequire } from 'node:module';
import { fileURLToPath } from 'node:url';

const require = createRequire(import.meta.url);
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const directory = fileURLToPath(new URL('./', import.meta.url));
const previewDirectory = fileURLToPath(new URL('../../artifacts/avatar/', import.meta.url));
await mkdir(previewDirectory, { recursive: true });
const browser = await chromium.launch({
  headless: true,
  executablePath: process.env.BROWSER_EXECUTABLE || undefined,
  args: ['--disable-dev-shm-usage', '--no-sandbox'],
});

try {
  const page = await browser.newPage({ viewport: { width: 512, height: 512 }, deviceScaleFactor: 1 });
  const errors = [];
  page.on('pageerror', error => errors.push(error.message));
  await page.goto(new URL('./avatar.html', import.meta.url).href);
  await page.waitForFunction(() => document.documentElement.dataset.ready === 'true');
  assert.deepEqual(errors, [], 'The avatar HTML must render without JavaScript errors.');

  const statistics = await page.evaluate(() => {
    const canvas = document.querySelector('#avatar');
    const data = canvas.getContext('2d').getImageData(0, 0, canvas.width, canvas.height).data;
    const colors = new Set();
    let opaque = true;
    for (let i = 0; i < data.length; i += 4) {
      colors.add([...data.slice(i, i + 4)].join(','));
      opaque &&= data[i + 3] === 255;
    }
    return { width: canvas.width, height: canvas.height, colors: colors.size, opaque };
  });
  assert.equal(statistics.width, 32);
  assert.equal(statistics.height, 32);
  assert.equal(statistics.colors, 3, 'Use exactly three flat colors without smoothed pixels.');
  assert.equal(statistics.opaque, true);

  for (const size of [512, 1024]) {
    await page.setViewportSize({ width: size, height: size });
    const filename = `${directory}radio1-logo-${size}.png`;
    await page.locator('#avatar').screenshot({ path: filename, scale: 'css' });
    const png = await readFile(filename);
    assert.equal(png.subarray(0, 8).toString('hex'), '89504e470d0a1a0a');
    assert.equal(png.readUInt32BE(16), size);
    assert.equal(png.readUInt32BE(20), size);
    console.log(`Captured ${size} × ${size} PNG.`);
  }

  // Inspect real image downsizing and Discord-style circular crops.
  const image = `data:image/png;base64,${(await readFile(`${directory}radio1-logo-512.png`)).toString('base64')}`;
  await page.setViewportSize({ width: 640, height: 320 });
  await page.setContent(`<!doctype html><html><head><style>
    * { box-sizing: border-box; }
    body { margin: 0; padding: 32px; background: #303532; color: #eee9db; font: 14px system-ui; }
    main { display: flex; align-items: center; gap: 36px; }
    img { display: block; border-radius: 50%; }
    .large { width: 256px; height: 256px; }
    section { display: grid; gap: 24px; }
    figure { margin: 0; display: flex; gap: 16px; align-items: center; }
    .medium { width: 64px; height: 64px; }
    .small { width: 32px; height: 32px; }
  </style></head><body><main>
    <img class="large" src="${image}" alt="Circle crop">
    <section><figure><img class="medium" src="${image}" alt="64 pixel preview"><figcaption>64 px</figcaption></figure>
    <figure><img class="small" src="${image}" alt="32 pixel preview"><figcaption>32 px</figcaption></figure></section>
  </main></body></html>`);
  await page.locator('img').first().waitFor();
  await page.screenshot({ path: `${previewDirectory}circle-and-small-sizes.png` });
  console.log(`Validated ${statistics.colors} flat colors; circular and small-size preview saved.`);
} finally {
  await browser.close();
}
