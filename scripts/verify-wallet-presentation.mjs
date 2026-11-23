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

try {
  let input = ''
  for await (const chunk of process.stdin) input += chunk
  const { presentation, audience, nonce, trusted_document: trustedDocument } = JSON.parse(input)
  if (!presentation || presentation.version !== 1 || typeof presentation.holder !== 'string' || !presentation.holder.startsWith('did:yeying:wid_')) fail('invalid wallet identity presentation')
  if (presentation.audience !== audience || presentation.nonce !== nonce || !Array.isArray(presentation.scopes) || !presentation.scopes.includes('identity.basic')) fail('presentation context mismatch')
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
  process.stdout.write(JSON.stringify({ did: presentation.holder }))
} catch {
  process.stderr.write('Wallet identity presentation verification failed\n')
  process.exitCode = 1
}
