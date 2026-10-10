const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

let onDrop;
let changeCount = 0;
const input = { files: [], dispatchEvent: () => changeCount++ };
const document = {
  addEventListener: (name, handler, capture) => {
    assert.equal(name, "drop");
    assert.equal(capture, true);
    onDrop = handler;
  },
  querySelector: () => input,
};
vm.runInNewContext(
  fs.readFileSync(path.join(__dirname, "../assets/uploadDropFix.js"), "utf8"),
  { document, Event },
);

const files = Array.from({ length: 5 }, (_, index) => ({ name: `${index}.pdf` }));
let prevented = false;
let stopped = false;
onDrop({
  target: { closest: () => ({}) },
  dataTransfer: { files },
  preventDefault: () => { prevented = true; },
  stopImmediatePropagation: () => { stopped = true; },
});
assert.equal(input.files, files);
assert.equal(changeCount, 1);
assert.equal(prevented, true);
assert.equal(stopped, true);

onDrop({ target: { closest: () => ({}) }, dataTransfer: { files: files.slice(0, 1) } });
assert.equal(changeCount, 1);
