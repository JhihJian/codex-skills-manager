(() => {
  const state = { checked: false, authenticated: false, csrfToken: "", actor: null, loginPromise: null };

  function createLoginDialog() {
    const dialog = document.createElement("dialog");
    dialog.className = "auth-dialog";
    dialog.innerHTML = `
      <form method="dialog" class="auth-form">
        <div class="auth-mark" aria-hidden="true">CS</div>
        <div><h2>本机访问验证</h2><p>使用服务启动时生成的访问密钥。</p></div>
        <label><span>访问密钥</span><input name="token" type="password" autocomplete="current-password" required /></label>
        <p class="field-error" data-auth-error hidden></p>
        <button class="primary-button" value="login">登录</button>
      </form>`;
    document.body.appendChild(dialog);
    return dialog;
  }

  async function readStatus() {
    if (state.checked) return state;
    const response = await window.fetch("/api/auth/status", { headers: { Accept: "application/json" } });
    const payload = await response.json();
    state.checked = true;
    state.authenticated = Boolean(payload.authenticated);
    state.csrfToken = payload.csrfToken || "";
    state.actor = payload.actor || null;
    return state;
  }

  function requestLogin() {
    if (state.loginPromise) return state.loginPromise;
    state.loginPromise = new Promise((resolve, reject) => {
      const dialog = document.querySelector(".auth-dialog") || createLoginDialog();
      const form = dialog.querySelector("form");
      const input = form.elements.token;
      const error = dialog.querySelector("[data-auth-error]");
      const submit = dialog.querySelector("button");
      const handler = async (event) => {
        event.preventDefault();
        submit.disabled = true;
        error.hidden = true;
        try {
          const response = await window.fetch("/api/auth/login", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ token: input.value }),
          });
          const payload = await response.json();
          if (!response.ok) throw new Error(payload.error || "访问密钥无效");
          state.checked = true;
          state.authenticated = true;
          state.csrfToken = payload.csrfToken || "";
          state.actor = payload.actor || null;
          input.value = "";
          dialog.close();
          resolve(state);
        } catch (cause) {
          error.textContent = cause.message;
          error.hidden = false;
          input.select();
          submit.disabled = false;
          return;
        }
        form.removeEventListener("submit", handler);
        state.loginPromise = null;
      };
      form.addEventListener("submit", handler);
      dialog.addEventListener("cancel", (event) => {
        event.preventDefault();
        reject(new Error("需要登录后继续"));
        form.removeEventListener("submit", handler);
        state.loginPromise = null;
      }, { once: true });
      dialog.showModal();
      window.setTimeout(() => input.focus(), 0);
    });
    return state.loginPromise;
  }

  async function authenticatedFetch(input, options = {}, retried = false) {
    await readStatus();
    if (!state.authenticated) await requestLogin();
    const method = String(options.method || "GET").toUpperCase();
    const headers = new Headers(options.headers || {});
    if (!["GET", "HEAD", "OPTIONS"].includes(method)) headers.set("X-CSRF-Token", state.csrfToken);
    const response = await window.fetch(input, { ...options, headers });
    if (response.status === 401 && !retried) {
      state.checked = true;
      state.authenticated = false;
      state.csrfToken = "";
      await requestLogin();
      return authenticatedFetch(input, options, true);
    }
    return response;
  }

  window.skillAuth = { fetch: authenticatedFetch, status: readStatus, login: requestLogin, state };
})();