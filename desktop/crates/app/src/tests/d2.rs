//! The Diff view wraps long lines (on by default, the Wrap toggle), hides
//! its file list behind a rail, and the Worktrees sidebar collapses to a
//! rail on every screen; all three are kept in the app's state file.

use super::*;
use crate::diff_view::DRow;

/// One file: a context line, a removed short line and an added one far
/// wider than any pane, then a context line.
fn long_diff() -> crate::diff::Diff {
    let long = "x".repeat(40) + &" all the criteria within one pass".repeat(20);
    crate::diff::parse(&format!(
        "diff --git a/src/a.py b/src/a.py\n--- a/src/a.py\n+++ b/src/a.py\n@@ -1,3 +1,3 @@\n ctx\n-short\n+{long}\n end\n"
    ))
}

fn open_diff(h: &mut Harness<'static, App>, diff: crate::diff::Diff) {
    let s = &mut h.state_mut().state;
    s.selected = Some("/r/repo-f".into());
    s.diff = Some(crate::diff_view::DiffState::loaded("/r/repo-f", "main", diff));
    s.diff_open = true;
    h.run_steps(3);
}

fn rows(h: &Harness<'static, App>) -> Vec<DRow> {
    h.state().state.diff.as_ref().unwrap().rows.clone()
}

fn diff_app() -> Harness<'static, App> {
    let (app, _tx) = app(vec![
        worktree("/r/repo", "main", vec![]),
        worktree("/r/repo-f", "f", vec![agent("a1", "Implementer", "running", "s1")]),
    ]);
    let mut h = harness(app);
    h.run_steps(2);
    h
}

#[test]
fn long_lines_wrap_by_default_and_wrap_is_a_remembered_toggle() {
    let mut h = diff_app();
    open_diff(&mut h, long_diff());
    let wrapped = rows(&h);
    let more = wrapped.iter().filter(|r| matches!(r, DRow::More(0, 3, _))).count();
    assert!(more >= 3, "the long line takes several rows: {wrapped:?}");
    // Nothing is left past the pane's edge: no sideways scrolling.
    assert_eq!(h.state().state.diff.as_ref().unwrap().hoff, 0.0);
    // Split: the pair is as tall as its long side, the short side blank.
    h.get_by_label("Split").click();
    h.run_steps(3);
    let split = rows(&h);
    let pair = crate::diff::Pair::Sides(Some(2), Some(3));
    let tall = split.iter().filter(|r| matches!(r, DRow::PairMore(0, p, _) if *p == pair)).count();
    assert!(tall > more, "a split pane is narrower, so more rows: {tall} vs {more}");
    // Off: one row a line, the sideways bar back; the choice is saved.
    h.get_by_label("Wrap").click();
    h.run_steps(3);
    assert!(h.state().state.saved.diff_nowrap);
    assert!(!rows(&h).iter().any(|r| matches!(r, DRow::More(..) | DRow::PairMore(..))));
    assert_eq!(rows(&h).len(), 5, "file, hunk, ctx, the pair, end");
    // Alt+Z, outside a text field, turns it back on.
    h.key_press_modifiers(egui::Modifiers::ALT, egui::Key::Z);
    h.run_steps(3);
    assert!(!h.state().state.saved.diff_nowrap);
    assert!(rows(&h).iter().any(|r| matches!(r, DRow::PairMore(..))));
}

