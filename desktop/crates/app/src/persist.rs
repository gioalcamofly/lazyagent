//! What the app keeps between runs, per repo (the web page keeps it in
//! localStorage): attention marks (for engines without an inbox; the
//! engine keeps them otherwise, imported once), notification settings, the
//! theme, Home's last look, and review comments. `$XDG_STATE_HOME/lazyagent/desktop/<hash>.json`,
//! written atomically (temp file + rename) off the UI thread, at most once
//! a second.

use std::collections::BTreeMap;
use std::path::PathBuf;
use std::time::{Duration, Instant};

use lazyagent_proto::discovery::{self, Env};
use serde::{Deserialize, Serialize};

use lazyagent_proto::types::Actionable;

use crate::attention::Marks;

const DEBOUNCE: Duration = Duration::from_secs(1);

#[derive(Debug, Clone, Default, PartialEq, Serialize, Deserialize)]
pub struct Saved {
    #[serde(default)]
    pub marks: Marks,
    /// Notification kind → on (attention::SETTINGS); missing = on.
    #[serde(default)]
    pub notify: BTreeMap<String, bool>,
    #[serde(default)]
    pub theme: Option<String>,
    /// The diff viewer's layout.
    #[serde(default)]
    pub diff_split: bool,
    /// The diff viewer leaves out the uncommitted changes.
    #[serde(default)]
    pub diff_hide_uncommitted: bool,
    /// Help → Check automatically, turned off.
    #[serde(default)]
    pub update_check_off: bool,
    /// Review comments per worktree (D5): opaque to this module.
    #[serde(default)]
    pub comments: BTreeMap<String, serde_json::Value>,
    /// The marks above went to the engine's inbox (once).
    #[serde(default)]
    pub marks_imported: bool,
    /// When Home was last looked at (Unix seconds): later changes get a dot.
    #[serde(default)]
    pub home_seen: Option<f64>,
    /// Home's in-flight list folded.
    #[serde(default)]
    pub home_fold: bool,
}

impl Saved {
    pub fn notify_on(&self, setting: &str) -> bool {
        self.notify.get(setting).copied().unwrap_or(true)
    }

    /// The `actionables_mark` calls that carry this app's own marks over to
    /// the engine's items: `(id, action, until)`. A dismissal holds for an
    /// item the engine has seen since before it (same condition: the ids
    /// are the same, the first-seen times are not); a snooze becomes a
    /// park until its time.
    pub fn marks_to_import(&self, items: &[&Actionable], now: f64) -> Vec<(String, &'static str, Option<f64>)> {
        let mut out = vec![];
        for item in items {
            let dismissed = self.marks.dismissed.iter().any(|(occurrence, at)| {
                occurrence.rsplit_once('@').is_some_and(|(id, _)| id == item.id) && item.since <= *at
            });
            if dismissed {
                out.push((item.id.clone(), "dismiss", None));
            } else if let Some(until) = self.marks.snoozed.get(&item.id).filter(|u| **u > now) {
                out.push((item.id.clone(), "park", Some(*until)));
            }
        }
        out
    }
}

pub fn path(root: &str, env: &Env) -> PathBuf {
    let base = env
        .get("XDG_STATE_HOME")
        .map(PathBuf::from)
        .unwrap_or_else(|| PathBuf::from(env.get("HOME").unwrap_or_default()).join(".local/state"));
    base.join("lazyagent").join("desktop").join(format!("{}.json", discovery::repo_digest(root)))
}

pub fn load(path: &std::path::Path) -> Saved {
    std::fs::read(path).ok().and_then(|b| serde_json::from_slice(&b).ok()).unwrap_or_default()
}

/// Writes `saved` a second after the last change, from a thread.
pub struct Saver {
    path: PathBuf,
    dirty_since: Option<Instant>,
}

impl Saver {
    pub fn new(path: PathBuf) -> Saver {
        Saver { path, dirty_since: None }
    }

