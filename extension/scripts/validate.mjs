import fs from "node:fs";
import path from "node:path";
import vm from "node:vm";
import { fileURLToPath } from "node:url";

const scriptDirectory = path.dirname(fileURLToPath(import.meta.url));
const extensionRoot = path.resolve(scriptDirectory, "..");
const sourceRoot = path.join(extensionRoot, "src");

for (const file of ["background.js", "sidepanel.js", "options.js"]) {
  const source = fs.readFileSync(path.join(sourceRoot, file), "utf8");
  new vm.Script(source, { filename: file });
}

for (const browser of ["chromium", "firefox"]) {
  const manifest = JSON.parse(
    fs.readFileSync(path.join(extensionRoot, "manifests", `${browser}.json`), "utf8")
  );
  if (manifest.manifest_version !== 3) {
    throw new Error(`${browser} manifest must use Manifest V3`);
  }
  for (const file of ["background.js", "sidepanel.html", "options.html"]) {
    if (!fs.existsSync(path.join(sourceRoot, file))) {
      throw new Error(`Missing shared extension file: ${file}`);
    }
  }
}

console.log("Extension JavaScript and manifests are valid.");
