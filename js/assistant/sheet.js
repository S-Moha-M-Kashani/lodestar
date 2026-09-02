import { renderChatChrome, restoreChatScroll } from './chrome.js';
import { brainModels, probeBrainModels } from './models.js';
import { assistantState } from './session.js';
import { mountAssistantTools, renderChatDock } from './tools.js';
import { renderSessionCost } from './transcript.js';
import { collapseToWidget } from './widget.js';
import { renderProposals, renderSuggestedEdits } from '../ui/proposals.js';

// The Assistant sheet — the full-screen shell for the conversation.
//
// What is peculiar to this shell lives here: the heading, the dock in the
// margin, the session's cost, the strip of things waiting on the user, and the
// control that folds the whole thing back into the corner widget.
// Everything from the transcript down to the send button is drawn by
// chrome.js, which the widget calls too — the two shells are the same
// conversation seen through different frames, and only one of them is ever in
// the document.

export function renderAssistant() {
  // Asked on entering the view rather than at load, and retried on every entry
  // until it answers — a brain started after the page must be found without a
  // reload. Not awaited: the view renders from the presets and re-renders only
  // if the answer changes a pick.
  if (!brainModels.provider) probeBrainModels();
  const sheet = document.createElement('section');
  sheet.className = 'assistant-sheet';

  const head = document.createElement('div');
  head.className = 'assistant-head';
  const heading = document.createElement('h2');
  heading.textContent = 'Assistant';
  head.appendChild(heading);
  // The tools — search the record, History, New chat, the gear — sit right of
  // the heading, in the sheet they act on. render() rescues the node back to
  // the app header before every wipe, so this move is always of a live node.
  mountAssistantTools(head);
  // The way back down to the corner, and the mirror of the widget's expand.
  // That pairing is what makes the two shells one feature: the same chat, the
  // same unsent draft, a different amount of screen given to it.
  const collapse = document.createElement('button');
  collapse.type = 'button';
  collapse.id = 'assistant-collapse';
  collapse.className = 'btn ghost assistant-collapse';
  collapse.textContent = '⤡';
  collapse.title = 'Collapse into the corner widget';
  collapse.setAttribute('aria-label', 'Collapse the Assistant into the widget');
  collapse.addEventListener('click', collapseToWidget);
  head.appendChild(collapse);
  sheet.appendChild(head);

  // First in the sheet because that is where it is on screen — the margin at
  // the top-left corner — and because tab order should reach the chat you are
  // in before the conversation itself. Absolutely positioned out of the sheet
  // on a wide window; with no margin to sit in it becomes an ordinary row here.
  sheet.appendChild(renderChatDock());

  // Nothing above the transcript but what this conversation cost. Searching
  // the record, the chats, New chat and the settings are all in the app header
  // now, in one row of controls — the sheet holds the conversation and the
  // things that belong to it, and a row of furniture above every reply was
  // exactly what made the assistant's own history hard to find.
  const cost = renderSessionCost();
  if (cost) {
    const toolbar = document.createElement('div');
    toolbar.className = 'assistant-toolbar';
    toolbar.appendChild(cost);
    sheet.appendChild(toolbar);
  }

  // Anything waiting on the user shares one pinned strip directly under the
  // row above. Pinned because it used to scroll away: it is above the
  // transcript, which is right until the transcript is long enough to push the
  // composer past the fold — the reader is then at the bottom typing while the
  // thing needing their decision sits off the top of the screen. Nothing
  // waiting, nothing shown: the strip must not sit there empty.
  if (assistantState.proposals.length || assistantState.edits.length) {
    const waiting = document.createElement('div');
    waiting.className = 'assistant-waiting';
    if (assistantState.proposals.length) waiting.appendChild(renderProposals());
    if (assistantState.edits.length) waiting.appendChild(renderSuggestedEdits());
    sheet.appendChild(waiting);
  }

  const log = renderChatChrome(sheet, 'sheet');
  restoreChatScroll(log);
  return sheet;
}
