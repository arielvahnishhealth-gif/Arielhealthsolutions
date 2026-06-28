const form = document.querySelector("[data-quote-form]");
const statusEl = document.querySelector("[data-form-status]");
const stickyQuote = document.querySelector(".sticky-quote");

if (form && statusEl) {
  form.addEventListener("submit", () => {
    const stateInput = form.elements.state;
    if (stateInput) {
      stateInput.value = String(stateInput.value || "").trim().toUpperCase();
    }
    statusEl.textContent = "Submitting request...";
  });
}

if (stickyQuote && form) {
  const updateStickyQuote = () => {
    const rect = form.getBoundingClientRect();
    const formVisible = rect.top < window.innerHeight && rect.bottom > 0;
    stickyQuote.classList.toggle("is-hidden", formVisible);
  };

  updateStickyQuote();
  window.addEventListener("scroll", updateStickyQuote, { passive: true });
  window.addEventListener("resize", updateStickyQuote);
}
