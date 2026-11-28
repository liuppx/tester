import type { APIRequestContext, APIResponse } from '@playwright/test';
import { setTimeout as wait } from 'node:timers/promises';

export async function successful<T>(response: APIResponse, label: string): Promise<T> {
  const body = (await response.json()) as { success?: boolean; data: T; message?: string };
  if (!response.ok() || !body.success) {
    throw new Error(`${label}: HTTP ${response.status()} ${body.message ?? ''}`);
  }
  return body.data;
}

async function waitForTask(admin: APIRequestContext, taskID: string) {
  const deadline = Date.now() + 15_000;
  do {
    const task = await successful<{ status: string; error_message?: string }>(
      await admin.get(`/api/v1/admin/tasks/${taskID}`),
      'Read channel task',
    );
    if (task.status === 'succeeded') return;
    if (task.status === 'failed' || task.status === 'canceled') {
      throw new Error(`Channel task ${task.status}: ${task.error_message ?? ''}`);
    }
    await wait(250);
  } while (Date.now() < deadline);
  throw new Error('Channel task did not complete within 15 seconds');
}

export async function publishTestChannel(
  admin: APIRequestContext, channelID: string, upstreamModel: string, model: string,
  endpoint: string, stream: boolean,
) {
  const channelPath = `/api/v1/admin/channel/${channelID}`;
  const { task } = await successful<{ task: { id: string } }>(
    await admin.post(`${channelPath}/refresh`, { data: { action: 'models' } }),
    'Refresh channel models',
  );
  await waitForTask(admin, task.id);
  await successful(
    await admin.put(`${channelPath}/models`, {
      data: {
        channel_models: [
          {
            model: upstreamModel,
            upstream_model: upstreamModel,
            provider: 'openai',
            type: 'text',
            endpoints: [endpoint],
            selected: true,
            input_price: 1,
            output_price: 1,
            price_unit: 'per_1k_tokens',
            currency: 'CNY',
          },
        ],
      },
    }),
    'Enable channel model',
  );
  await successful(
    await admin.put(`${channelPath}/endpoints`, {
      data: { model: upstreamModel, endpoint: endpoint, enabled: true },
    }),
    'Enable channel endpoint',
  );
  const { tasks } = await successful<{ tasks: Array<{ id: string }> }>(
    await admin.post(`${channelPath}/tests`, {
      data: {
        target_configs: [{ model: upstreamModel, endpoint: endpoint, is_stream: stream }],
      },
    }),
    'Verify channel endpoint',
  );
  if (tasks.length !== 1) throw new Error('Expected one verification task');
  await waitForTask(admin, tasks[0]!.id);
  await successful(
    await admin.put(`${channelPath}/models/publish`, {
      data: { model: upstreamModel, publish_enabled: true, published_model: model },
    }),
    'Publish channel model',
  );
}
