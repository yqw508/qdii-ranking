"use strict";
const COLLECTION = "qdii_premium_cache";
function createStore(db) {
  const read = async doc => {
    const result = await doc.get();
    const value = Array.isArray(result.data) ? result.data[0] : result.data;
    if (!value) return null;
    const { _id, ...state } = value;
    return state;
  };
  return {
    async claim(key, owner, now, seed) {
      return db.runTransaction(async tx => {
        const doc = tx.collection(COLLECTION).doc(key);
        const state = await read(doc) || seed;
        if (state.lock_until > now) return { acquired: false, state, busy: true };
        if (state.next_attempt > now) return { acquired: false, state, busy: false };
        const claimed = { ...state, lock_until: now + 35000, lock_owner: owner };
        await doc.set(claimed);
        return { acquired: true, state: claimed };
      });
    },
    async finish(key, owner, state) {
      return db.runTransaction(async tx => {
        const doc = tx.collection(COLLECTION).doc(key);
        const current = await read(doc);
        if (current?.lock_owner !== owner) return current || state;
        await doc.set(state);
        return state;
      });
    }
  };
}
module.exports = { createStore, COLLECTION };
