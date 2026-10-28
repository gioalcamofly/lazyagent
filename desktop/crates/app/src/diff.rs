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
    /// From the uncommitted part (staged, unstaged, untracked), not what
    /// the pull request shows.
    pub uncommitted: bool,
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
    File { path, old_path, status: "modified", rows: vec![], added: 0, removed: 0, binary: false, uncommitted: false }
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

/// `get_diff`'s committed part, then its uncommitted one (`pr`), as one
/// list of files: the uncommitted ones last, and marked.
pub fn parse_pr(committed: &str, uncommitted: &str) -> Diff {
    let mut d = parse(committed);
    let u = parse(uncommitted);
    d.truncated |= u.truncated;
    d.files.extend(u.files.into_iter().map(|f| File { uncommitted: true, ..f }));
    d
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

/// A line's code as it is drawn: tabs as four spaces.
pub fn expand_tabs(text: &str) -> String {
    text.replace('\t', "    ")
}

/// Where a line soft-wraps to fit `cols` columns, like GitHub: the char
/// ranges of its pieces (at least one), each broken after its last space
/// when that space is in the piece's second half, else at the column.
pub fn wrap_line(text: &str, cols: usize) -> Vec<std::ops::Range<usize>> {
    let cols = cols.max(1);
    let chars: Vec<char> = text.chars().collect();
    let mut out = vec![];
    let mut start = 0;
    while chars.len() - start > cols {
        let hard = start + cols;
        let end = (start + cols / 2..hard).rev().find(|&i| chars[i] == ' ').map_or(hard, |i| i + 1);
        out.push(start..end);
        start = end;
    }
    out.push(start..chars.len());
    out
}

/// How many screen rows a line takes at `cols` columns.
pub fn wrap_count(text: &str, cols: usize) -> usize {
    let n = text.chars().map(|c| if c == '\t' { 4 } else { 1 }).sum::<usize>();
    if n <= cols.max(1) { 1 } else { wrap_line(&expand_tabs(text), cols).len() }
}

/// A review comment, anchored to a file, a side and a line (its last
/// line, for a range), with the thread that follows when it was asked.
#[derive(Debug, Clone, PartialEq, Eq, Default, Serialize, Deserialize)]
pub struct Comment {
    pub path: String,
    /// "old" (a removed line) or "new"
    pub side: String,
    pub line: u32,
    /// The selected lines; a range's are prefixed `+`, `-` or ` `.
    pub code: String,
    pub text: String,
    /// A range's first line (absent: one line).
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub start_line: Option<u32>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub start_side: Option<String>,
    /// Sent to the agent as a question, not kept for the review.
    #[serde(default, skip_serializing_if = "std::ops::Not::not")]
    pub asked: bool,
    /// Sent with a review: a thread now, no longer part of the next one.
    #[serde(default, skip_serializing_if = "std::ops::Not::not")]
    pub sent: bool,
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub replies: Vec<Reply>,
    /// On the uncommitted part: its lines are the working tree's, and the
    /// same file can be in both parts.
    #[serde(default, skip_serializing_if = "std::ops::Not::not")]
    pub uncommitted: bool,
    /// Its id for the agent, which answers it with `review_reply` (given
    /// when it is first sent).
    #[serde(default, skip_serializing_if = "String::is_empty")]
    pub cid: String,
}

#[derive(Debug, Clone, PartialEq, Eq, Default, Serialize, Deserialize)]
pub struct Reply {
    /// "you", "agent" or "note"
    pub who: String,
    pub text: String,
    /// An answer through `review_reply`: the engine's reply id.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub id: Option<String>,
}

impl Reply {
    pub fn new(who: &str, text: impl Into<String>) -> Reply {
        Reply { who: who.into(), text: text.into(), id: None }
    }
}

impl Comment {
    pub fn id(&self) -> String {
        match (&self.start_side, self.start_line) {
            (Some(ss), Some(sl)) => format!("{}:{ss}:{sl}-{}:{}{}", self.path, self.side, self.line, self.part()),
            _ => self.anchor_id(),
        }
    }

    /// The line it shows under.
    pub fn anchor_id(&self) -> String {
        format!("{}:{}:{}{}", self.path, self.side, self.line, self.part())
    }

    /// Asked, or sent with a review: a conversation, not a draft.
    pub fn out(&self) -> bool {
        self.asked || self.sent
    }

    fn part(&self) -> &'static str {
        if self.uncommitted { ":u" } else { "" }
    }

    /// Its place as the agent reads it: `path:12`, and which part.
    fn located(&self) -> String {
        format!("{}:{}{}", self.path, self.place(), if self.uncommitted { " (uncommitted)" } else { "" })
    }

    /// `12`, `12 (removed line)`, `12-15`, or `-3…+5` across sides.
    pub fn place(&self) -> String {
        let old = self.side == "old";
        match (&self.start_side, self.start_line) {
            (Some(ss), Some(sl)) if *ss == self.side => {
                format!("{sl}-{}{}", self.line, if old { " (removed lines)" } else { "" })
            }
            (Some(ss), Some(sl)) => {
                let mark = |side: &str| if side == "old" { '-' } else { '+' };
                format!("{}{sl}…{}{}", mark(ss), mark(&self.side), self.line)
            }
            _ => format!("{}{}", self.line, if old { " (removed line)" } else { "" }),
        }
    }

    /// The code as quoted to the agent.
    fn quoted(&self) -> String {
        if self.start_line.is_none() {
            return format!("  > {}", self.code.trim());
        }
        self.code.lines().map(|l| format!("  > {}", l.trim_end())).collect::<Vec<_>>().join("\n")
    }
}

