#!/usr/bin/env node
// scripts/set-password.mjs — mint the verifier Lodestar boots with.
//
//   npm run auth:setup
//
// Prints one line for the ignored .env:
//
//   LODESTAR_AUTH_PASSWORD_HASH=scrypt$1$16384$8$1$…$…
//
// The password itself is never an argument. That is not fussiness: an argument
// is visible in `ps` to every other user on the machine, it is written to
// ~/.zsh_history verbatim, and it ends up in any shell transcript the terminal
// keeps. So it is read from stdin — with the terminal's echo switched off when
// there is a terminal — and the plaintext leaves this process only as a scrypt
// digest. Nothing here writes a file: the operator pastes the line where they
// want it, which keeps this script incapable of clobbering a .env it did not
// write.
import { hashPassword, SCRYPT_PARAMS, MIN_PASSWORD_LENGTH }
  from '../auth/local-auth.mjs';

if (process.argv.slice(2).some((a) => !a.startsWith('-'))) {
  console.error(
    'Refusing a password given as an argument — `ps` and your shell history '
    + 'would both keep it. Run `npm run auth:setup` with no arguments and type '
    + 'it at the prompt.');
  process.exit(2);
}

// Written as codes rather than literals: a raw ^C in a source file is a
// character no diff, review or terminal renders honestly.
const ETX = String.fromCharCode(3);    // ^C
const EOT = String.fromCharCode(4);    // ^D
const DEL = String.fromCharCode(127);  // what most terminals send for Backspace
const BS = String.fromCharCode(8);

/** One line from stdin. On a terminal the characters are consumed raw so the
 *  password never appears on screen or in a scrollback buffer; off a terminal
 *  (a pipe, a test) there is nothing to echo and the line is simply read. */
function readSecret(prompt) {
  return new Promise((resolve, reject) => {
    const { stdin, stderr } = process;
    // The prompt goes to stderr so `npm run auth:setup > line.txt` captures
    // the hash alone.
    stderr.write(prompt);
    const tty = Boolean(stdin.isTTY);
    let buf = '';
    let settled = false;
    const done = (value) => {
      if (settled) return;
      settled = true;
      stdin.off('data', onData);
      if (tty) { stdin.setRawMode(false); stdin.pause(); }
      stderr.write('\n');
      resolve(value);
    };
    const onData = (chunk) => {
      if (!tty) {
        buf += chunk.toString('utf8');
        const nl = buf.indexOf('\n');
        if (nl !== -1) done(buf.slice(0, nl).replace(/\r$/, ''));
        return;
      }
      for (const ch of chunk.toString('utf8')) {
        if (ch === '\n' || ch === '\r') { done(buf); return; }
        // ^C and ^D at a password prompt mean "stop", not "submit".
        if (ch === ETX || ch === EOT) { stdin.setRawMode(false); process.exit(130); }
        if (ch === DEL || ch === BS) buf = buf.slice(0, -1);
        else buf += ch;
      }
    };
    if (tty) stdin.setRawMode(true);
    stdin.resume();
    stdin.on('data', onData);
    stdin.on('error', reject);
    // A pipe that closes without a newline still gave us a password.
    stdin.on('end', () => done(buf.replace(/\r$/, '')));
  });
}

const password = await readSecret('New Lodestar password: ');
if (password.length < MIN_PASSWORD_LENGTH) {
  console.error(`Too short — use at least ${MIN_PASSWORD_LENGTH} characters.`);
  process.exit(1);
}
if (process.stdin.isTTY) {
  const again = await readSecret('Again: ');
  if (again !== password) {
    console.error('The two entries differ. Nothing was changed.');
    process.exit(1);
  }
}

const hash = hashPassword(password);
// stdout carries the line and nothing else, so it can be redirected or piped.
console.log(`LODESTAR_AUTH_PASSWORD_HASH=${hash}`);
process.stderr.write(
  `\nscrypt N=${SCRYPT_PARAMS.N} r=${SCRYPT_PARAMS.r} p=${SCRYPT_PARAMS.p}. `
  + 'Put that line in .env (already git-ignored), then start the server.\n'
  + 'The password is not stored anywhere — losing it means running this again.\n');
