const checkBankUploadFiles = (files) => {
  const alert = document.getElementById("bank-upload-client-error");
  if (!alert) return false;
  const selected = Array.from(files);
  const maxFiles = Number(alert.dataset.maxFiles);
  const maxFileBytes = Number(alert.dataset.maxFileBytes);
  const maxTotalBytes = Number(alert.dataset.maxTotalBytes);
  const en = document.documentElement.lang === "en";
  const oversized = selected.find((file) => file.size > maxFileBytes);
  let error = "";
  if (selected.length > maxFiles) {
    error = en ? `Select at most ${maxFiles} PDFs at once.` : `Выбери не больше ${maxFiles} PDF за раз.`;
  } else if (oversized) {
    error = en
      ? `${oversized.name}: PDF exceeds ${maxFileBytes / 1048576} MiB.`
      : `${oversized.name}: PDF больше ${maxFileBytes / 1048576} MiB.`;
  } else if (selected.reduce((sum, file) => sum + file.size, 0) > maxTotalBytes) {
    error = en
      ? `Total PDF size exceeds ${maxTotalBytes / 1048576} MiB.`
      : `Общий размер PDF больше ${maxTotalBytes / 1048576} MiB.`;
  }
  alert.textContent = error;
  alert.style.display = error ? "block" : "none";
  return Boolean(error);
};

document.addEventListener("change", (event) => {
  if (!event.target.matches?.('#kaspi-upload input[type="file"]')) return;
  if (!checkBankUploadFiles(event.target.files)) return;
  event.stopImmediatePropagation();
  event.target.value = "";
}, true);

// Dash 4.1 reads dragged files asynchronously, after the browser clears the drag data store.
// Route multi-file drops through its file-input handler until the upstream fix is stable.
document.addEventListener("drop", (event) => {
  if (!event.target.closest?.("#kaspi-upload")) return;
  if (checkBankUploadFiles(event.dataTransfer?.files || [])) {
    event.preventDefault();
    event.stopImmediatePropagation();
    return;
  }
  if (event.dataTransfer?.files.length < 2) return;
  const input = document.querySelector('#kaspi-upload input[type="file"]');
  if (!input) return;
  try {
    input.files = event.dataTransfer.files;
  } catch {
    return;
  }
  event.preventDefault();
  event.stopImmediatePropagation();
  input.dispatchEvent(new Event("change", { bubbles: true }));
}, true);
