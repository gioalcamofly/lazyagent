//! `get_diff` text → files → rows (the web page's lib/diff.js, U5): `git
//! diff` output plus the engine's own entries for untracked files
//! (`diff --git a/f b/f`, `new file`, then the raw contents), binary
//! markers and the truncation notice. Pure; parsed off the UI thread.

use std::collections::BTreeMap;

use serde::{Deserialize, Serialize};

pub const TRUNCATED: &str = "… diff truncated";

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum RowKind {
    Hunk,
    Ctx,
    Add,
    Del,
    Note,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Row {
    pub kind: RowKind,
    pub text: String,
    pub old: Option<u32>,
    pub new: Option<u32>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct File {
    pub path: String,
    pub old_path: String,
    /// modified, added, deleted, renamed, staged
    pub status: &'static str,
    pub rows: Vec<Row>,
    pub added: u32,
    pub removed: u32,
    pub binary: bool,
}

#[derive(Debug, Clone, PartialEq, Eq, Default)]
pub struct Diff {
    pub files: Vec<File>,
    pub truncated: bool,
}

fn new_file(header: &str) -> File {
    // "diff --git a/x b/y": b/ is a rename's new name.
    let rest = header.trim_start_matches("diff --git ");
    let (old_path, path) = match rest.strip_prefix("a/").and_then(|r| r.split_once(" b/")) {
        Some((a, b)) => (a.to_string(), b.to_string()),
        None => (rest.to_string(), rest.to_string()),
    };
    File { path, old_path, status: "modified", rows: vec![], added: 0, removed: 0, binary: false }
}

/// `@@ -a,b +c,d @@ …` → (a, c)
fn hunk_start(line: &str) -> Option<(u32, u32)> {
    let rest = line.strip_prefix("@@ -")?;
    let (old, rest) = rest.split_once(' ')?;
    let new = rest.strip_prefix('+')?.split(' ').next()?;
    let first = |s: &str| s.split(',').next()?.parse::<u32>().ok();
    rest.contains("@@").then_some(())?;
    Some((first(old)?, first(new)?))
}

pub fn parse(text: &str) -> Diff {
    let mut diff = Diff::default();
    let lines: Vec<&str> = text.split('\n').collect();
    let (mut in_hunk, mut raw) = (false, false);
    let (mut old_no, mut new_no) = (0u32, 0u32);
    let mut i = 0;
    while i < lines.len() {
        let line = lines[i];
        i += 1;
        if line.starts_with("diff --git ") {
            diff.files.push(new_file(line));
            (in_hunk, raw) = (false, false);
            continue;
        }
        if line.starts_with(TRUNCATED) {
            diff.truncated = true;
            continue;
        }
        let Some(file) = diff.files.last_mut() else { continue };
        if raw {
            // An untracked file's contents: every line is an addition; a
            // blank line before the next entry is the separator.
            let next = lines.get(i).copied();
            if line.is_empty() && next.is_none_or(|n| n.starts_with("diff --git ") || n.starts_with(TRUNCATED)) {
                continue;
            }
            new_no += 1;
            file.rows.push(Row { kind: RowKind::Add, text: line.to_string(), old: None, new: Some(new_no) });
            file.added += 1;
            continue;
        }
        if let Some((o, n)) = hunk_start(line) {
            in_hunk = true;
            (old_no, new_no) = (o, n);
            file.rows.push(Row { kind: RowKind::Hunk, text: line.to_string(), old: None, new: None });
            continue;
        }
        if !in_hunk {
            if line == "new file" || line.starts_with("new file mode") {
                file.status = "added";
                if line == "new file" {
                    let next = lines.get(i).copied().unwrap_or("");
                    if next == "Binary file" || next.ends_with("too large to show") {
                        file.binary = next == "Binary file";
                        file.rows.push(Row { kind: RowKind::Note, text: next.to_string(), old: None, new: None });
                        i += 1;
                    } else {
                        raw = true;
                        new_no = 0;
                    }
                }
            } else if line.starts_with("deleted file mode") {
                file.status = "deleted";
            } else if line.starts_with("rename from ") {
                file.status = "renamed";
            } else if line.starts_with("Binary files ") || line == "Binary file" {
                file.binary = true;
                file.rows.push(Row { kind: RowKind::Note, text: "Binary file".into(), old: None, new: None });
            } else if line == "staged" {
                file.status = "staged";
            }
            continue;
        }
        let mut chars = line.chars();
        let body = || line.get(1..).unwrap_or("").to_string();
        match chars.next() {
            Some('+') => {
                file.rows.push(Row { kind: RowKind::Add, text: body(), old: None, new: Some(new_no) });
                new_no += 1;
                file.added += 1;
            }
            Some('-') => {
                file.rows.push(Row { kind: RowKind::Del, text: body(), old: Some(old_no), new: None });
                old_no += 1;
                file.removed += 1;
            }
            Some(' ') => {
                file.rows.push(Row { kind: RowKind::Ctx, text: body(), old: Some(old_no), new: Some(new_no) });
                old_no += 1;
                new_no += 1;
            }
            Some('\\') => file.rows.push(Row { kind: RowKind::Note, text: line.to_string(), old: None, new: None }),
            None => in_hunk = false,
            _ => {}
        }
    }
    diff
}

/// One side-by-side row: context on both sides, a run of deletions paired
/// with the run of additions after it; hunk headers and notes span both.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Pair {
    Sides(Option<usize>, Option<usize>),
    Wide(usize),
}

pub fn split_rows(rows: &[Row]) -> Vec<Pair> {
    let mut out = vec![];
    let mut i = 0;
    while i < rows.len() {
        match rows[i].kind {
            RowKind::Del | RowKind::Add => {
                let (mut dels, mut adds) = (vec![], vec![]);
                while i < rows.len() && rows[i].kind == RowKind::Del {
                    dels.push(i);
                    i += 1;
                }
                while i < rows.len() && rows[i].kind == RowKind::Add {
                    adds.push(i);
                    i += 1;
                }
                for k in 0..dels.len().max(adds.len()) {
                    out.push(Pair::Sides(dels.get(k).copied(), adds.get(k).copied()));
                }
            }
            RowKind::Ctx => {
                out.push(Pair::Sides(Some(i), Some(i)));
                i += 1;
            }
            _ => {
                out.push(Pair::Wide(i));
                i += 1;
            }
        }
    }
    out
}

/// A review comment, anchored to a file, a side and a line.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct Comment {
    pub path: String,
    /// "old" (a removed line) or "new"
    pub side: String,
    pub line: u32,
    pub code: String,
    pub text: String,
}

