#!/usr/bin/env node
import { createPublicKey, verify } from 'node:crypto'

const fail = message => { throw new Error(message) }
const canonical = value => {
  if (value === null) return 'null'
  if (typeof value === 'number') {
    if (!Number.isFinite(value)) fail('invalid number')
    return JSON.stringify(Object.is(value, -0) ? 0 : value)
  }
  if (typeof value === 'string' || typeof value === 'boolean') return JSON.stringify(value)
  if (Array.isArray(value)) return `[${value.map(canonical).join(',')}]`
  if (typeof value === 'object') return `{${Object.keys(value).sort().map(key => `${JSON.stringify(key)}:${canonical(value[key])}`).join(',')}}`
  fail('unsupported identity data')
}
const decode = value => Buffer.from(value.replace(/-/g, '+').replace(/_/g, '/') + '='.repeat((4 - value.length % 4) % 4), 'base64')
function verifyProof(publicKey, signature, payload) {
  if (typeof publicKey !== 'string' || typeof signature !== 'string') fail('proof is missing')
  const raw = decode(publicKey)
  if (raw.length !== 32) fail('invalid Ed25519 key')
  const key = createPublicKey({ key: Buffer.concat([Buffer.from('302a300506032b6570032100', 'hex'), raw]), format: 'der', type: 'spki' })
  if (!verify(null, Buffer.from(canonical(payload)), key, decode(signature))) fail('invalid signature')
}
function controller(document, method, purpose) {
  if (typeof method !== 'string' || !Array.isArray(document.controllers)) fail('identity controller missing')
  const found = document.controllers.find(item => item && method === `${document.id}#${item.controllerId}` && item.status === 'active' && Array.isArray(item.purposes) && item.purposes.includes(purpose))
  if (!found) fail('identity controller is not authorized')
  return found
}

function safeIssuerEndpoint(value, origin) {
  const url = new URL(value, origin)
  if (url.origin !== origin || url.username || url.password || url.search || url.hash) fail('unsafe identity issuer endpoint')
  return url
}

async function verifyIdentityCredentials(presentation, issuerUrl) {
  const base = new URL(issuerUrl)
  if (!['https:', 'http:'].includes(base.protocol) || base.username || base.password || base.search || base.hash ||
      (base.protocol !== 'https:' && !['localhost', '127.0.0.1', '::1'].includes(base.hostname))) fail('identity issuer must use HTTPS')
  base.pathname = base.pathname.replace(/\/$/, '')
  const origin = base.origin
  const expectedIssuer = `did:web:${base.host}`
  const getJson = async url => {
    let response
    try {
      response = await fetch(url, { headers: { accept: 'application/json' }, redirect: 'error', signal: AbortSignal.timeout(5000) })
    } catch (error) {
      fail(`identity issuer request failed: ${new URL(url).pathname}`)
    }
    if (!response.ok) fail('identity issuer unavailable')
    return response.json()
  }
  const metadata = await getJson(new URL('/.well-known/openid-credential-issuer', origin))
  if (metadata.issuer !== expectedIssuer) fail('identity issuer mismatch')
  const jwksUrl = safeIssuerEndpoint(metadata.jwks_uri, origin)
  const statusUrl = safeIssuerEndpoint(metadata.credential_status_uri, origin)
  const jwks = await getJson(jwksUrl)
  const expectedTypes = ['WalletAccountCredential', 'UsernameCredential', 'EmailCredential']
  const verified = new Map()
  const credentialIds = []
  for (const token of presentation.credentials || []) {
    const [headerPart, payloadPart, signaturePart] = String(token).split('.')
    if (!headerPart || !payloadPart || !signaturePart) fail('identity credential is invalid')
    let header, claims
    try { header = JSON.parse(decode(headerPart).toString('utf8')); claims = JSON.parse(decode(payloadPart).toString('utf8')) }
    catch { fail('identity credential is invalid') }
    const types = claims.vc?.type
    const type = Array.isArray(types) ? expectedTypes.find(item => types.includes(item)) : null
    if (!type) continue
    if (header.alg !== 'EdDSA' || header.typ !== 'JWT' || claims.iss !== expectedIssuer || claims.sub !== presentation.holder ||
        !claims.jti || !Number.isFinite(claims.exp) || claims.exp <= Date.now() / 1000 ||
        !Number.isFinite(claims.nbf) || claims.nbf > Date.now() / 1000) fail('identity credential claims are invalid')
    const jwk = jwks.keys?.find(key => key.kty === 'OKP' && key.crv === 'Ed25519' && key.kid === header.kid)
    if (!jwk?.x) fail('identity credential issuer key is missing')
    const raw = Buffer.from(jwk.x, 'base64url')
    if (raw.length !== 32) fail('identity credential issuer key is invalid')
    const key = createPublicKey({ key: Buffer.concat([Buffer.from('302a300506032b6570032100', 'hex'), raw]), format: 'der', type: 'spki' })
    if (!verify(null, Buffer.from(`${headerPart}.${payloadPart}`), key, decode(signaturePart))) fail('identity credential signature is invalid')
    const credentialSubject = claims.vc?.credentialSubject
    if (credentialSubject?.id !== presentation.holder) fail('identity credential subject mismatch')
    verified.set(type, credentialSubject)
    credentialIds.push(claims.jti)
  }
  for (const type of expectedTypes) if (!verified.has(type)) fail(`required identity credential is missing: ${type}`)
  const walletCredential = verified.get('WalletAccountCredential')
  const wallet = presentation.walletProof || {}
  if (String(walletCredential.address || '').toLowerCase() !== String(wallet.address || '').toLowerCase() ||
      String(walletCredential.chainKey || '').toLowerCase() !== String(wallet.chainKey || '').toLowerCase()) fail('wallet account credential does not match the presentation')
  if (typeof verified.get('UsernameCredential').username !== 'string' || !verified.get('UsernameCredential').username.trim()) fail('verified username credential is invalid')
  if (typeof verified.get('EmailCredential').email !== 'string' || !verified.get('EmailCredential').email.trim()) fail('verified email credential is invalid')
  let response
  try {
    response = await fetch(statusUrl, {
      method: 'POST', redirect: 'error', signal: AbortSignal.timeout(5000),
      headers: { accept: 'application/json', 'content-type': 'application/json' },
      body: JSON.stringify({ issuer: expectedIssuer, credentials: credentialIds }),
    })
  } catch {
    fail('wallet account credential status request failed')
  }
  if (!response.ok) fail('wallet account credential status unavailable')
  const status = await response.json()
  if (status.code !== 0 || status.data?.issuer !== expectedIssuer || credentialIds.some(id => status.data?.statuses?.[id] !== 'active')) fail('identity credential is not active')
}

