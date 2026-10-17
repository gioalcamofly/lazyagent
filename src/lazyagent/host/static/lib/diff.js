// Unified diff → files → rows, for the diff viewer (ui-plan §3d, U5).
//
// Input is get_diff's text: `git diff` output, plus the engine's own entries
// for untracked files ("diff --git a/f b/f\nnew file\n<contents>"), binary
// markers and a truncation notice. Output is a flat model the viewer can
// virtualise: every file is a header row followed by fixed-height rows.

const HUNK = /^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@(.*)$/;
export const TRUNCATED = "… diff truncated";

/** @typedef {{kind: "hunk"|"ctx"|"add"|"del"|"note", text: string, old?: number, new?: number}} Row */
/** @typedef {{path: string, oldPath: string, status: string, rows: Row[], added: number, removed: number, binary: boolean}} DiffFile */

function newFile(header) {
  // "diff --git a/x b/y": take b/ (a rename's new name).
  const m = /^diff --git a\/(.*?) b\/(.*)$/.exec(header);
  const oldPath = m ? m[1] : header;
  const path = m ? m[2] : header;
  return { path, oldPath, status: "modified", rows: [], added: 0, removed: 0, binary: false };
}

/** Parse get_diff's text. @returns {{files: DiffFile[], truncated: boolean}} */
export function parseDiff(text) {
  const files = [];
  let truncated = false;
  let file = null;
  let inHunk = false;
  // The engine's untracked-file entries carry raw contents, not a patch.
  let raw = false;
  let oldNo = 0;
  let newNo = 0;
  const lines = text.split("\n");
  for (let i = 0; i < lines.length; i++) {
    const line = lines[i];
    if (line.startsWith("diff --git ")) {
      file = newFile(line);
      files.push(file);
      inHunk = false;
      raw = false;
      continue;
    }
    if (line.startsWith(TRUNCATED)) {
      truncated = true;
      continue;
    }
    if (!file) continue;
    if (raw) {
      // Contents of an untracked file: every line is an addition. A blank
      // line followed by the next "diff --git" is the entries' separator.
      if (line === "" && (i + 1 >= lines.length || lines[i + 1].startsWith("diff --git ") || lines[i + 1].startsWith(TRUNCATED))) continue;
      file.rows.push({ kind: "add", text: line, new: ++newNo });
      file.added++;
      continue;
    }
    const hunk = HUNK.exec(line);
    if (hunk) {
      inHunk = true;
      oldNo = Number(hunk[1]);
      newNo = Number(hunk[3]);
      file.rows.push({ kind: "hunk", text: line });
      continue;
    }
    if (!inHunk) {
      // Header lines.
      if (line === "new file" || line.startsWith("new file mode")) {
        file.status = "added";
        if (line === "new file") {
          // The engine's untracked entry: contents follow, unless a marker.
          const next = lines[i + 1] || "";
          if (next === "Binary file" || /too large to show$/.test(next)) {
            file.binary = next === "Binary file";
            file.rows.push({ kind: "note", text: next === "Binary file" ? "Binary file" : next });
            i++;
          } else {
            raw = true;
            newNo = 0;
          }
        }
      } else if (line.startsWith("deleted file mode")) file.status = "deleted";
      else if (line.startsWith("rename from ")) file.status = "renamed";
      else if (line.startsWith("Binary files ") || line === "Binary file") {
        file.binary = true;
        file.rows.push({ kind: "note", text: "Binary file" });
      } else if (line === "staged") file.status = "staged";
      continue;
    }
    const tag = line[0];
    const body = line.slice(1);
    if (tag === "+") {
      file.rows.push({ kind: "add", text: body, new: newNo++ });
      file.added++;
    } else if (tag === "-") {
      file.rows.push({ kind: "del", text: body, old: oldNo++ });
      file.removed++;
    } else if (tag === " ") {
      file.rows.push({ kind: "ctx", text: body, old: oldNo++, new: newNo++ });
    } else if (tag === "\\") {
      file.rows.push({ kind: "note", text: line });
    } else if (line === "") {
      // A trailing empty line between entries.
      inHunk = false;
    }
  }
  return { files, truncated };
}

/**
 * Side-by-side rows: context on both sides, a run of deletions paired with
 * the run of additions after it.
 * @returns {{left: Row|null, right: Row|null, kind?: string}[]}
 */
export function splitRows(rows) {
  const out = [];
  for (let i = 0; i < rows.length; ) {
    const row = rows[i];
    if (row.kind === "del" || row.kind === "add") {
      const dels = [];
      const adds = [];
      while (i < rows.length && rows[i].kind === "del") dels.push(rows[i++]);
      while (i < rows.length && rows[i].kind === "add") adds.push(rows[i++]);
      for (let k = 0; k < Math.max(dels.length, adds.length); k++) {
        out.push({ left: dels[k] || null, right: adds[k] || null });
      }
      continue;
    }
    if (row.kind === "ctx") out.push({ left: row, right: row });
    else out.push({ left: null, right: null, kind: row.kind, text: row.text });
    i++;
  }
  return out;
}

/** Where a comment points: the file, the side and the line number. */
export function anchorOf(path, row) {
  const side = row.kind === "del" ? "old" : "new";
  const line = side === "old" ? row.old : row.new;
  return { path, side, line, code: row.text, id: `${path}:${side}:${line}` };
}

/** The prompt that sends a review's comments to an agent (§4c step 3). */
export function reviewPrompt(comments, { summary = "" } = {}) {
  const byFile = new Map();
  for (const c of comments) {
    if (!byFile.has(c.path)) byFile.set(c.path, []);
    byFile.get(c.path).push(c);
  }
  const parts = ["Review comments on your changes. Please address each one:"];
  if (summary.trim()) parts.push("", summary.trim());
  for (const [path, list] of byFile) {
    parts.push("");
    for (const c of list.sort((a, b) => a.line - b.line)) {
      const where = `${path}:${c.line}${c.side === "old" ? " (removed line)" : ""}`;
      parts.push(`- ${where}`, `  > ${c.code.trim()}`, `  ${c.text.trim().replace(/\n/g, "\n  ")}`);
    }
  }
  return parts.join("\n");
}
