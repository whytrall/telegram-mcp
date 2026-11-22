let revision = null;
let stage = null;
let editingCredentials = false;
const $ = (selector) => document.querySelector(selector);

async function post(action, data) {
  const response = await fetch(action, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(data),
  });
  const result = await response.json();
  if (!response.ok) throw new Error(result.error);
}

async function update() {
  try {
    const response = await fetch('status', { cache: 'no-store' });
    if (!response.ok) throw new Error();
    const state = await response.json();
    if (state.authorized) editingCredentials = false;
    const credentials = state.stage === 'credentials' || editingCredentials;
    $('#credentials').hidden = !credentials;
    $('#qr-section').hidden = state.stage !== 'qr' || credentials;
    $('#recovery').hidden = state.stage !== 'error' || credentials;
    const verification = ['password', 'email', 'code', 'email_code'].includes(state.stage);
    $('#verification').hidden = !verification || credentials;
    $('#login-card').hidden = state.authorized || (
      !credentials && !verification && state.stage !== 'qr' && state.stage !== 'error'
    );
    if (state.stage !== stage) {
      $('#value').value = '';
      $('#error').textContent = state.error || '';
    }
    stage = state.stage;
    if (verification) {
      $('#verification-label').textContent = {
        password: 'Two-step verification password', email: 'Login email',
        code: 'Telegram login code', email_code: 'Email login code',
      }[stage];
      $('#value').type = stage === 'password' ? 'password' : stage === 'email' ? 'email' : 'text';
      $('#value').autocomplete = stage === 'password' ? 'current-password' : 'one-time-code';
    }
    if (state.stage === 'qr' && revision !== state.revision) {
      revision = state.revision;
      $('#qr').src = 'qr.svg?v=' + revision;
    }
    $('h1').textContent = state.authorized ? 'You’re connected' : 'Connect Telegram';
    $('#status').textContent = state.authorized ? 'Logged in. You can return to the chat.' :
      credentials ? 'First, connect your Telegram app.' : stage === 'qr' ? 'Scan to sign in.' :
      verification ? 'Complete the next login step.' : stage === 'error' ? state.error : 'Waiting for Telegram…';
  } catch {
    $('#status').textContent = 'The login page is disconnected.';
  }
}

async function submit(form, action, data) {
  const button = form.querySelector('button');
  button.disabled = true;
  $('#error').textContent = '';
  try {
    await post(action, data);
    editingCredentials = false;
  } catch (error) {
    $('#error').textContent = error.message;
  } finally {
    button.disabled = false;
    await update();
  }
}

$('#credentials').addEventListener('submit', async (event) => {
  event.preventDefault();
  const form = event.currentTarget;
  const data = { app_id: form.elements.app_id.value, app_hash: form.elements.app_hash.value };
  form.elements.app_hash.value = '';
  await submit(form, 'credentials', data);
});
$('#verification').addEventListener('submit', async (event) => {
  event.preventDefault();
  const data = { value: $('#value').value };
  $('#value').value = '';
  await submit(event.currentTarget, stage, data);
});
$('#retry').addEventListener('click', async () => {
  try { await post('retry', {}); await update(); }
  catch (error) { $('#error').textContent = error.message; }
});
$('#change-credentials').addEventListener('click', () => { editingCredentials = true; update(); });
update();
setInterval(update, 1000);
