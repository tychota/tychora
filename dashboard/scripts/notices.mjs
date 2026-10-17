// Only the production bundle dependencies; versions and notices come from the lock install.
import { readFile, mkdir, writeFile } from "node:fs/promises";
const packages = ["react", "react-dom", "scheduler"];
const notices = await Promise.all(
  packages.map(async (name) => {
    const metadata = JSON.parse(
      await readFile(`node_modules/${name}/package.json`, "utf8"),
    );
    return `${name}@${metadata.version}\n${await readFile(`node_modules/${name}/LICENSE`, "utf8")}`;
  }),
);
await mkdir("public/assets", { recursive: true });
await writeFile("public/assets/THIRD-PARTY-NOTICES.txt", notices.join("\n\n"));
