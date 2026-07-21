// Reusable password Show/Hide for every ContextFlow auth page.
//
// Markup contract: a password field is wrapped in .field-wrap containing the
// <input> and a <button type="button" class="pw-toggle">. Using a real button
// makes it keyboard-accessible for free (Enter/Space fire the click); it is not
// a link. Toggling only the input's `type` preserves name/autocomplete, so
// password managers keep working. Progressive enhancement: without JS the field
// stays a normal password input.
document.querySelectorAll('.pw-toggle').forEach(function (btn) {
  var wrap = btn.closest('.field-wrap');
  var input = wrap && wrap.querySelector('input');
  if (!input) return;
  btn.setAttribute('aria-pressed', 'false');   // hidden by default
  btn.addEventListener('click', function () {
    var reveal = input.type === 'password';
    input.type = reveal ? 'text' : 'password';
    btn.textContent = reveal ? 'Hide' : 'Show';
    btn.setAttribute('aria-pressed', reveal ? 'true' : 'false');
    btn.setAttribute('aria-label', (reveal ? 'Hide' : 'Show') + ' password');
  });
});