/// Where a comment on `row` of `file` points, in its part.
pub fn anchor_in(file: &File, row: &Row) -> Option<Comment> {
    anchor(&file.path, row).map(|c| Comment { uncommitted: file.uncommitted, ..c })
}

/// Where a comment on `row` points (`None` for hunk headers and notes).
pub fn anchor(path: &str, row: &Row) -> Option<Comment> {
    let (side, line) = match row.kind {
        RowKind::Del => ("old", row.old?),
        RowKind::Add | RowKind::Ctx => ("new", row.new?),
        _ => return None,
    };
    Some(Comment { path: path.to_string(), side: side.into(), line, code: row.text.clone(), ..Default::default() })
}

/// A comment on the selected rows (hunk headers and notes skipped): one
/// line is a plain anchor, more are a range ending at the last one.
pub fn anchor_range<'a>(path: &str, rows: impl IntoIterator<Item = &'a Row>) -> Option<Comment> {
    let rows: Vec<&Row> = rows.into_iter().filter(|r| anchor(path, r).is_some()).collect();
    let (first, last) = (rows.first()?, rows.last()?);
    let mut c = anchor(path, last)?;
    if rows.len() > 1 {
        let start = anchor(path, first)?;
        c.start_line = Some(start.line);
        c.start_side = Some(start.side);
        c.code = rows
            .iter()
            .map(|r| {
                let mark = match r.kind {
                    RowKind::Add => '+',
                    RowKind::Del => '-',
                    _ => ' ',
                };
                format!("{mark}{}", r.text)
            })
            .collect::<Vec<_>>()
            .join("\n");
    }
    Some(c)
}

/// How an agent is asked to answer a comment with `review_reply`.
const REPLY_HOW: &str = "the review_reply tool of the lazyagent_app MCP server";

/// `path:12 [comment_id: c1a2b3]`: where, and the id it is answered by.
fn tagged(c: &Comment) -> String {
    if c.cid.is_empty() { c.located() } else { format!("{} [comment_id: {}]", c.located(), c.cid) }
}

