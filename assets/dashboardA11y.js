document.addEventListener("keydown", (event) => {
  const row = event.target.closest?.(".finrep-report-row-clickable[role='button']");
  const activatesRow = event.key === "Enter" || event.key === " " || event.code === "Space";
  if (!row || !activatesRow || event.repeat) return;

  event.preventDefault();
  row.click();
});
