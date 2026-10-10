const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const MiB = 1048576;

const handlers = {};
let changeCount = 0;
const input = { files: [], dispatchEvent: () => changeCount++ };
const alert = {
  dataset: { maxFiles: "5", maxFileBytes: String(10 * MiB), maxTotalBytes: String(20 * MiB) },
  style: {},
  textContent: "",
};
const document = {
  addEventListener: (name, handler, capture) => {
    assert.equal(capture, true);
    handlers[name] = handler;
  },
  documentElement: { lang: "ru" },
  getElementById: () => alert,
  querySelector: () => input,
};
vm.runInNewContext(
  fs.readFileSync(path.join(__dirname, "../assets/uploadDropFix.js"), "utf8"),
  { document, Event },
);

const files = Array.from({ length: 5 }, (_, index) => ({ name: `${index}.pdf`, size: MiB }));
let prevented = false;
let stopped = false;
const drop = (selected) => handlers.drop({
  target: { closest: () => ({}) },
  dataTransfer: { files: selected },
  preventDefault: () => { prevented = true; },
  stopImmediatePropagation: () => { stopped = true; },
});
drop(files);
assert.equal(input.files, files);
assert.equal(changeCount, 1);
assert.equal(prevented, true);
assert.equal(stopped, true);
assert.equal(alert.style.display, "none");

drop(files.slice(0, 1));
assert.equal(changeCount, 1);

for (const [invalid, reason] of [
  [[...files, files[0]], "не больше 5"],
  [[{ name: "large.pdf", size: 11 * MiB }], "large.pdf"],
  [files.map((file) => ({ ...file, size: 5 * MiB })), "больше 20 MiB"],
]) {
  drop(invalid);
  assert.match(alert.textContent, new RegExp(reason));
  assert.equal(alert.style.display, "block");
  assert.equal(input.files, files);
}

input.files = [{ name: "large.pdf", size: 11 * MiB }];
input.value = "selected";
input.matches = () => true;
stopped = false;
handlers.change({
  target: input,
  stopImmediatePropagation: () => { stopped = true; },
});
assert.equal(input.value, "");
assert.equal(stopped, true);
assert.match(alert.textContent, /large.pdf/);

document.documentElement.lang = "en";
drop([...files, files[0]]);
assert.match(alert.textContent, /Select at most 5 PDFs/);
