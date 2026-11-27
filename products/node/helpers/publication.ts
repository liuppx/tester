import { Wallet } from 'ethers';
import type { APIResponse } from '@playwright/test';

import { apiContext } from '../../../shared/api';
import { envFor, expect, test } from '../fixtures';
import { loginWithWallet } from './auth';
import { buildCreateApplicationBody, signAction } from './signedAction';

interface Application {
  uid: string;
  name: string;
  status: string;
  isOnline: boolean;
}

interface Audit {
  meta: { uid: string };
  summary: { state: string; approvalCount: number; isDecided: boolean };
}

async function readData<T>(response: APIResponse): Promise<T> {
  expect(response.status(), await response.text()).toBe(200);
  const body = (await response.json()) as { code: number; data: T };
  expect(body.code).toBe(0);
  return body.data;
}

export async function withPublication(
  exercise: (fixture: {
    app: Application;
    auditId: string;
    readAudit: () => Promise<Audit>;
    approve: () => Promise<void>;
    readApplication: () => Promise<Application>;
    readMarket: () => Promise<Application[]>;
    unpublish: () => Promise<void>;
  }) => Promise<void>,
) {
  const env = envFor('node');
  const base = env.baseURL;
  const adminKey = process.env.NODE_TEST_ADMIN_PRIVATE_KEY;
  test.skip(!base || !adminKey || !process.env.NODE_TEST_DATABASE, 'requires the isolated Node source-mode database and test administrator');
  const admin = new Wallet(adminKey!);
  const owner = Wallet.createRandom();
  const ownerSession = await loginWithWallet(base!, owner.privateKey);
  const adminSession = await loginWithWallet(base!, admin.privateKey);
  const ownerApi = await apiContext(base!, { Authorization: `Bearer ${ownerSession.token}` });
  const adminApi = await apiContext(base!, { Authorization: `Bearer ${adminSession.token}` });
  try {
    const { body, did, version } = await buildCreateApplicationBody(owner, owner.address);
    const app = await readData<Application>(await ownerApi.post('/api/v1/public/applications', { data: body }));
    expect(app).toMatchObject({ name: body.name, status: 'BUSINESS_STATUS_OFFLINE', isOnline: false });
    const auditBody = {
      auditType: 'application',
      appOrServiceMetadata: JSON.stringify({ uid: app.uid, did, version, name: app.name, operateType: 'application' }),
      applicant: `${owner.address}::${owner.address}`,
      approver: admin.address.toLowerCase(),
      reason: '上架申请',
    };
    const auditSignature = await signAction(owner, {
      action: 'audit_submit',
      actor: owner.address,
      payload: { ...auditBody, targetType: 'application', targetDid: did, targetVersion: version },
    });
    const audit = await readData<Audit>(
      await ownerApi.post('/api/v1/public/audits', { data: { ...auditBody, ...auditSignature } }),
    );
    const auditId = audit.meta.uid;
    const appPath = `/api/v1/public/applications/${app.uid}`;
    const auditPath = `/api/v1/public/audits/${auditId}`;
    await exercise({
      app,
      auditId,
      readAudit: async () => readData<Audit>(await ownerApi.get(auditPath)),
      approve: async () => {
        const approver = admin.address.toLowerCase();
        const text = 'Tester publication approval';
        const signature = await signAction(admin, {
          action: 'audit_decision',
          actor: approver,
          payload: { auditId, decision: 'approve', approver, text },
        });
        const decision = await readData<{ status: string }>(
          await adminApi.post(`/api/v1/admin/audits/${auditId}/approve`, { data: { text, ...signature } }),
        );
        expect(decision.status).toBe('COMMENT_STATUS_AGREE');
      },
      readApplication: async () => readData<Application>(await ownerApi.get(appPath)),
      readMarket: async () => {
        const data = await readData<{ items: Application[] }>(
          await ownerApi.post('/api/v1/public/applications/search', {
            data: { condition: { keyword: app.name, status: 'BUSINESS_STATUS_ONLINE' } },
          }),
        );
        return data.items;
      },
      unpublish: async () => {
        const signature = await signAction(owner, {
          action: 'application_unpublish',
          actor: owner.address,
          payload: { applicationUid: app.uid },
        });
        expect(await readData(await ownerApi.post(`${appPath}/unpublish`, { data: signature }))).toEqual({ unpublished: true });
      },
    });
  } finally {
    await Promise.all([ownerApi.dispose(), adminApi.dispose()]);
  }
}
