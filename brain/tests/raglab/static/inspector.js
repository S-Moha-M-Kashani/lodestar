// The Inspector's whole frontend: three views over the read-only :9003 API,
// two of which now auto-follow whatever the lab (:9002) actually ran.
const CHOSEN = {
  index: { chunker: 'semantic-drift', embedder: 'sentence-transformers' },
  retrieval: { retriever: 'hybrid-rrf', k: 8, reranker: 'lexical',
               time_filter: true, grader: 'llm', grade_threshold: 0.4 },
};

const views = ['groundtruth', 'chunks', 'retrieval'];
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

// --- Ground truth ---
async function loadGroundTruth() {
  const body = await (await fetch('/api/groundtruth')).json();
  const root = document.getElementById('view-groundtruth');
  root.innerHTML = '';
  const qsel = document.getElementById('retrieval-question');
  for (const q of body.questions) {
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
function renderRetrievalRows(candidates) {
  const table = document.querySelector('.retrieval-table');
  const rows = document.getElementById('retrieval-rows');
  table.hidden = false;
  rows.innerHTML = '';
  for (const c of candidates) {
    const tr = document.createElement('tr');
    tr.className = 'retrieval-row ' + (c.gold ? 'retrieval-row--gold' : 'retrieval-row--plain');
    const cell = v => (v === null || v === undefined) ? '·' : v;
    tr.innerHTML = `<td dir="rtl">${(c.text || '').slice(0, 60)}</td>
      <td>${cell(c.dense_rank)}</td><td>${cell(c.bm25_rank)}</td>
      <td>${cell(c.fused_rank)}</td><td>${cell(c.rerank_score)}</td>
      <td>${cell(c.grade_score)}</td><td>${c.kept ? '✓' : '✗'}</td>
      <td>${c.gold ? '●' : ''}</td>`;
    rows.appendChild(tr);
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

// --- Auto-follow: poll the lab (:9002) through our own /api/follow every ~2s,
// and only touch the DOM when the followed job actually changed — a tab
// re-rendering on every tick would collapse the user's expanded <details>. ---
const followed = { indexJobId: null, queryJobId: null };

function showLabDown(el) {
  el.textContent = 'lab: down — start it with `npm run raglab`';
}

function renderFollow(body) {
  const chunksCfg = document.getElementById('chunks-active-config');
  const retrievalCfg = document.getElementById('retrieval-active-config');

  if (body.lab === 'down') {
    showLabDown(chunksCfg);
    showLabDown(retrievalCfg);
    return;
  }

  if (body.index) {
    if (body.index.job_id !== followed.indexJobId) {
      followed.indexJobId = body.index.job_id;
      chunksCfg.textContent = formatConfig(body.index.config);
      renderChunkGroups(document.getElementById('chunks-body'),
                        body.index.chunks_by_session || []);
    }
  } else {
    chunksCfg.textContent = 'lab: up — no finished index job yet';
  }

  if (body.query) {
    if (body.query.job_id !== followed.queryJobId) {
      followed.queryJobId = body.query.job_id;
      retrievalCfg.textContent = formatConfig(body.query.config);
      renderRetrievalRows(body.query.trace.candidates);
      document.getElementById('retrieval-answer').textContent = body.query.answer || '';
    }
  } else {
    retrievalCfg.textContent = 'lab: up — no finished query job yet';
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
