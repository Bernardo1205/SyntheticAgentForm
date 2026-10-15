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
  const generateBtn = document.getElementById('generateBtn');
  let currentFormId = null;

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
    div.textContent = str;
    return div.innerHTML;
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

  function setGenerationStatus(message, isError) {
    if (!generationStatus) return;
    generationStatus.textContent = message;
    generationStatus.classList.add('visible');
    generationStatus.classList.toggle('error', !!isError);
    generationStatus.classList.toggle('success', !isError);
  }

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

  if (syntheticConfigForm) {
    syntheticConfigForm.addEventListener('submit', async (e) => {
      e.preventDefault();
      generationStatus.classList.remove('visible', 'error', 'success');

      if (!currentFormId) {
        setGenerationStatus('Please load a Google Form first.', true);
        return;
      }

      const numResponses = parseInt(document.getElementById('num_responses').value, 10);
      const targetAudience = document.getElementById('target_audience').value.trim();
      const formUrl = document.getElementById('form_url').value.trim();

      if (!numResponses || numResponses < 1) {
        setGenerationStatus('Number of responses must be greater than 0.', true);
        return;
      }
      if (!targetAudience) {
        setGenerationStatus('Target audience is required.', true);
        return;
      }

      generateBtn.disabled = true;
      try {
        const body = new URLSearchParams();
        body.set('form_url', formUrl || currentFormId);
        body.set('num_responses', String(numResponses));
        body.set('target_audience', targetAudience);

        const res = await fetch(`${API_BASE}/submit-form-url`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
          credentials: 'include',
          body: body.toString(),
        });

        const data = await res.json();
        if (!res.ok) {
          setGenerationStatus(data.detail || 'Failed to submit generation request.', true);
          return;
        }

        setGenerationStatus(data.message || 'Generation request submitted.', false);
      } catch (err) {
        setGenerationStatus('Network error while submitting generation request.', true);
      } finally {
        generateBtn.disabled = false;
      }
    });
  }
})();

