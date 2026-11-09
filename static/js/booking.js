document.querySelectorAll("[data-seat-form]").forEach((form) => {
  const checkboxes = Array.from(form.querySelectorAll('input[name="seat_numbers"]'));
  const passengerCount = form.querySelector("[data-passenger-count]");
  const message = form.querySelector("[data-seat-message]");
  const submitButton = form.querySelector("[data-book-button]");

  const updateSelection = () => {
    const selectedCount = checkboxes.filter((seat) => seat.checked).length;
    const requiredCount = Number(passengerCount.value);
    checkboxes.forEach((seat) => {
      const unavailable = seat.closest(".seat-option").classList.contains("is-occupied");
      seat.disabled = unavailable || (!seat.checked && selectedCount >= requiredCount);
    });
    message.textContent = selectedCount === requiredCount
      ? `${selectedCount} asiento${selectedCount === 1 ? "" : "s"} seleccionado${selectedCount === 1 ? "" : "s"}.`
      : `Selecciona ${requiredCount} asiento${requiredCount === 1 ? "" : "s"}; llevas ${selectedCount}.`;
    submitButton.disabled = selectedCount !== requiredCount;
  };

  checkboxes.forEach((seat) => seat.addEventListener("change", updateSelection));
  passengerCount.addEventListener("change", () => {
    const requiredCount = Number(passengerCount.value);
    checkboxes.filter((seat) => seat.checked).slice(requiredCount).forEach((seat) => {
      seat.checked = false;
    });
    updateSelection();
  });
  form.addEventListener("submit", (event) => {
    if (checkboxes.filter((seat) => seat.checked).length !== Number(passengerCount.value)) {
      event.preventDefault();
      updateSelection();
    }
  });
  updateSelection();
});
