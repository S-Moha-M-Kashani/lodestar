// The Inspector's whole frontend: three views over the read-only :9003 API.
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
    if (job.state === 'error') throw new Error(job.error);
    await new Promise(r => setTimeout(r, 500));
  }
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

// --- Chunks ---
document.getElementById('build-chunks').addEventListener('click', async () => {
  const status = document.getElementById('chunks-status');
  status.textContent = 'building…';
  const acc = await (await fetch('/api/chunks',
    { method: 'POST', headers: { 'content-type': 'application/json' },
      body: JSON.stringify(CHOSEN) })).json();
  const result = await pollJob(acc.job_id);
  status.textContent = `${result.total} chunks`;
  const body = document.getElementById('chunks-body');
  body.innerHTML = '';
  for (const g of result.chunks_by_session) {
    const det = document.createElement('details');
    det.className = 'chunk-session';
    det.innerHTML = `<summary>${g.session_id} (${g.chunks.length} chunks)</summary>`;
    g.chunks.forEach((c, i) => {
      const p = document.createElement('div');
      p.dir = 'rtl';
      p.textContent = `chunk ${i + 1}: ${c.text}`;
      det.appendChild(p);
    });
    body.appendChild(det);
  }
});

// --- Retrieval ---
document.getElementById('run-trace').addEventListener('click', async () => {
  const status = document.getElementById('retrieval-status');
  const qid = document.getElementById('retrieval-question').value;
  status.textContent = 'retrieving…';
  const acc = await (await fetch('/api/trace',
    { method: 'POST', headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ ...CHOSEN, question_id: qid }) })).json();
  const result = await pollJob(acc.job_id);
  status.textContent = qid;
  const table = document.querySelector('.retrieval-table');
  const rows = document.getElementById('retrieval-rows');
  table.hidden = false;
  rows.innerHTML = '';
  for (const c of result.trace.candidates) {
    const tr = document.createElement('tr');
    tr.className = 'retrieval-row ' + (c.gold ? 'retrieval-row--gold' : 'retrieval-row--plain');
    const cell = v => (v === null || v === undefined) ? '·' : v;
    tr.innerHTML = `<td dir="rtl">${c.text.slice(0, 60)}</td>
      <td>${cell(c.dense_rank)}</td><td>${cell(c.bm25_rank)}</td>
      <td>${cell(c.fused_rank)}</td><td>${cell(c.rerank_score)}</td>
      <td>${cell(c.grade_score)}</td><td>${c.kept ? '✓' : '✗'}</td>
      <td>${c.gold ? '●' : ''}</td>`;
    rows.appendChild(tr);
  }
});
