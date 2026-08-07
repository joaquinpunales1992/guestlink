/* Show only the fields that matter for the chosen channel.
 *
 * A WhatsApp service has no referral link; an Airbnb or Viator one is not
 * routed to a provider. Leaving every field on screen invites filling in the
 * ones that are ignored — and a referral URL saved against a WhatsApp service
 * looks configured but never reaches a guest.
 *
 * Hiding only; nothing is cleared. Switching channel back must not silently
 * discard a link the host pasted earlier.
 */
(function () {
  "use strict";

  function apply(channel) {
    var referral = channel === "airbnb" || channel === "viator";
    document.querySelectorAll(".referral-only").forEach(function (el) {
      el.style.display = referral ? "" : "none";
    });
    document.querySelectorAll(".whatsapp-only").forEach(function (el) {
      el.style.display = referral ? "none" : "";
    });
  }

  document.addEventListener("DOMContentLoaded", function () {
    var select = document.getElementById("id_channel");
    if (!select) return;
    apply(select.value);
    select.addEventListener("change", function () {
      apply(select.value);
    });
  });
})();
