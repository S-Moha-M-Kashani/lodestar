// Click a column header to sort by it. One file, loaded by both lab pages.
//
// The panel (:9002) and the Inspector (:9003) are served out of this same
// directory, so "what does clicking a header do" gets one answer instead of two
// that drift. Its ordering rules are unit tested from Node in
// `tests/sorttable.test.js`, which evaluates this file in a `vm` context — hence
// no module wrapper here and no DOM access outside a function.
//
// Three states per column, not two: sort, reverse, then **back to the order the
// table was served in**. That order is itself information — the leaderboard's is
// a ranking and the ledger's is newest-first — and a two-state toggle would make
// it unreachable after the first click.
const SortTable = (() => {
  // What the two panels render when a number was never measured. Not '-': a lone
  // minus sign is not a placeholder anybody here writes, and treating it as one
  // would swallow a column of them.
  const MISSING = new Set(['', '—', '–', '·', 'n/a']);
  const LEADING_NUMBER = /^([+-]?\d+(?:\.\d+)?)([\s\S]*)$/;
  // A number followed by one of these is not a number: '2026-08-04' is a date and
  // '16:00:08' a time, both of which sort correctly as their own zero-padded text
  // and would collapse to 2026 and 16 if read as figures.
  const NOT_A_UNIT = /^[-/:.]\d/;

  // What one cell sorts as. `data-sort` on the cell wins where a renderer knows
  // better than its own text; everything else reads what the row *shows*, which
  // is also what makes a cell holding a button or a bar sort by its label.
  function cellKey(text) {
    const shown = String(text === null || text === undefined ? '' : text).trim();
    if (MISSING.has(shown.toLowerCase())) {
      return { missing: true, number: null, text: '' };
    }
    // Commas dropped for the numeric probe only: '1,204' is 1204, and the text
    // key keeps the cell exactly as rendered.
    const probe = shown.replace(/,/g, '');
    let number = null;
    const found = probe.match(LEADING_NUMBER);
    if (found && !NOT_A_UNIT.test(found[2])) {
      // A trailing unit or qualifier is fine — '83%', '12s', and the
      // leaderboard's '0.747 ± 0.042', which has to sort on 0.747 or 0.9 files
      // below 0.75 ± 0.1.
      number = parseFloat(found[1]);
    } else if (!found) {
      // '± 0.042' on its own: the mark qualifies the figure, it is not part of it.
      const error = probe.match(/^±\s*([+-]?\d+(?:\.\d+)?)$/);
      if (error) number = parseFloat(error[1]);
    }
    return { missing: false, number, text: shown };
  }

  // `dir` is 1 for ascending, -1 for descending.
  function compare(a, b, dir) {
    if (a.missing || b.missing) {
      // Last whichever way the column points. A dash means "never measured", so
      // it is neither a small value nor a large one — and a leaderboard led by
      // the rows that measured least is the single mistake it exists to prevent.
      // Two of them are equal, which leaves them in served order.
      if (a.missing && b.missing) return 0;
      return a.missing ? 1 : -1;
    }
    if (a.number !== null && b.number !== null) return (a.number - b.number) * dir;
    // A column mixing figures and words sorts as words. Rare enough to accept and
    // better than an arbitrary rule about which kind wins.
    return a.text.localeCompare(b.text, undefined,
      // Case-insensitive because the lab's labels are typed by hand and mix case;
      // numeric so 'C-2' files before 'C-10'.
      { sensitivity: 'base', numeric: true }) * dir;
  }

  // Which way a column opens on its first click. Numbers lead with the best and
  // text reads A to Z: someone clicking a score column wants the top of it,
  // someone clicking a name column wants the alphabet.
  //
  // But "best" is not always "highest". A *rank* is best at 1, so the generic rule
  // opened the Inspector's dense column at rank 46 — the candidate dense liked
  // least, the opposite of the question being asked. A column whose best value is
  // its lowest says so with `data-order="ascending"`, which keeps that knowledge
  // where the column is declared rather than in a list of header names here.
  function opening(th, hasNumbers) {
    const told = (th.getAttribute('data-order') || '').toLowerCase();
    if (told === 'ascending') return 1;
    if (told === 'descending') return -1;
    return hasNumbers ? -1 : 1;
  }

  // Wire one table. Safe to call again on the same element — a re-rendered table
  // is a new element, and a second call on the same one would stack listeners.
  function make(table) {
    if (!table || table.dataset.sortWired) return;
    const head = table.tHead && table.tHead.rows[table.tHead.rows.length - 1];
    const body = table.tBodies[0];
    if (!head || !body || !body.rows.length) return;
    table.dataset.sortWired = '1';
    table.classList.add('sortable');
    // Captured once: this is the order the service sent, and the third click
    // restores exactly it.
    const served = Array.from(body.rows);
    const heads = Array.from(head.cells);
    let column = -1;
    let dir = 0;

    const keyOf = (row, at) => {
      const cell = row.cells[at];
      if (!cell) return cellKey('');
      const told = cell.getAttribute('data-sort');
      return cellKey(told === null ? cell.textContent : told);
    };

    const opensAt = (at) => opening(
      heads[at], served.some((row) => keyOf(row, at).number !== null));

    function apply() {
      for (const th of heads) th.removeAttribute('aria-sort');
      let rows = served;
      if (column >= 0) {
        rows = served
          .map((row, at) => ({ row, at }))
          .sort((x, y) => compare(keyOf(x.row, column), keyOf(y.row, column), dir)
            // Explicit, so a tie provably keeps served order rather than relying
            // on the engine's sort being stable.
            || x.at - y.at)
          .map((held) => held.row);
        heads[column].setAttribute('aria-sort',
          dir === 1 ? 'ascending' : 'descending');
      }
      for (const row of rows) body.appendChild(row);
    }

    heads.forEach((th, at) => {
      if (th.hasAttribute('data-nosort')) return;
      th.classList.add('sort-col');
      th.tabIndex = 0;
      th.setAttribute('role', 'button');
      th.title = 'sort by this column · again to reverse · a third time for the '
        + 'order it was served in';
      const cycle = () => {
        if (column !== at) { column = at; dir = opensAt(at); }
        else if (dir === opensAt(at)) { dir = -dir; }
        else { column = -1; dir = 0; }
        apply();
      };
      th.addEventListener('click', cycle);
      th.addEventListener('keydown', (event) => {
        if (event.key === 'Enter' || event.key === ' ') {
          event.preventDefault();
          cycle();
        }
      });
    });
  }

  // Every table under a root, for a page that renders several at once.
  function makeAll(root) {
    for (const table of (root || document).querySelectorAll('table')) make(table);
  }

  return { cellKey, compare, opening, make, makeAll };
})();

if (typeof window !== 'undefined') window.SortTable = SortTable;
