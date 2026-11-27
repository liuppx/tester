import { readFileSync, writeFileSync, renameSync } from 'node:fs';
import { join, relative, sep } from 'node:path';
import type { FullConfig, FullResult, Location, Reporter, Suite, TestCase } from '@playwright/test/reporter';

function outcome(test: TestCase): string {
  if (!test.results.length) return 'not-run';
  if (test.results.at(-1)?.status === 'interrupted') return 'interrupted';
  const reported = test.outcome();
  if (test.expectedStatus === 'failed') {
    if (reported === 'expected') return 'expected-failure';
    if (reported === 'flaky') return 'flaky-expected-failure';
  }
  return reported;
}

export default class AnalysisReporter implements Reporter {
  private config!: FullConfig;
  private suite!: Suite;

  onBegin(config: FullConfig, suite: Suite): void {
    this.config = config;
    this.suite = suite;
  }

  private failureLocation(location: Location | undefined, project: string | undefined) {
    if (!location || !project) return null;
    const file = relative(this.config.rootDir, location.file).split(sep).join('/');
    if (!file.startsWith(`products/${project}/`) && !file.startsWith('shared/')) return null;
    return { file, line: location.line };
  }

  onEnd(result: FullResult): void {
    const output = process.env.TESTER_ANALYSIS_RESULTS_FILE;
    if (!output || !this.suite) return;
    const projects = Object.fromEntries(this.config.projects.map(project => [
      project.name, { retries: project.retries, timeout_ms: project.timeout },
    ]));
    const cases = this.suite.allTests().map(test => ({
      id: test.id,
      project: test.parent.project()?.name,
      case_ids: [...new Set(test.titlePath().slice(1).flatMap(
        part => part.match(/\b[A-Z][A-Z0-9]*(?:-[A-Z0-9]+)*-\d{3}\b/g) ?? [],
      ))],
      file: relative(this.config.rootDir, test.location.file).split(sep).join('/'),
      line: test.location.line,
      outcome: outcome(test),
      expected_status: test.expectedStatus,
      timeout_ms: test.timeout,
      attempts: test.results.map(attempt => ({
        retry: attempt.retry,
        status: attempt.status,
        duration_ms: attempt.duration,
        failure_location: this.failureLocation(attempt.error?.location, test.parent.project()?.name),
      })),
    }));
    const template = readFileSync(join(this.config.rootDir, 'tester.env.template'), 'utf8');
    const configured = Object.fromEntries(
      Array.from(template.matchAll(/^[ 	]*(?:#[ 	]*)?([A-Z][A-Z0-9_]+)=/gm), match => [
        match[1], Boolean(process.env[match[1]]?.trim()),
      ]),
    );
    const temporary = output + '.tmp';
    writeFileSync(temporary, JSON.stringify({
      status: result.status,
      config: { workers: this.config.workers, projects, playwright_version: this.config.version, configured },
      cases,
    }), { mode: 0o600 });
    renameSync(temporary, output);
  }
}