impl Comment {
    pub fn id(&self) -> String {
        format!("{}:{}:{}", self.path, self.side, self.line)
    }
}

/// Where a comment on `row` points (`None` for hunk headers and notes).
pub fn anchor(path: &str, row: &Row) -> Option<Comment> {
    let (side, line) = match row.kind {
        RowKind::Del => ("old", row.old?),
        RowKind::Add | RowKind::Ctx => ("new", row.new?),
        _ => return None,
    };
    Some(Comment { path: path.to_string(), side: side.into(), line, code: row.text.clone(), text: String::new() })
}

/// The prompt that sends a review to an agent (ui-plan §4c step 3).
pub fn review_prompt(comments: &[Comment], summary: &str) -> String {
    let mut by_file: BTreeMap<&str, Vec<&Comment>> = BTreeMap::new();
    let mut order = vec![];
    for c in comments {
        if !by_file.contains_key(c.path.as_str()) {
            order.push(c.path.as_str());
        }
        by_file.entry(&c.path).or_default().push(c);
    }
    let mut parts = vec!["Review comments on your changes. Please address each one:".to_string()];
    if !summary.trim().is_empty() {
        parts.push(String::new());
        parts.push(summary.trim().to_string());
    }
    for path in order {
        parts.push(String::new());
        let mut list = by_file[path].clone();
        list.sort_by_key(|c| c.line);
        for c in list {
            let removed = if c.side == "old" { " (removed line)" } else { "" };
            parts.push(format!("- {path}:{}{removed}", c.line));
            parts.push(format!("  > {}", c.code.trim()));
            parts.push(format!("  {}", c.text.trim().replace('\n', "\n  ")));
        }
    }
    parts.join("\n")
}

