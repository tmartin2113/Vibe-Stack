#!/usr/bin/env node
/**
 * fetch-openclaw-skills.mjs
 *
 * Parses the VoltAgent awesome-openclaw-skills catalog and downloads
 * SKILL.md files into a local directory for use by Paperclip agents.
 *
 * Usage:
 *   node fetch-openclaw-skills.mjs [--concurrency 10] [--force]
 */

import fs from "node:fs";
import path from "node:path";
import https from "node:https";

const CATALOG_DIR = path.resolve("skill-sources/voltagent-skills/categories");
const OUTPUT_DIR = path.resolve("skill-sources/openclaw-skills/skills");

// Link patterns — the catalog has used different URL schemes over time:
// Old: https://github.com/openclaw/skills/tree/main/skills/<author>/<name>/SKILL.md
// New: https://clawskills.sh/skills/<author>-<skillname>
const LINK_RE_GITHUB = /\(https:\/\/github\.com\/openclaw\/skills\/tree\/main\/skills\/([^)]+\/SKILL\.md)\)/g;
const LINK_RE_CLAWSKILLS = /\[([\w-]+)\]\(https:\/\/clawskills\.sh\/skills\/([\w.-]+-[\w.-]+)\)/g;

// Category file → tag mapping for auto-tagging downloaded skills
const CATEGORY_TAGS = {
  "ai-and-llms": "ai",
  "browser-and-automation": "automation",
  "cli-utilities": "cli",
  "coding-agents-and-ides": "development",
  "communication": "communication",
  "data-and-analytics": "data",
  "devops-and-cloud": "devops",
  "git-and-github": "development",
  "image-and-video-generation": "media",
  "ios-and-macos-development": "development",
  "marketing-and-sales": "marketing",
  "notes-and-pkm": "knowledge",
  "pdf-and-documents": "docs",
  "productivity-and-tasks": "productivity",
  "search-and-research": "research",
  "security-and-passwords": "security",
  "web-and-frontend-development": "frontend",
};

const args = process.argv.slice(2);
const force = args.includes("--force");
const concurrencyIdx = args.indexOf("--concurrency");
const CONCURRENCY = concurrencyIdx >= 0 ? parseInt(args[concurrencyIdx + 1], 10) || 10 : 10;

function fetch(url) {
  return new Promise((resolve, reject) => {
    const req = https.get(url, { headers: { "User-Agent": "paperclip-skill-fetcher/1.0" } }, (res) => {
      if (res.statusCode >= 300 && res.statusCode < 400 && res.headers.location) {
        return fetch(res.headers.location).then(resolve, reject);
      }
      let data = "";
      res.on("data", (chunk) => (data += chunk));
      res.on("end", () => resolve({ status: res.statusCode, body: data }));
    });
    req.on("error", reject);
    req.setTimeout(15000, () => { req.destroy(); reject(new Error("timeout")); });
  });
}

async function sleep(ms) {
  return new Promise((r) => setTimeout(r, ms));
}

