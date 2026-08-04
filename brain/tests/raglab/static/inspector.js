// The Inspector's whole frontend: four views over the read-only :9003 API —
// ground truth, chunks, retrieval, generation — three of which auto-follow
// whatever the lab (:9002) actually ran.
const CHOSEN = {
  index: { chunker: 'semantic-drift', embedder: 'sentence-transformers' },
  retrieval: { retriever: 'hybrid-rrf', k: 8, reranker: 'lexical',
               time_filter: true, grader: 'llm', grade_threshold: 0.4 },
};

const views = ['groundtruth', 'chunks', 'retrieval', 'generation'];
function show(view) {
  for (const v of views) {
    document.getElementById(`view-${v}`).hidden = v !== view;
    const tab = document.getElementById(`tab-${v}`);
    tab.setAttribute('aria-selected', String(v === view));
  }
}
for (const v of views) {
  document.getElementById(`tab-${v}`).addEventListener('click', () => show(v));
}
show('groundtruth');

async function pollJob(jobId) {
  for (;;) {
    const job = await (await fetch(`/api/jobs/${jobId}`)).json();
    if (job.state === 'done') return job.result;
    if (job.state === 'error') throw new Error(job.error || 'job failed');
    await new Promise(r => setTimeout(r, 500));
  }
}

// Turn a POST response into its job id, or throw the server's own reason
// (400/404/409) instead of letting callers poll `/api/jobs/undefined` forever.
async function startedJob(response) {
  const body = await response.json();
  if (!body.job_id) throw new Error(body.detail || 'could not start job');
  return body.job_id;
}

// "showing: session · ascii-hash · hybrid-rrf k=8 · lexical · grader=none" —
// the config that produced whatever is on screen, so a reader is never left
// guessing which pipeline the rows in front of them came from.
function formatConfig(cfg) {
  if (!cfg) return '';
  const parts = [];
  if (cfg.index) parts.push(`${cfg.index.chunker} · ${cfg.index.embedder}`);
  if (cfg.retrieval) {
    parts.push(`${cfg.retrieval.retriever} k=${cfg.retrieval.k}`,
               cfg.retrieval.reranker, `grader=${cfg.retrieval.grader}`);
  }
  return `showing: ${parts.join(' · ')}`;
}

function escapeHtml(text) {
  return String(text === null || text === undefined ? '' : text)
    .replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
}

// --- What every score means: the '!' marks, reading the lab's own text -------
// Fetched from /api/explain rather than written here, so this page and the
// panel on :9002 cannot end up explaining the same metric differently.
let EXPLAIN = { metrics: [], help: {} };

// Which followed job each view is currently drawing. Declared up here because
// `loadGroundTruth` clears two of these when the fixture lands, and it runs
// before the follow loop is set up further down.
const followed = { indexJobId: null, queryJobId: null, retrievalJobId: null,
                   generationJobId: null };

function measureOf(key) {
  return EXPLAIN.metrics.find(m => m.key === key) || { key, label: key };
}

// The sentence the '!' opens: a metric's own note and formula when it has them,
// falling back to the help topic the lab writes for the same key.
function whyText(key) {
  const m = measureOf(key);
  return [m.note, m.formula && `formula: ${m.formula}`, m.source && `computed by ${m.source}`,
          EXPLAIN.help[`metric.${key}`]].filter(Boolean).join(' — ');
}

function whyMark(key) {
  const label = escapeHtml(measureOf(key).label || key);
  return `<button type="button" class="inspector-why" data-why="${escapeHtml(key)}"`
    + ` aria-label="What is ${label}?">!</button>`;
}

// One listener for the page: the marks are re-rendered on every poll tick, and
// a listener per button would leak one per render.
document.addEventListener('click', event => {
  const button = event.target.closest('.inspector-why');
  if (!button) return;
  const open = button.nextElementSibling;
  if (open && open.classList.contains('inspector-why-text')) { open.remove(); return; }
  const note = document.createElement('span');
  note.className = 'inspector-why-text';
  note.textContent = whyText(button.dataset.why) || 'no description for this one yet';
  button.after(note);
});

