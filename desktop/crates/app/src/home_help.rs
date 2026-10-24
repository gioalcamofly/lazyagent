//! The Home guide: `?` on Home and Help → Home guide. The same text is
//! docs/guides/home.md (a test keeps their headings and keys in step).

use eframe::egui::{self, RichText, Ui};

use crate::theme::Theme;

/// Sections of the guide: a heading and its paragraphs (a leading "- " is a
/// bullet).
pub const GUIDE: &[(&str, &[&str])] = &[
    (
        "What Home is",
        &["One line per work item: a branch with its PR, its agents and its CI. The engine derives every \
             line from facts (agent status, PR, CI, git), so nothing on Home is a board to keep up to date. \
             The items that need you come first; everything else is In flight below."],
    ),
    (
        "The zones",
        &[
            "- Now: an agent is blocked on you (a tool approval, a question). Shown at once.",
            "- Round: quick things that need you but block nobody (CI red with no agent on it, a conflict, \
             a merge-ready PR, a PR with no reviewer, an item the orchestrator handed back). Delivered in \
             rounds, see below.",
            "- To sit down with: things that need reading (a decision brief, changes requested, a finished \
             branch to review). Shown at once, outside the rounds, against your limit (deep_max).",
            "- In flight: everything whose ball is not yours, with who has it and since when. Done rows \
             (merged, closed) stay a day, dimmed.",
            "- Parked: work items you parked, with their inbox items, folded until you open it (Shift+Z). \
             A parked line says when it comes back, or why it waits, and \"needs you\" when one of its items \
             is still yours. Items a planner holds in its own Waiting lane wait here too, as \"waiting \
             (board)\" or \"waiting: <reason>\".",
        ],
    ),
    (
        "The ball",
        &["Who must move next. you: the line is in your inbox. agent: an agent works on it, or the \
             orchestrator holds it. others: a reviewer or a teammate (or your label says so). stalled: \
             nobody runs and nothing moves (no agent, an agent to restore, no activity for a day). done: \
             merged, closed, or already merged into develop or main (with a suggestion to close the PR)."],
    ),
    (
        "The states",
        &["The state is a short fact, never a guess: \"approve: npm test\", \"CI red: tests\", \"conflict with \
             base\", \"merge: approved, CI green\", \"review requested\", \"PR has no reviewer\", \"orchestrator \
             · looking\", \"orchestrator · verifying tests\", \"agent a1 · on it\", \"agent a1 finished; still CI \
             red\", \"parked until Tue 08:00\", \"waiting (board)\". A label you set shows before it: \"QA · CI \
             running\"; an open dependency after it: \"Edit · waits on Cart totals\"."],
    ),
    (
        "Rounds, and changed since your last look",
        &[
            "Now lines show at once. A line entering Round waits for a cut point (you dismiss, park, pass \
             or take back an item; you come back from a terminal; or 30 minutes pass); until then it stays \
             where it was, marked \"you · next round\", and the Round header counts it (\"next round: N\"). \
             Between cut points nothing moves under your cursor.",
            "A dot (•) marks a line whose timeline changed since you last looked at it. Expanding a line \
             shows \"Since you looked\" and marks it seen; m marks one line, Shift+M everything.",
        ],
    ),
    (
        "The decision view",
        &[
            "Enter (or v) on a brief opens it: the question and its options on the left, the recommendation \
             preselected (or the orchestrator's suggestion when it handed the brief to you), what the agent \
             tried, how reversible it is and what goes wrong if the choice is wrong, and a comment box. The \
             evidence is on the right, one tab per piece: a diff hunk, a log's or a file's lines, a notes \
             section, a link; the item's timeline is under it. \"Outdated\" means the branch moved since the \
             brief.",
            "Answer (Ctrl+Enter) sends \"Decision d3: B — <label>. <comment>\" to the agent once its turn is \
             over, never in the middle of one, and adds it to the agent's notes (Decisions), so a restored \
             agent still knows. A decision the orchestrator took shows as \"decided by orchestrator\" with \
             Revert.",
            "- Explain this (y): the owner agent explains in at most 10 lines. When it is not running but can \
             be restored, the view offers \"Restore agent and explain\" (Shift+Y); it is never restored on \
             its own. With neither, the orchestrator explains; with nobody, the button is disabled and Open \
             terminal and Pass to orchestrator are there instead. y works on any Home line too.",
            "- Request a new brief (b): through the same chain, with a note; the agent answers with a new \
             brief that replaces this one.",
        ],
    ),
    (
        "Pass to orchestrator",
        &[
            "Shift+O passes the item to the orchestrator for two hours: the line reads \"orchestrator · \
             looking\" in In flight. When it is done it releases the item with a note that stays on the row \
             (\"orchestrator: re-ran CI\"); if it hands the item back, the item returns to Round with that \
             note as its state. u takes a pass back. A pass that runs out comes back as \"delegation lapsed\".",
            "The orchestrator can also hold an item itself while it verifies a worker's branch or waits on \
             your merge (\"orchestrator · <note>\"), and the engine holds the finished work of the agents it \
             spawned for an hour first. If it does not release them, they come to you as \"orchestrator did \
             not respond\".",
        ],
    ),
    (
        "What the orchestrator may do",
        &[
            "- Presence: you count as present while you use the app (input in the last 15 minutes), as \
             away otherwise; the bar on top sets it (auto, present, away).",
            "- Mandate: while you are present it answers only small reversible questions and relays, \
             restores agents or re-runs CI; while you are away it may also answer reversible and partly \
             reversible briefs, rebase through the owner agent and spawn workers within the caps. A brief \
             that is not reversible always comes to you. A passed item allows the away list.",
            "- Never alone: merge to a default or protected branch, publish (tag, release, package), \
             force-push a shared branch, delete a branch or worktree with unpushed work, close or reopen a \
             PR. Those are always yours.",
            "- Revert: a decision the orchestrator took shows on its row for a day; Shift+U reverts it, \
             tells the agent your choice, stops what it spawned and resets the branch while nothing was \
             pushed.",
            "- Keep for me (the engine's keep_for_me): nothing on that item is routed to the orchestrator.",
        ],
    ),
    (
        "Projects, priority and not started",
        &["A planner or a script can feed Home the tasks it tracks (work_items_upsert). A task with no \
             branch yet counts in In flight's header (\"3 not started\"), together with the issues assigned \
             to you in a tracker. Once a branch carries its ticket key (SHOP-12-checkout), the task joins \
             that line with its title and priority. In flight groups lines by project under a header \
             (\"▸ Checkout revamp · 3\"); a line whose dependency is still open or in review reads \"waits \
             on <title>\". High priority shows ▴ before the name and low ▾; priority orders lines inside a \
             level, after anything that blocks an agent. A project that \"continues while in review\" lets \
             the orchestrator start its next task once the dependencies are merged or in review: alone \
             while you are away, as a proposal while you are present. u on a waiting line unparks it until \
             the planner changes it."],
    ),
    (
        "Delegate to the branch's agent",
        &["d hands the line's item to the agent already on the branch (CI red, a conflict, a failed \
             run): the line reads \"agent a1 · on it\" in In flight. If the agent asks you something, that \
             question shows instead. If it finishes its turn and the fact still holds, the item comes back to \
             Round as \"agent finished; still <fact>\"."],
    ),
    (
        "Auto-delegation",
        &["Off by default. In Auto-delegation (top bar) you choose which stalled kinds the engine hands \
             over on its own (a conflict with the base branch, an agent that died) and to whom (the \
             branch's owner agent or the orchestrator). It tries at most twice per item, never on main or a \
             teammate's branch; after two failed tries the item comes to To sit down with with what was \
             tried."],
    ),
    (
        "Getting the most out of Home",
        &[
            "- Work in rounds. Answer Now at once, then take the Round when it is delivered rather than \
             reacting to every line as it appears.",
            "- Park and label. Park what cannot move today (until tomorrow, until something changes) and \
             label what waits on a person (others · QA): your inbox then holds only what you can act on.",
            "- Pass routine items. A red CI, a rebase or a relay is the orchestrator's or the branch \
             agent's work: Shift+O or d, and read its note when it comes back.",
            "- Set your presence. Mark yourself away before a meeting so the orchestrator may decide more \
             alone, and present when you want to see its proposals first.",
            "- Sit down with the deep items in a focus block, not between two Now items.",
        ],
    ),
];

