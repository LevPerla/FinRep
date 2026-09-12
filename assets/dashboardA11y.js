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

const syncDashboardSettings = () => {
  const settings = document.getElementById("dashboard-settings");
  if (!settings) return;

  const viewportMode = window.matchMedia("(max-width: 768px)").matches ? "mobile" : "desktop";
  if (settings.dataset.viewportMode === viewportMode) return;

  settings.open = viewportMode === "desktop";
  settings.dataset.viewportMode = viewportMode;
};

const refreshDashboardEnhancements = () => {
  labelTransactionDropdowns();
  syncDashboardSettings();
};

new MutationObserver(refreshDashboardEnhancements).observe(document.documentElement, {
  childList: true,
  subtree: true,
});
window.addEventListener("resize", syncDashboardSettings);
refreshDashboardEnhancements();