async function loadExplain() {
  try { EXPLAIN = await (await fetch('/api/explain')).json(); }
  catch (error) { /* the marks fall back to the bare key; not worth failing on */ }
}
loadExplain();

// --- Ground truth ---
// Kept by id as well as rendered, because the retrieval and generation views
// restate a question's own facts and ideal answer beside its rows, and those
// belong to the fixture rather than to any run.
const GT = new Map();

async function loadGroundTruth() {
  const body = await (await fetch('/api/groundtruth')).json();
  const root = document.getElementById('view-groundtruth');
  root.innerHTML = '';
  const qsel = document.getElementById('retrieval-question');
  for (const q of body.questions) {
    GT.set(q.id, q);
    const row = document.createElement('div');
    row.className = 'gt-row';
    const quotes = (q.evidence || []).map(e => e.quote).join(' · ');
    row.innerHTML = `<b>${q.id}</b> · ${q.type} · ${q.difficulty}
      <div dir="rtl">${q.question_fa}</div>
      <div>${q.question_en || ''}</div>
      <div><i>answer:</i> <span dir="rtl">${q.answer_fa || ''}</span></div>
      <div><i>evidence:</i> <span dir="rtl">${quotes}</span></div>`;
    root.appendChild(row);
    const opt = document.createElement('option');
    opt.value = q.id; opt.textContent = `${q.id} — ${q.question_fa.slice(0, 40)}`;
    qsel.appendChild(opt);
  }
  // The retrieval and generation views restate each question's facts and ideal
  // answer from this map, and the first poll can easily beat this fetch. Forget
  // which jobs were rendered so the next tick redraws them with the fixture
  // available — otherwise a race leaves every ideal answer showing '—' until
  // the next run.
  followed.retrievalJobId = null;
  followed.generationJobId = null;
}
loadGroundTruth();

// --- Chunks: shared render, used by both the followed view and the manual one ---
function renderChunkGroups(container, groups) {
  container.innerHTML = '';
  for (const g of groups) {
    const det = document.createElement('details');
    det.className = 'chunk-session';
    det.innerHTML = `<summary>${g.session_id} (${g.chunks.length} chunks)</summary>`;
    g.chunks.forEach((c, i) => {
      const p = document.createElement('div');
      p.dir = 'rtl';
      p.textContent = `chunk ${i + 1}: ${c.text}`;
      det.appendChild(p);
    });
    container.appendChild(det);
  }
}

document.getElementById('build-chunks').addEventListener('click', async () => {
  const status = document.getElementById('chunks-status');
  try {
    status.textContent = 'building…';
    const response = await fetch('/api/chunks',
      { method: 'POST', headers: { 'content-type': 'application/json' },
        body: JSON.stringify(CHOSEN) });
    const result = await pollJob(await startedJob(response));
    status.textContent = `${result.total} chunks`;
    document.getElementById('chunks-active-config').textContent = formatConfig(CHOSEN);
    renderChunkGroups(document.getElementById('chunks-body'), result.chunks_by_session);
  } catch (error) {
    status.textContent = error.message;
  }
});

// --- Retrieval: shared render ---
// One candidate per row, cloned from the page's own template so the columns are
// written once and every table — the single-question one and the per-question
// ones — carries the same header. Row background is the *ground truth's*
// verdict (white = gold, gray = not); `kept` is the pipeline's, in its own
// column, because a gold chunk the pipeline dropped is the thing worth seeing.
// The full chunk text with its gold evidence painted green. The ranges come
// from the service (`gold_spans`), never from a search in the browser: a
// candidate can be gold because the quote *contains* it, and that one has
// nothing verbatim to mark — a range invented here would draw a green stripe
// over text the ground truth never quoted.
function highlighted(text, spans) {
  const source = text || '';
  if (!spans || !spans.length) return escapeHtml(source);
  let out = '', at = 0;
  for (const [start, end] of spans) {
    out += escapeHtml(source.slice(at, start))
      + `<mark class="evidence-mark">${escapeHtml(source.slice(start, end))}</mark>`;
    at = end;
  }
  return out + escapeHtml(source.slice(at));
}

