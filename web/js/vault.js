// Token at rest. All *.github.io project sites share ONE browser origin, so other apps' scripts can read our
// IndexedDB. Default: the token is encrypted with a user passphrase (PBKDF2-SHA256 600k + AES-GCM) and only
// salt/iv/ciphertext are stored (kv key "vault"). The decrypted token lives in memory for the session only.
// Optional "plain" mode (explicit, warned) stores it as-is. A non-extractable key alone would NOT help, because
// same-origin code could use a stored key too; the passphrase is what protects the token.
import * as store from './store.js';

export const ITER = 600000;
const AAD = new TextEncoder().encode('lessonnotes-token-v1');
const b64 = (u8) => { let s = ''; for (const b of u8) s += String.fromCharCode(b); return btoa(s); };
const unb64 = (s) => Uint8Array.from(atob(s), (c) => c.charCodeAt(0));

async function deriveKey(pass, salt, iter) {
  const km = await crypto.subtle.importKey('raw', new TextEncoder().encode(pass), 'PBKDF2', false, ['deriveKey']);
  return crypto.subtle.deriveKey({ name: 'PBKDF2', hash: 'SHA-256', salt, iterations: iter }, km, { name: 'AES-GCM', length: 256 }, false, ['encrypt', 'decrypt']);
}
export async function seal(token, pass) {
  const salt = crypto.getRandomValues(new Uint8Array(16)), iv = crypto.getRandomValues(new Uint8Array(12));
  const ct = new Uint8Array(await crypto.subtle.encrypt({ name: 'AES-GCM', iv, additionalData: AAD }, await deriveKey(pass, salt, ITER), new TextEncoder().encode(token)));
  return { v: 1, mode: 'locked', kdf: 'PBKDF2-SHA256', iter: ITER, salt: b64(salt), iv: b64(iv), ct: b64(ct) };
}
export async function open(vault, pass) {
  try {
    const pt = await crypto.subtle.decrypt({ name: 'AES-GCM', iv: unb64(vault.iv), additionalData: AAD }, await deriveKey(pass, unb64(vault.salt), vault.iter), unb64(vault.ct));
    return new TextDecoder().decode(pt);
  } catch { throw new Error('wrong passphrase'); }
}
export const load = () => store.kvGet('vault');
export const save = (v) => store.kvSet('vault', v);
export const remove = () => store.kvDel('vault');
export const plainVault = (token) => ({ v: 1, mode: 'plain', token });
