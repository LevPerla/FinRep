var dagfuncs = window.dashAgGridFunctions = window.dashAgGridFunctions || {};

function finrepCategorySelection(api) {
  if (!api.__finrepCategorySelection) {
    api.__finrepCategorySelection = new Set();
  }
  return api.__finrepCategorySelection;
}

function finrepRefreshCategoryCells(api) {
  api.refreshCells({columns: ["category"], force: true});
}

dagfuncs.finrepCategorySelectionReset = function (params) {
  params.api.__finrepCategorySelection = new Set();
  params.api.__finrepCategoryAnchor = null;
  finrepRefreshCategoryCells(params.api);
};

dagfuncs.finrepCategoryCellClicked = function (params) {
  if (!params.column || params.column.getColId() !== "category" || !params.node) {
    return;
  }

  var selection = finrepCategorySelection(params.api);
  var event = params.event || {};
  var rowId = params.node.id;

  if (event.shiftKey && Number.isInteger(params.api.__finrepCategoryAnchor)) {
    selection.clear();
    var start = Math.min(params.api.__finrepCategoryAnchor, params.rowIndex);
    var end = Math.max(params.api.__finrepCategoryAnchor, params.rowIndex);
    params.api.forEachNodeAfterFilterAndSort(function (node, index) {
      if (index >= start && index <= end) {
        selection.add(node.id);
      }
    });
  } else if (event.ctrlKey || event.metaKey) {
    if (selection.has(rowId)) {
      selection.delete(rowId);
    } else {
      selection.add(rowId);
    }
    params.api.__finrepCategoryAnchor = params.rowIndex;
  } else {
    selection.clear();
    selection.add(rowId);
    params.api.__finrepCategoryAnchor = params.rowIndex;
  }

  finrepRefreshCategoryCells(params.api);
};

dagfuncs.finrepCategoryClipboard = function (params) {
  if (!params.column || params.column.getColId() !== "category" || !params.event) {
    return;
  }

  var event = params.event;
  if (!(event.ctrlKey || event.metaKey)) {
    return;
  }

  var key = String(event.key || "").toLowerCase();
  if (key === "c") {
    event.preventDefault();
    navigator.clipboard.writeText(String(params.value || ""));
    return;
  }
  if (key !== "v") {
    return;
  }

  event.preventDefault();
  navigator.clipboard.readText().then(function (clipboardText) {
    var category = String(clipboardText || "").split(/[\t\r\n]/, 1)[0].trim();
    var editorParams = params.column.getColDef().cellEditorParams || {};
    var categories = editorParams.values || [];
    if (!category || !categories.includes(category)) {
      return;
    }

    var selection = finrepCategorySelection(params.api);
    if (selection.size === 0 && params.node) {
      selection.add(params.node.id);
    }
    params.api.forEachNode(function (node) {
      if (selection.has(node.id)) {
        node.setDataValue("category", category);
      }
    });
    finrepRefreshCategoryCells(params.api);
  });
};
