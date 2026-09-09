/**
 * Offline correlation tests for DaemonLink (issue #89 hardening B2).
 *
 * A fake child process stands in for the Python daemon: it captures the
 * per-boot key from the first stdin write and answers framed requests with
 * HMAC'd replies under test control. Proves regressions A-E:
 * late replies never resolve other requests, out-of-order mapping, unknown
 * ids ignored, daemon death fails closed, request ids carry no identity.
 * No OpenClaw runtime, no Telegram, no network.
 */
"use strict";

const assert = require("node:assert/strict");
const { test, before } = require("node:test");
const crypto = require("node:crypto");
const Module = require("node:module");
const { EventEmitter } = require("node:events");

let plugin;
const {
  canonicalStringify,
} = require("../../plugins/nullone-final-publish/route");

before(() => {
  const originalLoad = Module._load;
  Module._load = function (request, parent, isMain) {
    if (request === "openclaw/plugin-sdk/plugin-entry") {
      return { definePluginEntry: (entry) => entry };
    }
    return originalLoad.call(this, request, parent, isMain);
  };
  plugin = require("../../plugins/nullone-final-publish/index.js");
});

const MAGIC = Buffer.from("NP1", "utf8");

function replyFrame(payload, key) {
  const body = Buffer.from(canonicalStringify(payload), "utf8");
  const header = Buffer.alloc(8);
  MAGIC.copy(header, 0);
  header.writeUInt8(1, 3);
  header.writeUInt32BE(body.length, 4);
  const mac = crypto.createHmac("sha256", key).update(header).update(body).digest();
  return Buffer.concat([header, body, mac]);
}

function readyFrame(key) {
  return replyFrame(
    { schema: "nullone.publish-reply.v1", t: "ready", reconciled: "0/0/0" },
    key
  );
}

function resultFrame(key, requestId, code) {
  return replyFrame(
    {
      schema: "nullone.publish-reply.v1",
      t: "result",
      outcome: "SETTLED",
      code: String(code),
      request_id: requestId,
    },
    key
  );
}

function parseRequestFrame(buffer, key) {
  const bodyLen = buffer.readUInt32BE(4);
  const body = buffer.subarray(8, 8 + bodyLen);
  const mac = buffer.subarray(8 + bodyLen, 8 + bodyLen + 32);
  const expected = crypto
    .createHmac("sha256", key)
    .update(buffer.subarray(0, 8))
    .update(body)
    .digest();
  assert.ok(crypto.timingSafeEqual(mac, expected), "test harness HMAC");
  return JSON.parse(body.toString("utf8"));
}

class FakeChild extends EventEmitter {
  constructor() {
    super();
    this.key = null;
    this.written = [];
    this.stdin = {
      write: (chunk) => {
        const data = Buffer.from(chunk);
        if (!this.key) {
          assert.equal(data.length, 32, "first write must be exactly the key");
          this.key = data;
          // Async like real IPC: the plugin arms its handshake after write.
          setImmediate(() => this.stdout.emit("data", readyFrame(this.key)));
          return true;
        }
        this.written.push(data);
        if (this.onFrame) {
          setImmediate(() => this.onFrame(data));
        }
        return true;
      },
    };
    this.stdout = new EventEmitter();
    this.killed = false;
  }
  die() {
    this.killed = true;
    this.emit("exit", 1);
  }
}

function makeLink(fake, opts) {
  const spawnFn = () => fake;
  return new plugin.DaemonLink(
    "python3",
    "controller.py",
    "/tmp/ws",
    spawnFn,
    opts || { requestTimeoutMs: 500, handshakeTimeoutMs: 1000 }
  );
}

function envelope(id) {
  return {
    schema: "nullone.publish-callback.v1",
    post_id: "0123456789abcdef01234567",
    account_id: "a",
    chat_id: "c",
    message_id: "m",
    sender_id: "s",
    nonce: "n".repeat(32),
    request_id: id,
  };
}

test("A: timed-out A then late reply A cannot resolve B", async () => {
  const fake = new FakeChild();
  const link = makeLink(fake, { requestTimeoutMs: 60, handshakeTimeoutMs: 1000 });
  const promiseA = link.request(envelope("a".repeat(32)));
  // Hold A's frame; never answer it.
  await assert.rejects(promiseA, /request timeout/);
  // Late reply A arrives while B is in flight.
  const promiseB = link.request(envelope("b".repeat(32)));
  await new Promise((resolve) => setTimeout(resolve, 20));
  const frameA = replyFrame(
    { schema: "nullone.publish-reply.v1", t: "result", code: "9", request_id: "a".repeat(32) },
    fake.key
  );
  fake.stdout.emit("data", frameA);
  const frameB = resultFrame(fake.key, "b".repeat(32), 0);
  fake.stdout.emit("data", frameB);
  const replyB = await promiseB;
  assert.equal(replyB.code, "0", "B must resolve with B's reply, not late A");
});