/// The prompt that sends a review to an agent (ui-plan §4c step 3). With
/// ids (`Comment::cid`) it asks for an answer to each with `review_reply`.
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
        list.sort_by_key(|c| c.start_line.unwrap_or(c.line));
        for c in list {
            parts.push(format!("- {}", tagged(c)));
            parts.push(c.quoted());
            parts.push(format!("  {}", c.text.trim().replace('\n', "\n  ")));
        }
    }
    if comments.iter().any(|c| !c.cid.is_empty()) {
        parts.push(String::new());
        parts.push(format!(
            "For each comment, once you have addressed it, call {REPLY_HOW} with its comment_id and a short answer (what you changed, or why not), so the diff view shows it next to the code; say it here in the conversation too."
        ));
    }
    parts.join("\n")
}

/// The markers the agent is asked to put around its answer.
pub fn answer_markers(id: &str) -> (String, String) {
    (format!("⟦answer {id}⟧"), format!("⟦end {id}⟧"))
}

/// The prompt that asks the agent about a comment now: where, the code,
/// the thread so far, and how the answer reaches the diff view: with
/// `review_reply` when the comment has an id, else between markers
/// (`marker_id`) that are read back from the agent's output.
pub fn ask_prompt(c: &Comment, base: &str, marker_id: &str) -> String {
    let against = if c.uncommitted || base.trim().is_empty() {
        String::new()
    } else {
        format!(" (diff against {})", base.trim())
    };
    let mut parts = vec![format!("A question about {}{against}:", tagged(c)), c.quoted()];
    // The question is the comment, or the last of the follow-ups.
    let asking = match c.replies.iter().rposition(|r| r.who == "you") {
        Some(i) => {
            parts.push(String::new());
            parts.push("So far:".into());
            parts.push(format!("- me: {}", c.text.trim()));
            for r in c.replies[..i].iter().filter(|r| r.who != "note") {
                let who = if r.who == "agent" { "you" } else { "me" };
                parts.push(format!("- {who}: {}", r.text.trim()));
            }
            c.replies[i].text.trim()
        }
        None => c.text.trim(),
    };
    parts.push(String::new());
    parts.push(asking.to_string());
    parts.push(String::new());
    if c.cid.is_empty() {
        let (start, end) = answer_markers(marker_id);
        parts.push(format!(
            "Answer it (change files only if it asks you to). Start your answer with the line {start} and end it with the line {end}, so the diff view can show it next to the code."
        ));
    } else {
        parts.push(format!(
            "Answer it here (change files only if it asks you to), and also call {REPLY_HOW} with comment_id \"{}\" and your answer, so the diff view shows it next to the code.",
            c.cid
        ));
    }
    parts.join("\n")
}

/// The answer between the markers in an agent's output (the last one),
/// without the CLI's bullet and the indentation it adds; `None` without
/// both markers on lines of their own.
pub fn parse_answer(lines: &[String], marker_id: &str) -> Option<String> {
    let (start, end) = answer_markers(marker_id);
    let bare = |l: &str| l.trim().trim_start_matches('⏺').trim().to_string();
    let from = lines.iter().rposition(|l| bare(l) == start)?;
    let to = from + 1 + lines[from + 1..].iter().position(|l| bare(l) == end)?;
    let body: Vec<String> =
        lines[from + 1..to].iter().map(|l| l.trim_end().trim_start_matches('⏺').trim_end().to_string()).collect();
    let indent = body.iter().filter(|l| !l.trim().is_empty()).map(|l| l.len() - l.trim_start().len()).min()?;
    let text = body.iter().map(|l| l.get(indent..).unwrap_or("")).collect::<Vec<_>>().join("\n");
    let text = text.trim_matches('\n').to_string();
    (!text.trim().is_empty()).then_some(text)
}

#[cfg(test)]
mod tests {
    use super::*;

    const MODIFIED: &str = "diff --git a/src/app.py b/src/app.py\nindex 1..2 100644\n--- a/src/app.py\n+++ b/src/app.py\n@@ -1,3 +1,4 @@\n import os\n-x = 1\n+x = 2\n+y = 3\n print(x)\n\\ No newline at end of file\n";