// With contextual headers on, the first 60 characters of every chunk are the
// same shape of metadata — date, mood, topics, storyline — so a preview taken
// from character 0 shows the header and nothing that tells one chunk from the
// next. The preview starts after a leading [ … ] header; the reveal still shows
// the whole text, header included, because that header is part of what was
// embedded and therefore part of why the chunk ranked where it did.
function previewOf(text) {
  const close = text.startsWith('[') ? text.indexOf(']') : -1;
  return (close === -1 ? text : text.slice(close + 1)).trim() || text;
}

function chunkCell(candidate) {
  const text = candidate.text || '';
  const spans = candidate.gold_spans || [];
  const preview = previewOf(text);
  // Gold with no span is a real state, not an error, and saying so is the whole
  // reason the spans are computed where the marking is.
  const footnote = (candidate.gold && !spans.length)
    ? '<span class="no-evidence">gold: this chunk sits inside the evidence quote, '
      + 'so there is no verbatim span to highlight</span>' : '';
  return `<td class="chunk-cell"><span class="chunk-preview" dir="rtl" tabindex="0">`
    + `${escapeHtml(preview.slice(0, 60))}${preview.length > 60 ? '…' : ''}</span>`
    + `<div class="chunk-reveal" dir="rtl">${highlighted(text, spans)}${footnote}</div></td>`;
}

// A candidate's path through the ranking, as a shape. One cell per step that
// produces a rank — dense, BM25, then the RRF fusion of the two — with bar
// height standing for how high the chunk was at that step. Reading three
// numbers and subtracting them is what this replaces: a chunk that BM25 loved
// and dense missed has a silhouette you recognise across twenty rows.
//
// `cap` is the number of candidates in this table, so the scale is the run's own
// depth rather than an arbitrary constant. aria-hidden, because the same three
// ranks follow in their own columns.
function ladder(candidate, cap) {
  const steps = [['dense', candidate.dense_rank], ['bm25', candidate.bm25_rank],
                 ['RRF', candidate.fused_rank]];
  if (steps.every(([, rank]) => !rank)) {
    return '<td><span class="ladder-none">·</span></td>';
  }
  const cells = steps.map(([, rank]) => {
    // Rank 1 is full height; anything past the table's depth keeps a stub, so a
    // bar is never absent in a way that could read as "no data".
    const height = rank ? Math.max(8, Math.round(100 * (1 - (rank - 1) / Math.max(1, cap)))) : 0;
    return `<i class="ladder-step" style="--h:${height}%"></i>`;
  }).join('');
  const label = steps.map(([name, rank]) => `${name} ${rank || '—'}`).join(' · ');
  return `<td><span class="ladder" title="${escapeHtml(label)}" aria-hidden="true">`
    + `${cells}</span></td>`;
}

// A table inside its own scroller, so nine columns on a phone move the table
// and never the page. The wrapper only scrolls at narrow widths (see the media
// query) — on a wide screen it would clip the reveal hanging below a row.
function scrollable(table) {
  const box = document.createElement('div');
  box.className = 'table-scroll';
  box.appendChild(table);
  return box;
}

function retrievalTable(candidates) {
  const table = document.getElementById('retrieval-table-template')
    .content.firstElementChild.cloneNode(true);
  const body = table.querySelector('tbody');
  const rows = candidates || [];
  const cell = v => (v === null || v === undefined) ? '·' : v;
  for (const c of rows) {
    const tr = document.createElement('tr');
    tr.className = 'retrieval-row ' + (c.gold ? 'retrieval-row--gold' : 'retrieval-row--plain');
    tr.innerHTML = chunkCell(c) + ladder(c, rows.length)
      + `<td class="num">${cell(c.dense_rank)}</td>
      <td class="num">${cell(c.bm25_rank)}</td>
      <td class="num">${cell(c.fused_rank)}</td>
      <td class="num">${cell(c.rerank_score)}</td>
      <td class="num">${cell(c.grade_score)}</td>
      <td>${c.kept ? '✓' : '✗'}</td>
      <td>${c.gold ? '●' : ''}</td>`;
    body.appendChild(tr);
  }
  return table;
}

