// tests/asana.test.js
//
// The Asana adapter. An Asana export is not a second import pipeline: it is a
// translation into the JSON the existing importer already accepts, so the file
// still arrives through parseState, the same add-or-substitute dialog and the
// same whole-board PUT. That is the whole design, and it is why this module is
// worth testing on its own — it is a pure function from one document to
// another, with no board, no DOM and no network anywhere near it.
//
// js/core/asana.js therefore imports nothing. That is deliberate rather than
// incidental: every other core module reaches ui/dom.js sooner or later, and
// the moment this one does, this file stops loading under node.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { asanaToLodestar, isAsanaExport } from '../js/core/asana.js';

const HERE = dirname(fileURLToPath(import.meta.url));
const EXPORT = JSON.parse(readFileSync(join(HERE, 'fixtures/asana-export.json'), 'utf8'));

const byTitle = (cards, text) => cards.find((c) => c.title.includes(text));

// This is a unit test.
test('an Asana export is recognised, and a Lodestar board is not', () => {
  assert.equal(isAsanaExport(EXPORT), true);

  // The board's own export shape, which the plain importer must keep. A file
  // claimed by both adapters would be translated when it needed no translating.
  assert.equal(isAsanaExport({ version: 1, cards: [{ title: 'x' }] }), false);
  // Shape-alike but not tasks, and the degenerate cases the file input allows.
  assert.equal(isAsanaExport({ data: [{ gid: '1', resource_type: 'project' }] }), false);
  assert.equal(isAsanaExport({ data: [] }), false);
  assert.equal(isAsanaExport(null), false);
  assert.equal(isAsanaExport('a string'), false);
});

// This is a unit test.
test('a task becomes a card, and what has no home on the board is dropped', () => {
  const { version, cards } = asanaToLodestar(EXPORT);
  assert.equal(version, 1);

  const methods = byTitle(cards, 'methods section');
  assert.equal(methods.title, 'Draft the methods section');
  assert.equal(methods.type, 'task');
  // Completed in Asana is Done here — the column id is still 'answered'.
  assert.equal(methods.columnId, 'answered');
  assert.equal(methods.deadline, '2026-02-09');
  assert.equal(methods.createdAt, Date.parse('2026-01-05T09:00:00.000Z'));
  // When it was finished, not when Asana last touched the row: the Review
  // view reads updatedAt to decide what has been left alone.
  assert.equal(methods.updatedAt, Date.parse('2026-02-10T13:53:05.074Z'));
  // Project and section are the only Asana grouping a card can carry today.
  assert.deepEqual(methods.tags, ['manuscript project', 'manuscript']);
  // The notes are kept whole and the link is appended, so the card can always
  // be taken back to the row it came from.
  assert.equal(methods.notes,
    'Two paragraphs, no more.\n\nAsana: https://app.asana.com/1/9/project/7/task/100');

  // An open task in a section named "In progress" is the one column Asana
  // actually tells us about; every other open task starts in the Inbox.
  assert.equal(byTitle(cards, 'Bibliography').columnId, 'in-progress');
  assert.equal(byTitle(cards, 'Submit final files').columnId, 'inbox');

  // No notes, no due date, no memberships: the card is still whole, and the
  // provenance line does not arrive behind a blank first line.
  const bib = byTitle(cards, 'Bibliography');
  assert.equal(bib.notes, 'Asana: https://app.asana.com/1/9/project/7/task/200');
  assert.equal(bib.deadline, '');
  assert.equal(bib.updatedAt, Date.parse('2026-03-02T08:00:00.000Z'));

  // Assignees, followers, hearts and workspaces have no meaning on a
  // single-person board, and a card that carried them would only be lying.
  assert.deepEqual(Object.keys(methods).filter((k) => /assign|follow|heart|like|workspace|gid/i.test(k)), []);

  // The board's own gate still runs after this — the converter never mints an
  // id or a ledger number, so the same file can be imported twice safely.
  assert.ok(cards.every((c) => c.id === undefined && c.num === undefined));
});

// This is a unit test.
test('the subtask tree is flattened once, however often Asana repeats a task', () => {
  const { cards } = asanaToLodestar(EXPORT);

  // Six tasks in the fixture survive: three top-level, three nested three
  // deep. The seventh has a blank name and the eighth is a repeat.
  assert.deepEqual(cards.map((c) => c.title), [
    'Draft the methods section',
    'Bibliography issues',
    'Submit final files',
    'Declaration of interests',
    'Highlights file',
    'Three bullet points, 85 characters each',
  ]);

  // "Declaration of interests" is in the export twice — nested under its
  // parent and again at the top level with `parent` set. Asana does this to
  // any subtask that is also a project member, and without a dedupe by gid
  // every such task is imported as two cards that then drift apart.
  assert.equal(cards.filter((c) => c.title === 'Declaration of interests').length, 1);

  // A card cannot hold a parent yet, so the relationship is written where it
  // survives: in the notes, above the link. Losing it silently would leave
  // "Three bullet points, 85 characters each" unattached to anything.
  const decl = byTitle(cards, 'Declaration of interests');
  assert.match(decl.notes, /^Required even when there is nothing to declare\.\n\nSubtask of: Submit final files\nAsana: /);
  assert.equal(decl.columnId, 'answered');
  assert.match(byTitle(cards, 'Three bullet points').notes, /^Subtask of: Highlights file\nAsana: /);

  // A nested subtask inherits nothing from its parent's memberships, so it
  // carries no tags rather than borrowed ones.
  assert.deepEqual(decl.tags, []);
});