    #[test]
    fn long_lines_wrap_after_a_space_or_at_the_column() {
        let pieces = |text: &str, cols| -> Vec<String> {
            let chars: Vec<char> = text.chars().collect();
            wrap_line(text, cols).into_iter().map(|r| chars[r].iter().collect()).collect()
        };
        assert_eq!(pieces("short", 10), ["short"]);
        assert_eq!(pieces("", 10), [""]);
        // After the last space in the piece's second half, like GitHub.
        assert_eq!(pieces("all the criteria were met", 10), ["all the ", "criteria ", "were met"]);
        // No space late enough: at the column.
        assert_eq!(pieces("abcdefghijklmnopqrstuvwxyz", 10), ["abcdefghij", "klmnopqrst", "uvwxyz"]);
        // Every char once, in order, never a piece over the width.
        let text = "    \"\"\"Returns the rows that match all criteria within one pass of the list.\"\"\"";
        let got = pieces(text, 17);
        assert_eq!(got.concat(), text);
        assert!(got.iter().all(|p| p.chars().count() <= 17), "{got:?}");
        // Tabs count as four columns.
        assert_eq!(wrap_count("\tabcd", 8), 1);
        assert_eq!(wrap_count("\tabcde", 8), 2);
        assert_eq!(wrap_count("x", 0), 1);
    }

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
    fn ranges_in_the_review_prompt() {
        let d = parse(MODIFIED);
        let rows = &d.files[0].rows;
        // The hunk header is skipped; three lines across both sides.
        let mut mixed = anchor_range("src/app.py", &rows[0..4]).unwrap();
        assert_eq!((mixed.start_line, mixed.start_side.as_deref(), mixed.line), (Some(1), Some("new"), 2));
        assert_eq!(mixed.code, " import os\n-x = 1\n+x = 2");
        assert_eq!(mixed.id(), "src/app.py:new:1-new:2");
        assert_eq!(mixed.anchor_id(), "src/app.py:new:2");
        mixed.text = "why?".into();
        let mut old = anchor_range("src/app.py", [&rows[1], &rows[2]]).unwrap();
        old.start_side = Some("old".into());
        old.side = "old".into();
        old.text = "keep".into();
        assert_eq!(old.place(), "1-2 (removed lines)");
        let one = anchor_range("src/app.py", [&rows[0], &rows[4]]).unwrap();
        assert_eq!((one.start_line, one.line, one.code.as_str()), (None, 3, "y = 3"), "one line is a plain anchor");
        assert!(anchor_range("src/app.py", [&rows[0]]).is_none());
        assert_eq!(
            review_prompt(&[mixed, old], ""),
            "Review comments on your changes. Please address each one:\n\n\
             - src/app.py:1-2\n  >  import os\n  > -x = 1\n  > +x = 2\n  why?\n\
             - src/app.py:1-2 (removed lines)\n  >  import os\n  > -x = 1\n  keep"
        );
        let mut across = anchor_range("src/app.py", &rows[2..4]).unwrap();
        across.text = "x".into();
        assert_eq!(across.place(), "-2…+2");
    }