#[cfg(test)]
mod tests {
    use super::*;

    const MODIFIED: &str = "diff --git a/src/app.py b/src/app.py\nindex 1..2 100644\n--- a/src/app.py\n+++ b/src/app.py\n@@ -1,3 +1,4 @@\n import os\n-x = 1\n+x = 2\n+y = 3\n print(x)\n\\ No newline at end of file\n";

    #[test]
    fn a_modified_file() {
        let d = parse(MODIFIED);
        assert_eq!(d.files.len(), 1);
        let f = &d.files[0];
        assert_eq!((f.path.as_str(), f.status, f.added, f.removed), ("src/app.py", "modified", 2, 1));
        let kinds: Vec<RowKind> = f.rows.iter().map(|r| r.kind).collect();
        use RowKind::*;
        assert_eq!(kinds, [Hunk, Ctx, Del, Add, Add, Ctx, Note]);
        assert_eq!((f.rows[2].old, f.rows[3].new, f.rows[4].new), (Some(2), Some(2), Some(3)));
        assert_eq!((f.rows[5].old, f.rows[5].new), (Some(3), Some(4)));
    }

    #[test]
    fn renamed_binary_untracked_and_truncated() {
        let text = "diff --git a/old.txt b/new.txt\nsimilarity index 90%\nrename from old.txt\nrename to new.txt\n\
                    diff --git a/img.png b/img.png\nBinary files a/img.png and b/img.png differ\n\
                    diff --git a/notes.md b/notes.md\nnew file\nfirst\n\nthird\n\n\
                    diff --git a/blob.bin b/blob.bin\nnew file\nBinary file\n\
                    … diff truncated at 64 KB";
        let d = parse(text);
        assert!(d.truncated);
        let f: Vec<(&str, &str, &str, bool)> =
            d.files.iter().map(|f| (f.path.as_str(), f.old_path.as_str(), f.status, f.binary)).collect();
        assert_eq!(
            f,
            [
                ("new.txt", "old.txt", "renamed", false),
                ("img.png", "img.png", "modified", true),
                ("notes.md", "notes.md", "added", false),
                ("blob.bin", "blob.bin", "added", true)
            ]
        );
        // The untracked file's raw contents, the empty line inside kept.
        let notes: Vec<&str> = d.files[2].rows.iter().map(|r| r.text.as_str()).collect();
        assert_eq!(notes, ["first", "", "third"]);
        assert_eq!(d.files[2].rows[2].new, Some(3));
    }

    #[test]
    fn split_pairs_runs() {
        let d = parse(MODIFIED);
        let pairs = split_rows(&d.files[0].rows);
        assert_eq!(
            pairs,
            [
                Pair::Wide(0),
                Pair::Sides(Some(1), Some(1)),
                Pair::Sides(Some(2), Some(3)),
                Pair::Sides(None, Some(4)),
                Pair::Sides(Some(5), Some(5)),
                Pair::Wide(6)
            ]
        );
    }

    #[test]
    fn anchors_and_the_review_prompt() {
        let d = parse(MODIFIED);
        let rows = &d.files[0].rows;
        assert_eq!(anchor("src/app.py", &rows[0]), None);
        let mut del = anchor("src/app.py", &rows[2]).unwrap();
        assert_eq!((del.side.as_str(), del.line, del.id()), ("old", 2, "src/app.py:old:2".to_string()));
        del.text = "why change this?".into();
        let mut add = anchor("src/app.py", &rows[4]).unwrap();
        add.text = "name it better\nand test it".into();
        let mut other = anchor("README.md", &rows[1]).unwrap();
        other.path = "README.md".into();
        other.text = "typo".into();
        assert_eq!(
            review_prompt(&[add, del, other], "Looks close."),
            "Review comments on your changes. Please address each one:\n\nLooks close.\n\n\
             - src/app.py:2 (removed line)\n  > x = 1\n  why change this?\n\
             - src/app.py:3\n  > y = 3\n  name it better\n  and test it\n\n\
             - README.md:1\n  > import os\n  typo"
        );
    }
}