#[test]
fn a_comment_on_a_wrapped_line_anchors_to_that_line() {
    let mut h = diff_app();
    open_diff(&mut h, long_diff());
    // Rows: 0 file, 1 hunk, 2 ctx, 3 -short, 4 +long, 5.. its pieces.
    let rows_now = rows(&h);
    assert_eq!(rows_now[4], DRow::Row(0, 3));
    assert_eq!(rows_now[6], DRow::More(0, 3, 2));
    // A click on a continuation row's number column: the long line's popup.
    let none = egui::Modifiers::NONE;
    press(&mut h, 6, 20.0, true, none);
    press(&mut h, 6, 20.0, false, none);
    let draft = h.state().state.ask.as_ref().expect("the popup").comment.clone();
    assert_eq!((draft.side.as_str(), draft.line, draft.start_line), ("new", 2, None));
    assert!(draft.code.contains("all the criteria"), "the whole line, not a piece");
    h.event(egui::Event::Text("Split this line".into()));
    h.run_steps(2);
    h.get_by_label("Add to review").click();
    h.run_steps(3);
    let all = h.state().state.comments("/r/repo-f");
    assert_eq!(all.len(), 1);
    // The thread shows under the line's last piece, before the next line.
    let rows_now = rows(&h);
    let at = rows_now.iter().position(|r| *r == DRow::Comment(0, all[0].id())).unwrap();
    assert!(matches!(rows_now[at - 1], DRow::More(0, 3, _)), "{rows_now:?}");
    assert_eq!(rows_now[at + 1], DRow::Row(0, 4));
    // A drag from the context line down into the pieces selects both lines.
    h.state_mut().state.set_comments("/r/repo-f", &[]);
    h.run_steps(2);
    press(&mut h, 2, 200.0, true, none);
    for y in [3, 4, 5, 6] {
        let pos = diff_row_pos(&h, y, 202.0);
        h.event(egui::Event::PointerMoved(pos));
        h.run_steps(2);
    }
    press(&mut h, 6, 202.0, false, none);
    let draft = &h.state().state.ask.as_ref().expect("the popup").comment;
    assert_eq!((draft.start_line, draft.line), (Some(1), 2));
    // Split: a continuation row's right half is the new side's line.
    h.key_press(egui::Key::Escape);
    h.run_steps(2);
    h.state_mut().state.saved.diff_split = true;
    h.run_steps(3);
    let right = h.state().state.diff.as_ref().unwrap().rows_rect.width() / 2.0 + 20.0;
    // Rows: 0 file, 1 hunk, 2 ctx, 3 (-short | +long), 4.. its pieces.
    assert!(matches!(rows(&h)[5], DRow::PairMore(_, _, 2)));
    press(&mut h, 5, right, true, none);
    press(&mut h, 5, right, false, none);
    let draft = &h.state().state.ask.as_ref().expect("the popup").comment;
    assert_eq!((draft.side.as_str(), draft.line), ("new", 2));
}

#[test]
fn scroll_to_a_file_and_next_previous_file_land_on_its_header() {
    let mut h = diff_app();
    // Wrapped lines in every file: the headers move down by their pieces.
    let text = sample_diff(4, 40).replace("compute_better(", &"compute_better_than_before(".repeat(30));
    open_diff(&mut h, crate::diff::parse(&text));
    assert!(rows(&h).iter().any(|r| matches!(r, DRow::More(..))));
    let header = |h: &Harness<'static, App>, fi: usize| rows(h).iter().position(|r| *r == DRow::File(fi)).unwrap();
    let top = |h: &Harness<'static, App>| h.state().state.diff.as_ref().unwrap().top;
    // The file list's jump: its header on top.
    h.get_by_label("src/f1.rs").click();
    h.run_steps(4);
    assert_eq!(top(&h), header(&h, 1));
    // ] and [ from there.
    h.key_press(egui::Key::CloseBracket);
    h.run_steps(4);
    assert_eq!(top(&h), header(&h, 2));
    h.key_press(egui::Key::OpenBracket);
    h.run_steps(4);
    assert_eq!(top(&h), header(&h, 1));
    h.key_press(egui::Key::OpenBracket);
    h.run_steps(4);
    assert_eq!(top(&h), header(&h, 0));
}

