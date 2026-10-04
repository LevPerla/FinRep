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

function finrepCategoriesForRow(params) {
  var categoryContext = params.colDef?.context || {};
  var directional = params.data?.direction === "credit"
    ? (categoryContext.incomeCategories || [])
    : (categoryContext.expenseCategories || []);
  var neutral = categoryContext.neutralCategories || [];
  return directional.concat(neutral.filter(function (category) {
    return !directional.includes(category);
  }));
}

dagfuncs.finrepCategoryEditorParams = function (params) {
  return {values: finrepCategoriesForRow(params)};
};

function finrepTruthy(value) {
  return value === true || ["true", "1", "yes"].includes(
    String(value || "").toLowerCase()
  );
}

function finrepRestoreImportDecision(data) {
  if (finrepTruthy(data.duplicate_in_staging)) {
    return ["skip", "duplicate_in_staging"];
  }
  if (finrepTruthy(data.duplicate_in_source)) {
    return ["review", "possible_duplicate"];
  }
  if (finrepTruthy(data.possible_pending_match)) {
    return ["review", "possible_pending_match"];
  }
  if (String(data.replaces_source_id || "")) {
    return ["import", "replaces_pending"];
  }
  return ["import", ""];
}

dagfuncs.finrepCategoryCellChanged = function (params) {
  if (!params.column || params.column.getColId() !== "category" || !params.node) {
    return;
  }
  var neutral = params.colDef?.context?.neutralCategories || [];
  var decision = neutral.includes(params.newValue)
    ? ["skip", "internal_transfer"]
    : finrepRestoreImportDecision(params.data || {});
  params.node.setDataValue("import_action", decision[0]);
  params.node.setDataValue("skip_reason", decision[1]);
  params.api.refreshCells({
    rowNodes: [params.node],
    columns: ["category", "import_action", "skip_reason"],
    force: true
  });
};

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

  window.__finrepCategoryClipboardContext = params;
  finrepRefreshCategoryCells(params.api);
};

function finrepActiveCategoryContext() {
  return window.__finrepCategoryClipboardContext || null;
}

function finrepCopyCategory(event) {
  var context = finrepActiveCategoryContext();
  if (!context || !event.clipboardData) {
    return;
  }
  event.clipboardData.setData(
    "text/plain",
    String(context.node?.data?.category || context.value || "")
  );
  event.preventDefault();
}

function finrepPasteCategory(event) {
  var context = finrepActiveCategoryContext();
  if (!context || !event.clipboardData) {
    return;
  }
  var category = String(event.clipboardData.getData("text/plain") || "")
    .split(/[\t\r\n]/, 1)[0]
    .trim();
  var categories = finrepCategoriesForRow(context);
  if (!category || !categories.includes(category)) {
    return;
  }

  event.preventDefault();
  var selection = finrepCategorySelection(context.api);
  if (selection.size === 0 && context.node) {
    selection.add(context.node.id);
  }
  context.api.forEachNode(function (node) {
    var nodeParams = {
      colDef: context.column.getColDef(),
      data: node.data
    };
    if (selection.has(node.id) && finrepCategoriesForRow(nodeParams).includes(category)) {
      node.setDataValue("category", category);
    }
  });
  finrepRefreshCategoryCells(context.api);
}

if (!window.__finrepCategoryClipboardListenersInstalled) {
  document.addEventListener("click", function (event) {
    var grid = document.getElementById("kaspi-import-grid");
    if (!grid || !event.target || !grid.contains(event.target)) {
      window.__finrepCategoryClipboardContext = null;
    }
  });
  document.addEventListener("copy", finrepCopyCategory);
  document.addEventListener("paste", finrepPasteCategory);
  window.__finrepCategoryClipboardListenersInstalled = true;
}
