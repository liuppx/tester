import assert from 'node:assert/strict'
import { createServer } from 'node:http'
import { generateKeyPairSync, sign } from 'node:crypto'
import { spawn } from 'node:child_process'
import { fileURLToPath } from 'node:url'

const helper = fileURLToPath(new URL('./verify-wallet-presentation.mjs', import.meta.url))
const canonical = value => value === null ? 'null' : typeof value !== 'object' ? JSON.stringify(value) : Array.isArray(value) ? `[${value.map(canonical).join(',')}]` : `{${Object.keys(value).sort().map(key => `${JSON.stringify(key)}:${canonical(value[key])}`).join(',')}}`
const b64 = value => Buffer.from(value).toString('base64url')
const did = 'did:yeying:wid_' + 'a'.repeat(32)
const controllerId = 'controller-1'
const identityKeys = generateKeyPairSync('ed25519')
const issuerKeys = generateKeyPairSync('ed25519')
const publicKey = b64(identityKeys.publicKey.export({ format: 'der', type: 'spki' }).subarray(-32))
const issuerPublic = b64(issuerKeys.publicKey.export({ format: 'der', type: 'spki' }).subarray(-32))
const controller = { controllerId, publicKey, algorithm: 'Ed25519', purposes: ['authentication', 'assertion', 'manage'], status: 'active' }
const documentUnsigned = { version: 1, id: did, walletIdentityId: did.slice('did:yeying:'.length), revision: 1, controllers: [controller], accounts: [], issuers: [] }
const document = { ...documentUnsigned, proof: {
  type: 'YeyingIdentityDocumentProofV1', verificationMethod: `${did}#${controllerId}`, purpose: 'manage',
  proofValue: b64(sign(null, Buffer.from(canonical(documentUnsigned)), identityKeys.privateKey)),
} }
const credentialHeader = { alg: 'EdDSA', typ: 'JWT', kid: 'issuer-key-1' }
const credentialClaims = [
  { iss: '', sub: did, iat: Math.floor(Date.now() / 1000), nbf: Math.floor(Date.now() / 1000), exp: Math.floor(Date.now() / 1000) + 3600, jti: 'urn:test:wallet-account', vc: { type: ['VerifiableCredential', 'WalletAccountCredential'], credentialSubject: { id: did, chainKey: 'eip155:1', address: '0x1111111111111111111111111111111111111111' } } },
  { iss: '', sub: did, iat: Math.floor(Date.now() / 1000), nbf: Math.floor(Date.now() / 1000), exp: Math.floor(Date.now() / 1000) + 3600, jti: 'urn:test:username', vc: { type: ['VerifiableCredential', 'UsernameCredential'], credentialSubject: { id: did, username: 'tester-user' } } },
  { iss: '', sub: did, iat: Math.floor(Date.now() / 1000), nbf: Math.floor(Date.now() / 1000), exp: Math.floor(Date.now() / 1000) + 3600, jti: 'urn:test:email', vc: { type: ['VerifiableCredential', 'EmailCredential'], credentialSubject: { id: did, email: 'tester@example.test' } } },
]
let issuer = ''
const server = createServer(async (request, response) => {
  if (request.url === '/.well-known/openid-credential-issuer') {
    response.setHeader('content-type', 'application/json')
    response.end(JSON.stringify({ issuer, jwks_uri: `${issuerOrigin}/.well-known/jwks.json`, credential_status_uri: `${issuerOrigin}/api/v1/public/identity/credentials/status` }))
  } else if (request.url === '/.well-known/jwks.json') {
    response.setHeader('content-type', 'application/json')
    response.end(JSON.stringify({ keys: [{ kty: 'OKP', crv: 'Ed25519', kid: 'issuer-key-1', x: issuerPublic }] }))
  } else if (request.url === '/api/v1/public/identity/credentials/status') {
    let body = ''; for await (const chunk of request) body += chunk
    const payload = JSON.parse(body)
    response.setHeader('content-type', 'application/json')
    response.end(JSON.stringify({ code: 0, data: { issuer, statuses: Object.fromEntries(payload.credentials.map(id => [id, statusValue])) } }))
  } else { response.statusCode = 404; response.end() }
})
let issuerOrigin = ''
let statusValue = 'active'
await new Promise(resolve => server.listen(0, '127.0.0.1', resolve))
issuerOrigin = `http://127.0.0.1:${server.address().port}`
issuer = `did:web:127.0.0.1:${server.address().port}`
const credentials = credentialClaims.map(claims => {
  claims.iss = issuer
  const input = `${b64(JSON.stringify(credentialHeader))}.${b64(JSON.stringify(claims))}`
  return `${input}.${b64(sign(null, Buffer.from(input), issuerKeys.privateKey))}`
})
const unsigned = {
  version: 1, holder: did, audience: 'https://tester.example', nonce: 'challenge-1',
  issuedAt: new Date().toISOString(), expiresAt: new Date(Date.now() + 60000).toISOString(),
  scopes: ['identity.basic', 'identity.wallet', 'identity.username', 'identity.email'], identityDocument: document,
  credentials, walletProof: { chainKey: 'eip155:1', address: credentialClaims[0].vc.credentialSubject.address },
}
const presentation = { ...unsigned, proof: {
  type: 'YeyingIdentityPresentationProofV1', verificationMethod: `${did}#${controllerId}`, purpose: 'authentication',
  proofValue: b64(sign(null, Buffer.from(canonical(unsigned)), identityKeys.privateKey)),
} }
async function verify(value, audience = unsigned.audience, nonce = unsigned.nonce, trustedDocument = document) {
  const environment = { ...process.env, TESTER_IDENTITY_ISSUER_URL: issuerOrigin }
  return new Promise(resolve => {
    const child = spawn(process.execPath, [helper], { env: environment, stdio: ['pipe', 'pipe', 'pipe'] })
    let stdout = '', stderr = ''
    child.stdout.setEncoding('utf8').on('data', value => { stdout += value })
    child.stderr.setEncoding('utf8').on('data', value => { stderr += value })
    const timeout = setTimeout(() => child.kill('SIGKILL'), 10000)
    child.on('close', status => { clearTimeout(timeout); resolve({ status, stdout, stderr }) })
    child.stdin.end(JSON.stringify({ presentation: value, audience, nonce, trusted_document: trustedDocument, issuer_url: issuerOrigin }))
  })
}