/// Home's keys, for the guide's table.
pub const HOME_KEYS: &[(&str, &str)] = &[
    ("J K ↓ ↑", "Next / previous line"),
    ("Enter", "Answer a Now line (its terminal); open a brief's decision view; expand any other"),
    ("V", "The decision view of the line's brief (or of the orchestrator's decision)"),
    ("Y  Shift+Y", "Explain this: by its agent or the orchestrator / restore its agent and explain"),
    ("O", "The line's agent terminal"),
    ("N", "Its session notes"),
    ("P", "Its PR (or tracker task)"),
    ("X", "Dismiss the line's item (this occurrence, for every client)"),
    ("Z", "Park: 1 h, tomorrow 08:00, 1 week, until something changes, indefinitely (keys 1-5; W the whole work item)"),
    ("Shift+Z", "Show / hide Parked"),
    ("Shift+O", "Pass to the orchestrator"),
    ("U", "Take a pass or hold back, or clear your label, park, close or delegation"),
    ("D", "Delegate the line's item to the branch's agent"),
    ("L", "Label who has the ball (e.g. others · QA)"),
    ("Shift+X", "Close the work item as done or dropped"),
    ("M  Shift+M", "Mark the line seen / everything seen"),
    ("Shift+U", "Revert the orchestrator's decision on the line"),
    ("F", "Fold In flight"),
    ("G W  G H", "The worktrees / back to Home"),
    ("?", "This guide"),
];

