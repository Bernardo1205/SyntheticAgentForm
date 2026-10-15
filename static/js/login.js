(() => {
  const params = new URLSearchParams(window.location.search);
  const errorLine = document.getElementById('errorLine');
  if (!errorLine) return;

  const authError = params.get('auth_error');
  if (authError) {
    errorLine.textContent = 'Login failed: ' + authError;
  }
})();