try {
  const valid = await verify(presentation)
  assert.equal(valid.status, 0, valid.stderr)
  assert.deepEqual(JSON.parse(valid.stdout), { did })
  const refreshedDocument = structuredClone(document)
  refreshedDocument.proof.created = new Date(Date.now() + 1000).toISOString()
  const { proof: ignoredDocumentProof, ...unsignedRefreshedDocument } = refreshedDocument
  refreshedDocument.proof.proofValue = b64(sign(null, Buffer.from(canonical(unsignedRefreshedDocument)), identityKeys.privateKey))
  const represented = { ...structuredClone(presentation), identityDocument: refreshedDocument }
  const { proof: ignoredProof, ...unsignedRepresentation } = represented
  represented.proof.proofValue = b64(sign(null, Buffer.from(canonical(unsignedRepresentation)), identityKeys.privateKey))
  const refreshedProofResult = await verify(represented)
  assert.equal(refreshedProofResult.status, 0, refreshedProofResult.stderr)
  assert.deepEqual(JSON.parse(refreshedProofResult.stdout), { did })
  assert.notEqual((await verify(presentation, unsigned.audience, 'wrong-challenge')).status, 0)
  const forged = structuredClone(presentation)
  forged.proof.proofValue = b64(Buffer.alloc(64))
  assert.notEqual((await verify(forged)).status, 0)
  assert.notEqual((await verify(presentation, unsigned.audience, unsigned.nonce, { ...document, revision: 99 })).status, 0)
  statusValue = 'revoked'
  assert.notEqual((await verify(presentation)).status, 0)
  statusValue = 'active'
  const withoutEmail = structuredClone(presentation)
  withoutEmail.credentials.pop()
  const withoutEmailUnsigned = { ...withoutEmail }; delete withoutEmailUnsigned.proof
  withoutEmail.proof.proofValue = b64(sign(null, Buffer.from(canonical(withoutEmailUnsigned)), identityKeys.privateKey))
  assert.notEqual((await verify(withoutEmail)).status, 0)
  process.stdout.write('Wallet presentation, issuer credential, status, and challenge verification passed.\n')
} finally {
  server.close()
}