// Parse all category files and extract unique skill paths
function discoverSkills() {
  const categoryFiles = fs.readdirSync(CATALOG_DIR).filter((f) => f.endsWith(".md"));
  const skills = new Map(); // skillName -> { path, rawUrl, tags, description, author, clawskillsUrl }

  for (const file of categoryFiles) {
    const content = fs.readFileSync(path.join(CATALOG_DIR, file), "utf-8");
    const categoryBase = file.replace(".md", "");
    const tag = CATEGORY_TAGS[categoryBase] || "other";

    // Old-format links: github.com/openclaw/skills/tree/main/skills/<author>/<name>/SKILL.md
    for (const match of content.matchAll(LINK_RE_GITHUB)) {
      const skillPath = match[1]; // e.g. "author/skillname/SKILL.md"
      const parts = skillPath.split("/");
      if (parts.length < 3) continue;
      const author = parts[0];
      const skillName = parts[1];

      if (skills.has(skillName)) {
        const existing = skills.get(skillName);
        if (!existing.tags.includes(tag)) existing.tags.push(tag);
      } else {
        const lineRe = new RegExp(`\\[${skillName.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")}\\]\\([^)]+\\)\\s*-\\s*(.+)`, "i");
        const descMatch = content.match(lineRe);
        const description = descMatch ? descMatch[1].trim() : "";

        skills.set(skillName, {
          skillPath,
          rawUrl: `https://raw.githubusercontent.com/openclaw/skills/main/skills/${skillPath}`,
          tags: [tag],
          description,
          author,
          clawskillsUrl: `https://clawskills.sh/skills/${author}-${skillName}`,
        });
      }
    }

    // New-format links: [skillname](https://clawskills.sh/skills/<author>-<skillname>)
    for (const match of content.matchAll(LINK_RE_CLAWSKILLS)) {
      const skillName = match[1];
      const authorSkill = match[2]; // e.g. "mfergpt-4claw"
      // Extract author: everything before the last occurrence of -skillName
      const suffixIdx = authorSkill.lastIndexOf(`-${skillName}`);
      const author = suffixIdx > 0 ? authorSkill.slice(0, suffixIdx) : authorSkill.split("-")[0];

      if (skills.has(skillName)) {
        const existing = skills.get(skillName);
        if (!existing.tags.includes(tag)) existing.tags.push(tag);
        // Upgrade source info if we didn't have author before
        if (!existing.author) {
          existing.author = author;
          existing.clawskillsUrl = `https://clawskills.sh/skills/${authorSkill}`;
        }
      } else {
        // Extract description from the markdown line
        const lineRe = new RegExp(`\\[${skillName.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")}\\]\\([^)]+\\)\\s*-\\s*(.+)`, "i");
        const descMatch = content.match(lineRe);
        const description = descMatch ? descMatch[1].trim() : "";

        skills.set(skillName, {
          skillPath: `${author}/${skillName}/SKILL.md`,
          rawUrl: `https://raw.githubusercontent.com/openclaw/skills/main/skills/${author}/${skillName}/SKILL.md`,
          tags: [tag],
          description,
          author,
          clawskillsUrl: `https://clawskills.sh/skills/${authorSkill}`,
        });
      }
    }
  }

  return skills;
}

async function downloadBatch(entries, startIdx) {
  const results = { downloaded: 0, skipped: 0, failed: 0 };

  const promises = entries.map(async ([skillName, info]) => {
    const outDir = path.join(OUTPUT_DIR, skillName);
    const outFile = path.join(outDir, "SKILL.md");

    if (!force && fs.existsSync(outFile)) {
      results.skipped++;
      return;
    }

    try {
      const res = await fetch(info.rawUrl);
      if (res.status !== 200) {
        results.failed++;
        return;
      }

      fs.mkdirSync(outDir, { recursive: true });
      fs.writeFileSync(outFile, res.body);
      results.downloaded++;
    } catch {
      results.failed++;
    }
  });

  await Promise.all(promises);
  return results;
}

async function main() {
  console.log("Discovering skills from catalog...");
  const skills = discoverSkills();
  console.log(`Found ${skills.size} unique skills across categories`);

  fs.mkdirSync(OUTPUT_DIR, { recursive: true });

  const entries = [...skills.entries()];
  let downloaded = 0, skipped = 0, failed = 0;

  for (let i = 0; i < entries.length; i += CONCURRENCY) {
    const batch = entries.slice(i, i + CONCURRENCY);
    const res = await downloadBatch(batch, i);
    downloaded += res.downloaded;
    skipped += res.skipped;
    failed += res.failed;

    const progress = Math.min(i + CONCURRENCY, entries.length);
    process.stdout.write(`\r  [${progress}/${entries.length}] downloaded=${downloaded} skipped=${skipped} failed=${failed}`);

    // Rate limit: small delay between batches
    if (i + CONCURRENCY < entries.length) await sleep(100);
  }

  console.log(`\n\nDone. ${downloaded} downloaded, ${skipped} skipped, ${failed} failed.`);

  // Write a metadata index for quick lookups
  const index = {};
  for (const [name, info] of skills) {
    index[name] = {
      tags: info.tags,
      description: info.description,
      author: info.author || null,
      source: info.clawskillsUrl || null,
    };
  }
  const indexPath = path.join(OUTPUT_DIR, "..", "index.json");
  fs.writeFileSync(indexPath, JSON.stringify(index, null, 2));
  console.log(`Wrote skill index to ${indexPath}`);
}

main().catch((e) => { console.error(e); process.exit(1); });