// The question restated above its own rows, with the facts a right answer would
// have contained. `id` is enough to find both in the fixture.
function questionHead(questionId, fallbackFa) {
  const q = GT.get(questionId) || {};
  const facts = (q.key_facts || []).map(f => `<li>${escapeHtml(f)}</li>`).join('');
  return `<div class="question-head">`
    + `<div class="qh-fa" dir="rtl">${escapeHtml(q.question_fa || fallbackFa || '')}</div>`
    + `<div class="qh-en">${escapeHtml(q.question_en || '')}</div>`
    + (facts ? `<div class="qh-label">what a right answer contains</div>`
             + `<ol class="qh-facts">${facts}</ol>` : '')
    + `</div>`;
}

// The one-line summary a collapsed question shows. Same shape in both views, so
// a reader scanning either list is reading the same sentence.
function questionSummary(id, type, difficulty, tally) {
  return `<summary><span class="q-id">${escapeHtml(id)}</span> `
    + `<span class="q-tally">${escapeHtml(type || '')} · ${escapeHtml(difficulty || '')}`
    + `${tally ? ' — ' + escapeHtml(tally) : ''}</span></summary>`;
}

function renderRetrievalRows(candidates) {
  const host = document.getElementById('retrieval-body');
  host.innerHTML = '';
  host.appendChild(scrollable(retrievalTable(candidates)));
}

// The followed experiment: one collapsible table per selected question, with
// that question's own counts on the summary line. Collapsed by default — a set
// of thirty questions is a page you scan, then open the one that looks wrong.
function renderQuestionTables(questions) {
  const host = document.getElementById('retrieval-questions');
  host.innerHTML = '';
  for (const q of questions) {
    const candidates = (q.trace && q.trace.candidates) || [];
    const gold = candidates.filter(c => c.gold).length;
    const kept = candidates.filter(c => c.kept).length;
    const det = document.createElement('details');
    det.className = 'retrieval-question';
    det.innerHTML = questionSummary(q.question_id, q.type, q.difficulty,
        `${candidates.length} candidates · ${kept} kept · ${gold} gold`)
      + questionHead(q.question_id, q.question_fa);
    det.appendChild(scrollable(retrievalTable(candidates)));
    host.appendChild(det);
  }
}

document.getElementById('run-trace').addEventListener('click', async () => {
  const status = document.getElementById('retrieval-status');
  try {
    const qid = document.getElementById('retrieval-question').value;
    status.textContent = 'retrieving…';
    const response = await fetch('/api/trace',
      { method: 'POST', headers: { 'content-type': 'application/json' },
        body: JSON.stringify({ ...CHOSEN, question_id: qid }) });
    const result = await pollJob(await startedJob(response));
    status.textContent = qid;
    document.getElementById('retrieval-active-config').textContent = formatConfig(CHOSEN);
    renderRetrievalRows(result.trace.candidates);
    // /api/trace is retrieval only — no generation ran, so no answer to show.
    document.getElementById('retrieval-answer').textContent = '';
  } catch (error) {
    status.textContent = error.message;
  }
});

// --- Generation: the ideal answer, the written one, and the scores ----------
// Per-question deterministic scores, in pipeline order. RAGAS's judged metrics
// are per *run*, not per question, so they are rendered once above instead.
const GEN_METRICS = ['answer_similarity', 'answer_token_f1', 'key_fact_coverage',
                     'abstained_correctly', 'false_abstention'];