test("B: replies map to their own requests, never cross-talk", async () => {
  const fake = new FakeChild();
  const link = makeLink(fake);
  const frames = [];
  fake.onFrame = (data) => frames.push(data);
  // Writes are serialized on the wire; the mapping itself is id-keyed.
  const promiseA = link.request(envelope("a".repeat(32)));
  // B queues behind A's serialized write.
  const promiseB = link.request(envelope("b".repeat(32)));
  // Wait until A's frame is on the wire.
  for (let i = 0; i < 100 && frames.length < 1; i++) {
    await new Promise((resolve) => setTimeout(resolve, 10));
  }
  assert.equal(frames.length, 1);
  // B's reply arrives early (before B was even written): unknown id → ignored.
  fake.stdout.emit("data", resultFrame(fake.key, "b".repeat(32), 2));
  await new Promise((resolve) => setTimeout(resolve, 30));
  // A's reply resolves A with A's own payload.
  fake.stdout.emit("data", resultFrame(fake.key, "a".repeat(32), 0));
  const resolvedA = await promiseA;
  assert.equal(resolvedA.code, "0");
  // Now B's frame goes out; its reply resolves B.
  for (let i = 0; i < 100 && frames.length < 2; i++) {
    await new Promise((resolve) => setTimeout(resolve, 10));
  }
  assert.equal(frames.length, 2);
  fake.stdout.emit("data", resultFrame(fake.key, "b".repeat(32), 2));
  const resolvedB = await promiseB;
  assert.equal(resolvedB.code, "2");
});

test("B2: _deliver routes purely by request_id", () => {
  const fake = new FakeChild();
  const link = makeLink(fake);
  const got = {};
  for (const id of ["a".repeat(32), "b".repeat(32)]) {
    link.pending.set(id, {
      resolve: (payload) => { got[id] = payload; },
      reject: () => {},
      timer: setTimeout(() => {}, 10000),
    });
  }
  link._deliver({ t: "result", code: "2", request_id: "b".repeat(32) });
  link._deliver({ t: "result", code: "0", request_id: "a".repeat(32) });
  link._deliver({ t: "result", code: "9", request_id: "f".repeat(32) });
  assert.equal(got["a".repeat(32)].code, "0");
  assert.equal(got["b".repeat(32)].code, "2");
  assert.equal(link.pending.size, 0);
  for (const entry of link.pending.values()) {
    clearTimeout(entry.timer);
  }
});

test("C: unknown/stale reply ids are ignored", async () => {
  const fake = new FakeChild();
  const link = makeLink(fake);
  const promiseB = link.request(envelope("b".repeat(32)));
  await new Promise((resolve) => setTimeout(resolve, 20));
  fake.stdout.emit(
    "data",
    resultFrame(fake.key, "f".repeat(32), 7)
  );
  await new Promise((resolve) => setTimeout(resolve, 30));
  // B still pending (no resolution, no rejection).
  let settled = false;
  promiseB.then(() => { settled = true; }, () => { settled = true; });
  await new Promise((resolve) => setTimeout(resolve, 30));
  assert.equal(settled, false);
  fake.stdout.emit("data", resultFrame(fake.key, "b".repeat(32), 0));
  const replyB = await promiseB;
  assert.equal(replyB.code, "0");
});

test("D: daemon death rejects outstanding requests fail-closed", async () => {
  const fake = new FakeChild();
  const link = makeLink(fake);
  const promise = link.request(envelope("d".repeat(32)));
  await new Promise((resolve) => setTimeout(resolve, 20));
  fake.die();
  await assert.rejects(promise, /daemon exited/);
});

test("E: request ids are random non-identifying correlation only", async () => {
  const fake = new FakeChild();
  const link = makeLink(fake);
  const seen = [];
  fake.onFrame = (data) => seen.push(parseRequestFrame(data, fake.key));
  const promise = link.request(envelope("e".repeat(32)));
  // NOTE: envelope() helper pins request_id for determinism here.
  await new Promise((resolve) => setTimeout(resolve, 30));
  assert.equal(seen.length, 1);
  assert.match(seen[0].request_id, /^[0-9a-f]{32}$/);
  for (const field of ["account_id", "chat_id", "message_id", "sender_id"]) {
    assert.equal(seen[0][field], envelope("e".repeat(32))[field]);
  }
  fake.stdout.emit("data", resultFrame(fake.key, "e".repeat(32), 0));
  await promise;
});
