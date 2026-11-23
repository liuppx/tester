import assert from 'node:assert/strict'
import { generateKeyPairSync, sign } from 'node:crypto'
import { spawnSync } from 'node:child_process'
import { fileURLToPath } from 'node:url'

const helper = fileURLToPath(new URL('./verify-wallet-presentation.mjs', import.meta.url))
const canonical = value => value === null ? 'null' : typeof value !== 'object' ? JSON.stringify(value) : Array.isArray(value) ? `[${value.map(canonical).join(',')}]` : `{${Object.keys(value).sort().map(key => `${JSON.stringify(key)}:${canonical(value[key])}`).join(',')}}`
const b64 = value => Buffer.from(value).toString('base64url')
const pair = generateKeyPairSync('ed25519')
const publicKey = b64(pair.publicKey.export({ format: 'der', type: 'spki' }).subarray(-32))
const did = 'did:yeying:wid_' + 'a'.repeat(32)
const controllerId = 'controller-1'
const controller = { controllerId, publicKey, algorithm: 'Ed25519', purposes: ['authentication', 'assertion', 'manage'], status: 'active' }
const documentUnsigned = { version: 1, id: did, walletIdentityId: did.slice('did:yeying:'.length), revision: 1, controllers: [controller], accounts: [], issuers: [] }
const document = { ...documentUnsigned, proof: {
  type: 'YeyingIdentityDocumentProofV1', verificationMethod: `${did}#${controllerId}`, purpose: 'manage',
  proofValue: b64(sign(null, Buffer.from(canonical(documentUnsigned)), pair.privateKey)),
} }
const unsigned = { version: 1, holder: did, audience: 'https://tester.example', nonce: 'challenge-1', issuedAt: new Date().toISOString(), expiresAt: new Date(Date.now() + 60000).toISOString(), scopes: ['identity.basic'], identityDocument: document }
const presentation = { ...unsigned, proof: {
  type: 'YeyingIdentityPresentationProofV1', verificationMethod: `${did}#${controllerId}`, purpose: 'authentication',
  proofValue: b64(sign(null, Buffer.from(canonical(unsigned)), pair.privateKey)),
} }
function verify(value, audience = unsigned.audience, nonce = unsigned.nonce, trustedDocument = document) {
  return spawnSync(process.execPath, [helper], { input: JSON.stringify({ presentation: value, audience, nonce, trusted_document: trustedDocument }), encoding: 'utf8' })
}

const valid = verify(presentation)
assert.equal(valid.status, 0, valid.stderr)
assert.deepEqual(JSON.parse(valid.stdout), { did })
assert.notEqual(verify(presentation, unsigned.audience, 'wrong-challenge').status, 0)
const attacker = generateKeyPairSync('ed25519')
const attackerPublicKey = b64(attacker.publicKey.export({ format: 'der', type: 'spki' }).subarray(-32))
const forgedController = { ...controller, publicKey: attackerPublicKey }
const forgedDocumentUnsigned = { ...documentUnsigned, controllers: [forgedController] }
const forgedDocument = { ...forgedDocumentUnsigned, proof: {
  ...document.proof, proofValue: b64(sign(null, Buffer.from(canonical(forgedDocumentUnsigned)), attacker.privateKey)),
} }
const forgedUnsigned = { ...unsigned, identityDocument: forgedDocument }
const forgedIdentity = { ...forgedUnsigned, proof: {
  ...presentation.proof, proofValue: b64(sign(null, Buffer.from(canonical(forgedUnsigned)), attacker.privateKey)),
} }
assert.notEqual(verify(forgedIdentity).status, 0)
const forged = structuredClone(presentation)
forged.proof.proofValue = b64(Buffer.alloc(64))
assert.notEqual(verify(forged).status, 0)
process.stdout.write('Wallet presentation signatures and challenge verification passed.\n')