const fmt = v => (v === null || v === undefined) ? '·'
  : (typeof v === 'number' ? (Math.round(v * 1000) / 1000).toString() : String(v));

function metricLine(row, keys) {
  const present = keys.filter(k => row[k] !== undefined && row[k] !== null);
  if (!present.length) return '<div class="gen-metrics">no scores for this one</div>';
  return '<div class="gen-metrics">' + present.map(k =>
    `<span class="gen-metric">${escapeHtml(measureOf(k).label || k)}: `
    + `<b>${fmt(row[k])}</b>${whyMark(k)}</span>`).join('') + '</div>';
}

// `traces` is keyed by question id and only passed when the retrieval on screen
// came from this same evaluation — showing another run's ranks under this run's
// answer would invent a pipeline that never existed.
function renderGeneration(view, traces) {
  const ragasHost = document.getElementById('generation-ragas');
  const host = document.getElementById('generation-questions');
  const ragas = (view.ragas && view.ragas.metrics) || {};
  const keys = Object.keys(ragas);
  ragasHost.innerHTML = keys.length
    ? '<div class="gen-ragas"><h4>judged over the whole run</h4>'
      + metricLine(ragas, keys)
      + (view.ragas.decision !== null && view.ragas.decision !== undefined
         ? `<div class="gen-metrics"><span class="gen-metric">decision score `
           + `<b>${fmt(view.ragas.decision)}</b>${whyMark('ragas_decision')}</span></div>` : '')
      + '</div>'
    : '<div class="inspector-active-config">no RAGAS judging on this run '
      + '(ragas_mode=off) — the deterministic scores below are what exists</div>';

  host.innerHTML = '';
  for (const row of view.rows || []) {
    const gt = GT.get(row.id) || {};
    // A collapsible block per question, exactly as the retrieval view lists
    // them: the unit you reason about is one question, in both views.
    const det = document.createElement('details');
    det.className = 'gen-question';
    const scored = GEN_METRICS.filter(k => row[k] !== undefined && row[k] !== null);
    const tally = row.abstained ? 'abstained'
      : `${scored.length} score${scored.length === 1 ? '' : 's'}`;
    det.innerHTML = questionSummary(row.id, row.type, row.difficulty, tally)
      + questionHead(row.id, '')
      + '<div class="gen-answers">'
      + '<div class="gen-answer gen-answer--ideal"><h4>what the diary says</h4>'
      + `<div dir="rtl">${escapeHtml(gt.answer_fa || '—')}</div></div>`
      + '<div class="gen-answer gen-answer--actual">'
      + `<h4>what this run wrote${row.abstained ? ' — it refused' : ''}</h4>`
      + `<div dir="rtl">${escapeHtml(row.answer || '—')}</div></div>`
      + '</div>'
      + metricLine(row, GEN_METRICS);
    const trace = traces && traces.get(row.id);
    if (trace) {
      const inner = document.createElement('details');
      inner.className = 'gen-trace';
      inner.innerHTML = '<summary>the retrieval this answer was written from</summary>';
      inner.appendChild(scrollable(retrievalTable(trace.candidates || [])));
      det.appendChild(inner);
    }
    host.appendChild(det);
  }
}

// --- Auto-follow: poll the lab (:9002) through our own /api/follow every ~2s,
// and only touch the DOM when the followed job actually changed — a tab
// re-rendering on every tick would collapse the user's expanded <details>. ---

function showLabDown(el) {
  el.textContent = 'Nothing to show until the lab is running. Start it with '
    + '`npm run raglab`.';
}

// One line in the header, so an empty view never leaves the reader guessing
// whether nothing ran or nothing is listening.
function setFollowState(body) {
  const el = document.getElementById('follow-state');
  el.dataset.lab = body.lab;
  el.textContent = body.lab === 'up'
    ? `following the lab at ${body.lab_url}`
    : `cannot reach the lab at ${body.lab_url}`;
}

