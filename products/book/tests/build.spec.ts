import { execFile } from 'node:child_process';
import { existsSync, mkdtempSync, readFileSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { promisify } from 'node:util';

import { localizedTitle } from '../../../shared/title';
import { test, expect } from '../fixtures';
import { abs } from './helpers';

const build = promisify(execFile);
const reader = abs('reader');

test(localizedTitle('【构建配置 / DATA】 BK-DATA-033 文档目录和 VitePress 阅读器配置齐全', 'BK-DATA-033 documentation and VitePress reader are configured'), () => {
  expect(existsSync(abs('agent/SUMMARY.md'))).toBe(true);
  expect(existsSync(join(reader, '.vitepress/config.mts'))).toBe(true);
  const manifest = JSON.parse(readFileSync(join(reader, 'package.json'), 'utf8'));
  expect(manifest.scripts.build).toMatch(/^vitepress build\b/);
});

test(localizedTitle('【构建配置 / DATA】 BK-DATA-034 VitePress 阅读器产出 HTML', 'BK-DATA-034 the VitePress reader builds HTML'), async () => {
  test.setTimeout(120_000);
  const output = mkdtempSync(join(tmpdir(), 'tester-book-build-'));
  try {
    await build('npm', ['run', 'build', '--', '--outDir', output], {
      cwd: reader,
      timeout: 110_000,
    });
    expect(readFileSync(join(output, 'index.html'), 'utf8')).toContain('<html');
    expect(readFileSync(join(output, 'yeying/specification/API_SPECIFICATION.html'), 'utf8')).toContain('<html');
  } finally {
    rmSync(output, { recursive: true, force: true });
  }
});
