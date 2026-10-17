// The keyboard table the help sheet (?) is generated from: every TUI binding
// (app.py, worktree_list.py and the modals; `tui` holds their Textual key
// names, which a test checks against LazyAgent.BINDINGS) and the page's own
// keys. ui-plan.md §7a is the design.
//
// `where` says who acts on the key today: "tui" = the hosted TUI, when its
// pane has the keyboard; "page" = this page.

export const SCOPES = {
  global: "Anywhere",
  sidebar: "Sidebar",
  outside: "Outside a terminal",
  modal: "In a dialog",
};

export const KEYMAP = [
  { keys: ["Ctrl+K"], tui: ["ctrl+k"], action: "Focus the sidebar", scope: "global", where: "tui" },
  { keys: ["Ctrl+J"], tui: ["ctrl+j"], action: "Focus the active agent", scope: "global", where: "tui" },
  { keys: ["Ctrl+D"], tui: ["ctrl+d"], action: "Show the diff", scope: "global", where: "tui" },
  { keys: ["Ctrl+L"], tui: ["ctrl+l"], action: "Focus the worktree shell", scope: "global", where: "tui" },
  {
    keys: ["Alt+]", "Alt+N"],
    tui: ["alt+right_square_bracket", "alt+n"],
    action: "Next agent tab",
    scope: "global",
    where: "tui",
  },
  {
    keys: ["Alt+[", "Alt+P"],
    tui: ["alt+left_square_bracket", "alt+p"],
    action: "Previous agent tab",
    scope: "global",
    where: "tui",
  },
  { keys: ["Alt+G"], tui: ["alt+g"], action: "Refresh git status", scope: "global", where: "tui" },
  { keys: ["J", "K"], tui: ["j", "k"], action: "Next / previous worktree", scope: "sidebar", where: "tui" },
  { keys: ["S"], tui: ["s"], action: "Spawn an agent", scope: "outside", where: "tui" },
  { keys: ["X"], tui: ["x"], action: "Stop the agent", scope: "outside", where: "tui" },
  { keys: ["C"], tui: ["c"], action: "Create a worktree", scope: "outside", where: "tui" },
  { keys: ["D"], tui: ["d"], action: "Remove the worktree", scope: "outside", where: "tui" },
  { keys: ["R"], tui: ["r"], action: "Refresh the worktree list", scope: "outside", where: "tui" },
  { keys: ["?"], tui: ["question_mark"], action: "This help (the TUI has its own)", scope: "outside", where: "page" },
  {
    keys: ["Q"],
    tui: ["q"],
    action: "Quit lazyagent: no key in the page (it stops every agent); use q in the TUI",
    scope: "outside",
    where: "tui",
  },
  {
    keys: ["N", "D", "R", "L", "Backspace"],
    tui: ["n", "d", "r", "l", "backspace"],
    action: "Spawn dialog: normal, skip permissions, resume (pick / last), new session",
    scope: "modal",
    where: "tui",
  },
  { keys: ["Y", "N"], tui: ["y", "n"], action: "Confirm dialog: yes / no", scope: "modal", where: "tui" },
  { keys: ["F"], tui: ["f"], action: "Remove dialog: toggle force", scope: "modal", where: "tui" },
  { keys: ["Esc"], tui: ["escape"], action: "Close a dialog (this help too)", scope: "modal", where: "page" },
];