#[test]
fn a_narrower_pane_wraps_again_and_keeps_the_line_on_top() {
    let mut h = diff_app();
    let text = sample_diff(1, 200).replace("// unchanged line", &"// a long unchanged line ".repeat(12));
    open_diff(&mut h, crate::diff::parse(&text));
    h.state_mut().state.diff.as_mut().unwrap().scroll_to = Some(60);
    h.run_steps(4);
    let line = |h: &Harness<'static, App>| {
        let d = h.state().state.diff.as_ref().unwrap();
        match &d.rows[d.top] {
            DRow::Row(f, r) | DRow::More(f, r, _) => (*f, *r),
            other => panic!("{other:?}"),
        }
    };
    let before = (line(&h), rows(&h).len());
    // The sidebar collapses: wider panes, fewer rows, the same line on top.
    h.key_press_modifiers(egui::Modifiers::CTRL, egui::Key::B);
    h.run_steps(5);
    assert!(rows(&h).len() < before.1, "{} vs {}", rows(&h).len(), before.1);
    assert_eq!(line(&h), before.0);
}

#[test]
fn the_file_list_hides_behind_a_rail_and_comes_back() {
    let mut h = diff_app();
    open_diff(&mut h, crate::diff::parse(&sample_diff(2, 80)));
    let width = |h: &Harness<'static, App>| h.state().state.diff.as_ref().unwrap().rows_rect.width();
    let with_list = width(&h);
    assert!(h.query_by_label("src/f1.rs").is_some());
    h.get_by_label("◂").click();
    h.run_steps(3);
    assert!(h.state().state.saved.diff_files_hidden);
    assert!(h.query_by_label("src/f1.rs").is_none(), "the list is gone");
    assert!(width(&h) > with_list + 150.0, "the diff takes the room: {} vs {with_list}", width(&h));
    // The rail's ▼ is the next file.
    h.get_by_label("▼").click();
    h.run_steps(4);
    let d = h.state().state.diff.as_ref().unwrap();
    assert_eq!(d.rows[d.top], DRow::File(1));
    // ▸ (or f) brings it back.
    h.get_by_label("▸").click();
    h.run_steps(3);
    assert!(!h.state().state.saved.diff_files_hidden);
    assert!(h.query_by_label("src/f1.rs").is_some());
    h.key_press(egui::Key::F);
    h.run_steps(3);
    assert!(h.state().state.saved.diff_files_hidden);
    // Also from the View menu.
    h.get_by_label("View").click();
    h.run_steps(2);
    h.get_by_label("Diff file list  (f)").click();
    h.run_steps(3);
    assert!(!h.state().state.saved.diff_files_hidden);
}

#[test]
fn the_sidebar_collapses_on_every_screen() {
    let mut h = diff_app();
    h.get_by_label("«").click();
    h.run_steps(3);
    assert!(h.state().state.saved.sidebar_hidden);
    assert!(h.query_by_label("+ New").is_none() && h.query_by_label("«").is_none(), "only the rail");
    h.get_by_label("»").click();
    h.run_steps(3);
    assert!(!h.state().state.saved.sidebar_hidden);
    assert!(h.query_by_label("+ New").is_some() && h.query_by_label("»").is_none());
    // Ctrl+B anywhere: Home, the guide, the diff.
    for go in [crate::app::View::Home, crate::app::View::Guide, crate::app::View::Worktrees] {
        h.state_mut().state.view = go;
        h.run_steps(2);
        h.key_press_modifiers(egui::Modifiers::CTRL, egui::Key::B);
        h.run_steps(3);
        assert!(h.state().state.saved.sidebar_hidden, "{go:?}");
        assert!(h.query_by_label("»").is_some() && h.query_by_label("+ New").is_none(), "{go:?}");
        h.key_press_modifiers(egui::Modifiers::CTRL | egui::Modifiers::SHIFT, egui::Key::B);
        h.run_steps(3);
        assert!(!h.state().state.saved.sidebar_hidden, "{go:?}");
    }
    // The View menu item, and Ctrl+K (focus the sidebar) expands it.
    h.get_by_label("View").click();
    h.run_steps(2);
    h.get_by_label("Worktrees sidebar  (Ctrl+B)").click();
    h.run_steps(3);
    assert!(h.state().state.saved.sidebar_hidden);
    h.key_press_modifiers(egui::Modifiers::CTRL, egui::Key::K);
    h.run_steps(3);
    assert!(!h.state().state.saved.sidebar_hidden);
}

