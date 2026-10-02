import { mount } from "svelte";
import App from "./App.svelte";
import "./app.css";
import "@fortawesome/fontawesome-free/css/fontawesome.min.css";
import "@fortawesome/fontawesome-free/css/solid.min.css";

// Apply the persisted theme before the first paint to avoid a flash.
const savedTheme = localStorage.getItem("ctx-theme");
if (
  savedTheme === "dark" ||
  (!savedTheme && window.matchMedia("(prefers-color-scheme: dark)").matches)
) {
  document.documentElement.classList.add("dark");
}

const app = mount(App, { target: document.getElementById("app") });

export default app;