    #[test]
    fn the_ask_prompt_and_its_answer() {
        let d = parse(MODIFIED);
        let mut c = anchor_range("src/app.py", &d.files[0].rows[2..4]).unwrap();
        c.text = "Why 2?".into();
        let p = ask_prompt(&c, "main", "k3");
        assert!(
            p.starts_with("A question about src/app.py:-2…+2 (diff against main):\n  > -x = 1\n  > +x = 2\n\nWhy 2?\n")
        );
        assert!(p.contains("Start your answer with the line ⟦answer k3⟧ and end it with the line ⟦end k3⟧"));
        // A follow-up carries the thread.
        c.replies = vec![Reply::new("agent", "The spec says so."), Reply::new("you", "Which spec?")];
        let p = ask_prompt(&c, "", "k4");
        assert!(p.contains("about src/app.py:-2…+2:\n"));
        assert!(p.contains("So far:\n- me: Why 2?\n- you: The spec says so.\n\nWhich spec?\n"), "{p}");
        // The answer as a CLI prints it: the prompt echoed (markers inside
        // a sentence), a bullet, an indent, the answer wrapped.
        let out: Vec<String> = [
            "> A question about src/app.py … with the line ⟦answer k4⟧ and end it with the line ⟦end k4⟧, so",
            "",
            "⏺ ⟦answer k4⟧",
            "  The ticket's spec, section 2:",
            "    x must be even.",
            "",
            "  ⟦end k4⟧",
            "",
            "> ",
        ]
        .iter()
        .map(|s| s.to_string())
        .collect();
        assert_eq!(parse_answer(&out, "k4").as_deref(), Some("The ticket's spec, section 2:\n  x must be even."));
        assert_eq!(parse_answer(&out, "k3"), None, "another question's markers");
        assert_eq!(parse_answer(&out[..5], "k4"), None, "no end yet");
    }

    #[test]
    fn with_ids_the_prompts_ask_for_review_reply() {
        let d = parse(MODIFIED);
        let rows = &d.files[0].rows;
        let mut a = anchor("src/app.py", &rows[4]).unwrap();
        a.text = "name it better".into();
        a.cid = "c1a2b3".into();
        let mut b = anchor("src/app.py", &rows[2]).unwrap();
        b.text = "why?".into();
        b.cid = "c4d5e6".into();
        let p = review_prompt(&[a.clone(), b], "");
        assert!(p.contains("- src/app.py:2 (removed line) [comment_id: c4d5e6]\n  > x = 1\n  why?\n"), "{p}");
        assert!(p.contains("- src/app.py:3 [comment_id: c1a2b3]\n"), "{p}");
        assert!(p.ends_with(
            "call the review_reply tool of the lazyagent_app MCP server with its comment_id and a short answer (what you changed, or why not), so the diff view shows it next to the code; say it here in the conversation too."
        ), "{p}");
        let q = ask_prompt(&a, "main", "k9");
        assert!(q.starts_with("A question about src/app.py:3 [comment_id: c1a2b3] (diff against main):\n"), "{q}");
        assert!(q.contains("with comment_id \"c1a2b3\" and your answer"), "{q}");
        assert!(!q.contains('⟦'), "no markers: {q}");
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

    #[test]
    fn the_uncommitted_part_is_marked_and_anchored_apart() {
        let d = parse_pr(MODIFIED, MODIFIED);
        assert_eq!(d.files.len(), 2);
        assert!(!d.files[0].uncommitted && d.files[1].uncommitted);
        let committed = anchor_in(&d.files[0], &d.files[0].rows[2]).unwrap();
        let mut open = anchor_in(&d.files[1], &d.files[1].rows[2]).unwrap();
        assert_ne!(committed.id(), open.id(), "the same line in both parts is two anchors");
        assert_eq!(committed.anchor_id(), "src/app.py:old:2");
        assert_eq!(open.anchor_id(), "src/app.py:old:2:u");
        open.text = "why?".into();
        assert!(review_prompt(std::slice::from_ref(&open), "").contains("- src/app.py:2 (removed line) (uncommitted)"));
        assert!(
            ask_prompt(&open, "origin/main", "k")
                .starts_with("A question about src/app.py:2 (removed line) (uncommitted):")
        );
        // Saved before the parts: still the committed anchor.
        let old: Comment = serde_json::from_value(serde_json::json!({
            "path": "src/app.py", "side": "old", "line": 2, "code": "x = 1", "text": "t"
        }))
        .unwrap();
        assert_eq!(old.id(), committed.id());
        assert!(!serde_json::to_string(&old).unwrap().contains("uncommitted"));
    }

    #[test]
    fn a_truncated_uncommitted_part_marks_the_diff() {
        let d = parse_pr("", &format!("{MODIFIED}\n{TRUNCATED} (too large to display)"));
        assert!(d.truncated && d.files[0].uncommitted);
    }
}
