// tests/schedule.test.js
//
// A habit's schedule: one slot per repetition, each able to name a month, a
// weekday, a day of the month and an hour. Four things here can go quietly
// wrong, and two of them would lose a reminder somebody is relying on.
//
//   1. The migration. `habitTimes` held plain 'HH:MM' strings until this
//      change and keeps its name and its column, so the whole migration is
//      this one validator: a string must coerce to a slot naming that hour.
//      Get it wrong and every habit on every existing board silently stops
//      reminding, with nothing to point at.
//   2. Position is identity. A slot *is* the nth repetition, so the validator
//      may not sort and may not de-duplicate — both of which the old
//      habitTimesVal did. A form that rearranges itself under the hand filling
//      it in is worse than one that asks twice.
//   3. Which fields a period reads. The cadence decides that, and nothing else
//      does: a stored weekday must survive a trip through monthly and back.
//   4. The arithmetic behind the reminder, which is one sentence — the slots
//      whose moment has already passed inside this period — where an empty
//      slot's moment is the period's own start. That is how "no times set
//      means the whole target is expected", the behaviour before this change,
//      falls out with no branch for it.
//
// Every assert names a moment with `new Date(…)` rather than reading the
// clock, so this suite cannot change its answer overnight.
//
// js/core/schedule.js imports nothing, like js/core/plan.js, js/core/draft.js
// and js/core/nesting.js: the moment it reaches ui/dom.js this file stops
// loading under node.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { SLOT_READS, expectedBy, slotVal, slotsText, slotsVal } from '../js/core/schedule.js';

const EMPTY = { month: '', weekday: '', day: '', time: '' };
const slot = (over) => ({ ...EMPTY, ...over });

// This is a unit test.
test('a slot is validated field by field, and a clock string becomes one', () => {
  // The migration: the shape every board carried before this change.
  assert.deepEqual(slotVal('07:00'), slot({ time: '07:00' }));
  assert.deepEqual(slotVal('21:30'), slot({ time: '21:30' }));
  // A string that was never a valid clock time is not one now either.
  assert.deepEqual(slotVal('7:30'), EMPTY);
  assert.deepEqual(slotVal('25:99'), EMPTY);

  // The full shape, and the two spellings a select and an import send.
  assert.deepEqual(slotVal({ month: '03', weekday: '2', day: '04', time: '08:15' }),
    { month: '03', weekday: '2', day: '04', time: '08:15' });
  assert.deepEqual(slotVal({ month: 3, weekday: 2, day: 4, time: '08:15' }),
    { month: '03', weekday: '2', day: '04', time: '08:15' });

  // Out of range, absent and nonsense all blank the field and keep the slot —
  // the `category` rule, one level down.
  assert.deepEqual(slotVal({ month: '13', weekday: '0', day: '32', time: 'noon' }), EMPTY);
  assert.deepEqual(slotVal({ month: '00', weekday: '8', day: '00' }), EMPTY);
  assert.deepEqual(slotVal({}), EMPTY);
  for (const v of [undefined, null, 7, true, [], 'nonsense']) {
    assert.deepEqual(slotVal(v), EMPTY, `${JSON.stringify(v)} is not a slot`);
  }
  // Every key is stored whatever the cadence is: which are *read* is the
  // cadence's business, so a weekday survives a trip through monthly.
  assert.equal(slotVal({ weekday: '5' }).weekday, '5');
});

// This is a unit test.
test('slotsVal holds exactly one slot per repetition, in repetition order', () => {
  // Padded up: three repetitions with no times is three empty slots, which is
  // what an unscheduled habit has always meant.
  assert.deepEqual(slotsVal([], 3), [EMPTY, EMPTY, EMPTY]);
  assert.deepEqual(slotsVal(undefined, 2), [EMPTY, EMPTY]);
  assert.deepEqual(slotsVal('nope', 1), [EMPTY]);

  // Truncated down, keeping the first ones exactly as they were: lowering the
  // count drops the trailing repetitions, never the middle.
  const four = ['07:00', '09:00', '13:00', '19:00'];
  assert.deepEqual(slotsVal(four, 2).map((s) => s.time), ['07:00', '09:00']);
  assert.deepEqual(slotsVal(four, 6).map((s) => s.time),
    ['07:00', '09:00', '13:00', '19:00', '', '']);

  // No sorting and no de-duplication. A slot is row n, so the old validator's
  // tidying would now renumber somebody's rows and drop one of them.
  assert.deepEqual(slotsVal(['21:00', '07:00', '07:00'], 3).map((s) => s.time),
    ['21:00', '07:00', '07:00']);
  // An invalid entry blanks its own row and does not shift the ones after it.
  assert.deepEqual(slotsVal(['07:00', 'rubbish', '13:00'], 3).map((s) => s.time),
    ['07:00', '', '13:00']);
});

