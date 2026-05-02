const titleEl    = document.getElementById('page-title');
const urlEl      = document.getElementById('page-url');
const addBtn     = document.getElementById('add-btn');
const statusEl   = document.getElementById('status');
const serverInput = document.getElementById('server-url');
const apiKeyInput = document.getElementById('api-key');

let currentUrl   = '';
let currentTitle = '';

// Load saved settings and current tab info on popup open
chrome.storage.local.get(['serverUrl', 'apiKey'], (data) => {
  if (data.serverUrl) serverInput.value = data.serverUrl;
  if (data.apiKey)    apiKeyInput.value = data.apiKey;
});

chrome.tabs.query({ active: true, currentWindow: true }, (tabs) => {
  if (tabs && tabs[0]) {
    currentUrl   = tabs[0].url   || '';
    currentTitle = tabs[0].title || currentUrl;
    titleEl.textContent = currentTitle;
    urlEl.textContent   = currentUrl;
  }
});

// Save settings on change
serverInput.addEventListener('change', () => {
  chrome.storage.local.set({ serverUrl: serverInput.value.trim() });
});
apiKeyInput.addEventListener('change', () => {
  chrome.storage.local.set({ apiKey: apiKeyInput.value.trim() });
});

// Add to SearchX
addBtn.addEventListener('click', () => {
  const serverUrl = serverInput.value.trim();
  if (!serverUrl) {
    showStatus('Please enter your SearchX server URL.', 'error');
    return;
  }
  if (!currentUrl) {
    showStatus('No URL to add.', 'error');
    return;
  }

  addBtn.disabled = true;
  addBtn.textContent = 'Adding…';
  statusEl.style.display = 'none';

  const payload = {
    url:     currentUrl,
    title:   currentTitle,
    api_key: apiKeyInput.value.trim(),
  };

  fetch(`${serverUrl}/api/extension/add`, {
    method:  'POST',
    headers: { 'Content-Type': 'application/json' },
    body:    JSON.stringify(payload),
  })
    .then((r) => r.json().then((d) => ({ ok: r.ok, data: d })))
    .then(({ ok, data }) => {
      if (ok && data.ok) {
        showStatus(data.message || 'Page added successfully!', 'success');
      } else {
        showStatus(data.error || 'Failed to add page.', 'error');
      }
    })
    .catch((err) => {
      showStatus(`Connection error: ${err.message}`, 'error');
    })
    .finally(() => {
      addBtn.disabled = false;
      addBtn.textContent = 'Add to SearchX';
    });
});

function showStatus(msg, type) {
  statusEl.textContent  = msg;
  statusEl.className    = `status ${type}`;
}
