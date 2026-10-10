var dagfuncs = window.dashAgGridFunctions = window.dashAgGridFunctions || {};

function finrepCellSelection(api) {
  if (!api.__finrepCellSelection) {
    api.__finrepCellSelection = new Set();
  }
  return api.__finrepCellSelection;
}

function finrepRefreshSelectedCells(api) {
  api.refreshCells({columns: ["category", "import_action"], force: true});
}

function finrepCategoriesForRow(params) {
  var categoryContext = params.colDef?.context || {};
  var direction = params.data?.direction;
  var directional = direction === "credit"
    ? (categoryContext.incomeCategories || [])
    : direction === "debit"
      ? (categoryContext.expenseCategories || [])
      : (categoryContext.incomeCategories || []).concat(
          categoryContext.expenseCategories || []
        ).filter(function (category, index, categories) {
          return categories.indexOf(category) === index;
        });
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
  if (["Возникновение дебиторской задолженности", "Возникновение кредиторской задолженности",
       "Дебиторская задолженность", "Кредиторская задолженность"].includes(params.newValue)) {
    params.node.setDataValue("debt_id", "");
  } else if (["Погашение дебиторской задолженности", "Погашение кредиторской задолженности",
              "Погашение деб. зад.", "Погашение кред. зад."].includes(params.newValue)) {
    params.node.setDataValue("counterparty", "");
  }
  params.api.refreshCells({
    rowNodes: [params.node],
    columns: ["category", "import_action", "skip_reason"],
    force: true
  });
};

dagfuncs.finrepInputCellChanged = function (params) {
  if (!params.column || !params.node || !params.data) {
    return;
  }
  var columnId = params.column.getColId();
  if (["date", "category", "amount", "currency", "comment", "import_action", "counterparty", "debt_id"].includes(columnId)
      && params.data.validation_error) {
    params.node.setDataValue("validation_error", "");
  }
  if (columnId === "import_action") {
    params.node.setDataValue("skip_reason", params.newValue === "skip" ? "manual_skip" : "");
    return;
  }
  if (columnId !== "amount" || params.data.source !== "manual_grid") {
    return;
  }
  var raw = String(params.newValue ?? "").replace(/\s/g, "").replace(",", ".");
  var amount = Number(raw);
  if (!Number.isFinite(amount) || amount === 0) {
    if (params.data.direction) {
      params.node.setDataValue("direction", "");
    }
    return;
  }
  var direction = amount > 0 ? "credit" : "debit";
  if (params.data.direction !== direction) {
    params.node.setDataValue("direction", direction);
  }
  var allowedCategories = finrepCategoriesForRow({
    colDef: params.colDef,
    data: {direction: direction}
  });
  if (params.data.category && !allowedCategories.includes(params.data.category)) {
    params.node.setDataValue("category", "");
    params.node.setDataValue("import_action", "import");
    params.node.setDataValue("skip_reason", "");
  }
  var normalizedAmount = raw.replace(/^[+-]/, "");
  if (String(params.data.amount) !== normalizedAmount) {
    params.data.amount = normalizedAmount;
  }
  params.api.refreshCells({rowNodes: [params.node], columns: ["amount", "category"], force: true});
};

dagfuncs.finrepInputSelectionReset = function (params) {
  params.api.__finrepCellSelection = new Set();
  params.api.__finrepCellAnchor = null;
  params.api.__finrepSelectedColumn = null;
  finrepRefreshSelectedCells(params.api);
};

dagfuncs.finrepInputCellClicked = function (params) {
  var columnId = params.column?.getColId();
  if (!params.node || !["category", "import_action"].includes(columnId)) {
    window.__finrepInputClipboardContext = null;
    return;
  }

  var selection = finrepCellSelection(params.api);
  if (params.api.__finrepSelectedColumn !== columnId) {
    selection.clear();
    params.api.__finrepCellAnchor = null;
  }
  params.api.__finrepSelectedColumn = columnId;
  var event = params.event || {};
  var rowId = params.node.id;

  if (event.shiftKey && Number.isInteger(params.api.__finrepCellAnchor)) {
    selection.clear();
    var start = Math.min(params.api.__finrepCellAnchor, params.rowIndex);
    var end = Math.max(params.api.__finrepCellAnchor, params.rowIndex);
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
    params.api.__finrepCellAnchor = params.rowIndex;
  } else {
    selection.clear();
    selection.add(rowId);
    params.api.__finrepCellAnchor = params.rowIndex;
  }

  window.__finrepInputClipboardContext = params;
  finrepRefreshSelectedCells(params.api);
};

function finrepActiveInputContext() {
  return window.__finrepInputClipboardContext || null;
}

function finrepCopyInputCell(event) {
  var context = finrepActiveInputContext();
  if (!context || !event.clipboardData) {
    return;
  }
  event.clipboardData.setData(
    "text/plain",
    String(context.node?.data?.[context.column.getColId()] || context.value || "")
  );
  event.preventDefault();
}

function finrepPasteInputCell(event) {
  var context = finrepActiveInputContext();
  if (!context || !event.clipboardData) {
    return;
  }
  var value = String(event.clipboardData.getData("text/plain") || "")
    .split(/[\t\r\n]/, 1)[0]
    .trim();
  var columnId = context.column.getColId();
  if (columnId === "import_action" ? !["import", "skip"].includes(value)
      : !value || !finrepCategoriesForRow(context).includes(value)) {
    return;
  }

  event.preventDefault();
  var selection = finrepCellSelection(context.api);
  if (selection.size === 0 && context.node) {
    selection.add(context.node.id);
  }
  context.api.forEachNode(function (node) {
    var nodeParams = {
      colDef: context.column.getColDef(),
      data: node.data
    };
    if (selection.has(node.id) && (columnId === "import_action"
        || finrepCategoriesForRow(nodeParams).includes(value))) {
      node.setDataValue(columnId, value);
    }
  });
  finrepRefreshSelectedCells(context.api);
}

if (!window.__finrepCategoryClipboardListenersInstalled) {
  document.addEventListener("click", function (event) {
    var grid = document.getElementById("kaspi-import-grid");
    if (!grid || !event.target || !grid.contains(event.target)) {
      window.__finrepInputClipboardContext = null;
    }
  });
  document.addEventListener("copy", finrepCopyInputCell);
  document.addEventListener("paste", finrepPasteInputCell);
  window.__finrepCategoryClipboardListenersInstalled = true;
}