// This is a unit test.
test('the cadence decides which fields a slot’s moment is read from', () => {
  // The table the dialog disables its controls from, and momentOf reads.
  assert.deepEqual(SLOT_READS.daily, { month: false, weekday: false, day: false, time: true });
  assert.deepEqual(SLOT_READS.weekly, { month: false, weekday: true, day: false, time: true });
  assert.deepEqual(SLOT_READS.monthly, { month: false, weekday: false, day: true, time: true });
  assert.deepEqual(SLOT_READS.yearly, { month: true, weekday: false, day: true, time: true });

  // A slot carrying every field is read four different ways. Wednesday
  // 2026-09-09, 12:00 — mid-week, mid-month, mid-year, so each period's own
  // answer is a different date.
  const at = new Date(2026, 8, 9, 12, 0);
  const full = [slot({ month: '03', weekday: '5', day: '20', time: '08:00' })];

  // Daily reads the hour: 08:00 today has passed.
  assert.equal(expectedBy('daily', full, 1, at), 1);
  // Weekly reads the weekday: Friday of this week is still ahead of Wednesday.
  assert.equal(expectedBy('weekly', full, 1, at), 0);
  // Monthly reads the day: the 20th of September is still ahead of the 9th.
  assert.equal(expectedBy('monthly', full, 1, at), 0);
  // Yearly reads the month too: 20 March has long passed.
  assert.equal(expectedBy('yearly', full, 1, at), 1);

  // A day the month does not have is that month's last day, not a skipped
  // slot: the 31st in a thirty-day month is the 30th. 2026-09-30 08:00 has not
  // passed at noon on the 9th, and has by the 30th at teatime.
  const end = [slot({ day: '31', time: '08:00' })];
  assert.equal(expectedBy('monthly', end, 1, at), 0);
  assert.equal(expectedBy('monthly', end, 1, new Date(2026, 8, 30, 17, 0)), 1);
  // Which is also true of 29 February in a common year: 2027-02-28.
  const feb = [slot({ month: '02', day: '29', time: '08:00' })];
  assert.equal(expectedBy('yearly', feb, 1, new Date(2027, 1, 27, 12, 0)), 0);
  assert.equal(expectedBy('yearly', feb, 1, new Date(2027, 1, 28, 12, 0)), 1);
});

// This is a unit test.
test('expectedBy counts the slots whose moment has passed in this period', () => {
  // With nothing scheduled every slot's moment is the period's start, so the
  // whole target is expected — the behaviour before this change, with no
  // branch of its own.
  const at = new Date(2026, 8, 9, 9, 0); // Wednesday 09:00
  assert.equal(expectedBy('daily', slotsVal([], 3), 3, at), 3);
  assert.equal(expectedBy('weekly', slotsVal([], 5), 5, at), 5);
  // A short array is the same statement: the repetitions with no slot at all
  // have no fixed time either.
  assert.equal(expectedBy('daily', [], 3, at), 3);

  // A slot ahead of now is not expected yet; one behind it is.
  assert.equal(expectedBy('daily', slotsVal(['21:00'], 1), 1, at), 0);
  assert.equal(expectedBy('daily', slotsVal(['21:00'], 1), 1, new Date(2026, 8, 9, 21, 30)), 1);
  // Exactly on the minute counts: the hour has arrived.
  assert.equal(expectedBy('daily', slotsVal(['09:00'], 1), 1, at), 1);

  // Both halves at once: one slot at 21:00 and two with no hour, at 09:00.
  const mixed = slotsVal(['21:00'], 3);
  assert.equal(expectedBy('daily', mixed, 3, at), 2);

  // A weekly slot waits for its day, and then does not wait any longer.
  const friday = [slot({ weekday: '5', time: '18:00' })];
  assert.equal(expectedBy('weekly', friday, 1, new Date(2026, 8, 8, 23, 0)), 0); // Tuesday
  assert.equal(expectedBy('weekly', friday, 1, new Date(2026, 8, 11, 17, 0)), 0); // Friday, early
  assert.equal(expectedBy('weekly', friday, 1, new Date(2026, 8, 12, 10, 0)), 1); // Saturday
  // Sunday is the 7th day, not the 0th: the week's last day, still inside it.
  const sunday = [slot({ weekday: '7', time: '18:00' })];
  assert.equal(expectedBy('weekly', sunday, 1, new Date(2026, 8, 13, 19, 0)), 1);
  assert.equal(expectedBy('weekly', sunday, 1, new Date(2026, 8, 13, 17, 0)), 0);
});

// This is a unit test.
test('slotsText reads a schedule back in the period’s own terms', () => {
  assert.equal(slotsText('daily', slotsVal([], 3), 3), '');
  assert.equal(slotsText('daily', slotsVal(['07:00', '21:00'], 2), 2), '07:00, 21:00');
  assert.equal(slotsText('weekly',
    [slot({ weekday: '2', time: '07:00' }), slot({ weekday: '5', time: '19:00' })], 2),
  'Tue 07:00, Fri 19:00');
  // A field the period does not read is not spoken, however it is stored.
  assert.equal(slotsText('daily', [slot({ weekday: '2', time: '07:00' })], 1), '07:00');
  assert.equal(slotsText('monthly', [slot({ day: '03', time: '09:30' })], 1), '3rd 09:30');
  assert.equal(slotsText('monthly', [slot({ day: '21' })], 1), '21st');
  assert.equal(slotsText('yearly', [slot({ month: '03', day: '12', time: '08:00' })], 1),
    '12 Mar 08:00');
  // The repetitions nobody has placed are counted rather than listed, or the
  // sentence would describe one repetition and quietly stand for three.
  assert.equal(slotsText('daily', slotsVal(['21:00'], 3), 3), '21:00, +2 any time');
  assert.equal(slotsText('daily', slotsVal(['21:00'], 2), 2), '21:00, +1 any time');
});