try {
  let input = ''
  for await (const chunk of process.stdin) input += chunk
  const { presentation, audience, nonce, trusted_document: trustedDocument, issuer_url: issuerUrl } = JSON.parse(input)
  if (!presentation || presentation.version !== 1 || typeof presentation.holder !== 'string' || !presentation.holder.startsWith('did:yeying:wid_')) fail('invalid wallet identity presentation')
  if (presentation.audience !== audience || presentation.nonce !== nonce || !Array.isArray(presentation.scopes) ||
      !presentation.scopes.includes('identity.basic') || !presentation.scopes.includes('identity.wallet') ||
      !presentation.scopes.includes('identity.username') || !presentation.scopes.includes('identity.email')) fail('presentation context mismatch')
  const issued = Date.parse(presentation.issuedAt)
  const expires = Date.parse(presentation.expiresAt)
  const now = Date.now()
  if (!Number.isFinite(issued) || !Number.isFinite(expires) || issued > now + 60000 || expires <= now - 60000) fail('presentation expired')
  const document = presentation.identityDocument
  if (!document || document.version !== 1 || document.id !== presentation.holder || !Number.isInteger(document.revision) || document.revision < 1) fail('identity document is invalid')
  if (trustedDocument && canonical(document) !== canonical(trustedDocument)) fail('identity document does not match pinned DID')
  const docProof = document.proof
  if (!docProof || docProof.type !== 'YeyingIdentityDocumentProofV1' || !['manage', 'assertionMethod'].includes(docProof.purpose)) fail('identity document proof is invalid')
  const docPurpose = docProof.purpose === 'assertionMethod' ? 'assertion' : docProof.purpose
  const docController = controller(document, docProof.verificationMethod, docPurpose)
  const { proof: ignoredDocProof, ...unsignedDocument } = document
  verifyProof(docController.publicKey, docProof.proofValue, unsignedDocument)
  const proof = presentation.proof
  if (!proof || proof.type !== 'YeyingIdentityPresentationProofV1' || proof.purpose !== 'authentication') fail('presentation proof is invalid')
  const authController = controller(document, proof.verificationMethod, 'authentication')
  const { proof: ignoredProof, ...unsignedPresentation } = presentation
  verifyProof(authController.publicKey, proof.proofValue, unsignedPresentation)
  await verifyIdentityCredentials(presentation, issuerUrl)
  process.stdout.write(JSON.stringify({ did: presentation.holder }))
} catch (error) {
  process.stderr.write(`Wallet identity presentation verification failed: ${error instanceof Error ? error.message : 'invalid identity data'}\n`)
  process.exitCode = 1
}
