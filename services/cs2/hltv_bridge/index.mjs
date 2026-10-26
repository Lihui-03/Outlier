/**
 * Thin CLI bridge: Python calls this with a JSON argument, gets JSON back.
 *
 * Usage:
 *   node index.mjs '{"method":"getResults","args":{"stars":1}}'
 *
 * Stdout: {"ok":true,"data":[...]}   on success
 *         {"ok":false,"error":"..."}  on failure
 *
 * Exit code 0 on success, 1 on error (Python checks both stdout and exit code).
 *
 * Anti-abuse note: all request throttling is handled by the Python caller
 * (hltv_provider.py). This bridge is stateless and does exactly one HLTV
 * call per invocation — no loops, no pagination by default.
 */

import { HLTV } from 'hltv';

function die(msg) {
  process.stdout.write(JSON.stringify({ ok: false, error: String(msg) }) + '\n');
  process.exit(1);
}

// --- parse argument -----------------------------------------------------------
const rawArg = process.argv[2] ?? '{}';
let cmd;
try {
  cmd = JSON.parse(rawArg);
} catch (e) {
  die(`Invalid JSON argument: ${e.message}`);
}

const { method, args = {} } = cmd;

if (!method) die('Missing "method" field in argument.');

const fn = HLTV[method];
if (typeof fn !== 'function') die(`Unknown HLTV method: "${method}"`);

// --- call HLTV ---------------------------------------------------------------
try {
  const data = await fn.call(HLTV, args);
  // Use a replacer to drop undefined values (JSON.stringify omits them, but
  // some HLTV fields are undefined rather than null).
  const out = JSON.stringify({ ok: true, data }, (_k, v) =>
    v === undefined ? null : v
  );
  process.stdout.write(out + '\n');
} catch (e) {
  die(e?.message ?? String(e));
}