/// The decision view's keys.
pub const DECISION_KEYS: &[(&str, &str)] = &[
    ("1-4", "Pick an option"),
    ("Ctrl+Enter", "Answer"),
    ("]  [", "Next / previous evidence tab"),
    ("Y  Shift+Y", "Explain this / restore its agent and explain"),
    ("B", "Request a new brief"),
    ("O", "The agent's terminal"),
    ("Shift+U", "Revert the orchestrator's decision"),
    ("Esc", "Back to Home"),
];

/// The guide, drawn (in the Home guide dialog).
pub fn show(ui: &mut Ui, t: &Theme) {
    for (heading, paragraphs) in GUIDE {
        ui.add_space(6.0);
        ui.label(RichText::new(*heading).strong().color(t.accent));
        for p in *paragraphs {
            let text = p.split_whitespace().collect::<Vec<_>>().join(" ");
            match text.strip_prefix("- ") {
                Some(bullet) => {
                    ui.horizontal_wrapped(|ui| {
                        ui.label(RichText::new("•").color(t.muted));
                        ui.label(bullet);
                    });
                }
                None => {
                    ui.label(text);
                }
            }
        }
    }
    ui.add_space(8.0);
    ui.label(RichText::new("Keys").strong().color(t.accent));
    egui::Grid::new("home-help-keys").num_columns(2).striped(true).spacing([14.0, 4.0]).show(ui, |ui| {
        for (keys, what) in HOME_KEYS {
            ui.label(RichText::new(*keys).monospace().color(t.accent));
            ui.label(*what);
            ui.end_row();
        }
    });
    ui.add_space(8.0);
    ui.label(RichText::new("Keys in the decision view").strong().color(t.accent));
    egui::Grid::new("home-help-decision-keys").num_columns(2).striped(true).spacing([14.0, 4.0]).show(ui, |ui| {
        for (keys, what) in DECISION_KEYS {
            ui.label(RichText::new(*keys).monospace().color(t.accent));
            ui.label(*what);
            ui.end_row();
        }
    });
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn the_guide_and_docs_guides_home_md_say_the_same() {
        let path = concat!(env!("CARGO_MANIFEST_DIR"), "/../../../docs/guides/home.md");
        let doc = std::fs::read_to_string(path).unwrap();
        for (heading, _) in GUIDE {
            assert!(doc.contains(&format!("## {heading}")), "docs/guides/home.md lacks ## {heading}");
        }
        for (keys, _) in HOME_KEYS.iter().chain(DECISION_KEYS) {
            assert!(doc.contains(&format!("`{keys}`")), "docs/guides/home.md lacks the key `{keys}`");
        }
    }
}
