import { defineConfig } from '@playwright/test';

export default defineConfig({
  testDir: './tests/browser',
  testMatch: '*.spec.mjs',
  timeout: 30000,
  fullyParallel: false,
  workers: 1,
  reporter: [['list'], ['json', { outputFile: 'test-results/browser-report.json' }]],
  use: { baseURL: 'http://127.0.0.1:8766', browserName: 'chromium',
    screenshot: 'only-on-failure', trace: 'retain-on-failure' },
  webServer: {
    command: '.venv/bin/python tests/browser/serve_demo.py',
    url: 'http://127.0.0.1:8766/health', reuseExistingServer: !process.env.CI, timeout: 30000,
  },
});
