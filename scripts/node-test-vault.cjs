// Build a private vault for one isolated Node test batch.
const fs = require('node:fs');
const path = require('node:path');
const { randomBytes } = require('node:crypto');

const [repo, sourceConfig, fixture] = process.argv.slice(2);
const { Wallet } = require(path.join(repo, 'node_modules/ethers'));
const { decryptSecrets, encryptSecrets, loadVault, replaceVault } =
  require(path.join(repo, 'scripts/secret-vault.cjs'));
const source = require(sourceConfig);
const base = path.dirname(sourceConfig);
const database = source.database ?? {};
if (database.type !== 'postgres' || !['127.0.0.1', 'localhost'].includes(database.host)
    || Number(database.port ?? 5432) !== 5432 || source.app?.env === 'production') {
  throw new Error('Node test vault requires a local PostgreSQL test configuration');
}

const password = fs.readFileSync(path.resolve(base, source.secrets.passwordFile), 'utf8').trim();
const secrets = decryptSecrets(loadVault(path.resolve(base, source.secrets.file)), password);
const admin = Wallet.createRandom();
const fixturePassword = randomBytes(32).toString('hex');
secrets.ADMIN_DIDS = admin.address.toLowerCase();
replaceVault(path.join(fixture, 'secrets.enc.json'), encryptSecrets(secrets, fixturePassword));
fs.writeFileSync(path.join(fixture, 'password'), fixturePassword, { mode: 0o600, flag: 'wx' });
process.stdout.write(JSON.stringify({ owner: secrets.DATABASE_USERNAME, admin: admin.privateKey }));
