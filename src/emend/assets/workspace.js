// Progressive enhancement only. Every page and action works without this file.
(() => {
  const root = document.documentElement;
  root.classList.add("js");
  if (!CSS.supports("selector(:has(*))")) root.classList.add("no-has");

  // While a run is active, reload when its recorded progress changes.
  const status = document.body.dataset.status;
  if (status) {
    const version = document.body.dataset.version;
    const poll = async () => {
      try {
        const response = await fetch(status, { cache: "no-store", credentials: "same-origin" });
        if (response.ok) {
          const state = await response.json();
          if (String(state.version) !== version || !state.active) {
            location.reload();
            return;
          }
        }
      } catch (error) {
        // The server may be restarting; keep the current page and retry.
      }
      setTimeout(poll, 900);
    };
    setTimeout(poll, 900);
  }

  // Choosing or dropping a book starts the import straight away.
  for (const input of document.querySelectorAll("input[data-autosubmit]")) {
    const zone = input.closest(".drop-zone");
    const title = zone && zone.querySelector(".drop-title");
    input.addEventListener("change", () => {
      if (!input.files.length) return;
      if (zone) zone.classList.add("is-busy");
      if (title) title.textContent = `Importing ${input.files[0].name}…`;
      input.form.requestSubmit();
    });
    if (!zone) continue;
    for (const name of ["dragenter", "dragover"]) {
      zone.addEventListener(name, () => zone.classList.add("is-over"));
    }
    for (const name of ["dragleave", "drop"]) {
      zone.addEventListener(name, () => zone.classList.remove("is-over"));
    }
  }

  // One submission per form; the button says what is happening.
  document.addEventListener("submit", (event) => {
    const form = event.target;
    if (form.dataset.busy) {
      event.preventDefault();
      return;
    }
    form.dataset.busy = "true";
    const button = event.submitter || form.querySelector("button[type=submit]");
    if (button) {
      button.setAttribute("aria-busy", "true");
      button.textContent = button.dataset.working || "Working…";
    }
  });

  window.addEventListener("pageshow", (event) => {
    if (event.persisted) location.reload();
  });
})();
