import { request as playwrightRequest, type APIResponse } from '@playwright/test';
import { randomBytes } from 'node:crypto';

export interface BasicAuth {
  type: 'basic';
  username: string;
  password: string;
}

export interface BearerAuth {
  type: 'bearer';
  token: string;
}

export type Auth = BasicAuth | BearerAuth;

export async function apiContext(baseURL: string, extraHeaders: Record<string, string> = {}) {
  const headers = { ...extraHeaders };
  const hostname = new URL(baseURL).hostname;
  if (
    process.env.TEST_ENV === 'local' &&
    (hostname === '127.0.0.1' || hostname === 'localhost') &&
    !headers['X-Forwarded-For']
  ) {
    // Local Router limits are keyed by client IP. Give each isolated API
    // context its own TEST-NET client so a large suite does not throttle later
    // tests on the shared loopback address.
    headers['X-Forwarded-For'] = `198.51.100.${1 + (randomBytes(1).readUInt8(0) % 254)}`;
  }
  return playwrightRequest.newContext({
    baseURL,
    extraHTTPHeaders: headers,
    ignoreHTTPSErrors: true,
  });
}

export function applyAuth(headers: Record<string, string>, auth?: Auth): Record<string, string> {
  if (!auth) return headers;
  if (auth.type === 'basic') {
    const token = Buffer.from(`${auth.username}:${auth.password}`).toString('base64');
    return { ...headers, Authorization: `Basic ${token}` };
  }
  if (auth.type === 'bearer') {
    return { ...headers, Authorization: `Bearer ${auth.token}` };
  }
  return headers;
}

export function expectStatus(
  res: APIResponse,
  predicate: number | number[] | ((s: number) => boolean),
) {
  const check =
    typeof predicate === 'number'
      ? (s: number) => s === predicate
      : Array.isArray(predicate)
        ? (s: number) => predicate.includes(s)
        : predicate;
  if (!check(res.status())) {
    throw new Error(
      `Expected ${
        typeof predicate === 'number'
          ? predicate
          : Array.isArray(predicate)
            ? `one of [${predicate.join(', ')}]`
            : 'predicate'
      } status, got ${res.status()} for ${res.url()}`,
    );
  }
}

export interface WebdavOptions {
  baseURL: string;
  prefix?: string;
  path: string;
  auth?: Auth;
}

export async function webdavGet(opts: WebdavOptions): Promise<APIResponse> {
  const ctx = await apiContext(opts.baseURL, applyAuth({}, opts.auth));
  const url = `${opts.prefix ?? ''}${opts.path}`;
  return ctx.fetch(url, { method: 'GET' });
}

export async function webdavPut(
  opts: WebdavOptions & { body: string | Buffer; contentType?: string },
): Promise<APIResponse> {
  const ctx = await apiContext(opts.baseURL, applyAuth({}, opts.auth));
  const url = `${opts.prefix ?? ''}${opts.path}`;
  return ctx.fetch(url, {
    method: 'PUT',
    data: opts.body,
    headers: opts.contentType ? { 'Content-Type': opts.contentType } : undefined,
  });
}

export async function webdavPropfind(opts: WebdavOptions & { depth?: 0 | 1 | 'infinity' }): Promise<APIResponse> {
  const ctx = await apiContext(opts.baseURL, applyAuth({}, opts.auth));
  const url = `${opts.prefix ?? ''}${opts.path}`;
  return ctx.fetch(url, {
    method: 'PROPFIND',
    headers: {
      Depth: String(opts.depth ?? 1),
      'Content-Type': 'application/xml',
    },
    data: '<?xml version="1.0"?><d:propfind xmlns:d="DAV:"><d:allprop/></d:propfind>',
  });
}
