// IndexedDB layer (database "lessonnotes"). Rules:
//  * ONE serial queue for all transactions (mutations never interleave),
//  * read-modify-write re-reads the record INSIDE the transaction,
//  * callers apply results to memory only after the returned promise (= tx.oncomplete) resolves,
//  * nothing but IDB requests happens inside a transaction callback.
const DB = 'lessonnotes';
let dbp = null;
let queue = Promise.resolve();

function open() {
  if (dbp) return dbp;
  dbp = new Promise((resolve, reject) => {
    const r = indexedDB.open(DB, 1);
    r.onupgradeneeded = () => {
      const d = r.result;
      d.createObjectStore('kv', { keyPath: 'k' });
      d.createObjectStore('cache', { keyPath: 'path' });
      d.createObjectStore('recs', { keyPath: 'id' });
      d.createObjectStore('chunks', { keyPath: ['rid', 'seq'] });
      d.createObjectStore('blobs', { keyPath: 'id' });
      d.createObjectStore('uploads', { keyPath: 'id' });
      d.createObjectStore('outbox', { keyPath: 'id' });
    };
    r.onsuccess = () => resolve(r.result);
    r.onerror = () => reject(r.error);
  });
  return dbp;
}

// fn(tx) runs synchronously inside the transaction and returns a thunk evaluated after commit.
function run(stores, mode, fn) {
  const p = queue.then(async () => {
    const db = await open();
    return new Promise((resolve, reject) => {
      const tx = db.transaction(stores, mode);
      let thunk;
      tx.oncomplete = () => { try { resolve(thunk ? thunk() : undefined); } catch (e) { reject(e); } };
      tx.onerror = () => reject(tx.error);
      tx.onabort = () => reject(tx.error || new Error('tx aborted'));
      thunk = fn(tx);
    });
  });
  queue = p.catch(() => {});
  return p;
}

export const get = (store, key) => run([store], 'readonly', (tx) => { const r = tx.objectStore(store).get(key); return () => r.result; });
export const getAll = (store) => run([store], 'readonly', (tx) => { const r = tx.objectStore(store).getAll(); return () => r.result; });
export const put = (store, value) => run([store], 'readwrite', (tx) => { tx.objectStore(store).put(value); return () => value; });
export const del = (store, key) => run([store], 'readwrite', (tx) => { tx.objectStore(store).delete(key); });
// fn(current) -> next record | null (delete) | undefined (no change)
export function update(store, key, fn) {
  return run([store], 'readwrite', (tx) => {
    const os = tx.objectStore(store);
    let result;
    const g = os.get(key);
    g.onsuccess = () => {
      const next = fn(g.result);
      if (next === undefined) { result = g.result; return; }
      if (next === null) os.delete(key); else os.put(next);
      result = next;
    };
    return () => result;
  });
}
export const clearStore = (store) => run([store], 'readwrite', (tx) => { tx.objectStore(store).clear(); });

export const kvGet = async (k, dflt = null) => { const r = await get('kv', k); return r ? r.v : dflt; };
// Defense in depth: the 'cfg' record can never carry a token, whatever the caller passes.
export const kvSet = (k, v) => put('kv', { k, v: k === 'cfg' && v && typeof v === 'object' ? (({ token, ...rest }) => rest)(v) : v });
export const kvDel = (k) => del('kv', k);

export function chunksOf(rid) {
  return run(['chunks'], 'readonly', (tx) => {
    const r = tx.objectStore('chunks').getAll(IDBKeyRange.bound([rid, 0], [rid, Infinity]));
    return () => r.result;
  });
}
export function deleteChunks(rid) {
  return run(['chunks'], 'readwrite', (tx) => { tx.objectStore('chunks').delete(IDBKeyRange.bound([rid, 0], [rid, Infinity])); });
}