    pub fn touch(&mut self) {
        self.dirty_since.get_or_insert_with(Instant::now);
    }

    /// Call each frame; returns how long until it wants to write, if dirty.
    pub fn tick(&mut self, saved: &Saved) -> Option<Duration> {
        let since = self.dirty_since?;
        let waited = since.elapsed();
        if waited < DEBOUNCE {
            return Some(DEBOUNCE - waited);
        }
        self.dirty_since = None;
        self.write(saved);
        None
    }

    /// Writes now (on exit).
    pub fn flush(&mut self, saved: &Saved) {
        if self.dirty_since.take().is_some() {
            let bytes = serde_json::to_vec_pretty(saved).unwrap_or_default();
            let _ = write_atomic(&self.path, &bytes);
        }
    }

    fn write(&self, saved: &Saved) {
        let bytes = serde_json::to_vec_pretty(saved).unwrap_or_default();
        let path = self.path.clone();
        std::thread::spawn(move || write_atomic(&path, &bytes));
    }
}

fn write_atomic(path: &std::path::Path, bytes: &[u8]) -> std::io::Result<()> {
    use std::os::unix::fs::PermissionsExt;
    let dir = path.parent().unwrap();
    std::fs::create_dir_all(dir)?;
    let tmp = path.with_extension(format!("tmp{}", std::process::id()));
    std::fs::write(&tmp, bytes)?;
    std::fs::set_permissions(&tmp, std::fs::Permissions::from_mode(0o600))?;
    std::fs::rename(&tmp, path)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn round_trip_and_debounce() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("x/y.json");
        assert_eq!(load(&path), Saved::default(), "missing file: defaults");
        let mut saved = Saved::default();
        saved.marks.dismissed.insert("a@1".into(), 2.0);
        saved.notify.insert("done".into(), false);
        let mut saver = Saver::new(path.clone());
        assert_eq!(saver.tick(&saved), None, "nothing to write");
        saver.touch();
        assert!(saver.tick(&saved).is_some(), "waits a second");
        saver.flush(&saved);
        let back = load(&path);
        assert_eq!(back, saved);
        assert!(!back.notify_on("done") && back.notify_on("approval"));
        std::fs::write(&path, "{not json").unwrap();
        assert_eq!(load(&path), Saved::default(), "a corrupt file is ignored");
    }

    #[test]
    fn marks_import_into_the_engines_items() {
        let item = |id: &str, since: f64| -> Actionable {
            serde_json::from_value(serde_json::json!({
                "id": id, "occurrence": format!("{id}@{since}"), "kind": "done", "level": "round", "title": "Your turn",
                "label": "", "detail": "", "state": "", "work_item": null, "worktree_path": null, "agent_id": null,
                "pr": null, "job_id": null, "exit_code": null, "blocks_agent": false, "priority": "medium",
                "since": since, "park": null
            }))
            .unwrap()
        };
        let mut saved = Saved::default();
        // Dismissed here at 50 (first seen at 40 by the app).
        saved.marks.dismissed.insert("Done:/w/a1@40".into(), 50.0);
        saved.marks.dismissed.insert("Done:/w/a2@40".into(), 50.0);
        saved.marks.snoozed.insert("Ci:/w:7".into(), 500.0);
        saved.marks.snoozed.insert("Ci:/w:8".into(), 90.0);
        let (a1, a2, ci7, ci8) =
            (item("Done:/w/a1", 30.0), item("Done:/w/a2", 60.0), item("Ci:/w:7", 1.0), item("Ci:/w:8", 1.0));
        let got = saved.marks_to_import(&[&a1, &a2, &ci7, &ci8], 100.0);
        assert_eq!(
            got,
            [("Done:/w/a1".to_string(), "dismiss", None), ("Ci:/w:7".to_string(), "park", Some(500.0))],
            "a2 came back after the dismissal; the snooze of #8 is over"
        );
    }
}
