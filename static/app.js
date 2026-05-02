// ── Local file open — show toast if blocked ────────────────────────────────
document.addEventListener('click', e => {
  const a = e.target.closest('a[href^="/open"]');
  if (!a) return;
  e.preventDefault();
  fetch(a.href)
    .then(r => {
      if (r.status === 403 || r.status === 404) {
        return r.json().then(j => showToast(j.error || 'Blocked', 'error'));
      }
    })
    .catch(() => showToast('Could not open file', 'error'));
});

function showToast(msg, type = 'success') {
  const t = document.createElement('div');
  t.className = `toast toast-${type}`;
  t.textContent = msg;
  document.body.appendChild(t);
  setTimeout(() => t.remove(), 3500);
}

// ── Autocomplete ───────────────────────────────────────────────────────────
const input = document.getElementById('search-input');
const list  = document.getElementById('autocomplete');

if (input && list) {
  let timer;
  input.addEventListener('input', () => {
    clearTimeout(timer);
    const q = input.value.trim().split(/\s+/).pop();
    if (q.length < 2) { list.innerHTML = ''; return; }
    timer = setTimeout(() => {
      fetch(`/api/suggest?q=${encodeURIComponent(q)}`)
        .then(r => r.json())
        .then(items => {
          list.innerHTML = items.map(s =>
            `<div class="autocomplete-item" data-val="${s}">${s}</div>`
          ).join('');
          list.querySelectorAll('.autocomplete-item').forEach(el => {
            el.addEventListener('mousedown', e => {
              e.preventDefault();
              const words = input.value.split(/\s+/);
              words[words.length - 1] = el.dataset.val;
              input.value = words.join(' ');
              list.innerHTML = '';
            });
          });
        });
    }, 200);
  });

  document.addEventListener('click', e => {
    if (!list.contains(e.target) && e.target !== input) list.innerHTML = '';
  });
}
