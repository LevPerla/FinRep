document.addEventListener("keydown", (event) => {
  const row = event.target.closest?.(".finrep-report-row-clickable[role='button']");
  const activatesRow = event.key === "Enter" || event.key === " " || event.code === "Space";
  if (!row || !activatesRow || event.repeat) return;

  event.preventDefault();
  row.click();
});

const labelTransactionDropdowns = () => {
  for (const id of ["transaction-input-date", "transaction-input-amount", "transaction-input-comment"]) {
    document.getElementById(id)?.setAttribute("aria-describedby", "transaction-input-message");
  }
  for (const id of ["transaction-input-category", "transaction-input-currency"]) {
    const container = document.getElementById(id);
    const control = container?.matches("button, [role='combobox'], input")
      ? container
      : container?.querySelector("button, [role='combobox'], input");
    if (!control) continue;
    const valueId = document.getElementById(`${id}-value`) ? ` ${id}-value` : "";
    control.setAttribute("aria-labelledby", `${id}-label${valueId}`);
    control.setAttribute("aria-describedby", "transaction-input-message");
  }
};

new MutationObserver(labelTransactionDropdowns).observe(document.documentElement, {
  childList: true,
  subtree: true,
});
labelTransactionDropdowns();
