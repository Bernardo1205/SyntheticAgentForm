(() => {
  const API_BASE = window.location.origin;

  const form = document.getElementById('formUrlForm');
  if (!form) return;

  const submitBtn = document.getElementById('submitBtn');
  const loading = document.getElementById('loading');
  const errorBox = document.getElementById('errorBox');
  const resultsWrap = document.getElementById('resultsWrap');
  const formTitle = document.getElementById('formTitle');
  const formDescription = document.getElementById('formDescription');
  const formIdLine = document.getElementById('formIdLine');
  const qList = document.getElementById('qList');
  const syntheticConfigForm = document.getElementById('syntheticConfigForm');
  const selectedFormHint = document.getElementById('selectedFormHint');
  const generationStatus = document.getElementById('generationStatus');
  const statusText = document.getElementById('statusText');
  const statusProgressFill = document.getElementById('statusProgressFill');
  const statusDetails = document.getElementById('statusDetails');
  const generateBtn = document.getElementById('generateBtn');

  const panelMeta = document.getElementById('panelMeta');
  const analyticsEmpty = document.getElementById('analyticsEmpty');
  const analyticsContent = document.getElementById('analyticsContent');
  const diversityScore = document.getElementById('diversityScore');
  const diversityBar = document.getElementById('diversityBar');
  const diversityBreakdown = document.getElementById('diversityBreakdown');
  const archetypeList = document.getElementById('archetypeList');
  const demographicsEl = document.getElementById('demographics');
  const personaCards = document.getElementById('personaCards');

  let currentFormId = null;

  const STAGE_MESSAGES = {
    pending: 'Starting…',
    loading_form: 'Loading form structure…',
    generating_personas: 'Generating personas…',
    simulating_answers: 'Simulating answers…',
    submitting: 'Submitting to Google Forms…',
    computing_statistics: 'Computing statistics…',
    done: 'Done',
  };

  function extractFormId(input) {
    const trimmed = input.trim();
    const match = trimmed.match(/forms\/d\/(?:e\/)?([a-zA-Z0-9_-]+)/);
    if (match) return match[1];
    return trimmed;
  }

  function humanizeType(type) {
    if (!type) return 'Unknown';
    return type.replace(/Question$/, '').replace(/([A-Z])/g, ' $1').trim();
  }

  function escapeHtml(str) {
    const div = document.createElement('div');
    div.textContent = str == null ? '' : String(str);
    return div.innerHTML;
  }

  function newJobId() {
    if (window.crypto && typeof window.crypto.randomUUID === 'function') return window.crypto.randomUUID();
    return `job-${Date.now()}-${Math.random().toString(16).slice(2)}`;
  }

  function renderResults(data) {
    currentFormId = data.formId || null;
    formTitle.textContent = data.title || '(untitled form)';
    formDescription.textContent = data.description || '';
    formDescription.style.display = data.description ? 'block' : 'none';
    formIdLine.textContent = `Form ID: ${data.formId}`;
    if (selectedFormHint) {
      selectedFormHint.textContent = currentFormId
        ? `Selected form: ${data.title || '(untitled form)'} (${currentFormId})`
        : 'Load a form first to enable generation.';
    }

    qList.innerHTML = '';
    if (!data.questions || data.questions.length === 0) {
      qList.innerHTML = '<p style="color:var(--muted);font-size:13px;">No questions found on this form.</p>';
    } else {
      data.questions.forEach((q, i) => {
        const item = document.createElement('div');
        item.className = 'q-item';
        item.innerHTML = `
          <div class="q-title"><span class="q-index">${i + 1}.</span>${escapeHtml(q.title || '(untitled question)')}</div>
          <div class="q-meta">
            <span class="badge">${escapeHtml(humanizeType(q.type))}</span>
            ${q.required ? '<span class="badge required">Required</span>' : ''}
          </div>
        `;
        qList.appendChild(item);
      });
    }
    resultsWrap.classList.add('visible');
  }

  // --- Status banner -----------------------------------------------------------

  function setGenerationStatus(message, state, details) {
    if (!generationStatus) return;
    statusText.textContent = message;
    generationStatus.classList.add('visible');
    generationStatus.classList.toggle('running', state === 'running');
    generationStatus.classList.toggle('error', state === 'error');
    generationStatus.classList.toggle('success', state === 'success');
    statusDetails.innerHTML = (details || []).map((d) => `<li>${escapeHtml(d)}</li>`).join('');
  }

  function setProgress(done, total) {
    if (!statusProgressFill) return;
    if (typeof done === 'number' && typeof total === 'number' && total > 0) {
      statusProgressFill.classList.remove('indeterminate');
      statusProgressFill.style.width = `${Math.min(100, Math.round((done / total) * 100))}%`;
    } else {
      statusProgressFill.classList.add('indeterminate');
      statusProgressFill.style.width = '';
    }
  }

  function applyProgress(progress) {
    const message = progress.message || STAGE_MESSAGES[progress.stage] || 'Working…';
    setGenerationStatus(message, 'running');
    setProgress(progress.done, progress.total);
  }

  function startProgressPolling(jobId) {
    let stopped = false;
    const tick = async () => {
      if (stopped) return;
      try {
        const res = await fetch(`${API_BASE}/generation-status/${encodeURIComponent(jobId)}`, { credentials: 'include' });
        if (res.ok) {
          const progress = await res.json();
          if (!stopped && progress.stage !== 'done' && progress.stage !== 'error') applyProgress(progress);
        }
      } catch (_) { /* ignore transient polling errors */ }
      if (!stopped) setTimeout(tick, 700);
    };
    setTimeout(tick, 400);
    return () => { stopped = true; };
  }

  // --- Analytics renderer ------------------------------------------------------

  function barRow(label, value, pct, colorClass) {
    return `
      <div class="stat-row">
        <span class="stat-label">${escapeHtml(label)}</span>
        <span class="stat-value">${escapeHtml(value)}</span>
        <div class="bar-track"><div class="bar-fill ${colorClass || ''}" data-width="${pct}"></div></div>
      </div>`;
  }

  function animateBars(root) {
    const fills = root.querySelectorAll('.bar-fill[data-width]');
    fills.forEach((el) => { el.style.width = '0%'; });
    requestAnimationFrame(() => {
      requestAnimationFrame(() => {
        fills.forEach((el) => { el.style.width = `${Math.max(0, Math.min(100, Number(el.dataset.width) || 0))}%`; });
      });
    });
  }

  function initials(name) {
    return (name || '?').split(/\s+/).filter(Boolean).slice(0, 2).map((p) => p[0].toUpperCase()).join('') || '?';
  }

  function renderAnalytics(data) {
    const meta = data.meta || {};
    const total = meta.num_responses || 0;
    const modeLabel = data.submission_mode === 'live' ? 'submitted to Google Forms'
      : data.submission_mode === 'partial' ? 'partially submitted'
      : data.submission_mode === 'mock' ? 'mock submission' : 'submission failed';
    panelMeta.textContent = `${total} personas · ${data.submitted_count}/${total} ${modeLabel} · ${meta.llm_provider === 'mock' ? 'offline generator' : (meta.model || 'LLM')}`;

    // Diversity index
    const score = parseFloat(String(meta.diversity_score || '0').replace('%', '')) || 0;
    diversityScore.textContent = meta.diversity_score || `${Math.round(score)}%`;
    diversityBar.dataset.width = score;
    const breakdown = data.diversity_breakdown || {};
    diversityBreakdown.innerHTML = Object.keys(breakdown)
      .map((k) => `<span class="chip">${escapeHtml(k)} <strong>${Math.round(breakdown[k])}%</strong></span>`)
      .join('');

    // Archetypes
    archetypeList.innerHTML = (data.archetypes || []).map((a, i) => `
      <li class="archetype-item">
        <div class="archetype-head">
          <span class="archetype-name">${escapeHtml(a.name)}</span>
          <span class="badge count">${a.count} · ${a.percentage}%</span>
        </div>
        <div class="bar-track"><div class="bar-fill c${i % 7}" data-width="${a.percentage}"></div></div>
        ${a.description ? `<p class="archetype-desc">${escapeHtml(a.description)}</p>` : ''}
      </li>`).join('') || '<li class="archetype-desc">No archetypes returned.</li>';

    // Demographics
    const groups = [
      ['Age', data.demographics && data.demographics.age_ranges],
      ['Gender', data.demographics && data.demographics.gender],
      ['Income', data.demographics && data.demographics.income_tiers],
      ['Education', data.demographics && data.demographics.education],
      ['Employment', data.demographics && data.demographics.employment],
    ];
    demographicsEl.innerHTML = groups
      .filter(([, entries]) => Array.isArray(entries) && entries.length)
      .map(([title, entries]) => `
        <div class="demo-group">
          <div class="demo-title">${escapeHtml(title)}</div>
          ${entries.map((e, i) => barRow(e.label, `${e.count} · ${e.percentage}%`, e.percentage, `c${i % 7}`)).join('')}
        </div>`).join('');

    // Persona spotlight
    personaCards.innerHTML = (data.sample_profiles || []).map((p) => `
      <article class="persona-card">
        <div class="persona-avatar">${escapeHtml(initials(p.name))}</div>
        <div>
          <div class="persona-name">${escapeHtml(p.name)} <span class="badge accent">${escapeHtml(p.archetype)}</span></div>
          <div class="persona-line">${escapeHtml(`${p.age} · ${p.gender} · ${p.location}`)}</div>
          <div class="persona-line">${escapeHtml(`${p.employment} · ${p.education} · ${p.income_bracket} income`)}</div>
          ${p.psychographic_summary ? `<p class="persona-summary">${escapeHtml(p.psychographic_summary)}</p>` : ''}
          ${(p.answers || []).length ? `<ul class="persona-answers">${p.answers.map((a) => `<li><strong>${escapeHtml(a.question)}</strong> — ${escapeHtml(a.answer)}</li>`).join('')}</ul>` : ''}
        </div>
      </article>`).join('') || '<p class="persona-line">No sample personas returned.</p>';

    analyticsEmpty.hidden = true;
    analyticsContent.hidden = false;
    animateBars(analyticsContent);
  }

  // --- Form loader -------------------------------------------------------------

  form.addEventListener('submit', async (e) => {
    e.preventDefault();
    errorBox.classList.remove('visible');
    resultsWrap.classList.remove('visible');

    const rawUrl = document.getElementById('form_url').value;
    const formId = extractFormId(rawUrl);
    if (!formId) {
      errorBox.textContent = "Couldn't find a form ID in that URL.";
      errorBox.classList.add('visible');
      return;
    }

    submitBtn.disabled = true;
    loading.classList.add('visible');

    try {
      const res = await fetch(`${API_BASE}/api/forms/${encodeURIComponent(formId)}`, {
        credentials: 'include',
      });
      const data = await res.json();

      if (!res.ok) {
        errorBox.textContent = data.detail || 'Failed to load form.';
        errorBox.classList.add('visible');
        return;
      }

      renderResults(data);
    } catch (err) {
      errorBox.textContent = 'Network error — is the server running?';
      errorBox.classList.add('visible');
    } finally {
      submitBtn.disabled = false;
      loading.classList.remove('visible');
    }
  });

  // --- Synthetic generation ----------------------------------------------------

  function describeDetail(detail) {
    if (Array.isArray(detail)) {
      return detail.map((d) => (d.loc ? `${d.loc[d.loc.length - 1]}: ${d.msg}` : d.msg || JSON.stringify(d))).join('; ');
    }
    return detail;
  }

  if (syntheticConfigForm) {
    syntheticConfigForm.addEventListener('submit', async (e) => {
      e.preventDefault();
      generationStatus.classList.remove('visible', 'error', 'success', 'running');

      if (!currentFormId) {
        setGenerationStatus('Please load a Google Form first.', 'error');
        return;
      }

      const numResponses = parseInt(document.getElementById('num_responses').value, 10);
      const targetAudience = document.getElementById('target_audience').value.trim();
      const formUrl = document.getElementById('form_url').value.trim();

      if (!numResponses || numResponses < 1) {
        setGenerationStatus('Number of responses must be greater than 0.', 'error');
        return;
      }
      if (!targetAudience) {
        setGenerationStatus('Target audience is required.', 'error');
        return;
      }

      const jobId = newJobId();
      generateBtn.disabled = true;
      setGenerationStatus('Starting generation…', 'running');
      setProgress(null, null);
      const stopPolling = startProgressPolling(jobId);

      try {
        const body = new URLSearchParams();
        body.set('form_url', formUrl || currentFormId);
        body.set('num_responses', String(numResponses));
        body.set('target_audience', targetAudience);
        body.set('job_id', jobId);

        const res = await fetch(`${API_BASE}/submit-form-url`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
          credentials: 'include',
          body: body.toString(),
        });

        const data = await res.json();
        stopPolling();
        if (!res.ok) {
          setGenerationStatus(describeDetail(data.detail) || 'Failed to submit generation request.', 'error');
          return;
        }

        renderAnalytics(data);
        const submittedLine = data.submission_mode === 'live'
          ? `${data.submitted_count} responses submitted to the Google Form.`
          : data.submission_mode === 'mock'
            ? `${data.submitted_count} responses generated (mock submission — not written to the form).`
            : `${data.submitted_count}/${data.meta.num_responses} responses submitted.`;
        setGenerationStatus(
          `Generated ${data.meta.num_responses} personas in ${data.meta.duration_seconds}s. ${submittedLine}`,
          data.submission_mode === 'failed' ? 'error' : 'success',
          data.submission_errors,
        );
      } catch (err) {
        stopPolling();
        setGenerationStatus('Network error while submitting generation request.', 'error');
      } finally {
        stopPolling();
        generateBtn.disabled = false;
      }
    });
  }
})();
