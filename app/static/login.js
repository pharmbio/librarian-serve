// Sign-in page: one form, two modes (sign in / create account).
const $ = (selector) => document.querySelector(selector);
const form = $("#auth-form");

const MODES = {
  login: {
    title: "Welcome back",
    submit: "Sign in",
    hint: "Passwords are stored as argon2 hashes.",
    autocomplete: "current-password",
  },
  register: {
    title: "Create your account",
    submit: "Create account",
    hint: "Passwords need at least 8 characters.",
    autocomplete: "new-password",
  },
};
let mode = "login";

// The same loose check as the server's EMAIL_PATTERN (../main.py).
const EMAIL_PATTERN = /^[^@\s]+@[^@\s]+\.[^@\s]+$/;
const svg = (path) => `<svg class="i" viewBox="0 0 24 24" aria-hidden="true">${path}</svg>`;
const EMAIL_OK = `${svg('<path d="M20 6 9 17l-5-5"/>')} Valid email address`;
const EMAIL_BAD = `${svg('<circle cx="12" cy="12" r="10"/><path d="M12 8v4M12 16h.01"/>')} Enter a valid email, like name@institute.org`;
let emailTouched = false; // no red while typing a first address, only once the reader leaves the field

// Where to go once signed in: the page that sent the reader here (?next=), but
// only a page on this site. Resolving it as a URL catches "//host" and "/\host",
// which browsers read as another site.
function nextPage() {
  try {
    const url = new URL(new URLSearchParams(location.search).get("next") ?? "/", location.origin);
    if (url.origin === location.origin) return url.pathname + url.search + url.hash;
  } catch {} // not a URL at all
  return "/";
}

// Show under the email field whether the address is valid. Returns validity.
function checkEmail() {
  const value = form.elements.email.value.trim();
  const valid = EMAIL_PATTERN.test(value);
  const shown = Boolean(value) && (valid || emailTouched);
  $("#email-field").classList.toggle("valid", shown && valid);
  $("#email-field").classList.toggle("invalid", shown && !valid);
  form.elements.email.setAttribute("aria-invalid", String(shown && !valid));
  $("#email-status").hidden = !shown;
  $("#email-status").innerHTML = valid ? EMAIL_OK : EMAIL_BAD;
  return valid;
}
form.elements.email.addEventListener("input", checkEmail);
form.elements.email.addEventListener("blur", () => {
  emailTouched = true;
  checkEmail();
});

function setMode(next) {
  mode = next;
  const copy = MODES[mode];
  $("#auth-title").textContent = copy.title;
  $("#auth-submit").textContent = copy.submit;
  $("#auth-hint").textContent = copy.hint;
  form.elements.password.autocomplete = copy.autocomplete;
  document.querySelectorAll(".register-only").forEach((field) => (field.hidden = mode !== "register"));
  $("#auth-error").hidden = true;
  document.querySelectorAll(".tabs button").forEach((tab) => tab.classList.toggle("active", tab.dataset.mode === mode));
}

document.querySelectorAll(".tabs button").forEach((tab) => tab.addEventListener("click", () => setMode(tab.dataset.mode)));

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  const error = $("#auth-error");
  const submit = $("#auth-submit");
  const body = { email: form.elements.email.value.trim(), password: form.elements.password.value };
  if (mode === "register") {
    body.institution = form.elements.institution.value.trim();
    body.position = form.elements.position.value.trim();
  }
  emailTouched = true;
  if (Object.values(body).some((value) => !value)) {
    checkEmail();
    error.textContent = "Fill in every field marked *.";
    error.hidden = false;
    return;
  }
  if (!checkEmail()) {
    error.hidden = true; // the message under the email field says why
    form.elements.email.focus();
    return;
  }
  submit.disabled = true;
  error.hidden = true;
  try {
    const response = await fetch(`/api/auth/${mode}`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(body),
    });
    if (response.ok) {
      // Confirm the browser kept the session cookie. Otherwise the app would
      // bounce straight back here with no explanation.
      if ((await fetch("/api/me")).ok) return location.replace(nextPage());
      error.textContent = "You're signed in, but this browser didn't keep the session cookie. Allow cookies for this site and try again.";
    } else {
      const reply = await response.json().catch(() => ({}));
      error.textContent = typeof reply.detail === "string" ? reply.detail : "Something went wrong. Try again.";
    }
  } catch {
    error.textContent = "Can't reach the server.";
  }
  error.hidden = false;
  submit.disabled = false;
});

// Already signed in? Skip straight to the app.
fetch("/api/me").then((response) => response.ok && location.replace(nextPage()));
form.elements.email.focus();
