// Dash 4.1 reads dragged files asynchronously, after the browser clears the drag data store.
// Route multi-file drops through its file-input handler until the upstream fix is stable.
document.addEventListener("drop", (event) => {
  if (!event.target.closest?.("#kaspi-upload") || event.dataTransfer?.files.length < 2) return;
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