function renderFollow(body) {
  const chunksCfg = document.getElementById('chunks-active-config');
  const retrievalCfg = document.getElementById('retrieval-active-config');
  const setCfg = document.getElementById('retrieval-set-config');
  const genCfg = document.getElementById('generation-active-config');
  setFollowState(body);

  if (body.lab === 'down') {
    showLabDown(chunksCfg);
    showLabDown(retrievalCfg);
    showLabDown(setCfg);
    showLabDown(genCfg);
    return;
  }

  if (body.generation) {
    if (body.generation.job_id !== followed.generationJobId) {
      followed.generationJobId = body.generation.job_id;
      const n = (body.generation.rows || []).length;
      // Only an evaluation generates, and the last one may be older than the
      // retrieval tables next door. Say which run this is, and hand the tables
      // over only when they belong to this same run.
      const sameRun = body.retrieval && body.retrieval.job_id === body.generation.job_id;
      const traces = sameRun
        ? new Map(body.retrieval.questions.map(q => [q.question_id, q.trace]))
        : null;
      genCfg.textContent = `${n} question${n === 1 ? '' : 's'} answered by the last `
        + `evaluation — ${formatConfig(body.generation.config)}`
        + (sameRun ? '' : ' (the Retrieval tab is showing a different, newer run)');
      renderGeneration(body.generation, traces);
    }
  } else {
    // An empty view is a place to say what to do next, not to report a state
    // the header already reports.
    genCfg.textContent = 'Run an evaluation from "3 · Generation & scoring" on '
      + 'the lab to see answers here. Retrieve on its own does not write any.';
    document.getElementById('generation-questions').innerHTML = '';
    document.getElementById('generation-ragas').innerHTML = '';
  }

  if (body.retrieval) {
    if (body.retrieval.job_id !== followed.retrievalJobId) {
      followed.retrievalJobId = body.retrieval.job_id;
      const n = body.retrieval.questions.length;
      const source = body.retrieval.kind === 'run'
        ? 'evaluation' : 'retrieval run';
      setCfg.textContent = `${n} selected question${n === 1 ? '' : 's'} from the `
        + `last ${source} — ${formatConfig(body.retrieval.config)}`;
      renderQuestionTables(body.retrieval.questions);
    }
  } else {
    setCfg.textContent = 'Press Retrieve on the lab, or run an evaluation, to '
      + 'get one table per selected question.';
  }

  if (body.index) {
    if (body.index.job_id !== followed.indexJobId) {
      followed.indexJobId = body.index.job_id;
      const groups = body.index.chunks_by_session || [];
      const total = groups.reduce((n, g) => n + g.chunks.length, 0);
      // Which run built these, because an experiment builds its own index: "the
      // chunks the evaluation used" and "the chunks you last pressed Build on"
      // are different claims and the reader has to be able to tell them apart.
      const source = { run: 'the last evaluation', retrieve: 'the last retrieval run' }[body.index.kind]
        || 'the last index build';
      chunksCfg.textContent = `${total} chunks in ${groups.length} sessions, `
        + `from ${source} — ${formatConfig(body.index.config)}`;
      renderChunkGroups(document.getElementById('chunks-body'), groups);
    }
  } else {
    chunksCfg.textContent = 'Build an index on the lab to read its chunks here.';
  }

  if (body.query) {
    if (body.query.job_id !== followed.queryJobId) {
      followed.queryJobId = body.query.job_id;
      retrievalCfg.textContent = formatConfig(body.query.config);
      renderRetrievalRows(body.query.trace.candidates);
      document.getElementById('retrieval-answer').textContent = body.query.answer || '';
    }
  } else {
    retrievalCfg.textContent = 'Ask one question from the query box on the lab '
      + 'to trace it here.';
  }
}

async function pollFollow() {
  try {
    renderFollow(await (await fetch('/api/follow')).json());
  } catch (error) {
    // A hiccup fetching our own origin — try again next tick rather than
    // treating a transient failure as "the lab is down".
  } finally {
    setTimeout(pollFollow, 2000);
  }
}
pollFollow();
