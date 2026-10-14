// Copy buttons: <button data-copy="element-id">. Loaded as a file because the CSP forbids inline JS.
document.addEventListener("click", (event) => {
  const button = event.target.closest("button[data-copy]");
  if (!button) return;
  const source = document.getElementById(button.dataset.copy);
  if (!source || !navigator.clipboard) return;
  navigator.clipboard.writeText(source.textContent).then(() => {
    button.textContent = "Copied";
    setTimeout(() => { button.textContent = "Copy"; }, 2000);
  });
});
