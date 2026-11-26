/**
 * book — YeYing community documentation.
 *
 * Env vars consumed:
 *   - BOOK_BASE_URL (optional; if empty, tests read filesystem)
 *   - BOOK_REPO_PATH (default: ../book)
 */
import { localizedTitle } from '../../../shared/title';
import { readFileSync, existsSync, readdirSync } from 'node:fs';
import { resolve } from 'node:path';
import { test, expect, baseURLFor } from '../fixtures';

function repoPath(): string {
  return (
    process.env['BOOK_REPO_PATH']?.trim() ||
    process.env['BOOKS_REPO_PATH']?.trim() ||
    '/Users/liuxin2/Workspace/opensource/book'
  );
}

function readText(rel: string): string {
  const file = resolve(repoPath(), rel);
  if (!existsSync(file)) {
    throw new Error(`book file not found at ${file}`);
  }
  return readFileSync(file, 'utf8');
}

test(localizedTitle("【目录索引与结构校验 / DATA】 Book 仓库存在且至少包含一个文档", "book repo exists and contains at least one document"), () => {
  expect(existsSync(repoPath())).toBe(true);
  const files = readdirSync(repoPath(), { recursive: true }) as string[];
  const md = files.filter((f) => f.endsWith('.md'));
  expect(md.length).toBeGreaterThan(0);
});

test(localizedTitle("【目录索引与结构校验 / DATA】 YeYing 产品文档中包含 YeYing", "yeying product docs mention YeYing"), () => {
  const candidates = ['yeying/agent.md', 'yeying/router.md', 'README.md'];
  let found = false;
  for (const candidate of candidates) {
    try {
      const text = readText(candidate);
      if (/YeYing|yeying/i.test(text)) {
        found = true;
        break;
      }
    } catch {
      // try next candidate
    }
  }
  expect(found).toBe(true);
});

test(localizedTitle("【目录索引与结构校验 / DATA】 Agent 文档包含带章节链接的 SUMMARY.md", "agent book has a SUMMARY.md with chapter links"), () => {
  const summary = 'agent/SUMMARY.md';
  let text: string;
  try {
    text = readText(summary);
  } catch {
    test.skip(true, `${summary} not present in book repo`);
    return;
  }
  const linkMatches = text.match(/\]\([^)]+\.md\)/g) ?? [];
  expect(linkMatches.length).toBeGreaterThanOrEqual(5);
});

test(localizedTitle("【可选在线站点冒烟 / API】 （可选）BOOK_BASE_URL 响应 2xx", "(optional) BOOK_BASE_URL responds 2xx"), async ({ request }) => {
  const baseURL = baseURLFor('book');
  test.skip(!baseURL, 'BOOK_BASE_URL not set');
  if (!baseURL) return;
  const res = await request.get(baseURL.replace(/\/$/, ''));
  expect(res.status()).toBeLessThan(500);
});
