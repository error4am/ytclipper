const form = document.getElementById("login-form");
const input = document.getElementById("password");
const error = document.getElementById("login-error");
const button = form.querySelector("button");

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  error.textContent = "";
  button.disabled = true;
  try {
    const response = await fetch("/api/login", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ password: input.value }),
    });
    const data = await response.json();
    if (!data.ok) {
      error.textContent = data.error || "could not sign in";
      input.select();
      return;
    }
    location.replace("/");
  } catch (_err) {
    error.textContent = "cannot reach the clipper";
  } finally {
    button.disabled = false;
  }
});
