/* Visitor-local review snapshots. All compare-and-swap decisions and writes run
 * inside ONE IndexedDB readwrite transaction. Never fall back to server globals
 * or localStorage (which cannot make a read/compare/write atomic across tabs).
 */
const DATABASE = "preflight.review_queue";
const STORE = "snapshots";
const MAX_BYTES = 30000000;

function invalid() {
  const error = new Error("Invalid review storage record");
  error.name = "ReviewStorageFormatError";
  return error;
}

function validRecord(record, namespace) {
  return record && record.namespace === namespace && record.version === 1 &&
    Number.isSafeInteger(record.revision) && record.revision > 0 &&
    typeof record.jsonl === "string" && new TextEncoder().encode(record.jsonl).length <= MAX_BYTES &&
    typeof record.write_id === "string" && record.write_id.length > 0;
}

function openDatabase() {
  return new Promise((resolve, reject) => {
    let finished = false;
    const timer = setTimeout(() => {
      finished = true;
      reject(new DOMException("Storage did not open", "InvalidStateError"));
    }, 8000);
    let request;
    try {
      request = indexedDB.open(DATABASE, 1);
    } catch (error) {
      clearTimeout(timer);
      reject(error);
      return;
    }
    request.onupgradeneeded = () => request.result.createObjectStore(STORE, {keyPath: "namespace"});
    request.onerror = () => { clearTimeout(timer); reject(request.error); };
    request.onblocked = () => { clearTimeout(timer); finished = true; reject(new DOMException("Storage upgrade blocked", "InvalidStateError")); };
    request.onsuccess = () => {
      clearTimeout(timer);
      if (finished) { request.result.close(); return; }
      resolve(request.result);
    };
  });
}

function transact(db, command, namespace) {
  return new Promise((resolve, reject) => {
    const mode = command.op === "save" ? "readwrite" : "readonly";
    let transaction;
    // Ask supporting browsers to flush a write before confirming completion.
    // Older browsers still provide atomic IndexedDB transaction completion.
    try {
      transaction = db.transaction(STORE, mode, {durability: "strict"});
    } catch (error) {
      if (!(error instanceof TypeError)) { reject(error); return; }
      transaction = db.transaction(STORE, mode);
    }
    let response;
    transaction.oncomplete = () => resolve(response);
    transaction.onabort = () => reject(transaction.error || new DOMException("Storage transaction aborted", "AbortError"));
    transaction.onerror = () => reject(transaction.error || new DOMException("Storage transaction failed", "UnknownError"));
    const store = transaction.objectStore(STORE);
    const read = store.get(namespace);
    read.onsuccess = () => {
      try {
        const current = read.result;
        if (current !== undefined && !validRecord(current, namespace)) throw invalid();
        const revision = current ? current.revision : 0;
        if (command.op === "load") {
          response = {id: command.id, status: "loaded", revision, jsonl: current ? current.jsonl : null};
          return;
        }
        if (current && current.write_id === command.write_id && current.jsonl === command.jsonl && revision === command.expected_revision + 1) {
          response = {id: command.id, status: "saved", revision};
          return;
        }
        if (revision !== command.expected_revision) {
          response = {id: command.id, status: "conflict", revision};
          return;
        }
        store.put({namespace, version: 1, revision: revision + 1, jsonl: command.jsonl, write_id: command.write_id});
        response = {id: command.id, status: "saved", revision: revision + 1};
      } catch (error) {
        // A quota/format failure must not leave a partially changed record.
        transaction.abort();
        reject(error);
      }
    };
  });
}

export async function execute(command, namespace) {
  let db;
  const id = command && typeof command.id === "string" ? command.id : "";
  try {
    if (!command || !id || id.length > 100 || !/^[a-zA-Z0-9_.:-]{1,128}$/.test(namespace) || !["load", "save"].includes(command.op)) throw invalid();
    if (command.op === "save" && (
      !Number.isSafeInteger(command.expected_revision) || command.expected_revision < 0 || command.expected_revision >= Number.MAX_SAFE_INTEGER ||
      typeof command.write_id !== "string" || !command.write_id || command.write_id.length > 100 ||
      typeof command.jsonl !== "string" || new TextEncoder().encode(command.jsonl).length > MAX_BYTES
    )) throw invalid();
    db = await openDatabase();
    return await transact(db, command, namespace);
  } catch (error) {
    const kind = ({QuotaExceededError: "quota", SecurityError: "denied", NotAllowedError: "denied", AbortError: "aborted", ReviewStorageFormatError: "invalid"})[error && error.name] || "unavailable";
    return {id, status: "error", error: kind};
  } finally {
    if (db) db.close();
  }
}
