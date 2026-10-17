// Runs chat.html's own script against a minimal fake DOM, for test_server.py.
//
//   node chat_page_harness.js chat.html
//
// It plays one session (set a token, ask, get an answer; ask again, get a 401;
// ask without a token) and prints JSON describing what the page built: every
// element by tag, class and text, and the requests it sent.
"use strict";

const fs = require("fs");
const vm = require("vm");

const page = fs.readFileSync(process.argv[2], "utf8");
const scripts = [...page.matchAll(/<script>([\s\S]*?)<\/script>/g)].map((m) => m[1]);
if (scripts.length !== 1) throw new Error(`expected one script, found ${scripts.length}`);

class Text {
  constructor(text) { this.nodeType = 3; this.data = String(text); }
  get textContent() { return this.data; }
}

class Element {
  constructor(tag) {
    this.nodeType = 1;
    this.tagName = tag.toUpperCase();
    this.className = "";
    this.childNodes = [];
    this.listeners = {};
    this.value = "";
    this.disabled = false;
    this.readOnly = false;
  }
  appendChild(child) { this.childNodes.push(child); return child; }
  get textContent() { return this.childNodes.map((c) => c.textContent).join(""); }
  set textContent(text) { this.childNodes = text === "" ? [] : [new Text(text)]; }
  addEventListener(type, fn) { (this.listeners[type] = this.listeners[type] || []).push(fn); }
  dispatch(type, extra) {
    const event = Object.assign({ preventDefault() {} }, extra);
    (this.listeners[type] || []).forEach((fn) => fn(event));
  }
  requestSubmit() { this.dispatch("submit"); }
  focus() {}
}

const ids = {};
for (const [id, tag] of [["token-form", "form"], ["token", "input"], ["token-state", "span"],
                         ["ask-form", "form"], ["question", "textarea"], ["send", "button"],
                         ["log", "div"]]) {
  ids[id] = new Element(tag);
}

const document = {
  body: { scrollHeight: 0 },
  getElementById: (id) => ids[id],
  createElement: (tag) => new Element(tag),
  createTextNode: (text) => new Text(text),
};

const requests = [];
const responses = [];
function fetch(url, options) {
  requests.push({ url, method: options.method, headers: options.headers, body: options.body });
  const next = responses.shift();
  return Promise.resolve({
    ok: next.status >= 200 && next.status < 300,
    status: next.status,
    text: () => Promise.resolve(JSON.stringify(next.body)),
  });
}

const sandbox = {
  document, fetch, AbortController, Date, JSON, Math, String, Array, Object,
  setTimeout: () => 0, clearTimeout: () => {}, setInterval: () => 0, clearInterval: () => {},
  window: { scrollTo() {} },
};
vm.runInNewContext(scripts[0], sandbox);

function tree(node) {
  if (node.nodeType === 3) return { text: node.data };
  return { tag: node.tagName.toLowerCase(), class: node.className,
           children: node.childNodes.map(tree) };
}

const settle = () => new Promise((resolve) => setImmediate(resolve));

async function ask(question) {
  ids.question.value = question;
  ids["ask-form"].dispatch("submit");
  for (let i = 0; i < 5; i++) await settle();
}

(async () => {
  const out = {};

  // A token pasted with spaces and line breaks around it
  ids.token.value = "  abc-123_XYZ\r\n";
  ids["token-form"].dispatch("submit");
  out.tokenFieldAfterSet = ids.token.value;
  out.statusAfterSet = ids["token-state"].textContent;

  responses.push({ status: 200, body: JSON.parse(process.argv[3] || "{}") });
  await ask("Is it healthy?");
  out.firstRequest = requests[0];
  out.reply = tree(ids.log.childNodes[0].childNodes[1]);
  out.sendDisabledAfterAnswer = ids.send.disabled;

  responses.push({ status: 401, body: { detail: "invalid or missing bearer token" } });
  await ask("Again?");
  out.errorReply = tree(ids.log.childNodes[1].childNodes[1]);
  out.statusAfter401 = ids["token-state"].textContent;
  out.statusClassAfter401 = ids["token-state"].className;

  await ask("Without a token?");
  out.requestsAfterNoToken = requests.length;
  out.statusAfterNoToken = ids["token-state"].textContent;

  process.stdout.write(JSON.stringify(out));
})().catch((err) => { console.error(err); process.exit(1); });
