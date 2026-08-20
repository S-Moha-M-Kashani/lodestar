import { assistantState } from './session.js';
import { submitChat } from './streaming.js';

// What to ask, offered only while the chat is still empty.
//
// The brain builds eight tools (server.py's tool list) and the composer has one
// line of placeholder text to advertise them in, so most of what the assistant
// can do was reachable only by guessing the right sentence. Two of these have
// no other entry point anywhere in the UI — a recap of a period, and a fact
// remembered for good — which makes the empty transcript the only place they
// are ever mentioned.
//
// One opener per capability, and the capability travels into the DOM with it:
// the e2e asserts on `data-capability`, never on the wording, so rewriting a
// suggestion is a copy edit while dropping a capability is a failing suite.
//
// Wording is fixed rather than drawn from the board ("triage your 12 Inbox
// cards"): the chip is a prompt for a model that will go and count, and this
// module otherwise needs no board state at all.
export const CHAT_SUGGESTIONS = [
  { capability: 'list_cards', text: "What's in my Inbox, and what should I pick up first?" },
  { capability: 'find_related', text: "What on my board is connected that I haven't noticed?" },
  { capability: 'web_search', text: 'Research how to start strength training at 35 and summarise the options.' },
  { capability: 'create_card', text: 'Turn this into a card: I keep forgetting to back up my photos.' },
  { capability: 'update_card', text: 'Take the oldest card in my Inbox and suggest a sharper title and category.' },
  { capability: 'recall_chat', text: 'What did we decide the last time we talked about my career?' },
  { capability: 'daily_recap', text: 'Recap what happened on my board this week.' },
  { capability: 'remember_fact', text: 'Remember that I plan my week on Sunday evenings.' },
];

/** The openers, as buttons under the empty-chat hint. A group with a name
 *  rather than a list of listitems: `role="listitem"` on a button replaces the
 *  role that says it can be pressed, and being pressable is the whole point. */
export function renderChatSuggestions() {
  const list = document.createElement('div');
  list.className = 'chat-suggestions';
  list.setAttribute('role', 'group');
  list.setAttribute('aria-label', 'Suggested questions');
  for (const { capability, text } of CHAT_SUGGESTIONS) {
    const chip = document.createElement('button');
    chip.type = 'button';
    chip.className = 'chat-suggest';
    chip.dataset.capability = capability;
    chip.textContent = text;
    chip.addEventListener('click', () => {
      // Printed into the composer before it is sent, and in that order on
      // purpose: submitChat can hold a turn back to ask whether it belongs in
      // this chat, and the words then have to be somewhere the reader can see
      // and edit them. sendChat empties the draft on its way out, so a turn
      // that does go leaves no copy of itself behind in the composer.
      assistantState.draft = text;
      submitChat(text);
    });
    list.appendChild(chip);
  }
  return list;
}
