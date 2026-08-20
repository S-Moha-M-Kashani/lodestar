import { $ } from './dom.js';

// ask() and prompt() — the in-app dialogs that stand in for the native ones.
// confirm()/alert() are never used here: they cannot be styled to look like
// the rest of the ledger, and they freeze the page while they are up.

export function ask({ title, message, okLabel = 'OK', cancelLabel = 'Cancel', danger = false }) {
  return new Promise((resolve) => {
    const confirmDialog = $('#confirm-dialog');
    $('#confirm-title').textContent = title;
    $('#confirm-copy').textContent = message;
    const ok = $('#confirm-ok');
    ok.textContent = okLabel;
    ok.className = 'btn ' + (danger ? 'danger' : 'primary');
    const cancel = $('#confirm-cancel');
    cancel.hidden = cancelLabel === null;
    if (cancelLabel !== null) cancel.textContent = cancelLabel;
    confirmDialog.returnValue = '';
    confirmDialog.addEventListener(
      'close',
      () => resolve(confirmDialog.returnValue === 'ok'),
      { once: true }
    );
    confirmDialog.showModal();
    ok.focus();
  });
}

/** ask() with a text field. Resolves to the trimmed text, or null when
 *  cancelled — which is why an empty string and a cancel are distinguishable
 *  rather than both falsy-and-identical. */
// Cancel is a plain button (see index.html for why), so closing on its click
// is wired here, once. close() keeps the '' returnValue prompt() just set,
// which is what makes the promise resolve null rather than a trimmed ''.
$('#prompt-cancel').addEventListener('click', () => $('#prompt-dialog').close());

export function prompt({ title, label, value = '', okLabel = 'Save' }) {
  return new Promise((resolve) => {
    const dialog = $('#prompt-dialog');
    $('#prompt-title').textContent = title;
    $('#prompt-label').textContent = label;
    const input = $('#prompt-input');
    input.value = value;
    $('#prompt-ok').textContent = okLabel;
    dialog.returnValue = '';
    dialog.addEventListener('close', () => resolve(
      dialog.returnValue === 'ok' ? input.value.trim() : null), { once: true });
    dialog.showModal();
    input.focus();
    input.select();
  });
}

/**
 * Move a card to a column, placed before the card with id `beforeId`
 * (or at the end of the column when beforeId is null).
 */
