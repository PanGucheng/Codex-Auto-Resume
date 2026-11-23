import { readFile, writeFile } from "node:fs/promises";
import { build } from "esbuild";

const { outputFiles: [script] } = await build({
  entryPoints: ["panel.ts"], bundle: true, write: false, minify: true,
  target: "es2022", format: "iife", logLevel: "warning",
});
const css = await readFile("panel.css", "utf8");
const markup = await readFile("index.html", "utf8");
const license = await readFile("../LICENSE", "utf8");
const notices = await readFile("../THIRD_PARTY_NOTICES.txt", "utf8");
const html = markup.replace("<!-- PANEL_STYLE -->", () => `<style>${css}</style>`)
  .replace("<!-- PANEL_LICENSES -->", () => `<!--\n${license}\n${notices}-->`)
  .replace("<!-- PANEL_SCRIPT -->", () => `<script>${script.text.replaceAll("</script", "<\\/script")}</script>`);
await writeFile("../plugins/codex-auto-resume/panel.html", html);