#[test]
fn wrap_and_both_collapses_survive_a_restart() {
    // Missing from an older state file: wrapped, list and sidebar shown.
    let old: crate::persist::Saved = serde_json::from_str(r#"{"diff_split": true}"#).unwrap();
    assert!(!old.diff_nowrap && !old.diff_files_hidden && !old.sidebar_hidden);
    let dir = tempfile::tempdir().unwrap();
    let path = crate::persist::path("/r/repo", &dir_env(&dir));
    let saved = crate::persist::Saved {
        diff_nowrap: true,
        diff_files_hidden: true,
        sidebar_hidden: true,
        ..Default::default()
    };
    let mut saver = crate::persist::Saver::new(path);
    saver.touch();
    saver.flush(&saved);
    let (tx, _rx) = mpsc::channel();
    let conn = Conn::new("/r/repo".into(), dir_env(&dir), tx, egui::Context::default());
    let state = State::new(conn, FontSet::bundled_only(), None);
    assert!(state.saved.diff_nowrap && state.saved.diff_files_hidden && state.saved.sidebar_hidden);
}

/// docs/design/desktop-screenshots/d2-*.png: a long line wrapped in Split,
/// then with the file list and the sidebar collapsed (dark), and Unified
/// wrapped (light).
#[test]
fn d2_screenshots() {
    let out = std::env::temp_dir();
    let (app, _tx) = app(vec![
        worktree("/r/repo", "main", vec![]),
        worktree("/r/repo-f", "feat-filters", vec![agent("a1", "Implementer", "waiting", "s1")]),
    ]);
    let mut h = Harness::builder().with_size([1400.0, 700.0]).build_ui_state(|ui, app: &mut App| app.draw(ui), app);
    h.run_steps(2);
    let text = "diff --git a/src/filters.py b/src/filters.py\n--- a/src/filters.py\n+++ b/src/filters.py\n@@ -1,6 +1,7 @@\n def matching(rows, criteria):\n-    \"\"\"Returns the rows that match.\"\"\"\n+    \"\"\"Returns the rows that match all criteria within one pass of the list, keeping their order; a row with a missing field never matches, and an empty criteria list matches every row.\"\"\"\n     out = []\n     for row in rows:\n-        if all(c(row) for c in criteria):\n+        if criteria and all(c(row) for c in criteria) or not criteria and row is not None and the_fields_are_present(row):\n             out.append(row)\n     return out\n";
    let parsed = crate::diff::parse(text);
    let mut c = crate::diff::anchor("src/filters.py", &parsed.files[0].rows[3]).unwrap();
    c.text = "Say what a row with a missing field does".into();
    open_diff(&mut h, parsed);
    h.state_mut().state.set_comments("/r/repo-f", &[c]);
    h.state_mut().state.saved.diff_split = true;
    h.run_steps(3);
    let shot = |h: &mut Harness<'static, App>, name: &str| match h.render() {
        Ok(image) => {
            image.save(out.join(format!("lazyagent-desktop-{name}.png"))).unwrap();
            true
        }
        Err(e) if std::env::var("LAZYAGENT_REQUIRE_GPU").as_deref() == Ok("1") => panic!("{e}"),
        Err(_) => false,
    };
    if !shot(&mut h, "d2-wrap-split-dark") {
        return;
    }
    h.state_mut().state.saved.diff_files_hidden = true;
    h.state_mut().state.saved.sidebar_hidden = true;
    h.run_steps(4);
    shot(&mut h, "d2-collapsed-dark");
    let ctx = h.ctx.clone();
    h.state_mut().state.toggle_theme(&ctx);
    h.state_mut().state.saved.diff_split = false;
    h.state_mut().state.saved.diff_files_hidden = false;
    h.state_mut().state.saved.sidebar_hidden = false;
    h.run_steps(4);
    shot(&mut h, "d2-wrap-unified-light");
}
