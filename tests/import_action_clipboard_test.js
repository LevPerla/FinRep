const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const listeners = {};
const window = { dashAgGridFunctions: {} };
const document = { addEventListener: (name, handler) => { listeners[name] = handler; } };
vm.runInNewContext(
  fs.readFileSync(path.join(__dirname, "../assets/dashAgGridFunctions.js"), "utf8"),
  { window, document, Set },
);

const { finrepInputCellClicked, finrepInputCellChanged } = window.dashAgGridFunctions;
const rows = ["skip", "review", "review"].map((action, index) => ({
  id: String(index),
  data: { import_action: action, skip_reason: "possible_duplicate" },
  setDataValue(field, value) {
    this.data[field] = value;
    if (field === "import_action") finrepInputCellChanged({
      column, node: this, data: this.data, newValue: value,
    });
  },
}));
const api = {
  refreshCells: () => {},
  forEachNode: (visit) => rows.forEach(visit),
  forEachNodeAfterFilterAndSort: (visit) => rows.forEach(visit),
};
const column = { getColId: () => "import_action", getColDef: () => ({}) };
const click = (index, event = {}) => finrepInputCellClicked({
  api, column, node: rows[index], rowIndex: index, event,
});

let copied = "";
click(0);
listeners.copy({ clipboardData: { setData: (_, value) => { copied = value; } }, preventDefault: () => {} });
assert.equal(copied, "skip");
click(1);
click(2, { shiftKey: true });
listeners.paste({ clipboardData: { getData: () => copied }, preventDefault: () => {} });
assert.deepEqual(rows.map((row) => row.data.import_action), ["skip", "skip", "skip"]);
assert.deepEqual(rows.slice(1).map((row) => row.data.skip_reason), ["manual_skip", "manual_skip"]);

const historyNode = {
  data: {category: "Прочие доходы", history_category: "Проценты"},
  setDataValue(field, value) { this.data[field] = value; },
};
const categoryChanged = () => window.dashAgGridFunctions.finrepCategoryCellChanged({
  column: {getColId: () => "category"}, node: historyNode, data: historyNode.data,
  newValue: historyNode.data.category, colDef: {context: {}}, api,
});
categoryChanged();
assert.equal(historyNode.data.import_action, "review");
assert.equal(historyNode.data.skip_reason, "category_history_conflict");
historyNode.data.category = "Проценты";
categoryChanged();
assert.equal(historyNode.data.import_action, "import");
historyNode.data.duplicate_in_source = true;
categoryChanged();
assert.equal(historyNode.data.import_action, "review");
